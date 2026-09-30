#!/usr/bin/env python3
"""
个人语音输入工具 v0.2 —— SenseVoice 多语言版
- 运行后立即录音，静音约 1.2 秒自动结束
- 中英（含日韩粤）混合识别，专有名词能力强
- use_itn：自动把口语数字/单位转成规范写法
- 结果自动进剪贴板 + 弹通知；全程本机离线
"""
import os
import json
import re
import subprocess
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from voice_personal_phonetics import apply_personal_phonetic_corrections
from voice_numeric_normalizer import (
    normalize_spoken_numbers as _normalize_spoken_numbers,
    normalize_spoken_numeric_sequences,
    normalize_spoken_versions,
)

BASE = str(Path(__file__).resolve().parent)
MODEL_DIR = str(
    Path(BASE) / "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09"
)
DOMAIN_DICT = str(
    Path.home()
    / "Library/Application Support/VoiceFly/Lexicon/domain_dict.txt"
)
LIVE_LEXICON_ROOT = (
    Path.home()
    / "Library/Application Support/VoiceFly/Lexicon"
)
MODEL_SCOPE_ID = (
    "sherpa-onnx-x-asr-480ms-streaming-zipformer-transducer-zh-en-punct-"
    "int8-2026-06-05"
)
_DOMAIN_DICT_CACHE = {"key": None, "mapping": []}
_VOICE_RULES_CACHE = {"key": None, "document": None}
_SPOKEN_NUMBER_EXCEPTIONS_CACHE = {"key": None, "phrases": ()}
_ENGINEERING_SYMBOL_CACHE = {"key": None, "rules": ()}


@dataclass(frozen=True)
class _Candidate:
    start: int
    end: int
    canonical: str
    priority: int
    order: int


def _domain_dict_candidates():
    """Prefer Simon's live dictionary, then the snapshot bundled with VoiceFly."""
    explicit = os.environ.get("VOICEFLY_DOMAIN_DICT", "").strip()
    if explicit:
        yield Path(explicit).expanduser()
    yield Path(DOMAIN_DICT)
    resource_dir = os.environ.get("VOICEFLY_RESOURCE_DIR")
    if resource_dir:
        yield Path(resource_dir) / "lexicon" / "domain_dict.txt"
        yield Path(resource_dir) / "domain_dict.txt"
    yield Path(__file__).resolve().parent / "domain_dict.txt"


def _voice_rules_candidates():
    """Prefer a synchronized generated lexicon, then packaged/source snapshots."""
    explicit = os.environ.get("VOICEFLY_VOICE_RULES_FILE", "").strip()
    if explicit:
        yield Path(explicit).expanduser()
    yield LIVE_LEXICON_ROOT / "voice_rules.json"
    yield LIVE_LEXICON_ROOT / "voice" / "voice_rules.json"
    resource_dir = os.environ.get("VOICEFLY_RESOURCE_DIR")
    if resource_dir:
        yield Path(resource_dir) / "lexicon" / "voice_rules.json"
    source_root = Path(__file__).resolve().parent
    yield source_root / "lexicon" / "generated" / "simon_all" / "voice" / "voice_rules.json"
    yield source_root / "lexicon" / "build" / "simon_all" / "voice" / "voice_rules.json"
    yield source_root / "lexicon" / "voice_rules.json"


def _spoken_number_exception_candidates():
    """Return the editable numeric-idiom exception table, preferring live data."""
    explicit = os.environ.get("VOICEFLY_SPOKEN_NUMBER_EXCEPTIONS", "").strip()
    if explicit:
        yield Path(explicit).expanduser()
    yield LIVE_LEXICON_ROOT / "spoken_number_exceptions.txt"
    yield LIVE_LEXICON_ROOT / "voice" / "spoken_number_exceptions.txt"
    resource_dir = os.environ.get("VOICEFLY_RESOURCE_DIR")
    if resource_dir:
        yield Path(resource_dir) / "lexicon" / "spoken_number_exceptions.txt"
    yield Path(__file__).resolve().parent / "lexicon" / "spoken_number_exceptions.txt"


