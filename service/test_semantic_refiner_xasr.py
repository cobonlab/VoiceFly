import hashlib
import io
import json
import sys
import tempfile
import threading
import time
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import numpy as np

import semantic_refiner_eval as EVAL
import semantic_refiner_xasr as XASR
from voice_hotwords import DecoderConfig, create_recognizer_with_fallback
from voice_refiner import AudioSnapshot


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class FakeStream:
    def __init__(self, recognizer):
        self.recognizer = recognizer
        self.finished = False

    def accept_waveform(self, sample_rate, samples):
        self.recognizer.accepted.append(
            (sample_rate, np.array(samples, dtype=np.float32, copy=True))
        )
        self.recognizer.index += 1
        self.recognizer.ready = True

    def input_finished(self):
        self.finished = True
        self.recognizer.ready = True


class ScriptedRecognizer:
    def __init__(
        self,
        frame_results,
        endpoints,
        *,
        final_result="",
        reject_inline=False,
    ):
        self.frame_results = list(frame_results)
        self.endpoints = list(endpoints)
        self.final_result = final_result
        self.reject_inline = reject_inline
        self.index = -1
        self.ready = False
        self.accepted = []
        self.created_with = []
        self.decode_calls = 0
        self.reset_calls = 0

    def create_stream(self, *args):
        self.created_with.append(args)
        if args and self.reject_inline:
            raise RuntimeError("inline graph unavailable")
        return FakeStream(self)

    def is_ready(self, _stream):
        return self.ready

    def decode_stream(self, _stream):
        self.decode_calls += 1
        self.ready = False

    def get_result(self, stream):
        if stream.finished:
            return self.final_result
        if 0 <= self.index < len(self.frame_results):
            return self.frame_results[self.index]
        return ""

    def is_endpoint(self, _stream):
        return 0 <= self.index < len(self.endpoints) and self.endpoints[self.index]

    def reset(self, _stream):
        self.reset_calls += 1


class StableFileFingerprintTests(unittest.TestCase):
    def test_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.bin"
            target.write_bytes(b"model")
            link = root / "link.bin"
            link.symlink_to(target)

            with self.assertRaises(XASR.VoiceFlypyXASRLoadError) as raised:
                XASR._stable_file_fingerprint(link)

        self.assertEqual(raised.exception.code, "x_asr_identity_read_failed")

    def test_hashes_and_stats_the_single_opened_inode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "asset.bin"
            replacement = root / "replacement.bin"
            old_bytes = b"old-model-bytes"
            new_bytes = b"new-model-bytes"
            path.write_bytes(old_bytes)
            replacement.write_bytes(new_bytes)
            old_inode = path.stat().st_ino
            real_open = XASR.os.open
            seen_flags = []

            def replace_after_open(candidate, flags):
                descriptor = real_open(candidate, flags)
                seen_flags.append(flags)
                replacement.replace(path)
                return descriptor

            with mock.patch.object(XASR.os, "open", side_effect=replace_after_open):
                fingerprint = XASR._stable_file_fingerprint(path)

            self.assertEqual(fingerprint["sha256"], sha256_bytes(old_bytes))
            self.assertEqual(fingerprint["inode"], old_inode)
            self.assertEqual(path.read_bytes(), new_bytes)
            self.assertEqual(len(seen_flags), 1)
            if hasattr(XASR.os, "O_NOFOLLOW"):
                self.assertTrue(seen_flags[0] & XASR.os.O_NOFOLLOW)


