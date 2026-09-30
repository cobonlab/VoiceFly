"""Pure VoiceFly v1 protocol validation and anonymous quality aggregation.

The fixed canonical strings are public protocol data.  They are intentionally
kept inside this module so callers cannot silently redefine the benchmark.
No public report contains a sample ID, transcript, truth, path, or per-sample
result.  Manual semantic review and an active-mode dry-run are not inferable
from A/B transcripts and therefore always remain pending here.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Callable


__all__ = (
    "EXPECTED_SAMPLE_COUNT",
    "EXPECTED_REFERENCE_CHARACTERS",
    "MAX_TRANSCRIPT_CHARACTERS",
    "ManifestMetadata",
    "PROTOCOL_CATEGORY_SAMPLE_NUMBERS",
    "PROTOCOL_SCHEMA_SHA256",
    "PROTOCOL_SPLIT_BY_SAMPLE_NUMBER",
    "PROTOCOL_VERSION",
    "PROTOCOL_REFERENCE_CHARACTERS_BY_SAMPLE_NUMBER",
    "ProtocolObservation",
    "ProtocolValidationError",
    "aggregate_report",
    "validate_manifest_metadata",
    "validate_public_report",
)


PROTOCOL_VERSION = "voicefly-semantic-refiner-v1"
EXPECTED_SAMPLE_COUNT = 24
RELATIVE_REDUCTION_MIN_BASELINE_DISTANCE = 10
RELATIVE_REDUCTION_NUMERATOR = 1
RELATIVE_REDUCTION_DENOMINATOR = 5
MAX_TRANSCRIPT_CHARACTERS = 32_768


class ProtocolValidationError(ValueError):
    """A value-only error whose message never includes private input."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True, repr=False)
class ManifestMetadata:
    sample_id: str = field(repr=False)
    split: str
    ground_truth: str = field(repr=False)
    tags: tuple[str, ...]

    def __repr__(self) -> str:
        return "ManifestMetadata(<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class ProtocolObservation:
    sample_id: str = field(repr=False)
    split: str
    ground_truth: str = field(repr=False)
    tags: tuple[str, ...]
    baseline: str | None = field(repr=False)
    candidate: str | None = field(repr=False)

    def __repr__(self) -> str:
        return "ProtocolObservation(<redacted>)"


@dataclass(frozen=True, slots=True)
class _ProtocolSpec:
    sample_id: str
    split: str
    ground_truth: str
    tags: tuple[str, ...]


_SPECS = (
    _ProtocolSpec("C01", "calibration", "W356", ("identifier",)),
    _ProtocolSpec("C02", "calibration", "T41", ("identifier",)),
    _ProtocolSpec("C03", "calibration", "T31 T39", ("identifier",)),
    _ProtocolSpec("C04", "calibration", "我是一个紧凶型的牌手", ("homophone",)),
    _ProtocolSpec(
        "C05",
        "calibration",
        "量化策略需要计算夏普比率和最大回撤",
        ("homophone",),
    ),
    _ProtocolSpec(
        "C06",
        "calibration",
        "OpenAI 的 Codex 可以调用 Python API",
        ("mixed",),
    ),
    _ProtocolSpec(
        "C07",
        "calibration",
        "下铺旁边的袋子里装着一瓶食用油脂",
        ("negative",),
    ),
    _ProtocolSpec(
        "C08",
        "calibration",
        "温度20°C 湿度50%",
        ("technical",),
    ),
    _ProtocolSpec("V01", "validation", "T26", ("identifier",)),
    _ProtocolSpec("V02", "validation", "T34", ("identifier",)),
    _ProtocolSpec("V03", "validation", "W356 T41", ("identifier",)),
    _ProtocolSpec(
        "V04",
        "validation",
        "按钮位采用三倍加注和持续下注",
        ("homophone",),
    ),
    _ProtocolSpec(
        "V05",
        "validation",
        "滚动回测必须避免前视偏差",
        ("homophone",),
    ),
    _ProtocolSpec(
        "V06",
        "validation",
        "在 BTN 位置用 GTO solver 计算 VPIP",
        ("mixed",),
    ),
    _ProtocolSpec(
        "V07",
        "validation",
        "印刷店把封面打版后发给我确认",
        ("negative",),
    ),
    _ProtocolSpec(
        "V08",
        "validation",
        "价格¥12.5 折扣15%",
        ("technical",),
    ),
    _ProtocolSpec("H01", "holdout", "T31 T39 T26 T34", ("identifier",)),
    _ProtocolSpec("H02", "holdout", "R52 K708", ("identifier",)),
    _ProtocolSpec(
        "H03",
        "holdout",
        "电子烟的雾化芯需要通过 TPD 认证",
        ("mixed", "homophone"),
    ),
    _ProtocolSpec(
        "H04",
        "holdout",
        "短线游资今天选择打板",
        ("homophone",),
    ),
    _ProtocolSpec(
        "H05",
        "holdout",
        "用 GPU 跑本地 ASR 用 CPU 做回退",
        ("mixed",),
    ),
    _ProtocolSpec(
        "H06",
        "holdout",
        "设备上的按钮未弹起 请先断电检查",
        ("negative",),
    ),
    _ProtocolSpec("H07", "holdout", "请输入± %和°", ("technical",)),
    _ProtocolSpec("H08", "holdout", "30% 1/3 21~1001", ("technical",)),
)

