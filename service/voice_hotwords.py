"""Hotword decoder configuration for VoiceFlypy.

This module deliberately has no AppKit, sherpa-onnx, or other third-party
imports.  Resource discovery and fallback policy can therefore be tested in a
plain Python process before the macOS service is packaged.
"""

from __future__ import annotations

import os
import math
import json
import hashlib
import tempfile
import stat
import unicodedata
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Mapping, Sequence


DISABLE_ENV = "VOICEFLYPY_DISABLE_HOTWORDS"
HOTWORDS_FILE_ENV = "VOICEFLYPY_HOTWORDS_FILE"
BPE_VOCAB_ENV = "VOICEFLYPY_BPE_VOCAB"
CUSTOM_PHRASES_ENV = "VOICEFLYPY_RIME_CUSTOM_PHRASES"
PERSONAL_USAGE_ENV = "VOICEFLYPY_PERSONAL_USAGE"
LEARNED_PHRASES_ENV = "VOICEFLYPY_RIME_LEARNED_PHRASES"
SYMBOL_ALIASES_ENV = "VOICEFLYPY_ENGINEERING_SYMBOL_ALIASES"
COMBINED_HOTWORDS_ENV = "VOICEFLYPY_COMBINED_HOTWORDS"
DEFAULT_HOTWORDS_SCORE = 1.5
DEFAULT_MAX_ACTIVE_PATHS = 8
CUSTOM_PHRASE_MIN_WEIGHT = 100
LEARNED_PHRASE_DEFAULT_WEIGHT = 200
EXPLICIT_PERSONAL_HOTWORD_SCORE = 4.0
_VALIDATION_CACHE: dict[tuple[str, str, int, int, str], tuple[bool, str]] = {}


class PersonalResourceError(OSError):
    """A configured personal source could not be read without data loss."""


@dataclass(frozen=True)
class DecoderConfig:
    """Resolved decoder mode plus enough detail for useful service logging."""

    enabled: bool
    reason: str
    decoding_method: str
    max_active_paths: int | None = None
    modeling_unit: str | None = None
    bpe_vocab: Path | None = None
    hotwords_file: Path | None = None
    hotwords_score: float | None = None
    resource_error: str | None = None

    def recognizer_kwargs(
        self,
        *,
        include_hotwords_file: bool = True,
    ) -> dict[str, object]:
        if not self.enabled:
            return {"decoding_method": "greedy_search"}
        options = {
            "decoding_method": self.decoding_method,
            "max_active_paths": self.max_active_paths,
            "modeling_unit": self.modeling_unit,
            "bpe_vocab": str(self.bpe_vocab),
            "hotwords_score": self.hotwords_score,
        }
        if include_hotwords_file:
            options["hotwords_file"] = str(self.hotwords_file)
        return options


@dataclass(frozen=True)
class FileFingerprint:
    path: str
    mtime_ns: int
    size: int
    sha256: str


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def validate_bpe_vocab(path: Path) -> tuple[bool, str]:
    """Reject placeholder vocabularies that produce a silent zero-bias graph."""
    try:
        if not path.is_file():
            return False, "not a file"
        count = 0
        with path.open(encoding="utf-8") as handle:
            for number, line in enumerate(handle, 1):
                line = line.rstrip("\r\n")
                if not line:
                    continue
                if "\t" not in line:
                    return False, f"line {number} has no tab-separated score"
                token, score_text = line.rsplit("\t", 1)
                if not token:
                    return False, f"line {number} has an empty token"
                try:
                    score = float(score_text)
                except ValueError:
                    return False, f"line {number} has an invalid score"
                if not math.isfinite(score):
                    return False, f"line {number} has a non-finite score"
                count += 1
    except (OSError, UnicodeError) as exc:
        return False, f"unreadable UTF-8: {exc}"
    if count < 1000:
        return False, f"only {count} token lines; need at least 1000"
    return True, f"{count} token lines"


def validate_hotwords_text(text: str) -> tuple[bool, str]:
    """Validate one already-read ``phrase :score`` hotword snapshot."""
    count = 0
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        phrase, separator, score_text = line.rpartition(":")
        phrase = phrase.strip()
        if not separator or not phrase:
            return False, f"line {number} must end with <non-empty phrase>:<score>"
        try:
            score = float(score_text.strip())
        except ValueError:
            return False, f"line {number} has an invalid score"
        if not math.isfinite(score) or not 0 < score <= 5:
            return False, f"line {number} score must be finite and in (0, 5]"
        count += 1
    if count < 1:
        return False, "contains no hotword entries"
    return True, f"{count} hotword entries"


