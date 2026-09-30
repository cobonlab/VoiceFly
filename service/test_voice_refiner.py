import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.error
import wave
from unittest import mock

import numpy as np

import voice_refiner as REFINER


def audio(seconds=0.1, sample_rate=16000):
    count = max(1, int(seconds * sample_rate))
    return REFINER.AudioSnapshot(
        samples=np.linspace(-0.25, 0.25, count, dtype=np.float32),
        sample_rate=sample_rate,
    )


def request(
    baseline="我是一个紧胸型的牌手",
    *,
    raw_text=None,
    snapshot=None,
):
    return REFINER.FinalizationRequest(
        session_id="session-test",
        raw_segments=(baseline,),
        raw_text=baseline if raw_text is None else raw_text,
        baseline_text=baseline,
        deadline_monotonic=time.monotonic() + 5,
        audio=snapshot if snapshot is not None else audio(),
    )


class StaticBackend:
    name = "static"

    def __init__(self, candidate=None, error=None):
        self.candidate = candidate
        self.error = error
        self.calls = []

    def refine(self, refinement_request, *, timeout_seconds):
        self.calls.append((refinement_request, timeout_seconds))
        if self.error is not None:
            raise self.error
        return self.candidate


class AudioBufferTests(unittest.TestCase):
    def test_snapshot_is_contiguous_immutable_and_exact(self):
        source = np.array([0.1, 0.2], dtype=np.float32)
        direct = REFINER.AudioSnapshot(samples=source, sample_rate=2)
        self.assertTrue(source.flags.writeable)
        source[0] = 0.9
        self.assertAlmostEqual(float(direct.samples[0]), 0.1)

        buffer = REFINER.BoundedAudioBuffer(max_seconds=1)
        self.assertTrue(buffer.append(np.array([[0.1], [0.2]], dtype=np.float32), 4))
        self.assertTrue(buffer.append(np.array([[0.3], [0.4]], dtype=np.float32), 4))
        snapshot = buffer.snapshot()

        self.assertEqual(snapshot.sample_rate, 4)
        np.testing.assert_allclose(snapshot.samples, [0.1, 0.2, 0.3, 0.4])
        self.assertAlmostEqual(snapshot.duration_seconds, 1.0)
        self.assertFalse(snapshot.samples.flags.writeable)

    def test_duration_overflow_discards_all_audio_and_stays_failed(self):
        buffer = REFINER.BoundedAudioBuffer(max_seconds=0.25)
        self.assertTrue(buffer.append(np.zeros(2, dtype=np.float32), 8))
        self.assertFalse(buffer.append(np.zeros(1, dtype=np.float32), 8))
        self.assertTrue(buffer.overflowed)
        self.assertEqual(buffer.sample_count, 0)
        self.assertIsNone(buffer.snapshot())
        self.assertFalse(buffer.append(np.zeros(1, dtype=np.float32), 8))

    def test_sample_rate_change_and_nonfinite_audio_fail_closed(self):
        changed = REFINER.BoundedAudioBuffer()
        changed.append(np.zeros(2, dtype=np.float32), 16000)
        self.assertFalse(changed.append(np.zeros(2, dtype=np.float32), 24000))
        self.assertEqual(changed.unavailable_reason, "sample_rate_changed")

        nonfinite = REFINER.BoundedAudioBuffer()
        self.assertFalse(nonfinite.append(np.array([np.nan], dtype=np.float32), 16000))
        self.assertEqual(nonfinite.unavailable_reason, "non_finite_samples")

        excessive_rate = REFINER.BoundedAudioBuffer()
        self.assertFalse(
            excessive_rate.append(
                np.zeros(1, dtype=np.float32),
                REFINER.MAX_CAPTURE_SAMPLE_RATE + 1,
            )
        )
        self.assertEqual(
            excessive_rate.unavailable_reason,
            "sample_rate_too_high",
        )


