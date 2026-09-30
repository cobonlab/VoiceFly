"""Bounded, fail-closed finalization for VoiceFlypy speech recognition.

The streaming recognizer remains responsible for low-latency partial text.  At
the end of a recording this module can give the same in-memory audio to a
second local model and validate its candidate before it is ever allowed to
replace the established result.

Privacy and reliability are deliberate properties of the interface:

* audio is kept in memory and bounded by duration;
* the shipped HTTP backend only accepts loopback endpoints;
* request/result reprs never contain recognized text;
* every unavailable, late, malformed, or suspicious answer falls back to the
  established text;
* shadow mode runs the new path without changing committed text.
"""

from __future__ import annotations

import difflib
import io
import json
import math
import os
import re
import socket
import threading
import time
import unicodedata
import urllib.error
import urllib.request
import uuid
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlsplit

import numpy as np


REFINER_MODES = frozenset({"off", "shadow", "active"})
SHERPA_OFFLINE_FAMILIES = frozenset(
    {"paraformer", "sense_voice", "qwen3_asr"}
)
MAX_BACKEND_RESPONSE_BYTES = 1024 * 1024
MAX_REFINER_TIMEOUT_SECONDS = 15.0
MAX_REFINER_AUDIO_SECONDS = 30.0
MAX_CAPTURE_SAMPLE_RATE = 384000
_PROTECTED_TOKEN_RE = re.compile(
    r"[A-Za-z]+[A-Za-z0-9._/+:-]*|[-+]?\d+(?:[.:/-]\d+)*"
)
# Unicode symbol categories (currency, math, modifiers, emoji, and other
# symbols) are protected without maintaining an inevitably incomplete list.
# These ASCII punctuation marks also carry technical meaning but Unicode
# classifies them as punctuation rather than symbols.
_PROTECTED_TECHNICAL_PUNCTUATION = frozenset(
    "$#@&_|\\^~%*/-:()[]{}<>`"
)
_IDENTIFIER_PREFIX_RE = re.compile(r"[A-Za-z]{1,4}")
_IDENTIFIER_DIGITS_RE = re.compile(r"[0-9]{1,32}")
_IDENTIFIER_SUFFIX_RE = re.compile(r"[A-Za-z0-9-]{0,8}")
_IDENTIFIER_SURFACE_SUFFIX_RE = re.compile(r"[A-Za-z-]{0,8}")
_IDENTIFIER_SEPARATOR_RE = re.compile(r"[ \t,，;；、]+")
_RAW_IDENTIFIER_ITEM = (
    r"[A-Za-z]{1,4}[ \t]*"
    r"[0-9零〇一二两三四五六七八九幺十百千万亿]"
    r"(?:[ \t]*[0-9零〇一二两三四五六七八九幺十百千万亿点.:/\-])*"
)
_RAW_IDENTIFIER_SEQUENCE_RE = re.compile(
    rf"{_RAW_IDENTIFIER_ITEM}(?:[ \t,，;；、]+{_RAW_IDENTIFIER_ITEM})*"
)


