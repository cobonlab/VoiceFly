import json
import unittest

import semantic_refiner_protocol_v1 as PROTOCOL


CANONICAL = (
    ("C01", "calibration", "W356", ("identifier",)),
    ("C02", "calibration", "T41", ("identifier",)),
    ("C03", "calibration", "T31 T39", ("identifier",)),
    ("C04", "calibration", "我是一个紧凶型的牌手", ("homophone",)),
    (
        "C05",
        "calibration",
        "量化策略需要计算夏普比率和最大回撤",
        ("homophone",),
    ),
    ("C06", "calibration", "OpenAI 的 Codex 可以调用 Python API", ("mixed",)),
    (
        "C07",
        "calibration",
        "下铺旁边的袋子里装着一瓶食用油脂",
        ("negative",),
    ),
    ("C08", "calibration", "温度20°C 湿度50%", ("technical",)),
    ("V01", "validation", "T26", ("identifier",)),
    ("V02", "validation", "T34", ("identifier",)),
    ("V03", "validation", "W356 T41", ("identifier",)),
    (
        "V04",
        "validation",
        "按钮位采用三倍加注和持续下注",
        ("homophone",),
    ),
    ("V05", "validation", "滚动回测必须避免前视偏差", ("homophone",)),
    (
        "V06",
        "validation",
        "在 BTN 位置用 GTO solver 计算 VPIP",
        ("mixed",),
    ),
    (
        "V07",
        "validation",
        "印刷店把封面打版后发给我确认",
        ("negative",),
    ),
    ("V08", "validation", "价格¥12.5 折扣15%", ("technical",)),
    ("H01", "holdout", "T31 T39 T26 T34", ("identifier",)),
    ("H02", "holdout", "R52 K708", ("identifier",)),
    (
        "H03",
        "holdout",
        "电子烟的雾化芯需要通过 TPD 认证",
        ("mixed", "homophone"),
    ),
    ("H04", "holdout", "短线游资今天选择打板", ("homophone",)),
    ("H05", "holdout", "用 GPU 跑本地 ASR 用 CPU 做回退", ("mixed",)),
    (
        "H06",
        "holdout",
        "设备上的按钮未弹起 请先断电检查",
        ("negative",),
    ),
    ("H07", "holdout", "请输入± %和°", ("technical",)),
    ("H08", "holdout", "30% 1/3 21~1001", ("technical",)),
)


def manifest_records():
    return [
        {
            "sample_id": sample_id,
            "split": split,
            "ground_truth": truth,
            "tags": list(tags),
        }
        for sample_id, split, truth, tags in CANONICAL
    ]


def observations(*, baseline_overrides=None, candidate_overrides=None):
    baseline_overrides = baseline_overrides or {}
    candidate_overrides = candidate_overrides or {}
    return [
        {
            "sample_id": sample_id,
            "split": split,
            "ground_truth": truth,
            "tags": list(tags),
            "baseline": baseline_overrides.get(sample_id, truth),
            "candidate": candidate_overrides.get(sample_id, truth),
        }
        for sample_id, split, truth, tags in CANONICAL
    ]


