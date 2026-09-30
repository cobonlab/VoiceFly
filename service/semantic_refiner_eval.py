#!/usr/bin/env python3
"""Read-only offline A/B evaluation for local speech recognizers.

The evaluator accepts a strict JSONL manifest, verifies every WAV SHA-256
before any provider starts, keeps every WAV below one canonical dataset root,
and prints one JSON report to stdout.  It never writes audio, transcripts, or
derived files.  The default CLI provider is the in-process sherpa backend;
tests may inject providers or a provider factory without installing models.

Provider calls run behind one daemon-worker gate with a caller-side deadline.
A timeout aborts the complete evaluation and no later call is started.  Python
cannot cancel an in-flight native sherpa call; callers that require a true kill
must run this evaluator as its own process and terminate that process.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import hmac
import io
import importlib
from importlib import metadata as importlib_metadata
import json
import logging
import math
import os
import re
import stat
import sys
import threading
import time
import unicodedata
import wave
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Callable, Iterator, Mapping, Protocol, Sequence, TextIO

import numpy as np

from voice_refiner import (
    AudioSnapshot,
    FinalizationRequest,
    MAX_CAPTURE_SAMPLE_RATE,
    MAX_REFINER_AUDIO_SECONDS,
    MAX_REFINER_TIMEOUT_SECONDS,
    SHERPA_OFFLINE_FAMILIES,
    SherpaOfflineBackend,
    _QWEN_TOKENIZER_FILES,
    _validate_sherpa_model_directory,
)
from semantic_refiner_protocol_v1 import (
    ManifestMetadata as ProtocolManifestMetadata,
    PROTOCOL_VERSION as VOICEFLY_PROTOCOL_V1,
    ProtocolObservation,
    ProtocolValidationError,
    aggregate_report as aggregate_protocol_v1_report,
    validate_manifest_metadata as validate_protocol_v1_manifest,
)


SCHEMA_VERSION = 1
EVALUATION_NAME = "voicefly_semantic_refiner_ab"
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_MANIFEST_LINE_BYTES = 256 * 1024
MAX_MANIFEST_SAMPLES = 10_000
MAX_WAV_FILE_BYTES = 32 * 1024 * 1024
MAX_WAV_CHANNELS = 8
MAX_WAV_PCM_BYTES = 32 * 1024 * 1024
MAX_TRANSCRIPT_CHARS = 16_384
MAX_PROVIDER_IDENTITY_BYTES = 256 * 1024
MAX_SHERPA_IDENTITY_FILE_BYTES = 8 * 1024 * 1024 * 1024
_NORMALIZATION_PREWARM_TEXT = "W三五六 温度二十摄氏度"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_SAFE_RUNTIME_VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+!-]{0,127}")
VOICEFLY_X_ASR_FAMILY = "voicefly_x_asr"
BASELINE_FAMILIES = frozenset((*SHERPA_OFFLINE_FAMILIES, VOICEFLY_X_ASR_FAMILY))
_WETEXT_LOGGER_LOCK = threading.Lock()
_PRIVATE_RESOURCE_IDENTITY_STATUS = "revalidated_not_exported"
_PRIVATE_NORMALIZATION_IDENTITY_FIELDS = (
    "spoken_number_exceptions",
    "engineering_symbol_aliases",
    "personal_phonetics_resource_present",
    "personal_phonetics_resource_sha256",
)


class EvaluationInputError(ValueError):
    """A machine-readable input rejection that never includes private text."""

    def __init__(self, code: str, *, line: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.line = line


class EvaluationAbortedError(RuntimeError):
    """A hard caller-side timeout; the native worker itself is not cancellable."""

    def __init__(self, role: str) -> None:
        super().__init__(f"{role}_provider_timeout")
        self.code = f"{role}_provider_timeout"
        self.native_call_cancelled = False
        self.kill_boundary = "independent_evaluation_process"


@dataclass(frozen=True)
class BackendSpec:
    family: str
    model_dir: Path | None = field(default=None, repr=False)
    num_threads: int = 1
    language: str = "zh"


@dataclass(frozen=True)
class EvaluationConfig:
    dataset_root: Path
    manifest_path: Path
    baseline: BackendSpec
    second_model: BackendSpec
    timeout_seconds: float = 15.0
    max_audio_seconds: float = 20.0
    max_wav_bytes: int = MAX_WAV_FILE_BYTES
    include_transcripts: bool = False
    protocol: str | None = None


@dataclass(frozen=True, repr=False)
class ManifestSample:
    sample_id: str
    wav_path: Path = field(repr=False)
    sha256: str = field(repr=False)
    ground_truth: str = field(repr=False)
    verbatim_truth: str | None = field(default=None, repr=False)
    split: str | None = None
    tags: tuple[str, ...] = field(default=(), repr=False)
    tags_json_list: bool = field(default=True, repr=False)
    is_identifier: bool = False
    wav_device: int = field(default=0, repr=False)
    wav_inode: int = field(default=0, repr=False)
    wav_size: int = field(default=0, repr=False)
    wav_mtime_ns: int = field(default=0, repr=False)
    wav_ctime_ns: int = field(default=0, repr=False)

    def __repr__(self) -> str:
        return (
            "ManifestSample("
            f"sample_id={self.sample_id!r}, ground_truth_chars="
            f"{len(self.ground_truth)}, is_identifier={self.is_identifier}, "
            f"wav_size={self.wav_size})"
        )


class TranscriptProvider(Protocol):
    name: str

    def transcribe(
        self,
        audio: AudioSnapshot,
        *,
        sample_id: str,
        timeout_seconds: float,
    ) -> str:
        ...


ProviderFactory = Callable[[BackendSpec], TranscriptProvider]


@dataclass(frozen=True, repr=False)
class _SherpaAssetSnapshot:
    """One required model file, with its private path kept out of repr/output."""

    role: str
    path: Path = field(repr=False)
    device: int = field(repr=False)
    inode: int = field(repr=False)
    size_bytes: int
    mtime_ns: int = field(repr=False)
    ctime_ns: int = field(repr=False)
    sha256: str

    @property
    def signature(self) -> tuple[int, int, int, int, int]:
        return (
            self.device,
            self.inode,
            self.size_bytes,
            self.mtime_ns,
            self.ctime_ns,
        )

    def public_record(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "stat": {
                "file_type": "regular",
                "size_bytes": self.size_bytes,
            },
            "sha256": self.sha256,
        }


def _normalize_sherpa_spec(spec: BackendSpec) -> tuple[str, int, str]:
    family = str(spec.family).strip().casefold().replace("-", "_")
    if family not in SHERPA_OFFLINE_FAMILIES:
        raise EvaluationInputError("sherpa_family_unsupported")
    if (
        isinstance(spec.num_threads, bool)
        or not isinstance(spec.num_threads, int)
        or not 1 <= spec.num_threads <= 8
    ):
        raise EvaluationInputError("sherpa_num_threads_invalid")
    language = str(spec.language).strip().casefold()
    if language not in {"auto", "zh", "en", "ja", "ko", "yue"}:
        raise EvaluationInputError("sherpa_language_invalid")
    return family, spec.num_threads, language


def _resolve_sherpa_onnx_version() -> str:
    try:
        version = importlib_metadata.version("sherpa-onnx")
    except importlib_metadata.PackageNotFoundError:
        try:
            import sherpa_onnx

            version = getattr(sherpa_onnx, "__version__", None)
        except (ImportError, OSError) as exc:
            raise EvaluationInputError(
                "sherpa_runtime_version_unavailable"
            ) from exc
    except (OSError, ValueError) as exc:
        raise EvaluationInputError("sherpa_runtime_version_unavailable") from exc
    if not isinstance(version, str) or _SAFE_RUNTIME_VERSION_RE.fullmatch(version) is None:
        raise EvaluationInputError("sherpa_runtime_version_invalid")
    return version


def _required_sherpa_files(
    assets: Mapping[str, Path],
) -> tuple[tuple[str, Path], ...]:
    required: list[tuple[str, Path]] = []
    for role in sorted(assets):
        asset = assets[role]
        if role != "tokenizer":
            required.append((role, asset))
            continue
        for filename in _QWEN_TOKENIZER_FILES:
            logical_name = filename.rsplit(".", 1)[0].replace("-", "_")
            tokenizer_role = (
                logical_name
                if logical_name.startswith("tokenizer_")
                else f"tokenizer_{logical_name}"
            )
            required.append((tokenizer_role, asset / filename))
    return tuple(required)


def _hash_sherpa_asset(role: str, path: Path) -> _SherpaAssetSnapshot:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or not 0 < before.st_size <= MAX_SHERPA_IDENTITY_FILE_BYTES
        ):
            raise EvaluationInputError("sherpa_model_identity_unavailable")
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_SHERPA_IDENTITY_FILE_BYTES:
                raise EvaluationInputError("sherpa_model_identity_unavailable")
            digest.update(chunk)
        after = os.fstat(descriptor)
    except EvaluationInputError:
        raise
    except OSError as exc:
        raise EvaluationInputError("sherpa_model_identity_unavailable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    before_signature = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    after_signature = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if before_signature != after_signature or total != after.st_size:
        raise EvaluationInputError("sherpa_model_identity_unstable")
    return _SherpaAssetSnapshot(
        role=role,
        path=path,
        device=after.st_dev,
        inode=after.st_ino,
        size_bytes=after.st_size,
        mtime_ns=after.st_mtime_ns,
        ctime_ns=after.st_ctime_ns,
        sha256=digest.hexdigest(),
    )


def _snapshot_sherpa_model(
    model_dir: Path,
    *,
    family: str,
    failure_code: str,
) -> tuple[Path, tuple[_SherpaAssetSnapshot, ...]]:
    try:
        root, assets = _validate_sherpa_model_directory(model_dir, family=family)
        snapshots = tuple(
            _hash_sherpa_asset(role, path)
            for role, path in _required_sherpa_files(assets)
        )
    except (EvaluationInputError, KeyError, OSError, ValueError) as exc:
        raise EvaluationInputError(failure_code) from exc
    return root, snapshots


def _same_sherpa_snapshot(
    left: Sequence[_SherpaAssetSnapshot],
    right: Sequence[_SherpaAssetSnapshot],
) -> bool:
    return tuple(
        (item.role, item.signature, item.sha256) for item in left
    ) == tuple((item.role, item.signature, item.sha256) for item in right)


def _assert_sherpa_asset_signatures(
    snapshots: Sequence[_SherpaAssetSnapshot],
    *,
    failure_code: str,
) -> None:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    for snapshot in snapshots:
        descriptor = -1
        try:
            descriptor = os.open(snapshot.path, flags)
            info = os.fstat(descriptor)
        except OSError as exc:
            raise EvaluationInputError(failure_code) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        signature = (
            info.st_dev,
            info.st_ino,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        )
        if not stat.S_ISREG(info.st_mode) or signature != snapshot.signature:
            raise EvaluationInputError(failure_code)


def _sherpa_inference_parameters(
    family: str,
    *,
    num_threads: int,
    language: str,
) -> dict[str, Any]:
    common: dict[str, Any] = {
        "num_threads": num_threads,
        "sample_rate": 16000,
        "decoding_method": "greedy_search",
        "debug": False,
        "provider": "cpu",
    }
    if family == "paraformer":
        return {"factory": "from_paraformer", "feature_dim": 80, **common}
    if family == "sense_voice":
        return {
            "factory": "from_sense_voice",
            "feature_dim": 80,
            "language": language,
            "use_itn": False,
            **common,
        }
    return {
        "factory": "from_qwen3_asr",
        "feature_dim": 128,
        "max_total_len": 512,
        "max_new_tokens": 128,
        "temperature": 1e-6,
        "top_p": 0.8,
        "seed": 42,
        "hotwords": "",
        **common,
    }


def _load_voicefly_normalization_service() -> Any:
    try:
        service = importlib.import_module("voice_fly_service")
    except Exception as exc:
        raise EvaluationInputError(
            "sherpa_normalization_service_unavailable"
        ) from exc
    if not callable(getattr(service, "normalize", None)):
        raise EvaluationInputError("sherpa_normalization_service_invalid")
    if not callable(
        getattr(service, "apply_personal_phonetic_corrections", None)
    ):
        raise EvaluationInputError("sherpa_normalization_service_invalid")
    return service


def _sherpa_normalization_identity(service: Any) -> dict[str, Any]:
    try:
        from semantic_refiner_xasr import _normalization_identity

        raw = _normalization_identity(service)
    except Exception as exc:
        raise EvaluationInputError(
            "sherpa_normalization_identity_unavailable"
        ) from exc
    if not isinstance(raw, Mapping):
        raise EvaluationInputError("sherpa_normalization_identity_invalid")
    policy_version = raw.get("itn_policy_version")
    if (
        not isinstance(policy_version, str)
        or _SAFE_RUNTIME_VERSION_RE.fullmatch(policy_version) is None
    ):
        raise EvaluationInputError("sherpa_normalization_identity_invalid")
    normalizer_sha256 = raw.get("normalizer_module_sha256")
    voice_tool_sha256 = raw.get("voice_tool_module_sha256")
    personal_module_sha256 = raw.get("personal_phonetics_module_sha256")
    personal_sha256 = raw.get("personal_phonetics_resource_sha256")
    for module_sha256 in (normalizer_sha256, personal_module_sha256):
        if module_sha256 is not None and (
            not isinstance(module_sha256, str)
            or _SHA256_RE.fullmatch(module_sha256) is None
        ):
            raise EvaluationInputError("sherpa_normalization_identity_invalid")
    if (
        not isinstance(personal_sha256, str)
        or _SHA256_RE.fullmatch(personal_sha256) is None
    ):
        raise EvaluationInputError("sherpa_normalization_identity_invalid")
    if (
        not isinstance(voice_tool_sha256, str)
        or _SHA256_RE.fullmatch(voice_tool_sha256) is None
    ):
        raise EvaluationInputError("sherpa_normalization_identity_invalid")
    semantic_resources: dict[str, dict[str, Any]] = {}
    for resource_name in (
        "spoken_number_exceptions",
        "engineering_symbol_aliases",
    ):
        resource = raw.get(resource_name)
        if not isinstance(resource, Mapping) or set(resource) != {
            "entry_count",
            "semantic_sha256",
        }:
            raise EvaluationInputError("sherpa_normalization_identity_invalid")
        entry_count = resource.get("entry_count")
        semantic_sha256 = resource.get("semantic_sha256")
        if (
            type(entry_count) is not int
            or entry_count < 0
            or not isinstance(semantic_sha256, str)
            or _SHA256_RE.fullmatch(semantic_sha256) is None
        ):
            raise EvaluationInputError("sherpa_normalization_identity_invalid")
        semantic_resources[resource_name] = {
            "entry_count": entry_count,
            "semantic_sha256": semantic_sha256,
        }
    runtime_versions = raw.get("runtime_versions")
    if not isinstance(runtime_versions, Mapping) or set(runtime_versions) != {
        "WeTextProcessing",
        "pynini",
    }:
        raise EvaluationInputError("sherpa_normalization_identity_invalid")
    for version in runtime_versions.values():
        if (
            not isinstance(version, str)
            or _SAFE_RUNTIME_VERSION_RE.fullmatch(version) is None
        ):
            raise EvaluationInputError("sherpa_normalization_identity_invalid")
    return {
        "entrypoint": "voice_fly_service.normalize",
        "itn_policy_version": policy_version,
        "itn_cache_policy": "memory_only",
        "apply_personal_phonetics": True,
        "diagnostic_output_policy": "fallback_signal_fail_closed_without_text",
        "wetext_logger_policy": "temporarily_disabled_under_lock",
        "normalizer_module_sha256": normalizer_sha256,
        "voice_tool_module_sha256": voice_tool_sha256,
        **semantic_resources,
        "personal_phonetics_module_sha256": personal_module_sha256,
        "personal_phonetics_resource_present": bool(
            raw.get("personal_phonetics_resource_present")
        ),
        "personal_phonetics_resource_sha256": personal_sha256,
        "runtime_versions": dict(runtime_versions),
    }


@contextmanager
def _suppress_wetext_logger() -> Iterator[None]:
    """Silence WeText's own logger without rebinding process output streams."""
    with _WETEXT_LOGGER_LOCK:
        logger = logging.getLogger("wetext")
        previously_disabled = logger.disabled
        logger.disabled = True
        try:
            yield
        finally:
            logger.disabled = previously_disabled