# The evaluator's aggregate CER denominator is fixed by the canonical v1
# ground truths after the same NFKC/whitespace normalization it applies at
# runtime.  Exposing this derived constant lets the process supervisor reject a
# syntactically valid child report that does not actually describe this corpus.
PROTOCOL_REFERENCE_CHARACTERS_BY_SAMPLE_NUMBER = MappingProxyType(
    {
        sample_number: len(
            "".join(unicodedata.normalize("NFKC", spec.ground_truth).split())
        )
        for sample_number, spec in enumerate(_SPECS, 1)
    }
)
PROTOCOL_SPLIT_BY_SAMPLE_NUMBER = MappingProxyType(
    {
        sample_number: spec.split
        for sample_number, spec in enumerate(_SPECS, 1)
    }
)
EXPECTED_REFERENCE_CHARACTERS = sum(
    PROTOCOL_REFERENCE_CHARACTERS_BY_SAMPLE_NUMBER.values()
)

_SPECS_BY_ID = {spec.sample_id: spec for spec in _SPECS}
_IDENTIFIER_IDS = frozenset({"C01", "C02", "C03", "V01", "V02", "V03", "H01", "H02"})
_TECHNICAL_IDS = frozenset({"C08", "V08", "H07", "H08"})
_POSITIVE_HOMOPHONE_IDS = frozenset({"C04", "C05", "V04", "V05", "H03", "H04"})
_NEGATIVE_CONTROL_IDS = frozenset({"C07", "V07", "H06"})
_ASCII_ENTITY_IDS = frozenset({"C06", "V06", "H03", "H05"})

_SAMPLE_NUMBER_BY_ID = {
    spec.sample_id: sample_number for sample_number, spec in enumerate(_SPECS, 1)
}
PROTOCOL_CATEGORY_SAMPLE_NUMBERS = MappingProxyType(
    {
        "identifiers": tuple(
            _SAMPLE_NUMBER_BY_ID[spec.sample_id]
            for spec in _SPECS
            if spec.sample_id in _IDENTIFIER_IDS
        ),
        "technical": tuple(
            _SAMPLE_NUMBER_BY_ID[spec.sample_id]
            for spec in _SPECS
            if spec.sample_id in _TECHNICAL_IDS
        ),
        "positive_homophones": tuple(
            _SAMPLE_NUMBER_BY_ID[spec.sample_id]
            for spec in _SPECS
            if spec.sample_id in _POSITIVE_HOMOPHONE_IDS
        ),
        "negative_controls": tuple(
            _SAMPLE_NUMBER_BY_ID[spec.sample_id]
            for spec in _SPECS
            if spec.sample_id in _NEGATIVE_CONTROL_IDS
        ),
        "ascii_entities": tuple(
            _SAMPLE_NUMBER_BY_ID[spec.sample_id]
            for spec in _SPECS
            if spec.sample_id in _ASCII_ENTITY_IDS
        ),
    }
)

_TECHNICAL_ATOMS = {
    "C08": ("20°C", "50%"),
    "V08": ("¥12.5", "15%"),
    "H07": ("±", "%", "°"),
    "H08": ("30%", "1/3", "21~1001"),
}

_IDENTIFIER_RE = re.compile(r"[A-Z][0-9]+(?: [A-Z][0-9]+)*\Z")
_ASCII_ENTITY_RE = re.compile(r"[A-Za-z]+")
_TECHNICAL_ATOM_RE = re.compile(
    r"¥[0-9]+(?:\.[0-9]+)?"
    r"|[0-9]+(?:\.[0-9]+)?°C"
    r"|[0-9]+(?:\.[0-9]+)?%"
    r"|[0-9]+/[0-9]+"
    r"|[0-9]+~[0-9]+"
    r"|[0-9]+(?:\.[0-9]+)?"
    r"|[±¥%°/~]"
)
_TECHNICAL_SYMBOLS = frozenset("±¥%°/~")

_SCHEMA_BYTES = json.dumps(
    {
        "version": PROTOCOL_VERSION,
        "samples": [
            {
                "sample_id": spec.sample_id,
                "split": spec.split,
                "ground_truth": spec.ground_truth,
                "tags": spec.tags,
            }
            for spec in _SPECS
        ],
    },
    ensure_ascii=False,
    separators=(",", ":"),
).encode("utf-8")
PROTOCOL_SCHEMA_SHA256 = hashlib.sha256(_SCHEMA_BYTES).hexdigest()


def _normalize_exact(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).strip().split())


def _normalize_for_distance(text: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKC", text)
        if not character.isspace()
    )


def _levenshtein(reference: Sequence[Any], hypothesis: Sequence[Any]) -> int:
    if len(reference) < len(hypothesis):
        reference, hypothesis = hypothesis, reference
    previous = list(range(len(hypothesis) + 1))
    for row, reference_item in enumerate(reference, 1):
        current = [row]
        for column, hypothesis_item in enumerate(hypothesis, 1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[column] + 1,
                    previous[column - 1] + (reference_item != hypothesis_item),
                )
            )
        previous = current
    return previous[-1]