def _engineering_symbol_alias_candidates():
    """Prefer the synchronized symbol vocabulary, then the packaged snapshot."""
    explicit = os.environ.get("VOICEFLY_ENGINEERING_SYMBOL_ALIASES", "").strip()
    if explicit:
        yield Path(explicit).expanduser()
    yield LIVE_LEXICON_ROOT / "engineering_symbol_aliases.tsv"
    resource_dir = os.environ.get("VOICEFLY_RESOURCE_DIR")
    if resource_dir:
        yield Path(resource_dir) / "lexicon" / "engineering_symbol_aliases.tsv"
    yield Path(__file__).resolve().parent / "lexicon/data/imports/engineering_symbol_aliases.tsv"


def _reset_rule_caches_for_tests():
    """Clear stat-keyed caches.  Kept private, but useful to deterministic tests."""
    _DOMAIN_DICT_CACHE.update(key=None, mapping=[])
    _VOICE_RULES_CACHE.update(key=None, document=None)
    _SPOKEN_NUMBER_EXCEPTIONS_CACHE.update(key=None, phrases=())
    _ENGINEERING_SYMBOL_CACHE.update(key=None, rules=())


def load_engineering_symbol_aliases():
    """Load ``spoken<TAB>glyph<TAB>mode`` rules with stat-based caching."""
    for path in _engineering_symbol_alias_candidates():
        try:
            stat = path.stat()
            cache_key = (str(path), stat.st_mtime_ns, stat.st_size)
            if _ENGINEERING_SYMBOL_CACHE["key"] == cache_key:
                return _ENGINEERING_SYMBOL_CACHE["rules"]
            rules = []
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line or line.lstrip().startswith("#"):
                    continue
                fields = line.split("\t")
                if len(fields) < 3:
                    continue
                spoken, glyph, mode = (field.strip() for field in fields[:3])
                if spoken and glyph and mode in {"explicit", "after_number"}:
                    rules.append((spoken, glyph, mode))
            rules.sort(key=lambda row: (-len(row[0]), row[0], row[2]))
            frozen = tuple(rules)
            _ENGINEERING_SYMBOL_CACHE.update(key=cache_key, rules=frozen)
            return frozen
        except (OSError, UnicodeError):
            continue
    return ()


def apply_engineering_symbols(text):
    """Render engineering glyphs only for explicit commands or numeric units."""
    for spoken, glyph, mode in load_engineering_symbol_aliases():
        if mode == "explicit":
            text = text.replace(spoken, glyph)
            continue
        pattern = re.compile(
            rf"(?<![A-Za-z0-9])(\d+(?:\.\d+)?)\s*{re.escape(spoken)}"
        )
        text = pattern.sub(lambda match: match.group(1) + glyph, text)
    return text


def load_spoken_number_exceptions():
    """Load protected Chinese phrases, one phrase per line, longest first."""
    for path in _spoken_number_exception_candidates():
        try:
            stat = path.stat()
            cache_key = (str(path), stat.st_mtime_ns, stat.st_size)
            if _SPOKEN_NUMBER_EXCEPTIONS_CACHE["key"] == cache_key:
                return _SPOKEN_NUMBER_EXCEPTIONS_CACHE["phrases"]
            phrases = tuple(sorted({
                line.strip()
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            }, key=lambda phrase: (-len(phrase), phrase)))
            _SPOKEN_NUMBER_EXCEPTIONS_CACHE.update(key=cache_key, phrases=phrases)
            return phrases
        except (OSError, UnicodeError):
            continue
    return ()

def load_domain_dict():
    """加载领域词典：误识别形态 -> 正确写法"""
    for path in _domain_dict_candidates():
        try:
            stat = path.stat()
            cache_key = (str(path), stat.st_mtime_ns, stat.st_size)
            if _DOMAIN_DICT_CACHE["key"] == cache_key:
                return _DOMAIN_DICT_CACHE["mapping"]

            mapping = []
            with path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.rstrip("\n")
                    if not line or line.startswith("#") or "\t" not in line:
                        continue
                    parts = line.split("\t")
                    morph, correct = parts[0].strip(), parts[1].strip()
                    if morph and correct:
                        mapping.append((morph, correct))
            # 长形态优先替换，避免短词先吃掉长词的一部分
            mapping.sort(key=lambda x: -len(x[0]))
            _DOMAIN_DICT_CACHE["key"] = cache_key
            _DOMAIN_DICT_CACHE["mapping"] = mapping
            return mapping
        except (OSError, UnicodeError):
            continue
    return []