class PrivacyAndSchemaTests(unittest.TestCase):
    def test_repr_never_contains_dictated_text(self):
        secret = "这是不应出现在日志里的正文"
        evidence = REFINER.RecognitionEvidence(text=secret, tokens=(secret,))
        refinement_request = REFINER.FinalizationRequest(
            session_id="safe-session",
            raw_segments=(secret,),
            raw_text=secret,
            baseline_text=secret,
            deadline_monotonic=time.monotonic() + 1,
            evidence=(evidence,),
        )
        candidate = REFINER.BackendCandidate(text=secret)
        result = REFINER.RefinementResult(
            status="accepted",
            text=secret,
            candidate_text=secret,
            backend="test",
            latency_ms=1,
        )
        for value in (evidence, refinement_request, candidate, result):
            self.assertNotIn(secret, repr(value))

    def test_identifier_schema_serializes_without_free_text_guessing(self):
        payload = {
            "identifiers": [
                {"prefix": "w", "digits": "356"},
                {"prefix": "T", "digits": "41", "suffix": "a"},
            ]
        }
        self.assertEqual(
            REFINER.serialize_identifier_sequence(payload),
            "W356 T41A",
        )

    def test_identifier_schema_rejects_hanzi_digits_and_unknown_fields(self):
        bad_payloads = (
            {"identifiers": [{"prefix": "W", "digits": "三五六"}]},
            {"identifiers": [{"prefix": "W", "digits": "356", "guess": "yes"}]},
            {"identifiers": []},
        )
        for payload in bad_payloads:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                REFINER.serialize_identifier_sequence(payload)