def _finite_positive(value: Any, *, name: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return parsed


def _environment_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    return _finite_positive(raw, name=name)


def _bounded_positive(value: Any, *, name: str, maximum: float) -> float:
    """Validate a positive number and enforce a non-configurable safety cap."""
    return min(_finite_positive(value, name=name), maximum)


@dataclass(frozen=True, repr=False)
class AudioSnapshot:
    """One immutable, mono float32 recording held only in memory."""

    samples: np.ndarray = field(compare=False)
    sample_rate: int

    def __post_init__(self) -> None:
        # Always own the array.  Marking a caller-owned array read-only would
        # be a surprising mutation and would not make the snapshot immutable
        # if another writable view still existed.
        samples = np.array(self.samples, dtype=np.float32, copy=True).reshape(-1)
        if samples.ndim != 1 or not samples.size:
            raise ValueError("audio snapshot must contain samples")
        if not np.isfinite(samples).all():
            raise ValueError("audio snapshot contains non-finite samples")
        if int(self.sample_rate) <= 0:
            raise ValueError("sample rate must be positive")
        if int(self.sample_rate) > MAX_CAPTURE_SAMPLE_RATE:
            raise ValueError("sample rate exceeds safety limit")
        samples.setflags(write=False)
        object.__setattr__(self, "samples", samples)
        object.__setattr__(self, "sample_rate", int(self.sample_rate))

    @property
    def duration_seconds(self) -> float:
        return self.samples.size / self.sample_rate

    def __repr__(self) -> str:
        return (
            "AudioSnapshot("
            f"samples={self.samples.size}, sample_rate={self.sample_rate}, "
            f"duration_seconds={self.duration_seconds:.3f})"
        )


class BoundedAudioBuffer:
    """Collect float32 microphone frames without permitting unbounded growth.

    Once the limit is crossed, all retained samples are discarded and the
    buffer remains overflowed.  A secondary model must never receive a silent
    truncation that could change the meaning of the utterance.
    """

    def __init__(self, max_seconds: float = 20.0) -> None:
        self.max_seconds = _bounded_positive(
            max_seconds,
            name="max_seconds",
            maximum=MAX_REFINER_AUDIO_SECONDS,
        )
        self._sample_rate: int | None = None
        self._sample_count = 0
        self._chunks: list[np.ndarray] = []
        self._reason = ""

    @property
    def sample_count(self) -> int:
        return self._sample_count

    @property
    def overflowed(self) -> bool:
        return self._reason == "duration_limit"

    @property
    def invalid(self) -> bool:
        return bool(self._reason and self._reason != "duration_limit")

    @property
    def unavailable_reason(self) -> str:
        return self._reason

    def append(self, samples: Any, sample_rate: int) -> bool:
        """Copy one frame into the buffer; return whether it was retained."""
        if self._reason:
            return False
        try:
            rate = int(sample_rate)
        except (TypeError, ValueError):
            self._discard("invalid_sample_rate")
            return False
        if rate <= 0:
            self._discard("invalid_sample_rate")
            return False
        if rate > MAX_CAPTURE_SAMPLE_RATE:
            self._discard("sample_rate_too_high")
            return False
        if self._sample_rate is None:
            self._sample_rate = rate
        elif rate != self._sample_rate:
            self._discard("sample_rate_changed")
            return False

        try:
            chunk = np.asarray(samples, dtype=np.float32).reshape(-1)
        except (TypeError, ValueError):
            self._discard("invalid_samples")
            return False
        if not chunk.size:
            return True
        if not np.isfinite(chunk).all():
            self._discard("non_finite_samples")
            return False

        max_samples = int(self.max_seconds * rate)
        if self._sample_count + chunk.size > max_samples:
            self._discard("duration_limit")
            return False
        self._chunks.append(chunk.copy())
        self._sample_count += int(chunk.size)
        return True

    def _discard(self, reason: str) -> None:
        self._chunks.clear()
        self._sample_count = 0
        self._reason = reason

    def snapshot(self) -> AudioSnapshot | None:
        if self._reason or not self._chunks or self._sample_rate is None:
            return None
        samples = np.concatenate(self._chunks)
        return AudioSnapshot(samples=samples, sample_rate=self._sample_rate)

    def clear(self) -> None:
        self._chunks.clear()
        self._sample_count = 0
        self._sample_rate = None
        self._reason = ""


@dataclass(frozen=True, repr=False)
class RecognitionEvidence:
    """Evidence retained from one acoustic segment, without logging it."""

    text: str = field(repr=False)
    tokens: tuple[str, ...] = field(default=(), repr=False)
    timestamps: tuple[float, ...] = ()
    token_probabilities: tuple[float, ...] = ()
    context_scores: tuple[float, ...] = ()

    def __repr__(self) -> str:
        return (
            "RecognitionEvidence("
            f"chars={len(self.text)}, tokens={len(self.tokens)}, "
            f"timestamps={len(self.timestamps)})"
        )


@dataclass(frozen=True, repr=False)
class FinalizationRequest:
    session_id: str
    raw_segments: tuple[str, ...] = field(repr=False)
    raw_text: str = field(repr=False)
    baseline_text: str = field(repr=False)
    deadline_monotonic: float
    audio: AudioSnapshot | None = field(default=None, repr=False, compare=False)
    audio_unavailable_reason: str = ""
    evidence: tuple[RecognitionEvidence, ...] = field(default=(), repr=False)
    locale: str = "zh-CN"

    def __repr__(self) -> str:
        duration = self.audio.duration_seconds if self.audio is not None else 0.0
        return (
            "FinalizationRequest("
            f"session={self.session_id!r}, raw_chars={len(self.raw_text)}, "
            f"baseline_chars={len(self.baseline_text)}, "
            f"audio_seconds={duration:.3f}, evidence={len(self.evidence)})"
        )


@dataclass(frozen=True, repr=False)
class BackendCandidate:
    text: str = field(default="", repr=False)
    backend: str = "unknown"
    confidence: float | None = None
    kind: str = "dictation"
    structured: Mapping[str, Any] | None = field(default=None, repr=False)

    def __repr__(self) -> str:
        return (
            "BackendCandidate("
            f"chars={len(self.text)}, backend={self.backend!r}, "
            f"confidence={self.confidence!r}, kind={self.kind!r}, "
            f"structured={self.structured is not None})"
        )


@dataclass(frozen=True, repr=False)
class RefinementResult:
    status: str
    text: str = field(repr=False)
    backend: str
    latency_ms: float
    reason: str = ""
    confidence: float | None = None
    kind: str = "dictation"
    candidate_text: str | None = field(default=None, repr=False)
    committed_text_changed: bool = False

    def __repr__(self) -> str:
        return (
            "RefinementResult("
            f"status={self.status!r}, chars={len(self.text)}, "
            f"candidate_chars={len(self.candidate_text or '')}, "
            f"backend={self.backend!r}, latency_ms={self.latency_ms:.3f}, "
            f"reason={self.reason!r}, confidence={self.confidence!r}, "
            f"kind={self.kind!r}, committed_text_changed="
            f"{self.committed_text_changed})"
        )


class RefinementBackend(Protocol):
    name: str

    def refine(
        self,
        request: FinalizationRequest,
        *,
        timeout_seconds: float,
    ) -> BackendCandidate:
        ...


def _audio_as_wav(audio: AudioSnapshot) -> bytes:
    """Encode mono float32 audio as an in-memory 16-bit PCM WAV."""
    clipped = np.clip(audio.samples, -1.0, 1.0)
    pcm = np.rint(clipped * 32767.0).astype("<i2", copy=False)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(audio.sample_rate)
        writer.writeframes(pcm.tobytes())
    return buffer.getvalue()


def _encode_multipart(
    fields: Mapping[str, str],
    *,
    filename: str,
    file_bytes: bytes,
) -> tuple[bytes, str]:
    boundary = f"----VoiceFlypy{uuid.uuid4().hex}"
    marker = boundary.encode("ascii")
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend(
            (
                b"--" + marker + b"\r\n",
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(
                    "utf-8"
                ),
                value.encode("utf-8"),
                b"\r\n",
            )
        )
    chunks.extend(
        (
            b"--" + marker + b"\r\n",
            (
                'Content-Disposition: form-data; name="file"; '
                f'filename="{filename}"\r\n'
            ).encode("utf-8"),
            b"Content-Type: audio/wav\r\n\r\n",
            file_bytes,
            b"\r\n",
            b"--" + marker + b"--\r\n",
        )
    )
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _require_loopback_http_endpoint(endpoint: str) -> str:
    parsed = urlsplit(endpoint)
    if parsed.scheme != "http":
        raise ValueError("semantic endpoint must use plain HTTP on loopback")
    if parsed.username or parsed.password:
        raise ValueError("semantic endpoint must not contain credentials")
    if parsed.hostname not in {"127.0.0.1", "::1"}:
        raise ValueError("semantic endpoint must be loopback-only")
    if parsed.query or parsed.fragment:
        raise ValueError("semantic endpoint must not contain query or fragment")
    if not parsed.path or parsed.path == "/":
        raise ValueError("semantic endpoint must include an inference path")
    return endpoint


class WhisperServerBackend:
    """Use a long-lived local whisper.cpp server as the second ASR model."""

    name = "whisper.cpp"

    def __init__(
        self,
        endpoint: str = "http://127.0.0.1:18080/inference",
        *,
        language: str = "auto",
        prompt: str = "",
        opener: Any | None = None,
    ) -> None:
        self.endpoint = _require_loopback_http_endpoint(endpoint)
        self.language = language.strip() or "auto"
        self.prompt = prompt.strip()
        if not re.fullmatch(r"(?:auto|[A-Za-z]{2,3})", self.language):
            raise ValueError("semantic language is invalid")
        if len(self.prompt) > 1024 or "\x00" in self.prompt:
            raise ValueError("semantic prompt is invalid")
        self._opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _RejectRedirectHandler(),
        )

    def refine(
        self,
        request: FinalizationRequest,
        *,
        timeout_seconds: float,
    ) -> BackendCandidate:
        if request.audio is None:
            raise ValueError("audio is unavailable")
        timeout = _finite_positive(timeout_seconds, name="timeout_seconds")
        fields = {
            "response_format": "verbose_json",
            "language": self.language,
            "temperature": "0.0",
            "temperature_inc": "0.0",
        }
        if self.prompt:
            fields["prompt"] = self.prompt
        body, content_type = _encode_multipart(
            fields,
            filename="voiceflypy-session.wav",
            file_bytes=_audio_as_wav(request.audio),
        )
        http_request = urllib.request.Request(
            self.endpoint,
            data=body,
            headers={
                "Content-Type": content_type,
                "Content-Length": str(len(body)),
                "User-Agent": "VoiceFlypy-Semantic-Shadow/1",
            },
            method="POST",
        )
        with self._opener.open(http_request, timeout=timeout) as response:
            raw = response.read(MAX_BACKEND_RESPONSE_BYTES + 1)
        if len(raw) > MAX_BACKEND_RESPONSE_BYTES:
            raise ValueError("semantic backend response exceeds size limit")
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
            raise ValueError("semantic backend response has no text")
        return BackendCandidate(
            text=payload["text"].strip(),
            backend=self.name,
            confidence=_whisper_confidence(payload),
            kind="dictation",
        )