def _validated_rule_document(payload):
    if not isinstance(payload, dict):
        return None
    normalized = {
        "schema_version": str(payload.get("schema_version", "")),
        "profile": str(payload.get("profile", "")),
        "release": str(payload.get("release", "")),
        "rules": [],
    }
    for group in ("semantic", "casing"):
        entries = payload.get(group, [])
        if not isinstance(entries, list):
            return None
        for source in entries:
            if not isinstance(source, dict):
                continue
            form = source.get("form")
            canonical = source.get("canonical")
            if not isinstance(form, str) or not form.strip():
                continue
            if not isinstance(canonical, str) or not canonical.strip():
                continue
            rule = dict(source)
            rule["form"] = form.strip()
            rule["canonical"] = canonical.strip()
            rule["priority"] = int(source.get("priority", 0))
            rule["positive_context"] = [
                str(item).strip()
                for item in source.get("positive_context", [])
                if str(item).strip()
            ]
            rule["negative_context"] = [
                str(item).strip()
                for item in source.get("negative_context", [])
                if str(item).strip()
            ]
            rule["_group"] = group
            normalized["rules"].append(rule)
    return normalized


def _load_voice_rule_document():
    for path in _voice_rules_candidates():
        try:
            stat = path.stat()
            cache_key = (str(path), stat.st_mtime_ns, stat.st_size)
            if _VOICE_RULES_CACHE["key"] == cache_key:
                return _VOICE_RULES_CACHE["document"]
            with path.open(encoding="utf-8") as handle:
                document = _validated_rule_document(json.load(handle))
            if document is None:
                continue
            _VOICE_RULES_CACHE["key"] = cache_key
            _VOICE_RULES_CACHE["document"] = document
            return document
        except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
            continue
    return None


def load_voice_rules():
    """Return validated structured rules, or an empty list when unavailable."""
    document = _load_voice_rule_document()
    return list(document["rules"]) if document else []

def normalize_english_case(text):
    """识别输出的英文往往是全大写：先转小写（句首大写、单独 i 变 I），
    之后再跑领域词典恢复专有名词的正确大小写。"""
    text = text.lower()
    text = re.sub(r"\bi\b", "I", text)
    if text and text[0].isalpha():
        text = text[0].upper() + text[1:]
    return text


def normalize_spoken_numbers(text):
    """Compatibility entry point backed by the canonical numeric engine."""
    return _normalize_spoken_numbers(text, load_spoken_number_exceptions())


def normalize_dictation_punctuation(text):
    """Replace prose punctuation while preserving structured-number syntax."""
    output = []
    for index, char in enumerate(text):
        is_numeric_separator = (
            char in {".", "．", "/", "／", ":", "：", "-", "－"}
            and index > 0
            and index + 1 < len(text)
            and text[index - 1].isdigit()
            and text[index + 1].isdigit()
        )
        is_numeric_sign = (
            char in {"-", "－"}
            and index + 1 < len(text)
            and text[index + 1].isdigit()
            and (
                index == 0
                or not text[index - 1].isdigit()
            )
        )
        is_meridiem_dot = (
            char in {".", "．"}
            and (
                (
                    index > 0
                    and index + 1 < len(text)
                    and text[index - 1] in "aApP"
                    and text[index + 1] in "mM"
                )
                or (
                    index >= 3
                    and text[index - 1] in "mM"
                    and text[index - 2] in {".", "．"}
                    and text[index - 3] in "aApP"
                )
            )
        )
        if char in {"%", "‰", "‱"}:
            output.append(char)
        elif is_numeric_separator or is_numeric_sign or is_meridiem_dot:
            output.append({"．": ".", "／": "/", "：": ":", "－": "-"}.get(char, char))
        elif unicodedata.category(char).startswith("P"):
            output.append(" ")
        else:
            output.append(char)
    return " ".join("".join(output).split())


_HORIZONTAL_WHITESPACE_RE = re.compile(r"([ \t]+)")
_SINGLE_SPELLED_CAPITAL_RE = re.compile(r"[A-Z]")
_ASCII_ALNUM_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")
_ALL_CAPS_WORD_RE = re.compile(r"[A-Z]{2,}")


