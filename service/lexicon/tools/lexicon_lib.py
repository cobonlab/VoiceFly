#!/usr/bin/env python3
"""Dependency-free compiler primitives for the Voice Fly master lexicon."""

from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


GENERATOR_VERSION = "0.1.0"
RIME_DICTIONARY_NAME = "voiceflypy_personal"
RIME_DICTIONARY_FILE = f"{RIME_DICTIONARY_NAME}.dict.yaml"
VALID_LANGUAGES = {"zh", "en", "mixed", "other"}
VALID_LAYERS = {"core", "personal", "domain", "project", "probation", "quarantine"}
VALID_VISIBILITY = {"public", "private", "client-secret"}
VALID_STATUSES = {"draft", "probation", "active", "deprecated", "quarantined"}
VALID_MATCH_MODES = {"token", "phrase", "substring", "context"}
VALID_RISKS = {"low", "medium", "high", "blocked"}
VALID_VARIANT_KINDS = {"asr_error", "spoken_alias", "canonical_case", "legacy_noop"}
TERM_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


class LexiconError(RuntimeError):
    """Raised when source lexicon data is unsafe or invalid."""


def stable_json(value: Any, *, indent: int | None = None) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        indent=indent,
        separators=(",", ":") if indent is None else None,
    )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def normalized_form(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    return " ".join(value.split()).casefold()


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LexiconError(f"cannot read JSON {path}: {exc}") from exc


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise LexiconError(f"cannot read pack {path}: {exc}") from exc
    for line_no, line in enumerate(lines, 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise LexiconError(f"invalid JSONL {path}:{line_no}: {exc}") from exc
        if not isinstance(record, dict):
            raise LexiconError(f"term must be an object at {path}:{line_no}")
        record["_source_file"] = path.as_posix()
        record["_source_line"] = line_no
        records.append(record)
    return records


def _require_type(term: dict[str, Any], field: str, expected: type) -> Any:
    value = term.get(field)
    if not isinstance(value, expected):
        raise LexiconError(f"{term.get('id', '<unknown>')}: {field} must be {expected.__name__}")
    return value


def validate_variant(term_id: str, variant: dict[str, Any], channel: str) -> None:
    if not isinstance(variant, dict):
        raise LexiconError(f"{term_id}: {channel} variant must be an object")
    form = variant.get("form")
    if not isinstance(form, str) or not form.strip() or "\t" in form or "\n" in form:
        raise LexiconError(f"{term_id}: invalid {channel} form {form!r}")
    if variant.get("kind") not in VALID_VARIANT_KINDS:
        raise LexiconError(f"{term_id}: invalid variant kind {variant.get('kind')!r}")
    if variant.get("match_mode") not in VALID_MATCH_MODES:
        raise LexiconError(f"{term_id}: invalid match mode {variant.get('match_mode')!r}")
    if variant.get("risk") not in VALID_RISKS:
        raise LexiconError(f"{term_id}: invalid risk {variant.get('risk')!r}")
    if not isinstance(variant.get("priority"), int):
        raise LexiconError(f"{term_id}: variant priority must be an integer")
    for field in ("model_scope", "positive_context", "negative_context", "sources"):
        value = variant.get(field)
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise LexiconError(f"{term_id}: variant {field} must be a string list")


def validate_term(term: dict[str, Any]) -> None:
    term_id = term.get("id")
    if not isinstance(term_id, str) or not TERM_ID_RE.fullmatch(term_id):
        raise LexiconError(f"invalid term id {term_id!r}")
    canonical = term.get("canonical")
    if not isinstance(canonical, str) or not canonical.strip() or "\t" in canonical or "\n" in canonical:
        raise LexiconError(f"{term_id}: invalid canonical form")
    preferred = _require_type(term, "preferred_forms", dict)
    if not preferred or not all(isinstance(k, str) and isinstance(v, str) and v for k, v in preferred.items()):
        raise LexiconError(f"{term_id}: preferred_forms must contain non-empty strings")
    if term.get("language") not in VALID_LANGUAGES:
        raise LexiconError(f"{term_id}: invalid language {term.get('language')!r}")
    if not isinstance(term.get("term_type"), str) or not term["term_type"]:
        raise LexiconError(f"{term_id}: term_type is required")
    domains = _require_type(term, "domains", list)
    if not domains or not all(isinstance(item, str) and TERM_ID_RE.fullmatch(item) for item in domains):
        raise LexiconError(f"{term_id}: invalid domains")
    if term.get("layer") not in VALID_LAYERS:
        raise LexiconError(f"{term_id}: invalid layer")
    if term.get("visibility") not in VALID_VISIBILITY:
        raise LexiconError(f"{term_id}: invalid visibility")
    if term.get("status") not in VALID_STATUSES:
        raise LexiconError(f"{term_id}: invalid status")
    for channel in ("speech", "casing"):
        variants = _require_type(term, channel, list)
        for variant in variants:
            validate_variant(term_id, variant, channel)
    keyboard = _require_type(term, "keyboard", dict)
    if not isinstance(keyboard.get("enabled"), bool):
        raise LexiconError(f"{term_id}: keyboard.enabled must be boolean")
    for field in ("pinyin", "flypy_codes"):
        values = keyboard.get(field)
        if not isinstance(values, list) or not all(isinstance(item, str) and item.strip() for item in values):
            raise LexiconError(f"{term_id}: keyboard.{field} must be a non-empty string list or []")
    weight = keyboard.get("weight")
    if not isinstance(weight, int) or weight < 0:
        raise LexiconError(f"{term_id}: keyboard.weight must be a non-negative integer")
    custom_phrase_commits = keyboard.get("custom_phrase_commits")
    if not isinstance(custom_phrase_commits, int) or custom_phrase_commits < 0:
        raise LexiconError(
            f"{term_id}: keyboard.custom_phrase_commits must be a non-negative integer"
        )
    hotword = _require_type(term, "hotword", dict)
    if not isinstance(hotword.get("enabled"), bool):
        raise LexiconError(f"{term_id}: hotword.enabled must be boolean")
    if not isinstance(hotword.get("forms"), list) or not all(
        isinstance(item, str) and item.strip() for item in hotword["forms"]
    ):
        raise LexiconError(f"{term_id}: hotword.forms must be a string list")
    form_scores = hotword.get("form_scores")
    if not isinstance(form_scores, dict) or not all(
        isinstance(key, str)
        and key in hotword["forms"]
        and isinstance(value, (int, float))
        and value >= 0
        for key, value in form_scores.items()
    ):
        raise LexiconError(f"{term_id}: hotword.form_scores must map listed forms to non-negative scores")
    if not isinstance(hotword.get("boost"), (int, float)) or hotword["boost"] < 0:
        raise LexiconError(f"{term_id}: hotword.boost must be non-negative")
    if not isinstance(hotword.get("model_scope"), list):
        raise LexiconError(f"{term_id}: hotword.model_scope must be a list")
    governance = _require_type(term, "governance", dict)
    for field in ("source", "owner", "created_at", "updated_at"):
        if not isinstance(governance.get(field), str):
            raise LexiconError(f"{term_id}: governance.{field} is required")
    for field in ("evidence_count", "confirmed_count", "rejected_count"):
        if not isinstance(governance.get(field), int) or governance[field] < 0:
            raise LexiconError(f"{term_id}: governance.{field} must be non-negative integer")


@dataclass(frozen=True)
class ResolvedProfile:
    config: dict[str, Any]
    terms: list[dict[str, Any]]
    source_files: list[Path]


def resolve_profile(root: Path, profile_id: str) -> ResolvedProfile:
    profile_path = root / "profiles" / f"{profile_id}.json"
    config = read_json(profile_path)
    if not isinstance(config, dict) or config.get("id") != profile_id:
        raise LexiconError(f"profile id mismatch in {profile_path}")
    packs = config.get("packs")
    statuses = config.get("include_status")
    if not isinstance(packs, list) or not packs or not all(isinstance(item, str) for item in packs):
        raise LexiconError(f"profile {profile_id}: packs must be a non-empty string list")
    if not isinstance(statuses, list) or not statuses or not set(statuses) <= VALID_STATUSES:
        raise LexiconError(f"profile {profile_id}: invalid include_status")

    source_files = [profile_path]
    all_terms: list[dict[str, Any]] = []
    ids: dict[str, str] = {}
    for pack in packs:
        pack_path = root / "data" / "packs" / f"{pack}.jsonl"
        if not pack_path.is_file():
            raise LexiconError(f"profile {profile_id}: missing pack {pack_path}")
        source_files.append(pack_path)
        for term in read_jsonl(pack_path):
            validate_term(term)
            source = f"{term['_source_file']}:{term['_source_line']}"
            previous = ids.get(term["id"])
            if previous:
                raise LexiconError(f"duplicate term id {term['id']} at {previous} and {source}")
            ids[term["id"]] = source
            if term["status"] in statuses:
                clean = {key: value for key, value in term.items() if not key.startswith("_source_")}
                all_terms.append(clean)
    all_terms.sort(key=lambda item: item["id"])
    return ResolvedProfile(config=config, terms=all_terms, source_files=source_files)


def detect_conflicts(terms: Iterable[dict[str, Any]]) -> dict[str, Any]:
    aliases: dict[tuple[str, str], dict[str, str]] = {}
    hard: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    semantic_aliases: dict[str, tuple[str, str]] = {}

    for term in terms:
        for channel in ("speech", "casing"):
            for variant in term[channel]:
                key = (channel, normalized_form(variant["form"]))
                existing = aliases.get(key)
                if existing and normalized_form(existing["canonical"]) != normalized_form(term["canonical"]):
                    hard.append(
                        {
                            "channel": channel,
                            "alias": variant["form"],
                            "first_term": existing["term_id"],
                            "first_canonical": existing["canonical"],
                            "second_term": term["id"],
                            "second_canonical": term["canonical"],
                        }
                    )
                else:
                    aliases[key] = {"term_id": term["id"], "canonical": term["canonical"]}
                if channel == "speech":
                    semantic_aliases[normalized_form(variant["form"])] = (term["id"], term["canonical"])

    for term in terms:
        target = semantic_aliases.get(normalized_form(term["canonical"]))
        if target and target[0] != term["id"]:
            warnings.append(
                {
                    "kind": "possible_cascade",
                    "term_id": term["id"],
                    "canonical": term["canonical"],
                    "matches_alias_of": target[0],
                    "next_canonical": target[1],
                }
            )
    hard.sort(key=lambda item: (item["channel"], normalized_form(item["alias"]), item["first_term"], item["second_term"]))
    warnings.sort(key=lambda item: (item["kind"], item["term_id"]))
    return {"hard_conflicts": hard, "warnings": warnings}


def rule_context_allows(rule: dict[str, Any], surrounding_text: str) -> bool:
    """Reference eligibility contract consumed by tests and future adapters."""
    haystack = normalized_form(surrounding_text)
    negative = rule.get("negative_context", [])
    if any(normalized_form(value) in haystack for value in negative):
        return False
    if rule.get("match_mode") != "context":
        return True
    positive = rule.get("positive_context", [])
    return bool(positive) and any(normalized_form(value) in haystack for value in positive)


def _json_cell(value: list[str]) -> str:
    return stable_json(value)


def _variant_rows(terms: list[dict[str, Any]], channel: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for term in terms:
        domain = term["domains"][0]
        for variant in term[channel]:
            rows.append(
                {
                    "form": variant["form"],
                    "canonical": term["canonical"],
                    "domain": domain,
                    "term_id": term["id"],
                    "kind": variant["kind"],
                    "match_mode": variant["match_mode"],
                    "risk": variant["risk"],
                    "priority": variant["priority"],
                    "model_scope": variant["model_scope"],
                    "positive_context": variant["positive_context"],
                    "negative_context": variant["negative_context"],
                }
            )
    rows.sort(key=lambda row: (-len(row["form"]), normalized_form(row["form"]), row["canonical"], row["term_id"]))
    return rows


def _structured_tsv(rows: list[dict[str, Any]]) -> str:
    columns = [
        "form",
        "canonical",
        "domain",
        "term_id",
        "kind",
        "match_mode",
        "risk",
        "priority",
        "model_scope",
        "positive_context",
        "negative_context",
    ]
    lines = ["\t".join(columns)]
    for row in rows:
        values: list[str] = []
        for column in columns:
            value = row[column]
            values.append(_json_cell(value) if isinstance(value, list) else str(value))
        lines.append("\t".join(values))
    return "\n".join(lines) + "\n"


def _legacy_voice_tsv(semantic: list[dict[str, Any]], casing: list[dict[str, Any]], profile: str, release: str) -> str:
    lines = [
        "# Generated by Voice Fly unified lexicon; do not edit.",
        f"# profile={profile} release={release} generator={GENERATOR_VERSION}",
        "# Backward-compatible format: form<TAB>canonical<TAB>domain",
    ]
    seen: set[tuple[str, str, str]] = set()
    for row in semantic + casing:
        key = (normalized_form(row["form"]), row["canonical"], row["domain"])
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"{row['form']}\t{row['canonical']}\t{row['domain']}")
    return "\n".join(lines) + "\n"


def _rime_dictionary(terms: list[dict[str, Any]], release: str) -> tuple[str, int]:
    entries: list[tuple[str, str, int, str]] = []
    for term in terms:
        keyboard = term["keyboard"]
        if not keyboard["enabled"]:
            continue
        for pinyin in keyboard["pinyin"]:
            entries.append((term["canonical"], " ".join(pinyin.split()), int(keyboard["weight"]), term["id"]))
    entries.sort(key=lambda item: (item[0], item[1], -item[2], item[3]))
    lines = [
        "# Generated by Voice Fly unified lexicon; do not edit.",
        "---",
        f"name: {RIME_DICTIONARY_NAME}",
        f'version: "{release}"',
        "sort: by_weight",
        "use_preset_vocabulary: false",
        "...",
    ]
    lines.extend(f"{text}\t{pinyin}\t{weight}" for text, pinyin, weight, _ in entries)
    return "\n".join(lines) + "\n", len(entries)


def _custom_phrase(terms: list[dict[str, Any]]) -> tuple[str, int]:
    entries: list[tuple[str, str, int, str]] = []
    for term in terms:
        keyboard = term["keyboard"]
        if not keyboard["enabled"]:
            continue
        commits = int(keyboard["custom_phrase_commits"])
        for code in keyboard["flypy_codes"]:
            entries.append((term["canonical"], "".join(code.split()), commits, term["id"]))
    entries.sort(key=lambda item: (item[1], item[0], item[3]))
    lines = [
        "# Generated explicit Flypy phrases; third column is integer commits.",
        "# text<TAB>flypy_code<TAB>commits",
    ]
    lines.extend(f"{text}\t{code}\t{commits}" for text, code, commits, _ in entries)
    return "\n".join(lines) + "\n", len(entries)


def _decoder_hotword_text(value: str) -> str:
    """Separate every CJK character while preserving Latin word tokens."""
    parts: list[str] = []
    buffer: list[str] = []

    def flush() -> None:
        if buffer:
            parts.extend("".join(buffer).split())
            buffer.clear()

    for char in unicodedata.normalize("NFKC", value):
        if "\u3400" <= char <= "\u9fff":
            flush()
            parts.append(char)
        elif char.isspace():
            flush()
        else:
            buffer.append(char)
    flush()
    return " ".join(parts)


def _hotword_source(
    terms: list[dict[str, Any]], profile: str, release: str
) -> tuple[str, str, str, int, int]:
    rows: list[dict[str, Any]] = []
    for term in terms:
        hotword = term["hotword"]
        if not hotword["enabled"]:
            continue
        for form in hotword["forms"]:
            rows.append(
                {
                    "canonical_text": term["canonical"],
                    "natural_text": form,
                    "decoder_text": _decoder_hotword_text(form),
                    "score": float(hotword["form_scores"].get(form, hotword["boost"])),
                    "domain": term["domains"][0],
                    "term_id": term["id"],
                    "model_scope": hotword["model_scope"],
                }
            )
    rows.sort(key=lambda item: (normalized_form(item["natural_text"]), item["term_id"]))
    columns = [
        "canonical_text",
        "natural_text",
        "decoder_text",
        "score",
        "domain",
        "term_id",
        "model_scope",
    ]
    lines = ["\t".join(columns)]
    lines.extend(
        "\t".join(
            _json_cell(row[column])
            if isinstance(row[column], list)
            else f"{row[column]:g}"
            if isinstance(row[column], float)
            else str(row[column])
            for column in columns
        )
        for row in rows
    )
    source_json = {
        "schema_version": "1.0.0",
        "profile": profile,
        "release": release,
        "decoder_contract": "CJK characters are separated by ASCII spaces; Latin word tokens are preserved.",
        "runtime_enabled": bool(rows),
        "entries": rows,
    }
    runtime_scores: dict[str, float] = {}
    for row in rows:
        decoder_text = row["decoder_text"]
        runtime_scores[decoder_text] = max(runtime_scores.get(decoder_text, 0.0), row["score"])
    runtime_lines = [
        f"{decoder_text} :{score:g}"
        for decoder_text, score in sorted(
            runtime_scores.items(), key=lambda item: (normalized_form(item[0]), item[0])
        )
    ]
    runtime_text = "\n".join(runtime_lines) + ("\n" if runtime_lines else "")
    return (
        "\n".join(lines) + "\n",
        stable_json(source_json, indent=2) + "\n",
        runtime_text,
        len(rows),
        len(runtime_lines),
    )


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def compile_profile(root: Path, profile_id: str, output: Path) -> dict[str, Any]:
    root = root.resolve()
    output = output.resolve()
    resolved = resolve_profile(root, profile_id)
    profile = resolved.config
    terms = resolved.terms
    conflicts = detect_conflicts(terms)
    if conflicts["hard_conflicts"]:
        first = conflicts["hard_conflicts"][0]
        raise LexiconError(
            f"profile {profile_id} has {len(conflicts['hard_conflicts'])} alias conflicts; "
            f"first is {first['alias']!r}: {first['first_canonical']!r} vs {first['second_canonical']!r}"
        )

    semantic = _variant_rows(terms, "speech")
    casing = _variant_rows(terms, "casing")
    release = profile["release"]
    rime_dict, rime_count = _rime_dictionary(terms, release)
    custom_phrase, custom_count = _custom_phrase(terms)
    hotwords, hotwords_json, hotwords_runtime, hotword_count, hotword_runtime_count = (
        _hotword_source(terms, profile_id, release)
    )

    voice_rules = {
        "schema_version": "1.0.0",
        "profile": profile_id,
        "release": release,
        "matching_contract": {
            "selection": "single_pass_leftmost_longest",
            "normalization": "NFKC; ASCII matching is case-insensitive",
            "context_fields": ["positive_context", "negative_context"],
            "risk_field": "risk",
        },
        "semantic": semantic,
        "casing": casing,
    }

    artifacts: dict[str, str] = {
        "voice/semantic.tsv": _structured_tsv(semantic),
        "voice/casing.tsv": _structured_tsv(casing),
        "voice/domain_dict.txt": _legacy_voice_tsv(semantic, casing, profile_id, release),
        "voice/voice_rules.json": stable_json(voice_rules, indent=2) + "\n",
        f"rime/{RIME_DICTIONARY_FILE}": rime_dict,
        "rime/custom_phrase.txt": custom_phrase,
        "speech/hotwords.source.tsv": hotwords,
        "speech/hotwords.source.json": hotwords_json,
        "speech/hotwords.txt": hotwords_runtime,
        "catalog.json": stable_json({"profile": profile_id, "release": release, "terms": terms}, indent=2) + "\n",
        "reports/conflicts.json": stable_json(conflicts, indent=2) + "\n",
    }
    for relative, content in artifacts.items():
        _write(output / relative, content)

    artifact_manifest: dict[str, dict[str, Any]] = {}
    for relative in sorted(artifacts):
        path = output / relative
        artifact_manifest[relative] = {"sha256": sha256_file(path), "bytes": path.stat().st_size}
    source_manifest: dict[str, dict[str, Any]] = {}
    for source in sorted(resolved.source_files):
        relative = source.resolve().relative_to(root).as_posix()
        source_manifest[relative] = {"sha256": sha256_file(source), "bytes": source.stat().st_size}

    source_digest = sha256_bytes(
        "".join(f"{name}\0{meta['sha256']}\n" for name, meta in source_manifest.items()).encode("utf-8")
    )
    generator_files: dict[str, dict[str, Any]] = {}
    for relative in (
        "schema/term.schema.json",
        "tools/compile_lexicon.py",
        "tools/lexicon_lib.py",
        "tools/migrate_legacy.py",
    ):
        path = root / relative
        if not path.is_file():
            raise LexiconError(f"missing reproducibility input {path}")
        generator_files[relative] = {"sha256": sha256_file(path), "bytes": path.stat().st_size}
    generator_digest = sha256_bytes(
        "".join(f"{name}\0{meta['sha256']}\n" for name, meta in generator_files.items()).encode("utf-8")
    )
    manifest = {
        "schema_version": "1.0.0",
        "generator_version": GENERATOR_VERSION,
        "release": release,
        "profile": profile_id,
        "active_packs": profile["packs"],
        "source_digest": source_digest,
        "source_files": source_manifest,
        "generator_digest": generator_digest,
        "generator_files": generator_files,
        "counts": {
            "terms": len(terms),
            "semantic_variants": len(semantic),
            "casing_variants": len(casing),
            "rime_dictionary_entries": rime_count,
            "custom_phrase_entries": custom_count,
            "hotword_entries": hotword_count,
            "hotword_runtime_entries": hotword_runtime_count,
            "hard_conflicts": len(conflicts["hard_conflicts"]),
            "warnings": len(conflicts["warnings"]),
        },
        "artifacts": artifact_manifest,
        "previous_release": None,
        "model_scope": ["sherpa-onnx-x-asr-480ms-streaming-zipformer-transducer-zh-en-punct-int8-2026-06-05"],
        "safety": {
            "unapproved_terms_emitted": 0,
            "profile_filtered": True,
            "legacy_voice_adapter_context_enforcement": False,
            "hotwords_runtime_enabled": hotword_runtime_count > 0,
            "hotwords_runtime_parameters": {
                "decoding_method": "modified_beam_search",
                "max_active_paths": 8,
                "modeling_unit": "bpe",
                "requires_nonempty_bpe_vocab": True,
                "fallback": "greedy_search",
            },
        },
    }
    _write(output / "manifest.json", stable_json(manifest, indent=2) + "\n")
    return manifest