class ValidatorTests(unittest.TestCase):
    def setUp(self):
        self.validator = REFINER.CandidateValidator(
            candidate_transform=lambda text: " ".join(text.split())
        )

    def test_contextual_homophone_correction_is_valid(self):
        candidate = REFINER.BackendCandidate(
            text="我是一个紧凶型的牌手",
            backend="test",
            confidence=0.9,
        )
        rendered, reason = self.validator.validate(request(), candidate)
        self.assertEqual(rendered, "我是一个紧凶型的牌手")
        self.assertEqual(reason, "")

    def test_free_text_cannot_change_ascii_or_numeric_entities(self):
        baseline = "编号 W356 和 T39"
        candidate = REFINER.BackendCandidate(
            text="编号 W365 和 T39",
            backend="test",
            confidence=0.99,
        )
        rendered, reason = self.validator.validate(request(baseline), candidate)
        self.assertIsNone(rendered)
        self.assertEqual(reason, "protected_token_changed")

    def test_low_similarity_and_low_confidence_are_rejected(self):
        low_confidence = REFINER.BackendCandidate(
            text="我是一个紧凶型的牌手",
            confidence=0.1,
        )
        self.assertEqual(
            self.validator.validate(request(), low_confidence)[1],
            "candidate_confidence_low",
        )
        unrelated = REFINER.BackendCandidate(
            text="今天天气真不错我们去公园吧",
            confidence=0.99,
        )
        self.assertEqual(
            self.validator.validate(request(), unrelated)[1],
            "candidate_similarity_low",
        )

    def test_typed_identifier_requires_high_confidence(self):
        payload = {"identifiers": [{"prefix": "W", "digits": "356"}]}
        accepted, reason = self.validator.validate(
            request("W365", raw_text="W 三六五"),
            REFINER.BackendCandidate(
                kind="identifier_sequence",
                structured=payload,
                confidence=0.95,
            ),
        )
        self.assertEqual(accepted, "W356")
        self.assertEqual(reason, "")
        rejected, reason = self.validator.validate(
            request("W365", raw_text="W 三六五"),
            REFINER.BackendCandidate(
                kind="identifier_sequence",
                structured=payload,
                confidence=0.7,
            ),
        )
        self.assertIsNone(rejected)
        self.assertEqual(reason, "identifier_confidence_low")

    def test_second_asr_can_correct_only_a_strict_identifier_utterance(self):
        corrected, reason = self.validator.validate(
            request("W365", raw_text="W 三六五"),
            REFINER.BackendCandidate(
                text="W356",
                confidence=0.96,
            ),
        )
        self.assertEqual(corrected, "W356")
        self.assertEqual(reason, "")

        rejected, reason = self.validator.validate(
            request("W365", raw_text="编号 W 三六五"),
            REFINER.BackendCandidate(
                text="W356",
                confidence=0.96,
            ),
        )
        self.assertIsNone(rejected)
        self.assertEqual(reason, "identifier_raw_evidence_missing")

    def test_identifier_override_is_canonical_ascii_with_single_spaces(self):
        identifier_request = request(
            "W365 T41",
            raw_text="W 三六五 T 四一",
        )
        for candidate_text in ("w 356\tt41", "ｗ３５６　ｔ４１"):
            with self.subTest(candidate_text=candidate_text):
                rendered, reason = self.validator.validate(
                    identifier_request,
                    REFINER.BackendCandidate(
                        text=candidate_text,
                        confidence=0.96,
                    ),
                )
                self.assertEqual(rendered, "W356 T41")
                self.assertEqual(reason, "")

    def test_identifier_change_requires_finite_high_confidence_and_bounded_edit(self):
        identifier_request = request("W365", raw_text="W 三六五")
        for confidence in (float("nan"), float("inf"), -0.1, 1.1, "0.99", True):
            with self.subTest(confidence=confidence):
                rendered, reason = self.validator.validate(
                    identifier_request,
                    REFINER.BackendCandidate(
                        text="W356",
                        confidence=confidence,
                    ),
                )
                self.assertIsNone(rendered)
                self.assertEqual(reason, "candidate_confidence_invalid")

        rendered, reason = self.validator.validate(
            request("W365 T41", raw_text="W 三六五 T 四一"),
            REFINER.BackendCandidate(text="W365 Q99", confidence=0.99),
        )
        self.assertIsNone(rendered)
        self.assertEqual(reason, "identifier_change_unbounded")

    def test_structured_identifier_cannot_replace_unrelated_prose(self):
        rendered, reason = self.validator.validate(
            request("今天天气不错"),
            REFINER.BackendCandidate(
                kind="identifier_sequence",
                structured={"identifiers": [{"prefix": "W", "digits": "999"}]},
                confidence=0.99,
            ),
        )
        self.assertIsNone(rendered)
        self.assertEqual(reason, "identifier_baseline_not_structured")

    def test_changed_dictation_requires_confidence_and_preserves_technical_symbols(self):
        rendered, reason = self.validator.validate(
            request(),
            REFINER.BackendCandidate(text="我是一个紧凶型的牌手"),
        )
        self.assertIsNone(rendered)
        self.assertEqual(reason, "candidate_confidence_missing")

        for baseline, changed in (
            ("温度 30℃", "温度 30℉"),
            ("比例 50%", "比例 50‰"),
            ("计算 3×4", "计算 3+4"),
        ):
            with self.subTest(baseline=baseline):
                rendered, reason = self.validator.validate(
                    request(baseline),
                    REFINER.BackendCandidate(
                        text=changed,
                        confidence=0.99,
                    ),
                )
                self.assertIsNone(rendered)
                self.assertEqual(reason, "protected_symbol_changed")

        for baseline, changed in (
            ("价格 $100", "价格 €100"),
            ("电路 A&B", "电路 A|B"),
            ("指数 2^3", "指数 2~3"),
            ("A & B", "A B &"),
            ("f(x)", "f[x]"),
        ):
            with self.subTest(baseline=baseline):
                rendered, reason = self.validator.validate(
                    request(baseline),
                    REFINER.BackendCandidate(
                        text=changed,
                        confidence=0.99,
                    ),
                )
                self.assertIsNone(rendered)
                self.assertEqual(reason, "protected_symbol_changed")

    def test_scoreless_identifier_canonicalization_cannot_change_commit(self):
        rendered, reason = self.validator.validate(
            request("w 356", raw_text="w 356"),
            REFINER.BackendCandidate(text="w 356", confidence=None),
        )
        self.assertIsNone(rendered)
        self.assertEqual(reason, "candidate_confidence_missing")

        for baseline, changed in (
            ("$100", "€100"),
            ("A&B", "A|B"),
            ("2^3", "2~3"),
        ):
            with self.subTest(baseline=baseline):
                rendered, reason = self.validator.validate(
                    request(baseline),
                    REFINER.BackendCandidate(
                        text=changed,
                        confidence=0.99,
                    ),
                )
                self.assertIsNone(rendered)
                self.assertEqual(reason, "protected_symbol_changed")

    def test_unchanged_identifier_tokens_are_still_canonicalized(self):
        rendered, reason = self.validator.validate(
            request("W356 T41", raw_text="W 三五六 T 四一"),
            REFINER.BackendCandidate(
                text="W356,T41",
                confidence=0.99,
            ),
        )
        self.assertEqual(rendered, "W356 T41")
        self.assertEqual(reason, "")

    def test_structured_identifier_obeys_total_output_limit(self):
        identifiers = [
            {
                "prefix": "ABCD",
                "digits": "1" * 32,
                "suffix": "ABCDEFGH",
            }
            for _ in range(16)
        ]
        rendered, reason = self.validator.validate(
            request("W1", raw_text="W 一"),
            REFINER.BackendCandidate(
                kind="identifier_sequence",
                structured={"identifiers": identifiers},
                confidence=0.99,
            ),
        )
        self.assertIsNone(rendered)
        self.assertEqual(reason, "candidate_too_long")


