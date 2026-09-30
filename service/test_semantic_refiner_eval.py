import hashlib
import io
import json
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr
from unittest import mock
import wave
from pathlib import Path

import numpy as np

import semantic_refiner_eval as EVAL
import semantic_refiner_protocol_v1 as PROTOCOL
import voice_refiner as REFINER
import voice_tool


def write_wav(path: Path, *, frames: int = 160) -> None:
    samples = np.zeros(frames, dtype="<i2")
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16000)
        writer.writeframes(samples.tobytes())


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_manifest(path: Path, records, *, add_hash: bool = True) -> None:
    prepared = []
    for original in records:
        record = dict(original)
        if add_hash and "sha256" not in record:
            wav = record.get("wav")
            candidate = path.parent / wav if isinstance(wav, str) else None
            record["sha256"] = (
                file_sha256(candidate)
                if candidate is not None and candidate.is_file()
                else "0" * 64
            )
        prepared.append(record)
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in prepared),
        encoding="utf-8",
    )


class FakeProvider:
    def __init__(self, name, responses, *, identity=None):
        self.name = name
        self.responses = dict(responses)
        self.calls = []
        self.identity = identity

    def transcribe(self, audio, *, sample_id, timeout_seconds):
        self.calls.append((sample_id, timeout_seconds, audio))
        self.assert_audio(audio)
        response = self.responses[sample_id]
        if isinstance(response, Exception):
            raise response
        return response

    @staticmethod
    def assert_audio(audio):
        if not isinstance(audio, REFINER.AudioSnapshot):
            raise AssertionError("expected AudioSnapshot")
        if audio.samples.flags.writeable:
            raise AssertionError("audio must be immutable")


class FakeSherpaBackend:
    def __init__(self, family, *, on_refine=None, texts=("稳定结果",)):
        self.name = f"sherpa-onnx/{family}"
        self.on_refine = on_refine
        self.texts = tuple(texts)
        self.refine_calls = 0

    def refine(self, _request, *, timeout_seconds):
        del timeout_seconds
        self.refine_calls += 1
        if self.on_refine is not None:
            self.on_refine()
        index = min(self.refine_calls - 1, len(self.texts) - 1)
        return type("Candidate", (), {"text": self.texts[index]})()


class FakeNormalizationService:
    ITN_POLICY_VERSION = "2.2.0-test"
    apply_engineering_symbols = staticmethod(voice_tool.apply_engineering_symbols)
    load_spoken_number_exceptions = staticmethod(
        voice_tool.load_spoken_number_exceptions
    )

    def __init__(self):
        self.calls = []

    @staticmethod
    def apply_personal_phonetic_corrections(value):
        return value

    def normalize(
        self,
        value,
        *,
        diagnostic_logger=None,
        memory_only_itn=False,
    ):
        self.calls.append((value, diagnostic_logger, memory_only_itn))
        return value