class SherpaTranscriptProvider:
    """Adapt ``SherpaOfflineBackend`` to the small evaluator interface."""

    def __init__(
        self,
        spec: BackendSpec,
        *,
        backend_factory: Callable[..., Any] = SherpaOfflineBackend,
        runtime_version_resolver: Callable[[], str] = _resolve_sherpa_onnx_version,
        normalization_service: Any | None = None,
    ) -> None:
        if spec.model_dir is None:
            raise EvaluationInputError("sherpa_model_dir_required")
        family, num_threads, language = _normalize_sherpa_spec(spec)
        root, before = _snapshot_sherpa_model(
            spec.model_dir,
            family=family,
            failure_code="sherpa_model_layout_invalid",
        )
        service = (
            _load_voicefly_normalization_service()
            if normalization_service is None
            else normalization_service
        )
        if not callable(getattr(service, "normalize", None)):
            raise EvaluationInputError("sherpa_normalization_service_invalid")
        normalization_before = _sherpa_normalization_identity(service)
        try:
            runtime_version = runtime_version_resolver()
        except EvaluationInputError:
            raise
        except BaseException as exc:
            raise EvaluationInputError(
                "sherpa_runtime_version_unavailable"
            ) from exc
        if (
            not isinstance(runtime_version, str)
            or _SAFE_RUNTIME_VERSION_RE.fullmatch(runtime_version) is None
        ):
            raise EvaluationInputError("sherpa_runtime_version_invalid")
        try:
            backend = backend_factory(
                family,
                root,
                num_threads=num_threads,
                language=language,
            )
        except BaseException:
            _, after_failure = _snapshot_sherpa_model(
                root,
                family=family,
                failure_code="sherpa_model_identity_unstable",
            )
            normalization_after_failure = _sherpa_normalization_identity(service)
            if not _same_sherpa_snapshot(before, after_failure):
                raise EvaluationInputError("sherpa_model_identity_unstable")
            if normalization_before != normalization_after_failure:
                raise EvaluationInputError(
                    "sherpa_normalization_identity_unstable"
                )
            raise
        _, after = _snapshot_sherpa_model(
            root,
            family=family,
            failure_code="sherpa_model_identity_unstable",
        )
        if not _same_sherpa_snapshot(before, after):
            raise EvaluationInputError("sherpa_model_identity_unstable")
        normalization_after = _sherpa_normalization_identity(service)
        if normalization_before != normalization_after:
            raise EvaluationInputError("sherpa_normalization_identity_unstable")
        self._backend = backend
        self._asset_snapshots = after
        self._normalization_service = service
        self._normalization_identity = normalization_after
        self._prepare_lock = threading.Lock()
        self._normalization_prepared = threading.Event()
        self._first_transcribe_lock = threading.Lock()
        self._first_transcribe_verified = False
        self._identity = {
            "schema_version": 1,
            "provider": "sherpa_onnx_offline",
            "family": family,
            "num_threads": num_threads,
            "language": language,
            "runtime": {
                "package": "sherpa-onnx",
                "version": runtime_version,
            },
            "inference_parameters": _sherpa_inference_parameters(
                family,
                num_threads=num_threads,
                language=language,
            ),
            "model_files": [item.public_record() for item in after],
            "normalization": dict(normalization_after),
            "preparation": {
                "required_before_transcription": True,
                "canonicalizer_prewarm": "fixed_non_private_probe",
                "probe_sha256": hashlib.sha256(
                    _NORMALIZATION_PREWARM_TEXT.encode("utf-8")
                ).hexdigest(),
                "included_in_sample_latency": False,
                "supervision_boundary": "evaluation_process",
            },
        }
        self.name = str(getattr(self._backend, "name", "sherpa-onnx"))

    @property
    def identity(self) -> dict[str, Any]:
        identity = copy.deepcopy(self._identity)
        normalization = identity["normalization"]
        for field in _PRIVATE_NORMALIZATION_IDENTITY_FIELDS:
            normalization.pop(field, None)
        normalization["private_resource_identity"] = (
            _PRIVATE_RESOURCE_IDENTITY_STATUS
        )
        identity["private_resource_identity"] = (
            _PRIVATE_RESOURCE_IDENTITY_STATUS
        )
        return identity

    def prepare(self) -> None:
        """Prewarm the shared memory-only canonicalizer outside clip timing."""
        with self._prepare_lock:
            if self._normalization_prepared.is_set():
                return
            self._normalize(_NORMALIZATION_PREWARM_TEXT)
            self._normalization_prepared.set()

    def _normalize(self, raw_text: Any) -> str:
        if not isinstance(raw_text, str):
            raise EvaluationInputError("sherpa_raw_transcript_invalid")
        if (
            _sherpa_normalization_identity(self._normalization_service)
            != self._normalization_identity
        ):
            raise EvaluationInputError("sherpa_normalization_identity_changed")
        fallback_signaled = False

        def mark_fallback(_message: object) -> None:
            nonlocal fallback_signaled
            fallback_signaled = True

        try:
            with _suppress_wetext_logger():
                normalized = self._normalization_service.normalize(
                    raw_text,
                    diagnostic_logger=mark_fallback,
                    memory_only_itn=True,
                )
        except Exception as exc:
            if (
                _sherpa_normalization_identity(self._normalization_service)
                != self._normalization_identity
            ):
                raise EvaluationInputError(
                    "sherpa_normalization_identity_changed"
                ) from exc
            raise EvaluationInputError("sherpa_normalization_failed") from exc
        if fallback_signaled:
            raise EvaluationInputError("sherpa_normalization_fallback")
        if (
            _sherpa_normalization_identity(self._normalization_service)
            != self._normalization_identity
        ):
            raise EvaluationInputError("sherpa_normalization_identity_changed")
        if not isinstance(normalized, str):
            raise EvaluationInputError("sherpa_normalization_result_invalid")
        return normalized

    def transcribe(
        self,
        audio: AudioSnapshot,
        *,
        sample_id: str,
        timeout_seconds: float,
    ) -> str:
        del sample_id
        if not self._normalization_prepared.is_set():
            raise EvaluationInputError("sherpa_provider_not_prepared")
        request = FinalizationRequest(
            session_id="offline-ab-eval",
            raw_segments=(),
            raw_text="",
            baseline_text="",
            deadline_monotonic=time.monotonic() + timeout_seconds,
            audio=audio,
        )
        candidate = None
        with self._first_transcribe_lock:
            if not self._first_transcribe_verified:
                _assert_sherpa_asset_signatures(
                    self._asset_snapshots,
                    failure_code="sherpa_model_changed_before_first_transcription",
                )
                try:
                    candidate = self._backend.refine(
                        request,
                        timeout_seconds=timeout_seconds,
                    )
                except BaseException:
                    _assert_sherpa_asset_signatures(
                        self._asset_snapshots,
                        failure_code="sherpa_model_changed_during_first_transcription",
                    )
                    raise
                _assert_sherpa_asset_signatures(
                    self._asset_snapshots,
                    failure_code="sherpa_model_changed_during_first_transcription",
                )
                self._first_transcribe_verified = True
        if candidate is None:
            candidate = self._backend.refine(
                request,
                timeout_seconds=timeout_seconds,
            )
        return self._normalize(candidate.text)


