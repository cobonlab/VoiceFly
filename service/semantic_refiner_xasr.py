"""Read-only adapter for VoiceFly's production streaming X-ASR path.

The adapter deliberately does not construct ``VoiceService`` and never opens
an input device.  It imports the service lazily, reuses the production model
and recognizer policy, binds an already-existing validated hotword snapshot,
and replays an in-memory ``AudioSnapshot`` in the service's 100 ms cadence.
It never invokes the production resolver because that resolver may rebuild a
user-cache snapshot; runtime-snapshot equality is therefore explicitly marked
unproven in its identity.  Final normalization uses WeText's memory-only ITN
mode so a cold evaluation process cannot populate the user's FST cache.
"""

from __future__ import annotations

import copy
import hashlib
import importlib
from importlib import metadata as importlib_metadata
import json
import math
import os
import re
import stat
import threading
from pathlib import Path
from typing import Any, Callable, Mapping

import numpy as np

from voice_refiner import AudioSnapshot
from voice_hotwords import (
    COMBINED_HOTWORDS_ENV,
    DEFAULT_HOTWORDS_SCORE,
    DEFAULT_MAX_ACTIVE_PATHS,
    DISABLE_ENV,
    DecoderConfig,
    bpe_vocab_candidates,
    hotword_candidates,
    validate_bpe_vocab,
    validate_hotwords,
)


FAMILY = "voicefly_x_asr"
FRAME_DURATION_SECONDS = 0.1
REQUIRED_MODEL_FILES = (
    "tokens.txt",
    "encoder.int8.onnx",
    "decoder.onnx",
    "joiner.int8.onnx",
)
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_NORMALIZATION_PREWARM_TEXT = "W三五六 温度二十摄氏度"
_MAX_NORMALIZATION_RESOURCE_ENTRIES = 100_000
_MAX_NORMALIZATION_RESOURCE_IDENTITY_BYTES = 8 * 1024 * 1024
_SAFE_RUNTIME_VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+!-]{0,127}")
_PRIVATE_RESOURCE_IDENTITY_STATUS = "revalidated_not_exported"
_PRIVATE_NORMALIZATION_IDENTITY_FIELDS = (
    "spoken_number_exceptions",
    "engineering_symbol_aliases",
    "personal_phonetics_resource_present",
    "personal_phonetics_resource_sha256",
)
_PRIVATE_XASR_IDENTITY_FIELDS = (
    "requested_decoder_sha256",
    "effective_decoder_sha256",
    "decoder_snapshot_source",
    "lexicon_sha256",
    "lexicon_entries",
    "lexicon_bytes",
)


class VoiceFlyXASRError(RuntimeError):
    """A machine-readable provider failure without transcript contents."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class VoiceFlyXASRLoadError(VoiceFlyXASRError):
    """The production model/configuration could not be loaded faithfully."""


class VoiceFlyXASRStreamError(VoiceFlyXASRError):
    """One in-memory recognition stream could not be run."""


def _stable_file_fingerprint(path: Path) -> dict[str, Any]:
    """Hash one non-symlink regular file through one stable descriptor."""
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise OSError("not a regular file")
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            digest.update(chunk)
        after = os.fstat(descriptor)
    except OSError as exc:
        raise VoiceFlyXASRLoadError("x_asr_identity_read_failed") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    signature_before = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    signature_after = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if signature_before != signature_after or total != after.st_size:
        raise VoiceFlyXASRLoadError("x_asr_identity_changed_during_read")
    return {
        "sha256": digest.hexdigest(),
        "bytes": after.st_size,
        "device": after.st_dev,
        "inode": after.st_ino,
        "mtime_ns": after.st_mtime_ns,
        "ctime_ns": after.st_ctime_ns,
    }


def _sha256_file(path: Path) -> tuple[str, int]:
    fingerprint = _stable_file_fingerprint(path)
    return str(fingerprint["sha256"]), int(fingerprint["bytes"])


def _stable_model_fingerprint(path: Path) -> dict[str, Any]:
    """Return content plus stat identity for one model artifact."""
    return _stable_file_fingerprint(path)


def _same_file_fingerprint(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
) -> bool:
    keys = ("sha256", "bytes", "device", "inode", "mtime_ns", "ctime_ns")
    return all(left.get(key) == right.get(key) for key in keys)


def _decoder_asset_fingerprint(
    decoder: Any,
    attribute: str,
    *,
    missing_code: str,
) -> dict[str, Any] | None:
    if not bool(getattr(decoder, "enabled", False)):
        return None
    value = getattr(decoder, attribute, None)
    if value is None:
        raise VoiceFlyXASRLoadError(missing_code)
    return _stable_file_fingerprint(Path(value))


def _safe_decoder_record(
    decoder: Any,
    *,
    bpe_fingerprint: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return only reproducible decoder fields; never reasons or paths."""
    bpe_path = getattr(decoder, "bpe_vocab", None)
    bpe_sha256 = None
    if bool(getattr(decoder, "enabled", False)):
        if bpe_path is None:
            raise VoiceFlyXASRLoadError("x_asr_decoder_bpe_missing")
        if bpe_fingerprint is None:
            bpe_fingerprint = _stable_file_fingerprint(Path(bpe_path))
        bpe_sha256 = str(bpe_fingerprint["sha256"])
    return {
        "enabled": bool(getattr(decoder, "enabled", False)),
        "decoding_method": str(getattr(decoder, "decoding_method", "")),
        "max_active_paths": getattr(decoder, "max_active_paths", None),
        "modeling_unit": getattr(decoder, "modeling_unit", None),
        "hotwords_score": getattr(decoder, "hotwords_score", None),
        "bpe_vocab_sha256": bpe_sha256,
    }


