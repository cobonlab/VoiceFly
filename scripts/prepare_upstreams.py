#!/usr/bin/env python3
"""Prepare official pinned source checkouts without installing the input method."""
import argparse
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def git(directory, *args, capture=False):
    return subprocess.run(
        ["git", "-C", str(directory), *args], check=True,
        text=True, capture_output=capture
    ).stdout

def prepare(recursive=False):
    manifest = json.loads((ROOT / "SOURCE_MANIFEST.json").read_text())
    for dependency in manifest["dependencies"]:
        name = dependency["name"]
        if name not in {"librime", "Sparkle", "plum"}:
            raise ValueError("unexpected upstream dependency")
        directory = ROOT / "input-method" / name
        if directory.exists():
            toplevel = Path(git(directory, "rev-parse", "--show-toplevel", capture=True).strip())
            if toplevel.resolve() != directory.resolve():
                raise ValueError(f"{name}: existing directory is not its own checkout")
            actual = git(directory, "rev-parse", "HEAD", capture=True).strip()
            if actual != dependency["commit"]:
                raise ValueError(f"{name}: existing checkout is at another revision")
            changes = git(directory, "status", "--porcelain", capture=True).strip()
            patch = ROOT / "patches" / f"{name}.patch"
            if changes:
                # Only the exact known patch may be reused. Other changes fail.
                diff = git(directory, "diff", "--no-ext-diff", "--no-textconv", "--abbrev=8", "HEAD", "--", capture=True)
                untracked = git(directory, "ls-files", "--others", "--exclude-standard", capture=True)
                if not patch.is_file() or untracked or diff != patch.read_text():
                    raise ValueError(f"{name}: existing checkout has modifications")
        else:
            subprocess.run(["git", "clone", "--filter=blob:none", "--no-checkout",
                            dependency["url"], str(directory)], check=True)
            git(directory, "checkout", "--detach", dependency["commit"])
        patch = ROOT / "patches" / f"{name}.patch"
        if patch.is_file():
            reverse = subprocess.run(["git", "-C", str(directory), "apply", "--reverse",
                                      "--check", str(patch)], capture_output=True)
            if reverse.returncode:
                git(directory, "apply", "--check", str(patch))
                git(directory, "apply", str(patch))
        if recursive:
            git(directory, "submodule", "update", "--init", "--recursive")
        print(f"Prepared {name} at {dependency['commit']}", flush=True)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--recursive", action="store_true")
    prepare(parser.parse_args().recursive)