class ManifestValidationTests(unittest.TestCase):
    def assert_error(self, expected_code, records):
        with self.assertRaises(PROTOCOL.ProtocolValidationError) as caught:
            PROTOCOL.validate_manifest_metadata(records)
        self.assertEqual(caught.exception.code, expected_code)
        self.assertEqual(str(caught.exception), expected_code)

    def test_exact_manifest_is_accepted_and_returned_in_canonical_order(self):
        validated = PROTOCOL.validate_manifest_metadata(reversed(manifest_records()))
        self.assertEqual(len(validated), 24)
        self.assertEqual(PROTOCOL.EXPECTED_REFERENCE_CHARACTERS, 276)
        self.assertEqual(validated[0].sample_id, "C01")
        self.assertEqual(validated[-1].sample_id, "H08")
        self.assertEqual(repr(validated[0]), "ManifestMetadata(<redacted>)")

    def test_public_sample_number_metadata_is_fixed_and_contains_no_text(self):
        self.assertEqual(
            tuple(PROTOCOL.PROTOCOL_REFERENCE_CHARACTERS_BY_SAMPLE_NUMBER.values()),
            (4, 3, 6, 10, 17, 25, 16, 11, 3, 3, 7, 14, 12, 22, 14, 12,
             12, 7, 16, 10, 17, 15, 7, 13),
        )
        self.assertEqual(
            tuple(PROTOCOL.PROTOCOL_SPLIT_BY_SAMPLE_NUMBER.values()),
            ("calibration",) * 8 + ("validation",) * 8 + ("holdout",) * 8,
        )
        self.assertEqual(
            PROTOCOL.PROTOCOL_CATEGORY_SAMPLE_NUMBERS["identifiers"],
            (1, 2, 3, 9, 10, 11, 17, 18),
        )
        with self.assertRaises(TypeError):
            PROTOCOL.PROTOCOL_REFERENCE_CHARACTERS_BY_SAMPLE_NUMBER[1] = 99

    def test_public_dataclasses_are_accepted_and_redact_their_repr(self):
        records = [
            PROTOCOL.ManifestMetadata(sample_id, split, truth, tags)
            for sample_id, split, truth, tags in CANONICAL
        ]
        self.assertEqual(len(PROTOCOL.validate_manifest_metadata(records)), 24)
        observation = PROTOCOL.ProtocolObservation(
            "C01",
            "calibration",
            "W356",
            ("identifier",),
            "private baseline",
            "private candidate",
        )
        self.assertEqual(repr(observation), "ProtocolObservation(<redacted>)")
        self.assertNotIn("private", repr(observation))

    def test_duplicate_missing_and_extra_ids_are_rejected_without_echo(self):
        duplicate = manifest_records()
        duplicate.append(dict(duplicate[0]))
        self.assert_error("duplicate_sample_id", duplicate)

        self.assert_error("protocol_sample_missing", manifest_records()[:-1])

        extra = manifest_records()
        extra[0] = {**extra[0], "sample_id": "PRIVATE-UNKNOWN-ID"}
        with self.assertRaises(PROTOCOL.ProtocolValidationError) as caught:
            PROTOCOL.validate_manifest_metadata(extra)
        self.assertEqual(caught.exception.code, "protocol_sample_extra")
        self.assertNotIn("PRIVATE-UNKNOWN-ID", str(caught.exception))

    def test_split_truth_and_tags_must_match_exact_protocol_metadata(self):
        cases = (
            ("split", "calibration", "split_mismatch"),
            ("ground_truth", "private wrong truth", "ground_truth_mismatch"),
            ("tags", ["mixed"], "tags_mismatch"),
        )
        for field, value, code in cases:
            with self.subTest(field=field):
                records = manifest_records()
                records[18] = {**records[18], field: value}
                self.assert_error(code, records)

    def test_h03_requires_both_mixed_and_homophone_tags(self):
        records = manifest_records()
        records[18]["tags"] = ["mixed"]
        self.assert_error("tags_mismatch", records)
        records[18]["tags"] = ["homophone", "mixed"]
        self.assertEqual(len(PROTOCOL.validate_manifest_metadata(records)), 24)

    def test_duplicate_tags_unknown_fields_and_non_sequences_are_rejected(self):
        records = manifest_records()
        records[18]["tags"] = ["mixed", "homophone", "mixed"]
        self.assert_error("tags_duplicate", records)

        records = manifest_records()
        records[0]["wav"] = "/private/path.wav"
        self.assert_error("manifest_record_fields_invalid", records)

        records = manifest_records()
        records[0]["tags"] = "identifier"
        self.assert_error("tags_type_invalid", records)


