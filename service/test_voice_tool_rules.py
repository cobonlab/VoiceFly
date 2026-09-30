import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import voice_tool


class ResourcePathTests(unittest.TestCase):
    def test_default_paths_follow_module_and_current_home(self):
        module_root = Path(voice_tool.__file__).resolve().parent
        self.assertIsInstance(voice_tool.BASE, str)
        self.assertIsInstance(voice_tool.MODEL_DIR, str)
        self.assertIsInstance(voice_tool.DOMAIN_DICT, str)
        self.assertEqual(Path(voice_tool.BASE), module_root)
        self.assertEqual(
            Path(voice_tool.MODEL_DIR),
            module_root
            / "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09",
        )
        self.assertEqual(
            Path(voice_tool.DOMAIN_DICT),
            Path.home()
            / "Library/Mobile Documents/com~apple~CloudDocs/AppConfig/RimeSync/domain_dict.txt",
        )

    def test_source_has_no_developer_home_literal(self):
        source = Path(voice_tool.__file__).read_text(encoding="utf-8")
        self.assertNotRegex(source, r"/Users/[^/\"'\s]+")


def rule(form, canonical, **overrides):
    result = {
        "form": form,
        "canonical": canonical,
        "kind": "asr_error",
        "match_mode": "token" if form.isascii() else "phrase",
        "priority": 100,
        "risk": "medium",
        "positive_context": [],
        "negative_context": [],
        "model_scope": ["*"],
    }
    result.update(overrides)
    return result


class StructuredVoiceRuleTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.rules_path = Path(self.tempdir.name) / "voice_rules.json"
        self.env = mock.patch.dict(
            os.environ,
            {
                "VOICEFLYPY_VOICE_RULES_FILE": str(self.rules_path),
                "VOICEFLYPY_ENABLE_SPOKEN_ALIASES": "0",
            },
        )
        self.env.start()
        voice_tool._reset_rule_caches_for_tests()

    def tearDown(self):
        self.env.stop()
        voice_tool._reset_rule_caches_for_tests()
        self.tempdir.cleanup()

    def write_rules(self, semantic=(), casing=()):
        self.rules_path.write_text(
            json.dumps(
                {
                    "schema_version": "1.0.0",
                    "profile": "test",
                    "release": "test",
                    "semantic": list(semantic),
                    "casing": list(casing),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        voice_tool._reset_rule_caches_for_tests()

    def test_single_pass_does_not_cascade_generated_text(self):
        self.write_rules(
            semantic=[
                rule("alpha", "beta"),
                rule("beta", "gamma"),
            ]
        )
        self.assertEqual(voice_tool.apply_domain_dict("alpha beta"), "beta gamma")

    def test_leftmost_longest_wins_before_priority(self):
        self.write_rules(
            semantic=[
                rule("CODE", "wrong", priority=999),
                rule("CODE X", "Codex", priority=1),
            ]
        )
        self.assertEqual(voice_tool.apply_domain_dict("use CODE X now"), "use Codex now")

    def test_ascii_rules_require_alphanumeric_boundaries(self):
        self.write_rules(semantic=[rule("AI", "OpenAI")])
        self.assertEqual(
            voice_tool.apply_domain_dict("chair ai ai2 2ai (AI)"),
            "chair OpenAI ai2 2ai (OpenAI)",
        )

    def test_spoken_alias_is_disabled_in_all_domain_profile(self):
        self.write_rules(
            semantic=[rule("BUTTON", "按钮位", kind="spoken_alias")]
        )
        self.assertEqual(voice_tool.apply_domain_dict("BUTTON play"), "BUTTON play")

    def test_casing_rules_still_apply(self):
        self.write_rules(
            casing=[
                rule(
                    "OPENAI",
                    "OpenAI",
                    kind="canonical_case",
                    risk="low",
                )
            ]
        )
        self.assertEqual(voice_tool.apply_domain_dict("openai model"), "OpenAI model")

    def test_high_risk_requires_context(self):
        self.write_rules(
            semantic=[
                rule(
                    "STRIP",
                    "Stripe",
                    risk="high",
                    match_mode="context",
                    positive_context=["付款"],
                    negative_context=["脱衣"],
                ),
                rule("SMOKE", "SMOK", risk="high"),
            ]
        )
        self.assertEqual(voice_tool.apply_domain_dict("STRIP 失败"), "STRIP 失败")
        self.assertEqual(voice_tool.apply_domain_dict("付款 STRIP 失败"), "付款 Stripe 失败")
        self.assertEqual(voice_tool.apply_domain_dict("付款脱衣 STRIP"), "付款脱衣 STRIP")
        self.assertEqual(voice_tool.apply_domain_dict("SMOKE alarm"), "SMOKE alarm")

    def test_letter_by_letter_high_risk_rule_is_low_ambiguity_exception(self):
        self.write_rules(semantic=[rule("R A G", "RAG", risk="high")])
        self.assertEqual(voice_tool.apply_domain_dict("use R A G today"), "use RAG today")

    def test_medium_rule_with_positive_context_still_requires_it(self):
        self.write_rules(
            semantic=[
                rule(
                    "VAR",
                    "VaR",
                    match_mode="context",
                    positive_context=["风险"],
                )
            ]
        )
        self.assertEqual(voice_tool.apply_domain_dict("declare VAR"), "declare VAR")
        self.assertEqual(voice_tool.apply_domain_dict("风险 VAR"), "风险 VaR")

    def test_blocked_rule_never_runs(self):
        self.write_rules(
            semantic=[
                rule(
                    "danger",
                    "changed",
                    risk="blocked",
                    positive_context=["danger"],
                )
            ]
        )
        self.assertEqual(voice_tool.apply_domain_dict("danger"), "danger")

    def test_model_scope_is_enforced(self):
        self.write_rules(
            semantic=[rule("ONLY", "changed", model_scope=["some-other-model"])]
        )
        self.assertEqual(voice_tool.apply_domain_dict("ONLY"), "ONLY")


class LegacyCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.dictionary = Path(self.tempdir.name) / "domain_dict.txt"
        self.dictionary.write_text("R A G\tRAG\tai\nSHOP IFY\tShopify\tcommerce\n", encoding="utf-8")
        self.env = mock.patch.dict(
            os.environ, {"VOICEFLYPY_DOMAIN_DICT": str(self.dictionary)}
        )
        self.env.start()
        voice_tool._reset_rule_caches_for_tests()

    def tearDown(self):
        self.env.stop()
        voice_tool._reset_rule_caches_for_tests()
        self.tempdir.cleanup()

    def test_legacy_public_api_and_fallback_remain_compatible(self):
        with mock.patch.object(voice_tool, "_voice_rules_candidates", return_value=iter(())):
            self.assertEqual(
                voice_tool.load_domain_dict(),
                [("SHOP IFY", "Shopify"), ("R A G", "RAG")],
            )
            self.assertEqual(
                voice_tool.apply_domain_dict("use SHOP IFY and R A G"),
                "use Shopify and RAG",
            )

    def test_normalize_english_case_signature_is_unchanged(self):
        self.assertEqual(voice_tool.normalize_english_case("I USE CODEX"), "I use codex")

    def test_invalid_json_falls_back_to_legacy_dictionary(self):
        malformed = Path(self.tempdir.name) / "broken.json"
        malformed.write_text("{not-json", encoding="utf-8")
        with mock.patch.object(
            voice_tool, "_voice_rules_candidates", return_value=iter((malformed,))
        ):
            self.assertEqual(voice_tool.apply_domain_dict("R A G"), "RAG")


class SpokenVersionNormalizationTests(unittest.TestCase):
    def test_letter_prefixed_spoken_versions(self):
        self.assertEqual(
            voice_tool.normalize_spoken_versions(
                "a 一点零 b 三点零 c 四点零 q 五点零"
            ),
            "a1.0 b3.0 c4.0 q5.0",
        )

    def test_multicomponent_and_arabic_digit_versions(self):
        self.assertEqual(
            voice_tool.normalize_spoken_versions("v 二点三点一 A 5点6"),
            "v2.3.1 A5.6",
        )

    def test_requires_standalone_letter_and_decimal_separator(self):
        unchanged = ["一点零食", "版本一点零", "beta 三点零", "a 三"]
        for text in unchanged:
            with self.subTest(text=text):
                self.assertEqual(voice_tool.normalize_spoken_versions(text), text)

    def test_punctuation_keeps_only_numeric_dots(self):
        self.assertEqual(
            voice_tool.normalize_dictation_punctuation("发布 a1.0，随后测试。好吗？"),
            "发布 a1.0 随后测试 好吗",
        )

    def test_punctuation_keeps_structured_numeric_separators(self):
        cases = {
            "日期2026/09/05，完成。": "日期2026/09/05 完成",
            "时间3:20p.m.，开始。": "时间3:20p.m. 开始",
            "比例1/3，区间10-20。": "比例1/3 区间10-20",
            "变化-13%，不是说明-文字。": "变化-13% 不是说明 文字",
            "全角2026／09／05，3：20。": "全角2026/09/05 3:20",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(
                    voice_tool.normalize_dictation_punctuation(source),
                    expected,
                )

    def test_joined_segments_can_form_a_version(self):
        joined = " ".join(["直接叫 b", "一点零吧"])
        self.assertEqual(
            voice_tool.normalize_spoken_versions(joined),
            "直接叫 b1.0吧",
        )


class SpelledAcronymNormalizationTests(unittest.TestCase):
    def test_uppercase_spelled_tokens_are_compacted(self):
        cases = {
            "H U D": "HUD",
            "请看 H  U\tD 数据": "请看 HUD 数据",
            "G T O 和 N L H E": "GTO 和 NLHE",
            "use H U D today": "use HUD today",
            "H U D Data": "HUD Data",
            "Data H U D": "Data HUD",
            "USE H U D NOW": "USE HUD NOW",
            "HUD": "HUD",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(
                    voice_tool.normalize_spelled_acronyms(source),
                    expected,
                )

    def test_punctuation_lowercase_and_words_remain_boundaries(self):
        unchanged = (
            "A，B",
            "x y z",
            "I am here",
            "I USE CODEX",
            "H U D2",
            "A B C1",
            "H U D E2",
            "A B C D1",
            "N L H E2",
            "H U DABC",
        )
        for source in unchanged:
            with self.subTest(source=source):
                self.assertEqual(
                    voice_tool.normalize_spelled_acronyms(source),
                    source,
                )


class SpokenNumberNormalizationTests(unittest.TestCase):
    def test_letter_prefixed_integers_are_compact(self):
        self.assertEqual(
            voice_tool.normalize_spoken_numbers("比如说 k 五 t 七"),
            "比如说 k5 t7",
        )

    def test_chinese_integer_units_are_evaluated(self):
        self.assertEqual(
            voice_tool.normalize_spoken_numbers("a 二十五 b 一百零三"),
            "a25 b103",
        )

    def test_quantities_ordinals_and_standalone_numbers(self):
        self.assertEqual(
            voice_tool.normalize_spoken_numbers(
                "第十二章需要 三 次测试 持续二十分钟 版本 五"
            ),
            "第12章需要 3次测试 持续20分钟 版本 5",
        )

    def test_single_non_one_digits_and_adjacent_digit_runs_use_arabic_form(self):
        text = "有一点问题 一会儿一起处理 五花八门 二三四 万一失败"
        self.assertEqual(
            voice_tool.normalize_spoken_numbers(text),
            "有一点问题 一会儿一起处理 五花八门 234 万一失败",
        )

    def test_numeric_idioms_are_protected_by_the_exception_table(self):
        with mock.patch.object(
            voice_tool,
            "load_spoken_number_exceptions",
            return_value=("五花八门", "三心二意"),
        ):
            self.assertEqual(
                voice_tool.normalize_spoken_numbers("五花八门 三心二意 二三四"),
                "五花八门 三心二意 234",
            )

    def test_joined_segments_can_form_letter_number(self):
        joined = " ".join(["型号是 k", "五 然后 t", "七"])
        self.assertEqual(
            voice_tool.normalize_spoken_numbers(joined),
            "型号是 k5 然后 t7",
        )


class SpokenNumericSequenceTests(unittest.TestCase):
    def test_standalone_spoken_decimal(self):
        self.assertEqual(
            voice_tool.normalize_spoken_numeric_sequences("一九二点一六八"),
            "192.168",
        )

    def test_ip_address_and_mixed_digits(self):
        cases = {
            "一九二点一六八点零点二四三": "192.168.0.243",
            "地址是 一九二点一六八点零点二四三 请连接":
                "地址是 192.168.0.243 请连接",
            "192 点 一六八 点 0 点 二四三": "192.168.0.243",
        }
        for spoken, expected in cases.items():
            with self.subTest(spoken=spoken):
                self.assertEqual(
                    voice_tool.normalize_spoken_numeric_sequences(spoken), expected
                )

    def test_chinese_integer_part_in_decimal(self):
        self.assertEqual(
            voice_tool.normalize_spoken_numeric_sequences("数值是 十二点五 "),
            "数值是 12.5 ",
        )

    def test_cross_segment_dotted_sequence(self):
        joined = " ".join(["一九二点", "一六八点零", "点二四三"])
        self.assertEqual(
            voice_tool.normalize_spoken_numeric_sequences(joined),
            "192.168.0.243",
        )

    def test_ordinary_chinese_point_phrases_are_preserved(self):
        unchanged = ["有一点问题", "买一点零食", "一点也不奇怪"]
        for text in unchanged:
            with self.subTest(text=text):
                self.assertEqual(
                    voice_tool.normalize_spoken_numeric_sequences(text), text
                )


class EngineeringSymbolNormalizationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.aliases = Path(self.tempdir.name) / "engineering_symbol_aliases.tsv"
        self.aliases.write_text(
            """度符号\t°\texplicit
正负号\t±\texplicit
百分号\t%\texplicit
摄氏度\t°C\tafter_number
度\t°\tafter_number
微米\tμm\tafter_number
平方米\tm²\tafter_number
""",
            encoding="utf-8",
        )
        self.env = mock.patch.dict(
            os.environ,
            {"VOICEFLYPY_ENGINEERING_SYMBOL_ALIASES": str(self.aliases)},
        )
        self.env.start()
        voice_tool._reset_rule_caches_for_tests()

    def tearDown(self):
        self.env.stop()
        voice_tool._reset_rule_caches_for_tests()
        self.tempdir.cleanup()

    def test_explicit_symbol_commands_render_anywhere(self):
        self.assertEqual(
            voice_tool.apply_engineering_symbols("输入正负号和百分号以及度符号"),
            "输入±和%以及°",
        )

    def test_units_render_only_after_numbers(self):
        self.assertEqual(
            voice_tool.apply_engineering_symbols("温度是20摄氏度 长度10微米 面积8平方米 旋转90度"),
            "温度是20°C 长度10μm 面积8m² 旋转90°",
        )

    def test_ordinary_degree_words_are_not_corrupted(self):
        text = "温度和程度都很重要 摄氏度是一种单位 微米是一种单位"
        self.assertEqual(voice_tool.apply_engineering_symbols(text), text)

    def test_ratio_glyphs_survive_punctuation_removal(self):
        self.assertEqual(
            voice_tool.normalize_dictation_punctuation("占比50%，误差2‰，上限1‱。"),
            "占比50% 误差2‰ 上限1‱",
        )


if __name__ == "__main__":
    unittest.main()
