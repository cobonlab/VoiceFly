"""Canonical numeric normalization for VoiceFlypy dictation.

This module is the only owner of spoken-number parsing.  The production
WeText mapping policy and the command-line compatibility helpers share these
same token definitions and integer conversion rules.
"""

import re
from dataclasses import dataclass
from functools import lru_cache


POLICY_VERSION = "4.0.0"


@dataclass(frozen=True)
class NumericNormalizationResult:
    text: str
    pending_from: int | None = None

SPOKEN_DIGIT_TRANSLATION = str.maketrans({
    "零": "0", "〇": "0", "一": "1", "二": "2", "两": "2",
    "三": "3", "四": "4", "五": "5", "六": "6", "七": "7",
    "八": "8", "九": "9", "幺": "1",
})
_DIGIT_VALUES = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3,
    "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_NUMBER_CHARS = "零〇一二两三四五六七八九十百千万亿"
_NUMBER_OR_ASCII_CHARS = _NUMBER_CHARS + "0-9"
_DIRECT_DIGIT_GROUP = r"[零〇一二两三四五六七八九幺]+"
_DIRECT_NUMBER_RE = re.compile(rf"{_DIRECT_DIGIT_GROUP}(?:点{_DIRECT_DIGIT_GROUP})*")
_NUMERIC_OUTPUT_RE = re.compile(r"[-+]?\d+(?:[ .:/-]\d+)*")
_CARDINAL_LEFT_CONTEXT_RE = re.compile(
    r"(?:IP|ip|版本|型号|编号|序号|代码|端口号?|房间号?|电话|手机|座机|"
    r"号码|单号|订单号|验证码|密码|账号|账户|数值|数量|总计|合计|第)"
    r"(?:是|为)?$"
)
_SINGLE_LATIN_LABEL_LEFT_CONTEXT_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]\s*$")
_CARDINAL_RIGHT_CONTEXT_RE = re.compile(
    r"^(?:号|房|室|章|节|页|集|期|年|月|日|点|时|分|秒|元|块|个|次|"
    r"层|米|克|公里|参数|端口|亿|万|千|百)"
)
_SAFE_MEASURE_MARKER_RE = re.compile(
    r"(?:百分|千分|万分之|摄氏度|华氏度|平方|立方|公里|千米|厘米|"
    r"毫米|微米|纳米|海里|公斤|千克|毫克|克|吨|毫升|升|分钟|小时|"
    r"毫秒|秒钟|伏特|安培|瓦特|赫兹|比特|字节|度|斤|米|秒|年|月|"
    r"日|天|人|个|次|层|页|章|节|集|期|号|股|手|倍|岁|份|名|台|"
    r"件|条|本|家|辆|块|毛|摩尔)"
)
_EXPLICIT_MATH_RE = re.compile(r"(?:加|减|乘|除|等于|负|正|开方|平方|立方|到|至)")
_TIME_QUALIFIER_RE = re.compile(r"^(?:凌晨|早上|上午|中午|下午|傍晚|晚上|夜里)")
_TIME_LEFT_CONTEXT_RE = re.compile(
    r"(?:凌晨|早上|上午|中午|下午|傍晚|晚上|夜里|时间|时刻|几点|"
    r"定在|安排在|从|到|于|约|大约|预计|计划)(?:是|为)?$"
)
_TIME_RIGHT_CONTEXT_RE = re.compile(
    r"^(?:开会|开始|结束|出发|到达|登机|下飞机|起床|睡觉|去|来|"
    r"左右|之前|之后|以前|以后)"
)
_ITN_ALWAYS_ACCEPTED_TYPES = frozenset({
    "char", "date", "fraction", "licenseplate", "money", "trainnumber", "whitelist",
})