class ProviderTests(unittest.TestCase):
    def make_service(
        self,
        root: Path,
        recognizer,
        *,
        joiner=None,
        service_normalizer=None,
        factory=None,
    ):
        model_dir = root / "x-asr-model"
        model_dir.mkdir(parents=True)
        for index, name in enumerate(XASR.REQUIRED_MODEL_FILES, 1):
            (model_dir / name).write_bytes((name + str(index)).encode("utf-8"))
        bpe = root / "bpe.vocab"
        bpe.write_text(
            "".join(f"token{index}\t0\n" for index in range(1000)),
            encoding="utf-8",
        )
        hotword_data = "\u91cf \u5b50 :2.5\n".encode("utf-8")
        hotwords = root / "hotwords.txt"
        hotwords.write_bytes(hotword_data)
        decoder = DecoderConfig(
            enabled=True,
            reason="test resources",
            decoding_method="modified_beam_search",
            max_active_paths=8,
            modeling_unit="bpe",
            bpe_vocab=bpe,
            hotwords_file=hotwords,
            hotwords_score=1.5,
        )
        snapshot = types.SimpleNamespace(
            text=hotword_data.decode("utf-8"),
            sha256=sha256_bytes(hotword_data),
            entries=1,
            byte_count=len(hotword_data),
        )
        empty_data = b""
        empty_snapshot = types.SimpleNamespace(
            text="",
            sha256=sha256_bytes(empty_data),
            entries=0,
            byte_count=0,
        )
        captured_kwargs = []

        def default_factory(**kwargs):
            captured_kwargs.append(kwargs)
            return recognizer

        normalization_loggers = []
        normalization_memory_only = []
        voice_tool_path = root / "fake_voice_tool.py"
        voice_tool_path.write_bytes(b"# deterministic test voice tool\n")
        voice_tool_state = {
            "spoken": ("一点问题",),
            "aliases": (("摄氏度", "°C", "after_number"),),
        }
        module_name = (
            "_xasr_test_voice_tool_"
            + sha256_bytes(str(root).encode("utf-8"))[:16]
        )
        voice_tool_module = types.ModuleType(module_name)
        voice_tool_module.__file__ = str(voice_tool_path)

        def load_spoken_number_exceptions():
            return tuple(voice_tool_state["spoken"])

        def load_engineering_symbol_aliases():
            return tuple(voice_tool_state["aliases"])

        def apply_engineering_symbols(value):
            return value

        for function in (
            load_spoken_number_exceptions,
            load_engineering_symbol_aliases,
            apply_engineering_symbols,
        ):
            function.__module__ = module_name
        voice_tool_module.load_spoken_number_exceptions = (
            load_spoken_number_exceptions
        )
        voice_tool_module.load_engineering_symbol_aliases = (
            load_engineering_symbol_aliases
        )
        voice_tool_module.apply_engineering_symbols = apply_engineering_symbols
        sys.modules[module_name] = voice_tool_module
        self.addCleanup(sys.modules.pop, module_name, None)

        def default_service_normalizer(
            value,
            *,
            diagnostic_logger=None,
            memory_only_itn=False,
        ):
            normalization_loggers.append(diagnostic_logger)
            normalization_memory_only.append(memory_only_itn)
            return value

        service = types.SimpleNamespace(
            MODEL_DIR=model_dir,
            RESOURCE_ROOT=root,
            SOURCE_ROOT=root,
            SAMPLE_RATE=16000,
            ENDPOINT_MIN_TRAILING_SILENCE_SECONDS=3.0,
            ITN_POLICY_VERSION="2.2.0-test",
            resolve_decoder_config=mock.Mock(return_value=decoder),
            build_combined_hotwords=mock.Mock(
                side_effect=AssertionError("writing builder must not run")
            ),
            create_recognizer_with_fallback=create_recognizer_with_fallback,
            load_inline_hotword_snapshot=mock.Mock(return_value=snapshot),
            EMPTY_HOTWORD_SNAPSHOT=empty_snapshot,
            join_recognition_segments=(joiner or (lambda values: "".join(values))),
            normalize=(service_normalizer or default_service_normalizer),
            apply_personal_phonetic_corrections=lambda value: value,
            apply_engineering_symbols=apply_engineering_symbols,
            load_spoken_number_exceptions=load_spoken_number_exceptions,
            readonly_environment={
                "VOICEFLYPY_BPE_VOCAB": str(bpe),
                "VOICEFLYPY_COMBINED_HOTWORDS": str(hotwords),
            },
            normalization_loggers=normalization_loggers,
            normalization_memory_only=normalization_memory_only,
            voice_tool_state=voice_tool_state,
            voice_tool_path=voice_tool_path,
        )
        return service, (factory or default_factory), captured_kwargs

    def test_48k_snapshot_uses_native_rate_and_100ms_frames(self):
        recognizer = ScriptedRecognizer(
            ["\u539f\u59cb", "\u539f\u59cb", "\u539f\u59cb"],
            [False, False, False],
        )
        normalizer = mock.Mock(return_value="\u5df2\u5f52\u4e00")
        with tempfile.TemporaryDirectory() as directory:
            service, factory, captured = self.make_service(Path(directory), recognizer)
            provider = XASR.VoiceFlypyXASRProvider(
                service_module=service,
                recognizer_factory=factory,
                normalizer=normalizer,
                environment=service.readonly_environment,
            )
            audio = AudioSnapshot(np.zeros(12_000, dtype=np.float32), 48_000)
            provider.prepare()
            result = provider.transcribe(audio, sample_id="clip", timeout_seconds=1)

        self.assertEqual(result, "\u5df2\u5f52\u4e00")
        self.assertEqual([rate for rate, _ in recognizer.accepted], [48_000] * 3)
        self.assertEqual([len(frame) for _, frame in recognizer.accepted], [4800, 4800, 2400])
        self.assertEqual(recognizer.decode_calls, 4)
        normalizer.assert_called_once_with("\u539f\u59cb")
        self.assertEqual(captured[0]["sample_rate"], 16000)
        self.assertEqual(captured[0]["num_threads"], 4)
        self.assertEqual(captured[0]["rule1_min_trailing_silence"], 3.0)
        self.assertNotIn("hotwords_file", captured[0])
        self.assertEqual(recognizer.created_with, [(service.load_inline_hotword_snapshot.return_value.text,)])
        identity = provider.identity
        private_identity = provider._identity
        self.assertEqual(identity["baseline_fidelity"], "readonly_snapshot_approximation")
        self.assertEqual(identity["runtime_snapshot_equivalence"], "unproven")
        self.assertEqual(len(identity["model_sha256"]), 64)
        self.assertEqual(
            identity["private_resource_identity"],
            "revalidated_not_exported",
        )
        for field in XASR._PRIVATE_XASR_IDENTITY_FIELDS:
            self.assertNotIn(field, identity)
        self.assertEqual(
            private_identity["lexicon_sha256"],
            sha256_bytes("\u91cf \u5b50 :2.5\n".encode()),
        )
        self.assertEqual(
            private_identity["decoder_snapshot_source"],
            "existing_combined_snapshot",
        )
        self.assertEqual(
            identity["normalization"]["itn_policy_version"],
            "2.2.0-test",
        )
        self.assertEqual(
            identity["normalization"]["private_resource_identity"],
            "revalidated_not_exported",
        )
        for field in (
            "spoken_number_exceptions",
            "engineering_symbol_aliases",
            "personal_phonetics_resource_present",
            "personal_phonetics_resource_sha256",
        ):
            self.assertNotIn(field, identity["normalization"])
        self.assertRegex(identity["runtime"]["version"], r"^[A-Za-z0-9]")
        self.assertEqual(
            set(identity["normalization"]["runtime_versions"]),
            {"WeTextProcessing", "pynini"},
        )
        self.assertEqual(
            identity["normalization"]["voice_tool_module_sha256"],
            sha256_bytes(b"# deterministic test voice tool\n"),
        )
        self.assertEqual(
            private_identity["normalization"]["spoken_number_exceptions"][
                "entry_count"
            ],
            1,
        )
        self.assertRegex(
            private_identity["normalization"]["engineering_symbol_aliases"][
                "semantic_sha256"
            ],
            r"^[0-9a-f]{64}$",
        )
        rendered_identity = json.dumps(identity, ensure_ascii=False)
        self.assertNotIn("一点问题", rendered_identity)
        self.assertNotIn("摄氏度", rendered_identity)
        self.assertNotIn(str(directory), rendered_identity)
        for private_sha256 in (
            private_identity["lexicon_sha256"],
            private_identity["normalization"]["spoken_number_exceptions"][
                "semantic_sha256"
            ],
            private_identity["normalization"]["engineering_symbol_aliases"][
                "semantic_sha256"
            ],
            private_identity["normalization"][
                "personal_phonetics_resource_sha256"
            ],
        ):
            self.assertNotIn(private_sha256, rendered_identity)
        service.resolve_decoder_config.assert_not_called()
        service.build_combined_hotwords.assert_not_called()

    def test_endpoint_segments_are_reset_joined_once_and_finally_normalized(self):
        recognizer = ScriptedRecognizer(
            ["\u7532", "", "\u4e59", ""],
            [False, True, False, True],
            final_result="\u4e19",
        )
        joiner = mock.Mock(side_effect=lambda values: "|".join(values))
        normalizer = mock.Mock(side_effect=lambda value: f"N[{value}]")
        with tempfile.TemporaryDirectory() as directory:
            service, factory, _ = self.make_service(
                Path(directory),
                recognizer,
                joiner=joiner,
            )
            provider = XASR.VoiceFlypyXASRProvider(
                service_module=service,
                recognizer_factory=factory,
                normalizer=normalizer,
                environment=service.readonly_environment,
            )
            provider.prepare()
            result = provider.transcribe(
                AudioSnapshot(np.zeros(6400, dtype=np.float32), 16_000),
                sample_id="segments",
                timeout_seconds=1,
            )

        self.assertEqual(result, "N[\u7532|\u4e59|\u4e19]")
        self.assertEqual(recognizer.reset_calls, 2)
        joiner.assert_called_once_with(["\u7532", "\u4e59", "\u4e19"])
        normalizer.assert_called_once_with("\u7532|\u4e59|\u4e19")

    def test_readonly_snapshot_never_calls_writing_resolver_or_changes_files(self):
        recognizer = ScriptedRecognizer(["\u7ed3\u679c"], [False])

        def snapshot_tree(root):
            return {
                str(path.relative_to(root)): (
                    path.stat().st_size,
                    path.stat().st_mtime_ns,
                    sha256_bytes(path.read_bytes()),
                )
                for path in root.rglob("*")
                if path.is_file()
            }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service, factory, _ = self.make_service(root, recognizer)
            before = snapshot_tree(root)
            provider = XASR.VoiceFlypyXASRProvider(
                service_module=service,
                recognizer_factory=factory,
                environment=service.readonly_environment,
            )
            provider.prepare()
            provider.transcribe(
                AudioSnapshot(np.zeros(1600, dtype=np.float32), 16_000),
                sample_id="readonly",
                timeout_seconds=1,
            )
            after = snapshot_tree(root)

        self.assertEqual(after, before)
        service.resolve_decoder_config.assert_not_called()
        service.build_combined_hotwords.assert_not_called()
        self.assertEqual(
            provider._identity["decoder_snapshot_source"],
            "existing_combined_snapshot",
        )
        self.assertNotIn("decoder_snapshot_source", provider.identity)

    def test_production_normalizer_receives_fail_closed_per_call_logger(self):
        recognizer = ScriptedRecognizer(["\u7ed3\u679c"], [False])
        received_loggers = []
        received_memory_only = []

        def production_normalizer(
            value,
            *,
            diagnostic_logger=None,
            memory_only_itn=False,
        ):
            received_loggers.append(diagnostic_logger)
            received_memory_only.append(memory_only_itn)
            return value

        with tempfile.TemporaryDirectory() as directory:
            service, factory, _ = self.make_service(
                Path(directory),
                recognizer,
                service_normalizer=production_normalizer,
            )
            provider = XASR.VoiceFlypyXASRProvider(
                service_module=service,
                recognizer_factory=factory,
                environment=service.readonly_environment,
            )
            captured = io.StringIO()
            with redirect_stdout(captured):
                provider.prepare()
                result = provider.transcribe(
                    AudioSnapshot(np.zeros(1600, dtype=np.float32), 16_000),
                    sample_id="quiet",
                    timeout_seconds=1,
                )

        self.assertEqual(result, "\u7ed3\u679c")
        self.assertEqual(captured.getvalue(), "")
        self.assertEqual(len(received_loggers), 2)
        self.assertTrue(all(callable(logger) for logger in received_loggers))
        self.assertEqual(received_memory_only, [True, True])
        self.assertEqual(
            provider.identity["normalization"]["itn_cache_policy"],
            "memory_only",
        )
        self.assertEqual(
            provider.identity["diagnostic_output_policy"],
            "fallback_signal_fail_closed_without_text",
        )

    def test_prepare_loads_once_and_prewarms_production_normalizer_once(self):
        recognizer = ScriptedRecognizer(["\u7ed3\u679c"], [False])
        with tempfile.TemporaryDirectory() as directory:
            service, factory, captured = self.make_service(Path(directory), recognizer)
            provider = XASR.VoiceFlypyXASRProvider(
                service_module=service,
                recognizer_factory=factory,
                environment=service.readonly_environment,
            )
            with self.assertRaises(XASR.VoiceFlypyXASRStreamError) as raised:
                provider.transcribe(
                    AudioSnapshot(np.zeros(1600, dtype=np.float32), 16_000),
                    sample_id="not-prepared",
                    timeout_seconds=1,
                )
            self.assertEqual(captured, [])
            provider.prepare()
            provider.prepare()
            before_transcribe = list(service.normalization_memory_only)
            result = provider.transcribe(
                AudioSnapshot(np.zeros(1600, dtype=np.float32), 16_000),
                sample_id="prepared",
                timeout_seconds=1,
            )

        self.assertEqual(result, "\u7ed3\u679c")
        self.assertEqual(raised.exception.code, "x_asr_provider_not_prepared")
        self.assertEqual(len(captured), 1)
        self.assertEqual(before_transcribe, [True])
        self.assertEqual(service.normalization_memory_only, [True, True])
        self.assertEqual(
            provider.identity["preparation"]["canonicalizer_prewarm"],
            "fixed_non_private_probe",
        )
        self.assertTrue(
            provider.identity["preparation"]["required_before_transcription"]
        )
        self.assertFalse(
            provider.identity["preparation"]["included_in_sample_latency"]
        )

    def test_production_normalization_fallback_fails_without_echoing_diagnostic(self):
        recognizer = ScriptedRecognizer(["\u7ed3\u679c"], [False])

        def fallback_normalizer(
            value,
            *,
            diagnostic_logger=None,
            memory_only_itn=False,
        ):
            del memory_only_itn
            if value != XASR._NORMALIZATION_PREWARM_TEXT:
                diagnostic_logger("private transcript/path must never escape")
            return value

        with tempfile.TemporaryDirectory() as directory:
            service, factory, _ = self.make_service(
                Path(directory),
                recognizer,
                service_normalizer=fallback_normalizer,
            )
            provider = XASR.VoiceFlypyXASRProvider(
                service_module=service,
                recognizer_factory=factory,
                environment=service.readonly_environment,
            )
            provider.prepare()
            with self.assertRaises(XASR.VoiceFlypyXASRStreamError) as caught:
                provider.transcribe(
                    AudioSnapshot(np.zeros(1600, dtype=np.float32), 16_000),
                    sample_id="quiet",
                    timeout_seconds=1,
                )

        self.assertEqual(caught.exception.code, "x_asr_normalization_fallback")
        self.assertNotIn("private", str(caught.exception))

    def test_normalization_identity_covers_code_and_loaded_semantics_anonymously(self):
        recognizer = ScriptedRecognizer(["结果"], [False])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service, _factory, _ = self.make_service(root, recognizer)
            initial = XASR._normalization_identity(service)
            service.voice_tool_state["spoken"] = ("另一个例外",)
            spoken_changed = XASR._normalization_identity(service)
            service.voice_tool_state["aliases"] = (
                ("百分之", "%", "after_number"),
            )
            aliases_changed = XASR._normalization_identity(service)
            module_bytes = service.voice_tool_path.read_bytes()
            service.voice_tool_path.write_bytes(
                bytes([module_bytes[0] ^ 1]) + module_bytes[1:]
            )
            module_changed = XASR._normalization_identity(service)

            rendered = json.dumps(initial, ensure_ascii=False, sort_keys=True)
            self.assertNotIn(str(root), rendered)
            self.assertNotIn("一点问题", rendered)
            self.assertNotIn("摄氏度", rendered)

        self.assertNotEqual(
            initial["spoken_number_exceptions"]["semantic_sha256"],
            spoken_changed["spoken_number_exceptions"]["semantic_sha256"],
        )
        self.assertEqual(
            initial["engineering_symbol_aliases"],
            spoken_changed["engineering_symbol_aliases"],
        )
        self.assertNotEqual(
            spoken_changed["engineering_symbol_aliases"]["semantic_sha256"],
            aliases_changed["engineering_symbol_aliases"]["semantic_sha256"],
        )
        self.assertEqual(
            spoken_changed["spoken_number_exceptions"],
            aliases_changed["spoken_number_exceptions"],
        )
        self.assertNotEqual(
            aliases_changed["voice_tool_module_sha256"],
            module_changed["voice_tool_module_sha256"],
        )
        self.assertEqual(
            aliases_changed["spoken_number_exceptions"],
            module_changed["spoken_number_exceptions"],
        )
        self.assertEqual(
            aliases_changed["engineering_symbol_aliases"],
            module_changed["engineering_symbol_aliases"],
        )
        self.assertEqual(initial["spoken_number_exceptions"]["entry_count"], 1)
        self.assertEqual(initial["engineering_symbol_aliases"]["entry_count"], 1)

    def test_normalization_identity_change_before_call_fails_closed(self):
        recognizer = ScriptedRecognizer(["结果"], [False])
        with tempfile.TemporaryDirectory() as directory:
            service, factory, _ = self.make_service(Path(directory), recognizer)
            provider = XASR.VoiceFlypyXASRProvider(
                service_module=service,
                recognizer_factory=factory,
                environment=service.readonly_environment,
            )
            provider.prepare()
            service.voice_tool_state["spoken"] = ("变更后的例外",)
            with self.assertRaises(XASR.VoiceFlypyXASRStreamError) as raised:
                provider.transcribe(
                    AudioSnapshot(np.zeros(1600, dtype=np.float32), 16_000),
                    sample_id="private-sample",
                    timeout_seconds=1,
                )

        self.assertEqual(
            raised.exception.code,
            "x_asr_normalization_identity_changed",
        )
        self.assertEqual(service.normalization_memory_only, [True])

    def test_normalization_identity_change_during_call_wins_over_result_or_error(self):
        for raise_private_error in (False, True):
            with self.subTest(raise_private_error=raise_private_error):
                recognizer = ScriptedRecognizer(["结果"], [False])
                holder = {}

                def mutating_normalizer(
                    value,
                    *,
                    diagnostic_logger=None,
                    memory_only_itn=False,
                ):
                    del diagnostic_logger, memory_only_itn
                    if value != XASR._NORMALIZATION_PREWARM_TEXT:
                        holder["service"].voice_tool_state["aliases"] = (
                            ("百分之", "%", "after_number"),
                        )
                        if raise_private_error:
                            raise RuntimeError("private transcript must not escape")
                    return value

                with tempfile.TemporaryDirectory() as directory:
                    service, factory, _ = self.make_service(
                        Path(directory),
                        recognizer,
                        service_normalizer=mutating_normalizer,
                    )
                    holder["service"] = service
                    provider = XASR.VoiceFlypyXASRProvider(
                        service_module=service,
                        recognizer_factory=factory,
                        environment=service.readonly_environment,
                    )
                    provider.prepare()
                    with self.assertRaises(
                        XASR.VoiceFlypyXASRStreamError
                    ) as raised:
                        provider.transcribe(
                            AudioSnapshot(
                                np.zeros(1600, dtype=np.float32),
                                16_000,
                            ),
                            sample_id="private-sample",
                            timeout_seconds=1,
                        )

                self.assertEqual(
                    raised.exception.code,
                    "x_asr_normalization_identity_changed",
                )
                self.assertNotIn("private", str(raised.exception))

    def test_normalization_resource_identity_rejects_unbounded_entry_count(self):
        entries = [""] * (XASR._MAX_NORMALIZATION_RESOURCE_ENTRIES + 1)
        with self.assertRaises(XASR.VoiceFlypyXASRLoadError) as raised:
            XASR._semantic_resource_identity(
                "spoken_number_exceptions",
                entries,
                row_width=1,
            )
        self.assertEqual(
            raised.exception.code,
            "x_asr_normalization_resource_invalid",
        )
        oversized = "x" * (
            XASR._MAX_NORMALIZATION_RESOURCE_IDENTITY_BYTES + 1
        )
        with self.assertRaises(XASR.VoiceFlypyXASRLoadError) as oversized_raised:
            XASR._semantic_resource_identity(
                "spoken_number_exceptions",
                [oversized],
                row_width=1,
            )
        self.assertEqual(
            oversized_raised.exception.code,
            "x_asr_normalization_resource_invalid",
        )

    def test_timeout_worker_never_rebinds_process_stdout_or_stderr(self):
        recognizer = ScriptedRecognizer(["\u7ed3\u679c"], [False])
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        received_memory_only = []

        def blocking_production(
            value,
            *,
            diagnostic_logger=None,
            memory_only_itn=False,
        ):
            received_memory_only.append(memory_only_itn)
            if value == XASR._NORMALIZATION_PREWARM_TEXT:
                return value
            entered.set()
            release.wait(1)
            finished.set()
            return value

        with tempfile.TemporaryDirectory() as directory:
            service, factory, _ = self.make_service(
                Path(directory),
                recognizer,
                service_normalizer=blocking_production,
            )
            provider = XASR.VoiceFlypyXASRProvider(
                service_module=service,
                recognizer_factory=factory,
                environment=service.readonly_environment,
            )
            provider.prepare()
            original_stdout = sys.stdout
            original_stderr = sys.stderr
            gate = EVAL._ProviderCallGate()
            with self.assertRaises(EVAL.EvaluationAbortedError):
                gate.invoke(
                    provider,
                    AudioSnapshot(np.zeros(1600, dtype=np.float32), 16_000),
                    role="baseline",
                    sample_id="timeout",
                    timeout_seconds=0.02,
                )
            self.assertTrue(entered.is_set())
            self.assertEqual(received_memory_only, [True, True])
            self.assertIs(sys.stdout, original_stdout)
            self.assertIs(sys.stderr, original_stderr)
            release.set()
            self.assertTrue(finished.wait(1))

    def test_concurrent_transcriptions_are_serialized_through_normalization(self):
        recognizer = ScriptedRecognizer(["结果"], [False])
        first_entered = threading.Event()
        release_first = threading.Event()
        second_started = threading.Event()
        normalizer_calls = []
        errors = []

        def blocking_normalizer(value):
            normalizer_calls.append(value)
            if len(normalizer_calls) == 1:
                first_entered.set()
                release_first.wait(1)
            return value

        with tempfile.TemporaryDirectory() as directory:
            service, factory, _ = self.make_service(Path(directory), recognizer)
            provider = XASR.VoiceFlypyXASRProvider(
                service_module=service,
                recognizer_factory=factory,
                normalizer=blocking_normalizer,
                environment=service.readonly_environment,
            )
            provider.prepare()
            audio = AudioSnapshot(np.zeros(1600, dtype=np.float32), 16_000)

            def invoke(*, announce=None):
                if announce is not None:
                    announce.set()
                try:
                    provider.transcribe(
                        audio,
                        sample_id="private-sample",
                        timeout_seconds=1,
                    )
                except BaseException as exc:
                    errors.append(exc)

            first = threading.Thread(target=invoke)
            second = threading.Thread(
                target=invoke,
                kwargs={"announce": second_started},
            )
            first.start()
            self.assertTrue(first_entered.wait(1))
            second.start()
            self.assertTrue(second_started.wait(1))
            time.sleep(0.05)
            stream_count_while_blocked = len(recognizer.created_with)
            normalizer_count_while_blocked = len(normalizer_calls)
            release_first.set()
            first.join(1)
            second.join(1)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(stream_count_while_blocked, 1)
        self.assertEqual(normalizer_count_while_blocked, 1)
        self.assertEqual(len(recognizer.created_with), 2)
        self.assertEqual(len(normalizer_calls), 2)

    def test_inline_hotword_stream_failure_uses_plain_stream_and_marks_approximation(self):
        recognizer = ScriptedRecognizer(
            ["\u7ed3\u679c"],
            [False],
            reject_inline=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            service, factory, _ = self.make_service(Path(directory), recognizer)
            provider = XASR.VoiceFlypyXASRProvider(
                service_module=service,
                recognizer_factory=factory,
                environment=service.readonly_environment,
            )
            provider.prepare()
            result = provider.transcribe(
                AudioSnapshot(np.zeros(1600, dtype=np.float32), 16_000),
                sample_id="fallback",
                timeout_seconds=1,
            )

        self.assertEqual(result, "\u7ed3\u679c")
        self.assertEqual(recognizer.created_with, [(service.load_inline_hotword_snapshot.return_value.text,), ()])
        self.assertTrue(provider.identity["stream_hotword_fallback"])
        self.assertEqual(
            provider.identity["baseline_fidelity"],
            "hotword_stream_approximation",
        )

    def test_decoder_factory_greedy_fallback_is_never_labeled_formal(self):
        recognizer = ScriptedRecognizer(["\u7ed3\u679c"], [False])
        calls = []

        def factory(**kwargs):
            calls.append(kwargs["decoding_method"])
            if kwargs["decoding_method"] == "modified_beam_search":
                raise RuntimeError("decoder unavailable")
            return recognizer

        with tempfile.TemporaryDirectory() as directory:
            service, _, _ = self.make_service(
                Path(directory),
                recognizer,
                factory=factory,
            )
            provider = XASR.VoiceFlypyXASRProvider(
                service_module=service,
                recognizer_factory=factory,
                environment=service.readonly_environment,
            )
            provider.prepare()
            provider.transcribe(
                AudioSnapshot(np.zeros(1600, dtype=np.float32), 16_000),
                sample_id="greedy",
                timeout_seconds=1,
            )

        self.assertEqual(calls, ["modified_beam_search", "greedy_search"])
        self.assertTrue(provider.identity["decoder_fallback"])
        self.assertFalse(provider.identity["decoder_configuration_match"])
        self.assertEqual(provider.identity["baseline_fidelity"], "greedy_approximation")

    def test_equal_length_model_rewrite_inside_factory_is_rejected(self):
        recognizer = ScriptedRecognizer(["never"], [False])

        def mutating_factory(**kwargs):
            encoder = Path(kwargs["encoder"])
            original = encoder.read_bytes()
            replacement = bytes([original[0] ^ 1]) + original[1:]
            self.assertEqual(len(replacement), len(original))
            encoder.write_bytes(replacement)
            return recognizer

        with tempfile.TemporaryDirectory() as directory:
            service, _, _ = self.make_service(Path(directory), recognizer)
            original_size = (service.MODEL_DIR / "encoder.int8.onnx").stat().st_size
            provider = XASR.VoiceFlypyXASRProvider(
                service_module=service,
                recognizer_factory=mutating_factory,
                environment=service.readonly_environment,
            )
            with self.assertRaises(XASR.VoiceFlypyXASRLoadError) as raised:
                provider._ensure_loaded()
            rewritten_size = (service.MODEL_DIR / "encoder.int8.onnx").stat().st_size

        self.assertEqual(raised.exception.code, "x_asr_model_changed_during_load")
        self.assertEqual(rewritten_size, original_size)
        self.assertEqual(
            provider.identity["error_code"],
            "x_asr_model_changed_during_load",
        )

    def test_equal_length_bpe_rewrite_inside_factory_is_rejected(self):
        recognizer = ScriptedRecognizer(["never"], [False])

        def mutating_factory(**kwargs):
            bpe = Path(kwargs["bpe_vocab"])
            original = bpe.read_bytes()
            bpe.write_bytes(bytes([original[0] ^ 1]) + original[1:])
            return recognizer

        with tempfile.TemporaryDirectory() as directory:
            service, _, _ = self.make_service(Path(directory), recognizer)
            provider = XASR.VoiceFlypyXASRProvider(
                service_module=service,
                recognizer_factory=mutating_factory,
                environment=service.readonly_environment,
            )
            with self.assertRaises(XASR.VoiceFlypyXASRLoadError) as raised:
                provider.prepare()

        self.assertEqual(raised.exception.code, "x_asr_decoder_changed_during_load")

    def test_equal_length_hotword_rewrite_inside_snapshot_load_is_rejected(self):
        recognizer = ScriptedRecognizer(["never"], [False])
        with tempfile.TemporaryDirectory() as directory:
            service, factory, _ = self.make_service(Path(directory), recognizer)
            original_snapshot = service.load_inline_hotword_snapshot.return_value

            def mutating_snapshot(decoder):
                hotwords = Path(decoder.hotwords_file)
                original = hotwords.read_bytes()
                hotwords.write_bytes(bytes([original[0] ^ 1]) + original[1:])
                return original_snapshot

            service.load_inline_hotword_snapshot.side_effect = mutating_snapshot
            provider = XASR.VoiceFlypyXASRProvider(
                service_module=service,
                recognizer_factory=factory,
                environment=service.readonly_environment,
            )
            with self.assertRaises(XASR.VoiceFlypyXASRLoadError) as raised:
                provider.prepare()

        self.assertEqual(
            raised.exception.code,
            "x_asr_hotword_changed_during_snapshot",
        )

    def test_recognizer_identity_is_path_independent(self):
        identities = []
        providers = []
        roots = []
        private_hashes = []
        private_decoder_identities = []
        private_phrases = []
        hotword_rows = ("量 子 :2.5\n", "私 密 :3.0\n")
        for index in range(2):
            temporary = tempfile.TemporaryDirectory()
            self.addCleanup(temporary.cleanup)
            root = Path(temporary.name)
            roots.append(str(root))
            recognizer = ScriptedRecognizer(["\u7ed3\u679c"], [False])
            service, factory, _ = self.make_service(root, recognizer)
            bpe_path = Path(
                service.readonly_environment["VOICEFLYPY_BPE_VOCAB"]
            )
            bpe_path.write_text(
                "".join(
                    f"{'token' if index == 0 else 'vocab'}{item}\t0\n"
                    for item in range(1000)
                ),
                encoding="utf-8",
            )
            hotword_data = hotword_rows[index].encode("utf-8")
            hotword_path = Path(
                service.readonly_environment["VOICEFLYPY_COMBINED_HOTWORDS"]
            )
            hotword_path.write_bytes(hotword_data)
            service.load_inline_hotword_snapshot.return_value = (
                types.SimpleNamespace(
                    text=hotword_data.decode("utf-8"),
                    sha256=sha256_bytes(hotword_data),
                    entries=1,
                    byte_count=len(hotword_data),
                )
            )
            private_phrase = f"私密词条-{root}"
            private_phrases.append(private_phrase)
            service.voice_tool_state["spoken"] = (private_phrase,)
            provider = XASR.VoiceFlypyXASRProvider(
                service_module=service,
                recognizer_factory=factory,
                environment=service.readonly_environment,
            )
            provider._ensure_loaded()
            exposed_identity = provider.identity
            original_runtime_version = exposed_identity["normalization"][
                "runtime_versions"
            ]["pynini"]
            exposed_identity["normalization"]["runtime_versions"][
                "pynini"
            ] = "tampered"
            self.assertEqual(
                provider.identity["normalization"]["runtime_versions"][
                    "pynini"
                ],
                original_runtime_version,
            )
            private_hashes.extend(
                (
                    provider._identity["lexicon_sha256"],
                    provider._identity["requested_decoder_sha256"],
                    provider._identity["effective_decoder_sha256"],
                    provider._identity["normalization"][
                        "spoken_number_exceptions"
                    ]["semantic_sha256"],
                )
            )
            private_decoder_identities.append(
                (
                    provider._identity["requested_decoder_sha256"],
                    provider._identity["effective_decoder_sha256"],
                )
            )
            identities.append(provider.identity)
            providers.append(provider)

        self.assertEqual(
            identities[0]["recognizer_options_sha256"],
            identities[1]["recognizer_options_sha256"],
        )
        self.assertNotEqual(
            private_decoder_identities[0],
            private_decoder_identities[1],
        )
        self.assertEqual(identities[0], identities[1])
        self.assertEqual(
            EVAL._provider_identity_sha256(providers[0]),
            EVAL._provider_identity_sha256(providers[1]),
        )
        for identity, root in zip(identities, roots):
            rendered = json.dumps(identity, sort_keys=True)
            self.assertNotIn(root, rendered)
            self.assertNotIn("model_dir_name", identity)
            for field in XASR._PRIVATE_XASR_IDENTITY_FIELDS:
                self.assertNotIn(field, identity)
            for field in XASR._PRIVATE_NORMALIZATION_IDENTITY_FIELDS:
                self.assertNotIn(field, identity["normalization"])
            for private_phrase in private_phrases:
                self.assertNotIn(private_phrase, rendered)
            for private_sha256 in private_hashes:
                self.assertNotIn(private_sha256, rendered)

    def test_missing_model_files_fail_before_decoder_or_recognizer(self):
        recognizer = ScriptedRecognizer(["never"], [False])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service, factory, captured = self.make_service(root, recognizer)
            for name in XASR.REQUIRED_MODEL_FILES:
                (service.MODEL_DIR / name).unlink()
            provider = XASR.VoiceFlypyXASRProvider(
                service_module=service,
                recognizer_factory=factory,
                environment=service.readonly_environment,
            )
            with self.assertRaises(XASR.VoiceFlypyXASRLoadError) as raised:
                provider.prepare()

        self.assertEqual(raised.exception.code, "x_asr_model_files_missing")
        service.resolve_decoder_config.assert_not_called()
        self.assertEqual(captured, [])
        self.assertEqual(provider.identity["load_status"], "failed")
        self.assertEqual(provider.identity["error_code"], "x_asr_model_files_missing")


if __name__ == "__main__":
    unittest.main()