def normalize_spelled_acronyms(text):
    """Compact low-ambiguity spelled capitals such as ``H U D``.

    Run this before punctuation cleanup so punctuation remains a hard boundary:
    ``A，B`` may become ``A B`` for dictation display, but must never become
    the unrelated acronym ``AB``.  Runs are discovered token by token so they
    also work inside English sentences.  A neighbouring token containing a
    digit blocks compaction, preventing a partial rewrite of identifiers such
    as ``H U D2`` or ``H U D E2``.  Lowercase variables and ordinary English
    words are intentionally left unchanged.
    """

    parts = _HORIZONTAL_WHITESPACE_RE.split(text)
    output = []
    index = 0
    while index < len(parts):
        if _SINGLE_SPELLED_CAPITAL_RE.fullmatch(parts[index]):
            end = index
            while (
                end + 2 < len(parts)
                and _HORIZONTAL_WHITESPACE_RE.fullmatch(parts[end + 1])
                and _SINGLE_SPELLED_CAPITAL_RE.fullmatch(parts[end + 2])
            ):
                end += 2

            count = (end - index) // 2 + 1
            if count >= 2:
                previous_token = parts[index - 2] if index >= 2 else ""
                next_token = parts[end + 2] if end + 2 < len(parts) else ""
                neighbours = (previous_token, next_token)
                identifier_blocked = any(
                    _ASCII_ALNUM_TOKEN_RE.fullmatch(token)
                    and any("0" <= char <= "9" for char in token)
                    for token in neighbours
                )
                # If only two standalone letters precede/follow an all-caps
                # token, they may be the truncated front of one identifier
                # (for example H U DABC).  Keep that ambiguous form intact.
                continuation_blocked = count == 2 and any(
                    _ALL_CAPS_WORD_RE.fullmatch(token) for token in neighbours
                )
                if not identifier_blocked and not continuation_blocked:
                    output.append("".join(parts[pos] for pos in range(index, end + 1, 2)))
                    index = end + 1
                    continue

        output.append(parts[index])
        index += 1
    return "".join(output)


def _legacy_rules():
    rules = []
    for order, (form, canonical) in enumerate(load_domain_dict()):
        rules.append(
            {
                "form": form,
                "canonical": canonical,
                "kind": "asr_error",
                "match_mode": "token" if form.isascii() else "phrase",
                "priority": 100,
                "risk": "medium",
                "positive_context": [],
                "negative_context": [],
                "_group": "semantic",
                "_legacy_order": order,
            }
        )
    return rules


def _is_spelled_token(form):
    """True for low-ambiguity letter-by-letter forms such as ``R A G``."""
    parts = form.split()
    return (
        len(parts) >= 2
        and all(len(part) == 1 and part.isascii() and part.isalnum() for part in parts)
    )


def _rule_occurrences(text, rule):
    form = rule["form"]
    mode = str(rule.get("match_mode", "phrase")).lower()
    if form.isascii():
        escaped = re.escape(form)
        if mode in {"token", "context"}:
            escaped = rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])"
        pattern = re.compile(escaped, re.IGNORECASE)
        yield from ((match.start(), match.end()) for match in pattern.finditer(text))
        return

    start = 0
    while True:
        start = text.find(form, start)
        if start < 0:
            return
        end = start + len(form)
        yield start, end
        start += max(1, len(form))


def _contains_context(haystack, needle):
    if needle.isascii():
        return re.search(re.escape(needle), haystack, re.IGNORECASE) is not None
    return needle in haystack


def _context_allows(text, start, end, rule):
    # Utterances are short, but a bounded window prevents an unrelated earlier
    # sentence from authorizing an ambiguous correction much later in the text.
    window = text[max(0, start - 48) : min(len(text), end + 48)]
    positives = rule.get("positive_context", [])
    negatives = rule.get("negative_context", [])
    if any(_contains_context(window, item) for item in negatives):
        return False
    if positives and not any(_contains_context(window, item) for item in positives):
        return False
    risk = str(rule.get("risk", "medium")).lower()
    if risk == "blocked":
        return False
    if risk == "high" and not positives and not _is_spelled_token(rule["form"]):
        return False
    return True


def _base_rule_enabled(rule):
    scope = rule.get("model_scope", ["*"])
    if scope and "*" not in scope and MODEL_SCOPE_ID not in scope:
        return False
    if rule.get("_group") == "semantic":
        kind = str(rule.get("kind", ""))
        if kind == "spoken_alias":
            return os.environ.get("VOICEFLY_ENABLE_SPOKEN_ALIASES", "").lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
        if kind != "asr_error":
            return False
    return True