_SPACED_NUMBER = rf"[{_NUMBER_CHARS}](?:[ \t]*[{_NUMBER_CHARS}])*"
_ORDINARY_QUANTITY_RE = re.compile(
    rf"(?P<number>{_SPACED_NUMBER})[ \t]*"
    r"(?P<unit>个|次|年|月|日|天|小时|分钟|秒|元|块|人|本|件|条|台|只|"
    r"张|位|岁|米|公里|克|公斤|页|章|节|期|层|遍|回|种|组|份|"
    r"枚|颗|套|辆|家|名|场|局|轮|倍)"
)
_IDENTIFIER_NUMBER_CHARS = _NUMBER_OR_ASCII_CHARS + "幺"
_IDENTIFIER_NUMBER_RE = re.compile(
    r"(?<![A-Za-z0-9])(?P<label>[A-Za-z]{1,8})[ \t]*"
    rf"(?P<number>[{_IDENTIFIER_NUMBER_CHARS}]"
    rf"(?:[ \t]*[{_IDENTIFIER_NUMBER_CHARS}])*|"
    rf"[-+]?\d+(?:[.:/-]\d+)*)"
)
_TRAILING_IDENTIFIER_LABEL_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:[A-Za-z]{1,8})[ \t]*$"
)
_QUANTITY_UNIT_PREFIX_RE = re.compile(
    r"^(?:个百分点|公里|公斤|分钟|小时|美元|人民币|毫秒|版本|"
    r"个|次|天|年|月|日|秒|元|块|股|手|倍|岁|米|层|页|章|集|"
    r"期|号|人|本|件|条|台|只|张|位|克|遍|回|种|组|份|枚|"
    r"颗|套|辆|家|名|场|局|轮)"
)
_OPEN_ENDED_ITN_TYPES = frozenset({
    "cardinal", "date", "fraction", "licenseplate", "math", "measure",
    "money", "time", "trainnumber",
})

_VERSION_DIGIT_RUN = r"[零〇一二两三四五六七八九0-9]+"
_CHINESE_NUMBER_RUN = rf"[{_NUMBER_OR_ASCII_CHARS}]+"
_SPOKEN_LETTER_VERSION_RE = re.compile(
    rf"(?<![A-Za-z0-9])([A-Za-z])\s+"
    rf"({_VERSION_DIGIT_RUN}(?:\s*(?:点|[.．])\s*{_VERSION_DIGIT_RUN})+)"
    rf"(?![A-Za-z0-9零〇一二两三四五六七八九])"
)
_SPOKEN_LETTER_INTEGER_RE = re.compile(
    rf"(?<![A-Za-z0-9])([A-Za-z])\s+({_CHINESE_NUMBER_RUN})"
    rf"(?![A-Za-z0-9零〇一二两三四五六七八九十百千万亿])"
)
_SPOKEN_ORDINAL_RE = re.compile(rf"第\s*({_CHINESE_NUMBER_RUN})")
_SPOKEN_QUANTITY_RE = re.compile(
    rf"({_CHINESE_NUMBER_RUN})\s*"
    r"(个百分点|公里|公斤|分钟|小时|美元|人民币|毫秒|版本|个|次|天|年|月|日|"
    r"秒|元|块|股|手|倍|岁|米|层|页|章|集|期|号)"
)
_STANDALONE_SPOKEN_INTEGER_RE = re.compile(rf"(?:(?<=\s)|^)({_CHINESE_NUMBER_RUN})(?=\s|$)")
_SPOKEN_DIGIT_SEQUENCE_RE = re.compile(r"[零〇一二两三四五六七八九]{2,}")
_SINGLE_SPOKEN_NON_ONE_DIGIT_RE = re.compile(r"[零〇二两三四五六七八九]")
_MULTIPOINT_NUMBER_RE = re.compile(
    rf"(?<![A-Za-z0-9零〇一二两三四五六七八九十百千万亿])"
    rf"({_CHINESE_NUMBER_RUN}(?:\s*(?:点|[.．])\s*{_VERSION_DIGIT_RUN}){{2,}})"
    rf"(?![A-Za-z0-9零〇一二两三四五六七八九十百千万亿])"
)
_STANDALONE_DECIMAL_RE = re.compile(
    rf"(?:(?<=\s)|^|(?<=[，。！？,!?]))"
    rf"({_CHINESE_NUMBER_RUN}\s*(?:点|[.．])\s*{_VERSION_DIGIT_RUN})"
    rf"(?=\s|$|[，。！？,!?])"
)