class _RejectRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Keep microphone bytes on the configured loopback request only."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _whisper_confidence(payload: Mapping[str, Any]) -> float | None:
    word_probabilities: list[float] = []
    weighted_logprob = 0.0
    weighted_duration = 0.0
    segments = payload.get("segments")
    if not isinstance(segments, list):
        return None
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        words = segment.get("words")
        if isinstance(words, list):
            for word in words:
                if not isinstance(word, dict):
                    continue
                probability = word.get("probability")
                if isinstance(probability, (int, float)) and math.isfinite(probability):
                    word_probabilities.append(max(0.0, min(1.0, float(probability))))
        avg_logprob = segment.get("avg_logprob")
        start = segment.get("start")
        end = segment.get("end")
        if (
            all(isinstance(value, (int, float)) for value in (avg_logprob, start, end))
            and all(math.isfinite(float(value)) for value in (avg_logprob, start, end))
        ):
            duration = max(0.001, float(end) - float(start))
            weighted_logprob += float(avg_logprob) * duration
            weighted_duration += duration
    if word_probabilities:
        return sum(word_probabilities) / len(word_probabilities)
    if weighted_duration:
        return max(0.0, min(1.0, math.exp(weighted_logprob / weighted_duration)))
    return None


_SHERPA_MODEL_LAYOUTS: Mapping[str, Mapping[str, str]] = {
    "paraformer": {
        "paraformer": "model.int8.onnx",
        "tokens": "tokens.txt",
    },
    "sense_voice": {
        "model": "model.int8.onnx",
        "tokens": "tokens.txt",
    },
    "qwen3_asr": {
        "conv_frontend": "conv_frontend.onnx",
        "encoder": "encoder.int8.onnx",
        "decoder": "decoder.int8.onnx",
        "tokenizer": "tokenizer",
    },
}
_QWEN_TOKENIZER_FILES = (
    "vocab.json",
    "merges.txt",
    "tokenizer_config.json",
)