def validate_hotwords(path: Path) -> tuple[bool, str]:
    """Validate sherpa-onnx ``phrase :score`` lines before enabling biasing."""
    try:
        if not path.is_file():
            return False, "not a file"
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return False, f"unreadable UTF-8: {exc}"
    return validate_hotwords_text(text)


def _decoder_hotword_text(value: str) -> str:
    """Render one natural phrase in sherpa-onnx hotword token form."""
    parts: list[str] = []
    buffer: list[str] = []

    def flush() -> None:
        if buffer:
            parts.extend("".join(buffer).split())
            buffer.clear()

    for char in unicodedata.normalize("NFKC", value.strip()):
        if "\u3400" <= char <= "\u9fff":
            flush()
            parts.append(char)
        elif char.isspace():
            flush()
        else:
            buffer.append(char)
    flush()
    return " ".join(parts)


def _valid_personal_text(value: str) -> bool:
    compact = "".join(value.split())
    return 2 <= len(compact) <= 12 and any("\u3400" <= char <= "\u9fff" for char in compact)


def _custom_phrase_score(weight: int) -> float:
    if weight >= 500:
        return 3.5
    if weight >= 200:
        return 3.0
    return 2.5


def _read_personal_lines(path: Path, kind: str) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise PersonalResourceError(f"{kind} is unreadable UTF-8 at {path}: {exc}") from exc


def load_custom_phrase_hotwords(
    path: Path,
    *,
    allow_missing_weight: bool = False,
) -> dict[str, float]:
    """Promote validated Rime phrases into decoder hotwords.

    Generated ``custom_phrase.txt`` rows always have three fields.  The
    mutable learned-phrase contract also accepts two fields, matching the
    Swift store, and assigns the same explicit-learning weight used by new
    writes.
    """
    result: dict[str, float] = {}
    for number, line in enumerate(_read_personal_lines(path, "Rime phrase source"), 1):
        if not line or line.lstrip().startswith("#"):
            continue
        fields = line.split("\t")
        valid_field_counts = {2, 3} if allow_missing_weight else {3}
        if len(fields) not in valid_field_counts:
            raise PersonalResourceError(
                f"Rime phrase source {path} line {number} needs "
                f"{'2 or 3' if allow_missing_weight else '3'} tab-separated fields"
            )
        text = fields[0].strip()
        code = fields[1].strip()
        if (
            not text
            or (allow_missing_weight and not 1 <= len(text) <= 12)
            or not code
            or not code.isascii()
            or not code.isalpha()
        ):
            raise PersonalResourceError(
                f"Rime phrase source {path} line {number} has invalid text or Flypy code"
            )
        if len(fields) == 2:
            weight = LEARNED_PHRASE_DEFAULT_WEIGHT
        else:
            try:
                weight = int(fields[2].strip())
            except ValueError as exc:
                raise PersonalResourceError(
                    f"Rime phrase source {path} line {number} has invalid integer weight"
                ) from exc
        if weight < CUSTOM_PHRASE_MIN_WEIGHT or not _valid_personal_text(text):
            continue
        decoder_text = _decoder_hotword_text(text)
        if decoder_text:
            score = _custom_phrase_score(weight)
            if allow_missing_weight:
                # This source is written only by the explicit double-space
                # learning gesture.  Keep that deliberate choice materially
                # stronger than passive candidate-frequency promotion.
                score = max(score, EXPLICIT_PERSONAL_HOTWORD_SCORE)
            result[decoder_text] = max(result.get(decoder_text, 0.0), score)
    return result


