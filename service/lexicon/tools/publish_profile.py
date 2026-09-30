#!/usr/bin/env python3
"""Validate and transactionally publish a compiled Voice Fly lexicon profile.

The compiler output is the only publication input.  This tool deliberately has
no code path for Rime user databases or generated ``build`` binaries.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SQUIRREL = Path(
    "/Library/Input Methods/VoiceFlypy.app/Contents/MacOS/Squirrel"
)
RIME_COMPOSITE_NAME = "luna_pinyin.voiceflypy.dict.yaml"
RIME_CUSTOM_NAME = "double_pinyin_flypy.custom.yaml"
RIME_LEARNED_NAME = "voiceflypy_learned_phrases.txt"

# Compiler artifacts are intentionally flattened at the speech service's live
# root.  Adding a compiler artifact does not implicitly make it publishable.
PUBLISHED_ARTIFACTS: tuple[tuple[str, str, str], ...] = (
    ("rime/voiceflypy_personal.dict.yaml", "rime", "voiceflypy_personal.dict.yaml"),
    ("rime/custom_phrase.txt", "rime", "custom_phrase.txt"),
    ("speech/hotwords.txt", "voice", "hotwords.txt"),
    ("voice/voice_rules.json", "voice", "voice_rules.json"),
    ("voice/domain_dict.txt", "voice", "domain_dict.txt"),
)


class PublishError(RuntimeError):
    """A release is unsafe to publish or a publication transaction failed."""


@dataclass(frozen=True)
class ValidatedRelease:
    root: Path
    build_dir: Path
    manifest_path: Path
    manifest: dict[str, Any]
    profile: str
    release: str


@dataclass(frozen=True)
class CopyOperation:
    source: Path
    target: Path
    group: str
    logical_name: str


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PublishError(f"cannot read valid JSON from {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PublishError(f"expected a JSON object in {path}")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_artifact_path(build_dir: Path, relative: str) -> Path:
    if not relative or "\\" in relative:
        raise PublishError(f"invalid artifact path {relative!r}")
    candidate_rel = Path(relative)
    if candidate_rel.is_absolute() or ".." in candidate_rel.parts or "." in candidate_rel.parts:
        raise PublishError(f"artifact path escapes build directory: {relative!r}")
    candidate = build_dir / candidate_rel
    try:
        candidate.resolve(strict=True).relative_to(build_dir.resolve(strict=True))
    except (FileNotFoundError, ValueError) as exc:
        raise PublishError(f"artifact is missing or escapes build directory: {relative}") from exc
    if candidate.is_symlink() or not candidate.is_file():
        raise PublishError(f"artifact must be a regular non-symlink file: {relative}")
    return candidate


def _require_exact_zero(value: Any, label: str) -> None:
    if type(value) is not int or value != 0:  # bool is deliberately rejected
        raise PublishError(f"{label} must be the integer 0, got {value!r}")


def _assert_rime_dictionary_release(path: Path, release: str) -> None:
    text = path.read_text(encoding="utf-8")
    if not re.search(r"(?m)^name:\s*voiceflypy_personal\s*$", text):
        raise PublishError(f"unexpected Rime dictionary name in {path}")
    version = re.search(r'(?m)^version:\s*["\']?([^"\'\s]+)["\']?\s*$', text)
    if not version or version.group(1) != release:
        found = version.group(1) if version else None
        raise PublishError(
            f"Rime dictionary release mismatch: expected {release!r}, got {found!r}"
        )


def validate_release(
    root: Path,
    profile: str,
    build_dir: Path | None = None,
    expected_release: str | None = None,
) -> ValidatedRelease:
    """Validate identity, safety gates, and every manifest artifact hash."""

    root = root.resolve()
    build_dir = (build_dir or (root / "build" / profile)).resolve()
    manifest_path = build_dir / "manifest.json"
    manifest = _read_json(manifest_path)

    if manifest.get("profile") != profile:
        raise PublishError(
            f"manifest profile mismatch: expected {profile!r}, got {manifest.get('profile')!r}"
        )
    release = manifest.get("release")
    if not isinstance(release, str) or not release.strip() or len(release) > 128:
        raise PublishError(f"manifest release is invalid: {release!r}")

    profile_path = root / "profiles" / f"{profile}.json"
    if profile_path.is_file():
        configured = _read_json(profile_path)
        if configured.get("id") != profile:
            raise PublishError(f"profile id mismatch in {profile_path}")
        if configured.get("release") != release:
            raise PublishError(
                f"profile release mismatch: manifest={release!r}, "
                f"profile={configured.get('release')!r}"
            )
    elif expected_release is None:
        raise PublishError(
            f"profile config is missing ({profile_path}); pass --release for an explicit check"
        )
    if expected_release is not None and release != expected_release:
        raise PublishError(
            f"release mismatch: expected {expected_release!r}, got {release!r}"
        )

    counts = manifest.get("counts")
    safety = manifest.get("safety")
    if not isinstance(counts, dict) or not isinstance(safety, dict):
        raise PublishError("manifest must contain counts and safety objects")
    _require_exact_zero(counts.get("hard_conflicts"), "counts.hard_conflicts")
    _require_exact_zero(
        safety.get("unapproved_terms_emitted"), "safety.unapproved_terms_emitted"
    )

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict) or not artifacts:
        raise PublishError("manifest artifacts must be a non-empty object")
    required = {relative for relative, _, _ in PUBLISHED_ARTIFACTS}
    missing = sorted(required - set(artifacts))
    if missing:
        raise PublishError(f"manifest is missing publish artifacts: {', '.join(missing)}")

    for relative in sorted(artifacts):
        metadata = artifacts[relative]
        if not isinstance(relative, str) or not isinstance(metadata, dict):
            raise PublishError("manifest artifact entries must map paths to objects")
        path = _safe_artifact_path(build_dir, relative)
        expected_hash = metadata.get("sha256")
        expected_bytes = metadata.get("bytes")
        if not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
            raise PublishError(f"invalid SHA-256 metadata for {relative}")
        if type(expected_bytes) is not int or expected_bytes < 0:
            raise PublishError(f"invalid byte metadata for {relative}")
        actual_hash = _sha256(path)
        actual_bytes = path.stat().st_size
        if actual_hash != expected_hash or actual_bytes != expected_bytes:
            raise PublishError(
                f"artifact integrity failure for {relative}: "
                f"sha256={actual_hash}, bytes={actual_bytes}"
            )

    conflicts = _read_json(build_dir / "reports" / "conflicts.json")
    if conflicts.get("hard_conflicts") != []:
        raise PublishError("reports/conflicts.json contains hard conflicts")

    voice_rules = _read_json(build_dir / "voice" / "voice_rules.json")
    if voice_rules.get("profile") != profile or voice_rules.get("release") != release:
        raise PublishError("voice_rules.json profile/release does not match manifest")
    _assert_rime_dictionary_release(
        build_dir / "rime" / "voiceflypy_personal.dict.yaml", release
    )
    if not any(
        line.strip()
        for line in (build_dir / "speech" / "hotwords.txt").read_text(encoding="utf-8").splitlines()
    ):
        raise PublishError("runtime hotwords file is empty")

    return ValidatedRelease(
        root=root,
        build_dir=build_dir,
        manifest_path=manifest_path,
        manifest=manifest,
        profile=profile,
        release=release,
    )


def _mapping_value(text: str, key: str) -> str | None:
    escaped = re.escape(key)
    match = re.search(
        rf"(?m)^\s*[\"']?{escaped}[\"']?\s*:\s*([^\s#]+)\s*(?:#.*)?$", text
    )
    return match.group(1).strip("\"'") if match else None


def _section_mapping_value(text: str, section: str, key: str) -> str | None:
    """Read one scalar from either a slash patch key or a nested YAML section."""

    direct = _mapping_value(text, f"{section}/{key}")
    if direct is not None:
        return direct

    section_indent: int | None = None
    section_pattern = re.compile(rf"^[\"']?{re.escape(section)}[\"']?\s*:\s*(?:#.*)?$")
    value_pattern = re.compile(
        rf"^[\"']?{re.escape(key)}[\"']?\s*:\s*([^\s#]+)\s*(?:#.*)?$"
    )
    for raw_line in text.splitlines():
        content = raw_line.lstrip(" ")
        if not content or content.startswith("#"):
            continue
        indent = len(raw_line) - len(content)
        if section_indent is None:
            if section_pattern.fullmatch(content):
                section_indent = indent
            continue
        if indent <= section_indent:
            return None
        match = value_pattern.fullmatch(content)
        if match:
            return match.group(1).strip("\"'")
    return None


def _section_list_values(text: str, section: str, key: str) -> tuple[str, ...]:
    """Read a block-style scalar list from a nested YAML section."""

    section_indent: int | None = None
    list_indent: int | None = None
    values: list[str] = []
    section_pattern = re.compile(rf"^[\"']?{re.escape(section)}[\"']?\s*:\s*(?:#.*)?$")
    key_pattern = re.compile(rf"^[\"']?{re.escape(key)}[\"']?\s*:\s*(?:#.*)?$")
    item_pattern = re.compile(r"^-\s*([^#]+?)\s*(?:#.*)?$")
    for raw_line in text.splitlines():
        content = raw_line.lstrip(" ")
        if not content or content.startswith("#"):
            continue
        indent = len(raw_line) - len(content)
        if section_indent is None:
            if section_pattern.fullmatch(content):
                section_indent = indent
            continue
        if indent <= section_indent:
            break
        if list_indent is None:
            if key_pattern.fullmatch(content):
                list_indent = indent
            continue
        if indent <= list_indent:
            break
        match = item_pattern.fullmatch(content)
        if match:
            values.append(match.group(1).strip().strip("\"'"))
    return tuple(values)


def assert_rime_mapping(custom_yaml: Path) -> None:
    if custom_yaml.is_symlink() or not custom_yaml.is_file():
        raise PublishError(f"Rime custom YAML is missing or unsafe: {custom_yaml}")
    text = custom_yaml.read_text(encoding="utf-8")
    expected = {
        "translator/dictionary": "luna_pinyin.voiceflypy",
        "translator/user_dict": "luna_pinyin",
    }
    for key, value in expected.items():
        actual = _mapping_value(text, key)
        if actual != value:
            raise PublishError(
                f"{custom_yaml} must already map {key!r} to {value!r}; got {actual!r}"
            )
    required_patterns = {
        "learned phrase translator": r"(?m)^\s*-\s*table_translator@learned_phrases\s*$",
        "learned phrase user_dict": r"(?m)^\s*user_dict:\s*[\"']?voiceflypy_learned_phrases[\"']?\s*(?:#.*)?$",
    }
    for label, pattern in required_patterns.items():
        if not re.search(pattern, text):
            raise PublishError(f"{custom_yaml} is missing {label}; mutable learning would be unsafe")
    learned_quality = _section_mapping_value(text, "learned_phrases", "initial_quality")
    if learned_quality != "1":
        raise PublishError(
            f"{custom_yaml} must set learned_phrases/initial_quality to 1; "
            f"got {learned_quality!r}"
        )
    learned_completion = _section_mapping_value(text, "learned_phrases", "enable_completion")
    if learned_completion != "false":
        raise PublishError(
            f"{custom_yaml} must set learned_phrases/enable_completion to false; "
            f"got {learned_completion!r}"
        )
    short_code_exclusions = _section_list_values(
        text, "learned_phrases", "disable_user_dict_for_patterns"
    )
    if "^[a-z]{1,2}$" not in short_code_exclusions:
        raise PublishError(
            f"{custom_yaml} must exclude one- and two-key legacy learned entries"
        )


def validate_composite(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise PublishError(f"Rime composite source is missing or unsafe: {path}")
    text = path.read_text(encoding="utf-8")
    if not re.search(r"(?m)^name:\s*luna_pinyin\.voiceflypy\s*$", text):
        raise PublishError(f"unexpected composite dictionary name in {path}")
    if not re.search(r"(?m)^\s*-\s*voiceflypy_personal\s*$", text):
        raise PublishError(f"composite dictionary does not import voiceflypy_personal: {path}")
    if not re.search(r"(?m)^\s*-\s*luna_pinyin\s*$", text):
        raise PublishError(f"composite dictionary does not import luna_pinyin: {path}")


def build_plan(
    release: ValidatedRelease,
    rime_dir: Path,
    live_root: Path,
    backup_dir: Path,
    composite_source: Path | None = None,
    custom_yaml: Path | None = None,
) -> tuple[list[CopyOperation], dict[str, Any]]:
    """Build a fully validated plan without writing to any supplied path."""

    rime_dir = rime_dir.resolve()
    live_root = live_root.resolve()
    backup_dir = backup_dir.resolve()
    custom_yaml = (custom_yaml or (rime_dir / RIME_CUSTOM_NAME)).resolve()
    assert_rime_mapping(custom_yaml)

    composite_target = rime_dir / RIME_COMPOSITE_NAME
    if composite_source is not None:
        composite_source = composite_source.resolve()
        validate_composite(composite_source)
    else:
        validate_composite(composite_target)

    protected_names = {"userdb", "build"}
    for destination_root in (rime_dir, live_root):
        if any(part.lower() in protected_names or part.lower().endswith(".userdb") for part in destination_root.parts):
            raise PublishError(f"refusing unsafe publication root: {destination_root}")
    for unsafe in (release.build_dir, rime_dir, live_root):
        if (
            backup_dir == unsafe
            or unsafe in backup_dir.parents
            or backup_dir in unsafe.parents
        ):
            raise PublishError(f"backup directory overlaps a publication/source root: {backup_dir}")
    if (
        rime_dir == live_root
        or rime_dir in live_root.parents
        or live_root in rime_dir.parents
    ):
        raise PublishError("Rime and speech live roots must be separate directories")

    operations: list[CopyOperation] = []
    for relative, group, filename in PUBLISHED_ARTIFACTS:
        destination_root = rime_dir if group == "rime" else live_root
        operations.append(
            CopyOperation(
                source=release.build_dir / relative,
                target=destination_root / filename,
                group=group,
                logical_name=relative,
            )
        )
    operations.append(
        CopyOperation(
            source=release.manifest_path,
            target=live_root / "manifest.json",
            group="voice",
            logical_name="manifest.json",
        )
    )
    if composite_source is not None and composite_source != composite_target.resolve():
        operations.append(
            CopyOperation(
                source=composite_source,
                target=composite_target,
                group="rime",
                logical_name=f"external:{RIME_COMPOSITE_NAME}",
            )
        )

    seen_targets: set[Path] = set()
    for operation in operations:
        if operation.target in seen_targets:
            raise PublishError(f"duplicate publication target: {operation.target}")
        seen_targets.add(operation.target)
        if operation.target.is_symlink() or (operation.target.exists() and not operation.target.is_file()):
            raise PublishError(f"publication target is not a safe regular file: {operation.target}")

    summary = {
        "mode": "dry-run",
        "profile": release.profile,
        "release": release.release,
        "build_dir": str(release.build_dir),
        "rime_dir": str(rime_dir),
        "live_root": str(live_root),
        "backup_dir": str(backup_dir),
        "rime_mapping": str(custom_yaml),
        "composite": str(composite_source or composite_target),
        "protected_user_files": [str(rime_dir / RIME_LEARNED_NAME)],
        "files": [
            {
                "artifact": operation.logical_name,
                "target": str(operation.target),
                "sha256": _sha256(operation.source),
            }
            for operation in operations
        ],
    }
    return operations, summary


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.voiceflypy-publish-", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        with source.open("rb") as source_handle, os.fdopen(descriptor, "wb") as target_handle:
            shutil.copyfileobj(source_handle, target_handle, length=1024 * 1024)
            target_handle.flush()
            os.fsync(target_handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, target)
        _fsync_directory(target.parent)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.voiceflypy-publish-", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise


def _reload_squirrel(squirrel_bin: Path) -> None:
    if squirrel_bin.is_symlink() or not squirrel_bin.is_file() or not os.access(squirrel_bin, os.X_OK):
        raise PublishError(f"Squirrel reload executable is missing or unsafe: {squirrel_bin}")
    completed = subprocess.run(
        [str(squirrel_bin), "--reload"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise PublishError(
            f"Squirrel --reload failed with exit {completed.returncode}"
            + (f": {detail}" if detail else "")
        )


def _rollback(
    operations: list[CopyOperation],
    originals: dict[Path, Path | None],
) -> list[str]:
    errors: list[str] = []
    for operation in reversed(operations):
        if operation.target not in originals:
            continue
        try:
            original = originals[operation.target]
            if original is None:
                if operation.target.exists() or operation.target.is_symlink():
                    operation.target.unlink()
                    _fsync_directory(operation.target.parent)
            else:
                _atomic_copy(original, operation.target)
        except BaseException as exc:  # keep restoring the remaining exact targets
            errors.append(f"{operation.target}: {exc}")
    return errors


def apply_plan(
    release: ValidatedRelease,
    operations: list[CopyOperation],
    summary: dict[str, Any],
    backup_dir: Path,
    squirrel_bin: Path,
) -> dict[str, Any]:
    """Apply one release transaction, restoring all targets on any failure."""

    backup_dir = backup_dir.resolve()
    if backup_dir.exists():
        raise PublishError(f"backup directory already exists: {backup_dir}")
    backup_dir.mkdir(parents=True, mode=0o700)
    originals: dict[Path, Path | None] = {}
    journal_entries: list[dict[str, Any]] = []

    try:
        for index, operation in enumerate(operations):
            target = operation.target
            backup = backup_dir / operation.group / f"{index:02d}-{target.name}"
            if target.exists():
                if target.is_symlink() or not target.is_file():
                    raise PublishError(f"unsafe publication target changed during preflight: {target}")
                _atomic_copy(target, backup)
                originals[target] = backup
            else:
                originals[target] = None
            journal_entries.append(
                {
                    "artifact": operation.logical_name,
                    "target": str(target),
                    "existed": originals[target] is not None,
                    "backup": str(backup.relative_to(backup_dir)) if originals[target] else None,
                    "old_sha256": _sha256(backup) if originals[target] else None,
                    "new_sha256": _sha256(operation.source),
                }
            )

        journal = {
            "schema_version": "1.0.0",
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "profile": release.profile,
            "release": release.release,
            "state": "prepared",
            "entries": journal_entries,
        }
        _write_json_atomic(backup_dir / "publication.json", journal)

        for operation in operations:
            _atomic_copy(operation.source, operation.target)

        try:
            _reload_squirrel(squirrel_bin.resolve())
        except BaseException as reload_error:
            rollback_errors = _rollback(operations, originals)
            second_reload_error: BaseException | None = None
            try:
                _reload_squirrel(squirrel_bin.resolve())
            except BaseException as exc:
                second_reload_error = exc
            journal["state"] = "rolled_back_after_reload_failure"
            journal["failure"] = str(reload_error)
            journal["rollback_errors"] = rollback_errors
            journal["restore_reload_error"] = (
                str(second_reload_error) if second_reload_error is not None else None
            )
            _write_json_atomic(backup_dir / "publication.json", journal)
            details = [str(reload_error)]
            if rollback_errors:
                details.append("rollback errors: " + "; ".join(rollback_errors))
            if second_reload_error is not None:
                details.append(f"reload after restore also failed: {second_reload_error}")
            raise PublishError("; ".join(details)) from reload_error

        journal["state"] = "published"
        journal["completed_at"] = (
            datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        )
        _write_json_atomic(backup_dir / "publication.json", journal)
        result = dict(summary)
        result["mode"] = "applied"
        result["backup_manifest"] = str(backup_dir / "publication.json")
        return result
    except BaseException as exc:
        # Reload failures already performed and journaled their rollback.
        publication = backup_dir / "publication.json"
        already_rolled_back = False
        if publication.is_file():
            try:
                already_rolled_back = _read_json(publication).get("state") == "rolled_back_after_reload_failure"
            except PublishError:
                pass
        if not already_rolled_back and originals:
            rollback_errors = _rollback(operations, originals)
            if rollback_errors:
                raise PublishError(
                    f"publication failed ({exc}); rollback errors: {'; '.join(rollback_errors)}"
                ) from exc
        if isinstance(exc, PublishError):
            raise
        raise PublishError(f"publication failed and was rolled back: {exc}") from exc


def publish(
    *,
    root: Path,
    profile: str,
    rime_dir: Path,
    live_root: Path,
    backup_dir: Path,
    apply: bool = False,
    build_dir: Path | None = None,
    expected_release: str | None = None,
    composite_source: Path | None = None,
    custom_yaml: Path | None = None,
    squirrel_bin: Path = DEFAULT_SQUIRREL,
) -> dict[str, Any]:
    release = validate_release(root, profile, build_dir, expected_release)
    operations, summary = build_plan(
        release,
        rime_dir,
        live_root,
        backup_dir,
        composite_source,
        custom_yaml,
    )
    if not apply:
        return summary
    return apply_plan(release, operations, summary, backup_dir, squirrel_bin)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate and publish one compiled Voice Fly lexicon profile"
    )
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--build-dir", type=Path)
    parser.add_argument("--release", dest="expected_release")
    parser.add_argument("--rime-dir", type=Path, required=True)
    parser.add_argument("--live-root", type=Path, required=True)
    parser.add_argument("--backup-dir", type=Path, required=True)
    parser.add_argument(
        "--composite-source",
        type=Path,
        help=(
            "optional validated luna_pinyin.voiceflypy source to publish; "
            "without it the target composite must already exist"
        ),
    )
    parser.add_argument(
        "--custom-yaml",
        type=Path,
        help="existing patched custom YAML to assert (default: RIME_DIR/double_pinyin_flypy.custom.yaml)",
    )
    parser.add_argument("--squirrel-bin", type=Path, default=DEFAULT_SQUIRREL)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="perform the transaction; without this flag the command is read-only",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = publish(
            root=args.root,
            profile=args.profile,
            build_dir=args.build_dir,
            expected_release=args.expected_release,
            rime_dir=args.rime_dir,
            live_root=args.live_root,
            backup_dir=args.backup_dir,
            composite_source=args.composite_source,
            custom_yaml=args.custom_yaml,
            squirrel_bin=args.squirrel_bin,
            apply=args.apply,
        )
    except PublishError as exc:
        print(f"publish_profile: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