def _coerce_tags(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ProtocolValidationError("tags_type_invalid")
    if not all(isinstance(tag, str) and tag for tag in value):
        raise ProtocolValidationError("tags_type_invalid")
    tags = tuple(value)
    if len(tags) != len(set(tags)):
        raise ProtocolValidationError("tags_duplicate")
    return tags


def _validate_metadata(metadata: ManifestMetadata) -> _ProtocolSpec:
    if not isinstance(metadata.sample_id, str):
        raise ProtocolValidationError("sample_id_type_invalid")
    spec = _SPECS_BY_ID.get(metadata.sample_id)
    if spec is None:
        raise ProtocolValidationError("protocol_sample_extra")
    if not isinstance(metadata.split, str) or metadata.split != spec.split:
        raise ProtocolValidationError("split_mismatch")
    if (
        not isinstance(metadata.ground_truth, str)
        or metadata.ground_truth != spec.ground_truth
    ):
        raise ProtocolValidationError("ground_truth_mismatch")
    tags = _coerce_tags(metadata.tags)
    if len(tags) != len(spec.tags) or set(tags) != set(spec.tags):
        raise ProtocolValidationError("tags_mismatch")
    return spec


def _metadata_from_record(record: object) -> ManifestMetadata:
    if isinstance(record, ManifestMetadata):
        metadata = ManifestMetadata(
            record.sample_id,
            record.split,
            record.ground_truth,
            _coerce_tags(record.tags),
        )
    else:
        if not isinstance(record, Mapping):
            raise ProtocolValidationError("manifest_record_type_invalid")
        required = {"sample_id", "split", "ground_truth", "tags"}
        if set(record) != required:
            raise ProtocolValidationError("manifest_record_fields_invalid")
        metadata = ManifestMetadata(
            sample_id=record["sample_id"],
            split=record["split"],
            ground_truth=record["ground_truth"],
            tags=_coerce_tags(record["tags"]),
        )
    _validate_metadata(metadata)
    return metadata


def _observation_from_record(record: object) -> ProtocolObservation:
    if isinstance(record, ProtocolObservation):
        observation = ProtocolObservation(
            record.sample_id,
            record.split,
            record.ground_truth,
            _coerce_tags(record.tags),
            record.baseline,
            record.candidate,
        )
    elif isinstance(record, Mapping):
        required = {
            "sample_id",
            "split",
            "ground_truth",
            "tags",
            "baseline",
            "candidate",
        }
        if set(record) != required:
            raise ProtocolValidationError("observation_record_fields_invalid")
        observation = ProtocolObservation(
            sample_id=record["sample_id"],
            split=record["split"],
            ground_truth=record["ground_truth"],
            tags=_coerce_tags(record["tags"]),
            baseline=record["baseline"],
            candidate=record["candidate"],
        )
    else:
        raise ProtocolValidationError("observation_record_type_invalid")
    _validate_metadata(
        ManifestMetadata(
            observation.sample_id,
            observation.split,
            observation.ground_truth,
            observation.tags,
        )
    )
    for value, code in (
        (observation.baseline, "baseline_transcript_invalid"),
        (observation.candidate, "candidate_transcript_invalid"),
    ):
        if value is not None and (
            not isinstance(value, str)
            or len(value) > MAX_TRANSCRIPT_CHARACTERS
            or "\x00" in value
        ):
            raise ProtocolValidationError(code)
    return observation


def _ordered_unique(
    records: Iterable[object],
    converter: Callable[[object], Any],
) -> tuple[Any, ...]:
    try:
        iterator = iter(records)
    except TypeError as exc:
        raise ProtocolValidationError("records_not_iterable") from exc
    by_id: dict[str, Any] = {}
    for record in iterator:
        converted = converter(record)
        sample_id = converted.sample_id
        if sample_id in by_id:
            raise ProtocolValidationError("duplicate_sample_id")
        by_id[sample_id] = converted
        if len(by_id) > EXPECTED_SAMPLE_COUNT:
            raise ProtocolValidationError("protocol_sample_extra")
    if len(by_id) != EXPECTED_SAMPLE_COUNT:
        raise ProtocolValidationError("protocol_sample_missing")
    return tuple(by_id[spec.sample_id] for spec in _SPECS)


def validate_manifest_metadata(
    records: Iterable[ManifestMetadata | Mapping[str, object]],
) -> tuple[ManifestMetadata, ...]:
    """Validate exactly the fixed 24 manifest records and return canonical order."""
    ordered = _ordered_unique(records, _metadata_from_record)
    for metadata in ordered:
        _validate_metadata(metadata)
    return ordered


def _validate_observations(
    records: Iterable[ProtocolObservation | Mapping[str, object]],
) -> tuple[ProtocolObservation, ...]:
    return _ordered_unique(records, _observation_from_record)


def _transcript(observation: ProtocolObservation, model: str) -> str | None:
    return observation.baseline if model == "baseline" else observation.candidate


def _is_exact(value: str | None, truth: str) -> bool:
    return value is not None and _normalize_exact(value) == _normalize_exact(truth)


def _identifier_signature(value: str | None) -> tuple[str, ...]:
    if value is None:
        return ()
    normalized = _normalize_exact(value)
    if not _IDENTIFIER_RE.fullmatch(normalized):
        return ()
    return tuple(normalized.split(" "))


def _ascii_signature(value: str | None) -> tuple[str, ...]:
    if value is None:
        return ()
    return tuple(_ASCII_ENTITY_RE.findall(unicodedata.normalize("NFKC", value)))


def _technical_atom_signature(value: str | None) -> tuple[str, ...]:
    if value is None:
        return ()
    return tuple(_TECHNICAL_ATOM_RE.findall(unicodedata.normalize("NFKC", value)))


def _technical_symbol_signature(value: str | None) -> tuple[str, ...]:
    if value is None:
        return ()
    return tuple(
        character
        for character in unicodedata.normalize("NFKC", value)
        if character in _TECHNICAL_SYMBOLS
    )


def _strict_positional_correct(
    expected: tuple[str, ...],
    actual: tuple[str, ...],
) -> int:
    if len(expected) != len(actual):
        return 0
    return sum(left == right for left, right in zip(expected, actual))


def _model_pair(
    metric_function: Callable[[str], dict[str, int]],
) -> dict[str, dict[str, int]]:
    return {
        "baseline": metric_function("baseline"),
        "candidate": metric_function("candidate"),
    }


def _identifier_summary(
    observations: Mapping[str, ProtocolObservation],
) -> dict[str, object]:
    specs = tuple(spec for spec in _SPECS if spec.sample_id in _IDENTIFIER_IDS)
    expected_tokens = {
        spec.sample_id: _identifier_signature(spec.ground_truth) for spec in specs
    }
    token_total = sum(len(tokens) for tokens in expected_tokens.values())

    def metrics(model: str) -> dict[str, int]:
        clip_exact = 0
        token_exact = 0
        for spec in specs:
            value = _transcript(observations[spec.sample_id], model)
            clip_exact += _is_exact(value, spec.ground_truth)
            token_exact += _strict_positional_correct(
                expected_tokens[spec.sample_id],
                _identifier_signature(value),
            )
        return {"clip_exact": clip_exact, "token_exact": token_exact}

    results = _model_pair(metrics)
    candidate = results["candidate"]
    status = (
        "pass"
        if candidate["clip_exact"] == len(specs)
        and candidate["token_exact"] == token_total
        else "fail"
    )
    return {
        "expected_clips": len(specs),
        "expected_tokens": token_total,
        **results,
        "gate_status": status,
    }


def _technical_summary(
    observations: Mapping[str, ProtocolObservation],
) -> dict[str, object]:
    specs = tuple(spec for spec in _SPECS if spec.sample_id in _TECHNICAL_IDS)
    atom_total = sum(len(_TECHNICAL_ATOMS[spec.sample_id]) for spec in specs)
    symbol_total = sum(
        len(_technical_symbol_signature(spec.ground_truth)) for spec in specs
    )

    def metrics(model: str) -> dict[str, int]:
        clip_exact = 0
        atom_exact = 0
        atom_sequence_exact = 0
        symbol_sequence_exact = 0
        symbol_edit_distance = 0
        for spec in specs:
            value = _transcript(observations[spec.sample_id], model)
            expected_atoms = _TECHNICAL_ATOMS[spec.sample_id]
            actual_atoms = _technical_atom_signature(value)
            expected_symbols = _technical_symbol_signature(spec.ground_truth)
            actual_symbols = _technical_symbol_signature(value)
            clip_exact += _is_exact(value, spec.ground_truth)
            atom_exact += _strict_positional_correct(expected_atoms, actual_atoms)
            atom_sequence_exact += expected_atoms == actual_atoms
            symbol_sequence_exact += expected_symbols == actual_symbols
            symbol_edit_distance += _levenshtein(expected_symbols, actual_symbols)
        return {
            "clip_exact": clip_exact,
            "atom_exact": atom_exact,
            "atom_sequence_exact": atom_sequence_exact,
            "symbol_sequence_exact": symbol_sequence_exact,
            "symbol_edit_distance": symbol_edit_distance,
        }

    results = _model_pair(metrics)
    candidate = results["candidate"]
    status = (
        "pass"
        if candidate["clip_exact"] == len(specs)
        and candidate["atom_exact"] == atom_total
        and candidate["atom_sequence_exact"] == len(specs)
        and candidate["symbol_sequence_exact"] == len(specs)
        and candidate["symbol_edit_distance"] == 0
        else "fail"
    )
    return {
        "expected_clips": len(specs),
        "expected_atoms": atom_total,
        "expected_symbols": symbol_total,
        **results,
        "gate_status": status,
    }


def _exact_category_summary(
    observations: Mapping[str, ProtocolObservation],
    sample_ids: frozenset[str],
    required_correct: int,
) -> dict[str, object]:
    specs = tuple(spec for spec in _SPECS if spec.sample_id in sample_ids)

    def metrics(model: str) -> dict[str, int]:
        return {
            "clip_exact": sum(
                _is_exact(
                    _transcript(observations[spec.sample_id], model),
                    spec.ground_truth,
                )
                for spec in specs
            )
        }

    results = _model_pair(metrics)
    return {
        "expected_clips": len(specs),
        "required_correct": required_correct,
        **results,
        "gate_status": (
            "pass"
            if results["candidate"]["clip_exact"] >= required_correct
            else "fail"
        ),
    }


def _ascii_summary(
    observations: Mapping[str, ProtocolObservation],
) -> dict[str, object]:
    specs = tuple(spec for spec in _SPECS if spec.sample_id in _ASCII_ENTITY_IDS)
    expected = {
        spec.sample_id: _ascii_signature(spec.ground_truth) for spec in specs
    }
    entity_total = sum(len(entities) for entities in expected.values())

    def metrics(model: str) -> dict[str, int]:
        sequence_exact = 0
        entity_exact = 0
        for spec in specs:
            actual = _ascii_signature(
                _transcript(observations[spec.sample_id], model)
            )
            wanted = expected[spec.sample_id]
            sequence_exact += actual == wanted
            entity_exact += _strict_positional_correct(wanted, actual)
        return {
            "sequence_exact": sequence_exact,
            "entity_exact": entity_exact,
        }

    results = _model_pair(metrics)
    candidate = results["candidate"]
    return {
        "expected_clips": len(specs),
        "expected_entities": entity_total,
        **results,
        "gate_status": (
            "pass"
            if candidate["sequence_exact"] == len(specs)
            and candidate["entity_exact"] == entity_total
            else "fail"
        ),
    }


def _split_comparison(
    observations: Mapping[str, ProtocolObservation],
    split: str,
) -> dict[str, object]:
    specs = tuple(spec for spec in _SPECS if spec.split == split)
    if any(
        observations[spec.sample_id].baseline is None
        or observations[spec.sample_id].candidate is None
        for spec in specs
    ):
        return {
            "sample_count": len(specs),
            "baseline_edit_distance": None,
            "candidate_edit_distance": None,
            "relative_reduction": None,
            "non_regression_status": "not_measured",
            "relative_reduction_status": "not_measured",
            "gate_status": "not_measured",
        }

    baseline_distance = 0
    candidate_distance = 0
    for spec in specs:
        reference = _normalize_for_distance(spec.ground_truth)
        baseline = _normalize_for_distance(observations[spec.sample_id].baseline or "")
        candidate = _normalize_for_distance(observations[spec.sample_id].candidate or "")
        baseline_distance += _levenshtein(reference, baseline)
        candidate_distance += _levenshtein(reference, candidate)

    non_regression_status = (
        "pass" if candidate_distance <= baseline_distance else "fail"
    )
    if baseline_distance < RELATIVE_REDUCTION_MIN_BASELINE_DISTANCE:
        relative_reduction = None
        relative_reduction_status = "inconclusive"
    else:
        relative_reduction = round(
            (baseline_distance - candidate_distance) / baseline_distance,
            6,
        )
        relative_reduction_status = (
            "pass"
            if candidate_distance * RELATIVE_REDUCTION_DENOMINATOR
            <= baseline_distance
            * (RELATIVE_REDUCTION_DENOMINATOR - RELATIVE_REDUCTION_NUMERATOR)
            else "fail"
        )

    if non_regression_status == "fail" or relative_reduction_status == "fail":
        gate_status = "fail"
    elif relative_reduction_status == "inconclusive":
        gate_status = "inconclusive"
    else:
        gate_status = "pass"
    return {
        "sample_count": len(specs),
        "baseline_edit_distance": baseline_distance,
        "candidate_edit_distance": candidate_distance,
        "relative_reduction": relative_reduction,
        "non_regression_status": non_regression_status,
        "relative_reduction_status": relative_reduction_status,
        "gate_status": gate_status,
    }


def _combined_quality_status(
    completion_status: str,
    categories: Mapping[str, Mapping[str, object]],
    splits: Mapping[str, Mapping[str, object]],
) -> str:
    quality_statuses = [completion_status]
    quality_statuses.extend(
        str(category["gate_status"]) for category in categories.values()
    )
    quality_statuses.extend(
        str(split["gate_status"]) for split in splits.values()
    )
    if any(status in {"fail", "not_measured"} for status in quality_statuses):
        return "fail"
    if "inconclusive" in quality_statuses:
        return "inconclusive"
    return "pass"


def aggregate_report(
    records: Iterable[ProtocolObservation | Mapping[str, object]],
) -> dict[str, object]:
    """Return fixed-category metrics with no private or per-sample content."""
    ordered = _validate_observations(records)
    observations = {item.sample_id: item for item in ordered}
    baseline_success = sum(item.baseline is not None for item in ordered)
    candidate_success = sum(item.candidate is not None for item in ordered)
    completion_status = (
        "pass"
        if baseline_success == EXPECTED_SAMPLE_COUNT
        and candidate_success == EXPECTED_SAMPLE_COUNT
        else "fail"
    )

    categories = {
        "identifiers": _identifier_summary(observations),
        "technical": _technical_summary(observations),
        "positive_homophones": _exact_category_summary(
            observations,
            _POSITIVE_HOMOPHONE_IDS,
            5,
        ),
        "negative_controls": _exact_category_summary(
            observations,
            _NEGATIVE_CONTROL_IDS,
            3,
        ),
        "ascii_entities": _ascii_summary(observations),
    }
    splits = {
        "validation": _split_comparison(observations, "validation"),
        "holdout": _split_comparison(observations, "holdout"),
    }

    quality_status = _combined_quality_status(
        completion_status,
        categories,
        splits,
    )

    return {
        "protocol": {
            "version": PROTOCOL_VERSION,
            "schema_sha256": PROTOCOL_SCHEMA_SHA256,
            "expected_sample_count": EXPECTED_SAMPLE_COUNT,
        },
        "scope": "quality_only",
        "provider_completion": {
            "expected_per_provider": EXPECTED_SAMPLE_COUNT,
            "baseline_successful": baseline_success,
            "baseline_failed": EXPECTED_SAMPLE_COUNT - baseline_success,
            "candidate_successful": candidate_success,
            "candidate_failed": EXPECTED_SAMPLE_COUNT - candidate_success,
            "gate_status": completion_status,
        },
        "categories": categories,
        "split_edit_distance": splits,
        "gates": {
            "automated_quality": {"status": quality_status},
            "performance_resources": {
                "status": "pending",
                "measurement": "not_measured",
            },
            "natural_chinese_manual_review": {
                "status": "pending",
                "measurement": "not_measured",
                "required_semantic_regressions": 0,
                "observed_semantic_regressions": None,
            },
            "active_dry_run": {
                "status": "pending",
                "measurement": "not_measured",
                "expected_clips": EXPECTED_SAMPLE_COUNT,
                "required_changed_commits": 0,
                "observed_changed_commits": None,
            },
            "shadow_readiness": {
                "status": (
                    "fail"
                    if quality_status == "fail"
                    else "inconclusive"
                    if quality_status == "inconclusive"
                    else "pending"
                )
            },
        },
    }


def _public_object(
    value: object,
    expected_fields: frozenset[str],
) -> dict[str, Any]:
    if type(value) is not dict:
        raise ProtocolValidationError("public_report_type_invalid")
    if set(value) != expected_fields:
        raise ProtocolValidationError("public_report_fields_invalid")
    return value


def _public_int(value: object, *, maximum: int) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ProtocolValidationError("public_report_count_invalid")
    return value


def _public_status(value: object, allowed: frozenset[str]) -> str:
    if type(value) is not str or value not in allowed:
        raise ProtocolValidationError("public_report_status_invalid")
    return value


def _public_model_metrics(
    value: object,
    limits: Mapping[str, int],
) -> dict[str, int]:
    metrics = _public_object(value, frozenset(limits))
    return {
        name: _public_int(metrics[name], maximum=maximum)
        for name, maximum in limits.items()
    }


def _validate_identifier_public(value: object) -> dict[str, object]:
    category = _public_object(
        value,
        frozenset(
            {
                "expected_clips",
                "expected_tokens",
                "baseline",
                "candidate",
                "gate_status",
            }
        ),
    )
    if (
        _public_int(category["expected_clips"], maximum=8) != 8
        or _public_int(category["expected_tokens"], maximum=14) != 14
    ):
        raise ProtocolValidationError("public_report_count_invalid")
    limits = {"clip_exact": 8, "token_exact": 14}
    _public_model_metrics(category["baseline"], limits)
    candidate = _public_model_metrics(category["candidate"], limits)
    status = _public_status(category["gate_status"], frozenset({"pass", "fail"}))
    expected = (
        "pass"
        if candidate["clip_exact"] == 8 and candidate["token_exact"] == 14
        else "fail"
    )
    if status != expected:
        raise ProtocolValidationError("public_report_consistency_invalid")
    return category


def _validate_technical_public(value: object) -> dict[str, object]:
    category = _public_object(
        value,
        frozenset(
            {
                "expected_clips",
                "expected_atoms",
                "expected_symbols",
                "baseline",
                "candidate",
                "gate_status",
            }
        ),
    )
    for field_name, expected in (
        ("expected_clips", 4),
        ("expected_atoms", 10),
        ("expected_symbols", 10),
    ):
        if _public_int(category[field_name], maximum=expected) != expected:
            raise ProtocolValidationError("public_report_count_invalid")
    limits = {
        "clip_exact": 4,
        "atom_exact": 10,
        "atom_sequence_exact": 4,
        "symbol_sequence_exact": 4,
        "symbol_edit_distance": 4 * MAX_TRANSCRIPT_CHARACTERS + 10,
    }
    _public_model_metrics(category["baseline"], limits)
    candidate = _public_model_metrics(category["candidate"], limits)
    status = _public_status(category["gate_status"], frozenset({"pass", "fail"}))
    expected = (
        "pass"
        if candidate["clip_exact"] == 4
        and candidate["atom_exact"] == 10
        and candidate["atom_sequence_exact"] == 4
        and candidate["symbol_sequence_exact"] == 4
        and candidate["symbol_edit_distance"] == 0
        else "fail"
    )
    if status != expected:
        raise ProtocolValidationError("public_report_consistency_invalid")
    return category


def _validate_exact_category_public(
    value: object,
    *,
    expected_clips: int,
    required_correct: int,
) -> dict[str, object]:
    category = _public_object(
        value,
        frozenset(
            {
                "expected_clips",
                "required_correct",
                "baseline",
                "candidate",
                "gate_status",
            }
        ),
    )
    if (
        _public_int(category["expected_clips"], maximum=expected_clips)
        != expected_clips
        or _public_int(category["required_correct"], maximum=expected_clips)
        != required_correct
    ):
        raise ProtocolValidationError("public_report_count_invalid")
    limits = {"clip_exact": expected_clips}
    _public_model_metrics(category["baseline"], limits)
    candidate = _public_model_metrics(category["candidate"], limits)
    status = _public_status(category["gate_status"], frozenset({"pass", "fail"}))
    expected = "pass" if candidate["clip_exact"] >= required_correct else "fail"
    if status != expected:
        raise ProtocolValidationError("public_report_consistency_invalid")
    return category


def _validate_ascii_public(value: object) -> dict[str, object]:
    category = _public_object(
        value,
        frozenset(
            {
                "expected_clips",
                "expected_entities",
                "baseline",
                "candidate",
                "gate_status",
            }
        ),
    )
    if (
        _public_int(category["expected_clips"], maximum=4) != 4
        or _public_int(category["expected_entities"], maximum=12) != 12
    ):
        raise ProtocolValidationError("public_report_count_invalid")
    limits = {"sequence_exact": 4, "entity_exact": 12}
    _public_model_metrics(category["baseline"], limits)
    candidate = _public_model_metrics(category["candidate"], limits)
    status = _public_status(category["gate_status"], frozenset({"pass", "fail"}))
    expected = (
        "pass"
        if candidate["sequence_exact"] == 4 and candidate["entity_exact"] == 12
        else "fail"
    )
    if status != expected:
        raise ProtocolValidationError("public_report_consistency_invalid")
    return category


def _validate_split_public(value: object) -> dict[str, object]:
    comparison = _public_object(
        value,
        frozenset(
            {
                "sample_count",
                "baseline_edit_distance",
                "candidate_edit_distance",
                "relative_reduction",
                "non_regression_status",
                "relative_reduction_status",
                "gate_status",
            }
        ),
    )
    if _public_int(comparison["sample_count"], maximum=8) != 8:
        raise ProtocolValidationError("public_report_count_invalid")
    status_values = frozenset({"pass", "fail", "inconclusive", "not_measured"})
    non_regression_status = _public_status(
        comparison["non_regression_status"],
        status_values,
    )
    relative_status = _public_status(
        comparison["relative_reduction_status"],
        status_values,
    )
    gate_status = _public_status(comparison["gate_status"], status_values)
    baseline_value = comparison["baseline_edit_distance"]
    candidate_value = comparison["candidate_edit_distance"]
    relative_value = comparison["relative_reduction"]

    if baseline_value is None or candidate_value is None:
        if not (
            baseline_value is None
            and candidate_value is None
            and relative_value is None
            and non_regression_status == "not_measured"
            and relative_status == "not_measured"
            and gate_status == "not_measured"
        ):
            raise ProtocolValidationError("public_report_consistency_invalid")
        return comparison

    maximum_distance = 8 * MAX_TRANSCRIPT_CHARACTERS
    baseline_distance = _public_int(baseline_value, maximum=maximum_distance)
    candidate_distance = _public_int(candidate_value, maximum=maximum_distance)
    expected_non_regression = (
        "pass" if candidate_distance <= baseline_distance else "fail"
    )
    if non_regression_status != expected_non_regression:
        raise ProtocolValidationError("public_report_consistency_invalid")

    if baseline_distance < RELATIVE_REDUCTION_MIN_BASELINE_DISTANCE:
        if relative_value is not None or relative_status != "inconclusive":
            raise ProtocolValidationError("public_report_consistency_invalid")
        expected_gate = "fail" if non_regression_status == "fail" else "inconclusive"
    else:
        if type(relative_value) is not float or not math.isfinite(relative_value):
            raise ProtocolValidationError("public_report_number_invalid")
        expected_relative = round(
            (baseline_distance - candidate_distance) / baseline_distance,
            6,
        )
        if relative_value != expected_relative:
            raise ProtocolValidationError("public_report_consistency_invalid")
        expected_relative_status = (
            "pass"
            if candidate_distance * RELATIVE_REDUCTION_DENOMINATOR
            <= baseline_distance
            * (RELATIVE_REDUCTION_DENOMINATOR - RELATIVE_REDUCTION_NUMERATOR)
            else "fail"
        )
        if relative_status != expected_relative_status:
            raise ProtocolValidationError("public_report_consistency_invalid")
        expected_gate = (
            "fail"
            if non_regression_status == "fail" or relative_status == "fail"
            else "pass"
        )
    if gate_status != expected_gate:
        raise ProtocolValidationError("public_report_consistency_invalid")
    return comparison


def _validate_pending_gate(
    value: object,
    expected_fields: frozenset[str],
) -> dict[str, Any]:
    gate = _public_object(value, expected_fields)
    _public_status(gate["status"], frozenset({"pending"}))
    _public_status(gate["measurement"], frozenset({"not_measured"}))
    return gate


def validate_public_report(report: object) -> None:
    """Strictly validate one anonymous aggregate report as untrusted JSON."""
    root = _public_object(
        report,
        frozenset(
            {
                "protocol",
                "scope",
                "provider_completion",
                "categories",
                "split_edit_distance",
                "gates",
            }
        ),
    )
    protocol = _public_object(
        root["protocol"],
        frozenset({"version", "schema_sha256", "expected_sample_count"}),
    )
    if (
        protocol["version"] != PROTOCOL_VERSION
        or protocol["schema_sha256"] != PROTOCOL_SCHEMA_SHA256
        or type(protocol["expected_sample_count"]) is not int
        or protocol["expected_sample_count"] != EXPECTED_SAMPLE_COUNT
    ):
        raise ProtocolValidationError("public_report_protocol_invalid")
    if type(root["scope"]) is not str or root["scope"] != "quality_only":
        raise ProtocolValidationError("public_report_scope_invalid")

    completion = _public_object(
        root["provider_completion"],
        frozenset(
            {
                "expected_per_provider",
                "baseline_successful",
                "baseline_failed",
                "candidate_successful",
                "candidate_failed",
                "gate_status",
            }
        ),
    )
    if _public_int(completion["expected_per_provider"], maximum=24) != 24:
        raise ProtocolValidationError("public_report_count_invalid")
    baseline_successful = _public_int(
        completion["baseline_successful"],
        maximum=24,
    )
    baseline_failed = _public_int(completion["baseline_failed"], maximum=24)
    candidate_successful = _public_int(
        completion["candidate_successful"],
        maximum=24,
    )
    candidate_failed = _public_int(completion["candidate_failed"], maximum=24)
    if (
        baseline_successful + baseline_failed != 24
        or candidate_successful + candidate_failed != 24
    ):
        raise ProtocolValidationError("public_report_consistency_invalid")
    completion_status = _public_status(
        completion["gate_status"],
        frozenset({"pass", "fail"}),
    )
    expected_completion = (
        "pass"
        if baseline_successful == 24 and candidate_successful == 24
        else "fail"
    )
    if completion_status != expected_completion:
        raise ProtocolValidationError("public_report_consistency_invalid")

    categories = _public_object(
        root["categories"],
        frozenset(
            {
                "identifiers",
                "technical",
                "positive_homophones",
                "negative_controls",
                "ascii_entities",
            }
        ),
    )
    _validate_identifier_public(categories["identifiers"])
    _validate_technical_public(categories["technical"])
    _validate_exact_category_public(
        categories["positive_homophones"],
        expected_clips=6,
        required_correct=5,
    )
    _validate_exact_category_public(
        categories["negative_controls"],
        expected_clips=3,
        required_correct=3,
    )
    _validate_ascii_public(categories["ascii_entities"])

    splits = _public_object(
        root["split_edit_distance"],
        frozenset({"validation", "holdout"}),
    )
    _validate_split_public(splits["validation"])
    _validate_split_public(splits["holdout"])
    if completion_status == "pass" and any(
        split["gate_status"] == "not_measured" for split in splits.values()
    ):
        raise ProtocolValidationError("public_report_consistency_invalid")

    gates = _public_object(
        root["gates"],
        frozenset(
            {
                "automated_quality",
                "performance_resources",
                "natural_chinese_manual_review",
                "active_dry_run",
                "shadow_readiness",
            }
        ),
    )
    quality_gate = _public_object(
        gates["automated_quality"],
        frozenset({"status"}),
    )
    quality_status = _public_status(
        quality_gate["status"],
        frozenset({"pass", "fail", "inconclusive"}),
    )
    expected_quality = _combined_quality_status(
        completion_status,
        categories,
        splits,
    )
    if quality_status != expected_quality:
        raise ProtocolValidationError("public_report_consistency_invalid")

    _validate_pending_gate(
        gates["performance_resources"],
        frozenset({"status", "measurement"}),
    )
    manual = _validate_pending_gate(
        gates["natural_chinese_manual_review"],
        frozenset(
            {
                "status",
                "measurement",
                "required_semantic_regressions",
                "observed_semantic_regressions",
            }
        ),
    )
    if (
        type(manual["required_semantic_regressions"]) is not int
        or manual["required_semantic_regressions"] != 0
        or manual["observed_semantic_regressions"] is not None
    ):
        raise ProtocolValidationError("public_report_consistency_invalid")
    active = _validate_pending_gate(
        gates["active_dry_run"],
        frozenset(
            {
                "status",
                "measurement",
                "expected_clips",
                "required_changed_commits",
                "observed_changed_commits",
            }
        ),
    )
    if (
        type(active["expected_clips"]) is not int
        or active["expected_clips"] != 24
        or type(active["required_changed_commits"]) is not int
        or active["required_changed_commits"] != 0
        or active["observed_changed_commits"] is not None
    ):
        raise ProtocolValidationError("public_report_consistency_invalid")

    shadow = _public_object(
        gates["shadow_readiness"],
        frozenset({"status"}),
    )
    shadow_status = _public_status(
        shadow["status"],
        frozenset({"pending", "inconclusive", "fail"}),
    )
    expected_shadow = (
        "fail"
        if quality_status == "fail"
        else "inconclusive"
        if quality_status == "inconclusive"
        else "pending"
    )
    if shadow_status != expected_shadow:
        raise ProtocolValidationError("public_report_consistency_invalid")


def _assert_protocol_invariants() -> None:
    if len(_SPECS) != EXPECTED_SAMPLE_COUNT or len(_SPECS_BY_ID) != len(_SPECS):
        raise RuntimeError("protocol sample invariant failed")
    if EXPECTED_REFERENCE_CHARACTERS != 276:
        raise RuntimeError("protocol reference-character invariant failed")
    expected_sample_numbers = set(range(1, EXPECTED_SAMPLE_COUNT + 1))
    if (
        set(PROTOCOL_REFERENCE_CHARACTERS_BY_SAMPLE_NUMBER)
        != expected_sample_numbers
        or set(PROTOCOL_SPLIT_BY_SAMPLE_NUMBER) != expected_sample_numbers
        or tuple(PROTOCOL_SPLIT_BY_SAMPLE_NUMBER.values())
        != ("calibration",) * 8 + ("validation",) * 8 + ("holdout",) * 8
    ):
        raise RuntimeError("protocol sample-number metadata invariant failed")
    expected_categories = {
        "identifiers": _IDENTIFIER_IDS,
        "technical": _TECHNICAL_IDS,
        "positive_homophones": _POSITIVE_HOMOPHONE_IDS,
        "negative_controls": _NEGATIVE_CONTROL_IDS,
        "ascii_entities": _ASCII_ENTITY_IDS,
    }
    if any(
        PROTOCOL_CATEGORY_SAMPLE_NUMBERS[name]
        != tuple(
            sample_number
            for sample_number, spec in enumerate(_SPECS, 1)
            if spec.sample_id in sample_ids
        )
        for name, sample_ids in expected_categories.items()
    ):
        raise RuntimeError("protocol category sample-number invariant failed")
    identifier_tokens = sum(
        len(_identifier_signature(_SPECS_BY_ID[item].ground_truth))
        for item in _IDENTIFIER_IDS
    )
    if identifier_tokens != 14:
        raise RuntimeError("identifier token invariant failed")
    if len(_TECHNICAL_IDS) != 4 or sum(map(len, _TECHNICAL_ATOMS.values())) != 10:
        raise RuntimeError("technical atom invariant failed")
    if len(_POSITIVE_HOMOPHONE_IDS) != 6 or len(_NEGATIVE_CONTROL_IDS) != 3:
        raise RuntimeError("homophone-control invariant failed")
    ascii_entities = sum(
        len(_ascii_signature(_SPECS_BY_ID[item].ground_truth))
        for item in _ASCII_ENTITY_IDS
    )
    if ascii_entities != 12:
        raise RuntimeError("ASCII entity invariant failed")


_assert_protocol_invariants()