def chinese_integer_to_arabic(value):
    value = re.sub(r"[ \t]+", "", value)
    if value.isascii() and value.isdigit():
        return value
    if not any(char in "十百千万亿" for char in value):
        return "".join(str(_DIGIT_VALUES.get(char, char)) for char in value)
    total = section = number = 0
    for char in value:
        if char.isdigit():
            number = number * 10 + int(char)
        elif char in _DIGIT_VALUES:
            number = _DIGIT_VALUES[char]
        elif char in "十百千":
            section += (number or 1) * {"十": 10, "百": 100, "千": 1000}[char]
            number = 0
        elif char in "万亿":
            section += number
            total += section * {"万": 10_000, "亿": 100_000_000}[char]
            section = number = 0
        else:
            return value
    return str(total + section + number)


@lru_cache(maxsize=8)
def _exception_patterns(exceptions):
    return tuple(
        re.compile(r"[ \t]*".join(re.escape(char) for char in phrase))
        for phrase in exceptions
        if phrase
    )


def _exception_ranges(text, exceptions):
    ranges = []
    for pattern in _exception_patterns(tuple(exceptions)):
        ranges.extend((match.start(), match.end()) for match in pattern.finditer(text))
    return tuple(ranges)


def _overlaps(start, end, ranges):
    return any(start < right and end > left for left, right in ranges)


def _direct_cardinal_replacement(raw, left_context, right_context, whole_span):
    compact = re.sub(r"\s+", "", raw)
    if not _DIRECT_NUMBER_RE.fullmatch(compact):
        return None
    groups = compact.split("点")
    digit_count = sum(len(group) for group in groups)
    labelled = bool(
        _CARDINAL_LEFT_CONTEXT_RE.search(left_context)
        or _CARDINAL_RIGHT_CONTEXT_RE.search(right_context)
    )
    if len(groups) > 1:
        if not (whole_span or labelled or digit_count >= 3):
            return None
        return ".".join(group.translate(SPOKEN_DIGIT_TRANSLATION) for group in groups)
    if digit_count >= 7 or labelled or whole_span:
        return compact.translate(SPOKEN_DIGIT_TRANSLATION)
    return None


def _approved_mapping(source, mapping, exception_ranges, *, is_final=True):
    start, end = mapping.input_start, mapping.input_end
    raw = source[start:end]
    replacement = mapping.output_text
    token_type = mapping.token_type
    if _overlaps(start, end, exception_ranges) or not isinstance(replacement, str):
        return raw
    if mapping.kind == "delete":
        return replacement if token_type == "char" else raw
    if not replacement:
        return raw
    if token_type == "money":
        if re.match(r"^(?:一|二|多)?次(?:方程|函数|多项式)", source[end:]):
            return raw
        return replacement
    if token_type in _ITN_ALWAYS_ACCEPTED_TYPES:
        return replacement

    left_context = source[max(0, start - 12):start]
    right_context = source[end:end + 8]
    whole_span = (
        is_final
        and not source[:start].strip()
        and not source[end:].strip()
    )
    if token_type == "measure":
        if not _SAFE_MEASURE_MARKER_RE.search(raw):
            return raw
        if raw.endswith("分") and not raw.endswith("分钟"):
            if not re.search(r"(?:得分|评分|分数|比分|满分|扣分)$", left_context):
                return raw
        return replacement
    if token_type == "time":
        if (
            whole_span
            or _TIME_QUALIFIER_RE.search(raw)
            or _TIME_LEFT_CONTEXT_RE.search(left_context)
            or _TIME_RIGHT_CONTEXT_RE.search(right_context)
        ):
            return replacement
        return raw
    if token_type == "math":
        return replacement if _EXPLICIT_MATH_RE.search(raw) else raw
    if token_type != "cardinal":
        return raw
    direct = _direct_cardinal_replacement(raw, left_context, right_context, whole_span)
    if direct is not None:
        return direct
    if not _NUMERIC_OUTPUT_RE.fullmatch(replacement):
        return raw
    if whole_span or _CARDINAL_LEFT_CONTEXT_RE.search(left_context):
        return replacement
    if _SINGLE_LATIN_LABEL_LEFT_CONTEXT_RE.search(left_context):
        return replacement
    if _CARDINAL_RIGHT_CONTEXT_RE.search(right_context):
        return replacement
    return raw


