#!/usr/bin/env python3
"""Append privacy-minimized review evidence to a local JSONL feedback log.

This recorder cannot promote terms or rewrite compiler inputs.  An explicit
human review must translate accepted evidence into the master JSONL packs.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[1]
CHANNELS = ("keyboard", "speech")
ACTIONS = ("accept", "reject", "undo")
MAX_SPAN_CHARS = 256


class FeedbackError(RuntimeError):
    """Feedback input or destination violates the append-only contract."""


def _normalize_timestamp(value: str | None) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
            "+00:00", "Z"
        )
    candidate = value.strip()
    try:
        parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FeedbackError(f"invalid RFC 3339 timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise FeedbackError("timestamp must include a UTC offset")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _validate_span(value: str, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise FeedbackError(f"{label} must be a non-empty span")
    if len(value) > MAX_SPAN_CHARS:
        raise FeedbackError(
            f"{label} is longer than {MAX_SPAN_CHARS} characters; pass only the corrected span"
        )
    if any(character in value for character in ("\n", "\r", "\x00")):
        raise FeedbackError(f"{label} cannot contain line breaks or NUL bytes")
    return value


def _validate_identifier(value: str, label: str, maximum: int = 128) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        rf"[A-Za-z0-9][A-Za-z0-9._-]{{0,{maximum - 1}}}", value
    ):
        raise FeedbackError(f"invalid {label}: {value!r}")
    return value


def build_event(
    *,
    channel: str,
    action: str,
    raw_span: str,
    canonical: str,
    domain: str,
    profile: str = "simon_all",
    release: str = "unreleased",
    timestamp: str | None = None,
    undoes_event_id: str | None = None,
) -> dict[str, Any]:
    """Create a deterministic event; equal explicit inputs yield one event ID."""

    if channel not in CHANNELS:
        raise FeedbackError(f"channel must be one of {', '.join(CHANNELS)}")
    if action not in ACTIONS:
        raise FeedbackError(
            "action must be accept, reject, or undo; feedback cannot directly promote production"
        )
    raw_span = _validate_span(raw_span, "raw_span")
    canonical = _validate_span(canonical, "canonical")
    domain = _validate_identifier(domain, "domain", 64).lower()
    profile = _validate_identifier(profile, "profile", 64)
    release = _validate_identifier(release, "release", 128)
    occurred_at = _normalize_timestamp(timestamp)

    if action == "undo":
        if undoes_event_id is None:
            raise FeedbackError("undo requires --undoes-event-id")
        if not re.fullmatch(r"vff_[0-9a-f]{32}", undoes_event_id):
            raise FeedbackError(f"invalid undoes_event_id: {undoes_event_id!r}")
    elif undoes_event_id is not None:
        raise FeedbackError("--undoes-event-id is valid only for an undo event")

    stable_body: dict[str, Any] = {
        "schema_version": "1.0.0",
        "occurred_at": occurred_at,
        "channel": channel,
        "action": action,
        "raw_span": raw_span,
        "canonical": canonical,
        "domain": domain,
        "profile": profile,
        "release": release,
        "undoes_event_id": undoes_event_id,
        "privacy": {
            "storage": "local_private",
            "content": "span_only",
            "whole_sentence_stored": False,
        },
        "production_effect": "none_pending_review",
    }
    encoded = json.dumps(
        stable_body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    event_id = "vff_" + hashlib.sha256(encoded).hexdigest()[:32]
    return {"event_id": event_id, **stable_body}


def _assert_feedback_destination(log_path: Path, master_root: Path) -> None:
    if log_path.is_symlink() or (log_path.exists() and not log_path.is_file()):
        raise FeedbackError(f"feedback log must be a regular non-symlink file: {log_path}")
    resolved = log_path.resolve()
    root = master_root.resolve()
    try:
        relative_to_master = resolved.relative_to(root)
    except ValueError:
        relative_to_master = None
    if relative_to_master is not None:
        if not relative_to_master.parts or relative_to_master.parts[0] != "feedback":
            raise FeedbackError(
                f"inside the master root, feedback may be written only under feedback/: {resolved}"
            )
    if resolved.name in {"manifest.json", "voice_rules.json", "domain_dict.txt", "hotwords.txt"}:
        raise FeedbackError(f"feedback log cannot target a runtime artifact: {resolved}")
    if resolved.suffix != ".jsonl":
        raise FeedbackError("feedback log must use a .jsonl suffix")


def _validate_event_contract(event: dict[str, Any]) -> None:
    if not isinstance(event, dict):
        raise FeedbackError("event must be an object")
    try:
        rebuilt = build_event(
            channel=event["channel"],
            action=event["action"],
            raw_span=event["raw_span"],
            canonical=event["canonical"],
            domain=event["domain"],
            profile=event["profile"],
            release=event["release"],
            timestamp=event["occurred_at"],
            undoes_event_id=event["undoes_event_id"],
        )
    except KeyError as exc:
        raise FeedbackError(f"event is missing required field {exc.args[0]!r}") from exc
    if event != rebuilt:
        raise FeedbackError(
            "event does not match the privacy-safe recorder contract or stable event ID"
        )


def append_event(
    log_path: Path,
    event: dict[str, Any],
    *,
    master_root: Path = ROOT,
) -> bool:
    """Append once under an exclusive lock and fsync; return False if duplicate."""

    _validate_event_contract(event)
    _assert_feedback_destination(log_path, master_root)
    parent = log_path.parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    was_present = log_path.exists()
    descriptor = os.open(
        log_path,
        os.O_RDWR | os.O_APPEND | os.O_CREAT | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "r+b", closefd=False) as handle:
            event_id = event.get("event_id")
            handle.seek(0)
            for line_number, raw_line in enumerate(handle, 1):
                if not raw_line.strip():
                    continue
                try:
                    prior = json.loads(raw_line.decode("utf-8"))
                except (UnicodeError, json.JSONDecodeError) as exc:
                    raise FeedbackError(
                        f"existing feedback log has invalid JSON at line {line_number}"
                    ) from exc
                if isinstance(prior, dict) and prior.get("event_id") == event_id:
                    return False

            payload = (
                json.dumps(
                    event,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
            handle.seek(0, os.SEEK_END)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(descriptor)

    if not was_present:
        directory_descriptor = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    return True


def record(
    *,
    log_path: Path,
    channel: str,
    action: str,
    raw_span: str,
    canonical: str,
    domain: str,
    profile: str = "simon_all",
    release: str = "unreleased",
    timestamp: str | None = None,
    undoes_event_id: str | None = None,
    master_root: Path = ROOT,
) -> tuple[dict[str, Any], bool]:
    event = build_event(
        channel=channel,
        action=action,
        raw_span=raw_span,
        canonical=canonical,
        domain=domain,
        profile=profile,
        release=release,
        timestamp=timestamp,
        undoes_event_id=undoes_event_id,
    )
    appended = append_event(log_path, event, master_root=master_root)
    return event, appended


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Append span-only evidence for later master-lexicon review"
    )
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--channel", choices=CHANNELS, required=True)
    parser.add_argument("--action", choices=ACTIONS, required=True)
    parser.add_argument("--raw", dest="raw_span", required=True, help="incorrect/input span only")
    parser.add_argument("--canonical", required=True, help="desired canonical span only")
    parser.add_argument("--domain", required=True)
    parser.add_argument("--profile", default="simon_all")
    parser.add_argument("--release", default="unreleased")
    parser.add_argument("--timestamp", help="explicit RFC 3339 timestamp (mainly for import/tests)")
    parser.add_argument("--undoes-event-id")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        event, appended = record(
            log_path=args.log,
            channel=args.channel,
            action=args.action,
            raw_span=args.raw_span,
            canonical=args.canonical,
            domain=args.domain,
            profile=args.profile,
            release=args.release,
            timestamp=args.timestamp,
            undoes_event_id=args.undoes_event_id,
        )
    except FeedbackError as exc:
        print(f"record_feedback: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {"appended": appended, "event": event},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