class SherpaProviderIdentityTests(unittest.TestCase):
    def write_model(self, root: Path, family: str) -> dict[str, Path]:
        root.mkdir(parents=True)
        if family == "paraformer":
            paths = {
                "paraformer": root / "model.int8.onnx",
                "tokens": root / "tokens.txt",
            }
        elif family == "sense_voice":
            paths = {
                "model": root / "model.int8.onnx",
                "tokens": root / "tokens.txt",
            }
        else:
            tokenizer = root / "tokenizer"
            tokenizer.mkdir()
            paths = {
                "conv_frontend": root / "conv_frontend.onnx",
                "encoder": root / "encoder.int8.onnx",
                "decoder": root / "decoder.int8.onnx",
                "tokenizer_vocab": tokenizer / "vocab.json",
                "tokenizer_merges": tokenizer / "merges.txt",
                "tokenizer_config": tokenizer / "tokenizer_config.json",
            }
        for role, path in paths.items():
            path.write_bytes(f"{family}:{role}:tiny-model-data".encode("ascii"))
        return paths

    @staticmethod
    def make_provider(
        spec,
        *,
        backend_factory=None,
        normalization_service=None,
    ):
        if backend_factory is None:
            backend_factory = lambda family, _root, **_kwargs: FakeSherpaBackend(
                family
            )
        return EVAL.SherpaTranscriptProvider(
            spec,
            backend_factory=backend_factory,
            runtime_version_resolver=lambda: "1.12.14-test",
            normalization_service=(
                FakeNormalizationService()
                if normalization_service is None
                else normalization_service
            ),
        )

    def test_identity_covers_every_strict_file_and_fixed_inference_parameters(self):
        expected_specific = {
            "paraformer": {
                "factory": "from_paraformer",
                "feature_dim": 80,
            },
            "sense_voice": {
                "factory": "from_sense_voice",
                "feature_dim": 80,
                "language": "yue",
                "use_itn": False,
            },
            "qwen3_asr": {
                "factory": "from_qwen3_asr",
                "feature_dim": 128,
                "max_total_len": 512,
                "max_new_tokens": 128,
                "temperature": 1e-6,
                "top_p": 0.8,
                "seed": 42,
                "hotwords": "",
            },
        }
        common = {
            "num_threads": 3,
            "sample_rate": 16000,
            "decoding_method": "greedy_search",
            "debug": False,
            "provider": "cpu",
        }
        with tempfile.TemporaryDirectory() as directory:
            temporary_root = Path(directory)
            for family in ("paraformer", "sense_voice", "qwen3_asr"):
                with self.subTest(family=family):
                    model_root = temporary_root / family
                    expected_files = self.write_model(model_root, family)
                    calls = []

                    def backend_factory(
                        actual_family,
                        actual_root,
                        *,
                        num_threads,
                        language,
                    ):
                        calls.append(
                            (
                                actual_family,
                                actual_root,
                                num_threads,
                                language,
                            )
                        )
                        return FakeSherpaBackend(actual_family)

                    provider = self.make_provider(
                        EVAL.BackendSpec(
                            family,
                            model_root,
                            num_threads=3,
                            language="YUE",
                        ),
                        backend_factory=backend_factory,
                    )
                    identity = provider.identity

                    self.assertEqual(identity["provider"], "sherpa_onnx_offline")
                    self.assertEqual(identity["family"], family)
                    self.assertEqual(identity["num_threads"], 3)
                    self.assertEqual(identity["language"], "yue")
                    self.assertEqual(
                        identity["runtime"],
                        {"package": "sherpa-onnx", "version": "1.12.14-test"},
                    )
                    self.assertEqual(
                        identity["inference_parameters"],
                        {**expected_specific[family], **common},
                    )
                    self.assertEqual(
                        identity["normalization"]["entrypoint"],
                        "voice_flypy_service.normalize",
                    )
                    self.assertEqual(
                        identity["normalization"]["itn_cache_policy"],
                        "memory_only",
                    )
                    self.assertTrue(
                        identity["normalization"]["apply_personal_phonetics"]
                    )
                    self.assertRegex(
                        identity["normalization"]["voice_tool_module_sha256"],
                        r"^[0-9a-f]{64}$",
                    )
                    self.assertEqual(
                        identity["private_resource_identity"],
                        "revalidated_not_exported",
                    )
                    self.assertEqual(
                        identity["normalization"]["private_resource_identity"],
                        "revalidated_not_exported",
                    )
                    for private_field in (
                        "spoken_number_exceptions",
                        "engineering_symbol_aliases",
                        "personal_phonetics_resource_present",
                        "personal_phonetics_resource_sha256",
                    ):
                        self.assertNotIn(
                            private_field,
                            identity["normalization"],
                        )
                    records = {
                        record["role"]: record for record in identity["model_files"]
                    }
                    self.assertEqual(set(records), set(expected_files))
                    for role, path in expected_files.items():
                        self.assertEqual(records[role]["sha256"], file_sha256(path))
                        self.assertEqual(
                            records[role]["stat"],
                            {
                                "file_type": "regular",
                                "size_bytes": path.stat().st_size,
                            },
                        )
                    rendered = json.dumps(identity, sort_keys=True)
                    self.assertNotIn(str(model_root), rendered)
                    self.assertNotIn("model_dir", rendered)
                    private_normalization = provider._normalization_identity
                    for private_sha256 in (
                        private_normalization["spoken_number_exceptions"][
                            "semantic_sha256"
                        ],
                        private_normalization["engineering_symbol_aliases"][
                            "semantic_sha256"
                        ],
                        private_normalization[
                            "personal_phonetics_resource_sha256"
                        ],
                    ):
                        self.assertNotIn(private_sha256, rendered)
                    original_runtime_version = provider.identity[
                        "normalization"
                    ]["runtime_versions"]["pynini"]
                    identity["normalization"]["runtime_versions"][
                        "pynini"
                    ] = "tampered"
                    self.assertEqual(
                        provider.identity["normalization"][
                            "runtime_versions"
                        ]["pynini"],
                        original_runtime_version,
                    )
                    self.assertEqual(
                        calls,
                        [(family, model_root.resolve(), 3, "yue")],
                    )

    def test_identity_is_stable_across_model_root_and_only_hash_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first_root = root / "first-private-location"
            second_root = root / "second-private-location"
            self.write_model(first_root, "qwen3_asr")
            self.write_model(second_root, "qwen3_asr")
            spec_kwargs = {"num_threads": 2, "language": "zh"}
            first = self.make_provider(
                EVAL.BackendSpec("qwen3_asr", first_root, **spec_kwargs)
            )
            second = self.make_provider(
                EVAL.BackendSpec("qwen3_asr", second_root, **spec_kwargs)
            )

            self.assertEqual(first.identity, second.identity)
            identity_sha256 = EVAL._provider_identity_sha256(first)
            report = EVAL._backend_report(
                name="qwen3_asr",
                edits=0,
                reference_characters=1,
                identifier_correct=0,
                identifier_total=0,
                successful=1,
                failed=0,
                latencies=[1.0],
                identity_sha256=identity_sha256,
            )

        self.assertEqual(report["identity_sha256"], identity_sha256)
        rendered = json.dumps(report, sort_keys=True)
        self.assertNotIn("model_files", rendered)
        self.assertNotIn("inference_parameters", rendered)
        self.assertNotIn("first-private-location", rendered)
        self.assertNotIn("second-private-location", rendered)

    def test_private_normalization_does_not_change_public_or_report_identity(self):
        class PrivateNormalizationService(FakeNormalizationService):
            def __init__(self, spoken):
                super().__init__()
                self.spoken = (spoken,)

            def load_spoken_number_exceptions(self):
                return self.spoken

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model_root = root / "model"
            self.write_model(model_root, "paraformer")
            first_secret = f"私密词条甲-{root}"
            second_secret = f"私密词条乙-{root}"
            first = self.make_provider(
                EVAL.BackendSpec("paraformer", model_root),
                normalization_service=PrivateNormalizationService(first_secret),
            )
            second = self.make_provider(
                EVAL.BackendSpec("paraformer", model_root),
                normalization_service=PrivateNormalizationService(second_secret),
            )
            first_private_hash = first._normalization_identity[
                "spoken_number_exceptions"
            ]["semantic_sha256"]
            second_private_hash = second._normalization_identity[
                "spoken_number_exceptions"
            ]["semantic_sha256"]
            first_identity = first.identity
            second_identity = second.identity

            self.assertNotEqual(first_private_hash, second_private_hash)
            self.assertEqual(first_identity, second_identity)
            self.assertEqual(
                EVAL._provider_identity_sha256(first),
                EVAL._provider_identity_sha256(second),
            )
            for identity in (first_identity, second_identity):
                rendered = json.dumps(identity, ensure_ascii=False, sort_keys=True)
                self.assertNotIn(first_secret, rendered)
                self.assertNotIn(second_secret, rendered)
                self.assertNotIn(str(root), rendered)
                self.assertNotIn(first_private_hash, rendered)
                self.assertNotIn(second_private_hash, rendered)
                for private_field in EVAL._PRIVATE_NORMALIZATION_IDENTITY_FIELDS:
                    self.assertNotIn(
                        private_field,
                        identity["normalization"],
                    )

    def test_backend_construction_requires_same_stat_and_sha_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            model_root = Path(directory) / "model"
            paths = self.write_model(model_root, "paraformer")
            target = paths["paraformer"]

            def mutating_factory(family, _root, **_kwargs):
                target.write_bytes(b"X" * target.stat().st_size)
                return FakeSherpaBackend(family)

            with self.assertRaises(EVAL.EvaluationInputError) as raised:
                self.make_provider(
                    EVAL.BackendSpec("paraformer", model_root),
                    backend_factory=mutating_factory,
                )

        self.assertEqual(raised.exception.code, "sherpa_model_identity_unstable")
        self.assertEqual(str(raised.exception), "sherpa_model_identity_unstable")

    def test_first_transcription_closes_lazy_load_file_races(self):
        audio = REFINER.AudioSnapshot(np.zeros(160, dtype=np.float32), 16000)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            before_root = root / "before"
            before_paths = self.write_model(before_root, "sense_voice")
            before_backend = FakeSherpaBackend("sense_voice")
            before_provider = self.make_provider(
                EVAL.BackendSpec("sense_voice", before_root),
                backend_factory=lambda *_args, **_kwargs: before_backend,
            )
            before_provider.prepare()
            before_target = before_paths["model"]
            before_target.write_bytes(b"B" * before_target.stat().st_size)
            with self.assertRaises(EVAL.EvaluationInputError) as before_raised:
                before_provider.transcribe(
                    audio,
                    sample_id="private-sample",
                    timeout_seconds=1,
                )

            during_root = root / "during"
            during_paths = self.write_model(during_root, "qwen3_asr")
            during_target = during_paths["tokenizer_vocab"]
            during_backend = FakeSherpaBackend(
                "qwen3_asr",
                on_refine=lambda: during_target.write_bytes(
                    b"D" * during_target.stat().st_size
                ),
            )
            during_provider = self.make_provider(
                EVAL.BackendSpec("qwen3_asr", during_root),
                backend_factory=lambda *_args, **_kwargs: during_backend,
            )
            during_provider.prepare()
            with self.assertRaises(EVAL.EvaluationInputError) as during_raised:
                during_provider.transcribe(
                    audio,
                    sample_id="private-sample",
                    timeout_seconds=1,
                )

        self.assertEqual(
            before_raised.exception.code,
            "sherpa_model_changed_before_first_transcription",
        )
        self.assertEqual(before_backend.refine_calls, 0)
        self.assertEqual(
            during_raised.exception.code,
            "sherpa_model_changed_during_first_transcription",
        )
        self.assertEqual(during_backend.refine_calls, 1)
        self.assertNotIn("private-sample", str(during_raised.exception))

    def test_prepare_is_required_idempotent_and_outside_transcription(self):
        audio = REFINER.AudioSnapshot(np.zeros(160, dtype=np.float32), 16000)
        with tempfile.TemporaryDirectory() as directory:
            model_root = Path(directory) / "model"
            self.write_model(model_root, "paraformer")
            normalization = FakeNormalizationService()
            provider = self.make_provider(
                EVAL.BackendSpec("paraformer", model_root),
                normalization_service=normalization,
            )

            with self.assertRaises(EVAL.EvaluationInputError) as raised:
                provider.transcribe(
                    audio,
                    sample_id="sample",
                    timeout_seconds=1,
                )
            provider.prepare()
            provider.prepare()
            result = provider.transcribe(
                audio,
                sample_id="sample",
                timeout_seconds=1,
            )

        self.assertEqual(raised.exception.code, "sherpa_provider_not_prepared")
        self.assertEqual(result, "稳定结果")
        self.assertEqual(len(normalization.calls), 2)
        self.assertEqual(normalization.calls[0][0], EVAL._NORMALIZATION_PREWARM_TEXT)
        self.assertEqual(normalization.calls[1][0], "稳定结果")
        self.assertTrue(callable(normalization.calls[0][1]))
        self.assertTrue(normalization.calls[0][2])
        self.assertFalse(
            provider.identity["preparation"]["included_in_sample_latency"]
        )

    def test_sherpa_revalidates_loaded_normalization_semantics_per_call(self):
        class MutableNormalizationService(FakeNormalizationService):
            def __init__(self):
                super().__init__()
                self.spoken = ("一点问题",)

            def load_spoken_number_exceptions(self):
                return self.spoken

        audio = REFINER.AudioSnapshot(np.zeros(160, dtype=np.float32), 16000)
        with tempfile.TemporaryDirectory() as directory:
            model_root = Path(directory) / "model"
            self.write_model(model_root, "paraformer")
            normalization = MutableNormalizationService()
            provider = self.make_provider(
                EVAL.BackendSpec("paraformer", model_root),
                normalization_service=normalization,
            )
            initial_identity = provider.identity
            provider.prepare()
            normalization.spoken = ("变更后的例外",)
            with self.assertRaises(EVAL.EvaluationInputError) as raised:
                provider.transcribe(
                    audio,
                    sample_id="private-sample",
                    timeout_seconds=1,
                )

        self.assertEqual(
            raised.exception.code,
            "sherpa_normalization_identity_changed",
        )
        self.assertNotIn("一点问题", json.dumps(initial_identity))
        self.assertNotIn("变更后的例外", str(raised.exception))

    def test_normalization_fallback_signal_aborts_prepare_without_private_text(self):
        class FallbackNormalizationService(FakeNormalizationService):
            def normalize(
                self,
                value,
                *,
                diagnostic_logger=None,
                memory_only_itn=False,
            ):
                self.calls.append((value, diagnostic_logger, memory_only_itn))
                diagnostic_logger("private transcript and path")
                return value

        with tempfile.TemporaryDirectory() as directory:
            model_root = Path(directory) / "model"
            self.write_model(model_root, "paraformer")
            provider = self.make_provider(
                EVAL.BackendSpec("paraformer", model_root),
                normalization_service=FallbackNormalizationService(),
            )
            with self.assertRaises(EVAL.EvaluationInputError) as caught:
                provider.prepare()

        self.assertEqual(caught.exception.code, "sherpa_normalization_fallback")
        self.assertNotIn("private", str(caught.exception))

    def test_sherpa_output_uses_production_memory_only_silent_normalization(self):
        import voice_flypy_service as service
        import voice_personal_phonetics as personal

        audio = REFINER.AudioSnapshot(np.zeros(160, dtype=np.float32), 16000)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model_root = root / "model"
            self.write_model(model_root, "paraformer")
            backend = FakeSherpaBackend(
                "paraformer",
                texts=("W三五六", "温度二十摄氏度"),
            )
            missing_personal_usage = root / "missing-personal-usage.json"
            with mock.patch.dict(
                service.os.environ,
                {"VOICEFLYPY_PERSONAL_USAGE": str(missing_personal_usage)},
            ):
                personal._reset_personal_phonetic_cache_for_tests()
                provider = self.make_provider(
                    EVAL.BackendSpec("paraformer", model_root),
                    backend_factory=lambda *_args, **_kwargs: backend,
                    normalization_service=service,
                )
                captured = io.StringIO()
                with mock.patch.object(EVAL.sys, "stdout", captured):
                    provider.prepare()
                    identifier = provider.transcribe(
                        audio,
                        sample_id="identifier",
                        timeout_seconds=1,
                    )
                    engineering = provider.transcribe(
                        audio,
                        sample_id="engineering",
                        timeout_seconds=1,
                    )
                personal._reset_personal_phonetic_cache_for_tests()

        self.assertEqual(identifier, "W356")
        self.assertEqual(engineering, "温度20°C")
        self.assertEqual(captured.getvalue(), "")
        self.assertEqual(
            provider.identity["normalization"]["itn_policy_version"],
            service.ITN_POLICY_VERSION,
        )