def _apply_itn_mappings(source, result, exceptions=(), *, is_final):
    mappings = tuple(result.mappings)
    exception_ranges = _exception_ranges(source, exceptions)
    parts = []
    cursor = 0
    pending_from = None
    for mapping in mappings:
        start, end = mapping.input_start, mapping.input_end
        if (
            not isinstance(start, int) or not isinstance(end, int)
            or start < cursor or end <= start or end > len(source)
            or mapping.input_text != source[start:end]
        ):
            raise ValueError("invalid ITN alignment")
        parts.append(source[cursor:start])
        raw = source[start:end]
        replacement = _approved_mapping(
            source,
            mapping,
            exception_ranges,
            is_final=is_final,
        )
        if (
            not is_final
            and end == len(source)
            and mapping.token_type in _OPEN_ENDED_ITN_TYPES
            and _approved_mapping(
                source,
                mapping,
                exception_ranges,
                is_final=True,
            ) != raw
        ):
            replacement = raw
            prefix = "".join(parts)
            label = _TRAILING_IDENTIFIER_LABEL_RE.search(prefix)
            pending_from = label.start() if label else len(prefix)
        parts.append(replacement)
        cursor = end
    parts.append(source[cursor:])
    return NumericNormalizationResult("".join(parts), pending_from)


def apply_itn_mappings(source, result, exceptions=()):
    return _apply_itn_mappings(
        source,
        result,
        exceptions,
        is_final=True,
    ).text


def _identifier_number_value(raw):
    compact = re.sub(r"[ \t]+", "", raw)
    if re.fullmatch(r"[-+]?\d+(?:[.:/-]\d+)*", compact):
        return compact
    if any(char in "十百千万亿" for char in compact):
        return chinese_integer_to_arabic(compact)
    return compact.translate(SPOKEN_DIGIT_TRANSLATION)