def _apply_rules_once(text, rules):
    candidates = []
    for order, rule in enumerate(rules):
        if not _base_rule_enabled(rule):
            continue
        for start, end in _rule_occurrences(text, rule):
            if not _context_allows(text, start, end, rule):
                continue
            candidates.append(
                _Candidate(
                    start,
                    end,
                    rule["canonical"],
                    int(rule.get("priority", 0)),
                    order,
                )
            )
    if not candidates:
        return text

    # Selection is made entirely against the original utterance.  Replacements
    # are never fed back into matching, eliminating accidental A -> B -> C chains.
    candidates.sort(
        key=lambda item: (
            item.start,
            -(item.end - item.start),
            -item.priority,
            item.order,
        )
    )
    output = []
    cursor = 0
    for candidate in candidates:
        if candidate.start < cursor:
            continue
        output.append(text[cursor : candidate.start])
        output.append(candidate.canonical)
        cursor = candidate.end
    output.append(text[cursor:])
    return "".join(output)

def apply_domain_dict(text):
    """Apply safe ASR-error/casing rules in one leftmost-longest pass.

    The generated JSON is preferred.  The legacy two-column dictionary remains
    a compatible fallback, and the historical public functions keep their
    signatures for older scripts.
    """
    text = unicodedata.normalize("NFKC", text)
    document = _load_voice_rule_document()
    rules = document["rules"] if document is not None else _legacy_rules()
    return _apply_rules_once(text, rules)


# ---- 静音检测参数 ----
SILENCE_SEC = 1.2        # 静音多久判定说完
MIN_UTTERANCE_SEC = 0.4  # 太短的忽略
FRAME = 3200             # 200ms @ 16kHz

def create_recognizer():
    import sherpa_onnx

    return sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=f"{MODEL_DIR}/model.int8.onnx",
        tokens=f"{MODEL_DIR}/tokens.txt",
        num_threads=4,
        use_itn=True,
        provider="cpu",
    )

def main():
    import numpy as np
    import sounddevice as sd

    recognizer = create_recognizer()
    print("🎤 请说话…（停 1.2 秒自动结束）", flush=True)

    frames = []
    with sd.InputStream(channels=1, dtype="float32", samplerate=16000) as mic:
        # 先采 0.5s 环境噪声定基线
        noise = []
        while len(noise) < 5:
            buf, _ = mic.read(FRAME)
            noise.append(buf)
        noise_rms = float(np.sqrt(np.mean(np.concatenate(noise) ** 2)))
        if noise_rms < 0.0005:
            print("\n❌ 麦克风没有任何声音输入（可能是权限未开启）。")
            print("   请打开：系统设置 → 隐私与安全性 → 麦克风")
            print("   确认你运行本工具的终端 App 已被勾选「允许」。")
            print("   设置后重新打开终端再试。")
            return
        threshold = max(0.01, noise_rms * 3.5)

        silence_frames = 0
        voiced_frames = 0
        started = False
        while True:
            buf, _ = mic.read(FRAME)
            rms = float(np.sqrt(np.mean(buf ** 2)))
            if rms > threshold:
                frames.append(buf)
                voiced_frames += 1
                silence_frames = 0
                started = True
                if voiced_frames % 5 == 1:
                    print(f"\r🎙️ 正在听…（已录 {voiced_frames * 0.2:.1f} 秒）", end="", flush=True)
            else:
                if started:
                    frames.append(buf)
                    silence_frames += 1
                    if silence_frames * 0.2 >= SILENCE_SEC:
                        break
                else:
                    frames = []  # 还没开始说话，丢弃

    if not frames or voiced_frames * 0.2 < MIN_UTTERANCE_SEC:
        print("\n没有识别到内容。")
        return

    print("\r⏳ 识别中…", end="", flush=True)
    audio = np.concatenate(frames).ravel()
    stream = recognizer.create_stream()
    stream.accept_waveform(16000, audio)
    recognizer.decode_stream(stream)
    final_text = apply_personal_phonetic_corrections(
        apply_domain_dict(normalize_english_case(stream.result.text.strip()))
    )

    print("\r" + " " * 30, end="\r")
    if not final_text:
        print("没有识别到内容。")
        return
    subprocess.run(["pbcopy"], input=final_text.encode("utf-8"))
    subprocess.run([
        "osascript", "-e",
        f'display notification "已复制到剪贴板" with title "语音输入" subtitle "{final_text[:40]}"',
    ])
    print(f"✅ 已复制：{final_text}")

if __name__ == "__main__":
    main()