class AggregateReportTests(unittest.TestCase):
    def test_perfect_transcripts_cover_fixed_counts_but_manual_gates_stay_pending(self):
        report = PROTOCOL.aggregate_report(observations())

        identifiers = report["categories"]["identifiers"]
        self.assertEqual(identifiers["expected_clips"], 8)
        self.assertEqual(identifiers["expected_tokens"], 14)
        self.assertEqual(identifiers["candidate"]["clip_exact"], 8)
        self.assertEqual(identifiers["candidate"]["token_exact"], 14)

        technical = report["categories"]["technical"]
        self.assertEqual(technical["expected_clips"], 4)
        self.assertEqual(technical["expected_atoms"], 10)
        self.assertEqual(technical["expected_symbols"], 10)
        self.assertEqual(technical["candidate"]["atom_exact"], 10)
        self.assertEqual(technical["candidate"]["symbol_sequence_exact"], 4)
        self.assertEqual(technical["candidate"]["symbol_edit_distance"], 0)

        homophones = report["categories"]["positive_homophones"]
        self.assertEqual(homophones["expected_clips"], 6)
        self.assertEqual(homophones["candidate"]["clip_exact"], 6)
        self.assertEqual(report["categories"]["negative_controls"]["expected_clips"], 3)
        self.assertEqual(report["categories"]["ascii_entities"]["expected_entities"], 12)

        for split in ("validation", "holdout"):
            comparison = report["split_edit_distance"][split]
            self.assertEqual(comparison["baseline_edit_distance"], 0)
            self.assertEqual(comparison["candidate_edit_distance"], 0)
            self.assertIsNone(comparison["relative_reduction"])
            self.assertEqual(comparison["relative_reduction_status"], "inconclusive")

        self.assertEqual(report["scope"], "quality_only")
        self.assertEqual(
            report["gates"]["automated_quality"]["status"],
            "inconclusive",
        )
        self.assertEqual(
            report["gates"]["performance_resources"],
            {"status": "pending", "measurement": "not_measured"},
        )
        self.assertEqual(
            report["gates"]["natural_chinese_manual_review"],
            {
                "status": "pending",
                "measurement": "not_measured",
                "required_semantic_regressions": 0,
                "observed_semantic_regressions": None,
            },
        )
        self.assertEqual(
            report["gates"]["active_dry_run"],
            {
                "status": "pending",
                "measurement": "not_measured",
                "expected_clips": 24,
                "required_changed_commits": 0,
                "observed_changed_commits": None,
            },
        )
        self.assertEqual(report["gates"]["shadow_readiness"]["status"], "inconclusive")

    def test_report_never_contains_ids_truth_transcripts_paths_or_details(self):
        private_baseline = "/Users/private/audio/C04.wav baseline secret"
        private_candidate = "candidate transcript secret"
        report = PROTOCOL.aggregate_report(
            observations(
                baseline_overrides={"C04": private_baseline},
                candidate_overrides={"C04": private_candidate},
            )
        )
        encoded = json.dumps(report, ensure_ascii=False, sort_keys=True)
        for sample_id, _split, truth, _tags in CANONICAL:
            self.assertNotIn(sample_id, encoded)
            self.assertNotIn(truth, encoded)
        self.assertNotIn(private_baseline, encoded)
        self.assertNotIn(private_candidate, encoded)
        self.assertNotIn("/Users/", encoded)
        self.assertNotIn("details", report)

    def test_identifier_movement_extra_tokens_and_case_are_strict_failures(self):
        report = PROTOCOL.aggregate_report(
            observations(
                candidate_overrides={
                    "C01": "w356",
                    "H01": "T34 T26 T39 T31",
                    "H02": "R52 K708 T99",
                }
            )
        )
        summary = report["categories"]["identifiers"]
        self.assertEqual(summary["candidate"]["clip_exact"], 5)
        self.assertLess(summary["candidate"]["token_exact"], 14)
        self.assertEqual(summary["gate_status"], "fail")

    def test_technical_atoms_and_symbol_order_detect_insert_delete_and_move(self):
        report = PROTOCOL.aggregate_report(
            observations(
                candidate_overrides={
                    "C08": "温度20℃ 湿度50%",
                    "V08": "价格12.5 折扣15%",
                    "H07": "请输入% ±和°",
                    "H08": "30% 1/3 21~1001 %",
                }
            )
        )
        summary = report["categories"]["technical"]
        self.assertLess(summary["candidate"]["clip_exact"], 4)
        self.assertLess(summary["candidate"]["atom_sequence_exact"], 4)
        self.assertLess(summary["candidate"]["symbol_sequence_exact"], 4)
        self.assertGreater(summary["candidate"]["symbol_edit_distance"], 0)
        self.assertEqual(summary["gate_status"], "fail")

    def test_nfkc_equivalent_degree_celsius_keeps_technical_gate_exact(self):
        report = PROTOCOL.aggregate_report(
            observations(candidate_overrides={"C08": "温度20℃ 湿度50%"})
        )
        summary = report["categories"]["technical"]
        self.assertEqual(summary["candidate"]["clip_exact"], 4)
        self.assertEqual(summary["candidate"]["atom_sequence_exact"], 4)
        self.assertEqual(summary["candidate"]["symbol_sequence_exact"], 4)
        self.assertEqual(summary["gate_status"], "pass")

    def test_ascii_entities_require_exact_case_sequence_and_no_extras(self):
        report = PROTOCOL.aggregate_report(
            observations(
                candidate_overrides={
                    "C06": "OpenAi 的 Codex 可以调用 Python API",
                    "H05": "用 GPU 跑本地 ASR 用 CPU TPU 做回退",
                }
            )
        )
        summary = report["categories"]["ascii_entities"]
        self.assertEqual(summary["candidate"]["sequence_exact"], 2)
        self.assertLess(summary["candidate"]["entity_exact"], 12)
        self.assertEqual(summary["gate_status"], "fail")

    def test_h03_counts_as_positive_homophone_and_mixed_ascii(self):
        one_error = PROTOCOL.aggregate_report(
            observations(candidate_overrides={"H03": "完全错误"})
        )
        homophones = one_error["categories"]["positive_homophones"]
        self.assertEqual(homophones["candidate"]["clip_exact"], 5)
        self.assertEqual(homophones["gate_status"], "pass")
        self.assertEqual(
            one_error["categories"]["ascii_entities"]["candidate"]["sequence_exact"],
            3,
        )

        two_errors = PROTOCOL.aggregate_report(
            observations(
                candidate_overrides={"H03": "完全错误", "C04": "另一个错误"}
            )
        )
        self.assertEqual(
            two_errors["categories"]["positive_homophones"]["gate_status"],
            "fail",
        )

    def test_negative_control_requires_three_of_three_exact(self):
        report = PROTOCOL.aggregate_report(
            observations(candidate_overrides={"V07": "印刷店把封面打板后发给我确认"})
        )
        summary = report["categories"]["negative_controls"]
        self.assertEqual(summary["candidate"]["clip_exact"], 2)
        self.assertEqual(summary["gate_status"], "fail")

    def test_provider_failure_is_aggregate_only_and_blocks_split_metrics(self):
        report = PROTOCOL.aggregate_report(
            observations(candidate_overrides={"H01": None})
        )
        completion = report["provider_completion"]
        self.assertEqual(completion["candidate_successful"], 23)
        self.assertEqual(completion["candidate_failed"], 1)
        self.assertEqual(completion["gate_status"], "fail")
        holdout = report["split_edit_distance"]["holdout"]
        self.assertIsNone(holdout["baseline_edit_distance"])
        self.assertEqual(holdout["gate_status"], "not_measured")
        self.assertEqual(report["gates"]["shadow_readiness"]["status"], "fail")
        self.assertNotIn("H01", json.dumps(report))

    def test_twenty_percent_reduction_passes_at_distance_ten(self):
        baseline = {
            "V04": "错" * 10 + CANONICAL[11][2],
            "H04": "错" * 10 + CANONICAL[19][2],
        }
        candidate = {
            "V04": "错" * 8 + CANONICAL[11][2],
            "H04": "错" * 8 + CANONICAL[19][2],
        }
        report = PROTOCOL.aggregate_report(
            observations(baseline_overrides=baseline, candidate_overrides=candidate)
        )
        for split in ("validation", "holdout"):
            comparison = report["split_edit_distance"][split]
            self.assertEqual(comparison["baseline_edit_distance"], 10)
            self.assertEqual(comparison["candidate_edit_distance"], 8)
            self.assertEqual(comparison["relative_reduction"], 0.2)
            self.assertEqual(comparison["non_regression_status"], "pass")
            self.assertEqual(comparison["relative_reduction_status"], "pass")
            self.assertEqual(comparison["gate_status"], "pass")

    def test_all_automated_gates_can_pass_but_shadow_remains_pending(self):
        baseline = {
            "V04": "错" * 10 + CANONICAL[11][2],
            "H04": "错" * 10 + CANONICAL[19][2],
        }
        report = PROTOCOL.aggregate_report(
            observations(baseline_overrides=baseline)
        )
        self.assertEqual(report["gates"]["automated_quality"]["status"], "pass")
        self.assertEqual(report["gates"]["shadow_readiness"]["status"], "pending")
        self.assertEqual(
            report["gates"]["natural_chinese_manual_review"]["measurement"],
            "not_measured",
        )
        self.assertEqual(
            report["gates"]["active_dry_run"]["measurement"],
            "not_measured",
        )

    def test_below_ten_is_inconclusive_and_zero_denominator_can_still_regress(self):
        below_ten = PROTOCOL.aggregate_report(
            observations(
                baseline_overrides={"V04": "错" * 9 + CANONICAL[11][2]},
                candidate_overrides={"V04": CANONICAL[11][2]},
            )
        )["split_edit_distance"]["validation"]
        self.assertEqual(below_ten["baseline_edit_distance"], 9)
        self.assertEqual(below_ten["relative_reduction_status"], "inconclusive")
        self.assertEqual(below_ten["gate_status"], "inconclusive")

        zero = PROTOCOL.aggregate_report(
            observations(candidate_overrides={"V04": "错" + CANONICAL[11][2]})
        )["split_edit_distance"]["validation"]
        self.assertEqual(zero["baseline_edit_distance"], 0)
        self.assertIsNone(zero["relative_reduction"])
        self.assertEqual(zero["relative_reduction_status"], "inconclusive")
        self.assertEqual(zero["non_regression_status"], "fail")
        self.assertEqual(zero["gate_status"], "fail")

    def test_less_than_twenty_percent_reduction_fails_when_baseline_is_sufficient(self):
        comparison = PROTOCOL.aggregate_report(
            observations(
                baseline_overrides={"H04": "错" * 10 + CANONICAL[19][2]},
                candidate_overrides={"H04": "错" * 9 + CANONICAL[19][2]},
            )
        )["split_edit_distance"]["holdout"]
        self.assertEqual(comparison["relative_reduction"], 0.1)
        self.assertEqual(comparison["non_regression_status"], "pass")
        self.assertEqual(comparison["relative_reduction_status"], "fail")
        self.assertEqual(comparison["gate_status"], "fail")

    def test_empty_string_is_valid_transcript_but_counts_as_quality_failure(self):
        report = PROTOCOL.aggregate_report(
            observations(candidate_overrides={"H01": ""})
        )
        self.assertEqual(report["provider_completion"]["candidate_successful"], 24)
        self.assertEqual(report["provider_completion"]["candidate_failed"], 0)
        self.assertEqual(
            report["categories"]["identifiers"]["gate_status"],
            "fail",
        )
        self.assertIsNotNone(
            report["split_edit_distance"]["holdout"]["candidate_edit_distance"]
        )
        self.assertEqual(
            report["split_edit_distance"]["holdout"]["gate_status"],
            "fail",
        )
        self.assertIsNone(PROTOCOL.validate_public_report(report))

    def test_invalid_provider_text_is_rejected_without_echo(self):
        invalid_values = (
            ("nul", "secret\x00text"),
            ("oversized", "x" * (PROTOCOL.MAX_TRANSCRIPT_CHARACTERS + 1)),
            ("non_string", 7),
        )
        for value_kind, private_value in invalid_values:
            with self.subTest(value_kind=value_kind):
                records = observations(candidate_overrides={"C01": private_value})
                with self.assertRaises(PROTOCOL.ProtocolValidationError) as caught:
                    PROTOCOL.aggregate_report(records)
                self.assertEqual(
                    caught.exception.code,
                    "candidate_transcript_invalid",
                )
                self.assertNotIn("secret", str(caught.exception))


