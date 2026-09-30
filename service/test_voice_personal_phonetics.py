import os
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import voice_personal_phonetics as PHONETICS


PINYIN = {
    "我": "wo",
    "要": "yao",
    "去": "qu",
    "紧": "jin",
    "凶": "xiong",
    "胸": "xiong",
    "井": "jing",
    "锦": "jin",
    "颈": "jing",
    "绣": "xiu",
    "型": "xing",
    "牌": "pai",
    "手": "shou",
    "进": "jin",
    "修": "xiu",
    "课": "ke",
    "程": "cheng",
    "河": "he",
    "山": "shan",
    "前": "qian",
    "后": "hou",
    "鼻": "bi",
    "音": "yin",
    "识": "shi",
    "别": "bie",
    "仍": "reng",
    "以": "yi",
    "继": "ji",
    "续": "xu",
}


def romanize(value):
    try:
        return tuple(PINYIN[char] for char in value)
    except KeyError:
        return None


def all_singles(value):
    return PHONETICS.HanSegmentation(
        tuple((index, index + 1) for index in range(len(value)))
    )


def segmented(*tokens):
    ranges = []
    cursor = 0
    for token in tokens:
        ranges.append((cursor, cursor + len(token)))
        cursor += len(token)

    def segmenter(value):
        if value != "".join(tokens):
            return None
        return PHONETICS.HanSegmentation(tuple(ranges))

    return segmenter


class PersonalPhoneticEncodingTests(unittest.TestCase):
    def test_active_xiaohe_algebra_encodes_relevant_syllables(self):
        self.assertIn("jb", PHONETICS._flypy_codes_for_pinyin("jin"))
        self.assertIn("jk", PHONETICS._flypy_codes_for_pinyin("jing"))
        self.assertIn("xs", PHONETICS._flypy_codes_for_pinyin("xiong"))
        self.assertIn("xq", PHONETICS._flypy_codes_for_pinyin("xiu"))
        self.assertIn("vi", PHONETICS._flypy_codes_for_pinyin("zhi"))

    def test_standard_nasal_pairs_collapse_but_weak_ng_is_separate(self):
        self.assertEqual(
            PHONETICS._fold_flypy_pair("jb"),
            PHONETICS._fold_flypy_pair("jk"),
        )
        self.assertNotEqual(
            PHONETICS._fold_flypy_pair("xs"),
            PHONETICS._fold_flypy_pair("xq"),
        )
        self.assertEqual(
            PHONETICS._fold_flypy_pair("xs", relaxed=True),
            PHONETICS._fold_flypy_pair("xq", relaxed=True),
        )


