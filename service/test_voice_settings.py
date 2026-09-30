import json
import os
from pathlib import Path
import tempfile
import unittest
from voice_settings import prefer_builtin_microphone, write_service_status


class VoiceSettingsTests(unittest.TestCase):
    def test_settings_are_optional_and_strict(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            self.assertFalse(prefer_builtin_microphone(path=path))
            for value, expected in [(True, True), (False, False), ("false", False), (1, False)]:
                path.write_text(json.dumps({"version": 1, "preferBuiltinMicrophone": value}))
                self.assertEqual(prefer_builtin_microphone(path=path), expected)
            path.write_text("{broken")
            self.assertTrue(prefer_builtin_microphone(default=True, path=path))
            path.write_text(json.dumps({"version": 2, "preferBuiltinMicrophone": True}))
            self.assertFalse(prefer_builtin_microphone(path=path))

    def test_health_is_private_and_text_free(self):
        with tempfile.TemporaryDirectory() as directory:
            write_service_status("idle", directory=directory)
            path = Path(directory) / "service-status.json"
            document = json.loads(path.read_text())
            self.assertEqual(document["phase"], "idle")
            self.assertEqual(set(document), {"phase", "updated", "pid", "version"})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(Path(directory).glob(".service-status-*")), [])

    def test_does_not_follow_status_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "protected"
            target.write_text("keep")
            (Path(directory) / "service-status.json").symlink_to(target)
            write_service_status("idle", directory=directory)
            self.assertEqual(target.read_text(), "keep")