def make_sherpa_provider(spec: BackendSpec) -> TranscriptProvider:
    return SherpaTranscriptProvider(spec)


def make_provider(spec: BackendSpec) -> TranscriptProvider:
    """Build an allowed provider without importing the live service eagerly."""
    if spec.family == VOICEFLY_X_ASR_FAMILY:
        from semantic_refiner_xasr import VoiceFlyXASRProvider

        return VoiceFlyXASRProvider()
    return make_sherpa_provider(spec)


def _finite_bounded_seconds(value: Any, *, code: str, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise EvaluationInputError(code) from exc
    if not math.isfinite(parsed) or not 0 < parsed <= maximum:
        raise EvaluationInputError(code)
    return parsed


def _bounded_wav_bytes(value: Any) -> int:
    if isinstance(value, bool):
        raise EvaluationInputError("invalid_max_wav_bytes")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise EvaluationInputError("invalid_max_wav_bytes") from exc
    if not 44 <= parsed <= MAX_WAV_FILE_BYTES:
        raise EvaluationInputError("invalid_max_wav_bytes")
    return parsed


@contextmanager
def _open_regular_no_follow(
    path: Path,
    *,
    max_bytes: int,
    error_prefix: str,
) -> Iterator[tuple[BinaryIO, os.stat_result]]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise EvaluationInputError(f"{error_prefix}_not_regular")
        if info.st_size > max_bytes:
            raise EvaluationInputError(f"{error_prefix}_too_large")
        handle = os.fdopen(descriptor, "rb", closefd=True)
        descriptor = -1
        with handle:
            yield handle, info
    except EvaluationInputError:
        raise
    except OSError as exc:
        raise EvaluationInputError(f"{error_prefix}_unreadable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _canonical_dataset_root(path: Path) -> Path:
    if path.is_symlink():
        raise EvaluationInputError("dataset_root_symlink")
    try:
        root = path.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise EvaluationInputError("dataset_root_missing") from exc
    if not root.is_dir():
        raise EvaluationInputError("dataset_root_not_directory")
    return root


def _resolve_wav(
    root: Path,
    relative_value: Any,
    *,
    max_wav_bytes: int,
    line: int,
) -> tuple[Path, os.stat_result]:
    if not isinstance(relative_value, str) or not relative_value:
        raise EvaluationInputError("wav_path_invalid", line=line)
    if len(relative_value) > 1024 or "\\" in relative_value or "\x00" in relative_value:
        raise EvaluationInputError("wav_path_invalid", line=line)
    relative = PurePosixPath(relative_value)
    if (
        relative.is_absolute()
        or relative.as_posix() != relative_value
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
        or relative.suffix.casefold() != ".wav"
    ):
        raise EvaluationInputError("wav_path_invalid", line=line)

    candidate = root
    for part in relative.parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise EvaluationInputError("wav_symlink", line=line)
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise EvaluationInputError("wav_missing", line=line) from exc
    if root not in resolved.parents:
        raise EvaluationInputError("wav_outside_dataset_root", line=line)
    try:
        info = resolved.stat()
    except OSError as exc:
        raise EvaluationInputError("wav_unreadable", line=line) from exc
    if not stat.S_ISREG(info.st_mode):
        raise EvaluationInputError("wav_not_regular", line=line)
    if info.st_size > max_wav_bytes:
        raise EvaluationInputError("wav_too_large", line=line)
    return resolved, info


def _parse_tags(raw: Any, *, line: int) -> tuple[tuple[str, ...], bool]:
    """Validate generic manifest tags while retaining safe fixed-protocol tags."""
    if raw is None:
        return (), False
    if isinstance(raw, str):
        if not raw or len(raw) > 128:
            raise EvaluationInputError("tags_invalid", line=line)
        return (raw,), raw.casefold() == "identifier"
    if isinstance(raw, list):
        if len(raw) > 32 or not all(
            isinstance(item, str) and 0 < len(item) <= 128 for item in raw
        ):
            raise EvaluationInputError("tags_invalid", line=line)
        if len(raw) != len(set(raw)):
            raise EvaluationInputError("tags_invalid", line=line)
        tags = tuple(raw)
        return tags, any(item.casefold() == "identifier" for item in tags)
    if isinstance(raw, dict):
        if len(raw) > 32 or not all(
            isinstance(key, str) and 0 < len(key) <= 128 for key in raw
        ):
            raise EvaluationInputError("tags_invalid", line=line)
        marker = raw.get("identifier") is True
        kind = raw.get("type", raw.get("kind", ""))
        if kind and not isinstance(kind, str):
            raise EvaluationInputError("tags_invalid", line=line)
        retained: list[str] = []
        if isinstance(kind, str) and kind:
            retained.append(kind)
        if marker and not any(item.casefold() == "identifier" for item in retained):
            retained.append("identifier")
        return tuple(retained), marker or (
            isinstance(kind, str) and kind.casefold() == "identifier"
        )
    raise EvaluationInputError("tags_invalid", line=line)


def _tags_mark_identifier(raw: Any, *, line: int) -> bool:
    """Backward-compatible helper retained for focused parser tests."""
    return _parse_tags(raw, line=line)[1]


def _looks_like_identifier(text: str) -> bool:
    normalized = unicodedata.normalize("NFKC", text).strip()
    if not normalized:
        return False
    item = r"[A-Za-z]{1,4}\s*[0-9]{1,32}[A-Za-z0-9-]{0,8}"
    return re.fullmatch(rf"{item}(?:[\s,，;；、]+{item})*", normalized) is not None


def _wav_signature(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _verify_wav_sha256(sample: ManifestSample) -> ManifestSample:
    """Hash one bounded WAV and require a stable file identity while reading."""
    with _open_regular_no_follow(
        sample.wav_path,
        max_bytes=sample.wav_size,
        error_prefix="wav",
    ) as (handle, before):
        if (before.st_dev, before.st_ino, before.st_size) != (
            sample.wav_device,
            sample.wav_inode,
            sample.wav_size,
        ):
            raise EvaluationInputError("wav_changed_before_sha256")
        digest = hashlib.sha256()
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(handle.fileno())
    if _wav_signature(before) != _wav_signature(after):
        raise EvaluationInputError("wav_changed_during_sha256")
    if not hmac.compare_digest(digest.hexdigest(), sample.sha256):
        raise EvaluationInputError("wav_sha256_mismatch")
    return replace(
        sample,
        wav_device=after.st_dev,
        wav_inode=after.st_ino,
        wav_size=after.st_size,
        wav_mtime_ns=after.st_mtime_ns,
        wav_ctime_ns=after.st_ctime_ns,
    )


def load_manifest(
    manifest_path: Path,
    dataset_root: Path,
    *,
    max_wav_bytes: int = MAX_WAV_FILE_BYTES,
) -> tuple[ManifestSample, ...]:
    """Parse and fully validate a strict manifest before any model runs."""
    root = _canonical_dataset_root(dataset_root)
    max_wav_bytes = _bounded_wav_bytes(max_wav_bytes)
    if manifest_path.is_symlink():
        raise EvaluationInputError("manifest_symlink")
    with _open_regular_no_follow(
        manifest_path,
        max_bytes=MAX_MANIFEST_BYTES,
        error_prefix="manifest",
    ) as (handle, _):
        raw_manifest = handle.read(MAX_MANIFEST_BYTES + 1)
    if len(raw_manifest) > MAX_MANIFEST_BYTES:
        raise EvaluationInputError("manifest_too_large")
    raw_lines = raw_manifest.splitlines()
    if not raw_lines:
        raise EvaluationInputError("manifest_empty")
    if len(raw_lines) > MAX_MANIFEST_SAMPLES:
        raise EvaluationInputError("manifest_too_many_samples")

    samples: list[ManifestSample] = []
    seen_ids: set[str] = set()
    for line_number, raw_line in enumerate(raw_lines, 1):
        if not raw_line or len(raw_line) > MAX_MANIFEST_LINE_BYTES:
            raise EvaluationInputError("manifest_line_invalid", line=line_number)
        try:
            payload = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EvaluationInputError("manifest_json_invalid", line=line_number) from exc
        if not isinstance(payload, dict):
            raise EvaluationInputError("manifest_record_not_object", line=line_number)
        allowed_fields = {
            "id",
            "wav",
            "sha256",
            "ground_truth",
            "tags",
            "split",
            "verbatim_truth",
        }
        if set(payload) - allowed_fields:
            raise EvaluationInputError("manifest_unknown_field", line=line_number)
        if not {"id", "wav", "sha256", "ground_truth"}.issubset(payload):
            raise EvaluationInputError("manifest_required_field_missing", line=line_number)

        sample_id = payload["id"]
        if (
            not isinstance(sample_id, str)
            or not sample_id
            or len(sample_id) > 256
            or any(ord(char) < 32 for char in sample_id)
        ):
            raise EvaluationInputError("sample_id_invalid", line=line_number)
        if sample_id in seen_ids:
            raise EvaluationInputError("duplicate_sample_id", line=line_number)

        expected_sha256 = payload["sha256"]
        if (
            not isinstance(expected_sha256, str)
            or _SHA256_RE.fullmatch(expected_sha256) is None
        ):
            raise EvaluationInputError("sha256_invalid", line=line_number)

        ground_truth = payload["ground_truth"]
        if (
            not isinstance(ground_truth, str)
            or not ground_truth.strip()
            or len(ground_truth) > MAX_TRANSCRIPT_CHARS
            or "\x00" in ground_truth
        ):
            raise EvaluationInputError("ground_truth_invalid", line=line_number)
        ground_truth = ground_truth.strip()
        if not _normalize_for_cer(ground_truth):
            raise EvaluationInputError("ground_truth_invalid", line=line_number)

        split = payload.get("split")
        if split is not None and (
            not isinstance(split, str)
            or not split
            or len(split) > 64
            or any(ord(character) < 32 for character in split)
        ):
            raise EvaluationInputError("split_invalid", line=line_number)
        verbatim_truth = payload.get("verbatim_truth")
        if verbatim_truth is not None and (
            not isinstance(verbatim_truth, str)
            or not verbatim_truth.strip()
            or len(verbatim_truth) > MAX_TRANSCRIPT_CHARS
            or "\x00" in verbatim_truth
        ):
            raise EvaluationInputError("verbatim_truth_invalid", line=line_number)

        wav_path, wav_info = _resolve_wav(
            root,
            payload["wav"],
            max_wav_bytes=max_wav_bytes,
            line=line_number,
        )
        tags, tagged_identifier = _parse_tags(
            payload.get("tags"),
            line=line_number,
        )
        samples.append(
            ManifestSample(
                sample_id=sample_id,
                wav_path=wav_path,
                sha256=expected_sha256,
                ground_truth=ground_truth,
                verbatim_truth=verbatim_truth,
                split=split,
                tags=tags,
                tags_json_list=isinstance(payload.get("tags"), list),
                is_identifier=tagged_identifier or _looks_like_identifier(ground_truth),
                wav_device=wav_info.st_dev,
                wav_inode=wav_info.st_ino,
                wav_size=wav_info.st_size,
                wav_mtime_ns=wav_info.st_mtime_ns,
                wav_ctime_ns=wav_info.st_ctime_ns,
            )
        )
        seen_ids.add(sample_id)
    # Complete this preflight before a provider is constructed or invoked.
    # Content hashes catch in-place mutation even when inode and size match.
    return tuple(_verify_wav_sha256(sample) for sample in samples)


def _decode_pcm(raw: bytes, sample_width: int) -> np.ndarray:
    if sample_width == 1:
        values = np.frombuffer(raw, dtype=np.uint8).astype(np.float32)
        return (values - 128.0) / 128.0
    if sample_width == 2:
        return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if sample_width == 3:
        octets = np.frombuffer(raw, dtype=np.uint8)
        if octets.size % 3:
            raise EvaluationInputError("wav_truncated_pcm")
        triples = octets.reshape(-1, 3).astype(np.int32)
        values = triples[:, 0] | (triples[:, 1] << 8) | (triples[:, 2] << 16)
        values = (values ^ 0x800000) - 0x800000
        return values.astype(np.float32) / 8388608.0
    if sample_width == 4:
        values = np.frombuffer(raw, dtype="<i4").astype(np.float64)
        return (values / 2147483648.0).astype(np.float32)
    raise EvaluationInputError("wav_sample_width_unsupported")


def load_wav(sample: ManifestSample, *, max_audio_seconds: float) -> AudioSnapshot:
    """Decode the exact immutable WAV bytes covered by the manifest hash."""
    max_audio_seconds = _finite_bounded_seconds(
        max_audio_seconds,
        code="invalid_max_audio_seconds",
        maximum=MAX_REFINER_AUDIO_SECONDS,
    )
    with _open_regular_no_follow(
        sample.wav_path,
        max_bytes=sample.wav_size,
        error_prefix="wav",
    ) as (handle, info):
        expected_signature = (
            sample.wav_device,
            sample.wav_inode,
            sample.wav_size,
            sample.wav_mtime_ns,
            sample.wav_ctime_ns,
        )
        if _wav_signature(info) != expected_signature:
            raise EvaluationInputError("wav_changed_after_manifest_validation")
        payload = handle.read(sample.wav_size + 1)
        after = os.fstat(handle.fileno())

    if (
        _wav_signature(after) != expected_signature
        or len(payload) != sample.wav_size
    ):
        raise EvaluationInputError("wav_changed_during_load")
    if not hmac.compare_digest(hashlib.sha256(payload).hexdigest(), sample.sha256):
        raise EvaluationInputError("wav_sha256_mismatch_after_manifest_validation")

    try:
        with wave.open(io.BytesIO(payload), "rb") as reader:
            if reader.getcomptype() != "NONE":
                raise EvaluationInputError("wav_compressed")
            channels = reader.getnchannels()
            sample_width = reader.getsampwidth()
            sample_rate = reader.getframerate()
            frame_count = reader.getnframes()
            if channels <= 0 or channels > MAX_WAV_CHANNELS:
                raise EvaluationInputError("wav_channels_invalid")
            if sample_width not in {1, 2, 3, 4}:
                raise EvaluationInputError("wav_sample_width_unsupported")
            if sample_rate <= 0 or sample_rate > MAX_CAPTURE_SAMPLE_RATE:
                raise EvaluationInputError("wav_sample_rate_invalid")
            if frame_count <= 0:
                raise EvaluationInputError("wav_empty")
            if frame_count / sample_rate > max_audio_seconds:
                raise EvaluationInputError("wav_too_long")
            if frame_count * channels * sample_width > MAX_WAV_PCM_BYTES:
                raise EvaluationInputError("wav_pcm_too_large")
            raw = reader.readframes(frame_count)
    except EvaluationInputError:
        raise
    except (EOFError, wave.Error) as exc:
        raise EvaluationInputError("wav_invalid") from exc

    values = _decode_pcm(raw, sample_width)
    if values.size != frame_count * channels:
        raise EvaluationInputError("wav_truncated_pcm")
    frames = values.reshape(frame_count, channels)
    mono = (
        frames[:, 0].astype(np.float32, copy=True)
        if channels == 1
        else frames.mean(axis=1, dtype=np.float64).astype(np.float32)
    )
    return AudioSnapshot(samples=mono, sample_rate=sample_rate)


def _normalize_for_cer(text: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKC", text)
        if not character.isspace()
    )


def _normalize_exact(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).strip().split())


def levenshtein_distance(reference: str, hypothesis: str) -> int:
    """Return code-point Levenshtein distance using bounded working memory."""
    if len(reference) < len(hypothesis):
        reference, hypothesis = hypothesis, reference
    previous = list(range(len(hypothesis) + 1))
    for row, reference_character in enumerate(reference, 1):
        current = [row]
        for column, hypothesis_character in enumerate(hypothesis, 1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[column] + 1,
                    previous[column - 1]
                    + (reference_character != hypothesis_character),
                )
            )
        previous = current
    return previous[-1]


def _percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


@dataclass(frozen=True)
class _ProviderResult:
    text: str | None
    latency_ms: float
    error_type: str | None = None
    error_code: str | None = None


class _ProviderCallGate:
    """Permit one bounded wait and forbid a second call after timeout.

    A daemon thread gives the Python caller a hard wait bound, but cannot
    cancel native inference.  The only true kill boundary is the evaluator
    process itself, so timeout is terminal for this evaluation.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._in_flight = False

    def invoke(
        self,
        provider: TranscriptProvider,
        audio: AudioSnapshot,
        *,
        role: str,
        sample_id: str,
        timeout_seconds: float,
    ) -> _ProviderResult:
        with self._lock:
            if self._in_flight:
                raise EvaluationAbortedError(role)
            self._in_flight = True

        started = time.perf_counter()
        mailbox: list[_ProviderResult] = []

        def worker() -> None:
            try:
                text = provider.transcribe(
                    AudioSnapshot(audio.samples, audio.sample_rate),
                    sample_id=sample_id,
                    timeout_seconds=timeout_seconds,
                )
                if not isinstance(text, str):
                    raise TypeError("provider transcript must be text")
                text = text.strip()
                if len(text) > MAX_TRANSCRIPT_CHARS or "\x00" in text:
                    raise ValueError("provider transcript is invalid")
                mailbox.append(
                    _ProviderResult(
                        text=text,
                        latency_ms=(time.perf_counter() - started) * 1000,
                    )
                )
            except BaseException as exc:
                raw_code = getattr(exc, "code", None)
                error_code = (
                    raw_code
                    if isinstance(raw_code, str)
                    and re.fullmatch(r"[a-z0-9_]{1,128}", raw_code)
                    else None
                )
                mailbox.append(
                    _ProviderResult(
                        text=None,
                        latency_ms=(time.perf_counter() - started) * 1000,
                        error_type=type(exc).__name__,
                        error_code=error_code,
                    )
                )
            finally:
                with self._lock:
                    self._in_flight = False

        thread = threading.Thread(
            target=worker,
            name="semantic-refiner-eval-provider",
            daemon=True,
        )
        try:
            thread.start()
        except BaseException:
            with self._lock:
                self._in_flight = False
            raise
        thread.join(timeout_seconds)
        if thread.is_alive():
            raise EvaluationAbortedError(role)
        if not mailbox:
            return _ProviderResult(
                text=None,
                latency_ms=(time.perf_counter() - started) * 1000,
                error_type="ProviderWorkerFailed",
            )
        return mailbox[0]


# A timed-out native call cannot be cancelled.  Keep its occupancy visible to
# every later evaluation in this process so retries cannot create more orphan
# inference workers and multiply their audio/model memory.
_PROCESS_PROVIDER_CALL_GATE = _ProviderCallGate()


def _backend_report(
    *,
    name: str,
    edits: int,
    reference_characters: int,
    identifier_correct: int,
    identifier_total: int,
    successful: int,
    failed: int,
    latencies: Sequence[float],
    identity_sha256: str | None = None,
) -> dict[str, Any]:
    report = {
        "name": name,
        "cer": round(edits / reference_characters, 6),
        "edit_distance": edits,
        "reference_characters": reference_characters,
        "identifier_exact_accuracy": (
            round(identifier_correct / identifier_total, 6)
            if identifier_total
            else None
        ),
        "identifier_correct": identifier_correct,
        "identifier_total": identifier_total,
        "successful_count": successful,
        "failed_count": failed,
        "latency_ms": {
            "p50": round(_percentile(latencies, 0.50) or 0.0, 3),
            "p95": round(_percentile(latencies, 0.95) or 0.0, 3),
        },
    }
    if identity_sha256 is not None:
        report["identity_sha256"] = identity_sha256
    return report


def _provider_identity_sha256(provider: TranscriptProvider) -> str | None:
    """Fingerprint provider metadata without emitting its paths or free text."""
    identity = getattr(provider, "identity", None)
    if identity is None:
        return None
    if callable(identity):
        identity = identity()
    if not isinstance(identity, Mapping):
        raise TypeError("provider identity must be a mapping")
    try:
        encoded = json.dumps(
            identity,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise TypeError("provider identity must be JSON-safe") from exc
    if len(encoded) > MAX_PROVIDER_IDENTITY_BYTES:
        raise TypeError("provider identity is too large")
    return hashlib.sha256(encoded).hexdigest()


def _prepare_provider(provider: TranscriptProvider, *, role: str) -> None:
    prepare = getattr(provider, "prepare", None)
    if prepare is None:
        return
    if not callable(prepare):
        raise EvaluationInputError(f"{role}_provider_prepare_invalid")
    try:
        prepare()
    except EvaluationInputError:
        raise
    except Exception as exc:
        raise EvaluationInputError(f"{role}_provider_prepare_failed") from exc


def _validate_protocol_samples(
    samples: Sequence[ManifestSample],
    protocol: str | None,
) -> None:
    if protocol is None:
        return
    if protocol != VOICEFLY_PROTOCOL_V1:
        raise EvaluationInputError("evaluation_protocol_unsupported")
    if any(not sample.tags_json_list for sample in samples):
        raise EvaluationInputError("protocol_v1_tags_type_invalid")
    try:
        validate_protocol_v1_manifest(
            ProtocolManifestMetadata(
                sample_id=sample.sample_id,
                split=sample.split if sample.split is not None else "",
                ground_truth=sample.ground_truth,
                tags=sample.tags,
            )
            for sample in samples
        )
    except ProtocolValidationError as exc:
        raise EvaluationInputError(f"protocol_v1_{exc.code}") from exc


def evaluate_samples(
    samples: Sequence[ManifestSample],
    baseline_provider: TranscriptProvider,
    second_provider: TranscriptProvider,
    *,
    timeout_seconds: float,
    max_audio_seconds: float,
    include_transcripts: bool = False,
    baseline_family: str = "baseline",
    second_family: str = "second_model",
    protocol: str | None = None,
) -> dict[str, Any]:
    if type(include_transcripts) is not bool:
        raise EvaluationInputError("include_transcripts_invalid")
    timeout_seconds = _finite_bounded_seconds(
        timeout_seconds,
        code="invalid_timeout_seconds",
        maximum=MAX_REFINER_TIMEOUT_SECONDS,
    )
    max_audio_seconds = _finite_bounded_seconds(
        max_audio_seconds,
        code="invalid_max_audio_seconds",
        maximum=MAX_REFINER_AUDIO_SECONDS,
    )
    if not samples:
        raise EvaluationInputError("manifest_empty")
    # The fixed protocol is validated before provider preparation, model load,
    # or inference so a malformed benchmark cannot consume model resources.
    _validate_protocol_samples(samples, protocol)
    # Preparation is intentionally outside every per-clip provider deadline.
    # The independent evaluator-process supervisor owns its wall-clock and
    # resource limits, including native model and memory-only ITN cold starts.
    _prepare_provider(baseline_provider, role="baseline")
    _prepare_provider(second_provider, role="second")

    reference_characters = 0
    identifier_total = 0
    baseline_edits = second_edits = 0
    baseline_identifier_correct = second_identifier_correct = 0
    baseline_successful = baseline_failed = 0
    second_successful = second_failed = 0
    baseline_latencies: list[float] = []
    second_latencies: list[float] = []
    agreement = disagreement = unavailable_comparison = 0
    second_better = baseline_better = equal_error = 0
    error_details: list[dict[str, Any]] = []
    protocol_observations: list[ProtocolObservation] = []
    call_gate = _PROCESS_PROVIDER_CALL_GATE

    for sample_number, sample in enumerate(samples, 1):
        audio = load_wav(sample, max_audio_seconds=max_audio_seconds)
        baseline = call_gate.invoke(
            baseline_provider,
            audio,
            role="baseline",
            sample_id=sample.sample_id,
            timeout_seconds=timeout_seconds,
        )
        second = call_gate.invoke(
            second_provider,
            audio,
            role="second_model",
            sample_id=sample.sample_id,
            timeout_seconds=timeout_seconds,
        )
        baseline_latencies.append(baseline.latency_ms)
        second_latencies.append(second.latency_ms)
        baseline_successful += baseline.text is not None
        baseline_failed += baseline.text is None
        second_successful += second.text is not None
        second_failed += second.text is None
        if protocol == VOICEFLY_PROTOCOL_V1:
            protocol_observations.append(
                ProtocolObservation(
                    sample_id=sample.sample_id,
                    split=sample.split if sample.split is not None else "",
                    ground_truth=sample.ground_truth,
                    tags=sample.tags,
                    baseline=baseline.text,
                    candidate=second.text,
                )
            )

        reference = _normalize_for_cer(sample.ground_truth)
        baseline_hypothesis = _normalize_for_cer(baseline.text or "")
        second_hypothesis = _normalize_for_cer(second.text or "")
        baseline_distance = levenshtein_distance(reference, baseline_hypothesis)
        second_distance = levenshtein_distance(reference, second_hypothesis)
        reference_characters += len(reference)
        baseline_edits += baseline_distance
        second_edits += second_distance

        if sample.is_identifier:
            identifier_total += 1
            baseline_identifier_correct += (
                baseline.text is not None
                and _normalize_exact(baseline.text) == _normalize_exact(sample.ground_truth)
            )
            second_identifier_correct += (
                second.text is not None
                and _normalize_exact(second.text) == _normalize_exact(sample.ground_truth)
            )

        if baseline.text is None or second.text is None:
            unavailable_comparison += 1
        elif _normalize_exact(baseline.text) == _normalize_exact(second.text):
            agreement += 1
        else:
            disagreement += 1

        if second_distance < baseline_distance:
            second_better += 1
        elif baseline_distance < second_distance:
            baseline_better += 1
        else:
            equal_error += 1

        if baseline_distance or second_distance or baseline.error_type or second.error_type:
            detail: dict[str, Any] = {
                "sample_number": sample_number,
                "reference_characters": len(reference),
                "baseline": {
                    "edit_distance": baseline_distance,
                    "cer": round(baseline_distance / len(reference), 6),
                    "latency_ms": round(baseline.latency_ms, 3),
                    "error_type": baseline.error_type,
                    "error_code": baseline.error_code,
                },
                "second_model": {
                    "edit_distance": second_distance,
                    "cer": round(second_distance / len(reference), 6),
                    "latency_ms": round(second.latency_ms, 3),
                    "error_type": second.error_type,
                    "error_code": second.error_code,
                },
            }
            if include_transcripts:
                detail["id"] = sample.sample_id
                detail["transcripts"] = {
                    "ground_truth": sample.ground_truth,
                    "baseline": baseline.text,
                    "second_model": second.text,
                }
                if sample.verbatim_truth is not None:
                    detail["transcripts"]["verbatim_truth"] = sample.verbatim_truth
                if sample.split is not None:
                    detail["split"] = sample.split
            error_details.append(detail)

    safe_baseline_family = (
        baseline_family if baseline_family in BASELINE_FAMILIES else "baseline"
    )
    safe_second_family = (
        second_family if second_family in SHERPA_OFFLINE_FAMILIES else "second_model"
    )
    complete = baseline_failed == 0 and second_failed == 0
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "evaluation": EVALUATION_NAME,
        "sample_count": len(samples),
        "privacy": {
            "audio_persisted": False,
            "transcripts_persisted": False,
            "paths_emitted": False if not include_transcripts else None,
            "free_text_included": include_transcripts,
            "sample_metadata_included": include_transcripts,
            "raw_provider_metadata_included": False,
            "transcripts_included": include_transcripts,
            "output": "stdout_only",
        },
        "complete": complete,
        "provider_failure_count": baseline_failed + second_failed,
        "normalization": {
            "cer": "Unicode NFKC code points with whitespace removed",
            "identifier_exact": "Unicode NFKC with outer/collapsed whitespace",
        },
        "baseline": _backend_report(
            name=safe_baseline_family,
            edits=baseline_edits,
            reference_characters=reference_characters,
            identifier_correct=baseline_identifier_correct,
            identifier_total=identifier_total,
            successful=baseline_successful,
            failed=baseline_failed,
            latencies=baseline_latencies,
            identity_sha256=_provider_identity_sha256(baseline_provider),
        ),
        "second_model": _backend_report(
            name=safe_second_family,
            edits=second_edits,
            reference_characters=reference_characters,
            identifier_correct=second_identifier_correct,
            identifier_total=identifier_total,
            successful=second_successful,
            failed=second_failed,
            latencies=second_latencies,
            identity_sha256=_provider_identity_sha256(second_provider),
        ),
        "comparison": {
            "agreement_count": agreement,
            "disagreement_count": disagreement,
            "unavailable_count": unavailable_comparison,
            "second_better_count": second_better,
            "baseline_better_count": baseline_better,
            "equal_error_count": equal_error,
        },
        "error_count": len(error_details),
        "errors": error_details,
    }
    if protocol == VOICEFLY_PROTOCOL_V1:
        try:
            report["quality_protocol"] = aggregate_protocol_v1_report(
                protocol_observations
            )
        except ProtocolValidationError as exc:
            raise EvaluationInputError(f"protocol_v1_{exc.code}") from exc
    return report


def run_evaluation(
    config: EvaluationConfig,
    *,
    baseline_provider: TranscriptProvider | None = None,
    second_provider: TranscriptProvider | None = None,
    provider_factory: ProviderFactory = make_provider,
) -> dict[str, Any]:
    """Load a manifest and run A/B, optionally with injected test providers."""
    if (baseline_provider is None) != (second_provider is None):
        raise ValueError("both providers must be injected together")
    if config.baseline.family not in BASELINE_FAMILIES:
        raise EvaluationInputError("baseline_family_unsupported")
    if config.second_model.family not in SHERPA_OFFLINE_FAMILIES:
        raise EvaluationInputError("second_family_must_be_offline")
    if config.baseline.family == VOICEFLY_X_ASR_FAMILY:
        if config.baseline.model_dir is not None:
            raise EvaluationInputError("x_asr_model_dir_override_forbidden")
    elif config.baseline.model_dir is None:
        raise EvaluationInputError("baseline_model_dir_required")
    if config.second_model.model_dir is None:
        raise EvaluationInputError("second_model_dir_required")
    max_wav_bytes = _bounded_wav_bytes(config.max_wav_bytes)
    samples = load_manifest(
        config.manifest_path,
        config.dataset_root,
        max_wav_bytes=max_wav_bytes,
    )
    # Reject malformed fixed-protocol metadata before provider construction.
    _validate_protocol_samples(samples, config.protocol)
    if baseline_provider is None:
        baseline_provider = provider_factory(config.baseline)
        second_provider = provider_factory(config.second_model)
    assert second_provider is not None
    return evaluate_samples(
        samples,
        baseline_provider,
        second_provider,
        timeout_seconds=config.timeout_seconds,
        max_audio_seconds=config.max_audio_seconds,
        include_transcripts=config.include_transcripts,
        baseline_family=config.baseline.family,
        second_family=config.second_model.family,
        protocol=config.protocol,
    )


def build_argument_parser() -> argparse.ArgumentParser:
    baseline_families = sorted(BASELINE_FAMILIES)
    second_families = sorted(SHERPA_OFFLINE_FAMILIES)
    parser = argparse.ArgumentParser(
        allow_abbrev=False,
        description=(
            "Read-only A/B evaluation of VoiceFly's production X-ASR baseline "
            "or an offline sherpa baseline against one offline sherpa model. "
            "Results are printed to stdout; models are never downloaded. A provider "
            "timeout aborts the run; use an independent process when native inference "
            "must be forcibly killed."
        )
    )
    parser.add_argument("--dataset-root", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--baseline-family", required=True, choices=baseline_families)
    parser.add_argument(
        "--baseline-model-dir",
        type=Path,
        help=(
            "required for offline baseline families; forbidden for voicefly_x_asr, "
            "which uses voice_fly_service.MODEL_DIR"
        ),
    )
    parser.add_argument("--second-family", required=True, choices=second_families)
    parser.add_argument("--second-model-dir", required=True, type=Path)
    parser.add_argument("--language", default="zh")
    parser.add_argument("--num-threads", type=int, choices=range(1, 9), default=1)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--max-audio-seconds", type=float, default=20.0)
    parser.add_argument(
        "--protocol",
        choices=(VOICEFLY_PROTOCOL_V1,),
        help="strictly validate and aggregate the fixed VoiceFly v1 benchmark",
    )
    output_group = parser.add_mutually_exclusive_group()
    output_group.add_argument(
        "--include-transcripts",
        action="store_true",
        help="explicitly include ground truth and recognizer transcripts in JSON",
    )
    output_group.add_argument(
        "--metrics-only",
        action="store_false",
        dest="include_transcripts",
        help="omit all free text and sample metadata (the default)",
    )
    parser.set_defaults(include_transcripts=False)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    args = build_argument_parser().parse_args(argv)
    config = EvaluationConfig(
        dataset_root=args.dataset_root,
        manifest_path=args.manifest,
        baseline=BackendSpec(
            args.baseline_family,
            args.baseline_model_dir,
            num_threads=args.num_threads,
            language=args.language,
        ),
        second_model=BackendSpec(
            args.second_family,
            args.second_model_dir,
            num_threads=args.num_threads,
            language=args.language,
        ),
        timeout_seconds=args.timeout,
        max_audio_seconds=args.max_audio_seconds,
        include_transcripts=args.include_transcripts,
        protocol=args.protocol,
    )
    try:
        report = run_evaluation(config)
    except EvaluationAbortedError as exc:
        error = {
            "schema_version": SCHEMA_VERSION,
            "evaluation": EVALUATION_NAME,
            "complete": False,
            "error": {
                "code": exc.code,
                "type": type(exc).__name__,
                "evaluation_aborted": True,
                "native_call_cancelled": exc.native_call_cancelled,
                "kill_boundary": exc.kill_boundary,
            },
        }
        print(json.dumps(error, ensure_ascii=False, sort_keys=True), file=stderr)
        return 3
    except EvaluationInputError as exc:
        error: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "evaluation": EVALUATION_NAME,
            "complete": False,
            "error": {"code": exc.code, "type": type(exc).__name__},
        }
        if exc.line is not None:
            error["error"]["line"] = exc.line
        print(json.dumps(error, ensure_ascii=False, sort_keys=True), file=stderr)
        return 2
    except Exception as exc:
        error = {
            "schema_version": SCHEMA_VERSION,
            "evaluation": EVALUATION_NAME,
            "complete": False,
            "error": {"code": "evaluation_failed", "type": type(exc).__name__},
        }
        print(json.dumps(error, ensure_ascii=False, sort_keys=True), file=stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, sort_keys=True), file=stdout)
    return 0 if report.get("complete") is True else 4


if __name__ == "__main__":
    raise SystemExit(main())