def _record_sha256(record: Mapping[str, Any]) -> str:
    payload = json.dumps(
        dict(record),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _distribution_version(name: str) -> str:
    try:
        version = importlib_metadata.version(name)
    except (importlib_metadata.PackageNotFoundError, OSError, ValueError) as exc:
        raise VoiceFlyXASRLoadError("x_asr_runtime_version_unavailable") from exc
    if not isinstance(version, str) or _SAFE_RUNTIME_VERSION_RE.fullmatch(version) is None:
        raise VoiceFlyXASRLoadError("x_asr_runtime_version_invalid")
    return version


def _semantic_resource_identity(
    role: str,
    entries: Any,
    *,
    row_width: int,
) -> dict[str, Any]:
    """Hash loaded normalization semantics without returning their text."""
    if (
        not isinstance(entries, (tuple, list))
        or len(entries) > _MAX_NORMALIZATION_RESOURCE_ENTRIES
    ):
        raise VoiceFlyXASRLoadError("x_asr_normalization_resource_invalid")
    canonical_entries: list[list[str]] = []
    encoded_entry_bytes = 0

    def append_entry(entry: list[str]) -> None:
        nonlocal encoded_entry_bytes
        if (
            sum(len(item) for item in entry)
            > _MAX_NORMALIZATION_RESOURCE_IDENTITY_BYTES
        ):
            raise VoiceFlyXASRLoadError(
                "x_asr_normalization_resource_invalid"
            )
        encoded = json.dumps(
            entry,
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
        encoded_entry_bytes += len(encoded) + 1
        if encoded_entry_bytes > _MAX_NORMALIZATION_RESOURCE_IDENTITY_BYTES:
            raise VoiceFlyXASRLoadError(
                "x_asr_normalization_resource_invalid"
            )
        canonical_entries.append(entry)

    if row_width == 1:
        for entry in entries:
            if not isinstance(entry, str):
                raise VoiceFlyXASRLoadError(
                    "x_asr_normalization_resource_invalid"
                )
            append_entry([entry])
    else:
        for entry in entries:
            if (
                not isinstance(entry, (tuple, list))
                or len(entry) != row_width
                or not all(isinstance(item, str) for item in entry)
            ):
                raise VoiceFlyXASRLoadError(
                    "x_asr_normalization_resource_invalid"
                )
            append_entry(list(entry))
    payload = json.dumps(
        {
            "schema": "voicefly-normalization-resource-v1",
            "role": role,
            "entries": canonical_entries,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(payload) > _MAX_NORMALIZATION_RESOURCE_IDENTITY_BYTES:
        raise VoiceFlyXASRLoadError("x_asr_normalization_resource_invalid")
    return {
        "entry_count": len(canonical_entries),
        "semantic_sha256": hashlib.sha256(payload).hexdigest(),
    }


def _truthy(value: str | None) -> bool:
    return (value or "").strip().casefold() in {"1", "true", "yes", "on"}


def _first_valid_readonly(
    paths: Any,
    validator: Callable[[Path], tuple[bool, str]],
) -> Path | None:
    """Select a validated existing file without creating or replacing it."""
    seen: set[str] = set()
    for candidate in paths:
        path = Path(candidate)
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        try:
            valid, _detail = validator(path)
        except (OSError, UnicodeError, ValueError, TypeError):
            continue
        if valid:
            return path
    return None


def _resolve_decoder_readonly(
    model_dir: Path,
    resource_root: Path,
    source_root: Path,
    environment: Mapping[str, str],
) -> tuple[DecoderConfig, str]:
    """Resolve an existing snapshot without invoking the writing resolver.

    The production resolver can rebuild ``combined_hotwords.txt``.  An
    evaluator must not do that, so this function only validates files already
    on disk.  It prefers the existing combined snapshot, then an immutable
    base hotword resource.  Neither can prove equality with another running
    service process; that limitation is carried in provider identity.
    """
    if _truthy(environment.get(DISABLE_ENV)):
        return DecoderConfig(False, "read-only environment disable", "greedy_search"), "environment_disabled"

    bpe = _first_valid_readonly(
        bpe_vocab_candidates(model_dir, resource_root, source_root, environment),
        validate_bpe_vocab,
    )
    explicit_combined = environment.get(COMBINED_HOTWORDS_ENV, "").strip()
    combined_candidates = []
    if explicit_combined:
        combined_candidates.append(Path(explicit_combined).expanduser())
    combined_candidates.append(
        Path.home() / "Library/Caches/VoiceFly/lexicon/combined_hotwords.txt"
    )
    hotwords = _first_valid_readonly(combined_candidates, validate_hotwords)
    source = "existing_combined_snapshot"
    if hotwords is None:
        hotwords = _first_valid_readonly(
            hotword_candidates(resource_root, source_root, environment),
            validate_hotwords,
        )
        source = "existing_base_snapshot"
    if bpe is None or hotwords is None:
        return (
            DecoderConfig(
                False,
                "read-only decoder resources unavailable",
                "greedy_search",
                bpe_vocab=bpe,
                hotwords_file=hotwords,
            ),
            "resources_unavailable",
        )
    return (
        DecoderConfig(
            True,
            "validated existing read-only snapshot",
            "modified_beam_search",
            max_active_paths=DEFAULT_MAX_ACTIVE_PATHS,
            modeling_unit="bpe",
            bpe_vocab=bpe,
            hotwords_file=hotwords,
            hotwords_score=DEFAULT_HOTWORDS_SCORE,
        ),
        source,
    )


def _normalization_identity(service: Any) -> dict[str, Any]:
    """Identify production normalization code and loaded semantic resources."""
    policy_version = str(getattr(service, "ITN_POLICY_VERSION", ""))
    if not policy_version:
        raise VoiceFlyXASRLoadError("x_asr_itn_policy_identity_missing")

    active_sha256 = ""
    personal_function = getattr(service, "apply_personal_phonetic_corrections", None)
    module_name = getattr(personal_function, "__module__", "")
    personal_module_sha256 = None
    if module_name:
        try:
            personal_module = importlib.import_module(module_name)
            loader = getattr(personal_module, "load_personal_phrase_matcher", None)
            if callable(loader):
                loader()
            cache = getattr(personal_module, "_MATCHER_CACHE", {})
            if isinstance(cache, Mapping):
                candidate = cache.get("sha256", "")
                if isinstance(candidate, str):
                    active_sha256 = candidate
            personal_module_path = getattr(personal_module, "__file__", None)
            if isinstance(personal_module_path, str) and personal_module_path:
                personal_module_sha256, _ = _sha256_file(
                    Path(personal_module_path)
                )
        except BaseException as exc:
            raise VoiceFlyXASRLoadError(
                "x_asr_personal_phonetics_identity_failed"
            ) from exc
    if active_sha256 and (
        len(active_sha256) != 64
        or any(character not in "0123456789abcdef" for character in active_sha256)
    ):
        raise VoiceFlyXASRLoadError("x_asr_personal_phonetics_identity_invalid")

    normalizer_module_sha256 = None
    service_path = getattr(service, "__file__", None)
    if isinstance(service_path, str) and service_path:
        normalizer_module_sha256, _ = _sha256_file(Path(service_path))

    apply_engineering_symbols = getattr(service, "apply_engineering_symbols", None)
    spoken_number_loader = getattr(
        service,
        "load_spoken_number_exceptions",
        None,
    )
    voice_tool_module_name = getattr(
        apply_engineering_symbols,
        "__module__",
        "",
    )
    if not voice_tool_module_name:
        raise VoiceFlyXASRLoadError("x_asr_voice_tool_identity_missing")
    try:
        voice_tool_module = importlib.import_module(voice_tool_module_name)
        voice_tool_path = getattr(voice_tool_module, "__file__", None)
        engineering_alias_loader = getattr(
            voice_tool_module,
            "load_engineering_symbol_aliases",
            None,
        )
        if (
            not isinstance(voice_tool_path, str)
            or not voice_tool_path
            or not callable(spoken_number_loader)
            or not callable(engineering_alias_loader)
        ):
            raise VoiceFlyXASRLoadError("x_asr_voice_tool_identity_missing")
        voice_tool_module_sha256, _ = _sha256_file(Path(voice_tool_path))
        spoken_number_identity = _semantic_resource_identity(
            "spoken_number_exceptions",
            spoken_number_loader(),
            row_width=1,
        )
        engineering_alias_identity = _semantic_resource_identity(
            "engineering_symbol_aliases",
            engineering_alias_loader(),
            row_width=3,
        )
    except VoiceFlyXASRLoadError:
        raise
    except BaseException as exc:
        raise VoiceFlyXASRLoadError(
            "x_asr_voice_tool_identity_failed"
        ) from exc
    wetext_version = _distribution_version("WeTextProcessing")
    pynini_version = _distribution_version("pynini")
    return {
        "itn_policy_version": policy_version,
        "itn_cache_policy": "memory_only",
        "normalizer_module_sha256": normalizer_module_sha256,
        "voice_tool_module_sha256": voice_tool_module_sha256,
        "spoken_number_exceptions": spoken_number_identity,
        "engineering_symbol_aliases": engineering_alias_identity,
        "personal_phonetics_module_sha256": personal_module_sha256,
        "personal_phonetics_resource_present": bool(active_sha256),
        "personal_phonetics_resource_sha256": active_sha256 or _EMPTY_SHA256,
        "runtime_versions": {
            "WeTextProcessing": wetext_version,
            "pynini": pynini_version,
        },
    }


class VoiceFlyXASRProvider:
    """Replay snapshots through the production VoiceFly X-ASR recognizer.

    ``service_module``, ``recognizer_factory``, ``normalizer`` and
    ``environment`` are explicit test seams.  Without them, all native/service
    imports and model loading are deferred until the required call to
    :meth:`prepare`; :meth:`transcribe` fails closed before preparation.
    """

    name = FAMILY

    def __init__(
        self,
        *,
        service_module: Any | None = None,
        recognizer_factory: Callable[..., Any] | None = None,
        normalizer: Callable[[str], str] | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self._service_module = service_module
        self._recognizer_factory_override = recognizer_factory
        self._normalizer_override = normalizer
        self._environment = dict(os.environ if environment is None else environment)
        self._load_lock = threading.Lock()
        self._prepare_lock = threading.Lock()
        self._inference_lock = threading.Lock()
        self._loaded = threading.Event()
        self._prepared = threading.Event()
        self._recognizer: Any | None = None
        self._join_segments: Callable[[Any], str] | None = None
        self._normalizer: Callable[[str], str] | None = None
        self._hotword_snapshot: Any | None = None
        self._inline_hotword_supported = False
        self._identity: dict[str, Any] = {
            "family": FAMILY,
            "load_status": "not_loaded",
        }
        self._stream_hotword_fallback = False

    @property
    def identity(self) -> dict[str, Any]:
        """Return public identity without committing private resource state."""
        identity = copy.deepcopy(self._identity)
        private_identity_removed = False
        for field in _PRIVATE_XASR_IDENTITY_FIELDS:
            if field in identity:
                identity.pop(field)
                private_identity_removed = True
        normalization = identity.get("normalization")
        if isinstance(normalization, dict):
            for field in _PRIVATE_NORMALIZATION_IDENTITY_FIELDS:
                if field in normalization:
                    normalization.pop(field)
                    private_identity_removed = True
            normalization["private_resource_identity"] = (
                _PRIVATE_RESOURCE_IDENTITY_STATUS
            )
        if private_identity_removed:
            identity["private_resource_identity"] = (
                _PRIVATE_RESOURCE_IDENTITY_STATUS
            )
        identity["stream_hotword_fallback"] = self._stream_hotword_fallback
        if self._stream_hotword_fallback:
            identity["baseline_fidelity"] = "hotword_stream_approximation"
        return identity

    def _fail_load(self, code: str, exc: BaseException | None = None) -> None:
        self._identity = {
            "family": FAMILY,
            "load_status": "failed",
            "error_code": code,
        }
        error = VoiceFlyXASRLoadError(code)
        if exc is None:
            raise error
        raise error from exc

    def _service(self) -> Any:
        if self._service_module is not None:
            return self._service_module
        try:
            self._service_module = importlib.import_module("voice_fly_service")
        except BaseException as exc:
            self._fail_load("x_asr_service_import_failed", exc)
        return self._service_module

    def _model_identity(
        self,
        model_dir: Path,
    ) -> tuple[str, list[dict[str, Any]], dict[str, dict[str, Any]]]:
        missing = [name for name in REQUIRED_MODEL_FILES if not (model_dir / name).is_file()]
        if missing:
            self._fail_load("x_asr_model_files_missing")
        files: list[dict[str, Any]] = []
        stable_state: dict[str, dict[str, Any]] = {}
        composite = hashlib.sha256()
        for name in REQUIRED_MODEL_FILES:
            fingerprint = _stable_model_fingerprint(model_dir / name)
            digest = str(fingerprint["sha256"])
            byte_count = int(fingerprint["bytes"])
            files.append({"name": name, "bytes": byte_count, "sha256": digest})
            stable_state[name] = fingerprint
            composite.update(name.encode("utf-8"))
            composite.update(b"\0")
            composite.update(bytes.fromhex(digest))
        return composite.hexdigest(), files, stable_state

    def _ensure_loaded(self) -> None:
        if self._loaded.is_set():
            return
        with self._load_lock:
            if self._loaded.is_set():
                return
            service = self._service()
            required_attributes = (
                "MODEL_DIR",
                "RESOURCE_ROOT",
                "SOURCE_ROOT",
                "SAMPLE_RATE",
                "ENDPOINT_MIN_TRAILING_SILENCE_SECONDS",
                "ITN_POLICY_VERSION",
                "create_recognizer_with_fallback",
                "load_inline_hotword_snapshot",
                "EMPTY_HOTWORD_SNAPSHOT",
                "join_recognition_segments",
                "normalize",
                "apply_personal_phonetic_corrections",
                "apply_engineering_symbols",
                "load_spoken_number_exceptions",
            )
            if any(not hasattr(service, name) for name in required_attributes):
                self._fail_load("x_asr_service_contract_invalid")

            model_dir = Path(service.MODEL_DIR)
            try:
                model_sha256, model_files, model_state_before = self._model_identity(
                    model_dir
                )
            except VoiceFlyXASRLoadError as exc:
                self._fail_load(exc.code, exc)
            except BaseException as exc:
                self._fail_load("x_asr_model_identity_failed", exc)

            base_options = {
                "tokens": str(model_dir / "tokens.txt"),
                "encoder": str(model_dir / "encoder.int8.onnx"),
                "decoder": str(model_dir / "decoder.onnx"),
                "joiner": str(model_dir / "joiner.int8.onnx"),
                "num_threads": 4,
                "sample_rate": int(service.SAMPLE_RATE),
                "feature_dim": 80,
                "enable_endpoint_detection": True,
                "rule1_min_trailing_silence": float(
                    service.ENDPOINT_MIN_TRAILING_SILENCE_SECONDS
                ),
                "rule2_min_trailing_silence": float(
                    service.ENDPOINT_MIN_TRAILING_SILENCE_SECONDS
                ),
                "rule3_min_utterance_length": 20.0,
            }
            try:
                requested_decoder, readonly_decoder_source = _resolve_decoder_readonly(
                    model_dir,
                    Path(service.RESOURCE_ROOT),
                    Path(service.SOURCE_ROOT),
                    self._environment,
                )
                requested_bpe_before = _decoder_asset_fingerprint(
                    requested_decoder,
                    "bpe_vocab",
                    missing_code="x_asr_decoder_bpe_missing",
                )
                requested_record = _safe_decoder_record(
                    requested_decoder,
                    bpe_fingerprint=requested_bpe_before,
                )
            except VoiceFlyXASRLoadError as exc:
                self._fail_load(exc.code, exc)
            except BaseException as exc:
                self._fail_load("x_asr_readonly_decoder_resolution_failed", exc)

            factory = self._recognizer_factory_override
            if factory is None:
                try:
                    factory = service.sherpa_onnx.OnlineRecognizer.from_transducer
                except BaseException as exc:
                    self._fail_load("x_asr_recognizer_factory_missing", exc)
            recognizer = None
            effective_decoder = None
            recognizer_load_error: BaseException | None = None
            try:
                recognizer, effective_decoder = service.create_recognizer_with_fallback(
                    factory,
                    base_options,
                    requested_decoder,
                    logger=lambda _message: None,
                    include_hotwords_file=False,
                )
            except BaseException as exc:
                recognizer_load_error = exc
            try:
                requested_bpe_after = _decoder_asset_fingerprint(
                    requested_decoder,
                    "bpe_vocab",
                    missing_code="x_asr_decoder_bpe_missing",
                )
            except BaseException as exc:
                self._fail_load("x_asr_decoder_changed_during_load", exc)
            if (
                (requested_bpe_before is None) != (requested_bpe_after is None)
                or (
                    requested_bpe_before is not None
                    and requested_bpe_after is not None
                    and not _same_file_fingerprint(
                        requested_bpe_before,
                        requested_bpe_after,
                    )
                )
            ):
                self._fail_load("x_asr_decoder_changed_during_load")
            try:
                (
                    model_sha256_after,
                    model_files_after,
                    model_state_after,
                ) = self._model_identity(model_dir)
            except BaseException as exc:
                self._fail_load("x_asr_model_changed_during_load", exc)
            if (
                model_sha256_after != model_sha256
                or model_files_after != model_files
                or model_state_after != model_state_before
            ):
                self._fail_load("x_asr_model_changed_during_load")
            if recognizer_load_error is not None:
                self._fail_load("x_asr_recognizer_load_failed", recognizer_load_error)
            if recognizer is None or effective_decoder is None:
                self._fail_load("x_asr_recognizer_load_failed")

            try:
                effective_bpe = _decoder_asset_fingerprint(
                    effective_decoder,
                    "bpe_vocab",
                    missing_code="x_asr_decoder_bpe_missing",
                )
                if effective_bpe is not None and (
                    requested_bpe_before is None
                    or not _same_file_fingerprint(
                        requested_bpe_before,
                        effective_bpe,
                    )
                ):
                    raise VoiceFlyXASRLoadError(
                        "x_asr_effective_decoder_identity_mismatch"
                    )
                effective_record = _safe_decoder_record(
                    effective_decoder,
                    bpe_fingerprint=effective_bpe,
                )
                hotword_before = _decoder_asset_fingerprint(
                    effective_decoder,
                    "hotwords_file",
                    missing_code="x_asr_decoder_hotwords_missing",
                )
            except VoiceFlyXASRLoadError as exc:
                self._fail_load(exc.code, exc)
            except BaseException as exc:
                self._fail_load("x_asr_hotword_snapshot_load_failed", exc)

            hotword_snapshot = service.EMPTY_HOTWORD_SNAPSHOT
            hotword_load_error: BaseException | None = None
            if hotword_before is not None:
                try:
                    hotword_snapshot = service.load_inline_hotword_snapshot(
                        effective_decoder
                    )
                except BaseException as exc:
                    hotword_load_error = exc
            try:
                hotword_after = _decoder_asset_fingerprint(
                    effective_decoder,
                    "hotwords_file",
                    missing_code="x_asr_decoder_hotwords_missing",
                )
            except BaseException as exc:
                self._fail_load("x_asr_hotword_changed_during_snapshot", exc)
            if (
                (hotword_before is None) != (hotword_after is None)
                or (
                    hotword_before is not None
                    and hotword_after is not None
                    and not _same_file_fingerprint(
                        hotword_before,
                        hotword_after,
                    )
                )
            ):
                self._fail_load("x_asr_hotword_changed_during_snapshot")
            if hotword_load_error is not None:
                self._fail_load(
                    "x_asr_hotword_snapshot_load_failed",
                    hotword_load_error,
                )
            try:
                normalization_identity = _normalization_identity(service)
                sherpa_runtime_version = _distribution_version("sherpa-onnx")
            except VoiceFlyXASRLoadError as exc:
                self._fail_load(exc.code, exc)
            except BaseException as exc:
                self._fail_load("x_asr_normalization_identity_failed", exc)

            snapshot_sha256 = str(getattr(hotword_snapshot, "sha256", ""))
            if len(snapshot_sha256) != 64 or any(
                character not in "0123456789abcdef" for character in snapshot_sha256
            ):
                self._fail_load("x_asr_hotword_snapshot_invalid")
            snapshot_entries = getattr(hotword_snapshot, "entries", None)
            snapshot_bytes = getattr(hotword_snapshot, "byte_count", None)
            if (
                type(snapshot_entries) is not int
                or snapshot_entries < 0
                or type(snapshot_bytes) is not int
                or snapshot_bytes < 0
            ):
                self._fail_load("x_asr_hotword_snapshot_invalid")
            if hotword_after is not None and (
                snapshot_sha256 != hotword_after["sha256"]
                or snapshot_bytes != hotword_after["bytes"]
            ):
                self._fail_load("x_asr_hotword_snapshot_identity_mismatch")
            if hotword_after is None and (
                snapshot_sha256 != _EMPTY_SHA256
                or snapshot_entries != 0
                or snapshot_bytes != 0
            ):
                self._fail_load("x_asr_hotword_snapshot_identity_mismatch")

            decoder_fallback = (
                requested_record["enabled"] and not effective_record["enabled"]
            )
            if decoder_fallback:
                fidelity = "greedy_approximation"
            else:
                fidelity = "readonly_snapshot_approximation"

            if self._normalizer_override is not None:
                normalizer = self._normalizer_override
            else:
                production_normalizer = service.normalize
                expected_normalization_sha256 = _record_sha256(
                    normalization_identity
                )

                def normalize_without_diagnostics(text: str) -> str:
                    def assert_normalization_identity() -> None:
                        try:
                            current_identity = _normalization_identity(service)
                            current_sha256 = _record_sha256(current_identity)
                        except BaseException as exc:
                            raise VoiceFlyXASRStreamError(
                                "x_asr_normalization_identity_changed"
                            ) from exc
                        if current_sha256 != expected_normalization_sha256:
                            raise VoiceFlyXASRStreamError(
                                "x_asr_normalization_identity_changed"
                            )

                    assert_normalization_identity()
                    fallback_signaled = False

                    def mark_fallback(_message: object) -> None:
                        nonlocal fallback_signaled
                        fallback_signaled = True

                    try:
                        normalized = production_normalizer(
                            text,
                            diagnostic_logger=mark_fallback,
                            memory_only_itn=True,
                        )
                    except BaseException as exc:
                        try:
                            assert_normalization_identity()
                        except VoiceFlyXASRStreamError as identity_exc:
                            raise identity_exc from exc
                        raise VoiceFlyXASRStreamError(
                            "x_asr_normalization_failed"
                        ) from exc
                    assert_normalization_identity()
                    if fallback_signaled:
                        raise VoiceFlyXASRStreamError(
                            "x_asr_normalization_fallback"
                        )
                    if not isinstance(normalized, str):
                        raise VoiceFlyXASRStreamError(
                            "x_asr_normalizer_result_invalid"
                        )
                    return normalized

                normalizer = normalize_without_diagnostics
            non_path_recognizer_options = {
                key: value
                for key, value in base_options.items()
                if key not in {"tokens", "encoder", "decoder", "joiner"}
            }
            recognizer_identity_record = {
                "options": non_path_recognizer_options,
                "model_files": {
                    item["name"]: item["sha256"] for item in model_files
                },
            }
            identity = {
                "family": FAMILY,
                "load_status": "loaded",
                "runtime": {
                    "package": "sherpa-onnx",
                    "version": sherpa_runtime_version,
                },
                "model_sha256": model_sha256,
                "model_files": model_files,
                "recognizer_options_sha256": _record_sha256(
                    recognizer_identity_record
                ),
                "requested_decoder_sha256": _record_sha256(requested_record),
                "effective_decoder_sha256": _record_sha256(effective_record),
                "decoder_method": effective_record["decoding_method"],
                "decoder_configuration_match": requested_record == effective_record,
                "decoder_fallback": decoder_fallback,
                "decoder_snapshot_source": readonly_decoder_source,
                "runtime_snapshot_equivalence": "unproven",
                "lexicon_sha256": snapshot_sha256,
                "lexicon_entries": snapshot_entries,
                "lexicon_bytes": snapshot_bytes,
                "normalization": normalization_identity,
                "normalization_identity_revalidation": (
                    "before_and_after_every_production_normalize"
                    if self._normalizer_override is None
                    else "test_override_not_revalidated"
                ),
                "diagnostic_output_policy": "fallback_signal_fail_closed_without_text",
                "preparation": {
                    "available": True,
                    "required_before_transcription": True,
                    "recognizer_load": "before_sample_timing_when_called",
                    "canonicalizer_prewarm": (
                        "fixed_non_private_probe"
                        if self._normalizer_override is None
                        else "test_override_not_preheated"
                    ),
                    "included_in_sample_latency": False,
                    "supervision_boundary": "evaluation_process",
                },
                "baseline_fidelity": fidelity,
            }
            # Publish the recognizer last.  A concurrent caller can observe
            # only the previous unloaded state or this complete loaded state.
            self._recognizer = recognizer
            self._hotword_snapshot = hotword_snapshot
            self._inline_hotword_supported = bool(effective_record["enabled"])
            self._join_segments = service.join_recognition_segments
            self._normalizer = normalizer
            self._identity = identity
            self._loaded.set()

    def prepare(self) -> None:
        """Load the baseline and prewarm production ITN before clip timing."""
        if self._prepared.is_set():
            return
        with self._prepare_lock:
            if self._prepared.is_set():
                return
            self._ensure_loaded()
            if self._normalizer_override is None:
                assert self._normalizer is not None
                self._normalizer(_NORMALIZATION_PREWARM_TEXT)
            self._prepared.set()

    def _create_stream(self) -> Any:
        assert self._recognizer is not None
        snapshot = self._hotword_snapshot
        text = str(getattr(snapshot, "text", "")) if snapshot is not None else ""
        use_inline = self._inline_hotword_supported and bool(text)
        try:
            return self._recognizer.create_stream(text) if use_inline else self._recognizer.create_stream()
        except BaseException as inline_exc:
            if not use_inline:
                raise VoiceFlyXASRStreamError("x_asr_stream_create_failed") from inline_exc
            try:
                stream = self._recognizer.create_stream()
            except BaseException as plain_exc:
                raise VoiceFlyXASRStreamError("x_asr_stream_create_failed") from plain_exc
            self._stream_hotword_fallback = True
            return stream

    def transcribe(
        self,
        audio: AudioSnapshot,
        *,
        sample_id: str,
        timeout_seconds: float,
    ) -> str:
        """Run exactly one snapshot without persisting audio or opening a mic."""
        del sample_id
        if not isinstance(audio, AudioSnapshot):
            raise TypeError("audio must be an AudioSnapshot")
        if not math.isfinite(float(timeout_seconds)) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        if not self._prepared.is_set():
            raise VoiceFlyXASRStreamError("x_asr_provider_not_prepared")
        with self._inference_lock:
            return self._transcribe_prepared(audio)

    def _transcribe_prepared(self, audio: AudioSnapshot) -> str:
        """Run one stream while the caller holds the serial inference lock."""
        assert self._recognizer is not None
        assert self._join_segments is not None
        assert self._normalizer is not None

        stream = self._create_stream()
        recognizer = self._recognizer
        committed_segments: list[str] = []
        last_segment_raw = ""
        frame_samples = max(1, int(round(audio.sample_rate * FRAME_DURATION_SECONDS)))
        try:
            for offset in range(0, audio.samples.size, frame_samples):
                frame = np.ascontiguousarray(
                    audio.samples[offset : offset + frame_samples],
                    dtype=np.float32,
                )
                stream.accept_waveform(audio.sample_rate, frame)
                while recognizer.is_ready(stream):
                    recognizer.decode_stream(stream)
                raw_partial = str(recognizer.get_result(stream) or "").strip()
                if raw_partial and raw_partial != last_segment_raw:
                    last_segment_raw = raw_partial
                if recognizer.is_endpoint(stream):
                    segment = raw_partial or last_segment_raw
                    if segment:
                        committed_segments.append(segment)
                    recognizer.reset(stream)
                    last_segment_raw = ""

            stream.input_finished()
            while recognizer.is_ready(stream):
                recognizer.decode_stream(stream)
            final_segment = str(recognizer.get_result(stream) or "").strip()
            if not final_segment:
                final_segment = last_segment_raw
            if final_segment:
                committed_segments.append(final_segment)
            raw_text = self._join_segments(committed_segments)
            result = self._normalizer(raw_text)
        except VoiceFlyXASRError:
            raise
        except BaseException as exc:
            raise VoiceFlyXASRStreamError("x_asr_stream_inference_failed") from exc
        if not isinstance(result, str):
            raise VoiceFlyXASRStreamError("x_asr_normalizer_result_invalid")
        return result


__all__ = [
    "FAMILY",
    "VoiceFlyXASRError",
    "VoiceFlyXASRLoadError",
    "VoiceFlyXASRProvider",
    "VoiceFlyXASRStreamError",
]
