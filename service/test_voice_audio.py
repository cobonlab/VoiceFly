import plistlib
import unittest
from pathlib import Path

import numpy as np

from voice_audio import (
    AdaptiveSpeechStartGate,
    input_device_candidates,
    open_first_available_input,
    refresh_device_registry,
)


DEVICES = [
    {"name": "liPods Pro", "max_input_channels": 1, "default_samplerate": 24000.0},
    {"name": "liPods Pro", "max_input_channels": 0, "default_samplerate": 48000.0},
    {"name": "iPhone麦克风", "max_input_channels": 1, "default_samplerate": 48000.0},
    {"name": "MacBook Air麦克风", "max_input_channels": 1, "default_samplerate": 48000.0},
]


class FakeDefault:
    device = (0, 1)


class FakeInputOutputPair:
    def __init__(self, input_index, output_index):
        self.values = (input_index, output_index)

    def __getitem__(self, index):
        return self.values[index]


class FakeStream:
    def __init__(self, module, kwargs):
        self.module = module
        self.kwargs = kwargs
        self.stopped = False
        self.closed = False

    def start(self):
        self.module.attempts.append(self.kwargs)
        if self.kwargs["device"] in self.module.failing_devices:
            raise RuntimeError("device unavailable")

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True


class FakeSoundDevice:
    default = FakeDefault()

    def __init__(self, failing_devices=()):
        self.failing_devices = set(failing_devices)
        self.attempts = []

    def query_devices(self):
        return DEVICES

    def InputStream(self, **kwargs):
        return FakeStream(self, kwargs)


class AudioInputTests(unittest.TestCase):
    def test_canonical_launch_agent_follows_system_default_input(self):
        launch_agent = (
            Path(__file__).resolve().parent
            / "com.simonli.voicefly.service.plist"
        )
        payload = plistlib.loads(launch_agent.read_bytes())
        environment = payload.get("EnvironmentVariables", {})
        self.assertNotEqual(environment.get("VOICEFLY_PREFER_BUILTIN_MIC"), "1")

    def test_default_input_uses_its_native_rate_and_read_size(self):
        candidates = input_device_candidates(DEVICES, (0, 1))
        self.assertEqual(candidates[0].index, 0)
        self.assertEqual(candidates[0].sample_rate, 24000)
        self.assertEqual(candidates[0].read_frames, 960)
        self.assertTrue(candidates[0].is_default)
        self.assertFalse(candidates[0].is_builtin)

    def test_builtin_microphone_is_first_fallback(self):
        candidates = input_device_candidates(DEVICES, (0, 1))
        self.assertEqual([candidate.index for candidate in candidates], [0, 3, 2])

    def test_sounddevice_pair_and_explicit_builtin_preference(self):
        pair = FakeInputOutputPair(2, 1)
        self.assertEqual(input_device_candidates(DEVICES, pair)[0].index, 2)
        self.assertEqual(
            input_device_candidates(DEVICES, pair, prefer_builtin=True)[0].index,
            3,
        )

    def test_output_only_and_invalid_devices_are_filtered(self):
        devices = DEVICES + [
            {"name": "bad", "max_input_channels": 1, "default_samplerate": 0},
            {"name": "broken", "max_input_channels": "?", "default_samplerate": 16000},
        ]
        candidates = input_device_candidates(devices, -1)
        self.assertEqual([candidate.index for candidate in candidates], [3, 0, 2])

    def test_open_retries_builtin_when_default_fails(self):
        sounddevice = FakeSoundDevice(failing_devices={0})
        active = open_first_available_input(sounddevice)
        self.assertEqual(active.candidate.index, 3)
        self.assertEqual(active.selection_reason, "builtin-fallback")
        self.assertEqual([attempt["device"] for attempt in sounddevice.attempts], [0, 3])
        self.assertEqual(sounddevice.attempts[0]["samplerate"], 24000)
        self.assertEqual(sounddevice.attempts[1]["samplerate"], 48000)
        active.close()
        self.assertTrue(active.stream.stopped)
        self.assertTrue(active.stream.closed)

    def test_open_reports_system_default_selection(self):
        sounddevice = FakeSoundDevice()
        active = open_first_available_input(sounddevice)
        self.assertEqual(active.candidate.name, "liPods Pro")
        self.assertEqual(active.selection_reason, "system-default")
        self.assertEqual(sounddevice.attempts[0]["blocksize"], 0)
        self.assertEqual(active.candidate.read_frames, 960)
        active.close()

    def test_open_refreshes_portaudio_before_reading_default_device(self):
        class RefreshingSoundDevice(FakeSoundDevice):
            def __init__(self):
                super().__init__()
                self.default = type("Default", (), {"device": (3, 1)})()
                self.events = []

            def _terminate(self):
                self.events.append("terminate")

            def _initialize(self):
                self.events.append("initialize")
                self.default.device = (0, 1)

            def query_devices(self):
                self.events.append("query")
                return DEVICES

        sounddevice = RefreshingSoundDevice()
        active = open_first_available_input(sounddevice)
        self.assertEqual(sounddevice.events[:3], ["terminate", "initialize", "query"])
        self.assertEqual(active.candidate.name, "liPods Pro")
        self.assertEqual(active.selection_reason, "system-default")
        active.close()

    def test_refresh_is_optional_for_compatible_test_or_audio_modules(self):
        self.assertFalse(refresh_device_registry(FakeSoundDevice()))

    def test_explicit_builtin_override_is_observable(self):
        active = open_first_available_input(
            FakeSoundDevice(),
            prefer_builtin=True,
        )
        self.assertEqual(active.candidate.name, "MacBook Air麦克风")
        self.assertEqual(active.selection_reason, "builtin-override")
        active.close()

    def test_no_input_devices_is_a_clear_error(self):
        sounddevice = FakeSoundDevice()
        sounddevice.query_devices = lambda: []
        with self.assertRaisesRegex(RuntimeError, "没有可用的麦克风"):
            open_first_available_input(sounddevice)


