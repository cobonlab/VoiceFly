"""VoiceFlypy local streaming ASR service for the Squirrel input method.

The service owns microphone permission and the sherpa-onnx model.  Squirrel
controls it with NSDistributedNotificationCenter and receives partial/final
text through the same local-only macOS mechanism.
"""

import hashlib
import os
import queue
import re
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import AppKit
import Foundation
import objc
import sherpa_onnx
import sounddevice as sd
# Pynini's main extension imports this helper by its top-level module name.
# Keep the explicit import so PyInstaller includes the binary in the app bundle.
import _pywrapfst  # noqa: F401
from itn.chinese.inverse_normalizer import InverseNormalizer

SOURCE_ROOT = Path(__file__).resolve().parent
if getattr(sys, "frozen", False):
    RESOURCE_ROOT = Path(str(Foundation.NSBundle.mainBundle().resourcePath()))
    MODEL_DIR = RESOURCE_ROOT / "models" / "streaming-zipformer"
else:
    RESOURCE_ROOT = SOURCE_ROOT
    MODEL_DIR = SOURCE_ROOT / "sherpa-onnx-x-asr-480ms-streaming-zipformer-transducer-zh-en-punct-int8-2026-06-05"
os.environ["VOICEFLYPY_RESOURCE_DIR"] = str(RESOURCE_ROOT)

from voice_hotwords import (  # noqa: E402 - resource root must be known first
    create_recognizer_with_fallback,
    resolve_decoder_config,
    validate_hotwords_text,
)
from voice_audio import (  # noqa: E402
    AdaptiveSpeechStartGate,
    open_first_available_input,
)
from voice_numeric_normalizer import (  # noqa: E402
    POLICY_VERSION as NUMERIC_POLICY_VERSION,
    normalize_mapped_numbers,
    normalize_mapped_numbers_partial,
)
from voice_personal_phonetics import (  # noqa: E402
    apply_personal_phonetic_corrections,
    build_personal_phrase_matcher,
)
from voice_refiner import (  # noqa: E402
    BoundedAudioBuffer,
    FinalTextRefiner,
    FinalizationRequest,
    ShadowRefinementRunner,
    build_refiner_from_environment,
)
from voice_tool import (  # noqa: E402
    apply_engineering_symbols,
    load_spoken_number_exceptions,
    normalize_dictation_punctuation,
    normalize_spelled_acronyms,
)
from voice_streaming_protocol import (  # noqa: E402
    PartialStabilityTracker,
    RevisionAttributionTracker,
)
SAMPLE_RATE = 16000


def _log_optional_refiner_failure(stage, exc):
    """Best-effort diagnostics for a path that must never affect dictation."""
    try:
        print(
            "[语义终判] "
            f"status=isolated_failure stage={stage} "
            f"error={type(exc).__name__}",
            flush=True,
        )
    except Exception:
        # stdout can be closed during process shutdown and tests may replace
        # the logger.  Observability is optional; the baseline FINAL is not.
        pass


_itn_cache_override = os.environ.get("VOICEFLYPY_ITN_CACHE_DIR", "").strip()
ITN_CACHE_DIR = (
    Path(_itn_cache_override).expanduser()
    if _itn_cache_override
    else Path.home() / "Library/Caches/VoiceFlypy/wetextprocessing"
)
ITN_POLICY_VERSION = NUMERIC_POLICY_VERSION
_itn_lock = threading.Lock()
_normalization_lock = threading.RLock()
_itn = None
_memory_only_itn = None
_STREAMING_PENDING_MARKER = "\ue10f"


@dataclass(frozen=True)
class StreamingNormalization:
    text: str
    stable_limit_codepoints: int | None = None


def _protect_literal_dong(text):
    """Keep ordinary ``洞`` out of WeText's telephone zero aliases."""
    marker = "\ue000VOICEFLYPY_LITERAL_DONG\ue001"
    return text.replace("洞", marker), marker


START_NAME = "com.simonli.voiceflypy.start"
STOP_NAME = "com.simonli.voiceflypy.stop"
STARTED_NAME = "com.simonli.voiceflypy.started"
MIC_READY_NAME = "com.simonli.voiceflypy.mic-ready"
PARTIAL_NAME = "com.simonli.voiceflypy.partial"
FINAL_NAME = "com.simonli.voiceflypy.final"
ERROR_NAME = "com.simonli.voiceflypy.error"
LEXICON_CHANGED_NAME = "com.simonli.voiceflypy.lexicon-changed"
HOTWORD_REFRESH_DEBOUNCE_SECONDS = 0.5
MAX_INLINE_HOTWORD_ENTRIES = 4096
MAX_INLINE_HOTWORD_BYTES = 256 * 1024
MICROPHONE_OPEN_TIMEOUT_SECONDS = 6.0
AUDIO_CAPTURE_QUEUE_SECONDS = 20.0
# Keep collecting briefly after Control is released so CoreAudio/Bluetooth can
# drain a final phoneme already in flight. This is deliberately not tied to
# the model's 480 ms encoder chunk: input_finished() flushes decoder context,
# while extending wall-clock capture to a full chunk only delays commit. The
# 40 ms application read quantum keeps the actual drain close to this budget.
AUDIO_RELEASE_TAIL_SECONDS = 0.16
# The user releases Control to finalize immediately, so phrase-level endpoint
# resets do not need to react to ordinary thinking pauses.  Short thresholds
# can freeze a one-glyph hypothesis and let the resumed decoder repeat that
# glyph at the next segment boundary.
ENDPOINT_MIN_TRAILING_SILENCE_SECONDS = 3.0
PREFER_BUILTIN_MIC = os.environ.get(
    "VOICEFLYPY_PREFER_BUILTIN_MIC",
    "0",
).strip().casefold() not in {"0", "false", "no", "off"}
MICROPHONE_INPUT_POLICY = (
    "builtin-override" if PREFER_BUILTIN_MIC else "system-default"
)


@dataclass(frozen=True)
class InlineHotwordSnapshot:
    text: str
    sha256: str
    entries: int
    byte_count: int


EMPTY_HOTWORD_SNAPSHOT = InlineHotwordSnapshot("", hashlib.sha256(b"").hexdigest(), 0, 0)


class MicrophoneOpenAttempt:
    def __init__(self):
        self.settled = threading.Event()
        self.timed_out = False