def _validate_sherpa_model_directory(
    model_dir: str | os.PathLike[str],
    *,
    family: str,
) -> tuple[Path, dict[str, Path]]:
    """Resolve one known sherpa model layout without accepting remote inputs."""
    try:
        supplied = Path(model_dir)
    except (TypeError, ValueError) as exc:
        raise ValueError("sherpa model directory is invalid") from exc
    if not supplied.is_absolute():
        raise ValueError("sherpa model directory must be an absolute local path")
    if supplied.is_symlink():
        raise ValueError("sherpa model directory must not be a symbolic link")
    try:
        root = supplied.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("sherpa model directory does not exist") from exc
    if not root.is_dir():
        raise ValueError("sherpa model path must be a directory")

    assets: dict[str, Path] = {}
    for key, relative_name in _SHERPA_MODEL_LAYOUTS[family].items():
        supplied_asset = root / relative_name
        if supplied_asset.is_symlink():
            raise ValueError(f"sherpa asset must not be a symbolic link: {relative_name}")
        try:
            asset = supplied_asset.resolve(strict=True)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ValueError(f"required sherpa asset is missing: {relative_name}") from exc
        if root not in asset.parents:
            raise ValueError("sherpa asset escapes the model directory")
        if key == "tokenizer":
            if not asset.is_dir():
                raise ValueError("qwen tokenizer must be a directory")
            for filename in _QWEN_TOKENIZER_FILES:
                supplied_tokenizer_file = asset / filename
                if supplied_tokenizer_file.is_symlink():
                    raise ValueError(
                        f"qwen tokenizer asset must not be a symbolic link: {filename}"
                    )
                try:
                    tokenizer_file = supplied_tokenizer_file.resolve(strict=True)
                except (OSError, RuntimeError, ValueError) as exc:
                    raise ValueError(
                        f"required qwen tokenizer asset is missing: {filename}"
                    ) from exc
                if asset not in tokenizer_file.parents:
                    raise ValueError("qwen tokenizer asset escapes its directory")
                if not tokenizer_file.is_file() or tokenizer_file.stat().st_size <= 0:
                    raise ValueError(
                        f"qwen tokenizer asset must be a non-empty file: {filename}"
                    )
        elif not asset.is_file() or asset.stat().st_size <= 0:
            raise ValueError(
                f"sherpa asset must be a non-empty file: {relative_name}"
            )
        assets[key] = asset
    return root, assets


class SherpaOfflineBackend:
    """Lazily run a bundled sherpa-onnx offline model on one complete clip.

    sherpa's Python bindings release the GIL during model construction and
    decoding, but they do not promise that a recognizer can be shared across
    arbitrary caller threads.  One private lock therefore covers both lazy
    initialization and the complete create/accept/decode/result transaction.
    """

    def __init__(
        self,
        family: str,
        model_dir: str | os.PathLike[str],
        *,
        num_threads: int = 1,
        language: str = "zh",
        recognizer_factory: Callable[..., Any] | None = None,
    ) -> None:
        normalized_family = str(family).strip().casefold().replace("-", "_")
        if normalized_family not in SHERPA_OFFLINE_FAMILIES:
            raise ValueError("unsupported sherpa offline family")
        if (
            isinstance(num_threads, bool)
            or not isinstance(num_threads, int)
            or not 1 <= num_threads <= 8
        ):
            raise ValueError("sherpa num_threads must be an integer from 1 to 8")
        normalized_language = str(language).strip().casefold()
        if normalized_language not in {"auto", "zh", "en", "ja", "ko", "yue"}:
            raise ValueError("sherpa language is invalid")
        root, _ = _validate_sherpa_model_directory(
            model_dir,
            family=normalized_family,
        )
        self.family = normalized_family
        self.model_dir = root
        self.num_threads = num_threads
        self.language = normalized_language
        self.name = f"sherpa-onnx/{normalized_family}"
        self._recognizer_factory = recognizer_factory
        self._recognizer: Any | None = None
        self._recognizer_lock = threading.Lock()

    @property
    def is_initialized(self) -> bool:
        with self._recognizer_lock:
            return self._recognizer is not None

    def _factory_and_options_locked(self) -> tuple[Callable[..., Any], dict[str, Any]]:
        _, assets = _validate_sherpa_model_directory(
            self.model_dir,
            family=self.family,
        )
        common: dict[str, Any] = {
            "num_threads": self.num_threads,
            "sample_rate": 16000,
            "decoding_method": "greedy_search",
            "debug": False,
            "provider": "cpu",
        }
        if self.family == "paraformer":
            options = {
                "paraformer": str(assets["paraformer"]),
                "tokens": str(assets["tokens"]),
                "feature_dim": 80,
                **common,
            }
            method_name = "from_paraformer"
        elif self.family == "sense_voice":
            options = {
                "model": str(assets["model"]),
                "tokens": str(assets["tokens"]),
                "feature_dim": 80,
                "language": self.language,
                # VoiceFlypy applies its established normalization exactly
                # once after candidate generation.
                "use_itn": False,
                **common,
            }
            method_name = "from_sense_voice"
        else:
            options = {
                "conv_frontend": str(assets["conv_frontend"]),
                "encoder": str(assets["encoder"]),
                "decoder": str(assets["decoder"]),
                "tokenizer": str(assets["tokenizer"]),
                "feature_dim": 128,
                "max_total_len": 512,
                "max_new_tokens": 128,
                "temperature": 1e-6,
                "top_p": 0.8,
                "seed": 42,
                "hotwords": "",
                **common,
            }
            method_name = "from_qwen3_asr"

        factory = self._recognizer_factory
        if factory is None:
            # Keep sherpa optional for test tools and for the default-off path.
            # Production already bundles this dependency for streaming ASR.
            import sherpa_onnx

            factory = getattr(sherpa_onnx.OfflineRecognizer, method_name)
        return factory, options

    def _recognizer_locked(self) -> Any:
        if self._recognizer is None:
            factory, options = self._factory_and_options_locked()
            recognizer = factory(**options)
            if not callable(getattr(recognizer, "create_stream", None)):
                raise TypeError("sherpa recognizer has no create_stream method")
            if not callable(getattr(recognizer, "decode_stream", None)):
                raise TypeError("sherpa recognizer has no decode_stream method")
            self._recognizer = recognizer
        return self._recognizer

    def refine(
        self,
        request: FinalizationRequest,
        *,
        timeout_seconds: float,
    ) -> BackendCandidate:
        if request.audio is None:
            raise ValueError("audio is unavailable")
        timeout = _finite_positive(timeout_seconds, name="timeout_seconds")
        if not self._recognizer_lock.acquire(timeout=timeout):
            raise TimeoutError("sherpa recognizer is busy")
        try:
            recognizer = self._recognizer_locked()
            stream = recognizer.create_stream()
            accept_waveform = getattr(stream, "accept_waveform", None)
            if not callable(accept_waveform):
                raise TypeError("sherpa stream has no accept_waveform method")
            # OfflineStream accepts one complete utterance exactly once and
            # resamples internally when the capture rate is not 16 kHz.
            accept_waveform(request.audio.sample_rate, request.audio.samples)
            recognizer.decode_stream(stream)
            result = getattr(stream, "result", None)
            text = getattr(result, "text", None)
            if not isinstance(text, str):
                raise ValueError("sherpa result has no text")
            return BackendCandidate(
                text=text.strip(),
                backend=self.name,
                # SenseVoice, Paraformer, and Qwen3-ASR do not expose a
                # calibrated confidence through this installed public API.
                confidence=None,
                kind="dictation",
            )
        finally:
            self._recognizer_lock.release()