class FinalTextRefinerTests(unittest.TestCase):
    def test_active_mode_accepts_valid_candidate(self):
        backend = StaticBackend(
            REFINER.BackendCandidate(
                text="我是一个紧凶型的牌手",
                backend="static",
                confidence=0.9,
            )
        )
        refiner = REFINER.FinalTextRefiner(mode="active", backend=backend)
        result = refiner.refine(request())
        self.assertEqual(result.status, "accepted")
        self.assertEqual(result.text, "我是一个紧凶型的牌手")
        self.assertTrue(result.committed_text_changed)

    def test_shadow_mode_never_changes_committed_text(self):
        backend = StaticBackend(
            REFINER.BackendCandidate(
                text="我是一个紧凶型的牌手",
                backend="static",
                confidence=0.9,
            )
        )
        refiner = REFINER.FinalTextRefiner(mode="shadow", backend=backend)
        result = refiner.refine(request())
        self.assertEqual(result.status, "shadow_valid")
        self.assertEqual(result.text, "我是一个紧胸型的牌手")
        self.assertEqual(result.candidate_text, "我是一个紧凶型的牌手")
        self.assertFalse(result.committed_text_changed)

    def test_missing_audio_timeout_and_backend_error_all_fall_back(self):
        missing_audio = REFINER.FinalizationRequest(
            session_id="missing",
            raw_segments=("原文",),
            raw_text="原文",
            baseline_text="原文",
            deadline_monotonic=time.monotonic() + 1,
            audio_unavailable_reason="duration_limit",
        )
        valid = REFINER.BackendCandidate(text="原文", confidence=1)
        self.assertEqual(
            REFINER.FinalTextRefiner(
                mode="active", backend=StaticBackend(valid)
            ).refine(missing_audio).status,
            "unavailable",
        )

        timed_out = request()
        timed_out = REFINER.FinalizationRequest(
            session_id=timed_out.session_id,
            raw_segments=timed_out.raw_segments,
            raw_text=timed_out.raw_text,
            baseline_text=timed_out.baseline_text,
            deadline_monotonic=time.monotonic() - 1,
            audio=timed_out.audio,
        )
        self.assertEqual(
            REFINER.FinalTextRefiner(
                mode="active", backend=StaticBackend(valid)
            ).refine(timed_out).status,
            "timeout",
        )

        error_result = REFINER.FinalTextRefiner(
            mode="active",
            backend=StaticBackend(error=RuntimeError("contains private text")),
        ).refine(request())
        self.assertEqual(error_result.status, "error")
        self.assertEqual(error_result.text, "我是一个紧胸型的牌手")
        self.assertEqual(error_result.reason, "RuntimeError")

    def test_malformed_backend_and_validator_failure_fall_back(self):
        malformed = REFINER.FinalTextRefiner(
            mode="active",
            backend=StaticBackend(candidate={"text": "not typed"}),
        ).refine(request())
        self.assertEqual(malformed.status, "error")
        self.assertEqual(malformed.reason, "backend_candidate_invalid")
        self.assertEqual(malformed.text, "我是一个紧胸型的牌手")

        validator = mock.Mock()
        validator.validate.side_effect = RuntimeError("private model output")
        broken_validator = REFINER.FinalTextRefiner(
            mode="active",
            backend=StaticBackend(
                REFINER.BackendCandidate(text="我是一个紧凶型的牌手", confidence=1)
            ),
            validator=validator,
        ).refine(request())
        self.assertEqual(broken_validator.status, "error")
        self.assertEqual(broken_validator.reason, "validator_RuntimeError")
        self.assertEqual(broken_validator.text, "我是一个紧胸型的牌手")

    def test_backend_wait_is_bounded_and_hung_call_cannot_pile_up(self):
        release = REFINER.threading.Event()
        exited = REFINER.threading.Event()

        class BlockingBackend:
            name = "blocking"

            def refine(self, _request, *, timeout_seconds):
                release.wait()
                exited.set()
                return REFINER.BackendCandidate(text="原文", confidence=1)

        refiner = REFINER.FinalTextRefiner(
            mode="active",
            backend=BlockingBackend(),
            timeout_seconds=0.02,
        )
        first_request = request("原文")
        first_request = REFINER.FinalizationRequest(
            session_id=first_request.session_id,
            raw_segments=first_request.raw_segments,
            raw_text=first_request.raw_text,
            baseline_text=first_request.baseline_text,
            deadline_monotonic=time.monotonic() + 0.02,
            audio=first_request.audio,
        )
        started = time.perf_counter()
        first = refiner.refine(first_request)
        elapsed = time.perf_counter() - started
        self.assertEqual(first.status, "timeout")
        self.assertLess(elapsed, 0.2)

        second = refiner.refine(request("原文"))
        self.assertEqual(second.status, "unavailable")
        self.assertEqual(second.reason, "backend_busy")
        release.set()
        self.assertTrue(exited.wait(0.2))

    def test_environment_configurable_limits_have_hard_caps(self):
        refiner = REFINER.FinalTextRefiner(
            mode="active",
            backend=StaticBackend(
                REFINER.BackendCandidate(text="原文", confidence=1)
            ),
            timeout_seconds=999,
            max_audio_seconds=999,
        )
        self.assertEqual(
            refiner.timeout_seconds,
            REFINER.MAX_REFINER_TIMEOUT_SECONDS,
        )
        self.assertEqual(
            refiner.max_audio_seconds,
            REFINER.MAX_REFINER_AUDIO_SECONDS,
        )