def load_inline_hotword_snapshot(decoder):
    """Read, validate and fingerprint one immutable context-graph snapshot."""
    if not decoder.enabled or decoder.hotwords_file is None:
        return EMPTY_HOTWORD_SNAPSHOT
    with decoder.hotwords_file.open("rb") as handle:
        data = handle.read(MAX_INLINE_HOTWORD_BYTES + 1)
    if len(data) > MAX_INLINE_HOTWORD_BYTES:
        raise OSError(
            f"inline hotwords exceed byte limit: {len(data)} > {MAX_INLINE_HOTWORD_BYTES}"
        )
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OSError(f"inline hotwords are not valid UTF-8: {exc}") from exc
    if "/" in text:
        raise OSError("inline hotwords contain reserved '/' separator")
    valid, detail = validate_hotwords_text(text)
    if not valid:
        raise OSError(f"inline hotwords failed validation: {detail}")
    entries = sum(
        1
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
    if entries > MAX_INLINE_HOTWORD_ENTRIES:
        raise OSError(
            f"inline hotwords exceed entry limit: {entries} > {MAX_INLINE_HOTWORD_ENTRIES}"
        )
    return InlineHotwordSnapshot(
        text=text,
        sha256=hashlib.sha256(data).hexdigest(),
        entries=entries,
        byte_count=len(data),
    )


def normalize(
    text,
    *,
    apply_personal_phonetics=True,
    diagnostic_logger=None,
    memory_only_itn=False,
):
    """Apply conservative mapped ITN, engineering symbols, and punctuation.

    ``memory_only_itn=True`` keeps evaluation read-only by constructing a
    separate WeText normalizer with its documented ``cache_dir=False`` mode.
    Default production callers retain the persistent ``ITN_CACHE_DIR`` cache.
    """
    if diagnostic_logger is not None and not callable(diagnostic_logger):
        raise TypeError("diagnostic_logger must be callable or None")
    if type(memory_only_itn) is not bool:
        raise TypeError("memory_only_itn must be bool")
    stripped = text.strip()
    if not stripped:
        # Constructing the inverse normalizer is intentionally lazy and can be
        # expensive on the first recording.  An empty partial/final must never
        # pay that cold-start cost, especially after a very short key hold.
        return ""
    # A shadow candidate can finish while the next recording is already
    # emitting partials.  Serialize access to the third-party FST and the
    # personal matcher caches, which previously ran only on one service thread.
    with _normalization_lock:
        options = {"apply_personal_phonetics": apply_personal_phonetics}
        # Keep the historical private-call shape for default callers and tests.
        # Only the evaluation adapter needs the new diagnostic seam.
        if diagnostic_logger is not None:
            options["diagnostic_logger"] = diagnostic_logger
        if memory_only_itn:
            options["memory_only_itn"] = True
        return _normalize_nonempty(stripped, **options)


def normalize_partial(
    text,
    *,
    apply_personal_phonetics=True,
    diagnostic_logger=None,
    memory_only_itn=False,
):
    """Normalize one open streaming hypothesis and expose its stable limit."""
    if diagnostic_logger is not None and not callable(diagnostic_logger):
        raise TypeError("diagnostic_logger must be callable or None")
    if type(memory_only_itn) is not bool:
        raise TypeError("memory_only_itn must be bool")
    stripped = text.strip()
    if not stripped:
        return StreamingNormalization("")
    with _normalization_lock:
        return _normalize_partial_nonempty(
            stripped,
            apply_personal_phonetics=apply_personal_phonetics,
            diagnostic_logger=diagnostic_logger,
            memory_only_itn=memory_only_itn,
        )


def _normalize_nonempty(
    stripped,
    *,
    apply_personal_phonetics,
    diagnostic_logger=None,
    memory_only_itn=False,
):
    return _normalize_pipeline(
        stripped,
        apply_personal_phonetics=apply_personal_phonetics,
        diagnostic_logger=diagnostic_logger,
        memory_only_itn=memory_only_itn,
        is_final=True,
    ).text


def _normalize_partial_nonempty(
    stripped,
    *,
    apply_personal_phonetics,
    diagnostic_logger=None,
    memory_only_itn=False,
):
    return _normalize_pipeline(
        stripped,
        apply_personal_phonetics=apply_personal_phonetics,
        diagnostic_logger=diagnostic_logger,
        memory_only_itn=memory_only_itn,
        is_final=False,
    )


def _normalize_pipeline(
    stripped,
    *,
    apply_personal_phonetics,
    diagnostic_logger,
    memory_only_itn,
    is_final,
):
    global _itn, _memory_only_itn
    if memory_only_itn:
        if _memory_only_itn is None:
            with _itn_lock:
                if _memory_only_itn is None:
                    _memory_only_itn = InverseNormalizer(
                        cache_dir=False,
                        enable_0_to_9=False,
                    )
        active_itn = _memory_only_itn
    else:
        if _itn is None:
            with _itn_lock:
                if _itn is None:
                    _itn = InverseNormalizer(
                        cache_dir=str(ITN_CACHE_DIR),
                        enable_0_to_9=False,
                    )
        active_itn = _itn
    protected, marker = _protect_literal_dong(stripped)
    pending_from = None
    try:
        mapped = active_itn.normalize_with_mapping(protected)
        exceptions = load_spoken_number_exceptions()
        if is_final:
            normalized = normalize_mapped_numbers(
                protected,
                mapped,
                exceptions,
            )
        else:
            partial = normalize_mapped_numbers_partial(
                protected,
                mapped,
                exceptions,
            )
            normalized = partial.text
            pending_from = partial.pending_from
        if normalized.count(marker) != protected.count(marker):
            raise ValueError("literal-dong marker mismatch")
    except Exception as exc:
        # Dictation must remain usable if the third-party FST or its alignment
        # fails.  Never put recognized text in logs; preserve the raw wording.
        message = (
            f"[ITN] 保守回退 chars={len(protected)} "
            f"error={type(exc).__name__}"
        )
        if diagnostic_logger is None:
            print(message, flush=True)
        else:
            try:
                diagnostic_logger(message)
            except Exception:
                # Diagnostics must never turn a conservative text fallback
                # into a failed dictation/evaluation.
                pass
        normalized = protected
        if not is_final:
            pending_from = 0
    if pending_from is not None:
        if _STREAMING_PENDING_MARKER in normalized:
            pending_from = 0
        else:
            normalized = (
                normalized[:pending_from]
                + _STREAMING_PENDING_MARKER
                + normalized[pending_from:]
            )
    normalized = normalized.replace(marker, "洞")
    normalized = apply_engineering_symbols(normalized)
    # Keep punctuation as a hard boundary while compacting literal spelled
    # capitals from the bilingual ASR (for example, H U D -> HUD).
    normalized = normalize_spelled_acronyms(normalized)
    normalized = normalize_dictation_punctuation(normalized)
    if apply_personal_phonetics:
        normalized = apply_personal_phonetic_corrections(normalized)
    normalized = collapse_leading_han_restart(normalized)
    if pending_from is None:
        return StreamingNormalization(normalized)
    if normalized.count(_STREAMING_PENDING_MARKER) != 1:
        return StreamingNormalization(
            normalized.replace(_STREAMING_PENDING_MARKER, ""),
            0,
        )
    stable_limit = normalized.index(_STREAMING_PENDING_MARKER)
    return StreamingNormalization(
        normalized.replace(_STREAMING_PENDING_MARKER, ""),
        stable_limit,
    )


def finalize_spacing(text):
    """Return one trailing separator for a non-empty committed utterance."""
    text = text.rstrip()
    return text + " " if text else ""


def _is_han_character(char):
    codepoint = ord(char)
    return (
        codepoint == 0x3007
        or 0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x20000 <= codepoint <= 0x323AF
    )


def collapse_leading_han_restart(text):
    """Remove the narrow ASR artifact ``字 字...`` at utterance start.

    A very early punctuation/endpoint hypothesis can be emitted as one Han
    glyph, followed by the resumed complete phrase beginning with that same
    glyph.  Restrict repair to the absolute beginning, exactly one separated
    Han glyph, and a multi-glyph Han continuation.  Contiguous repetitions
    such as ``好好学习`` and non-leading repetitions remain untouched.
    """

    first, separator, remainder = text.partition(" ")
    if (
        separator
        and len(first) == 1
        and _is_han_character(first)
        and len(remainder) >= 2
        and remainder[0] == first
        and _is_han_character(remainder[1])
    ):
        return remainder
    return text


def join_recognition_segments(segments):
    """Join acoustic endpoint segments without inventing Han word spaces.

    Sherpa endpoints are decoder/lifecycle boundaries, not punctuation.  A
    seam between two Han characters is therefore removed; every other seam
    keeps the historical single separator so English words, mixed scripts,
    units, and symbols are not silently glued together.
    """

    cleaned = []
    for segment in segments:
        value = str(segment or "").strip()
        if value:
            cleaned.append(value)
    if not cleaned:
        return ""

    joined = cleaned[0]
    for segment in cleaned[1:]:
        left = joined[-1]
        right = segment[0]
        separator = (
            "" if _is_han_character(left) and _is_han_character(right) else " "
        )
        joined += separator + segment
    return joined


_ITN_SELF_TEST_CASES = (
    ("我，我觉得这个思路正确", "我觉得这个思路正确"),
    ("我们中间加了一层下面这一段", "我们中间加了1层下面这一段"),
    ("贵一点也无所谓", "贵一点也无所谓"),
    ("这个是正儿八经吃肚子里面有效果的", "这个是正儿八经吃肚子里面有效果的"),
    ("这一点十分重要", "这一点十分重要"),
    ("一二三木头人", "一二三木头人"),
    ("七零八落 十拿九稳", "七零八落 十拿九稳"),
    ("二零二六年九月五日", "2026/09/05"),
    ("会议三点二十分开始", "会议3:20开始"),
    ("百分之三十", "30%"),
    ("版本二点三点一", "版本2.3.1"),
    ("编号二零二六零九零五", "编号20260905"),
    ("W 二十七 T 三十五", "W27 T35"),
    ("W 三五六 T 三九", "W356 T39"),
    ("W 356 T 三九", "W356 T39"),
    ("H U D", "HUD"),
    ("有三 个问题", "有3个问题"),
    ("测试三 次", "测试3次"),
    ("V 二零以及 T 三十", "V20以及 T30"),
    ("T 幺零机柜", "T10机柜"),
    ("V 幺零加上了 T300", "V10加上了 T300"),
    ("GB 300机柜", "GB300机柜"),
    ("GB 三百机柜", "GB300机柜"),
    (
        "现在有三个问题测试了三次我在这里住了三年我们有27个候选指标 包括 V10 V 二零以及 T30",
        "现在有3个问题测试了3次我在这里住了3年我们有27个候选指标 包括 V10 V20以及 T30",
    ),
)


def run_itn_self_test():
    """Exercise the packaged ITN policy and prewarm its exact FST profile."""
    failures = []
    for index, (source, expected) in enumerate(_ITN_SELF_TEST_CASES, start=1):
        if normalize(source, apply_personal_phonetics=False) != expected:
            failures.append(index)
    passed = len(_ITN_SELF_TEST_CASES) - len(failures)
    print(
        f"[ITN自检] policy={ITN_POLICY_VERSION} "
        f"passed={passed}/{len(_ITN_SELF_TEST_CASES)}",
        flush=True,
    )
    if failures:
        print(
            "[ITN自检] failed_cases=" + ",".join(map(str, failures)),
            flush=True,
        )
        return 1
    return 0


_PERSONAL_PHONETIC_SELF_TEST_CASES = (
    ("我是一个紧胸型的选手", "我是一个紧凶型的选手"),
    ("井胸这两个字", "紧凶这两个字"),
    ("锦胸 颈胸", "紧凶 紧凶"),
    ("我是一个锦绣型的牌手", "我是一个紧凶型的牌手"),
    ("锦绣河山", "锦绣河山"),
    ("我要去进修课程", "我要去进修课程"),
    ("前后鼻音识别", "前后鼻音识别"),
)


def run_personal_phonetic_self_test():
    """Verify Foundation romanization, boundaries and regional tolerance."""

    matcher = build_personal_phrase_matcher(("紧凶\tjbxs\t200",))
    failures = []
    for index, (source, expected) in enumerate(
        _PERSONAL_PHONETIC_SELF_TEST_CASES,
        start=1,
    ):
        actual = apply_personal_phonetic_corrections(source, matcher=matcher)
        if actual != expected:
            failures.append(index)
    passed = len(_PERSONAL_PHONETIC_SELF_TEST_CASES) - len(failures)
    print(
        f"[个性发音自检] passed={passed}/{len(_PERSONAL_PHONETIC_SELF_TEST_CASES)}",
        flush=True,
    )
    if failures:
        print(
            "[个性发音自检] failed_cases=" + ",".join(map(str, failures)),
            flush=True,
        )
        return 1
    return 0


class VoiceService(Foundation.NSObject):
    def init(self):
        self = objc.super(VoiceService, self).init()
        if self is None:
            return None
        self.center = Foundation.NSDistributedNotificationCenter.defaultCenter()
        self.lock = threading.RLock()
        self.phase = "idle"
        self.session_id = ""
        self.stream = None
        self.stop_event = threading.Event()
        self.stop_requested_at = None
        self.hotword_refresh_in_progress = False
        self.hotword_refresh_requested = False
        self.hotword_refresh_epoch = 0
        self.hotword_refresh_timer = None
        try:
            self.final_refiner = build_refiner_from_environment(
                candidate_transform=normalize,
            )
        except Exception as exc:
            # Configuration or optional-model failures must never prevent the
            # established recognizer from starting.  Text is deliberately not
            # involved in this startup diagnostic.
            self.final_refiner = FinalTextRefiner(mode="off")
            _log_optional_refiner_failure("configuration", exc)
        try:
            self.shadow_refinement_runner = ShadowRefinementRunner(
                self.final_refiner,
                on_result=self.safely_log_refinement_result,
            )
        except Exception as exc:
            self.shadow_refinement_runner = None
            _log_optional_refiner_failure("runner_construction", exc)
        try:
            print(
                "[语义终判] "
                f"mode={self.final_refiner.mode} "
                f"backend={self.final_refiner.backend_name} "
                f"timeout={self.final_refiner.timeout_seconds:.1f}s "
                f"audio_limit={self.final_refiner.max_audio_seconds:.1f}s",
                flush=True,
            )
        except Exception as exc:
            _log_optional_refiner_failure("startup_log", exc)
        required_files = (
            "tokens.txt",
            "encoder.int8.onnx",
            "decoder.onnx",
            "joiner.int8.onnx",
        )
        missing = [name for name in required_files if not (MODEL_DIR / name).is_file()]
        if missing:
            raise FileNotFoundError(
                f"VoiceFlypy model is incomplete at {MODEL_DIR}: {', '.join(missing)}"
            )
        print(f"[服务] 加载流式中英双语语音模型：{MODEL_DIR}", flush=True)
        self.base_recognizer_options = dict(
            tokens=str(MODEL_DIR / "tokens.txt"),
            encoder=str(MODEL_DIR / "encoder.int8.onnx"),
            decoder=str(MODEL_DIR / "decoder.onnx"),
            joiner=str(MODEL_DIR / "joiner.int8.onnx"),
            num_threads=4,
            sample_rate=SAMPLE_RATE,
            feature_dim=80,
            enable_endpoint_detection=True,
            # Control release finalizes immediately.  Only a long pause should
            # reset the decoder; aggressive one-second resets can split after
            # the first glyph and repeat it when speech resumes.
            rule1_min_trailing_silence=ENDPOINT_MIN_TRAILING_SILENCE_SECONDS,
            rule2_min_trailing_silence=ENDPOINT_MIN_TRAILING_SILENCE_SECONDS,
            rule3_min_utterance_length=20.0,
        )
        requested_decoder = resolve_decoder_config(
            MODEL_DIR,
            RESOURCE_ROOT,
            SOURCE_ROOT,
        )
        if requested_decoder.resource_error:
            print(
                "[服务] 个性词库更新无效，保留上一版热词："
                f"{requested_decoder.resource_error}",
                flush=True,
            )
        if requested_decoder.enabled:
            print(
                "[服务] 启用热词解码：modified_beam_search，"
                f"paths={requested_decoder.max_active_paths}，"
                f"hotwords={requested_decoder.hotwords_file}",
                flush=True,
            )
        else:
            print(
                f"[服务] 热词未启用，使用 greedy_search：{requested_decoder.reason}",
                flush=True,
            )
        self.initialize_recognizer(requested_decoder)
        print(
            "[服务] 麦克风选择策略："
            + (
                "强制优先内置麦克风"
                if PREFER_BUILTIN_MIC
                else "跟随 macOS 系统默认输入"
            )
            + f" ({MICROPHONE_INPUT_POLICY})",
            flush=True,
        )
        self.center.addObserver_selector_name_object_(
            self, "handleStart:", START_NAME, None
        )
        self.center.addObserver_selector_name_object_(
            self, "handleStop:", STOP_NAME, None
        )
        self.center.addObserver_selector_name_object_(
            self, "handleLexiconChanged:", LEXICON_CHANGED_NAME, None
        )
        print("[服务] 就绪，等待输入法发起语音输入。", flush=True)
        return self

    @objc.python_method
    def initialize_recognizer(self, requested_decoder):
        """Build one model and bind changing hotwords to individual streams."""
        self.recognizer, self.decoder_config = create_recognizer_with_fallback(
            sherpa_onnx.OnlineRecognizer.from_transducer,
            self.base_recognizer_options,
            requested_decoder,
            logger=lambda message: print(message, flush=True),
            include_hotwords_file=False,
        )
        self.recognizer_capability_config = self.decoder_config
        self.inline_hotword_supported = self.recognizer_capability_config.enabled
        try:
            self.hotword_snapshot = (
                load_inline_hotword_snapshot(self.decoder_config)
                if self.inline_hotword_supported
                else EMPTY_HOTWORD_SNAPSHOT
            )
        except (OSError, UnicodeError) as exc:
            self.hotword_snapshot = EMPTY_HOTWORD_SNAPSHOT
            print(
                "[服务] 启动时热词快照不可用，本次先使用无热词上下文："
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )

    def dealloc(self):
        if self.hotword_refresh_timer is not None:
            self.hotword_refresh_timer.cancel()
            self.hotword_refresh_timer = None
        self.center.removeObserver_(self)
        objc.super(VoiceService, self).dealloc()

    @objc.python_method
    def post(self, name, session_id, text="", message="", **metadata):
        info = {"session": session_id}
        if text:
            info["text"] = text
        if message:
            info["message"] = message
        info.update(metadata)
        self.center.postNotificationName_object_userInfo_deliverImmediately_(
            name, None, info, True
        )

    @objc.python_method
    def log_refinement_result(self, request, result):
        """Log only metrics; never persist dictated or candidate text."""
        candidate_text = result.candidate_text or ""
        baseline_utf16 = len(request.baseline_text.encode("utf-16-le")) // 2
        candidate_utf16 = len(candidate_text.encode("utf-16-le")) // 2
        confidence = (
            f"{result.confidence:.3f}"
            if isinstance(result.confidence, (int, float))
            else "none"
        )
        print(
            "[语义终判] "
            f"mode={self.final_refiner.mode} status={result.status} "
            f"backend={result.backend} latency_ms={result.latency_ms:.1f} "
            f"audio_ms={int((request.audio.duration_seconds if request.audio else 0) * 1000)} "
            f"baseline_utf16={baseline_utf16} candidate_utf16={candidate_utf16} "
            f"same={candidate_text == request.baseline_text if candidate_text else False} "
            f"confidence={confidence} reason={result.reason or 'none'}",
            flush=True,
        )

    @objc.python_method
    def safely_log_refinement_result(self, request, result):
        """Keep optional telemetry failures out of the recording lifecycle."""
        try:
            self.log_refinement_result(request, result)
            return True
        except Exception as exc:
            _log_optional_refiner_failure("result_log", exc)
            return False

    @objc.python_method
    def refine_final_text(
        self,
        session_id,
        raw_segments,
        raw_text,
        baseline_text,
        audio_buffer,
    ):
        """Run or schedule the optional final model without risking dictation."""
        stage = "configuration"
        try:
            refiner = getattr(self, "final_refiner", None)
            if (
                refiner is None
                or refiner.mode == "off"
                or not raw_text
                or not baseline_text
            ):
                return baseline_text

            stage = "audio_snapshot"
            audio = audio_buffer.snapshot() if audio_buffer is not None else None
            unavailable_reason = (
                audio_buffer.unavailable_reason
                if audio_buffer is not None
                else "audio_not_collected"
            )
            stage = "request_construction"
            request = FinalizationRequest(
                session_id=session_id,
                raw_segments=tuple(raw_segments),
                raw_text=raw_text,
                baseline_text=baseline_text,
                deadline_monotonic=refiner.deadline(),
                audio=audio,
                audio_unavailable_reason=unavailable_reason,
            )
            if audio_buffer is not None:
                stage = "audio_clear"
                audio_buffer.clear()

            if refiner.mode == "shadow":
                stage = "shadow_submit"
                runner = getattr(self, "shadow_refinement_runner", None)
                if runner is None or not runner.submit(request):
                    try:
                        print(
                            "[语义终判] "
                            "mode=shadow status=skipped reason=worker_busy",
                            flush=True,
                        )
                    except Exception as exc:
                        _log_optional_refiner_failure("shadow_skip_log", exc)
                return baseline_text

            stage = "active_refine"
            result = refiner.refine(request)
            stage = "result_log"
            if not self.safely_log_refinement_result(request, result):
                return baseline_text
            stage = "result_text"
            if not isinstance(result.text, str):
                raise TypeError("refiner result text must be str")
            return result.text
        except Exception as exc:
            _log_optional_refiner_failure(stage, exc)
            if audio_buffer is not None:
                try:
                    audio_buffer.clear()
                except Exception as clear_exc:
                    _log_optional_refiner_failure("audio_clear_after_failure", clear_exc)
            return baseline_text

    @objc.python_method
    def terminate_for_audio_recovery(self):
        os._exit(75)

    @objc.python_method
    def handle_microphone_open_timeout(self, session_id, attempt):
        if attempt.settled.is_set():
            return False
        with self.lock:
            if (
                attempt.settled.is_set()
                or session_id != self.session_id
                or self.phase == "idle"
            ):
                return False
            attempt.timed_out = True
            attempt.settled.set()
        message = "microphone open timed out; restarting voice service"
        print(f"[录音] {message} session={session_id}", flush=True)
        self.post(ERROR_NAME, session_id, message=message)
        self.terminate_for_audio_recovery()
        return True

    @objc.python_method
    def refresh_inline_hotwords(self):
        """Refresh the next stream's hotword text without rebuilding the model."""
        with self.lock:
            refresh_epoch = self.hotword_refresh_epoch

        requested_decoder = resolve_decoder_config(
            MODEL_DIR,
            RESOURCE_ROOT,
            SOURCE_ROOT,
        )
        if requested_decoder.resource_error:
            print(
                "[服务] 个性词库合并失败，保留上一版会话热词："
                f"{requested_decoder.resource_error}",
                flush=True,
            )
            return False
        if requested_decoder.enabled and not self.inline_hotword_supported:
            print(
                "[服务] 当前进程以 greedy_search 启动；新热词将在服务重启后启用。",
                flush=True,
            )
            return False
        if self.inline_hotword_supported and not requested_decoder.enabled:
            print(
                "[服务] 新热词资源未通过启用门槛，保留上一版会话热词。",
                flush=True,
            )
            return False

        if requested_decoder.enabled:
            active_signature = (
                self.recognizer_capability_config.decoding_method,
                self.recognizer_capability_config.max_active_paths,
                self.recognizer_capability_config.modeling_unit,
                str(self.recognizer_capability_config.bpe_vocab),
                self.recognizer_capability_config.hotwords_score,
            )
            requested_signature = (
                requested_decoder.decoding_method,
                requested_decoder.max_active_paths,
                requested_decoder.modeling_unit,
                str(requested_decoder.bpe_vocab),
                requested_decoder.hotwords_score,
            )
            if requested_signature != active_signature:
                print(
                    "[服务] 热词解码器结构配置已变化；为避免混用，等待服务重启。",
                    flush=True,
                )
                return False

        try:
            snapshot = load_inline_hotword_snapshot(requested_decoder)
        except (OSError, UnicodeError) as exc:
            print(
                "[服务] 新会话热词快照无效，保留上一版："
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )
            return False
        with self.lock:
            if refresh_epoch != self.hotword_refresh_epoch:
                self.hotword_refresh_requested = True
                print("[服务] 词库通知已推进代际，本次快照重新排队。", flush=True)
                return False
            if (
                requested_decoder.enabled == self.decoder_config.enabled
                and snapshot.sha256 == self.hotword_snapshot.sha256
            ):
                return False
            self.decoder_config = requested_decoder
            self.hotword_snapshot = snapshot

        print(
            f"[服务] 已更新下一会话热词快照：entries={snapshot.entries} "
            f"bytes={snapshot.byte_count} sha256={snapshot.sha256}",
            flush=True,
        )
        return True

    @objc.python_method
    def schedule_hotword_refresh(self):
        """Coalesce lightweight snapshot refreshes on one daemon thread."""
        with self.lock:
            if self.hotword_refresh_in_progress:
                self.hotword_refresh_requested = True
                return False
            self.hotword_refresh_in_progress = True

        def worker():
            while True:
                try:
                    self.refresh_inline_hotwords()
                except Exception as exc:
                    print(
                        "[服务] 后台词库检查失败；继续使用上一版会话热词："
                        f"{type(exc).__name__}: {exc}",
                        flush=True,
                    )
                with self.lock:
                    if self.hotword_refresh_requested:
                        self.hotword_refresh_requested = False
                        continue
                    self.hotword_refresh_in_progress = False
                    return

        try:
            refresh_thread = threading.Thread(target=worker, daemon=True)
            refresh_thread.start()
        except Exception as exc:
            with self.lock:
                self.hotword_refresh_in_progress = False
            print(
                "[服务] 无法启动后台词库线程；保留上一版会话热词："
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )
            return False
        return True

    @objc.python_method
    def lexicon_changed(self):
        """Debounce persisted generations before refreshing an in-memory snapshot."""
        with self.lock:
            self.hotword_refresh_epoch += 1
            epoch = self.hotword_refresh_epoch
            previous_timer = self.hotword_refresh_timer
            timer = threading.Timer(
                HOTWORD_REFRESH_DEBOUNCE_SECONDS,
                self.run_debounced_hotword_refresh,
                args=(epoch,),
            )
            timer.daemon = True
            self.hotword_refresh_timer = timer
        if previous_timer is not None:
            previous_timer.cancel()
        try:
            timer.start()
        except Exception as exc:
            with self.lock:
                if self.hotword_refresh_timer is timer:
                    self.hotword_refresh_timer = None
            print(
                "[服务] 无法启动词库去抖定时器，立即刷新轻量快照："
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )
            self.schedule_hotword_refresh()

    @objc.python_method
    def run_debounced_hotword_refresh(self, epoch):
        with self.lock:
            if epoch != self.hotword_refresh_epoch:
                return False
            self.hotword_refresh_timer = None
        return self.schedule_hotword_refresh()

    def handleLexiconChanged_(self, _notification):
        self.lexicon_changed()

    @objc.python_method
    def start_recording(self, session_id):
        failure_message = ""
        inline_fallback = False
        with self.lock:
            if self.phase != "idle":
                busy_message = f"service busy in phase={self.phase}"
                print(f"[录音] 拒绝 session={session_id}: {busy_message}", flush=True)
                self.post(ERROR_NAME, session_id, message=busy_message)
                return
            recognizer = self.recognizer
            snapshot = self.hotword_snapshot
            stream_started = time.perf_counter()
            try:
                stream = (
                    recognizer.create_stream(snapshot.text)
                    if self.inline_hotword_supported and snapshot.text
                    else recognizer.create_stream()
                )
            except Exception as inline_exc:
                if not (self.inline_hotword_supported and snapshot.text):
                    failure_message = f"{type(inline_exc).__name__}: {inline_exc}"
                    stream = None
                else:
                    inline_fallback = True
                    try:
                        stream = recognizer.create_stream()
                    except Exception as plain_exc:
                        failure_message = (
                            "inline stream failed "
                            f"({type(inline_exc).__name__}: {inline_exc}); "
                            "plain stream failed "
                            f"({type(plain_exc).__name__}: {plain_exc})"
                        )
                        stream = None
            stream_create_ms = (time.perf_counter() - stream_started) * 1000
            if stream is not None:
                self.phase = "recording"
                self.session_id = session_id
                self.stream = stream
                self.stop_event.clear()
                self.stop_requested_at = None
        if failure_message:
            print(f"[录音] 无法创建会话 session={session_id}: {failure_message}", flush=True)
            self.post(ERROR_NAME, session_id, message=failure_message)
            return
        if inline_fallback:
            print(
                f"[录音] 会话热词图创建失败，session={session_id} 使用无热词上下文。",
                flush=True,
            )
        effective_entries = 0 if inline_fallback else snapshot.entries
        print(
            f"[录音] 开始 session={session_id} entries={effective_entries} "
            f"stream_ms={stream_create_ms:.3f} monotonic={time.monotonic():.3f}",
            flush=True,
        )
        self.post(STARTED_NAME, session_id)
        try:
            recording_thread = threading.Thread(
                target=self.record_worker,
                args=(session_id, recognizer, stream),
                daemon=True,
            )
            recording_thread.start()
        except Exception as exc:
            message = f"recording thread unavailable: {type(exc).__name__}: {exc}"
            with self.lock:
                if self.session_id == session_id:
                    self.phase = "idle"
                    self.session_id = ""
                    self.stream = None
                    self.stop_event.clear()
            print(f"[录音] {message}", flush=True)
            self.post(ERROR_NAME, session_id, message=message)

    def handleStart_(self, notification):
        info = notification.userInfo() or {}
        session_id = str(info.get("session", ""))
        if session_id:
            self.start_recording(session_id)

    def handleStop_(self, notification):
        info = notification.userInfo() or {}
        session_id = str(info.get("session", ""))
        with self.lock:
            if self.phase != "recording" or session_id != self.session_id:
                return
            self.phase = "finalizing"
            self.stop_requested_at = time.monotonic()
            self.stop_event.set()
        print(f"[录音] 停止 session={session_id}", flush=True)

    @objc.python_method
    def record_worker(self, session_id, recognizer, stream):
        committed_segments = []
        last_segment_raw = ""
        last_published_text = ""
        partial_tracker = PartialStabilityTracker()
        revision_tracker = RevisionAttributionTracker()
        worker_started_at = time.monotonic()
        first_partial_ms = None
        decode_durations_ms = []
        normalize_durations_ms = []
        overflow_count = 0
        release_tail_frames = 0
        post_stop_partial_count = 0
        last_post_stop_partial_ms = None
        audio_buffer = None
        speech_gate = None
        try:
            refiner = getattr(self, "final_refiner", None)
            if refiner is not None and refiner.needs_audio:
                audio_buffer = BoundedAudioBuffer(
                    max_seconds=refiner.max_audio_seconds,
                )
        except Exception as exc:
            _log_optional_refiner_failure("audio_buffer_construction", exc)
        audio_buffer_failure_reported = False

        def combined_partial(current=""):
            raw_text = join_recognition_segments(
                committed_segments + ([current] if current else [])
            )
            started = time.perf_counter()
            try:
                # Segments can split a number or other spoken expression; apply
                # the open-tail pipeline after joining raw ASR text.
                return raw_text, normalize_partial(raw_text)
            finally:
                normalize_durations_ms.append(
                    (time.perf_counter() - started) * 1000
                )

        def text_utf16_length(text):
            return len(text.encode("utf-16-le")) // 2

        def publish_partial(raw_text, normalization):
            nonlocal last_published_text, first_partial_ms
            nonlocal post_stop_partial_count, last_post_stop_partial_ms
            text = normalization.text
            attribution = revision_tracker.observe(raw_text, text)
            if text == last_published_text:
                return False
            presentation = partial_tracker.observe(
                text,
                stable_limit_codepoints=normalization.stable_limit_codepoints,
            )
            self.post(
                PARTIAL_NAME,
                session_id,
                text=text,
                sequence=presentation.sequence,
                stable_utf16=presentation.stable_utf16,
                revision_utf16=presentation.revision_utf16,
                raw_revision_utf16=attribution.raw_revision_utf16,
                raw_rollback=attribution.raw_rollback,
                itn_reclassified=attribution.presentation_reclassification,
            )
            last_published_text = text
            published_at = time.monotonic()
            if first_partial_ms is None:
                first_partial_ms = (published_at - worker_started_at) * 1000
            stop_requested_at = getattr(self, "stop_requested_at", None)
            if (
                isinstance(stop_requested_at, (int, float))
                and published_at >= stop_requested_at
            ):
                post_stop_partial_count += 1
                last_post_stop_partial_ms = (
                    published_at - stop_requested_at
                ) * 1000
            print(
                f"[实时] seq={presentation.sequence} "
                f"utf16={text_utf16_length(text)} "
                f"stable_utf16={presentation.stable_utf16} "
                f"revision_utf16={presentation.revision_utf16} "
                f"raw_revision_utf16={attribution.raw_revision_utf16} "
                f"raw_rollback={int(attribution.raw_rollback)} "
                f"itn_reclassified={int(attribution.presentation_reclassification)}",
                flush=True,
            )
            return True

        active_input = None
        capture_thread = None
        capture_done = threading.Event()
        capture_error = []
        capture_queue = None
        capture_queue_drops = [0]
        capture_queue_peak = [0]
        open_attempt = MicrophoneOpenAttempt()
        open_watchdog = None
        try:
            if self.stop_event.is_set():
                # A short Control hold can deliver STOP before this daemon
                # thread gets scheduled.  Do not open CoreAudio after the
                # session is already over, and do not force a cold ITN load
                # merely to publish an empty terminal result.
                stream.input_finished()
                self.post(FINAL_NAME, session_id, text="")
                print(f"[定稿] <空>（麦克风启动前已停止）", flush=True)
                return
            open_watchdog = threading.Timer(
                MICROPHONE_OPEN_TIMEOUT_SECONDS,
                self.handle_microphone_open_timeout,
                args=(session_id, open_attempt),
            )
            open_watchdog.daemon = True
            open_watchdog.start()
            active_input = open_first_available_input(
                sd,
                prefer_builtin=PREFER_BUILTIN_MIC,
            )
            with self.lock:
                open_was_timed_out = open_attempt.timed_out
                if not open_was_timed_out:
                    open_attempt.settled.set()
            open_watchdog.cancel()
            if open_was_timed_out:
                active_input.close()
                active_input = None
                return
            mic = active_input.stream
            capture_rate = active_input.candidate.sample_rate
            frame_samples = active_input.candidate.read_frames
            capture_queue = queue.Queue(
                maxsize=max(
                    1,
                    int(AUDIO_CAPTURE_QUEUE_SECONDS * capture_rate / frame_samples),
                )
            )

            def capture_audio():
                try:
                    while True:
                        if self.stop_event.is_set():
                            requested_at = getattr(self, "stop_requested_at", None)
                            if not isinstance(requested_at, (int, float)):
                                requested_at = time.monotonic()
                            if (
                                time.monotonic() - requested_at
                                >= AUDIO_RELEASE_TAIL_SECONDS
                            ):
                                break
                        item = mic.read(frame_samples)
                        try:
                            capture_queue.put(item, timeout=0.1)
                            capture_queue_peak[0] = max(
                                capture_queue_peak[0],
                                capture_queue.qsize(),
                            )
                        except queue.Full:
                            capture_queue_drops[0] += 1
                except Exception as exc:
                    capture_error.append(exc)
                finally:
                    capture_done.set()

            capture_thread = threading.Thread(
                target=capture_audio,
                daemon=True,
                name="VoiceFlypyAudioCapture",
            )
            capture_thread.start()
            speech_gate = AdaptiveSpeechStartGate(capture_rate)
            self.post(MIC_READY_NAME, session_id)
            print(
                f"[录音] 使用麦克风：{active_input.candidate.name} "
                f"({capture_rate}Hz; selection="
                f"{getattr(active_input, 'selection_reason', 'unknown')}) "
                f"read_ms={frame_samples * 1000 / capture_rate:.1f} "
                f"start_gate={speech_gate.policy} "
                f"monotonic={time.monotonic():.3f}",
                flush=True,
            )
            try:
                while not capture_done.is_set() or not capture_queue.empty():
                    try:
                        buf, overflowed = capture_queue.get(timeout=0.05)
                    except queue.Empty:
                        continue
                    if overflowed:
                        overflow_count += 1
                    if self.stop_event.is_set():
                        release_tail_frames += 1
                    gate_was_open = speech_gate.opened
                    accepted_audio = speech_gate.process(buf.ravel())
                    if accepted_audio is None or accepted_audio.size == 0:
                        continue
                    if not gate_was_open and speech_gate.opened:
                        print(
                            "[起音] gate=open "
                            f"delay_ms={speech_gate.open_after_ms:.1f} "
                            f"suppressed_ms={speech_gate.suppressed_ms:.1f} "
                            f"noise_dbfs={speech_gate.noise_floor_dbfs:.1f} "
                            f"trigger_dbfs={speech_gate.trigger_level_dbfs:.1f} "
                            f"peak_dbfs={getattr(speech_gate, 'peak_level_dbfs', -1.0):.1f} "
                            f"speech_like_frames="
                            f"{getattr(speech_gate, 'speech_like_frames', -1)}",
                            flush=True,
                        )
                    if audio_buffer is not None:
                        try:
                            retained = audio_buffer.append(accepted_audio, capture_rate)
                            unavailable_reason = audio_buffer.unavailable_reason
                            if (
                                not retained
                                and unavailable_reason
                                and not audio_buffer_failure_reported
                            ):
                                audio_buffer_failure_reported = True
                                try:
                                    print(
                                        "[语义终判] 本会话不再收集模型音频："
                                        f"{unavailable_reason}",
                                        flush=True,
                                    )
                                except Exception as exc:
                                    _log_optional_refiner_failure(
                                        "audio_unavailable_log",
                                        exc,
                                    )
                        except Exception as exc:
                            audio_buffer = None
                            if not audio_buffer_failure_reported:
                                audio_buffer_failure_reported = True
                                _log_optional_refiner_failure("audio_buffer_append", exc)
                    decode_started = time.perf_counter()
                    stream.accept_waveform(capture_rate, accepted_audio)
                    while recognizer.is_ready(stream):
                        recognizer.decode_stream(stream)
                    raw_partial = str(recognizer.get_result(stream) or "").strip()
                    decode_durations_ms.append(
                        (time.perf_counter() - decode_started) * 1000
                    )
                    if raw_partial and raw_partial != last_segment_raw:
                        last_segment_raw = raw_partial
                        raw_text, partial = combined_partial(raw_partial)
                        publish_partial(raw_text, partial)
                    if recognizer.is_endpoint(stream):
                        segment = raw_partial or last_segment_raw
                        if segment:
                            committed_segments.append(segment)
                            # An acoustic endpoint is not punctuation.  Re-publish
                            # committed text without inventing a visible trailing
                            # space while Control remains held.
                            raw_text, partial = combined_partial()
                            publish_partial(raw_text, partial)
                            print(
                                f"[分段] utf16={text_utf16_length(partial.text)} "
                                f"segment_utf16={text_utf16_length(segment)}",
                                flush=True,
                            )
                        recognizer.reset(stream)
                        last_segment_raw = ""
                if capture_error:
                    raise capture_error[0]
            finally:
                if capture_thread is not None:
                    capture_thread.join(timeout=1.0)
                active_input.close()
                active_input = None

            stream.input_finished()
            while recognizer.is_ready(stream):
                recognizer.decode_stream(stream)
            final_segment = str(recognizer.get_result(stream) or "").strip()
            if not final_segment:
                final_segment = last_segment_raw
            if final_segment:
                committed_segments.append(final_segment)
            raw_text = join_recognition_segments(committed_segments)
            normalize_started = time.perf_counter()
            baseline_text = normalize(raw_text)
            normalize_durations_ms.append(
                (time.perf_counter() - normalize_started) * 1000
            )
            try:
                final_text = self.refine_final_text(
                    session_id,
                    committed_segments,
                    raw_text,
                    baseline_text,
                    audio_buffer,
                )
                if not isinstance(final_text, str):
                    raise TypeError("refiner final text must be str")
            except Exception as exc:
                _log_optional_refiner_failure("finalization_boundary", exc)
                final_text = baseline_text
            # Leave one separator after every non-empty dictation commit so a
            # later Control-held session starts as a distinct phrase.
            final_text = finalize_spacing(final_text)
            self.post(FINAL_NAME, session_id, text=final_text)
            decode_total_ms = sum(decode_durations_ms)
            normalize_total_ms = sum(normalize_durations_ms)
            stop_requested_at = getattr(self, "stop_requested_at", None)
            stop_to_final_ms = (
                max(0.0, (time.monotonic() - stop_requested_at) * 1000)
                if isinstance(stop_requested_at, (int, float))
                else -1.0
            )
            print(
                f"[定稿] utf16={text_utf16_length(final_text)} "
                f"segments={len(committed_segments)} overflows={overflow_count} "
                f"capture_queue_drops={capture_queue_drops[0]} "
                f"capture_queue_peak={capture_queue_peak[0]} "
                f"speech_gate={'open' if speech_gate and speech_gate.opened else 'closed'} "
                f"gate_open_ms="
                f"{speech_gate.open_after_ms if speech_gate and speech_gate.open_after_ms is not None else -1.0:.1f} "
                f"gate_suppressed_ms="
                f"{speech_gate.suppressed_ms if speech_gate else -1.0:.1f} "
                f"gate_peak_dbfs="
                f"{getattr(speech_gate, 'peak_level_dbfs', -1.0) if speech_gate else -1.0:.1f} "
                f"gate_speech_like_frames="
                f"{getattr(speech_gate, 'speech_like_frames', -1) if speech_gate else -1} "
                f"release_tail_frames={release_tail_frames} "
                f"release_tail_target_ms={AUDIO_RELEASE_TAIL_SECONDS * 1000:.1f} "
                f"post_stop_partials={post_stop_partial_count} "
                f"last_post_stop_partial_ms="
                f"{last_post_stop_partial_ms if last_post_stop_partial_ms is not None else -1.0:.1f} "
                f"partials={partial_tracker.sequence} "
                f"raw_rollbacks={revision_tracker.raw_rollback_count} "
                f"itn_reclassifications="
                f"{revision_tracker.presentation_reclassification_count} "
                f"stable_rollbacks={partial_tracker.rollback_count} "
                f"first_partial_ms={first_partial_ms if first_partial_ms is not None else -1.0:.1f} "
                f"decode_total_ms={decode_total_ms:.1f} "
                f"decode_max_ms={max(decode_durations_ms, default=0.0):.1f} "
                f"normalize_total_ms={normalize_total_ms:.1f} "
                f"normalize_max_ms={max(normalize_durations_ms, default=0.0):.1f} "
                f"stop_to_final_ms={stop_to_final_ms:.1f}",
                flush=True,
            )
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            print(f"[语音错误] {message}", flush=True)
            self.post(ERROR_NAME, session_id, message=message)
        finally:
            with self.lock:
                if not open_attempt.timed_out:
                    open_attempt.settled.set()
            if open_watchdog is not None:
                open_watchdog.cancel()
            if active_input is not None:
                try:
                    active_input.close()
                except Exception as exc:
                    print(f"[录音] 关闭麦克风失败：{exc}", flush=True)
            became_idle = False
            with self.lock:
                if self.session_id == session_id:
                    self.phase = "idle"
                    self.session_id = ""
                    self.stream = None
                    self.stop_event.clear()
                    became_idle = True
            if became_idle:
                # Lightweight refresh only; the recognizer is never rebuilt.
                self.schedule_hotword_refresh()


SERVICE = None


def main():
    global SERVICE
    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
    # Keep the Objective-C object alive for the lifetime of the application.
    SERVICE = VoiceService.alloc().init()
    app.run()


if __name__ == "__main__":
    if "--self-test-itn" in sys.argv[1:]:
        raise SystemExit(run_itn_self_test())
    if "--self-test-personal-phonetics" in sys.argv[1:]:
        raise SystemExit(run_personal_phonetic_self_test())
    main()