def serialize_identifier_sequence(payload: Mapping[str, Any]) -> str:
    """Turn typed identifier data into the only permitted surface form."""
    if not isinstance(payload, Mapping) or set(payload) != {"identifiers"}:
        raise ValueError("identifier payload must contain only identifiers")
    identifiers = payload["identifiers"]
    if not isinstance(identifiers, list) or not 1 <= len(identifiers) <= 16:
        raise ValueError("identifier sequence must contain 1 to 16 items")
    output: list[str] = []
    for item in identifiers:
        if not isinstance(item, Mapping):
            raise ValueError("identifier item must be an object")
        if not set(item).issubset({"prefix", "digits", "suffix"}):
            raise ValueError("identifier item contains unknown fields")
        prefix = item.get("prefix", "")
        digits = item.get("digits", "")
        suffix = item.get("suffix", "")
        if not all(isinstance(value, str) for value in (prefix, digits, suffix)):
            raise ValueError("identifier fields must be strings")
        if not _IDENTIFIER_PREFIX_RE.fullmatch(prefix):
            raise ValueError("identifier prefix is invalid")
        if not _IDENTIFIER_DIGITS_RE.fullmatch(digits):
            raise ValueError("identifier digits are invalid")
        if not _IDENTIFIER_SUFFIX_RE.fullmatch(suffix):
            raise ValueError("identifier suffix is invalid")
        output.append(prefix.upper() + digits + suffix.upper())
    return " ".join(output)


def _strict_identifier_tokens(text: str) -> tuple[str, ...] | None:
    """Parse and canonicalize only an entire identifier utterance."""
    normalized = unicodedata.normalize("NFKC", text).strip()
    if not normalized or any(char in normalized for char in "\r\n"):
        return None
    tokens: list[str] = []
    position = 0
    while position < len(normalized):
        if tokens:
            separator = _IDENTIFIER_SEPARATOR_RE.match(normalized, position)
            if separator is None:
                return None
            position = separator.end()
        prefix = _IDENTIFIER_PREFIX_RE.match(normalized, position)
        if prefix is None:
            return None
        position = prefix.end()
        while position < len(normalized) and normalized[position] in " \t":
            position += 1
        digits = _IDENTIFIER_DIGITS_RE.match(normalized, position)
        if digits is None:
            return None
        position = digits.end()
        suffix = _IDENTIFIER_SURFACE_SUFFIX_RE.match(normalized, position)
        if suffix is None:
            return None
        position = suffix.end()
        tokens.append(
            prefix.group(0).upper()
            + digits.group(0)
            + suffix.group(0).upper()
        )
        if len(tokens) > 16:
            return None
    return tuple(tokens)


def _raw_is_identifier_sequence(text: str) -> bool:
    normalized = unicodedata.normalize("NFKC", text).strip()
    return bool(normalized and _RAW_IDENTIFIER_SEQUENCE_RE.fullmatch(normalized))


def _identifier_change_is_bounded(
    baseline_tokens: tuple[str, ...],
    candidate_tokens: tuple[str, ...],
) -> bool:
    if len(baseline_tokens) != len(candidate_tokens):
        return False
    for baseline, candidate in zip(baseline_tokens, candidate_tokens):
        baseline_folded = baseline.casefold()
        candidate_folded = candidate.casefold()
        longest = max(len(baseline_folded), len(candidate_folded))
        shortest = min(len(baseline_folded), len(candidate_folded))
        if shortest / longest < 0.5:
            return False
        similarity = difflib.SequenceMatcher(
            None,
            baseline_folded,
            candidate_folded,
            autojunk=False,
        ).ratio()
        if similarity < 0.5:
            return False
        if _levenshtein_distance(baseline_folded, candidate_folded) > max(
            2,
            math.ceil(longest * 0.25),
        ):
            return False
    return True


def _levenshtein_distance(left: str, right: str) -> int:
    if len(left) < len(right):
        left, right = right, left
    previous = list(range(len(right) + 1))
    for left_index, left_char in enumerate(left, start=1):
        current = [left_index]
        for right_index, right_char in enumerate(right, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[right_index] + 1,
                    previous[right_index - 1] + (left_char != right_char),
                )
            )
        previous = current
    return previous[-1]


def _text_skeleton(text: str) -> str:
    return "".join(
        char.casefold()
        for char in unicodedata.normalize("NFKC", text)
        if not unicodedata.category(char).startswith(("P", "Z"))
    )


