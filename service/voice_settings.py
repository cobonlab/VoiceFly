"""Small local-only settings and health files; never include dictation text."""
import json
import os
from pathlib import Path
import tempfile
import time


def support_directory():
    return Path.home() / "Library/Application Support/VoiceFly"


def prefer_builtin_microphone(default=False, path=None):
    path = Path(path) if path else support_directory() / "settings.json"
    try:
        if path.is_symlink() or path.stat().st_size > 64 * 1024:
            return default
        settings = json.loads(path.read_text(encoding="utf-8"))
        if settings.get("version") != 1:
            return default
        value = settings.get("preferBuiltinMicrophone")
        return value if isinstance(value, bool) else default
    except (OSError, ValueError, TypeError, AttributeError):
        return default


def write_service_status(phase, *, error=None, stage=None, stage_seconds=None, directory=None):
    root = Path(directory) if directory else support_directory()
    if root.is_symlink():
        return
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    document = {"version": "0.2.0", "updated": time.time(), "pid": os.getpid(), "phase": phase}
    if error:
        # Caller supplies a fixed category, never an exception or recognized text.
        document["error"] = error
    if stage is not None:
        document["stage"] = stage
        document["stage_seconds"] = round(max(0, float(stage_seconds or 0)), 3)
    destination = root / "service-status.json"
    if destination.is_symlink():
        return
    fd, temporary = tempfile.mkstemp(prefix=".service-status-", dir=root)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(document, handle)
        os.replace(temporary, destination)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def write_audio_recovery(stage, elapsed, *, directory=None):
    """Keep only the latest fixed-category recovery record; never session text."""
    if stage not in {"finalizing", "microphone-close", "capture-stop"}:
        raise ValueError("unknown recovery stage")
    root = Path(directory) if directory else support_directory()
    if root.is_symlink():
        return
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = root / "last-audio-recovery.json"
    if destination.is_symlink():
        return
    document = {"updated": time.time(), "pid": os.getpid(), "stage": stage,
                "elapsed_seconds": round(max(0, float(elapsed)), 3), "reason": "audio_timeout"}
    fd, temporary = tempfile.mkstemp(prefix=".audio-recovery-", dir=root)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(document, handle)
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