class AdaptiveSpeechStartGateTests(unittest.TestCase):
    sample_rate = 1000
    frame_samples = 20

    @staticmethod
    def frame(level):
        return np.full(20, level, dtype=np.float32)

    @staticmethod
    def voiced_frame(amplitude=0.02):
        phase = np.arange(20, dtype=np.float32)
        return (amplitude * np.sin(2 * np.pi * phase / 10)).astype(np.float32)

    def calibrated_gate(self, noise_level=0.001):
        gate = AdaptiveSpeechStartGate(self.sample_rate)
        for _ in range(gate.settle_frames + gate.calibration_frames):
            self.assertIsNone(gate.process(self.frame(noise_level)))
        self.assertTrue(gate.calibrated)
        self.assertFalse(gate.opened)
        return gate

    def test_low_room_noise_never_opens_or_reaches_decoder(self):
        gate = self.calibrated_gate(0.0001)
        for _ in range(20):
            self.assertIsNone(gate.process(self.frame(0.0001)))
        self.assertFalse(gate.opened)
        self.assertGreaterEqual(gate.suppressed_ms, 500.0)

    def test_digital_zero_waits_for_a_live_microphone_signal(self):
        gate = AdaptiveSpeechStartGate(self.sample_rate)
        for _ in range(30):
            self.assertIsNone(gate.process(self.frame(0.0)))
        self.assertFalse(gate.calibrated)
        self.assertFalse(gate.opened)

        for _ in range(gate.settle_frames + gate.calibration_frames):
            self.assertIsNone(gate.process(self.frame(0.001)))
        self.assertTrue(gate.calibrated)
        self.assertFalse(gate.opened)

    def test_one_startup_spike_is_rejected(self):
        gate = self.calibrated_gate()
        self.assertIsNone(gate.process(self.frame(0.5)))
        self.assertIsNone(gate.process(self.frame(0.001)))
        self.assertIsNone(gate.process(self.frame(0.001)))
        self.assertFalse(gate.opened)

    def test_sustained_speech_opens_and_returns_preroll(self):
        gate = self.calibrated_gate()
        self.assertIsNone(gate.process(self.frame(0.001)))
        for _ in range(gate.confirmation_frames - 1):
            self.assertIsNone(gate.process(self.voiced_frame()))
        accepted = gate.process(self.voiced_frame())
        self.assertTrue(gate.opened)
        self.assertIsNotNone(accepted)
        self.assertGreaterEqual(
            accepted.size,
            (gate.confirmation_frames + 1) * gate.frame_samples,
        )
        np.testing.assert_array_equal(
            gate.process(self.frame(0.003)),
            self.frame(0.003),
        )

    def test_threshold_adapts_to_a_louder_microphone_noise_floor(self):
        gate = self.calibrated_gate(0.02)
        for _ in range(6):
            self.assertIsNone(gate.process(self.frame(0.02)))
        for _ in range(gate.confirmation_frames - 1):
            self.assertIsNone(gate.process(self.voiced_frame(0.08)))
        self.assertIsNotNone(gate.process(self.voiced_frame(0.08)))
        self.assertTrue(gate.opened)

    def test_clear_immediate_speech_survives_calibration(self):
        gate = AdaptiveSpeechStartGate(self.sample_rate)
        accepted = None
        for _ in range(gate.settle_frames + gate.calibration_frames):
            accepted = gate.process(self.voiced_frame())
        self.assertTrue(gate.calibrated)
        self.assertTrue(gate.opened)
        self.assertIsNotNone(accepted)

    def test_quiet_bluetooth_speech_does_not_poison_its_own_calibration(self):
        gate = AdaptiveSpeechStartGate(self.sample_rate)
        accepted = None
        for _ in range(gate.settle_frames + gate.calibration_frames):
            accepted = gate.process(self.voiced_frame(0.002))
        self.assertTrue(gate.calibrated)
        self.assertTrue(gate.opened)
        self.assertIsNotNone(accepted)
        self.assertLessEqual(gate.open_after_ms, 160.0)
        self.assertGreaterEqual(gate.speech_like_frames, gate.confirmation_frames)
        self.assertIsNotNone(gate.peak_level_dbfs)

    def test_one_speech_shaped_startup_impulse_does_not_open_gate(self):
        gate = AdaptiveSpeechStartGate(self.sample_rate)
        frames = [self.frame(0.001)] * (
            gate.settle_frames + gate.calibration_frames
        )
        frames[gate.settle_frames] = self.voiced_frame(0.5)
        for frame in frames:
            self.assertIsNone(gate.process(frame))
        self.assertTrue(gate.calibrated)
        self.assertFalse(gate.opened)

    def test_nonfinite_input_is_sanitized(self):
        gate = self.calibrated_gate()
        bad = np.array([np.nan, np.inf, -np.inf] * 7, dtype=np.float32)
        self.assertIsNone(gate.process(bad))
        self.assertFalse(gate.opened)


if __name__ == "__main__":
    unittest.main()