def _protected_text_signature(text: str) -> tuple[tuple[str, str], ...]:
    """Return one ordered stream of normalized technical tokens and symbols."""
    normalized = unicodedata.normalize("NFKC", text)
    signature: list[tuple[str, str]] = []
    position = 0
    while position < len(normalized):
        token = _PROTECTED_TOKEN_RE.match(normalized, position)
        if token is not None:
            signature.append(("token", token.group(0)))
            position = token.end()
            continue
        char = normalized[position]
        if (
            unicodedata.category(char).startswith("S")
            or char in _PROTECTED_TECHNICAL_PUNCTUATION
        ):
            signature.append(("symbol", char))
        position += 1
    return tuple(signature)


def _source_symbol_signature(text: str) -> tuple[str, ...]:
    """Preserve the identity of compatibility symbols such as ℃ and ℉."""
    return tuple(
        unicodedata.normalize("NFKC", char)
        for char in text
        if unicodedata.category(char).startswith("S")
    )


class CandidateValidator:
    """Reject unsafe model output instead of trying to repair it."""

    def __init__(
        self,
        *,
        candidate_transform: Callable[[str], str] | None = None,
        max_characters: int = 512,
        min_similarity: float = 0.45,
        min_dictation_confidence: float = 0.45,
        min_identifier_confidence: float = 0.90,
    ) -> None:
        self.candidate_transform = candidate_transform or (lambda text: text.strip())
        self.max_characters = int(max_characters)
        self.min_similarity = float(min_similarity)
        self.min_dictation_confidence = float(min_dictation_confidence)
        self.min_identifier_confidence = float(min_identifier_confidence)

    @staticmethod
    def _confidence_reason(confidence: Any) -> str:
        if confidence is None:
            return ""
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not math.isfinite(confidence)
            or not 0.0 <= float(confidence) <= 1.0
        ):
            return "candidate_confidence_invalid"
        return ""

    def _validate_identifier_surface(
        self,
        request: FinalizationRequest,
        rendered: str,
    ) -> str:
        baseline_tokens = _strict_identifier_tokens(request.baseline_text)
        candidate_tokens = _strict_identifier_tokens(rendered)
        if baseline_tokens is None:
            return "identifier_baseline_not_structured"
        if not _raw_is_identifier_sequence(request.raw_text):
            return "identifier_raw_evidence_missing"
        if candidate_tokens is None:
            return "identifier_candidate_not_structured"
        if not _identifier_change_is_bounded(baseline_tokens, candidate_tokens):
            return "identifier_change_unbounded"
        return ""

    def validate(
        self,
        request: FinalizationRequest,
        candidate: BackendCandidate,
    ) -> tuple[str | None, str]:
        confidence_reason = self._confidence_reason(candidate.confidence)
        if confidence_reason:
            return None, confidence_reason
        if candidate.kind == "identifier_sequence":
            if candidate.confidence is None:
                return None, "identifier_confidence_missing"
            if candidate.confidence < self.min_identifier_confidence:
                return None, "identifier_confidence_low"
            try:
                rendered = serialize_identifier_sequence(candidate.structured or {})
            except (TypeError, ValueError):
                return None, "identifier_schema_invalid"
            if len(rendered) > self.max_characters:
                return None, "candidate_too_long"
            identifier_rejection = self._validate_identifier_surface(
                request,
                rendered,
            )
            if identifier_rejection:
                return None, identifier_rejection
            return " ".join(_strict_identifier_tokens(rendered) or ()), ""

        if candidate.kind != "dictation":
            return None, "candidate_kind_unsupported"
        if not isinstance(candidate.text, str):
            return None, "candidate_text_invalid"
        if candidate.confidence is not None:
            if candidate.confidence < self.min_dictation_confidence:
                return None, "candidate_confidence_low"
        if not candidate.text.strip():
            return None, "candidate_empty"
        if len(candidate.text) > self.max_characters:
            return None, "candidate_too_long"
        if any(
            unicodedata.category(char) == "Cc" and char not in "\t\r\n"
            for char in candidate.text
        ):
            return None, "candidate_control_character"
        try:
            rendered = self.candidate_transform(candidate.text).strip()
        except Exception:
            return None, "candidate_transform_failed"
        if not rendered or len(rendered) > self.max_characters:
            return None, "candidate_render_invalid"
        if rendered != request.baseline_text and candidate.confidence is None:
            return None, "candidate_confidence_missing"
        if _source_symbol_signature(
            request.baseline_text
        ) != _source_symbol_signature(rendered):
            return None, "protected_symbol_changed"
        baseline_signature = _protected_text_signature(request.baseline_text)
        rendered_signature = _protected_text_signature(rendered)
        baseline_protected = tuple(
            value for kind, value in baseline_signature if kind == "token"
        )
        rendered_protected = tuple(
            value for kind, value in rendered_signature if kind == "token"
        )
        baseline_symbols = tuple(
            value for kind, value in baseline_signature if kind == "symbol"
        )
        rendered_symbols = tuple(
            value for kind, value in rendered_signature if kind == "symbol"
        )
        if baseline_symbols != rendered_symbols:
            return None, "protected_symbol_changed"
        if (
            baseline_signature != rendered_signature
            and baseline_protected == rendered_protected
        ):
            return None, "protected_symbol_changed"

        baseline_skeleton = _text_skeleton(request.baseline_text)
        candidate_skeleton = _text_skeleton(rendered)
        if not baseline_skeleton or not candidate_skeleton:
            return None, "candidate_skeleton_empty"
        length_ratio = len(candidate_skeleton) / len(baseline_skeleton)
        if not 0.5 <= length_ratio <= 1.8:
            return None, "candidate_length_ratio"
        similarity = difflib.SequenceMatcher(
            None,
            baseline_skeleton,
            candidate_skeleton,
            autojunk=False,
        ).ratio()
        if similarity < self.min_similarity:
            return None, "candidate_similarity_low"
        if baseline_protected != rendered_protected:
            # A second acoustic model may correct a misheard identifier, but
            # only when the entire utterance is identifier-shaped and its
            # confidence clears the stricter structured threshold.  Prose
            # containing an identifier remains protected.
            identifier_rejection = self._validate_identifier_surface(
                request,
                rendered,
            )
            if identifier_rejection:
                if identifier_rejection in {
                    "identifier_baseline_not_structured",
                    "identifier_candidate_not_structured",
                }:
                    return None, "protected_token_changed"
                return None, identifier_rejection
            if candidate.confidence is None:
                return None, "protected_token_changed"
            if candidate.confidence < self.min_identifier_confidence:
                return None, "identifier_confidence_low"
            rendered = " ".join(_strict_identifier_tokens(rendered) or ())
        else:
            # Even when its protected token sequence is unchanged, a strict
            # identifier utterance gets one deterministic surface form instead
            # of retaining model-specific casing or separators.
            baseline_identifiers = _strict_identifier_tokens(
                request.baseline_text
            )
            rendered_identifiers = _strict_identifier_tokens(rendered)
            if (
                baseline_identifiers is not None
                and rendered_identifiers is not None
                and _raw_is_identifier_sequence(request.raw_text)
            ):
                rendered = " ".join(rendered_identifiers)
        # Canonicalization is itself a text change.  Recheck after every
        # deterministic rewrite so a scoreless candidate cannot alter the
        # committed result by entering through an initially identical string.
        if rendered != request.baseline_text and candidate.confidence is None:
            return None, "candidate_confidence_missing"
        return rendered, ""