class PublicReportValidationTests(unittest.TestCase):
    def quality_pass_report(self):
        return PROTOCOL.aggregate_report(
            observations(
                baseline_overrides={
                    "V04": "错" * 10 + CANONICAL[11][2],
                    "H04": "错" * 10 + CANONICAL[19][2],
                }
            )
        )

    def assert_public_error(self, expected_code, report):
        with self.assertRaises(PROTOCOL.ProtocolValidationError) as caught:
            PROTOCOL.validate_public_report(report)
        self.assertEqual(caught.exception.code, expected_code)
        self.assertEqual(str(caught.exception), expected_code)

    def test_generated_success_failure_and_inconclusive_reports_validate(self):
        reports = (
            PROTOCOL.aggregate_report(observations()),
            self.quality_pass_report(),
            PROTOCOL.aggregate_report(
                observations(candidate_overrides={"H01": None})
            ),
        )
        for report in reports:
            with self.subTest(status=report["gates"]["shadow_readiness"]["status"]):
                self.assertIsNone(PROTOCOL.validate_public_report(report))

    def test_unknown_fields_are_rejected_at_every_schema_level(self):
        mutations = []
        report = self.quality_pass_report()
        report["private_transcript"] = "must-not-echo"
        mutations.append(report)

        report = self.quality_pass_report()
        report["categories"]["identifiers"]["candidate"]["extra"] = 1
        mutations.append(report)

        report = self.quality_pass_report()
        report["gates"]["performance_resources"]["path"] = "/private/audio"
        mutations.append(report)

        for report in mutations:
            with self.subTest():
                with self.assertRaises(PROTOCOL.ProtocolValidationError) as caught:
                    PROTOCOL.validate_public_report(report)
                self.assertEqual(caught.exception.code, "public_report_fields_invalid")
                self.assertNotIn("must-not-echo", str(caught.exception))
                self.assertNotIn("/private/audio", str(caught.exception))

    def test_malicious_strings_nan_and_non_json_numbers_are_rejected(self):
        report = self.quality_pass_report()
        report["provider_completion"]["baseline_successful"] = "<script>secret"
        self.assert_public_error("public_report_count_invalid", report)

        report = self.quality_pass_report()
        report["split_edit_distance"]["validation"]["relative_reduction"] = float("nan")
        self.assert_public_error("public_report_number_invalid", report)

        report = self.quality_pass_report()
        report["gates"]["automated_quality"]["status"] = "private malicious status"
        with self.assertRaises(PROTOCOL.ProtocolValidationError) as caught:
            PROTOCOL.validate_public_report(report)
        self.assertEqual(caught.exception.code, "public_report_status_invalid")
        self.assertNotIn("private malicious status", str(caught.exception))

    def test_count_bounds_protocol_hash_scope_and_status_consistency_are_strict(self):
        report = self.quality_pass_report()
        report["categories"]["identifiers"]["candidate"]["clip_exact"] = 9
        self.assert_public_error("public_report_count_invalid", report)

        report = self.quality_pass_report()
        report["protocol"]["schema_sha256"] = "0" * 64
        self.assert_public_error("public_report_protocol_invalid", report)

        report = self.quality_pass_report()
        report["scope"] = "all_automated_gates"
        self.assert_public_error("public_report_scope_invalid", report)

        report = self.quality_pass_report()
        report["categories"]["identifiers"]["gate_status"] = "fail"
        self.assert_public_error("public_report_consistency_invalid", report)

        report = self.quality_pass_report()
        report["gates"]["shadow_readiness"]["status"] = "pass"
        self.assert_public_error("public_report_status_invalid", report)


if __name__ == "__main__":
    unittest.main()
