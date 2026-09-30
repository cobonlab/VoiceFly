import io
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

import semantic_refiner_probe as PROBE
import voice_refiner as REFINER


class StaticBackend:
    name = "static-test"

    def __init__(self, candidate_text="我是一个紧凶型的牌手"):
        self.candidate_text = candidate_text
        self.requests = []

    def refine(self, request, *, timeout_seconds):
        self.requests.append((request, timeout_seconds))
        return REFINER.BackendCandidate(
            text=self.candidate_text,
            backend=self.name,
            confidence=0.95,
        )


def write_stereo_wav(path: Path) -> None:
    frames = np.array(
        [
            [32767, -32768],
            [16384, 16384],
            [-16384, -16384],
            [0, 0],
        ],
        dtype="<i2",
    )
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(2)
        writer.setsampwidth(2)
        writer.setframerate(4)
        writer.writeframes(frames.tobytes())


class WavLoadingTests(unittest.TestCase):
    def test_pcm_stereo_is_mixed_into_immutable_audio_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private-utterance.wav"
            write_stereo_wav(path)
            loaded = PROBE.load_pcm_wav(path, max_audio_seconds=2)

        self.assertEqual(loaded.source_channels, 2)
        self.assertEqual(loaded.source_sample_width_bits, 16)
        self.assertEqual(loaded.snapshot.sample_rate, 4)
        self.assertFalse(loaded.snapshot.samples.flags.writeable)
        np.testing.assert_allclose(
            loaded.snapshot.samples,
            [-1 / 65536, 0.5, -0.5, 0.0],
            atol=1e-7,
        )

    def test_duration_limit_is_checked_before_samples_are_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "long.wav"
            write_stereo_wav(path)
            with self.assertRaisesRegex(ValueError, "audio_too_long"):
                PROBE.load_pcm_wav(path, max_audio_seconds=0.5)


class ProbeReportTests(unittest.TestCase):
    def test_report_labels_x_asr_as_baseline_not_ground_truth(self):
        baseline = "我是一个紧胸型的牌手"
        backend = StaticBackend()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secret-name.wav"
            write_stereo_wav(path)
            report = PROBE.run_probe(
                PROBE.ProbeConfig(path, baseline, timeout_seconds=2),
                backend=backend,
            )

        self.assertEqual(report["mode"], "shadow")
        self.assertEqual(report["refinement"]["status"], "shadow_valid")
        self.assertEqual(report["refinement"]["committed_text_source"], "x_asr_baseline")
        self.assertTrue(report["refinement"]["would_replace_if_active"])
        self.assertEqual(
            report["transcripts"]["x_asr_baseline"]["role"],
            "baseline_not_ground_truth",
        )
        self.assertEqual(
            report["transcripts"]["local_model_candidate"]["text"],
            "我是一个紧凶型的牌手",
        )
        self.assertNotIn("secret-name", repr(report))
        self.assertEqual(len(backend.requests), 1)
        self.assertIsInstance(backend.requests[0][0].audio, REFINER.AudioSnapshot)

    def test_metrics_only_omits_all_transcript_text(self):
        baseline = "不应出现的基线文本"
        candidate = "不应出现的候选文本"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audio.wav"
            write_stereo_wav(path)
            report = PROBE.run_probe(
                PROBE.ProbeConfig(
                    path,
                    baseline,
                    timeout_seconds=2,
                    include_transcripts=False,
                ),
                backend=StaticBackend(candidate),
            )

        rendered = repr(report)
        self.assertNotIn("transcripts", report)
        self.assertNotIn(baseline, rendered)
        self.assertNotIn(candidate, rendered)
        self.assertFalse(report["privacy"]["audio_persisted"])
        self.assertFalse(report["privacy"]["transcripts_persisted"])
        self.assertFalse(report["privacy"]["input_path_emitted"])


if __name__ == "__main__":
    unittest.main()
