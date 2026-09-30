#!/usr/bin/env python3
"""Normalize the frozen Voice Fly legacy dictionaries into domain packs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

from lexicon_lib import LexiconError, detect_conflicts, normalized_form, stable_json, validate_term


MODEL = "sherpa-onnx-x-asr-480ms-streaming-zipformer-transducer-zh-en-punct-int8-2026-06-05"
MIGRATION_DATE = "2026-08-16"
DOMAIN_PACKS = {
    "AI": "ai_tech",
    "独立站": "commerce_dtc",
    "电子烟": "vape",
    "股票": "quant",
}
ALL_PACKS = ("core", "ai_tech", "commerce_dtc", "quant", "vape", "poker")
RISK_ORDER = {"low": 0, "medium": 1, "high": 2, "blocked": 3}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--domain-dict", required=True, type=Path)
    parser.add_argument("--custom-phrase", required=True, type=Path)
    parser.add_argument("--seed-root", required=True, type=Path)
    parser.add_argument("--pack-root", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    return parser.parse_args(argv)


def _read_tsv(path: Path, expected_columns: int) -> list[tuple[int, list[str]]]:
    rows: list[tuple[int, list[str]]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise LexiconError(f"cannot read legacy input {path}: {exc}") from exc
    for line_no, raw in enumerate(lines, 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        cells = raw.split("\t")
        if len(cells) != expected_columns or any(not cell.strip() for cell in cells):
            raise LexiconError(
                f"{path}:{line_no}: expected {expected_columns} non-empty tab-separated columns"
            )
        rows.append((line_no, [cell.strip() for cell in cells]))
    return rows


def _has_cjk(value: str) -> bool:
    return any("\u3400" <= char <= "\u9fff" for char in value)


def _has_latin(value: str) -> bool:
    return bool(re.search(r"[A-Za-z]", value))


def _language(value: str) -> str:
    cjk, latin = _has_cjk(value), _has_latin(value)
    if cjk and latin:
        return "mixed"
    if cjk:
        return "zh"
    if latin:
        return "en"
    return "other"


def _slug(value: str) -> str:
    digest = hashlib.sha1(normalized_form(value).encode("utf-8")).hexdigest()
    ascii_slug = re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")
    if ascii_slug:
        return f"{ascii_slug[:39]}_{digest[:8]}"
    return f"zh_{digest[:12]}"


def _match_mode(form: str, *, casing: bool = False) -> str:
    if _has_cjk(form):
        return "substring" if casing else "phrase"
    return "token"


def _semantic_risk(form: str) -> str:
    compact = re.sub(r"[^A-Za-z0-9\u3400-\u9fff]", "", form)
    if len(compact) <= 3:
        return "high"
    return "medium"


def _variant(
    form: str,
    *,
    kind: str,
    source: str,
    casing: bool = False,
    match_mode: str | None = None,
    risk: str | None = None,
    priority: int = 100,
    positive_context: Iterable[str] = (),
    negative_context: Iterable[str] = (),
    model_scope: Iterable[str] | None = None,
) -> dict[str, Any]:
    return {
        "form": form,
        "kind": kind,
        "match_mode": match_mode or _match_mode(form, casing=casing),
        "risk": risk or ("low" if casing else _semantic_risk(form)),
        "priority": int(priority),
        "model_scope": sorted(set(model_scope if model_scope is not None else (["*"] if casing else [MODEL]))),
        "positive_context": sorted(set(positive_context)),
        "negative_context": sorted(set(negative_context)),
        "sources": [source],
    }


def _empty_term(
    *,
    term_id: str,
    canonical: str,
    pack: str,
    term_type: str,
    source: str,
    layer: str = "domain",
) -> dict[str, Any]:
    return {
        "id": term_id,
        "canonical": canonical,
        "preferred_forms": {"mixed": canonical},
        "language": _language(canonical),
        "term_type": term_type,
        "domains": [pack],
        "layer": layer,
        "visibility": "private",
        "status": "active",
        "description": "",
        "speech": [],
        "casing": [],
        "keyboard": {
            "enabled": False,
            "pinyin": [],
            "flypy_codes": [],
            "weight": 0,
            "custom_phrase_commits": 0,
        },
        "hotword": {
            "enabled": False,
            "forms": [],
            "boost": 0,
            "form_scores": {},
            "model_scope": [],
        },
        "governance": {
            "source": source,
            "owner": "voiceflypy-local",
            "evidence_count": 0,
            "confirmed_count": 0,
            "rejected_count": 0,
            "created_at": MIGRATION_DATE,
            "updated_at": MIGRATION_DATE,
            "expires_at": None,
        },
    }


def _merge_variant(target: list[dict[str, Any]], incoming: dict[str, Any]) -> bool:
    """Merge a normalized duplicate, returning True when a row was collapsed."""
    key = normalized_form(incoming["form"])
    for existing in target:
        if normalized_form(existing["form"]) != key:
            continue
        for field in ("model_scope", "positive_context", "negative_context", "sources"):
            existing[field] = sorted(set(existing[field]) | set(incoming[field]))
        existing["priority"] = max(existing["priority"], incoming["priority"])
        if RISK_ORDER[incoming["risk"]] > RISK_ORDER[existing["risk"]]:
            existing["risk"] = incoming["risk"]
        if incoming["match_mode"] == "context":
            existing["match_mode"] = "context"
        return True
    target.append(incoming)
    return False


def migrate_domain_dict(path: Path) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    rows = _read_tsv(path, 3)
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    raw_by_pack: Counter[str] = Counter()
    semantic_rows = 0
    casing_rows = 0
    duplicate_rows = 0

    for line_no, (form, canonical, legacy_domain) in rows:
        if legacy_domain not in DOMAIN_PACKS:
            raise LexiconError(f"{path}:{line_no}: unknown domain {legacy_domain!r}")
        pack = DOMAIN_PACKS[legacy_domain]
        raw_by_pack[pack] += 1
        key = (pack, normalized_form(canonical))
        term = grouped.get(key)
        if term is None:
            term = _empty_term(
                term_id=f"{pack}.{_slug(canonical)}",
                canonical=canonical,
                pack=pack,
                term_type="legacy_term",
                source="legacy-domain-dict",
            )
            grouped[key] = term
        source = f"data/imports/domain_dict.txt:{line_no}"
        is_casing = normalized_form(form) == normalized_form(canonical)
        channel = "casing" if is_casing else "speech"
        if is_casing:
            casing_rows += 1
        else:
            semantic_rows += 1
        collapsed = _merge_variant(
            term[channel],
            _variant(
                form,
                kind="canonical_case" if is_casing else "asr_error",
                source=source,
                casing=is_casing,
            ),
        )
        duplicate_rows += int(collapsed)
        term["governance"]["evidence_count"] += 1

    packs: dict[str, list[dict[str, Any]]] = {pack: [] for pack in ALL_PACKS}
    for (pack, _), term in grouped.items():
        packs[pack].append(term)
    report = {
        "raw_rows": len(rows),
        "semantic_rows": semantic_rows,
        "casing_rows": casing_rows,
        "collapsed_duplicate_rows": duplicate_rows,
        "unique_variants": semantic_rows + casing_rows - duplicate_rows,
        "source_references": sum(
            len(variant["sources"])
            for terms in packs.values()
            for term in terms
            for channel in ("speech", "casing")
            for variant in term[channel]
        ),
        "raw_rows_by_pack": dict(sorted(raw_by_pack.items())),
    }
    return packs, report


def migrate_custom_phrase(path: Path, core: list[dict[str, Any]]) -> dict[str, Any]:
    rows = _read_tsv(path, 3)
    by_canonical = {normalized_form(term["canonical"]): term for term in core}
    for line_no, (canonical, code, commits_text) in rows:
        try:
            commits = int(commits_text)
        except ValueError as exc:
            raise LexiconError(f"{path}:{line_no}: commits must be an integer") from exc
        if commits < 0:
            raise LexiconError(f"{path}:{line_no}: commits must be non-negative")
        term = by_canonical.get(normalized_form(canonical))
        if term is None:
            term = _empty_term(
                term_id=f"core.{_slug(canonical)}",
                canonical=canonical,
                pack="core",
                term_type="custom_phrase",
                source="legacy-custom-phrase",
                layer="core",
            )
            core.append(term)
            by_canonical[normalized_form(canonical)] = term
        keyboard = term["keyboard"]
        keyboard["enabled"] = True
        keyboard["flypy_codes"] = sorted(set(keyboard["flypy_codes"]) | {"".join(code.split())})
        # Static dictionary weight and stabledb commits deliberately use different fields.
        keyboard["custom_phrase_commits"] = max(keyboard["custom_phrase_commits"], commits)
        term["governance"]["evidence_count"] += 1
    return {"raw_rows": len(rows), "terms": len(by_canonical), "commits_preserved": True}


def _seed_variant(value: Any, canonical: str, source: str, *, casing: bool) -> dict[str, Any]:
    if isinstance(value, str):
        form, options = value, {}
    elif isinstance(value, dict) and isinstance(value.get("form"), str):
        form, options = value["form"], value
    else:
        raise LexiconError(f"{source}: seed variant must be a string or an object with form")
    if casing:
        kind = "canonical_case"
    elif _has_latin(form) and _has_cjk(canonical):
        kind = "spoken_alias"
    else:
        kind = "asr_error"
    return _variant(
        form,
        kind=options.get("kind", kind),
        source=source,
        casing=casing,
        match_mode=options.get("match_mode"),
        risk=options.get("risk"),
        priority=options.get("priority", 120),
        positive_context=options.get("positive_context", []),
        negative_context=options.get("negative_context", []),
        model_scope=options.get("model_scope"),
    )


def _normalize_seed(seed: dict[str, Any], pack: str, source: str) -> dict[str, Any]:
    term_id, canonical = seed.get("id"), seed.get("canonical")
    if not isinstance(term_id, str) or not term_id.startswith(pack + "."):
        raise LexiconError(f"{source}: seed id must start with {pack}.")
    if not isinstance(canonical, str) or not canonical.strip():
        raise LexiconError(f"{source}: seed canonical is required")
    term = _empty_term(
        term_id=term_id,
        canonical=canonical,
        pack=pack,
        term_type=seed.get("term_type", "domain_term"),
        source="reviewed-seed",
    )
    for channel in ("speech", "casing"):
        values = seed.get(channel, [])
        if not isinstance(values, list):
            raise LexiconError(f"{source}: {channel} must be a list")
        for value in values:
            _merge_variant(term[channel], _seed_variant(value, canonical, source, casing=channel == "casing"))
    pinyin = seed.get("pinyin", [])
    flypy_codes = seed.get("flypy_codes", [])
    if not isinstance(pinyin, list) or not all(isinstance(item, str) and item.strip() for item in pinyin):
        raise LexiconError(f"{source}: pinyin must be a non-empty string list or []")
    if not isinstance(flypy_codes, list) or not all(isinstance(item, str) and item.strip() for item in flypy_codes):
        raise LexiconError(f"{source}: flypy_codes must be a non-empty string list or []")
    term["keyboard"] = {
        "enabled": bool(pinyin or flypy_codes),
        "pinyin": sorted(set(" ".join(value.split()) for value in pinyin)),
        "flypy_codes": sorted(set("".join(value.split()) for value in flypy_codes)),
        "weight": int(seed.get("weight", 150 if pinyin else 0)),
        "custom_phrase_commits": int(seed.get("custom_phrase_commits", 0)),
    }
    hotwords = seed.get("hotwords", [canonical])
    if not isinstance(hotwords, list) or not all(isinstance(item, str) and item.strip() for item in hotwords):
        raise LexiconError(f"{source}: hotwords must be a non-empty string list or []")
    form_scores = seed.get("hotword_scores", {})
    if not isinstance(form_scores, dict) or not all(
        isinstance(key, str)
        and key in hotwords
        and isinstance(value, (int, float))
        and value >= 0
        for key, value in form_scores.items()
    ):
        raise LexiconError(f"{source}: hotword_scores must map listed forms to non-negative scores")
    term["hotword"] = {
        "enabled": bool(hotwords),
        "forms": sorted(set(hotwords)),
        "boost": float(seed.get("hotword_boost", 2.5 if _has_cjk(canonical) else 1.5)),
        "form_scores": dict(sorted((key, float(value)) for key, value in form_scores.items())),
        "model_scope": [MODEL],
    }
    term["governance"]["evidence_count"] = 1
    return term


def _merge_term(existing: dict[str, Any], seed: dict[str, Any]) -> dict[str, Any]:
    existing["id"] = seed["id"]
    existing["term_type"] = seed["term_type"]
    existing["description"] = seed["description"]
    for channel in ("speech", "casing"):
        for variant in seed[channel]:
            _merge_variant(existing[channel], variant)
    for field in ("pinyin", "flypy_codes"):
        existing["keyboard"][field] = sorted(
            set(existing["keyboard"][field]) | set(seed["keyboard"][field])
        )
    existing["keyboard"]["enabled"] = bool(
        existing["keyboard"]["pinyin"] or existing["keyboard"]["flypy_codes"]
    )
    existing["keyboard"]["weight"] = max(
        existing["keyboard"]["weight"], seed["keyboard"]["weight"]
    )
    existing["keyboard"]["custom_phrase_commits"] = max(
        existing["keyboard"]["custom_phrase_commits"], seed["keyboard"]["custom_phrase_commits"]
    )
    existing["hotword"]["enabled"] = existing["hotword"]["enabled"] or seed["hotword"]["enabled"]
    existing["hotword"]["forms"] = sorted(
        set(existing["hotword"]["forms"]) | set(seed["hotword"]["forms"])
    )
    existing["hotword"]["boost"] = max(existing["hotword"]["boost"], seed["hotword"]["boost"])
    for form, score in seed["hotword"]["form_scores"].items():
        existing["hotword"]["form_scores"][form] = max(
            existing["hotword"]["form_scores"].get(form, 0), score
        )
    existing["hotword"]["model_scope"] = sorted(
        set(existing["hotword"]["model_scope"]) | set(seed["hotword"]["model_scope"])
    )
    existing["governance"]["source"] = "legacy-domain-dict+reviewed-seed"
    existing["governance"]["evidence_count"] += seed["governance"]["evidence_count"]
    return existing


def add_seeds(seed_root: Path, packs: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    report: dict[str, Any] = {}
    for path in sorted(seed_root.glob("*.json")):
        pack = path.stem
        if pack not in ALL_PACKS:
            raise LexiconError(f"unsupported seed pack {pack!r}")
        try:
            values = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LexiconError(f"cannot read seed {path}: {exc}") from exc
        if not isinstance(values, list):
            raise LexiconError(f"{path}: seed root must be a list")
        by_canonical = {normalized_form(term["canonical"]): term for term in packs[pack]}
        merged = 0
        for index, value in enumerate(values, 1):
            if not isinstance(value, dict):
                raise LexiconError(f"{path}:{index}: seed must be an object")
            source = f"data/seeds/{path.name}:{index}"
            seed = _normalize_seed(value, pack, source)
            key = normalized_form(seed["canonical"])
            if key in by_canonical:
                _merge_term(by_canonical[key], seed)
                merged += 1
            else:
                packs[pack].append(seed)
                by_canonical[key] = seed
        report[pack] = {"seed_rows": len(values), "merged_with_legacy": merged}
    return report


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def write_packs(pack_root: Path, packs: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    ids: dict[str, str] = {}
    report: dict[str, Any] = {}
    for pack in ALL_PACKS:
        terms = sorted(packs.get(pack, []), key=lambda item: item["id"])
        for term in terms:
            validate_term(term)
            previous = ids.get(term["id"])
            if previous:
                raise LexiconError(f"duplicate id {term['id']} in {previous} and {pack}")
            ids[term["id"]] = pack
        conflicts = detect_conflicts(terms)
        if conflicts["hard_conflicts"]:
            first = conflicts["hard_conflicts"][0]
            raise LexiconError(
                f"pack {pack} has {len(conflicts['hard_conflicts'])} conflicts; first: {first}"
            )
        content = "".join(stable_json(term) + "\n" for term in terms)
        _atomic_write(pack_root / f"{pack}.jsonl", content)
        report[pack] = {
            "terms": len(terms),
            "semantic_variants": sum(len(term["speech"]) for term in terms),
            "casing_variants": sum(len(term["casing"]) for term in terms),
            "rime_spellings": sum(len(term["keyboard"]["pinyin"]) for term in terms),
            "custom_phrases": sum(len(term["keyboard"]["flypy_codes"]) for term in terms),
            "hotword_forms": sum(len(term["hotword"]["forms"]) for term in terms),
            "warnings": len(conflicts["warnings"]),
        }
    return report


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        packs, legacy_report = migrate_domain_dict(args.domain_dict)
        custom_report = migrate_custom_phrase(args.custom_phrase, packs["core"])
        seed_report = add_seeds(args.seed_root, packs)
        pack_report = write_packs(args.pack_root, packs)
        report = {
            "schema_version": "1.0.0",
            "migration_date": MIGRATION_DATE,
            "legacy_domain_dict": legacy_report,
            "legacy_custom_phrase": custom_report,
            "seeds": seed_report,
            "packs": pack_report,
            "accounting": {
                "all_legacy_rows_classified": (
                    legacy_report["raw_rows"]
                    == legacy_report["semantic_rows"] + legacy_report["casing_rows"]
                    == legacy_report["source_references"]
                ),
                "all_requested_pack_files_created": set(ALL_PACKS) == set(pack_report),
            },
        }
        _atomic_write(args.report, stable_json(report, indent=2) + "\n")
    except LexiconError as exc:
        print(f"migration failed: {exc}", file=sys.stderr)
        return 2
    print(stable_json(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