class WhisperBackendTests(unittest.TestCase):
    def test_only_loopback_http_endpoints_are_accepted(self):
        for endpoint in (
            "https://127.0.0.1:18080/inference",
            "http://example.com/inference",
            "http://localhost:18080/inference",
            "http://user:pass@localhost/inference",
            "http://localhost/",
            "http://127.0.0.1:18080/inference?redirect=1",
        ):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                REFINER.WhisperServerBackend(endpoint)

    def test_language_and_prompt_are_bounded(self):
        with self.assertRaisesRegex(ValueError, "language"):
            REFINER.WhisperServerBackend(
                "http://127.0.0.1:18080/inference",
                language="zh\r\nInjected: yes",
            )
        with self.assertRaisesRegex(ValueError, "prompt"):
            REFINER.WhisperServerBackend(
                "http://127.0.0.1:18080/inference",
                prompt="x" * 1025,
            )

    def test_wav_is_sent_in_memory_and_verbose_json_is_parsed(self):
        payload = {
            "text": " 测试文本\n",
            "segments": [
                {
                    "start": 0.0,
                    "end": 0.1,
                    "avg_logprob": -0.1,
                    "words": [
                        {"word": "测试", "probability": 0.8},
                        {"word": "文本", "probability": 0.9},
                    ],
                }
            ],
        }

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return json.dumps(payload).encode()

        class Opener:
            def __init__(self):
                self.calls = []

            def open(self, http_request, *, timeout):
                self.calls.append((http_request, timeout))
                return Response()

        opener = Opener()
        backend = REFINER.WhisperServerBackend(
            "http://127.0.0.1:18080/inference",
            opener=opener,
        )
        candidate = backend.refine(request(), timeout_seconds=2)

        self.assertEqual(candidate.text, "测试文本")
        self.assertAlmostEqual(candidate.confidence, 0.85)
        http_request, timeout = opener.calls[0]
        self.assertEqual(timeout, 2)
        self.assertEqual(http_request.full_url, "http://127.0.0.1:18080/inference")
        self.assertIn(b'name="response_format"', http_request.data)
        self.assertIn(b"verbose_json", http_request.data)
        marker = b"\r\n\r\nRIFF"
        wav_start = http_request.data.index(marker) + 4
        wav_end = http_request.data.index(b"\r\n--", wav_start)
        with wave.open(io.BytesIO(http_request.data[wav_start:wav_end]), "rb") as reader:
            self.assertEqual(reader.getnchannels(), 1)
            self.assertEqual(reader.getsampwidth(), 2)
            self.assertEqual(reader.getframerate(), 16000)
            self.assertEqual(reader.getnframes(), 1600)


