#!/usr/bin/env python3
"""Compile one Voice Fly lexicon profile into isolated runtime artifacts."""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Sequence

from lexicon_lib import (
    LexiconError,
    compile_profile,
    resolve_profile,
    sha256_file,
    stable_json,
)


def _source_fingerprints(root: Path, resolved) -> dict[str, dict[str, object]]:
    root = root.resolve()
    return {
        source.resolve().relative_to(root).as_posix(): {
            "sha256": sha256_file(source),
            "bytes": source.stat().st_size,
        }
        for source in sorted(resolved.source_files)
    }


def _require_public_terms(profile: str, resolved) -> None:
    """Reject, rather than filter, a mixed or empty public generation."""
    if not resolved.terms:
        raise LexiconError(f"public export refused: profile {profile!r} has no active terms")
    restricted = Counter(
        term["visibility"] for term in resolved.terms if term["visibility"] != "public"
    )
    if restricted:
        summary = ", ".join(
            f"{visibility}={count}" for visibility, count in sorted(restricted.items())
        )
        raise LexiconError(
            f"public export refused: profile {profile!r} contains non-public terms ({summary})"
        )


def compile_public_profile(root: Path, profile: str, output: Path) -> dict[str, object]:
    """Compile a stable all-public generation without exposing a partial result.

    Ordinary local compilation deliberately retains its existing semantics.
    This stricter entry point is opt-in for artifacts intended for a public
    release.  It rejects mixed visibility instead of silently dropping terms.
    """
    root = root.resolve()
    output = output.resolve()
    if output.exists():
        raise LexiconError(
            f"public export output already exists; refusing non-atomic overwrite: {output}"
        )

    before = resolve_profile(root, profile)
    _require_public_terms(profile, before)
    before_fingerprints = _source_fingerprints(root, before)

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        dir=output.parent,
        prefix=f".{output.name}.public-export.",
    ) as temporary:
        candidate = Path(temporary) / "candidate"
        manifest = compile_profile(root, profile, candidate)

        # Sources can change while a larger profile is compiling.  Re-resolve
        # and compare bytes before publishing so a checked public epoch cannot
        # install artifacts built from a different or newly private epoch.
        after = resolve_profile(root, profile)
        _require_public_terms(profile, after)
        after_fingerprints = _source_fingerprints(root, after)
        if (
            before_fingerprints != after_fingerprints
            or manifest.get("source_files") != before_fingerprints
        ):
            raise LexiconError(
                f"public export refused: profile {profile!r} changed during compilation"
            )
        os.replace(candidate, output)
    return manifest


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--profile", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--public-export",
        action="store_true",
        help="fail closed unless every active term is explicitly public",
    )
    args = parser.parse_args(argv)
    try:
        if args.public_export:
            manifest = compile_public_profile(args.root, args.profile, args.output)
        else:
            manifest = compile_profile(args.root, args.profile, args.output)
    except (LexiconError, OSError) as exc:
        print(f"lexicon compile failed: {exc}", file=sys.stderr)
        return 2
    print(
        stable_json(
            {
                "profile": manifest["profile"],
                "release": manifest["release"],
                "counts": manifest["counts"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
