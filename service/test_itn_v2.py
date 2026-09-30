import tempfile
import types
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from itn.chinese.inverse_normalizer import InverseNormalizer

import voice_flypy_service as service


class ConservativeItnIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cache_dir = tempfile.TemporaryDirectory(prefix="voiceflypy-itn-v2-test-")
        cls.itn = InverseNormalizer(
            cache_dir=cls.cache_dir.name,
            enable_0_to_9=False,
        )

    @classmethod
    def tearDownClass(cls):
        cls.cache_dir.cleanup()

    def setUp(self):
        self.itn_patch = mock.patch.object(service, "_itn", self.itn)
        self.itn_patch.start()

    def tearDown(self):
        self.itn_patch.stop()

    def test_user_reported_natural_language_is_preserved_exactly(self):
        cases = (
            "下面这一段复制给你",
            "贵一点也无所谓",
            "这个是正儿八经吃肚子里面有效果的",
            (
                "你说吃的蜂胶吧 贵一点也无所谓 因为这个是正儿八经吃肚子"
                "里面有效果的 牙膏这东西 漱漱口就吐出来了 你说就是有效果"
                "可能也比较有限"
            ),
        )
        for source in cases:
            with self.subTest(source=source):
                self.assertEqual(service.normalize(source), source)

    def test_lexical_number_exceptions_prefer_original_hanzi(self):
        cases = (
            "一会儿一起处理",
            "万一失败",
            "第一感觉",
            "他一度认为这个办法可行",
            "一年一度的大会",
            "我有一点意见",
            "买一点零食",
            "给他一点零花钱",
            "这一点十分重要",
            "有一点十分奇怪",
            "差一点半小时",
            "把它一分为二",
            "我十分满意",
            "我有十足把握",
            "你千万不要迟到",
            "九一八事变",
            "一二九运动",
            "三三两两地走过来",
            "事情做得七七八八",
            "双方大概五五开",
            "一二三木头人",
            "两点意见",
            "下午两点开会",
            "十元二次方程",
        )
        for source in cases:
            with self.subTest(source=source):
                self.assertEqual(service.normalize(source), source)

    def test_ordinary_quantities_use_arabic_digits(self):
        cases = {
            "有三个问题": "有3个问题",
            "测试三次": "测试3次",
            "住了三年": "住了3年",
            "这里有二十七个候选": "这里有27个候选",
            "我们中间加了一层": "我们中间加了1层",
            "一年后再说": "1年后再说",
            "一分钟后": "1分钟后",
            "距离一米": "距离1米",
            "价格一元": "价格1元",
            "一九八四是一本书": "一九八四是1本书",
            "有三 个问题": "有3个问题",
            "测试三 次": "测试3次",
            "这里有二 十 七个候选": "这里有27个候选",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(service.normalize(source), expected)

    def test_all_editable_numeric_idioms_are_preserved(self):
        exception_path = (
            Path(service.__file__).resolve().parent
            / "lexicon"
            / "spoken_number_exceptions.txt"
        )
        phrases = tuple(
            line.strip()
            for line in exception_path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
        self.assertGreaterEqual(len(phrases), 48)
        for phrase in phrases:
            with self.subTest(phrase=phrase):
                self.assertEqual(service.normalize(phrase), phrase)

    def test_high_confidence_structured_numbers_are_rendered(self):
        cases = {
            "二零二六年九月五日": "2026/09/05",
            "下午三点二十分": "3:20p.m.",
            "会议三点二十分开始": "会议3:20开始",
            "时间是八点零五分": "时间是8:05",
            "百分之三十": "30%",
            "十二元五角": "¥12.5",
            "三分之一的人同意": "1/3的人同意",
            "温度二十摄氏度": "温度20°C",
            "延迟三百毫秒": "延迟300ms",
            "十分钟后": "10min后",
            "距离十米": "距离10m",
            "价格十元": "价格¥10",
            "模型有七十亿参数": "模型有70亿参数",
            "这里有一百个理由": "这里有100个理由",
            "一百块钱": "100块钱",
            "十二块五毛": "12块5毛",
            "二十一到一千零一": "21~1001",
            "鄂a七l六二u": "鄂a7l62u",
            "高一二三": "G123",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(service.normalize(source), expected)

    def test_decimal_versions_addresses_and_ids_are_deterministic(self):
        cases = {
            "三点一四一五": "3.1415",
            "一九二点一六八点零点一": "192.168.0.1",
            "版本二点三点一": "版本2.3.1",
            "v 二点三点一": "v2.3.1",
            "端口八零八零": "端口8080",
            "房间号三零八": "房间号308",
            "编号二零二六零九零五": "编号20260905",
            "验证码是一二三四五六": "验证码是123456",
            "一三八零零一三八零零零": "13800138000",
            "幺三八零零幺三八零零零": "13800138000",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(service.normalize(source), expected)

    def test_single_latin_letter_authorizes_adjacent_integer_rendering(self):
        cases = {
            "W 二十七": "W27",
            "W二十七": "W27",
            "W 二十七 T 三十五": "W27 T35",
            "W二十七T三十五": "W27T35",
            "型号 W 二十七": "型号 W27",
            "a 一百二十三": "a123",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(service.normalize(source), expected)

    def test_single_letter_identifier_digit_runs_are_compact_and_consistent(self):
        cases = {
            "W 三五六 T 三九": "W356 T39",
            "W 356 T 三九": "W356 T39",
            "W 三5六 T 3九": "W356 T39",
            "W356 T39": "W356 T39",
            "A 幺零": "A10",
            "B 3.14": "B3.14",
            "C 三九。": "C39",
            "V 二零以及 T 三十": "V20以及 T30",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(service.normalize(source), expected)

    def test_identifier_numbers_close_on_real_following_context(self):
        cases = {
            "T 幺零机柜": "T10机柜",
            "V 幺零机柜": "V10机柜",
            "V 幺零加上了 T300": "V10加上了 T300",
            "G 300机柜": "G300机柜",
            "GB 300机柜": "GB300机柜",
            "GB 三百机柜": "GB300机柜",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(service.normalize(source), expected)

    def test_streaming_identifier_tail_is_open_until_context_or_final(self):
        cases = {
            "T 幺": ("T 幺", 0),
            "T 幺零": ("T 幺零", 0),
            "T 幺零机": ("T10机", None),
            "T 幺零机柜": ("T10机柜", None),
            "前文 V 幺零": ("前文 V 幺零", 3),
            "前文 V 幺零加": ("前文 V10加", None),
        }
        for source, (expected, stable_limit) in cases.items():
            with self.subTest(source=source):
                partial = service.normalize_partial(source)
                self.assertEqual(partial.text, expected)
                self.assertEqual(partial.stable_limit_codepoints, stable_limit)

        self.assertEqual(service.normalize("T 幺零"), "T10")

    def test_multi_letter_identifier_requires_structural_evidence(self):
        cases = {
            "GB 300": "GB300",
            "GB 幺零": "GB10",
            "GB 三百": "GB 三百",
            "AW 二十七": "AW 二十七",
            "SHOW 三九": "SHOW 三九",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(service.normalize(source), expected)

    def test_multi_letter_words_and_punctuation_do_not_authorize_cardinals(self):
        cases = {
            "SHOW 二十七": "SHOW 二十七",
            "AW 二十七": "AW 二十七",
            "A，二十七": "A 二十七",
            "SHOW 三九": "SHOW 三九",
            "AW 三九": "AW 三九",
            "A，三九": "A 三九",
            "A 三九个人": "A 39个人",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(service.normalize(source), expected)

    def test_letter_integer_context_survives_acoustic_segment_joining(self):
        joined = service.join_recognition_segments(("W", "二十七", "T", "三十五"))
        self.assertEqual(joined, "W 二十七 T 三十五")
        self.assertEqual(service.normalize(joined), "W27 T35")

    def test_number_contract_is_identical_across_acoustic_segment_boundaries(self):
        joined = service.join_recognition_segments(("V", "二零以及", "T", "三十"))
        self.assertEqual(service.normalize(joined), "V20以及 T30")

    def test_literal_dong_remains_natural_language(self):
        self.assertEqual(service.normalize("山洞里有一点水"), "山洞里有一点水")

    def test_packaged_self_test_covers_policy_and_reports_version(self):
        output = StringIO()
        with redirect_stdout(output):
            status = service.run_itn_self_test()
        self.assertEqual(status, 0)
        self.assertIn("policy=4.0.0 passed=24/24", output.getvalue())

    def test_time_of_day_before_a_split_time_mapping_is_accepted(self):
        self.assertEqual(service.normalize("凌晨一点十二分"), "凌晨1:12")

    def test_hesitation_removal_remains_compatible(self):
        self.assertEqual(service.normalize("呃这个呃啊我不知道"), "这个我不知道")

    def test_spelled_uppercase_acronyms_are_compact_in_production_chain(self):
        cases = {
            "H U D": "HUD",
            "请看 H U D 数据": "请看 HUD 数据",
            "G T O 和 N L H E": "GTO 和 NLHE",
            "use H U D today": "use HUD today",
            "H U D Data": "HUD Data",
            "Data H U D": "Data HUD",
            "USE H U D NOW": "USE HUD NOW",
            "A，B": "A B",
            "x y z": "x y z",
            "I USE CODEX": "I USE CODEX",
            "H U D2": "H U D2",
            "A B C1": "A B C1",
            "N L H E2": "N L H E2",
            "H U DABC": "H U DABC",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(service.normalize(source), expected)


class ConservativeItnFailureTests(unittest.TestCase):
    def test_itn_exception_falls_back_without_losing_dictation(self):
        broken = mock.Mock()
        broken.normalize_with_mapping.side_effect = RuntimeError("raw text must not log")
        with (
            mock.patch.object(service, "_itn", broken),
            mock.patch("builtins.print") as printer,
        ):
            self.assertEqual(service.normalize("正儿八经，有一点问题。"), "正儿八经 有一点问题")
        printer.assert_called_once_with(
            "[ITN] 保守回退 chars=11 error=RuntimeError",
            flush=True,
        )

    def test_injected_diagnostic_logger_replaces_print_without_text(self):
        broken = mock.Mock()
        broken.normalize_with_mapping.side_effect = RuntimeError("private text")
        logger = mock.Mock()
        with (
            mock.patch.object(service, "_itn", broken),
            mock.patch("builtins.print") as printer,
        ):
            result = service.normalize(
                "不应进入日志的原文",
                diagnostic_logger=logger,
            )

        self.assertEqual(result, "不应进入日志的原文")
        printer.assert_not_called()
        logger.assert_called_once_with("[ITN] 保守回退 chars=9 error=RuntimeError")
        self.assertNotIn("不应进入日志的原文", logger.call_args.args[0])

    def test_invalid_alignment_falls_back_to_complete_original(self):
        bad_mapping = types.SimpleNamespace(
            input_start=1,
            input_end=3,
            input_text="错误",
            output_text="1",
            token_type="cardinal",
        )
        broken = mock.Mock()
        broken.normalize_with_mapping.return_value = types.SimpleNamespace(
            output_text="任意",
            mappings=(bad_mapping,),
        )
        with mock.patch.object(service, "_itn", broken):
            self.assertEqual(service.normalize("保留原文"), "保留原文")

    def test_lazy_constructor_uses_conservative_wetext_profile(self):
        fake = mock.Mock()
        fake.normalize_with_mapping.return_value = types.SimpleNamespace(
            output_text="测试十五",
            mappings=(),
        )
        with (
            mock.patch.object(service, "_itn", None),
            mock.patch.object(service, "InverseNormalizer", return_value=fake) as constructor,
        ):
            self.assertEqual(service.normalize("测试十五"), "测试十五")
        constructor.assert_called_once_with(
            cache_dir=str(service.ITN_CACHE_DIR),
            enable_0_to_9=False,
        )

    def test_memory_only_constructor_uses_false_and_leaves_cache_tree_unchanged(self):
        fake = mock.Mock()
        fake.normalize_with_mapping.return_value = types.SimpleNamespace(
            output_text="测试十五",
            mappings=(),
        )

        def snapshot_tree(root):
            paths = [root, *sorted(root.rglob("*"))]
            return {
                str(path.relative_to(root)) or ".": (
                    path.stat().st_mode,
                    path.stat().st_size,
                    path.stat().st_mtime_ns,
                    path.read_bytes() if path.is_file() else None,
                )
                for path in paths
            }

        with tempfile.TemporaryDirectory(prefix="voiceflypy-no-itn-cache-") as directory:
            monitored_cache = Path(directory) / "must-stay-readonly"
            monitored_cache.mkdir()
            (monitored_cache / "existing.marker").write_bytes(b"unchanged")
            monitored_cache.chmod(0o500)
            before = snapshot_tree(monitored_cache)
            try:
                with (
                    mock.patch.object(service, "ITN_CACHE_DIR", monitored_cache),
                    mock.patch.object(service, "_itn", None),
                    mock.patch.object(service, "_memory_only_itn", None),
                    mock.patch.object(
                        service,
                        "InverseNormalizer",
                        return_value=fake,
                    ) as constructor,
                ):
                    self.assertEqual(
                        service.normalize(
                            "测试十五",
                            memory_only_itn=True,
                        ),
                        "测试十五",
                    )
                    self.assertIsNone(service._itn)
                    after = snapshot_tree(monitored_cache)
            finally:
                monitored_cache.chmod(0o700)

        constructor.assert_called_once_with(
            cache_dir=False,
            enable_0_to_9=False,
        )
        self.assertEqual(after, before)

    def test_empty_input_does_not_construct_itn(self):
        with (
            mock.patch.object(service, "_itn", None),
            mock.patch.object(service, "InverseNormalizer") as constructor,
        ):
            self.assertEqual(service.normalize("  \t\n"), "")
        constructor.assert_not_called()


if __name__ == "__main__":
    unittest.main()