class SherpaOfflineBackendTests(unittest.TestCase):
    @staticmethod
    def make_model_dir(root, family):
        model_dir = Path(root) / family
        model_dir.mkdir()
        layouts = {
            "paraformer": ("model.int8.onnx", "tokens.txt"),
            "sense_voice": ("model.int8.onnx", "tokens.txt"),
            "qwen3_asr": (
                "conv_frontend.onnx",
                "encoder.int8.onnx",
                "decoder.int8.onnx",
            ),
        }
        for filename in layouts[family]:
            (model_dir / filename).write_bytes(b"model-test-data")
        if family == "qwen3_asr":
            tokenizer = model_dir / "tokenizer"
            tokenizer.mkdir()
            for filename in REFINER._QWEN_TOKENIZER_FILES:
                (tokenizer / filename).write_text("test", encoding="utf-8")
        return model_dir

    def test_all_families_are_lazy_and_accept_the_complete_snapshot_once(self):
        expected_keys = {
            "paraformer": {"paraformer", "tokens", "feature_dim"},
            "sense_voice": {"model", "tokens", "feature_dim", "use_itn", "language"},
            "qwen3_asr": {
                "conv_frontend",
                "encoder",
                "decoder",
                "tokenizer",
                "feature_dim",
                "max_total_len",
                "max_new_tokens",
                "temperature",
                "top_p",
                "seed",
                "hotwords",
            },
        }

        with tempfile.TemporaryDirectory() as root:
            for family in sorted(REFINER.SHERPA_OFFLINE_FAMILIES):
                with self.subTest(family=family):
                    model_dir = self.make_model_dir(root, family)
                    factory_calls = []
                    streams = []

                    class Result:
                        text = " 第二遍候选文本 "

                    class Stream:
                        def __init__(self):
                            self.accept_calls = []
                            self.result = Result()

                        def accept_waveform(self, sample_rate, samples):
                            self.accept_calls.append(
                                (sample_rate, np.array(samples, copy=True))
                            )

                    class Recognizer:
                        def create_stream(self):
                            stream = Stream()
                            streams.append(stream)
                            return stream

                        def decode_stream(self, stream):
                            self.last_stream = stream

                    def factory(**options):
                        factory_calls.append(options)
                        return Recognizer()

                    backend = REFINER.SherpaOfflineBackend(
                        family,
                        model_dir,
                        recognizer_factory=factory,
                    )
                    self.assertFalse(backend.is_initialized)
                    self.assertEqual(factory_calls, [])

                    snapshot = audio(seconds=0.01, sample_rate=48000)
                    candidate = backend.refine(
                        request(snapshot=snapshot),
                        timeout_seconds=1,
                    )
                    self.assertTrue(backend.is_initialized)
                    self.assertEqual(len(factory_calls), 1)
                    self.assertTrue(expected_keys[family].issubset(factory_calls[0]))
                    self.assertEqual(factory_calls[0]["provider"], "cpu")
                    self.assertEqual(factory_calls[0]["sample_rate"], 16000)
                    if family == "sense_voice":
                        self.assertFalse(factory_calls[0]["use_itn"])
                        self.assertEqual(factory_calls[0]["language"], "zh")
                    self.assertEqual(candidate.text, "第二遍候选文本")
                    self.assertEqual(candidate.backend, f"sherpa-onnx/{family}")
                    self.assertIsNone(candidate.confidence)
                    self.assertEqual(len(streams), 1)
                    self.assertEqual(len(streams[0].accept_calls), 1)
                    sample_rate, samples = streams[0].accept_calls[0]
                    self.assertEqual(sample_rate, 48000)
                    np.testing.assert_array_equal(samples, snapshot.samples)

                    backend.refine(request(snapshot=snapshot), timeout_seconds=1)
                    self.assertEqual(len(factory_calls), 1)
                    self.assertEqual(len(streams), 2)
                    self.assertTrue(all(len(s.accept_calls) == 1 for s in streams))

    def test_model_paths_are_strictly_validated(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(ValueError, "unsupported"):
                REFINER.SherpaOfflineBackend("unknown", Path(root))
            with self.assertRaisesRegex(ValueError, "absolute"):
                REFINER.SherpaOfflineBackend("paraformer", "relative/model")

            missing = Path(root) / "missing"
            missing.mkdir()
            (missing / "model.int8.onnx").write_bytes(b"model")
            with self.assertRaisesRegex(ValueError, "tokens.txt"):
                REFINER.SherpaOfflineBackend("paraformer", missing)

            empty = Path(root) / "empty"
            empty.mkdir()
            (empty / "model.int8.onnx").write_bytes(b"")
            (empty / "tokens.txt").write_text("token", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "non-empty"):
                REFINER.SherpaOfflineBackend("sense_voice", empty)

            qwen = self.make_model_dir(root, "qwen3_asr")
            (qwen / "tokenizer" / "vocab.json").unlink()
            with self.assertRaisesRegex(ValueError, "vocab.json"):
                REFINER.SherpaOfflineBackend("qwen3_asr", qwen)

    def test_factory_initialization_and_decoding_are_serialized(self):
        with tempfile.TemporaryDirectory() as root:
            model_dir = self.make_model_dir(root, "paraformer")
            entered = threading.Event()
            release = threading.Event()
            state_lock = threading.Lock()
            state = {"active": 0, "max_active": 0, "factory_calls": 0}

            class Result:
                text = "候选"

            class Stream:
                result = Result()

                def __init__(self):
                    self.accept_count = 0

                def accept_waveform(self, _sample_rate, _samples):
                    self.accept_count += 1

            class Recognizer:
                def create_stream(self):
                    return Stream()

                def decode_stream(self, _stream):
                    with state_lock:
                        state["active"] += 1
                        state["max_active"] = max(
                            state["max_active"], state["active"]
                        )
                    entered.set()
                    release.wait(1)
                    with state_lock:
                        state["active"] -= 1

            def factory(**_options):
                with state_lock:
                    state["factory_calls"] += 1
                return Recognizer()

            backend = REFINER.SherpaOfflineBackend(
                "paraformer",
                model_dir,
                recognizer_factory=factory,
            )
            errors = []

            def invoke():
                try:
                    backend.refine(request(), timeout_seconds=1)
                except Exception as exc:
                    errors.append(exc)

            first = threading.Thread(target=invoke)
            second = threading.Thread(target=invoke)
            first.start()
            self.assertTrue(entered.wait(0.2))
            second.start()
            time.sleep(0.02)
            with state_lock:
                self.assertEqual(state["active"], 1)
                self.assertEqual(state["factory_calls"], 1)
            release.set()
            first.join(1)
            second.join(1)

            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(state["max_active"], 1)
            self.assertEqual(state["factory_calls"], 1)


class ShadowRunnerTests(unittest.TestCase):
    def test_runner_allows_only_one_inference_and_releases_slot(self):
        candidate = REFINER.BackendCandidate(text="原文", confidence=1)
        refiner = REFINER.FinalTextRefiner(
            mode="shadow",
            backend=StaticBackend(candidate),
        )
        captured = []

        class CapturedThread:
            def __init__(self, *, target, daemon):
                self.target = target
                self.daemon = daemon
                captured.append(self)

            def start(self):
                pass

        results = []
        runner = REFINER.ShadowRefinementRunner(
            refiner,
            on_result=lambda req, result: results.append((req, result)),
            thread_factory=CapturedThread,
        )
        first = request("原文")
        self.assertTrue(runner.submit(first))
        self.assertFalse(runner.submit(first))
        self.assertTrue(runner.in_progress)
        captured[0].target()
        self.assertFalse(runner.in_progress)
        self.assertEqual(results[0][1].status, "shadow_valid")


if __name__ == "__main__":
    unittest.main()
