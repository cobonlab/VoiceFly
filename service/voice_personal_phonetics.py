"""Conservative pronunciation repair for explicitly learned Chinese phrases.

The decoder hotword graph can prefer a written token path, but it cannot map a
different homophonic path back to the user's canonical spelling.  This module
provides that second step for the small, explicit ``double-space + length``
lexicon only.  Ordinary candidate-frequency learning is intentionally excluded.

Matching is tone-insensitive and accepts the Mandarin front/back nasal pairs
``an/ang``, ``en/eng``, ``in/ing``, ``ian/iang`` and ``uan/uang``.  A narrower,
context-gated ``j/q/x + iong/iu`` fallback covers weak final ``-ng`` (for
example ``凶`` -> ``绣``) without turning ordinary words such as ``进修`` into
a personal term.  If two explicitly saved phrases collapse to the same
pronunciation signature, that signature is disabled instead of guessing.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import re
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence


PERSONAL_USAGE_ENV = "VOICEFLY_PERSONAL_USAGE"
MAX_PERSONAL_USAGE_BYTES = 4 * 1024 * 1024
MAX_PERSONAL_USAGE_TERMS = 10_000
MAX_EXPLICIT_PHRASES = 256
MIN_PHRASE_LENGTH = 2
MAX_PHRASE_LENGTH = 5
MIN_EXPLICIT_WEIGHT = 100

_HAN_RUN_RE = re.compile(
    r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U000323af]+"
)
_PINYIN_TOKEN_RE = re.compile(r"[a-zv]+")
_RELAXED_CONTEXT_MARKERS = (
    "牌手",
    "选手",
    "扑克",
    "德州",
    "打法",
    "风格",
    "松凶",
    "紧弱",
    "松弱",
    "筹码",
    "底池",
    "这个词",
    "这些字",
    "这两个字",
    "这几个字",
    "说的是",
    "我说",
    "识别",
    "输入",
    "语音",
    "名字",
    "名称",
    "叫做",
    "术语",
    "简称",
)
_UMLAUTED_U_TO_V = str.maketrans(
    {
        "ü": "v",
        "ǖ": "v",
        "ǘ": "v",
        "ǚ": "v",
        "ǜ": "v",
        "Ü": "v",
        "Ǖ": "v",
        "Ǘ": "v",
        "Ǚ": "v",
        "Ǜ": "v",
    }
)


@dataclass(frozen=True)
class PersonalPhraseMatcher:
    """Immutable pronunciation index safe to share across partial results."""

    by_length: dict[int, dict[tuple[str, ...], str]]
    relaxed_by_length: dict[int, dict[tuple[str, ...], str]]
    lengths: tuple[int, ...]
    term_count: int
    collision_count: int


EMPTY_MATCHER = PersonalPhraseMatcher({}, {}, (), 0, 0)


@dataclass(frozen=True)
class HanSegmentation:
    """Complete word-boundary map for one contiguous Han run."""

    ranges: tuple[tuple[int, int], ...]

    def classify(self, start: int, end: int) -> str | None:
        """Return ``singles`` or ``word`` when a span is boundary-safe."""

        selected = [
            (token_start, token_end)
            for token_start, token_end in self.ranges
            if token_start < end and token_end > start
        ]
        if (
            not selected
            or selected[0][0] != start
            or selected[-1][1] != end
            or any(left[1] != right[0] for left, right in zip(selected, selected[1:]))
        ):
            return None
        if len(selected) == 1 and selected[0][1] - selected[0][0] > 1:
            return "word"
        if all(token_end - token_start == 1 for token_start, token_end in selected):
            return "singles"
        return None
_MATCHER_CACHE = {
    "stat_key": None,
    "path": None,
    "matcher": EMPTY_MATCHER,
    "sha256": "",
}


def _is_han(char: str) -> bool:
    codepoint = ord(char)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x20000 <= codepoint <= 0x323AF
    )


def _fold_flypy_pair(pair: str, *, relaxed: bool = False) -> str:
    """Collapse only approved regional pronunciation neighbours."""

    initial, final = pair
    if final in {"j", "h"}:  # an / ang
        return initial + ":an~ang"
    if final in {"f", "g"}:  # en / eng
        return initial + ":en~eng"
    if final in {"b", "k"}:  # in / ing
        return initial + ":in~ing"
    if initial in "jqxnl" and final in {"m", "l"}:  # ian / iang
        return initial + ":ian~iang"
    if initial not in "jqxnl" and final in {"r", "l"}:  # uan / uang
        return initial + ":uan~uang"
    if relaxed and initial in "jqx" and final in {"s", "q"}:  # iong / iu
        return initial + ":iong~iu"
    return pair


def _xform(value: str, pattern: str, replacement: str) -> str:
    return re.sub(pattern, replacement, value)


def _flypy_codes_for_pinyin(syllable: str) -> frozenset[str]:
    """Encode one toneless pinyin syllable with the active Xiaohe algebra."""

    source = syllable.strip().lower().replace("u:", "v")
    if not source or re.fullmatch(r"[a-zv]+", source) is None:
        return frozenset()

    variants = {source}
    if re.fullmatch(r"[jqxy]u", source):
        variants.add(source[0] + "v")
    match = re.fullmatch(r"([aoe])([ioun])", source)
    if match:
        variants.add(match.group(1) * 2 + match.group(2))

    transforms = (
        (r"^([aoe])(ng)?$", r"\1\1\2"),
        (r"iu$", "Q"),
        (r"(.)ei$", r"\1W"),
        (r"uan$", "R"),
        (r"[uv]e$", "T"),
        (r"un$", "Y"),
        (r"^sh", "U"),
        (r"^ch", "I"),
        (r"^zh", "V"),
        (r"uo$", "O"),
        (r"ie$", "P"),
        (r"i?ong$", "S"),
        (r"ing$|uai$", "K"),
        (r"(.)ai$", r"\1D"),
        (r"(.)en$", r"\1F"),
        (r"(.)eng$", r"\1G"),
        (r"[iu]ang$", "L"),
        (r"(.)ang$", r"\1H"),
        (r"ian$", "M"),
        (r"(.)an$", r"\1J"),
        (r"(.)ou$", r"\1Z"),
        (r"[iu]a$", "X"),
        (r"iao$", "N"),
        (r"(.)ao$", r"\1C"),
        (r"ui$", "V"),
        (r"in$", "B"),
    )
    encoded: set[str] = set()
    for value in variants:
        for pattern, replacement in transforms:
            value = _xform(value, pattern, replacement)
        value = value.lower()
        if len(value) == 2 and value.isascii() and value.isalpha():
            encoded.add(value)
    return frozenset(encoded)


def _signature_from_code(code: str, character_count: int) -> tuple[str, ...] | None:
    normalized = code.strip().lower()
    if (
        len(normalized) != character_count * 2
        or not normalized.isascii()
        or not normalized.isalpha()
    ):
        return None
    return tuple(
        _fold_flypy_pair(normalized[index : index + 2])
        for index in range(0, len(normalized), 2)
    )


def _build_matcher(entries: Iterable[tuple[str, str]]) -> PersonalPhraseMatcher:
    signatures: dict[tuple[str, ...], str | None] = {}
    relaxed_signatures: dict[tuple[str, ...], str | None] = {}
    parsed_count = 0
    for canonical, code in entries:
        canonical = "".join(canonical.split())
        code = code.strip().lower()
        if not (
            MIN_PHRASE_LENGTH <= len(canonical) <= MAX_PHRASE_LENGTH
            and all(_is_han(char) for char in canonical)
        ):
            continue
        signature = _signature_from_code(code, len(canonical))
        if signature is None:
            # Older migrated rows may contain legitimate abbreviated Rime
            # codes.  They remain valid learned phrases and decoder hotwords,
            # but are not precise enough to authorize phonetic rewriting.
            continue
        parsed_count += 1
        if parsed_count > MAX_EXPLICIT_PHRASES:
            raise ValueError(f"explicit phrase count exceeds {MAX_EXPLICIT_PHRASES}")
        previous = signatures.get(signature)
        if previous is None and signature not in signatures:
            signatures[signature] = canonical
        elif previous != canonical:
            signatures[signature] = None

        relaxed_signature = tuple(
            _fold_flypy_pair(code[index : index + 2], relaxed=True)
            for index in range(0, len(code), 2)
        )
        relaxed_previous = relaxed_signatures.get(relaxed_signature)
        if relaxed_previous is None and relaxed_signature not in relaxed_signatures:
            relaxed_signatures[relaxed_signature] = canonical
        elif relaxed_previous != canonical:
            relaxed_signatures[relaxed_signature] = None

    by_length: dict[int, dict[tuple[str, ...], str]] = {}
    relaxed_by_length: dict[int, dict[tuple[str, ...], str]] = {}
    collision_count = 0
    for signature, canonical in signatures.items():
        if canonical is None:
            collision_count += 1
            continue
        by_length.setdefault(len(signature), {})[signature] = canonical
    for signature, canonical in relaxed_signatures.items():
        if canonical is None:
            collision_count += 1
            continue
        relaxed_by_length.setdefault(len(signature), {})[signature] = canonical
    return PersonalPhraseMatcher(
        by_length=by_length,
        relaxed_by_length=relaxed_by_length,
        lengths=tuple(sorted(by_length, reverse=True)),
        term_count=sum(len(group) for group in by_length.values()),
        collision_count=collision_count,
    )


def build_personal_phrase_matcher(lines: Iterable[str]) -> PersonalPhraseMatcher:
    """Test/helper adapter for valid explicit learned-phrase TSV rows."""

    entries: list[tuple[str, str]] = []
    for number, line in enumerate(lines, 1):
        if not line or line.lstrip().startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) not in {2, 3}:
            raise ValueError(f"line {number} needs 2 or 3 tab-separated fields")
        if len(fields) == 3:
            try:
                weight = int(fields[2].strip())
            except ValueError as exc:
                raise ValueError(f"line {number} has an invalid integer weight") from exc
            if weight < MIN_EXPLICIT_WEIGHT:
                continue
        entries.append((fields[0], fields[1]))
    return _build_matcher(entries)


def build_personal_usage_matcher(payload: object) -> PersonalPhraseMatcher:
    """Validate schema 1.0.0 and index only ``explicit_count > 0`` terms."""

    if not isinstance(payload, dict) or payload.get("schema_version") != "1.0.0":
        raise ValueError("personal usage has an unsupported or missing schema_version")
    terms = payload.get("terms")
    if not isinstance(terms, dict) or len(terms) > MAX_PERSONAL_USAGE_TERMS:
        raise ValueError("personal usage has an invalid or oversized terms object")
    entries: list[tuple[str, str]] = []
    for canonical, record in terms.items():
        if not isinstance(canonical, str) or not isinstance(record, dict):
            raise ValueError("personal usage contains an invalid term record")
        code = record.get("flypy_code")
        selections = record.get("selection_count")
        explicit = record.get("explicit_count")
        updated_at = record.get("updated_at")
        if (
            not 2 <= len(canonical) <= 12
            or any(char.isspace() for char in canonical)
            or not isinstance(code, str)
            or not code
            or not code.isascii()
            or not code.isalpha()
            or type(selections) is not int
            or type(explicit) is not int
            or selections < 0
            or explicit < 0
            or not isinstance(updated_at, str)
        ):
            raise ValueError("personal usage contains a schema-invalid term")
        if explicit > 0:
            entries.append((canonical, code))
    return _build_matcher(entries)


def _personal_usage_candidates() -> tuple[Path, ...]:
    explicit = os.environ.get(PERSONAL_USAGE_ENV, "").strip()
    if explicit:
        return (Path(explicit).expanduser(),)
    return (
        Path.home()
        / "Library/Application Support/VoiceFly/Lexicon/personal_usage.json",
    )


def _reset_personal_phonetic_cache_for_tests() -> None:
    _MATCHER_CACHE.update(
        stat_key=None,
        path=None,
        matcher=EMPTY_MATCHER,
        sha256="",
    )


def load_personal_phrase_matcher() -> PersonalPhraseMatcher:
    """Load explicit records from the local usage mirror, retaining last good."""

    for path in _personal_usage_candidates():
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            return _MATCHER_CACHE["matcher"]
        if not stat.S_ISREG(metadata.st_mode):
            return _MATCHER_CACHE["matcher"]
        stat_key = (
            str(path),
            metadata.st_ino,
            metadata.st_mtime_ns,
            metadata.st_size,
        )
        if _MATCHER_CACHE["stat_key"] == stat_key:
            return _MATCHER_CACHE["matcher"]
        try:
            data = path.read_bytes()
            if len(data) > MAX_PERSONAL_USAGE_BYTES:
                raise ValueError(
                    f"personal usage bytes exceed {MAX_PERSONAL_USAGE_BYTES}"
                )
            digest = hashlib.sha256(data).hexdigest()
            if (
                _MATCHER_CACHE["path"] == str(path)
                and _MATCHER_CACHE["sha256"] == digest
            ):
                _MATCHER_CACHE["stat_key"] = stat_key
                return _MATCHER_CACHE["matcher"]
            matcher = build_personal_usage_matcher(
                json.loads(data.decode("utf-8"))
            )
        except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
            return _MATCHER_CACHE["matcher"]
        _MATCHER_CACHE.update(
            stat_key=stat_key,
            path=str(path),
            matcher=matcher,
            sha256=digest,
        )
        return matcher

    # A genuinely missing optional file means no explicit personal phrases.
    _MATCHER_CACHE.update(
        stat_key=("missing",),
        path=None,
        matcher=EMPTY_MATCHER,
        sha256="",
    )
    return EMPTY_MATCHER


def _romanize_han_run(value: str) -> tuple[str, ...] | None:
    """Use the already-required macOS Foundation transliterator, lazily."""

    try:
        import Foundation

        source = Foundation.NSString.stringWithString_(value)
        latin = source.stringByApplyingTransform_reverse_(
            Foundation.NSStringTransformToLatin,
            False,
        )
    except Exception:
        return None
    if latin is None:
        return None
    protected_umlaut = str(latin).translate(_UMLAUTED_U_TO_V)
    toneless = "".join(
        char
        for char in unicodedata.normalize("NFD", protected_umlaut)
        if not unicodedata.combining(char)
    ).lower()
    syllables = tuple(_PINYIN_TOKEN_RE.findall(toneless))
    return syllables if len(syllables) == len(value) else None


def _segment_han_words(value: str) -> HanSegmentation | None:
    """Return a complete boundary map from the macOS linguistic tokenizer."""

    try:
        import Foundation

        source = Foundation.NSString.stringWithString_(value)
        words: list[str] = []

        def collect(substring, _substring_range, _enclosing_range, _stop):
            word = str(substring)
            if word and all(_is_han(char) for char in word):
                words.append(word)

        source.enumerateSubstringsInRange_options_usingBlock_(
            (0, len(value)),
            Foundation.NSStringEnumerationByWords,
            collect,
        )
        ranges: list[tuple[int, int]] = []
        cursor = 0
        for word in words:
            start = value.find(word, cursor)
            if start != cursor:
                return None
            end = start + len(word)
            ranges.append((start, end))
            cursor = end
        if cursor != len(value):
            return None
        return HanSegmentation(tuple(ranges))
    except Exception:
        return None


def _candidate_signatures(
    syllables: Sequence[str],
    *,
    relaxed: bool = False,
) -> Iterable[tuple[str, ...]]:
    choices: list[tuple[str, ...]] = []
    for syllable in syllables:
        codes = _flypy_codes_for_pinyin(syllable)
        folded = tuple(
            sorted({_fold_flypy_pair(code, relaxed=relaxed) for code in codes})
        )
        if not folded:
            return ()
        choices.append(folded)
    return itertools.product(*choices)


def _repair_han_run(
    value: str,
    matcher: PersonalPhraseMatcher,
    syllables: Sequence[str],
    segmentation: HanSegmentation,
    context_text: str,
    context_offset: int,
) -> str:
    output: list[str] = []
    position = 0
    while position < len(value):
        replacement: tuple[int, str] | None = None
        for length in matcher.lengths:
            if position + length > len(value):
                continue
            strict_targets = {
                matcher.by_length[length][signature]
                for signature in _candidate_signatures(
                    syllables[position : position + length]
                )
                if signature in matcher.by_length[length]
            }
            source = value[position : position + length]
            span_kind = segmentation.classify(position, position + length)
            if span_kind is None:
                continue
            absolute_start = context_offset + position
            absolute_end = absolute_start + length
            window = context_text[
                max(0, absolute_start - 48) : min(len(context_text), absolute_end + 48)
            ]
            has_context = any(
                marker in window for marker in _RELAXED_CONTEXT_MARKERS
            )
            if len(strict_targets) == 1:
                canonical = next(iter(strict_targets))
                shares_hanzi = bool(set(source) & set(canonical))
                isolated_run = position == 0 and length == len(value)
                if source == canonical or (
                    span_kind == "singles"
                    and (shares_hanzi or isolated_run or has_context)
                ):
                    replacement = (length, canonical)
                    break
                # A real, segmented word is stronger evidence than an
                # out-of-context personal pronunciation collision.
                continue
            if len(strict_targets) > 1:
                break

            if not has_context:
                continue
            relaxed_targets = {
                matcher.relaxed_by_length[length][signature]
                for signature in _candidate_signatures(
                    syllables[position : position + length],
                    relaxed=True,
                )
                if signature in matcher.relaxed_by_length[length]
            }
            if len(relaxed_targets) == 1:
                replacement = (length, next(iter(relaxed_targets)))
                break
            if len(relaxed_targets) > 1:
                # Different encoded alternatives matched different saved terms.
                # Preserve the recognized wording rather than choosing one.
                break
        if replacement is None:
            output.append(value[position])
            position += 1
            continue
        length, canonical = replacement
        output.append(canonical)
        position += length
    return "".join(output)


def apply_personal_phonetic_corrections(
    text: str,
    *,
    matcher: PersonalPhraseMatcher | None = None,
    romanizer: Callable[[str], Sequence[str] | None] | None = None,
    segmenter: Callable[[str], HanSegmentation | None] | None = None,
) -> str:
    """Canonicalize homophone/nasal variants of explicit personal phrases."""

    active = load_personal_phrase_matcher() if matcher is None else matcher
    if not text or not active.lengths:
        return text
    to_pinyin = _romanize_han_run if romanizer is None else romanizer
    find_words = _segment_han_words if segmenter is None else segmenter
    output: list[str] = []
    cursor = 0
    try:
        for match in _HAN_RUN_RE.finditer(text):
            output.append(text[cursor : match.start()])
            run = match.group(0)
            syllables = to_pinyin(run)
            segmentation = find_words(run)
            if (
                syllables is None
                or len(syllables) != len(run)
                or segmentation is None
            ):
                output.append(run)
            else:
                output.append(
                    _repair_han_run(
                        run,
                        active,
                        syllables,
                        segmentation,
                        text,
                        match.start(),
                    )
                )
            cursor = match.end()
    except Exception:
        # Recognition must never fail because optional personalization failed.
        return text
    output.append(text[cursor:])
    return "".join(output)
