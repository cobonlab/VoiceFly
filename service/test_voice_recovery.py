import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from voice_recovery import RecoveryDeadline
from voice_settings import write_audio_recovery, write_service_status
from voice_audio import ActiveInput


class RecoveryTests(unittest.TestCase):
    def test_failed_stop_still_closes_but_error_is_not_hidden(self):
        stream = mock.Mock()
        stream.stop.side_effect = RuntimeError("stop failed")
        active = ActiveInput(stream, None, "test")
        with self.assertRaises(RuntimeError):
            active.close()
        stream.close.assert_called_once()

    def test_failed_close_is_not_reported_as_success(self):
        stream = mock.Mock()
        stream.close.side_effect = RuntimeError("close failed")
        with self.assertRaises(RuntimeError):
            ActiveInput(stream, None, "test").close()
    def test_normal_completion_cancels_timeout(self):
        fired = threading.Event()
        deadline = RecoveryDeadline(0.04, fired.set)
        self.assertTrue(deadline.finish())
        self.assertFalse(fired.wait(0.1))

    def test_blocked_operation_expires_once_and_cannot_report_success(self):
        fired = threading.Event()
        deadline = RecoveryDeadline(0.02, fired.set)
        self.assertTrue(fired.wait(1))
        self.assertFalse(deadline.finish())
        self.assertFalse(deadline.finish())

    def test_old_finished_deadline_does_not_affect_later_work(self):
        old = mock.Mock()
        earlier = RecoveryDeadline(0.02, old)
        earlier.finish()
        fired = threading.Event()
        later = RecoveryDeadline(0.02, fired.set)
        self.assertTrue(fired.wait(1))
        old.assert_not_called()
        self.assertFalse(later.finish())

    def test_blocked_stop_exits_process_and_next_process_can_start(self):
        code = '''
import os, threading
from voice_recovery import RecoveryDeadline
guard = RecoveryDeadline(0.05, lambda: os._exit(75))
# Model the observed native stop call that never returns. It must not make
# the process look idle or strand the next session.
threading.Event().wait()
'''
        result = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).parent,
                                timeout=3, capture_output=True)
        self.assertEqual(result.returncode, 75)
        result = subprocess.run([sys.executable, "-c", "print('ready')"], timeout=3,
                                capture_output=True, text=True)
        self.assertEqual(result.stdout.strip(), "ready")

    def test_recovery_record_is_bounded_private_and_text_free(self):
        with tempfile.TemporaryDirectory() as directory:
            write_audio_recovery("microphone-close", 3.1, directory=directory)
            write_audio_recovery("finalizing", 6.1, directory=directory)
            path = Path(directory)/"last-audio-recovery.json"
            data = json.loads(path.read_text())
            self.assertEqual(set(data), {"updated", "pid", "stage", "elapsed_seconds", "reason"})
            self.assertEqual(data["stage"], "finalizing")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(len(list(Path(directory).iterdir())), 1)
            with self.assertRaises(ValueError):
                write_audio_recovery("untrusted text", 1, directory=directory)

    def test_recovery_record_does_not_follow_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)/"keep"
            target.write_text("unchanged")
            (Path(directory)/"last-audio-recovery.json").symlink_to(target)
            write_audio_recovery("microphone-close", 3, directory=directory)
            self.assertEqual(target.read_text(), "unchanged")

    def test_health_reports_stage_time_without_dictation(self):
        with tempfile.TemporaryDirectory() as directory:
            write_service_status("finalizing", stage="microphone-close", stage_seconds=2.5,
                                 directory=directory)
            data = json.loads((Path(directory)/"service-status.json").read_text())
            self.assertEqual(data["stage_seconds"], 2.5)
            self.assertEqual(data["stage"], "microphone-close")
            self.assertNotIn("text", data)