class FinalTextRefiner:
    """Run one local backend under a bounded wait and fail back to baseline."""

    def __init__(
        self,
        *,
        mode: str = "off",
        backend: RefinementBackend | None = None,
        validator: CandidateValidator | None = None,
        timeout_seconds: float = 1.5,
        max_audio_seconds: float = 20.0,
        thread_factory: Callable[..., threading.Thread] = threading.Thread,
    ) -> None:
        normalized_mode = str(mode).strip().casefold()
        if normalized_mode not in REFINER_MODES:
            raise ValueError(f"unsupported refiner mode: {mode}")
        if normalized_mode != "off" and backend is None:
            raise ValueError("enabled refiner requires a backend")
        self.mode = normalized_mode
        self.backend = backend
        self.validator = validator or CandidateValidator()
        self.timeout_seconds = _bounded_positive(
            timeout_seconds,
            name="timeout_seconds",
            maximum=MAX_REFINER_TIMEOUT_SECONDS,
        )
        self.max_audio_seconds = _bounded_positive(
            max_audio_seconds,
            name="max_audio_seconds",
            maximum=MAX_REFINER_AUDIO_SECONDS,
        )
        self.thread_factory = thread_factory
        # If a backend ignores cancellation or its own socket timeout, retain
        # at most one daemon call.  Later sessions fall back immediately
        # instead of creating an unbounded pile of threads and audio buffers.
        self._backend_gate = threading.Lock()

    @property
    def needs_audio(self) -> bool:
        return self.mode != "off"

    @property
    def backend_name(self) -> str:
        return getattr(self.backend, "name", "none")

    def deadline(self) -> float:
        return time.monotonic() + self.timeout_seconds

    def _fallback(
        self,
        request: FinalizationRequest,
        *,
        status: str,
        started: float,
        reason: str = "",
        candidate: BackendCandidate | None = None,
    ) -> RefinementResult:
        candidate_text = (
            candidate.text
            if candidate is not None and isinstance(candidate.text, str)
            else None
        )
        backend = (
            candidate.backend
            if candidate is not None and isinstance(candidate.backend, str)
            else self.backend_name
        )
        confidence = (
            float(candidate.confidence)
            if candidate is not None
            and isinstance(candidate.confidence, (int, float))
            and not isinstance(candidate.confidence, bool)
            and math.isfinite(candidate.confidence)
            else None
        )
        kind = (
            candidate.kind
            if candidate is not None and isinstance(candidate.kind, str)
            else "dictation"
        )
        return RefinementResult(
            status=status,
            text=request.baseline_text,
            candidate_text=candidate_text,
            backend=backend,
            latency_ms=(time.perf_counter() - started) * 1000,
            reason=reason,
            confidence=confidence,
            kind=kind,
        )

    def _invoke_backend(
        self,
        request: FinalizationRequest,
        *,
        wait_seconds: float,
    ) -> tuple[str, BackendCandidate | None, BaseException | None]:
        """Run at most one backend call and bound how long the caller waits."""
        if not self._backend_gate.acquire(blocking=False):
            return "busy", None, None

        done = threading.Event()
        outcome: dict[str, Any] = {}
        release_guard = threading.Lock()
        gate_released = False

        def release_gate_once() -> None:
            nonlocal gate_released
            with release_guard:
                if not gate_released:
                    gate_released = True
                    self._backend_gate.release()

        def worker() -> None:
            try:
                outcome["candidate"] = self.backend.refine(  # type: ignore[union-attr]
                    request,
                    timeout_seconds=wait_seconds,
                )
            except BaseException as exc:
                outcome["error"] = exc
            finally:
                release_gate_once()
                done.set()

        try:
            thread = self.thread_factory(target=worker, daemon=True)
            thread.start()
        except BaseException as exc:
            release_gate_once()
            return "error", None, exc

        if not done.wait(wait_seconds):
            return "timeout", None, None
        error = outcome.get("error")
        if isinstance(error, BaseException):
            return "error", None, error
        candidate = outcome.get("candidate")
        if not isinstance(candidate, BackendCandidate):
            return "malformed", None, None
        return "complete", candidate, None

    def refine(self, request: FinalizationRequest) -> RefinementResult:
        started = time.perf_counter()
        if self.mode == "off":
            return self._fallback(request, status="disabled", started=started)
        if request.audio is None:
            return self._fallback(
                request,
                status="unavailable",
                started=started,
                reason=request.audio_unavailable_reason or "audio_unavailable",
            )
        if request.audio.duration_seconds > self.max_audio_seconds:
            return self._fallback(
                request,
                status="unavailable",
                started=started,
                reason="audio_too_long",
            )
        deadline = request.deadline_monotonic
        if (
            isinstance(deadline, bool)
            or not isinstance(deadline, (int, float))
            or not math.isfinite(deadline)
        ):
            return self._fallback(
                request,
                status="error",
                started=started,
                reason="deadline_invalid",
            )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return self._fallback(request, status="timeout", started=started)
        invocation, candidate, error = self._invoke_backend(
            request,
            wait_seconds=min(remaining, self.timeout_seconds),
        )
        if invocation == "busy":
            return self._fallback(
                request,
                status="unavailable",
                started=started,
                reason="backend_busy",
            )
        if invocation == "timeout" or time.monotonic() > deadline:
            return self._fallback(request, status="timeout", started=started)
        if invocation == "malformed":
            return self._fallback(
                request,
                status="error",
                started=started,
                reason="backend_candidate_invalid",
            )
        if error is not None:
            if isinstance(error, (TimeoutError, socket.timeout)):
                return self._fallback(request, status="timeout", started=started)
            if isinstance(error, urllib.error.URLError):
                reason = (
                    "backend_timeout"
                    if isinstance(error.reason, TimeoutError)
                    else "backend_unavailable"
                )
                status = "timeout" if reason == "backend_timeout" else "unavailable"
                return self._fallback(
                    request,
                    status=status,
                    started=started,
                    reason=reason,
                )
            return self._fallback(
                request,
                status="error",
                started=started,
                reason=type(error).__name__,
            )
        if candidate is None:
            return self._fallback(
                request,
                status="error",
                started=started,
                reason="backend_result_missing",
            )
        try:
            rendered, rejection = self.validator.validate(request, candidate)
        except BaseException as exc:
            return self._fallback(
                request,
                status="error",
                started=started,
                reason=f"validator_{type(exc).__name__}",
                candidate=candidate,
            )
        if time.monotonic() > deadline:
            return self._fallback(
                request,
                status="timeout",
                started=started,
                reason="validation_late",
                candidate=candidate,
            )
        if rendered is None:
            return self._fallback(
                request,
                status="shadow_invalid" if self.mode == "shadow" else "invalid",
                started=started,
                reason=rejection,
                candidate=candidate,
            )
        if self.mode == "shadow":
            return RefinementResult(
                status="shadow_valid",
                text=request.baseline_text,
                candidate_text=rendered,
                backend=candidate.backend,
                latency_ms=(time.perf_counter() - started) * 1000,
                confidence=candidate.confidence,
                kind=candidate.kind,
            )
        return RefinementResult(
            status="accepted",
            text=rendered,
            candidate_text=rendered,
            backend=candidate.backend,
            latency_ms=(time.perf_counter() - started) * 1000,
            confidence=candidate.confidence,
            kind=candidate.kind,
            committed_text_changed=rendered != request.baseline_text,
        )


