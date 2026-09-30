import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import voice_hotwords as HOTWORDS
from voice_hotwords import (
    BPE_VOCAB_ENV,
    COMBINED_HOTWORDS_ENV,
    CUSTOM_PHRASES_ENV,
    DISABLE_ENV,
    HOTWORDS_FILE_ENV,
    LEARNED_PHRASES_ENV,
    PERSONAL_USAGE_ENV,
    SYMBOL_ALIASES_ENV,
    create_recognizer_with_fallback,
    build_combined_hotwords,
    decoder_resource_fingerprint,
    resolve_decoder_config,
    should_reload_decoder,
    validate_bpe_vocab,
    validate_hotwords,
    validate_hotwords_text,
)


def write_vocab(path, count=1000):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"token{index}\t{-index / 10}\n" for index in range(count)), encoding="utf-8")


def write_hotwords(path, content="RAG :1.5\n夏 普 比 率 :2.5\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class HotwordConfigTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.model = root / "model"
        self.resource = root / "resource"
        self.source = root / "source"
        self.live = root / "live"

    def tearDown(self):
        self.tempdir.cleanup()

    def valid_resources(self):
        write_vocab(self.model / "bpe.vocab")
        write_hotwords(self.live / "hotwords.txt")

    def resolve(self, env=None):
        return resolve_decoder_config(
            self.model,
            self.resource,
            self.source,
            env={} if env is None else env,
            live_lexicon_root=self.live,
        )

    def test_valid_resources_enable_expected_decoder_parameters(self):
        self.valid_resources()
        config = self.resolve()
        self.assertTrue(config.enabled)
        self.assertEqual(config.decoding_method, "modified_beam_search")
        self.assertEqual(config.max_active_paths, 8)
        self.assertEqual(config.modeling_unit, "bpe")
        self.assertEqual(config.hotwords_score, 1.5)
        self.assertEqual(config.recognizer_kwargs()["hotwords_file"], str(self.live / "hotwords.txt"))
        inline_options = config.recognizer_kwargs(include_hotwords_file=False)
        self.assertNotIn("hotwords_file", inline_options)
        self.assertEqual(inline_options["decoding_method"], "modified_beam_search")
        self.assertEqual(inline_options["hotwords_score"], 1.5)

    def test_disable_environment_forces_greedy(self):
        self.valid_resources()
        config = self.resolve({DISABLE_ENV: "true"})
        self.assertFalse(config.enabled)
        self.assertEqual(config.recognizer_kwargs(), {"decoding_method": "greedy_search"})

    def test_missing_resource_falls_back_to_greedy_with_clear_reason(self):
        write_vocab(self.model / "bpe.vocab")
        config = self.resolve()
        self.assertFalse(config.enabled)
        self.assertIn("hotwords.txt", config.reason)

    def test_invalid_live_file_falls_back_to_valid_bundle(self):
        write_vocab(self.model / "bpe.vocab")
        write_hotwords(self.live / "hotwords.txt", "not scored\n")
        write_hotwords(self.resource / "lexicon" / "hotwords.txt")
        config = self.resolve()
        self.assertTrue(config.enabled)
        self.assertEqual(config.hotwords_file, self.resource / "lexicon" / "hotwords.txt")

    def test_one_line_fake_vocab_is_rejected(self):
        write_vocab(self.model / "bpe.vocab", count=1)
        write_hotwords(self.live / "hotwords.txt")
        config = self.resolve()
        self.assertFalse(config.enabled)
        self.assertIn("only 1 token", config.reason)

    def test_bad_hotword_scores_are_rejected(self):
        write_vocab(self.model / "bpe.vocab")
        for content in ("RAG\n", "RAG :nan\n", "RAG :6\n", " :1.5\n"):
            with self.subTest(content=content):
                path = self.live / "hotwords.txt"
                write_hotwords(path, content)
                # Explicit unique path prevents a filesystem with coarse mtimes
                # from reusing a prior validation-cache entry of the same size.
                alternate = self.live / f"bad-{len(content)}-{content.encode().hex()}.txt"
                write_hotwords(alternate, content)
                config = self.resolve({HOTWORDS_FILE_ENV: str(alternate)})
                self.assertFalse(config.enabled)

    def test_public_format_validators(self):
        vocab = self.model / "bpe.vocab"
        hotwords = self.live / "hotwords.txt"
        write_vocab(vocab)
        write_hotwords(hotwords)
        self.assertTrue(validate_bpe_vocab(vocab)[0])
        self.assertTrue(validate_hotwords(hotwords)[0])
        self.assertTrue(validate_hotwords_text("测 试 :1.5\n")[0])
        self.assertFalse(validate_hotwords_text("broken\n")[0])

    def test_environment_paths_take_precedence(self):
        self.valid_resources()
        explicit_vocab = self.source / "chosen.vocab"
        explicit_hotwords = self.source / "chosen.hotwords"
        write_vocab(explicit_vocab)
        write_hotwords(explicit_hotwords)
        config = self.resolve(
            {
                BPE_VOCAB_ENV: str(explicit_vocab),
                HOTWORDS_FILE_ENV: str(explicit_hotwords),
            }
        )
        self.assertEqual(config.bpe_vocab, explicit_vocab)
        self.assertEqual(config.hotwords_file, explicit_hotwords)

    def test_fingerprint_changes_with_hotword_file(self):
        self.valid_resources()
        before = decoder_resource_fingerprint(self.resolve())
        write_hotwords(self.live / "hotwords.txt", "RAG :1.5\nCodex :1.5\nTPD :1.5\n")
        after = decoder_resource_fingerprint(self.resolve())
        self.assertNotEqual(before, after)
        self.assertEqual(after[-1].path, str(self.live / "hotwords.txt"))
        self.assertTrue(should_reload_decoder(before, self.resolve()))
        self.assertFalse(should_reload_decoder(after, self.resolve()))

    def test_custom_phrases_are_merged_but_single_chars_and_low_weights_are_not(self):
        self.valid_resources()
        custom = self.live / "custom_phrase.txt"
        custom.write_text(
            "单清\tujqk\t600\n任艺\trfyi\t200\n单\tuj\t500\n低频词\tdipici\t20\n",
            encoding="utf-8",
        )
        combined = self.live / "personal-combined.txt"
        config = self.resolve(
            {
                CUSTOM_PHRASES_ENV: str(custom),
                COMBINED_HOTWORDS_ENV: str(combined),
            }
        )
        text = config.hotwords_file.read_text(encoding="utf-8")
        self.assertEqual(config.hotwords_file, combined)
        self.assertIn("单 清 :3.5", text)
        self.assertIn("任 艺 :3", text)
        self.assertNotIn("单 :", text)
        self.assertNotIn("低 频 词", text)

    def test_static_and_learned_phrase_files_are_independent_merge_sources(self):
        self.valid_resources()
        static = self.live / "custom_phrase.txt"
        learned = self.live / "voiceflypy_learned_phrases.txt"
        combined = self.live / "separate-sources-combined.txt"
        static.write_text("静态词\tjktyci\t100\n", encoding="utf-8")
        learned.write_text("刚学会\tghxthv\t200\n", encoding="utf-8")

        config = self.resolve(
            {
                CUSTOM_PHRASES_ENV: str(static),
                LEARNED_PHRASES_ENV: str(learned),
                COMBINED_HOTWORDS_ENV: str(combined),
            }
        )

        text = config.hotwords_file.read_text(encoding="utf-8")
        self.assertIn("静 态 词 :2.5", text)
        self.assertIn("刚 学 会 :4", text)

    def test_two_column_learned_phrase_uses_explicit_default_weight(self):
        self.valid_resources()
        learned = self.live / "voiceflypy_learned_phrases.txt"
        combined = self.live / "two-column-combined.txt"
        learned.write_text("两列词\tldlci\n", encoding="utf-8")

        config = self.resolve(
            {
                LEARNED_PHRASES_ENV: str(learned),
                COMBINED_HOTWORDS_ENV: str(combined),
            }
        )

        self.assertIsNone(config.resource_error)
        self.assertIn("两 列 词 :4", combined.read_text(encoding="utf-8"))

    def test_invalid_learned_phrase_code_is_reported_and_old_generation_survives(self):
        self.valid_resources()
        learned = self.live / "voiceflypy_learned_phrases.txt"
        combined = self.live / "invalid-learned-combined.txt"
        write_hotwords(combined, "旧 学 词 :3\n")
        learned.write_text("坏编码\t!!!\t200\n", encoding="utf-8")

        config = self.resolve(
            {
                LEARNED_PHRASES_ENV: str(learned),
                COMBINED_HOTWORDS_ENV: str(combined),
            }
        )

        self.assertIsNotNone(config.resource_error)
        self.assertEqual(combined.read_text(encoding="utf-8"), "旧 学 词 :3\n")

    def test_learned_phrase_length_matches_swift_store_contract(self):
        self.valid_resources()
        learned = self.live / "voiceflypy_learned_phrases.txt"
        learned.write_text("超过十二个汉字长度的学习词条\tabcdefghijkl\t200\n", encoding="utf-8")
        config = self.resolve({LEARNED_PHRASES_ENV: str(learned)})
        self.assertIsNotNone(config.resource_error)
        self.assertIn("invalid text", config.resource_error)

    def test_default_learned_phrase_path_is_discovered_for_live_service(self):
        fake_home = Path(self.tempdir.name) / "home"
        live = (
            fake_home
            / "Library/Mobile Documents/com~apple~CloudDocs/AppConfig/RimeSync"
            / "VoiceFlypyLexicon"
        )
        write_vocab(self.model / "bpe.vocab")
        write_hotwords(live / "hotwords.txt")
        learned = fake_home / "Library/Rime-VoiceFlypy/voiceflypy_learned_phrases.txt"
        learned.parent.mkdir(parents=True, exist_ok=True)
        learned.write_text("默认学习\tmrxhxt\t200\n", encoding="utf-8")

        with mock.patch.object(Path, "home", return_value=fake_home):
            config = resolve_decoder_config(
                self.model,
                self.resource,
                self.source,
                env={},
            )

        self.assertTrue(config.enabled)
        self.assertIn("默 认 学 习 :4", config.hotwords_file.read_text(encoding="utf-8"))

    def test_live_service_reads_local_usage_snapshot_instead_of_icloud_ledger(self):
        fake_home = Path(self.tempdir.name) / "home"
        live = (
            fake_home
            / "Library/Mobile Documents/com~apple~CloudDocs/AppConfig/RimeSync"
            / "VoiceFlypyLexicon"
        )
        write_vocab(self.model / "bpe.vocab")
        write_hotwords(live / "hotwords.txt")
        runtime = (
            fake_home
            / "Library/Application Support/VoiceFlypy/Lexicon/personal_usage.json"
        )
        runtime.parent.mkdir(parents=True, exist_ok=True)
        runtime.write_text(
            '{"schema_version":"1.0.0","terms":{"紧凶":'
            '{"flypy_code":"jbxs","selection_count":0,"explicit_count":1,'
            '"updated_at":"2026-09-08T00:00:00Z"}}}',
            encoding="utf-8",
        )
        # A directory at the old cloud-ledger path proves the service no longer
        # even attempts to consume that runtime-unsafe location.
        (live / "personal_usage.json").mkdir()

        with mock.patch.object(Path, "home", return_value=fake_home):
            config = resolve_decoder_config(
                self.model,
                self.resource,
                self.source,
                env={},
            )

        self.assertIsNone(config.resource_error)
        self.assertIn("紧 凶 :4", config.hotwords_file.read_text(encoding="utf-8"))

    def test_packaged_symbol_aliases_precede_a_runtime_unsafe_cloud_placeholder(self):
        fake_home = Path(self.tempdir.name) / "home"
        live = (
            fake_home
            / "Library/Mobile Documents/com~apple~CloudDocs/AppConfig/RimeSync"
            / "VoiceFlypyLexicon"
        )
        write_vocab(self.model / "bpe.vocab")
        write_hotwords(live / "hotwords.txt")
        bundled = self.resource / "lexicon" / "engineering_symbol_aliases.tsv"
        bundled.parent.mkdir(parents=True, exist_ok=True)
        bundled.write_text("正负号\t±\texplicit\n", encoding="utf-8")
        (live / "engineering_symbol_aliases.tsv").mkdir()

        with mock.patch.object(Path, "home", return_value=fake_home):
            config = resolve_decoder_config(
                self.model,
                self.resource,
                self.source,
                env={},
            )

        self.assertIsNone(config.resource_error)
        self.assertIn("正 负 号 :2.5", config.hotwords_file.read_text(encoding="utf-8"))

    def test_malformed_personal_source_preserves_previous_combined_generation(self):
        self.valid_resources()
        custom = self.live / "custom_phrase.txt"
        combined = self.live / "preserved-combined.txt"
        old_content = "旧 热 词 :3\n"
        write_hotwords(combined, old_content)
        custom.write_text("损坏的行没有制表字段\n", encoding="utf-8")

        config = self.resolve(
            {
                CUSTOM_PHRASES_ENV: str(custom),
                COMBINED_HOTWORDS_ENV: str(combined),
            }
        )

        self.assertTrue(config.enabled)
        self.assertEqual(config.hotwords_file, combined)
        self.assertIsNotNone(config.resource_error)
        self.assertIn("line 1", config.resource_error)
        self.assertEqual(combined.read_text(encoding="utf-8"), old_content)

    def test_abnormal_default_source_is_not_treated_as_optional_absence(self):
        fake_home = Path(self.tempdir.name) / "home"
        live = (
            fake_home
            / "Library/Mobile Documents/com~apple~CloudDocs/AppConfig/RimeSync"
            / "VoiceFlypyLexicon"
        )
        write_vocab(self.model / "bpe.vocab")
        write_hotwords(live / "hotwords.txt")
        rime = fake_home / "Library/Rime-VoiceFlypy"
        rime.mkdir(parents=True)
        (rime / "custom_phrase.txt").write_text("静态词\tjtyc\t100\n", encoding="utf-8")
        (rime / "voiceflypy_learned_phrases.txt").mkdir()
        combined = fake_home / "Library/Caches/VoiceFlypy/lexicon/combined_hotwords.txt"
        write_hotwords(combined, "旧 学 词 :3\n")

        with mock.patch.object(Path, "home", return_value=fake_home):
            config = resolve_decoder_config(self.model, self.resource, self.source, env={})

        self.assertIsNotNone(config.resource_error)
        self.assertIn("not a regular file", config.resource_error)
        self.assertEqual(config.hotwords_file, combined)
        self.assertEqual(combined.read_text(encoding="utf-8"), "旧 学 词 :3\n")

    def test_default_source_stat_permission_error_fails_closed(self):
        candidate = self.live / "voiceflypy_learned_phrases.txt"
        with mock.patch.object(Path, "lstat", side_effect=PermissionError("denied")):
            with self.assertRaisesRegex(HOTWORDS.PersonalResourceError, "cannot inspect"):
                HOTWORDS._first_file([candidate], "learned phrase source")

    def test_failed_temp_validation_never_replaces_existing_combined_file(self):
        self.valid_resources()
        combined = self.live / "atomic-combined.txt"
        old_content = "旧 版 :2\n"
        write_hotwords(combined, old_content)
        learned = self.live / "voiceflypy_learned_phrases.txt"
        learned.write_text("新词\txbci\t200\n", encoding="utf-8")
        validated_paths = []

        def reject_temp(path):
            validated_paths.append(path)
            return False, "forced validation failure"

        with mock.patch.object(HOTWORDS, "validate_hotwords", side_effect=reject_temp):
            with self.assertRaisesRegex(OSError, "before publish"):
                build_combined_hotwords(
                    self.live / "hotwords.txt",
                    combined,
                    learned_phrases=learned,
                )

        self.assertEqual(combined.read_text(encoding="utf-8"), old_content)
        self.assertEqual(len(validated_paths), 1)
        self.assertNotEqual(validated_paths[0], combined)
        self.assertFalse(validated_paths[0].exists())

    def test_successful_publish_fsyncs_file_and_parent_directory(self):
        self.valid_resources()
        learned = self.live / "voiceflypy_learned_phrases.txt"
        learned.write_text("新词\txbci\t200\n", encoding="utf-8")
        combined = self.live / "durable-combined.txt"
        real_fsync = os.fsync
        synced_descriptors = []

        def tracking_fsync(descriptor):
            synced_descriptors.append(descriptor)
            return real_fsync(descriptor)

        with mock.patch.object(HOTWORDS.os, "fsync", side_effect=tracking_fsync):
            build_combined_hotwords(
                self.live / "hotwords.txt",
                combined,
                learned_phrases=learned,
            )

        self.assertEqual(len(synced_descriptors), 2)
        self.assertTrue(validate_hotwords(combined)[0])

    def test_missing_explicit_personal_source_is_reported_not_silently_ignored(self):
        self.valid_resources()
        missing = self.live / "does-not-exist.json"
        config = self.resolve({PERSONAL_USAGE_ENV: str(missing)})
        self.assertTrue(config.enabled)
        self.assertIsNotNone(config.resource_error)
        self.assertIn("unreadable", config.resource_error)

    def test_unknown_personal_usage_schema_is_reported_and_not_merged(self):
        self.valid_resources()
        usage = self.live / "personal_usage.json"
        usage.write_text('{"schema_version":"2.0.0","terms":{}}', encoding="utf-8")
        config = self.resolve({PERSONAL_USAGE_ENV: str(usage)})
        self.assertIsNotNone(config.resource_error)
        self.assertIn("schema_version", config.resource_error)

    def test_incomplete_personal_usage_record_is_reported_and_not_merged(self):
        self.valid_resources()
        usage = self.live / "personal_usage.json"
        usage.write_text(
            '{"schema_version":"1.0.0","terms":{"残缺记录":'
            '{"selection_count":2,"explicit_count":0,"updated_at":""}}}',
            encoding="utf-8",
        )
        config = self.resolve({PERSONAL_USAGE_ENV: str(usage)})
        self.assertIsNotNone(config.resource_error)
        self.assertIn("does not match schema", config.resource_error)

    def test_invalid_symbol_mode_is_reported(self):
        self.valid_resources()
        aliases = self.live / "engineering_symbol_aliases.tsv"
        aliases.write_text("正负号\t±\tunknown\n", encoding="utf-8")
        config = self.resolve({SYMBOL_ALIASES_ENV: str(aliases)})
        self.assertIsNotNone(config.resource_error)
        self.assertIn("invalid required fields", config.resource_error)

    def test_fingerprint_hash_detects_same_size_same_mtime_rewrite(self):
        self.valid_resources()
        path = self.live / "hotwords.txt"
        before_stat = path.stat()
        before = decoder_resource_fingerprint(self.resolve())
        original = path.read_text(encoding="utf-8")
        changed = original.replace("RAG", "TAG")
        self.assertEqual(len(original.encode()), len(changed.encode()))
        path.write_text(changed, encoding="utf-8")
        os.utime(path, ns=(before_stat.st_atime_ns, before_stat.st_mtime_ns))

        requested = self.resolve()
        after = decoder_resource_fingerprint(requested)
        self.assertNotEqual(before, after)
        self.assertNotEqual(before[-1].sha256, after[-1].sha256)
        self.assertTrue(should_reload_decoder(before, requested))

    def test_passive_candidate_usage_never_biases_asr_but_explicit_learning_does(self):
        self.valid_resources()
        usage = self.live / "personal_usage.json"
        usage.write_text(
            """{
              "schema_version": "1.0.0",
              "terms": {
                "候选词": {"flypy_code": "hxxtci", "selection_count": 2, "explicit_count": 0, "updated_at": ""},
                "高频词": {"flypy_code": "gcbbci", "selection_count": 999, "explicit_count": 0, "updated_at": ""},
                "刚记录": {"flypy_code": "ghjilu", "selection_count": 0, "explicit_count": 1, "updated_at": ""},
                "未达标": {"flypy_code": "wwdbbc", "selection_count": 1, "explicit_count": 0, "updated_at": ""}
              }
            }""",
            encoding="utf-8",
        )
        combined = self.live / "usage-combined.txt"
        config = self.resolve(
            {
                PERSONAL_USAGE_ENV: str(usage),
                COMBINED_HOTWORDS_ENV: str(combined),
            }
        )
        text = config.hotwords_file.read_text(encoding="utf-8")
        self.assertNotIn("候 选 词", text)
        self.assertNotIn("高 频 词", text)
        self.assertIn("刚 记 录 :4", text)
        self.assertNotIn("未 达 标", text)

    def test_engineering_symbol_names_bias_spoken_names_instead_of_raw_glyphs(self):
        self.valid_resources()
        aliases = self.live / "engineering_symbol_aliases.tsv"
        aliases.write_text(
            "正负号\t±\texplicit\n摄氏度\t°C\tafter_number\n",
            encoding="utf-8",
        )
        combined = self.live / "symbol-combined.txt"
        config = self.resolve(
            {
                SYMBOL_ALIASES_ENV: str(aliases),
                COMBINED_HOTWORDS_ENV: str(combined),
            }
        )
        text = config.hotwords_file.read_text(encoding="utf-8")
        self.assertIn("正 负 号 :2.5", text)
        self.assertIn("摄 氏 度 :2.5", text)
        self.assertNotIn("±", text)

    def test_personal_change_rebuilds_combined_file_and_triggers_reload(self):
        self.valid_resources()
        custom = self.live / "custom_phrase.txt"
        combined = self.live / "reload-combined.txt"
        environment = {
            CUSTOM_PHRASES_ENV: str(custom),
            COMBINED_HOTWORDS_ENV: str(combined),
        }
        custom.write_text("单清\tujqk\t200\n", encoding="utf-8")
        before = decoder_resource_fingerprint(self.resolve(environment))
        custom.write_text("单清\tujqk\t200\n任艺\trfyi\t200\n", encoding="utf-8")
        requested = self.resolve(environment)
        after = decoder_resource_fingerprint(requested)
        self.assertNotEqual(before, after)
        self.assertTrue(should_reload_decoder(before, requested))
        self.assertIn("任 艺 :3", combined.read_text(encoding="utf-8"))


class RecognizerFallbackTests(unittest.TestCase):
    def test_inline_hotword_recognizer_omits_file_but_keeps_tokenizer(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            vocab = root / "bpe.vocab"
            hotwords = root / "hotwords.txt"
            write_vocab(vocab)
            write_hotwords(hotwords)
            config = resolve_decoder_config(
                root,
                root,
                root,
                env={BPE_VOCAB_ENV: str(vocab), HOTWORDS_FILE_ENV: str(hotwords)},
                live_lexicon_root=root / "unused",
            )
            calls = []
            recognizer, active = create_recognizer_with_fallback(
                lambda **kwargs: calls.append(kwargs) or "inline-recognizer",
                {"tokens": "tokens.txt"},
                config,
                include_hotwords_file=False,
            )

        self.assertEqual(recognizer, "inline-recognizer")
        self.assertTrue(active.enabled)
        self.assertNotIn("hotwords_file", calls[0])
        self.assertEqual(calls[0]["bpe_vocab"], str(vocab))

    def test_hotword_initialization_failure_retries_greedy_once(self):
        calls = []

        def factory(**kwargs):
            calls.append(kwargs)
            if kwargs["decoding_method"] == "modified_beam_search":
                raise RuntimeError("bad context graph")
            return "greedy-recognizer"

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            vocab = root / "bpe.vocab"
            hotwords = root / "hotwords.txt"
            write_vocab(vocab)
            write_hotwords(hotwords)
            config = resolve_decoder_config(
                root,
                root,
                root,
                env={BPE_VOCAB_ENV: str(vocab), HOTWORDS_FILE_ENV: str(hotwords)},
                live_lexicon_root=root / "unused",
            )
            messages = []
            recognizer, active = create_recognizer_with_fallback(
                factory, {"tokens": "tokens.txt"}, config, logger=messages.append
            )

        self.assertEqual(recognizer, "greedy-recognizer")
        self.assertFalse(active.enabled)
        self.assertEqual([call["decoding_method"] for call in calls], ["modified_beam_search", "greedy_search"])
        self.assertIn("自动回退", messages[0])

    def test_greedy_initialization_failure_is_not_hidden(self):
        config = resolve_decoder_config(Path("missing"), Path("missing"), Path("missing"), env={})
        with self.assertRaisesRegex(RuntimeError, "model broken"):
            create_recognizer_with_fallback(
                lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("model broken")),
                {},
                config,
            )


if __name__ == "__main__":
    unittest.main()