class EvaluationReportTests(unittest.TestCase):
    def make_config(
        self,
        root,
        manifest,
        *,
        include_transcripts=False,
        max_wav_bytes=None,
        timeout_seconds=1,
        protocol=None,
    ):
        return EVAL.EvaluationConfig(
            dataset_root=Path(root),
            manifest_path=Path(manifest),
            baseline=EVAL.BackendSpec("paraformer", Path(root) / "baseline-model"),
            second_model=EVAL.BackendSpec("qwen3_asr", Path(root) / "second-model"),
            timeout_seconds=timeout_seconds,
            max_audio_seconds=1,
            max_wav_bytes=(
                EVAL.MAX_WAV_FILE_BYTES if max_wav_bytes is None else max_wav_bytes
            ),
            include_transcripts=include_transcripts,
            protocol=protocol,
        )

    def write_protocol_dataset(self, root, *, h03_tags=("mixed", "homophone")):
        records = []
        responses = {}
        for spec in PROTOCOL._SPECS:
            filename = f"{spec.sample_id}.wav"
            write_wav(Path(root) / filename)
            tags = h03_tags if spec.sample_id == "H03" else spec.tags
            records.append(
                {
                    "id": spec.sample_id,
                    "wav": filename,
                    "ground_truth": spec.ground_truth,
                    "split": spec.split,
                    "tags": list(tags),
                }
            )
            responses[spec.sample_id] = spec.ground_truth
        manifest = Path(root) / "manifest.jsonl"
        write_manifest(manifest, records)
        return manifest, responses

    def test_fixed_protocol_is_validated_and_emits_only_anonymous_aggregate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, responses = self.write_protocol_dataset(root)
            report = EVAL.run_evaluation(
                self.make_config(
                    root,
                    manifest,
                    protocol=PROTOCOL.PROTOCOL_VERSION,
                ),
                baseline_provider=FakeProvider("baseline", responses),
                second_provider=FakeProvider("second", responses),
            )

        aggregate = report["quality_protocol"]
        PROTOCOL.validate_public_report(aggregate)
        rendered = json.dumps(aggregate, ensure_ascii=False, sort_keys=True)
        self.assertEqual(aggregate["scope"], "quality_only")
        self.assertEqual(
            aggregate["categories"]["identifiers"]["expected_tokens"],
            14,
        )
        self.assertEqual(
            aggregate["gates"]["performance_resources"]["measurement"],
            "not_measured",
        )
        for spec in PROTOCOL._SPECS:
            self.assertNotIn(spec.sample_id, rendered)
            self.assertNotIn(spec.ground_truth, rendered)

    def test_fixed_protocol_rejects_h03_tag_mismatch_before_provider_use(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, responses = self.write_protocol_dataset(
                root,
                h03_tags=("mixed",),
            )
            baseline = FakeProvider("baseline", responses)
            second = FakeProvider("second", responses)
            with self.assertRaises(EVAL.EvaluationInputError) as caught:
                EVAL.run_evaluation(
                    self.make_config(
                        root,
                        manifest,
                        protocol=PROTOCOL.PROTOCOL_VERSION,
                    ),
                    baseline_provider=baseline,
                    second_provider=second,
                )

        self.assertEqual(caught.exception.code, "protocol_v1_tags_mismatch")
        self.assertEqual(baseline.calls, [])
        self.assertEqual(second.calls, [])

    def test_fixed_protocol_rejects_non_list_tags_before_provider_construction(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, _responses = self.write_protocol_dataset(root)
            records = [
                json.loads(line)
                for line in manifest.read_text(encoding="utf-8").splitlines()
            ]
            records[0]["tags"] = "identifier"
            write_manifest(manifest, records)
            factory = mock.Mock(
                side_effect=AssertionError("provider construction must not run")
            )

            with self.assertRaises(EVAL.EvaluationInputError) as caught:
                EVAL.run_evaluation(
                    self.make_config(
                        root,
                        manifest,
                        protocol=PROTOCOL.PROTOCOL_VERSION,
                    ),
                    provider_factory=factory,
                )

        self.assertEqual(caught.exception.code, "protocol_v1_tags_type_invalid")
        factory.assert_not_called()

    def test_reports_cer_identifier_accuracy_agreement_and_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_wav(root / "one.wav")
            write_wav(root / "two.wav")
            manifest = root / "manifest.jsonl"
            write_manifest(
                manifest,
                [
                    {
                        "id": "identifier-1",
                        "wav": "one.wav",
                        "ground_truth": "W356",
                        "tags": ["identifier"],
                    },
                    {
                        "id": "speech-1",
                        "wav": "two.wav",
                        "ground_truth": "甲乙",
                        "verbatim_truth": " 甲 乙 ",
                        "split": "test",
                    },
                ],
            )
            baseline = FakeProvider(
                "baseline-fake",
                {"identifier-1": "W356", "speech-1": "甲丙"},
            )
            second = FakeProvider(
                "second-fake",
                {"identifier-1": "W356", "speech-1": "甲乙"},
            )
            report = EVAL.run_evaluation(
                self.make_config(root, manifest, include_transcripts=True),
                baseline_provider=baseline,
                second_provider=second,
            )

        self.assertEqual(report["sample_count"], 2)
        self.assertEqual(report["baseline"]["edit_distance"], 1)
        self.assertEqual(report["baseline"]["reference_characters"], 6)
        self.assertEqual(report["baseline"]["cer"], round(1 / 6, 6))
        self.assertEqual(report["second_model"]["cer"], 0)
        self.assertEqual(report["baseline"]["identifier_exact_accuracy"], 1)
        self.assertEqual(report["second_model"]["identifier_exact_accuracy"], 1)
        self.assertEqual(report["baseline"]["successful_count"], 2)
        self.assertEqual(report["second_model"]["failed_count"], 0)
        self.assertEqual(report["comparison"]["agreement_count"], 1)
        self.assertEqual(report["comparison"]["disagreement_count"], 1)
        self.assertEqual(report["comparison"]["second_better_count"], 1)
        self.assertEqual(report["error_count"], 1)
        self.assertEqual(report["errors"][0]["id"], "speech-1")
        self.assertEqual(
            report["errors"][0]["transcripts"]["ground_truth"],
            "甲乙",
        )
        self.assertEqual(
            report["errors"][0]["transcripts"]["verbatim_truth"],
            " 甲 乙 ",
        )
        self.assertEqual(report["errors"][0]["split"], "test")
        self.assertEqual(len(baseline.calls), 2)
        self.assertEqual(len(second.calls), 2)
        self.assertFalse(report["privacy"]["audio_persisted"])
        self.assertEqual(report["privacy"]["output"], "stdout_only")
        self.assertGreaterEqual(report["baseline"]["latency_ms"]["p95"], 0)

    def test_metrics_only_omits_all_transcript_bodies_and_paths(self):
        ground_truth = "不可泄露的人工真值"
        baseline_text = "不可泄露的第一遍正文"
        second_text = "不可泄露的第二遍正文"
        verbatim_truth = "不可泄露的逐字真值"
        sample_id = "/Users/private/客户姓名.wav"
        split = "逐字真值-不可泄露"
        provider_path = "/private/模型/客户"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_wav(root / "private-name.wav")
            manifest = root / "manifest.jsonl"
            write_manifest(
                manifest,
                [
                    {
                        "id": sample_id,
                        "wav": "private-name.wav",
                        "ground_truth": ground_truth,
                        "verbatim_truth": verbatim_truth,
                        "split": split,
                    }
                ],
            )
            report = EVAL.run_evaluation(
                self.make_config(root, manifest),
                baseline_provider=FakeProvider(
                    provider_path,
                    {sample_id: baseline_text},
                    identity={"model_path": provider_path},
                ),
                second_provider=FakeProvider(
                    "私密 provider 名称",
                    {sample_id: second_text},
                ),
            )

        rendered = json.dumps(report, ensure_ascii=False)
        self.assertNotIn(ground_truth, rendered)
        self.assertNotIn(baseline_text, rendered)
        self.assertNotIn(second_text, rendered)
        self.assertNotIn(verbatim_truth, rendered)
        self.assertNotIn(sample_id, rendered)
        self.assertNotIn(split, rendered)
        self.assertNotIn(provider_path, rendered)
        self.assertNotIn("私密 provider 名称", rendered)
        self.assertNotIn("private-name", rendered)
        self.assertEqual(report["errors"][0]["sample_number"], 1)
        self.assertNotIn("id", report["errors"][0])
        self.assertNotIn("split", report["errors"][0])
        self.assertNotIn("transcripts", report["errors"][0])
        self.assertFalse(report["privacy"]["transcripts_included"])
        self.assertFalse(report["privacy"]["paths_emitted"])
        self.assertFalse(report["privacy"]["sample_metadata_included"])
        self.assertFalse(report["privacy"]["raw_provider_metadata_included"])
        self.assertEqual(report["baseline"]["name"], "paraformer")
        self.assertEqual(report["second_model"]["name"], "qwen3_asr")
        self.assertRegex(report["baseline"]["identity_sha256"], r"^[0-9a-f]{64}$")

    def test_provider_failure_is_rejected_without_leaking_exception_message(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_wav(root / "clip.wav")
            manifest = root / "manifest.jsonl"
            write_manifest(
                manifest,
                [{"id": "sample", "wav": "clip.wav", "ground_truth": "甲乙"}],
            )
            report = EVAL.run_evaluation(
                self.make_config(root, manifest, include_transcripts=False),
                baseline_provider=FakeProvider("baseline", {"sample": "甲乙"}),
                second_provider=FakeProvider(
                    "second",
                    {"sample": RuntimeError("private model output")},
                ),
            )

        self.assertEqual(report["second_model"]["successful_count"], 0)
        self.assertEqual(report["second_model"]["failed_count"], 1)
        self.assertEqual(report["second_model"]["cer"], 1)
        self.assertEqual(report["comparison"]["unavailable_count"], 1)
        self.assertEqual(
            report["errors"][0]["second_model"]["error_type"],
            "RuntimeError",
        )
        self.assertFalse(report["complete"])
        self.assertEqual(report["provider_failure_count"], 1)
        self.assertNotIn("private model output", repr(report))

        stdout = io.StringIO()
        with mock.patch.object(EVAL, "run_evaluation", return_value=report):
            exit_code = EVAL.main(
                [
                    "--dataset-root",
                    "/private/dataset",
                    "--manifest",
                    "/private/dataset/manifest.jsonl",
                    "--baseline-family",
                    "paraformer",
                    "--baseline-model-dir",
                    "/private/baseline",
                    "--second-family",
                    "qwen3_asr",
                    "--second-model-dir",
                    "/private/second",
                ],
                stdout=stdout,
                stderr=io.StringIO(),
            )
        self.assertEqual(exit_code, 4)
        self.assertFalse(json.loads(stdout.getvalue())["complete"])

    def test_empty_transcript_is_successful_but_scores_as_a_quality_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_wav(root / "clip.wav")
            manifest = root / "manifest.jsonl"
            write_manifest(
                manifest,
                [{"id": "sample", "wav": "clip.wav", "ground_truth": "甲乙"}],
            )
            report = EVAL.run_evaluation(
                self.make_config(root, manifest),
                baseline_provider=FakeProvider("baseline", {"sample": ""}),
                second_provider=FakeProvider("second", {"sample": "甲乙"}),
            )

        self.assertTrue(report["complete"])
        self.assertEqual(report["baseline"]["successful_count"], 1)
        self.assertEqual(report["baseline"]["failed_count"], 0)
        self.assertEqual(report["baseline"]["edit_distance"], 2)

    def test_cli_defaults_to_metrics_only_and_requires_explicit_opt_in(self):
        arguments = [
            "--dataset-root",
            "/private/dataset",
            "--manifest",
            "/private/dataset/manifest.jsonl",
            "--baseline-family",
            "paraformer",
            "--baseline-model-dir",
            "/private/baseline",
            "--second-family",
            "qwen3_asr",
            "--second-model-dir",
            "/private/second",
        ]
        parser = EVAL.build_argument_parser()
        self.assertFalse(parser.parse_args(arguments).include_transcripts)
        self.assertIsNone(parser.parse_args(arguments).protocol)
        self.assertEqual(
            parser.parse_args(
                [*arguments, "--protocol", PROTOCOL.PROTOCOL_VERSION]
            ).protocol,
            PROTOCOL.PROTOCOL_VERSION,
        )
        self.assertFalse(
            parser.parse_args([*arguments, "--metrics-only"]).include_transcripts
        )
        self.assertTrue(
            parser.parse_args([*arguments, "--include-transcripts"]).include_transcripts
        )
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as abbreviated:
                parser.parse_args([*arguments, "--include-trans"])
        self.assertEqual(abbreviated.exception.code, 2)
        self.assertFalse(
            EVAL.EvaluationConfig(
                dataset_root=Path("/private/dataset"),
                manifest_path=Path("/private/dataset/manifest.jsonl"),
                baseline=EVAL.BackendSpec("paraformer", Path("/private/baseline")),
                second_model=EVAL.BackendSpec("qwen3_asr", Path("/private/second")),
            ).include_transcripts
        )

    def test_provider_factory_can_be_injected_without_models(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_wav(root / "clip.wav")
            manifest = root / "manifest.jsonl"
            write_manifest(
                manifest,
                [{"id": "sample", "wav": "clip.wav", "ground_truth": "正确"}],
            )
            config = self.make_config(root, manifest)
            created = []

            def factory(spec):
                created.append(spec.family)
                return FakeProvider(spec.family, {"sample": "正确"})

            report = EVAL.run_evaluation(config, provider_factory=factory)

        self.assertEqual(created, ["paraformer", "qwen3_asr"])
        self.assertEqual(report["baseline"]["cer"], 0)
        self.assertEqual(report["second_model"]["cer"], 0)

    def test_provider_preparation_finishes_before_any_timed_transcription(self):
        events = []

        class PreparingProvider(FakeProvider):
            def prepare(self):
                events.append(("prepare", self.name))

            def transcribe(self, audio, *, sample_id, timeout_seconds):
                self.assert_audio(audio)
                events.append(("transcribe", self.name))
                return self.responses[sample_id]

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_wav(root / "clip.wav")
            manifest = root / "manifest.jsonl"
            write_manifest(
                manifest,
                [{"id": "sample", "wav": "clip.wav", "ground_truth": "正确"}],
            )
            report = EVAL.run_evaluation(
                self.make_config(root, manifest),
                baseline_provider=PreparingProvider(
                    "baseline",
                    {"sample": "正确"},
                ),
                second_provider=PreparingProvider(
                    "second",
                    {"sample": "正确"},
                ),
            )

        self.assertTrue(report["complete"])
        self.assertEqual(
            events,
            [
                ("prepare", "baseline"),
                ("prepare", "second"),
                ("transcribe", "baseline"),
                ("transcribe", "second"),
            ],
        )

    def test_provider_preparation_failure_aborts_the_complete_run_safely(self):
        class BrokenPreparationProvider(FakeProvider):
            def prepare(self):
                raise RuntimeError("private preparation failure")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_wav(root / "clip.wav")
            manifest = root / "manifest.jsonl"
            write_manifest(
                manifest,
                [{"id": "sample", "wav": "clip.wav", "ground_truth": "正确"}],
            )
            baseline = BrokenPreparationProvider(
                "baseline",
                {"sample": "正确"},
            )
            second = FakeProvider("second", {"sample": "正确"})
            with self.assertRaises(EVAL.EvaluationInputError) as raised:
                EVAL.run_evaluation(
                    self.make_config(root, manifest),
                    baseline_provider=baseline,
                    second_provider=second,
                )

        self.assertEqual(raised.exception.code, "baseline_provider_prepare_failed")
        self.assertNotIn("private preparation failure", str(raised.exception))
        self.assertEqual(baseline.calls, [])
        self.assertEqual(second.calls, [])

        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(
            EVAL,
            "run_evaluation",
            side_effect=raised.exception,
        ):
            exit_code = EVAL.main(
                [
                    "--dataset-root",
                    "/private/dataset",
                    "--manifest",
                    "/private/dataset/manifest.jsonl",
                    "--baseline-family",
                    "paraformer",
                    "--baseline-model-dir",
                    "/private/baseline",
                    "--second-family",
                    "qwen3_asr",
                    "--second-model-dir",
                    "/private/second",
                ],
                stdout=stdout,
                stderr=stderr,
            )
        self.assertEqual(exit_code, 2)
        self.assertEqual(stdout.getvalue(), "")
        failure_report = json.loads(stderr.getvalue())
        self.assertFalse(failure_report["complete"])
        self.assertEqual(
            failure_report["error"]["code"],
            "baseline_provider_prepare_failed",
        )

    def test_x_asr_is_baseline_only_and_reports_body_free_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_wav(root / "clip.wav")
            manifest = root / "manifest.jsonl"
            write_manifest(
                manifest,
                [{"id": "sample", "wav": "clip.wav", "ground_truth": "\u771f\u503c"}],
            )
            config = self.make_config(root, manifest, include_transcripts=False)
            config = EVAL.EvaluationConfig(
                dataset_root=config.dataset_root,
                manifest_path=config.manifest_path,
                baseline=EVAL.BackendSpec(EVAL.VOICEFLYPY_X_ASR_FAMILY),
                second_model=config.second_model,
                timeout_seconds=config.timeout_seconds,
                max_audio_seconds=config.max_audio_seconds,
                include_transcripts=False,
            )
            identity = {
                "model_sha256": "a" * 64,
                "decoder_sha256": "b" * 64,
                "private_resource_identity": "revalidated_not_exported",
                "normalization": {
                    "itn_policy_version": "2.2.0",
                    "private_resource_identity": "revalidated_not_exported",
                },
            }
            report = EVAL.run_evaluation(
                config,
                baseline_provider=FakeProvider(
                    "voiceflypy_x_asr",
                    {"sample": "\u771f\u503c"},
                    identity=identity,
                ),
                second_provider=FakeProvider("offline", {"sample": "\u771f\u503c"}),
            )

            invalid = EVAL.EvaluationConfig(
                dataset_root=root,
                manifest_path=manifest,
                baseline=EVAL.BackendSpec("paraformer", root / "baseline"),
                second_model=EVAL.BackendSpec(EVAL.VOICEFLYPY_X_ASR_FAMILY),
            )
            with self.assertRaises(EVAL.EvaluationInputError) as raised:
                EVAL.run_evaluation(
                    invalid,
                    baseline_provider=FakeProvider("baseline", {"sample": "\u771f\u503c"}),
                    second_provider=FakeProvider("x-asr", {"sample": "\u771f\u503c"}),
                )

        encoded_identity = json.dumps(
            identity,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        self.assertEqual(
            report["baseline"]["identity_sha256"],
            hashlib.sha256(encoded_identity).hexdigest(),
        )
        self.assertNotIn("model_sha256", report["baseline"])
        self.assertNotIn("\u771f\u503c", json.dumps(report, ensure_ascii=False))
        self.assertEqual(raised.exception.code, "second_family_must_be_offline")

    def test_timeout_aborts_before_second_provider_or_later_sample(self):
        class BlockingProvider:
            name = "blocking"

            def __init__(self):
                self.calls = []
                self.release = threading.Event()
                self.finished = threading.Event()

            def transcribe(self, _audio, *, sample_id, timeout_seconds):
                self.calls.append((sample_id, timeout_seconds))
                self.release.wait(2)
                self.finished.set()
                return "迟到结果"

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_wav(root / "one.wav")
            write_wav(root / "two.wav")
            manifest = root / "manifest.jsonl"
            write_manifest(
                manifest,
                [
                    {"id": "first", "wav": "one.wav", "ground_truth": "一"},
                    {"id": "later", "wav": "two.wav", "ground_truth": "二"},
                ],
            )
            blocking = BlockingProvider()
            second = FakeProvider(
                "must-not-run",
                {"first": "一", "later": "二"},
            )
            started = time.perf_counter()
            with self.assertRaises(EVAL.EvaluationAbortedError) as raised:
                EVAL.run_evaluation(
                    self.make_config(
                        root,
                        manifest,
                        timeout_seconds=0.02,
                    ),
                    baseline_provider=blocking,
                    second_provider=second,
                )
            elapsed = time.perf_counter() - started

            self.assertEqual(raised.exception.code, "baseline_provider_timeout")
            self.assertFalse(raised.exception.native_call_cancelled)
            self.assertEqual(
                raised.exception.kill_boundary,
                "independent_evaluation_process",
            )
            self.assertLess(elapsed, 0.3)
            self.assertEqual(blocking.calls, [("first", 0.02)])
            self.assertEqual(second.calls, [])

            retry_started = time.perf_counter()
            with self.assertRaises(EVAL.EvaluationAbortedError):
                EVAL.run_evaluation(
                    self.make_config(
                        root,
                        manifest,
                        timeout_seconds=0.02,
                    ),
                    baseline_provider=blocking,
                    second_provider=second,
                )
            self.assertLess(time.perf_counter() - retry_started, 0.3)
            self.assertEqual(blocking.calls, [("first", 0.02)])
            self.assertEqual(second.calls, [])
            self.assertEqual(
                sum(
                    thread.name == "semantic-refiner-eval-provider"
                    for thread in threading.enumerate()
                ),
                1,
            )
            blocking.release.set()
            self.assertTrue(blocking.finished.wait(1))


class ManifestSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        write_wav(self.root / "ok.wav")
        self.manifest = self.root / "manifest.jsonl"

    def tearDown(self):
        self.temporary.cleanup()

    def assert_manifest_error(self, code, records, *, max_wav_bytes=None):
        write_manifest(self.manifest, records)
        with self.assertRaises(EVAL.EvaluationInputError) as raised:
            EVAL.load_manifest(
                self.manifest,
                self.root,
                max_wav_bytes=(
                    EVAL.MAX_WAV_FILE_BYTES
                    if max_wav_bytes is None
                    else max_wav_bytes
                ),
            )
        self.assertEqual(raised.exception.code, code)

    def test_rejects_duplicate_id_and_missing_ground_truth(self):
        self.assert_manifest_error(
            "duplicate_sample_id",
            [
                {"id": "same", "wav": "ok.wav", "ground_truth": "一"},
                {"id": "same", "wav": "ok.wav", "ground_truth": "二"},
            ],
        )
        self.assert_manifest_error(
            "ground_truth_invalid",
            [{"id": "empty", "wav": "ok.wav", "ground_truth": "   "}],
        )

    def test_requires_lowercase_sha256(self):
        write_manifest(
            self.manifest,
            [{"id": "missing", "wav": "ok.wav", "ground_truth": "真值"}],
            add_hash=False,
        )
        with self.assertRaises(EVAL.EvaluationInputError) as raised:
            EVAL.load_manifest(self.manifest, self.root)
        self.assertEqual(raised.exception.code, "manifest_required_field_missing")

        self.assert_manifest_error(
            "sha256_invalid",
            [
                {
                    "id": "uppercase",
                    "wav": "ok.wav",
                    "ground_truth": "真值",
                    "sha256": file_sha256(self.root / "ok.wav").upper(),
                }
            ],
        )

    def test_hash_preflight_detects_same_inode_and_size_mutation_before_providers(self):
        original_info = (self.root / "ok.wav").stat()
        write_manifest(
            self.manifest,
            [{"id": "changed", "wav": "ok.wav", "ground_truth": "真值"}],
        )
        with (self.root / "ok.wav").open("r+b") as handle:
            handle.seek(-1, 2)
            original = handle.read(1)
            handle.seek(-1, 2)
            handle.write(bytes([original[0] ^ 0x01]))
        changed_info = (self.root / "ok.wav").stat()
        self.assertEqual(original_info.st_ino, changed_info.st_ino)
        self.assertEqual(original_info.st_size, changed_info.st_size)

        baseline = FakeProvider("baseline", {"changed": "真值"})
        second = FakeProvider("second", {"changed": "真值"})
        config = EVAL.EvaluationConfig(
            dataset_root=self.root,
            manifest_path=self.manifest,
            baseline=EVAL.BackendSpec("paraformer", self.root / "baseline-model"),
            second_model=EVAL.BackendSpec("qwen3_asr", self.root / "second-model"),
        )
        with self.assertRaises(EVAL.EvaluationInputError) as raised:
            EVAL.run_evaluation(
                config,
                baseline_provider=baseline,
                second_provider=second,
            )
        self.assertEqual(raised.exception.code, "wav_sha256_mismatch")
        self.assertEqual(baseline.calls, [])
        self.assertEqual(second.calls, [])

    def test_load_wav_decodes_verified_snapshot_not_later_file_mutation(self):
        write_manifest(
            self.manifest,
            [{"id": "race", "wav": "ok.wav", "ground_truth": "真值"}],
        )
        sample = EVAL.load_manifest(self.manifest, self.root)[0]
        original_hash = file_sha256(self.root / "ok.wav")
        original_wave_open = wave.open
        mutation_happened = False

        def mutate_after_verified_read(source, mode="rb"):
            nonlocal mutation_happened
            if isinstance(source, io.BytesIO) and not mutation_happened:
                mutation_happened = True
                with (self.root / "ok.wav").open("r+b") as handle:
                    handle.seek(44)
                    handle.write(np.full(160, 32767, dtype="<i2").tobytes())
            return original_wave_open(source, mode)

        with mock.patch.object(EVAL.wave, "open", side_effect=mutate_after_verified_read):
            audio = EVAL.load_wav(sample, max_audio_seconds=1)

        self.assertTrue(mutation_happened)
        self.assertNotEqual(file_sha256(self.root / "ok.wav"), original_hash)
        self.assertEqual(np.count_nonzero(audio.samples), 0)

    def test_rejects_absolute_traversal_and_noncanonical_paths(self):
        for wav in (str((self.root / "ok.wav").resolve()), "../ok.wav", "./ok.wav"):
            with self.subTest(wav=wav):
                self.assert_manifest_error(
                    "wav_path_invalid",
                    [{"id": "bad", "wav": wav, "ground_truth": "真值"}],
                )

    def test_rejects_wav_symlink_even_when_target_is_inside_root(self):
        link = self.root / "link.wav"
        link.symlink_to(self.root / "ok.wav")
        self.assert_manifest_error(
            "wav_symlink",
            [{"id": "link", "wav": "link.wav", "ground_truth": "真值"}],
        )

    def test_rejects_wav_over_configured_size_before_provider_runs(self):
        self.assert_manifest_error(
            "wav_too_large",
            [{"id": "large", "wav": "ok.wav", "ground_truth": "真值"}],
            max_wav_bytes=44,
        )

    def test_rejects_symlink_manifest(self):
        real_manifest = self.root / "real.jsonl"
        write_manifest(
            real_manifest,
            [{"id": "one", "wav": "ok.wav", "ground_truth": "真值"}],
        )
        link = self.root / "linked.jsonl"
        link.symlink_to(real_manifest)
        with self.assertRaises(EVAL.EvaluationInputError) as raised:
            EVAL.load_manifest(link, self.root)
        self.assertEqual(raised.exception.code, "manifest_symlink")


class MetricPrimitiveTests(unittest.TestCase):
    def test_levenshtein_and_percentiles(self):
        self.assertEqual(EVAL.levenshtein_distance("kitten", "sitting"), 3)
        self.assertEqual(EVAL._percentile([10, 20, 30, 40], 0.5), 25)
        self.assertEqual(EVAL._percentile([10, 20, 30, 40], 0.95), 38.5)


if __name__ == "__main__":
    unittest.main()