def _starts_with_han(text):
    return bool(text) and bool(re.match(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]", text))


def _is_identifier_number(label, raw_number, right_context):
    visible_right = right_context.lstrip(" \t")
    if _QUANTITY_UNIT_PREFIX_RE.match(visible_right):
        return False
    if len(label) == 1:
        return True
    if not label.isupper():
        return False
    compact_number = re.sub(r"[ \t]+", "", raw_number)
    return (
        bool(re.search(r"\d", compact_number))
        or "幺" in compact_number
        or _starts_with_han(visible_right)
    )


def _normalize_identifier_numbers(text, exception_ranges, *, is_final):
    parts = []
    cursor = 0
    pending_from = None
    for match in _IDENTIFIER_NUMBER_RE.finditer(text):
        if _overlaps(match.start(), match.end(), exception_ranges):
            continue
        label = match.group("label")
        raw_number = match.group("number")
        right_context = text[match.end():]
        if not _is_identifier_number(label, raw_number, right_context):
            continue
        parts.append(text[cursor:match.start()])
        replacement_start = sum(len(part) for part in parts)
        has_spoken_digits = bool(
            re.search(r"[零〇一二两三四五六七八九幺十百千万亿]", raw_number)
        )
        if not is_final and match.end() == len(text) and has_spoken_digits:
            parts.append(match.group(0))
            pending_from = replacement_start
        else:
            parts.append(label + _identifier_number_value(raw_number))
        cursor = match.end()
    parts.append(text[cursor:])
    return NumericNormalizationResult("".join(parts), pending_from)


def _normalize_quantities(text, exceptions):
    exception_ranges = _exception_ranges(text, exceptions)

    def replace_quantity(match):
        if _overlaps(match.start(), match.end(), exception_ranges):
            return match.group(0)
        suffix = text[match.end():]
        unit = match.group("unit")
        if unit == "元" and re.match(r"(?:一|二|多)?次(?:方程|函数|多项式)", suffix):
            return match.group(0)
        if unit == "次" and re.match(r"(?:方程|函数|多项式)", suffix):
            return match.group(0)
        return chinese_integer_to_arabic(match.group("number")) + unit

    return _ORDINARY_QUANTITY_RE.sub(replace_quantity, text)


def _normalize_contextual_numbers(text, exceptions=(), *, is_final, protected_from=None):
    """Normalize complete numeric tokens and preserve an open streaming tail."""
    if protected_from is not None:
        prefix = _normalize_contextual_numbers(
            text[:protected_from],
            exceptions,
            is_final=False,
        )
        return NumericNormalizationResult(
            prefix.text + text[protected_from:],
            prefix.pending_from if prefix.pending_from is not None else len(prefix.text),
        )

    exception_ranges = _exception_ranges(text, exceptions)
    identifiers = _normalize_identifier_numbers(
        text,
        exception_ranges,
        is_final=is_final,
    )
    if identifiers.pending_from is None:
        return NumericNormalizationResult(
            _normalize_quantities(identifiers.text, exceptions)
        )
    prefix = _normalize_quantities(
        identifiers.text[:identifiers.pending_from],
        exceptions,
    )
    return NumericNormalizationResult(
        prefix + identifiers.text[identifiers.pending_from:],
        len(prefix),
    )


def normalize_contextual_numbers(text, exceptions=()):
    """Normalize quantities and identifiers with final-utterance semantics."""
    return _normalize_contextual_numbers(
        text,
        exceptions,
        is_final=True,
    ).text


def normalize_mapped_numbers(source, result, exceptions=()):
    """Run the single production numeric pipeline exactly once."""
    mapped = _apply_itn_mappings(
        source,
        result,
        exceptions,
        is_final=True,
    )
    return _normalize_contextual_numbers(
        mapped.text,
        exceptions,
        is_final=True,
    ).text


def normalize_mapped_numbers_partial(source, result, exceptions=()):
    """Normalize a streaming hypothesis without treating its tail as final."""
    mapped = _apply_itn_mappings(
        source,
        result,
        exceptions,
        is_final=False,
    )
    return _normalize_contextual_numbers(
        mapped.text,
        exceptions,
        is_final=False,
        protected_from=mapped.pending_from,
    )


def normalize_spoken_versions(text):
    def replace(match):
        version = re.sub(r"\s+", "", match.group(2))
        version = version.replace("点", ".").replace("．", ".")
        return match.group(1) + version.translate(SPOKEN_DIGIT_TRANSLATION)
    return _SPOKEN_LETTER_VERSION_RE.sub(replace, text)


def normalize_spoken_numeric_sequences(text):
    def replace(match):
        parts = re.split(r"\s*(?:点|[.．])\s*", match.group(1))
        return ".".join(
            [chinese_integer_to_arabic(parts[0])]
            + [part.translate(SPOKEN_DIGIT_TRANSLATION) for part in parts[1:]]
        )
    text = _MULTIPOINT_NUMBER_RE.sub(replace, text)
    return _STANDALONE_DECIMAL_RE.sub(replace, text)


def normalize_spoken_numbers(text, exceptions=()):
    protected = {}
    for index, phrase in enumerate(exceptions):
        if phrase not in text:
            continue
        marker = f"\ue000{index}\ue001"
        text = text.replace(phrase, marker)
        protected[marker] = phrase
    text = _SPOKEN_LETTER_INTEGER_RE.sub(
        lambda match: match.group(1) + chinese_integer_to_arabic(match.group(2)), text
    )
    text = _SPOKEN_ORDINAL_RE.sub(
        lambda match: "第" + chinese_integer_to_arabic(match.group(1)), text
    )
    text = _SPOKEN_QUANTITY_RE.sub(
        lambda match: chinese_integer_to_arabic(match.group(1)) + match.group(2), text
    )
    text = _STANDALONE_SPOKEN_INTEGER_RE.sub(
        lambda match: chinese_integer_to_arabic(match.group(1)), text
    )
    text = _SPOKEN_DIGIT_SEQUENCE_RE.sub(
        lambda match: match.group(0).translate(SPOKEN_DIGIT_TRANSLATION), text
    )
    text = _SINGLE_SPOKEN_NON_ONE_DIGIT_RE.sub(
        lambda match: match.group(0).translate(SPOKEN_DIGIT_TRANSLATION), text
    )
    for marker, phrase in protected.items():
        text = text.replace(marker, phrase)
    return text