class PersonalPhraseMatcherTests(unittest.TestCase):
    def matcher(self):
        return PHONETICS.build_personal_phrase_matcher(["紧凶\tjbxs\t200"])

    def test_homophones_and_front_back_nasals_resolve_to_saved_spelling(self):
        result = PHONETICS.apply_personal_phonetic_corrections(
            "锦胸 颈胸 井胸 紧胸",
            matcher=self.matcher(),
            romanizer=romanize,
            segmenter=all_singles,
        )
        self.assertEqual(result, "紧凶 紧凶 紧凶 紧凶")

    def test_real_segmented_word_is_preserved_without_authorizing_context(self):
        result = PHONETICS.apply_personal_phonetic_corrections(
            "我要去进修课程",
            matcher=self.matcher(),
            romanizer=romanize,
            segmenter=segmented("我", "要", "去", "进修", "课程"),
        )
        self.assertEqual(result, "我要去进修课程")

    def test_weak_ng_variant_requires_context_and_then_repairs(self):
        matcher = self.matcher()
        ordinary = PHONETICS.apply_personal_phonetic_corrections(
            "锦绣河山",
            matcher=matcher,
            romanizer=romanize,
            segmenter=segmented("锦绣", "河山"),
        )
        poker = PHONETICS.apply_personal_phonetic_corrections(
            "锦绣型牌手",
            matcher=matcher,
            romanizer=romanize,
            segmenter=segmented("锦绣", "型", "牌手"),
        )
        self.assertEqual(ordinary, "锦绣河山")
        self.assertEqual(poker, "紧凶型牌手")

    def test_ambiguous_saved_signature_is_disabled(self):
        matcher = PHONETICS.build_personal_phrase_matcher(
            ["甲词\tjaci\t200", "假词\tjaci\t200"]
        )
        self.assertEqual(matcher.term_count, 0)
        self.assertGreaterEqual(matcher.collision_count, 1)

    def test_abbreviated_legacy_code_is_skipped_without_losing_other_terms(self):
        matcher = PHONETICS.build_personal_phrase_matcher(
            ["旧短语\tjqy\t200", "紧凶\tjbxs\t200"]
        )
        self.assertEqual(matcher.term_count, 1)

    def test_missing_platform_segmentation_fails_closed(self):
        result = PHONETICS.apply_personal_phonetic_corrections(
            "锦胸",
            matcher=self.matcher(),
            romanizer=romanize,
            segmenter=lambda _value: None,
        )
        self.assertEqual(result, "锦胸")

    def test_match_cannot_be_cut_across_two_normal_words(self):
        matcher = PHONETICS.build_personal_phrase_matcher(["隐式\tybui\t200"])
        result = PHONETICS.apply_personal_phonetic_corrections(
            "前后鼻音识别",
            matcher=matcher,
            romanizer=romanize,
            segmenter=segmented("前后", "鼻音", "识别"),
        )
        self.assertEqual(result, "前后鼻音识别")

    def test_unrelated_single_character_sequence_needs_context_when_embedded(self):
        matcher = PHONETICS.build_personal_phrase_matcher(["任艺\trfyi\t200"])
        result = PHONETICS.apply_personal_phonetic_corrections(
            "仍以继续",
            matcher=matcher,
            romanizer=romanize,
            segmenter=all_singles,
        )
        self.assertEqual(result, "仍以继续")

    def test_foundation_integration_handles_observed_outputs(self):
        try:
            import Foundation  # noqa: F401
        except ImportError:
            self.skipTest("Foundation is available only in the macOS service environment")
        matcher = self.matcher()
        observed = {
            "我是一个紧胸型的选手": "我是一个紧凶型的选手",
            "井胸这两个字": "紧凶这两个字",
            "锦胸 颈胸": "紧凶 紧凶",
            "我是一个锦绣型的牌手": "我是一个紧凶型的牌手",
            "锦绣河山": "锦绣河山",
            "我要去进修课程": "我要去进修课程",
        }
        for source, expected in observed.items():
            with self.subTest(source=source):
                self.assertEqual(
                    PHONETICS.apply_personal_phonetic_corrections(
                        source,
                        matcher=matcher,
                    ),
                    expected,
                )


class PersonalPhraseLoaderTests(unittest.TestCase):
    def setUp(self):
        PHONETICS._reset_personal_phonetic_cache_for_tests()

    def tearDown(self):
        PHONETICS._reset_personal_phonetic_cache_for_tests()

    def test_loader_reads_explicit_file_and_retains_last_good_on_corruption(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "personal_usage.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0.0",
                        "terms": {
                            "紧凶": {
                                "flypy_code": "jbxs",
                                "selection_count": 0,
                                "explicit_count": 1,
                                "updated_at": "2026-09-08T00:00:00Z",
                            },
                            "普通候选": {
                                "flypy_code": "putshx",
                                "selection_count": 20,
                                "explicit_count": 0,
                                "updated_at": "2026-09-08T00:00:00Z",
                            },
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            with mock.patch.dict(
                os.environ,
                {PHONETICS.PERSONAL_USAGE_ENV: str(path)},
                clear=False,
            ):
                good = PHONETICS.load_personal_phrase_matcher()
                self.assertEqual(good.term_count, 1)
                path.write_text("broken\n", encoding="utf-8")
                retained = PHONETICS.load_personal_phrase_matcher()
                self.assertIs(retained, good)

    def test_missing_optional_file_loads_empty_matcher(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "missing.txt"
            with mock.patch.dict(
                os.environ,
                {PHONETICS.PERSONAL_USAGE_ENV: str(path)},
                clear=False,
            ):
                self.assertEqual(
                    PHONETICS.load_personal_phrase_matcher().term_count,
                    0,
                )


if __name__ == "__main__":
    unittest.main()