class ShadowRefinementRunner:
    """Run at most one non-blocking shadow inference at a time."""

    def __init__(
        self,
        refiner: FinalTextRefiner,
        *,
        on_result: Callable[[FinalizationRequest, RefinementResult], None] | None = None,
        thread_factory: Callable[..., threading.Thread] = threading.Thread,
    ) -> None:
        self.refiner = refiner
        self.on_result = on_result
        self.thread_factory = thread_factory
        self._lock = threading.Lock()
        self._in_progress = False

    @property
    def in_progress(self) -> bool:
        with self._lock:
            return self._in_progress

    def submit(self, request: FinalizationRequest) -> bool:
        if self.refiner.mode != "shadow":
            return False
        with self._lock:
            if self._in_progress:
                return False
            self._in_progress = True

        def worker() -> None:
            try:
                result = self.refiner.refine(request)
                if self.on_result is not None:
                    self.on_result(request, result)
            finally:
                with self._lock:
                    self._in_progress = False

        try:
            thread = self.thread_factory(target=worker, daemon=True)
            thread.start()
        except Exception:
            with self._lock:
                self._in_progress = False
            return False
        return True


def build_refiner_from_environment(
    *,
    candidate_transform: Callable[[str], str] | None = None,
) -> FinalTextRefiner:
    mode = os.environ.get("VOICEFLYPY_REFINER_MODE", "off").strip().casefold()
    timeout_default = 6.0 if mode == "shadow" else 1.5
    timeout_seconds = _environment_float(
        "VOICEFLYPY_REFINER_TIMEOUT_SECONDS",
        timeout_default,
    )
    max_audio_seconds = _environment_float(
        "VOICEFLYPY_REFINER_MAX_AUDIO_SECONDS",
        20.0,
    )
    validator = CandidateValidator(candidate_transform=candidate_transform)
    if mode == "off":
        return FinalTextRefiner(
            mode="off",
            validator=validator,
            timeout_seconds=timeout_seconds,
            max_audio_seconds=max_audio_seconds,
        )
    endpoint = os.environ.get(
        "VOICEFLYPY_WHISPER_ENDPOINT",
        "http://127.0.0.1:18080/inference",
    ).strip()
    language = os.environ.get("VOICEFLYPY_WHISPER_LANGUAGE", "auto").strip()
    prompt = os.environ.get("VOICEFLYPY_WHISPER_PROMPT", "").strip()
    return FinalTextRefiner(
        mode=mode,
        backend=WhisperServerBackend(
            endpoint=endpoint,
            language=language,
            prompt=prompt,
        ),
        validator=validator,
        timeout_seconds=timeout_seconds,
        max_audio_seconds=max_audio_seconds,
    )