def load_personal_usage_hotwords(path: Path) -> dict[str, float]:
    """Load only phrases explicitly saved for speech recognition.

    Ordinary candidate selections measure keyboard frequency, not acoustic
    recognition mistakes. Feeding them into the context graph lets common
    prefixes (for example ``讲讲`` after ``讲``) suppress otherwise valid
    speech continuations. ``explicit_count`` is the cross-modality boundary:
    the user's explicit learning gesture may bias ASR, passive typing may not.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PersonalResourceError(f"personal usage is unreadable at {path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != "1.0.0":
        raise PersonalResourceError(
            f"personal usage {path} has an unsupported or missing schema_version"
        )
    terms = payload.get("terms")
    if not isinstance(terms, dict):
        raise PersonalResourceError(f"personal usage {path} must contain an object named terms")
    result: dict[str, float] = {}
    for key, record in terms.items():
        if not isinstance(key, str) or not isinstance(record, dict):
            raise PersonalResourceError(
                f"personal usage {path} contains a non-string term or non-object record"
            )
        selections = record.get("selection_count")
        explicit = record.get("explicit_count")
        code = record.get("flypy_code")
        updated_at = record.get("updated_at")
        if type(selections) is not int or type(explicit) is not int or selections < 0 or explicit < 0:
            raise PersonalResourceError(
                f"personal usage {path} term {key!r} has invalid counters"
            )
        if (
            not 2 <= len(key) <= 12
            or any(char.isspace() for char in key)
            or not isinstance(code, str)
            or not code
            or not code.isascii()
            or not code.isalpha()
            or not isinstance(updated_at, str)
        ):
            raise PersonalResourceError(
                f"personal usage {path} term {key!r} does not match schema 1.0.0"
            )
        if not _valid_personal_text(key):
            continue
        if explicit < 1:
            continue
        decoder_text = _decoder_hotword_text(key)
        if decoder_text:
            result[decoder_text] = max(
                result.get(decoder_text, 0.0),
                EXPLICIT_PERSONAL_HOTWORD_SCORE,
            )
    return result


def load_symbol_alias_hotwords(path: Path) -> dict[str, float]:
    """Bias recognition toward spoken engineering-symbol names, not glyphs."""
    result: dict[str, float] = {}
    for number, line in enumerate(_read_personal_lines(path, "symbol alias source"), 1):
        if not line or line.lstrip().startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) < 3:
            raise PersonalResourceError(
                f"symbol alias source {path} line {number} needs 3 tab-separated fields"
            )
        spoken = fields[0].strip()
        mode = fields[2].strip()
        if not spoken or not fields[1].strip() or mode not in {"explicit", "after_number"}:
            raise PersonalResourceError(
                f"symbol alias source {path} line {number} has invalid required fields"
            )
        if not _valid_personal_text(spoken):
            continue
        decoder_text = _decoder_hotword_text(spoken)
        if decoder_text:
            result[decoder_text] = max(result.get(decoder_text, 0.0), 2.5)
    return result


def _read_scored_hotwords(path: Path) -> dict[str, float]:
    result: dict[str, float] = {}
    try:
        handle = path.open(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise OSError(f"base hotwords are unreadable at {path}: {exc}") from exc
    with handle:
        for number, line in enumerate(handle, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            phrase, separator, score_text = line.rpartition(":")
            if not separator:
                raise OSError(f"base hotwords {path} line {number} is malformed")
            phrase = phrase.strip()
            try:
                score = float(score_text.strip())
            except ValueError as exc:
                raise OSError(f"base hotwords {path} line {number} has invalid score") from exc
            if not phrase or not math.isfinite(score) or not 0 < score <= 5:
                raise OSError(f"base hotwords {path} line {number} has invalid phrase or score")
            result[phrase] = max(result.get(phrase, 0.0), score)
    return result


def _first_file(paths: Sequence[Path], kind: str) -> Path | None:
    """Return the first regular default file; only ENOENT is optional."""
    for path in paths:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise PersonalResourceError(f"cannot inspect {kind} at {path}: {exc}") from exc
        if not stat.S_ISREG(metadata.st_mode):
            raise PersonalResourceError(f"{kind} is not a regular file at {path}")
        return path
    return None


def _atomic_write_validated_hotwords(path: Path, content: str) -> None:
    """Validate a unique completed temp file before atomically publishing it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise OSError(f"refusing symlink output: {path}")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.{os.getpid()}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        valid, detail = validate_hotwords(temporary)
        if not valid:
            raise OSError(f"combined hotwords failed validation before publish: {detail}")
        try:
            if path.read_text(encoding="utf-8") == content:
                return
        except (OSError, UnicodeError):
            pass
        os.replace(temporary, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def build_combined_hotwords(
    base_hotwords: Path,
    output: Path,
    *,
    custom_phrases: Path | None = None,
    learned_phrases: Path | None = None,
    personal_usage: Path | None = None,
    symbol_aliases: Path | None = None,
) -> tuple[Path, int]:
    """Merge immutable base terms with personal keyboard learning atomically."""
    additions: dict[str, float] = {}
    if custom_phrases is not None:
        additions.update(load_custom_phrase_hotwords(custom_phrases))
    if learned_phrases is not None:
        for phrase, score in load_custom_phrase_hotwords(
            learned_phrases,
            allow_missing_weight=True,
        ).items():
            additions[phrase] = max(additions.get(phrase, 0.0), score)
    if personal_usage is not None:
        for phrase, score in load_personal_usage_hotwords(personal_usage).items():
            additions[phrase] = max(additions.get(phrase, 0.0), score)
    if symbol_aliases is not None:
        for phrase, score in load_symbol_alias_hotwords(symbol_aliases).items():
            additions[phrase] = max(additions.get(phrase, 0.0), score)
    if not additions and all(
        source is None
        for source in (custom_phrases, learned_phrases, personal_usage, symbol_aliases)
    ):
        return base_hotwords, 0

    merged = _read_scored_hotwords(base_hotwords)
    for phrase, score in additions.items():
        merged[phrase] = max(merged.get(phrase, 0.0), score)
    content = "".join(
        f"{phrase} :{score:g}\n"
        for phrase, score in sorted(merged.items(), key=lambda item: (item[0].casefold(), item[0]))
    )
    _atomic_write_validated_hotwords(output, content)
    return output, len(additions)


def _first_valid(
    kind: str,
    paths: Sequence[Path],
    validator: Callable[[Path], tuple[bool, str]],
) -> tuple[Path | None, tuple[str, ...]]:
    seen: set[str] = set()
    failures = []
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        try:
            stat = path.stat()
            digest = _file_sha256(path)
        except OSError:
            continue
        cache_key = (kind, key, stat.st_mtime_ns, stat.st_size, digest)
        result = _VALIDATION_CACHE.get(cache_key)
        if result is None:
            result = validator(path)
            _VALIDATION_CACHE[cache_key] = result
        valid, detail = result
        if valid:
            return path, tuple(failures)
        failures.append(f"{path}: {detail}")
    return None, tuple(failures)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_fingerprint(path: Path | None) -> FileFingerprint | None:
    """Return resource identity including bytes, not only mutable metadata."""
    if path is None:
        return None
    try:
        stat = path.stat()
        digest = _file_sha256(path)
    except OSError:
        return None
    return FileFingerprint(str(path), stat.st_mtime_ns, stat.st_size, digest)


def decoder_resource_fingerprint(
    decoder: DecoderConfig,
) -> tuple[bool, str, FileFingerprint | None, FileFingerprint | None]:
    """Stable comparison value used by the service's idle-time hot reload."""
    return (
        decoder.enabled,
        decoder.decoding_method,
        file_fingerprint(decoder.bpe_vocab),
        file_fingerprint(decoder.hotwords_file),
    )


def should_reload_decoder(previous_fingerprint, requested: DecoderConfig) -> bool:
    """Pure idle-time reload decision used by the AppKit service."""
    return decoder_resource_fingerprint(requested) != previous_fingerprint


def _configured_path(env: Mapping[str, str], name: str) -> tuple[Path, ...]:
    value = env.get(name, "").strip()
    return (Path(value).expanduser(),) if value else ()


def _configured_or_first_file(
    environment: Mapping[str, str],
    env_name: str,
    defaults: Sequence[Path],
    kind: str,
) -> Path | None:
    """Treat an explicit path as required; defaults remain optional."""
    configured = _configured_path(environment, env_name)
    return configured[0] if configured else _first_file(defaults, kind)


def _personal_resource_paths(
    resource_root: Path,
    environment: Mapping[str, str],
    live_lexicon_root: Path,
    *,
    include_default_user_paths: bool,
) -> tuple[Path | None, Path | None, Path | None, Path | None, Path]:
    default_custom_phrases = (
        (Path.home() / "Library/Rime-VoiceFlypy/custom_phrase.txt",)
        if include_default_user_paths
        else ()
    )
    custom_phrases = _configured_or_first_file(
        environment,
        CUSTOM_PHRASES_ENV,
        default_custom_phrases,
        "static custom phrase source",
    )
    default_learned_phrases = (
        (Path.home() / "Library/Rime-VoiceFlypy/voiceflypy_learned_phrases.txt",)
        if include_default_user_paths
        else ()
    )
    learned_phrases = _configured_or_first_file(
        environment,
        LEARNED_PHRASES_ENV,
        default_learned_phrases,
        "learned phrase source",
    )
    default_personal_usage = (
        (
            Path.home()
            / "Library/Application Support/VoiceFlypy/Lexicon/personal_usage.json"
        ),
    ) if include_default_user_paths else (live_lexicon_root / "personal_usage.json",)
    personal_usage = _configured_or_first_file(
        environment,
        PERSONAL_USAGE_ENV,
        default_personal_usage,
        "personal usage source",
    )
    symbol_aliases = _configured_or_first_file(
        environment,
        SYMBOL_ALIASES_ENV,
        (
            resource_root / "lexicon" / "engineering_symbol_aliases.tsv",
            live_lexicon_root / "engineering_symbol_aliases.tsv",
        ),
        "symbol alias source",
    )
    combined_output = _combined_hotword_output(
        environment,
        live_lexicon_root,
        include_default_user_paths=include_default_user_paths,
    )
    return custom_phrases, learned_phrases, personal_usage, symbol_aliases, combined_output


def _combined_hotword_output(
    environment: Mapping[str, str],
    live_lexicon_root: Path,
    *,
    include_default_user_paths: bool,
) -> Path:
    configured_output = _configured_path(environment, COMBINED_HOTWORDS_ENV)
    return configured_output[0] if configured_output else (
        Path.home() / "Library/Caches/VoiceFlypy/lexicon/combined_hotwords.txt"
        if include_default_user_paths
        else live_lexicon_root / "combined_hotwords.txt"
    )


def hotword_candidates(
    resource_root: Path,
    source_root: Path,
    env: Mapping[str, str],
    live_lexicon_root: Path | None = None,
) -> tuple[Path, ...]:
    """Return hotword files in precedence order.

    An explicit environment override is first.  The generated iCloud snapshot
    is next so a synchronized master lexicon can take effect after a service
    restart.  The signed app bundle remains the reliable offline fallback.
    """
    if live_lexicon_root is None:
        live_lexicon_root = (
            Path.home()
            / "Library/Mobile Documents/com~apple~CloudDocs/AppConfig/RimeSync"
            / "VoiceFlypyLexicon"
        )
    return (
        *_configured_path(env, HOTWORDS_FILE_ENV),
        live_lexicon_root / "hotwords.txt",
        live_lexicon_root / "speech" / "hotwords.txt",
        resource_root / "lexicon" / "hotwords.txt",
        resource_root / "lexicon" / "speech" / "hotwords.txt",
        source_root / "lexicon" / "generated" / "simon_all" / "speech" / "hotwords.txt",
        source_root / "lexicon" / "build" / "simon_all" / "speech" / "hotwords.txt",
        source_root / "lexicon" / "hotwords.txt",
    )


def bpe_vocab_candidates(
    model_dir: Path,
    resource_root: Path,
    source_root: Path,
    env: Mapping[str, str],
) -> tuple[Path, ...]:
    return (
        *_configured_path(env, BPE_VOCAB_ENV),
        resource_root / "lexicon" / "bpe.vocab",
        source_root / "lexicon" / "bpe.vocab",
        model_dir / "bpe.vocab",
        resource_root / "models" / "streaming-zipformer" / "bpe.vocab",
    )


def resolve_decoder_config(
    model_dir: Path,
    resource_root: Path,
    source_root: Path,
    *,
    env: Mapping[str, str] | None = None,
    live_lexicon_root: Path | None = None,
) -> DecoderConfig:
    """Resolve hotword resources, degrading safely to greedy search."""
    environment = os.environ if env is None else env
    include_default_user_paths = live_lexicon_root is None
    if _truthy(environment.get(DISABLE_ENV)):
        return DecoderConfig(False, f"disabled by {DISABLE_ENV}", "greedy_search")

    bpe_vocab, bpe_failures = _first_valid(
        "bpe",
        bpe_vocab_candidates(model_dir, resource_root, source_root, environment),
        validate_bpe_vocab,
    )
    hotwords_file, hotword_failures = _first_valid(
        "hotwords",
        hotword_candidates(
            resource_root,
            source_root,
            environment,
            live_lexicon_root=live_lexicon_root,
        ),
        validate_hotwords,
    )
    if live_lexicon_root is None:
        live_lexicon_root = (
            Path.home()
            / "Library/Mobile Documents/com~apple~CloudDocs/AppConfig/RimeSync"
            / "VoiceFlypyLexicon"
        )
    personal_count = 0
    personal_error = None
    if hotwords_file is not None:
        combined_output = _combined_hotword_output(
            environment,
            live_lexicon_root,
            include_default_user_paths=include_default_user_paths,
        )
        try:
            (
                custom_phrases,
                learned_phrases,
                personal_usage,
                symbol_aliases,
                _resolved_output,
            ) = _personal_resource_paths(
                resource_root,
                environment,
                live_lexicon_root,
                include_default_user_paths=include_default_user_paths,
            )
            hotwords_file, personal_count = build_combined_hotwords(
                hotwords_file,
                combined_output,
                custom_phrases=custom_phrases,
                learned_phrases=learned_phrases,
                personal_usage=personal_usage,
                symbol_aliases=symbol_aliases,
            )
        except OSError as exc:
            personal_error = f"personal hotword merge failed: {exc}"
            # The builder never replaces the output before validation.  Keep
            # the previous validated generation when one exists; otherwise
            # retain the validated immutable base for first-launch usability.
            previous_valid, _detail = validate_hotwords(combined_output)
            if previous_valid:
                hotwords_file = combined_output
    if bpe_vocab is None or hotwords_file is None:
        missing = []
        if bpe_vocab is None:
            missing.append("bpe.vocab")
        if hotwords_file is None:
            missing.append("hotwords.txt")
        invalid_details = (*bpe_failures, *hotword_failures)
        detail = "; ".join(invalid_details[:2])
        reason = "missing or invalid " + " and ".join(missing)
        if detail:
            reason += f" ({detail})"
        return DecoderConfig(
            False,
            reason,
            "greedy_search",
            bpe_vocab=bpe_vocab,
            hotwords_file=hotwords_file,
            resource_error=personal_error,
        )

    reason = f"validated hotword resources; personal entries={personal_count}"
    if personal_error:
        reason += f"; {personal_error}; kept last known-good hotwords"
    return DecoderConfig(
        True,
        reason,
        "modified_beam_search",
        max_active_paths=DEFAULT_MAX_ACTIVE_PATHS,
        modeling_unit="bpe",
        bpe_vocab=bpe_vocab,
        hotwords_file=hotwords_file,
        hotwords_score=DEFAULT_HOTWORDS_SCORE,
        resource_error=personal_error,
    )


def create_recognizer_with_fallback(
    factory: Callable[..., object],
    base_kwargs: Mapping[str, object],
    decoder: DecoderConfig,
    *,
    logger: Callable[[str], None] | None = None,
    include_hotwords_file: bool = True,
) -> tuple[object, DecoderConfig]:
    """Create a recognizer, retrying once with greedy search if hotwords fail."""
    log = logger or (lambda _message: None)
    requested_kwargs = dict(base_kwargs)
    requested_kwargs.update(
        decoder.recognizer_kwargs(include_hotwords_file=include_hotwords_file)
    )
    try:
        return factory(**requested_kwargs), decoder
    except Exception as exc:
        if not decoder.enabled:
            raise
        log(
            "[服务] 热词解码器初始化失败，自动回退 greedy_search："
            f"{type(exc).__name__}: {exc}"
        )
        fallback = replace(
            decoder,
            enabled=False,
            reason=f"hotword initialization failed: {type(exc).__name__}: {exc}",
            decoding_method="greedy_search",
            max_active_paths=None,
            modeling_unit=None,
            bpe_vocab=None,
            hotwords_file=None,
            hotwords_score=None,
        )
        fallback_kwargs = dict(base_kwargs)
        fallback_kwargs.update(fallback.recognizer_kwargs())
        return factory(**fallback_kwargs), fallback
