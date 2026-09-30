#!/usr/bin/env python3
"""Run the public synthetic Python subset without ASR or live recording."""
import subprocess
import sys
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1] / "service"
MODULES = (
    "test_voice_settings",
    "test_voice_audio",
    "test_voice_hotwords",
    "test_voice_personal_phonetics",
    "test_voice_streaming_protocol",
    "test_voice_refiner",
    "test_voice_tool_rules",
)

if __name__ == "__main__":
    raise SystemExit(subprocess.call(
        [sys.executable, "-m", "unittest", *MODULES], cwd=SERVICE
    ))
