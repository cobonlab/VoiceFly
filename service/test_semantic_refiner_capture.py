import os
import hashlib
import io
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock
import wave
from contextlib import redirect_stderr

import numpy as np

import semantic_refiner_capture as CAPTURE


def samples(frame_count, _sample_rate):
    return np.linspace(-0.5, 0.5, frame_count, dtype=np.float32)


class CaptureValidationTests(unittest.TestCase):
    def test_only_the_24_fixed_ids_are_allowed(self):
        expected = {
            f"{group}{number:02d}"
            for group in "CVH"
            for number in range(1, 9)
        }
        self.assertEqual(CAPTURE.ALLOWED_CLIP_IDS, expected)
        for invalid in ("C00", "C09", "V09", "H09", "c01", "../C01", "C01/x"):
            with self.subTest(invalid=invalid), self.assertRaises(CAPTURE.CaptureError):
                CAPTURE._validate_clip_id(invalid)

    def test_duration_is_finite_and_between_one_and_twelve_seconds(self):
        for invalid in (True, 0.999, 12.001, float("nan"), float("inf"), "bad"):
            with self.subTest(invalid=invalid), self.assertRaises(CAPTURE.CaptureError):
                CAPTURE._validate_duration(invalid)
        self.assertEqual(CAPTURE._validate_duration(1)[1], 16_000)
        self.assertEqual(CAPTURE._validate_duration(12)[1], 192_000)

    def test_relative_traversal_and_symlink_roots_are_rejected(self):
        with self.assertRaisesRegex(CAPTURE.CaptureError, "absolute"):
            CAPTURE.capture_one("C01", "relative/output", 1, audio_source=samples)
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            real = parent / "real"
            real.mkdir(mode=0o700)
            link = parent / "link"
            link.symlink_to(real, target_is_directory=True)
            with self.assertRaisesRegex(CAPTURE.CaptureError, "symbolic link"):
                CAPTURE.capture_one("C01", link, 1, audio_source=samples)
            traversal = parent / "real" / ".." / "escape"
            with self.assertRaisesRegex(CAPTURE.CaptureError, "traversal"):
                CAPTURE.capture_one("C01", traversal, 1, audio_source=samples)

    def test_existing_root_must_be_private(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "dataset"
            root.mkdir(mode=0o755)
            os.chmod(root, 0o755)
            with self.assertRaisesRegex(CAPTURE.CaptureError, "0700"):
                CAPTURE.capture_one("C01", root, 1, audio_source=samples)


class CapturePersistenceTests(unittest.TestCase):
    def test_success_is_fixed_pcm16_atomic_private_and_manifest_free(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "dataset"
            messages = []
            target = CAPTURE.capture_one(
                "C01",
                root,
                1,
                audio_source=samples,
                emit=messages.append,
            )
            self.assertEqual(target, root.resolve() / "C01.wav")
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.assertEqual(sorted(path.name for path in root.iterdir()), ["C01.wav"])
            self.assertFalse((root / "manifest.json").exists())
            with wave.open(str(target), "rb") as reader:
                self.assertEqual(reader.getnchannels(), 1)
                self.assertEqual(reader.getsampwidth(), 2)
            self.assertEqual(reader.getframerate(), 16_000)
            self.assertEqual(reader.getnframes(), 16_000)
            self.assertTrue(any("未播放、未上传" in message for message in messages))
            expected_hash = hashlib.sha256(target.read_bytes()).hexdigest()
            self.assertTrue(
                any(f"sha256={expected_hash}" in message for message in messages)
            )

    def test_existing_target_is_rejected_before_audio_and_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "dataset"
            root.mkdir(mode=0o700)
            target = root / "V03.wav"
            target.write_bytes(b"keep-me")
            os.chmod(target, 0o600)
            source = mock.Mock(side_effect=AssertionError("must not record"))
            with self.assertRaisesRegex(CAPTURE.CaptureError, "already exists"):
                CAPTURE.capture_one("V03", root, 1, audio_source=source)
            self.assertEqual(target.read_bytes(), b"keep-me")
            source.assert_not_called()

    def test_target_created_during_capture_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "dataset"
            root.mkdir(mode=0o700)

            def racing_source(frame_count, _sample_rate):
                target = root / "H08.wav"
                target.write_bytes(b"racer")
                os.chmod(target, 0o600)
                return np.zeros(frame_count, dtype=np.float32)

            with self.assertRaisesRegex(CAPTURE.CaptureError, "already exists"):
                CAPTURE.capture_one("H08", root, 1, audio_source=racing_source)
            self.assertEqual((root / "H08.wav").read_bytes(), b"racer")
            self.assertFalse(any(path.suffix == ".tmp" for path in root.iterdir()))

    def test_root_replaced_during_capture_does_not_redirect_publish(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root = parent / "dataset"
            root.mkdir(mode=0o700)
            bound_root = parent / "original-dataset"

            def replacing_source(frame_count, _sample_rate):
                root.rename(bound_root)
                root.mkdir(mode=0o700)
                os.chmod(root, 0o700)
                return np.zeros(frame_count, dtype=np.float32)

            CAPTURE.capture_one(
                "C03",
                root,
                1,
                audio_source=replacing_source,
                emit=lambda _message: None,
            )

            self.assertTrue((bound_root / "C03.wav").is_file())
            self.assertFalse((root / "C03.wav").exists())
            self.assertEqual(list(root.iterdir()), [])

    def test_invalid_audio_cleans_up_without_publishing(self):
        invalid_sources = (
            lambda frames, rate: np.zeros((frames, 2), dtype=np.float32),
            lambda frames, rate: np.full(frames, np.nan, dtype=np.float32),
            lambda frames, rate: np.zeros(frames - 1, dtype=np.float32),
            lambda frames, rate: CAPTURE.AudioCapture(
                np.zeros(frames, dtype=np.float32), sample_rate=48_000
            ),
        )
        for index, source in enumerate(invalid_sources):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "dataset"
                with self.assertRaises(CAPTURE.CaptureError):
                    CAPTURE.capture_one("C02", root, 1, audio_source=source)
                self.assertFalse((root / "C02.wav").exists())
                self.assertEqual(list(root.iterdir()), [])

    def test_completed_temp_counts_toward_limit_and_is_cleaned_on_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "dataset"
            root.mkdir(mode=0o700)
            expected_wav_size = 44 + 16_000 * 2

            def growing_source(frame_count, _sample_rate):
                filler = root / "existing.bin"
                filler.write_bytes(b"x" * 101)
                os.chmod(filler, 0o600)
                return np.zeros(frame_count, dtype=np.float32)

            with mock.patch.object(
                CAPTURE,
                "DATASET_HARD_LIMIT_BYTES",
                expected_wav_size + 100,
            ):
                with self.assertRaisesRegex(CAPTURE.CaptureError, "hard limit"):
                    CAPTURE.capture_one("V08", root, 1, audio_source=growing_source)
            self.assertFalse((root / "V08.wav").exists())
            self.assertEqual([path.name for path in root.iterdir()], ["existing.bin"])

    def test_root_replacement_cannot_redirect_capacity_scan_or_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root = parent / "dataset"
            root.mkdir(mode=0o700)
            bound_root = parent / "original-dataset"
            expected_wav_size = 44 + 16_000 * 2

            def replacing_and_growing_source(frame_count, _sample_rate):
                root.rename(bound_root)
                root.mkdir(mode=0o700)
                os.chmod(root, 0o700)
                filler = bound_root / "existing.bin"
                filler.write_bytes(b"x" * 101)
                os.chmod(filler, 0o600)
                return np.zeros(frame_count, dtype=np.float32)

            with mock.patch.object(
                CAPTURE,
                "DATASET_HARD_LIMIT_BYTES",
                expected_wav_size + 100,
            ):
                with self.assertRaisesRegex(CAPTURE.CaptureError, "hard limit"):
                    CAPTURE.capture_one(
                        "V07",
                        root,
                        1,
                        audio_source=replacing_and_growing_source,
                        emit=lambda _message: None,
                    )

            self.assertEqual(
                [path.name for path in bound_root.iterdir()],
                ["existing.bin"],
            )
            self.assertEqual(list(root.iterdir()), [])


class DefaultAudioSourceTests(unittest.TestCase):
    def test_default_source_announces_device_countdown_and_recording(self):
        class Default:
            device = (0, 1)

        class Stream:
            def __init__(self, options):
                self.options = options
                self.started = False
                self.stopped = False
                self.closed = False

            def start(self):
                self.started = True

            def read(self, frames):
                return np.zeros((frames, 1), dtype=np.float32), False

            def stop(self):
                self.stopped = True

            def close(self):
                self.closed = True

        class SoundDevice:
            default = Default()

            def __init__(self):
                self.streams = []

            def query_devices(self):
                return [
                    {
                        "name": "Fake Mac microphone",
                        "max_input_channels": 1,
                        "default_samplerate": 48_000,
                    }
                ]

            def InputStream(self, **options):
                stream = Stream(options)
                self.streams.append(stream)
                return stream

        sounddevice = SoundDevice()
        messages = []
        sleeps = []
        capture = CAPTURE._sounddevice_audio_source(
            16_000,
            16_000,
            countdown_seconds=2,
            emit=messages.append,
            sounddevice_module=sounddevice,
            sleep=sleeps.append,
        )
        self.assertEqual(np.asarray(capture.samples).shape, (16_000, 1))
        self.assertEqual(sounddevice.streams[0].options["channels"], 1)
        self.assertEqual(sounddevice.streams[0].options["dtype"], "float32")
        self.assertEqual(sounddevice.streams[0].options["samplerate"], 16_000)
        self.assertEqual(sleeps, [1.0, 1.0])
        self.assertTrue(any("已选输入设备" in message for message in messages))
        self.assertTrue(any("开始录音" in message for message in messages))
        self.assertTrue(any("录音结束" in message for message in messages))
        self.assertTrue(sounddevice.streams[0].stopped)
        self.assertTrue(sounddevice.streams[0].closed)

    def test_audio_backend_discovery_failures_are_capture_errors(self):
        real_import = __import__

        def fail_sounddevice(name, *args, **kwargs):
            if name == "sounddevice":
                raise ModuleNotFoundError("private import detail")
            return real_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=fail_sounddevice):
            with self.assertRaisesRegex(CAPTURE.CaptureError, "ModuleNotFoundError"):
                CAPTURE._sounddevice_audio_source(16_000, 16_000)

        def fail_voice_audio(name, *args, **kwargs):
            if name == "voice_audio":
                raise RuntimeError("private helper detail")
            return real_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=fail_voice_audio):
            with self.assertRaisesRegex(CAPTURE.CaptureError, "RuntimeError"):
                CAPTURE._sounddevice_audio_source(
                    16_000,
                    16_000,
                    sounddevice_module=object(),
                )

        sounddevice = mock.Mock()
        sounddevice.query_devices.side_effect = OSError("private device detail")
        with self.assertRaisesRegex(CAPTURE.CaptureError, "OSError") as raised:
            CAPTURE._sounddevice_audio_source(
                16_000,
                16_000,
                sounddevice_module=sounddevice,
            )
        self.assertNotIn("private device detail", str(raised.exception))

    def test_cli_reports_backend_import_failure_without_traceback(self):
        real_import = __import__

        def fail_sounddevice(name, *args, **kwargs):
            if name == "sounddevice":
                raise ModuleNotFoundError("private import detail")
            return real_import(name, *args, **kwargs)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "dataset"
            stderr = io.StringIO()
            with mock.patch("builtins.__import__", side_effect=fail_sounddevice):
                with redirect_stderr(stderr):
                    result = CAPTURE.main(
                        [
                            "H01",
                            "--output-root",
                            str(root),
                            "--duration",
                            "1",
                            "--countdown",
                            "0",
                        ],
                        emit=lambda _message: None,
                    )

        rendered = stderr.getvalue()
        self.assertEqual(result, 2)
        self.assertIn("录音失败", rendered)
        self.assertIn("ModuleNotFoundError", rendered)
        self.assertNotIn("Traceback", rendered)
        self.assertNotIn("private import detail", rendered)


if __name__ == "__main__":
    unittest.main()
