import io
import json
import signal
import time
import unittest
from dataclasses import replace
from unittest import mock

import semantic_refiner_supervisor as SUPERVISOR
import semantic_refiner_protocol_v1 as PROTOCOL


REFERENCE_CHARACTERS = PROTOCOL.EXPECTED_REFERENCE_CHARACTERS


def quality_protocol_report(*, baseline_overrides=None, candidate_overrides=None):
    baseline_overrides = baseline_overrides or {}
    candidate_overrides = candidate_overrides or {}
    return PROTOCOL.aggregate_report(
        PROTOCOL.ProtocolObservation(
            sample_id=spec.sample_id,
            split=spec.split,
            ground_truth=spec.ground_truth,
            tags=spec.tags,
            baseline=baseline_overrides.get(spec.sample_id, spec.ground_truth),
            candidate=candidate_overrides.get(spec.sample_id, spec.ground_truth),
        )
        for spec in PROTOCOL._SPECS
    )


def provider_report(name):
    return {
        "name": name,
        "cer": 0.0,
        "edit_distance": 0,
        "reference_characters": REFERENCE_CHARACTERS,
        "identifier_exact_accuracy": 1.0,
        "identifier_correct": 8,
        "identifier_total": 8,
        "successful_count": 24,
        "failed_count": 0,
        "latency_ms": {"p50": 1.0, "p95": 1.0},
        "identity_sha256": "a" * 64,
    }


def valid_report():
    return {
        "schema_version": 1,
        "evaluation": "voiceflypy_semantic_refiner_ab",
        "sample_count": 24,
        "privacy": {
            "audio_persisted": False,
            "transcripts_persisted": False,
            "paths_emitted": False,
            "free_text_included": False,
            "sample_metadata_included": False,
            "raw_provider_metadata_included": False,
            "transcripts_included": False,
            "output": "stdout_only",
        },
        "complete": True,
        "provider_failure_count": 0,
        "normalization": {
            "cer": "Unicode NFKC code points with whitespace removed",
            "identifier_exact": "Unicode NFKC with outer/collapsed whitespace",
        },
        "baseline": provider_report("voiceflypy_x_asr"),
        "second_model": provider_report("qwen3_asr"),
        "comparison": {
            "agreement_count": 24,
            "disagreement_count": 0,
            "unavailable_count": 0,
            "second_better_count": 0,
            "baseline_better_count": 0,
            "equal_error_count": 24,
        },
        "error_count": 0,
        "errors": [],
        "quality_protocol": quality_protocol_report(),
    }


def consistent_validation_error_report():
    report = valid_report()
    observations = []
    for spec in PROTOCOL._SPECS:
        replacement = (
            "错" + spec.ground_truth[1:]
            if spec.sample_id == "V04"
            else spec.ground_truth
        )
        observations.append(
            PROTOCOL.ProtocolObservation(
                sample_id=spec.sample_id,
                split=spec.split,
                ground_truth=spec.ground_truth,
                tags=spec.tags,
                baseline=replacement,
                candidate=replacement,
            )
        )
    report["quality_protocol"] = PROTOCOL.aggregate_report(observations)
    for role in ("baseline", "second_model"):
        report[role].update(
            cer=round(1 / REFERENCE_CHARACTERS, 6),
            edit_distance=1,
        )
    reference_characters = PROTOCOL.PROTOCOL_REFERENCE_CHARACTERS_BY_SAMPLE_NUMBER[12]
    report["error_count"] = 1
    report["errors"] = [
        {
            "sample_number": 12,
            "reference_characters": reference_characters,
            "baseline": {
                "edit_distance": 1,
                "cer": round(1 / reference_characters, 6),
                "latency_ms": 1.0,
                "error_type": None,
                "error_code": None,
            },
            "second_model": {
                "edit_distance": 1,
                "cer": round(1 / reference_characters, 6),
                "latency_ms": 1.0,
                "error_type": None,
                "error_code": None,
            },
        }
    ]
    return report


class FakeProcess:
    def __init__(self, report=None, *, stderr=b"", polls=None, returncode=0):
        payload = b"" if report is None else json.dumps(report).encode("utf-8")
        self.pid = 42420
        self.stdout = io.BytesIO(payload)
        self.stderr = io.BytesIO(stderr)
        self.returncode = None
        self._final_returncode = returncode
        self._polls = list([None, returncode] if polls is None else polls)
        self.killed = False

    def poll(self):
        if self.returncode is not None:
            return self.returncode
        if self._polls:
            value = self._polls.pop(0)
            if value is not None:
                self.returncode = value
            return value
        return None

    def wait(self, timeout=None):
        del timeout
        if self.returncode is None:
            self.returncode = self._final_returncode
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -signal.SIGKILL


class FakeProbe:
    def __init__(self, snapshots=None, *, baseline=None, group_counts=None):
        self.baseline = baseline or SUPERVISOR.SystemSnapshot(100, "normal", 60)
        self.snapshots = list(
            snapshots
            or [SUPERVISOR.ResourceSnapshot(100, "normal", 60, 1024, 1)]
        )
        self.group_counts = list([0] if group_counts is None else group_counts)
        self.closed = False

    def preflight(self):
        if isinstance(self.baseline, BaseException):
            raise self.baseline
        return self.baseline

    def snapshot(self, pgid):
        del pgid
        if not self.snapshots:
            return SUPERVISOR.ResourceSnapshot(100, "normal", 60, 1024, 1)
        value = self.snapshots.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    def group_member_count(self, pgid):
        del pgid
        if self.group_counts:
            return self.group_counts.pop(0)
        return 0

    def close(self):
        self.closed = True


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def monotonic(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds
        time.sleep(0)


class Harness:
    def __init__(self, process, probe=None):
        self.process = process
        self.probe = probe or FakeProbe()
        self.spawned = []
        self.terminated = []
        self.clock = FakeClock()

    def process_factory(self, command, **options):
        self.spawned.append((command, options))
        return self.process

    def terminator(self, process, pgid, probe, limits):
        del probe, limits
        self.terminated.append((process, pgid))
        process.returncode = -signal.SIGTERM

    def run(self, *, limits=None):
        return SUPERVISOR.run_supervised(
            ["--dataset-root", "/private/dataset", "--metrics-only"],
            limits or SUPERVISOR.Limits(),
            process_factory=self.process_factory,
            probe_factory=lambda: self.probe,
            getpgid=lambda pid: pid,
            group_terminator=self.terminator,
            monotonic=self.clock.monotonic,
            sleep=self.clock.sleep,
        )


class SupervisorRunTests(unittest.TestCase):
    def test_success_forces_metrics_only_and_safe_spawn_contract(self):
        harness = Harness(FakeProcess(valid_report()))
        result = harness.run()

        self.assertEqual(result.exit_code, 0)
        self.assertTrue(result.report["complete"])
        self.assertEqual(result.report["supervisor"], SUPERVISOR.SUPERVISOR_NAME)
        self.assertTrue(result.report["resource_observed"]["monitor_complete"])
        self.assertEqual(result.report["resource_observed"]["monitor_samples"], 2)
        observed_safety = result.report["performance_resources"][
            "safety_limits_observed"
        ]
        self.assertEqual(observed_safety["status"], "pass")
        self.assertFalse(observed_safety["kernel_enforced"])
        self.assertEqual(
            result.report["evaluation"]["quality_protocol"]["scope"],
            "quality_only",
        )
        self.assertNotIn("errors", result.report["evaluation"])
        self.assertFalse(harness.terminated)
        command, options = harness.spawned[0]
        self.assertEqual(command[-1], "--metrics-only")
        self.assertEqual(command.count("--metrics-only"), 1)
        self.assertEqual(command.count("--protocol"), 1)
        self.assertIn(PROTOCOL.PROTOCOL_VERSION, command)
        self.assertEqual(command[command.index("--num-threads") + 1], "1")
        self.assertEqual(
            command[command.index("--timeout") + 1],
            str(SUPERVISOR.SINGLE_CALL_TIMEOUT_SECONDS),
        )
        self.assertFalse(options["shell"])
        self.assertTrue(options["start_new_session"])
        self.assertIs(options["stdin"], SUPERVISOR.subprocess.DEVNULL)
        self.assertIs(options["stdout"], SUPERVISOR.subprocess.PIPE)
        self.assertIs(options["stderr"], SUPERVISOR.subprocess.PIPE)
        self.assertTrue(options["close_fds"])
        self.assertFalse(options["text"])
        self.assertTrue(harness.probe.closed)

    def test_immediate_child_exit_without_a_resource_sample_fails_closed(self):
        process = FakeProcess(valid_report(), polls=[0])
        probe = FakeProbe(
            [SUPERVISOR.ResourceSnapshot(100, "normal", 60, 0, 0)],
            group_counts=[0],
        )
        result = Harness(process, probe).run()

        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(
            result.report["error"]["code"],
            "resource_monitor_unavailable",
        )

    def test_include_transcripts_is_rejected_before_preflight_or_spawn(self):
        spawned = []
        result = SUPERVISOR.run_supervised(
            ["--dataset-root", "/private/secret", "--include-transcripts"],
            process_factory=lambda *args, **kwargs: spawned.append((args, kwargs)),
        )
        self.assertEqual(result.exit_code, 2)
        self.assertEqual(result.report["error"]["code"], "transcript_output_forbidden")
        self.assertFalse(spawned)
        self.assertNotIn("secret", json.dumps(result.report))

    def test_protocol_and_resource_control_overrides_are_rejected_before_spawn(self):
        cases = (
            (["--protocol", "private"], "evaluation_protocol_override_forbidden"),
            (["--protocol=private"], "evaluation_protocol_override_forbidden"),
            (["--timeout", "999"], "controlled_evaluator_argument_forbidden"),
            (["--num-threads=8"], "controlled_evaluator_argument_forbidden"),
            (["--language", "private"], "controlled_evaluator_argument_forbidden"),
        )
        for arguments, expected in cases:
            with self.subTest(arguments=arguments):
                spawned = []
                result = SUPERVISOR.run_supervised(
                    ["--dataset-root", "/private/secret", *arguments],
                    process_factory=lambda *args, **kwargs: spawned.append(
                        (args, kwargs)
                    ),
                )
                self.assertEqual(result.exit_code, 2)
                self.assertEqual(result.report["error"]["code"], expected)
                self.assertFalse(spawned)
                self.assertNotIn("secret", json.dumps(result.report))

    def test_rss_swap_pressure_and_probe_failure_kill_the_group(self):
        cases = (
            (
                SUPERVISOR.ResourceSnapshot(
                    100,
                    "normal",
                    60,
                    SUPERVISOR.RSS_LIMIT_BYTES + 1,
                    1,
                ),
                "rss_limit_exceeded",
            ),
            (
                SUPERVISOR.ResourceSnapshot(
                    100 + SUPERVISOR.SWAP_GROWTH_LIMIT_BYTES + 1,
                    "normal",
                    60,
                    1024,
                    1,
                ),
                "swap_limit_exceeded",
            ),
            (
                SUPERVISOR.ResourceSnapshot(100, "warning", 60, 1024, 1),
                "memory_pressure_not_normal",
            ),
            (
                SUPERVISOR.ResourceSnapshot(100, "normal", 19, 1024, 1),
                "memory_pressure_not_normal",
            ),
            (SUPERVISOR.MonitorUnavailableError("private"), "resource_monitor_unavailable"),
        )
        for snapshot, expected in cases:
            with self.subTest(expected=expected):
                process = FakeProcess(valid_report(), polls=[None])
                harness = Harness(process, FakeProbe([snapshot]))
                result = harness.run()
                self.assertEqual(result.report["error"]["code"], expected)
                self.assertEqual(harness.terminated, [(process, process.pid)])
                self.assertFalse(result.report["complete"])

    def test_preflight_pressure_or_monitor_failure_never_spawns(self):
        for baseline, expected in (
            (SUPERVISOR.SystemSnapshot(100, "critical", 60), "memory_pressure_not_normal"),
            (SUPERVISOR.SystemSnapshot(100, "normal", 10), "memory_pressure_not_normal"),
            (SUPERVISOR.MonitorUnavailableError("secret"), "resource_monitor_unavailable"),
            (SUPERVISOR.SystemSnapshot(-1, "normal", 60), "resource_monitor_unavailable"),
            (SUPERVISOR.SystemSnapshot(100, "unknown", 60), "resource_monitor_unavailable"),
            (SUPERVISOR.SystemSnapshot(100, "normal", 101), "resource_monitor_unavailable"),
        ):
            with self.subTest(expected=expected):
                harness = Harness(FakeProcess(valid_report()), FakeProbe(baseline=baseline))
                result = harness.run()
                self.assertEqual(result.report["error"]["code"], expected)
                self.assertFalse(harness.spawned)

    def test_invalid_resource_snapshot_kills_the_group(self):
        invalid_snapshots = (
            SUPERVISOR.ResourceSnapshot(-1, "normal", 60, 1024, 1),
            SUPERVISOR.ResourceSnapshot(100, "unknown", 60, 1024, 1),
            SUPERVISOR.ResourceSnapshot(100, "normal", 60, -1, 1),
            SUPERVISOR.ResourceSnapshot(100, "normal", 60, 1024, -1),
        )
        for snapshot in invalid_snapshots:
            with self.subTest(snapshot=snapshot):
                process = FakeProcess(valid_report(), polls=[None])
                harness = Harness(process, FakeProbe([snapshot]))
                result = harness.run()
                self.assertEqual(
                    result.report["error"]["code"],
                    "resource_monitor_unavailable",
                )
                self.assertEqual(harness.terminated, [(process, process.pid)])

    def test_exit_transition_still_checks_global_swap_postflight(self):
        process = FakeProcess(valid_report(), polls=[None, 0])
        probe = FakeProbe(
            [
                SUPERVISOR.ResourceSnapshot(100, "normal", 60, 1024, 1),
                SUPERVISOR.ResourceSnapshot(
                    100 + SUPERVISOR.SWAP_GROWTH_LIMIT_BYTES + 1,
                    "normal",
                    60,
                    0,
                    0,
                ),
            ]
        )
        harness = Harness(process, probe)

        result = harness.run()

        self.assertEqual(result.report["error"]["code"], "swap_limit_exceeded")
        self.assertEqual(harness.terminated, [(process, process.pid)])

    def test_timeout_kills_group(self):
        process = FakeProcess(valid_report(), polls=[None, None, None])
        harness = Harness(process)
        limits = replace(
            SUPERVISOR.Limits(),
            wall_timeout_seconds=0.05,
            poll_interval_seconds=0.10,
        )
        result = harness.run(limits=limits)
        self.assertEqual(result.report["error"]["code"], "wall_timeout_exceeded")
        self.assertEqual(harness.terminated, [(process, process.pid)])

    def test_combined_output_limit_kills_or_rejects_without_echo(self):
        private = b"private transcript and /Users/private/audio.wav"
        process = FakeProcess(valid_report(), stderr=private)
        harness = Harness(process)
        result = harness.run(limits=replace(SUPERVISOR.Limits(), output_bytes=8))
        rendered = json.dumps(result.report)
        self.assertEqual(result.report["error"]["code"], "output_limit_exceeded")
        self.assertNotIn("private", rendered)
        self.assertNotIn("audio", rendered)

    def test_nonzero_invalid_incomplete_and_privacy_reports_are_nonzero(self):
        invalid_private = FakeProcess(None, stderr=b"/private/transcript", returncode=2)
        invalid_private.stdout = io.BytesIO(b"not-json private transcript")
        incomplete = valid_report()
        incomplete["complete"] = False
        incomplete["provider_failure_count"] = 1
        leaky = valid_report()
        leaky["privacy"] = dict(leaky["privacy"], transcripts_included=True)
        leaky["secret_transcript"] = "never echo me"
        for process, expected in (
            (invalid_private, "child_failed"),
            (FakeProcess(None, polls=[None, 0]), "child_output_invalid"),
            (FakeProcess(incomplete), "child_report_incomplete"),
            (FakeProcess(leaky), "child_output_invalid"),
        ):
            with self.subTest(expected=expected):
                result = Harness(process).run()
                self.assertNotEqual(result.exit_code, 0)
                self.assertEqual(result.report["error"]["code"], expected)
                self.assertNotIn("transcript", json.dumps(result.report))

    def test_leader_exit_with_descendant_is_killed(self):
        process = FakeProcess(valid_report(), polls=[0])
        probe = FakeProbe(group_counts=[1, 0])
        harness = Harness(process, probe)
        result = harness.run()
        self.assertEqual(result.report["error"]["code"], "descendant_process_survived")
        self.assertEqual(harness.terminated, [(process, process.pid)])

    def test_process_group_contract_mismatch_kills_only_leader(self):
        process = FakeProcess(valid_report(), polls=[None])
        probe = FakeProbe()
        result = SUPERVISOR.run_supervised(
            ["--dataset-root", "/private/data"],
            process_factory=lambda *args, **kwargs: process,
            probe_factory=lambda: probe,
            getpgid=lambda pid: pid + 1,
        )
        self.assertEqual(result.report["error"]["code"], "process_group_invalid")
        self.assertTrue(process.killed)


class SupervisorPrimitiveTests(unittest.TestCase):
    def test_ctypes_structures_match_darwin_abi(self):
        self.assertEqual(SUPERVISOR.ctypes.sizeof(SUPERVISOR._ProcTaskInfo), 96)
        self.assertEqual(SUPERVISOR.ctypes.sizeof(SUPERVISOR._SwapUsage), 32)
        self.assertEqual(SUPERVISOR.ctypes.sizeof(SUPERVISOR._VMStatistics64), 152)

    def test_metrics_schema_rejects_unknown_text_even_if_privacy_claim_is_false(self):
        report = valid_report()
        report["errors"] = [{"transcripts": {"ground_truth": "private"}}]
        report["error_count"] = 1
        self.assertEqual(
            SUPERVISOR._validate_metrics_report(report),
            "child_output_invalid",
        )

    def test_metrics_schema_requires_valid_fixed_protocol_aggregate(self):
        report = valid_report()
        report["quality_protocol"] = dict(report["quality_protocol"])
        report["quality_protocol"]["private_transcript"] = "must not pass"
        self.assertEqual(
            SUPERVISOR._validate_metrics_report(report),
            "child_output_invalid",
        )

    def test_metrics_schema_rejects_inconsistent_aggregate_counts(self):
        mutations = []

        wrong_reference = valid_report()
        wrong_reference["baseline"]["reference_characters"] += 1
        mutations.append(wrong_reference)

        impossible_accuracy = valid_report()
        impossible_accuracy["second_model"]["identifier_correct"] = 9
        mutations.append(impossible_accuracy)

        negative_latency = valid_report()
        negative_latency["second_model"]["latency_ms"]["p50"] = -1.0
        mutations.append(negative_latency)

        unavailable_despite_completion = valid_report()
        unavailable_despite_completion["comparison"]["unavailable_count"] = 1
        unavailable_despite_completion["comparison"]["agreement_count"] = 23
        mutations.append(unavailable_despite_completion)

        for report in mutations:
            with self.subTest(report=report):
                self.assertEqual(
                    SUPERVISOR._validate_metrics_report(report),
                    "child_output_invalid",
                )

    def test_metrics_schema_enforces_provider_roles(self):
        wrong_baseline = valid_report()
        wrong_baseline["baseline"]["name"] = "qwen3_asr"
        wrong_candidate = valid_report()
        wrong_candidate["second_model"]["name"] = "voiceflypy_x_asr"

        for report in (wrong_baseline, wrong_candidate):
            with self.subTest():
                self.assertEqual(
                    SUPERVISOR._validate_metrics_report(report),
                    "child_output_invalid",
                )

    def test_metrics_schema_rejects_inconsistent_error_details(self):
        report = valid_report()
        report["baseline"].update(
            cer=round(2 / REFERENCE_CHARACTERS, 6),
            edit_distance=2,
        )
        report["second_model"].update(
            cer=round(1 / REFERENCE_CHARACTERS, 6),
            edit_distance=1,
        )
        report["comparison"].update(
            agreement_count=23,
            disagreement_count=1,
            second_better_count=1,
            equal_error_count=23,
        )
        report["error_count"] = 1
        report["errors"] = [
            {
                "sample_number": 1,
                "reference_characters": 4,
                "baseline": {
                    "edit_distance": 2,
                    "cer": 0.5,
                    "latency_ms": 1.0,
                    "error_type": None,
                    "error_code": None,
                },
                "second_model": {
                    "edit_distance": 1,
                    "cer": 0.25,
                    "latency_ms": 1.0,
                    "error_type": None,
                    "error_code": None,
                },
            }
        ]
        self.assertEqual(
            SUPERVISOR._validate_metrics_report(report),
            "child_output_invalid",
        )

        report = consistent_validation_error_report()
        self.assertIsNone(SUPERVISOR._validate_metrics_report(report))
        report["errors"][0]["second_model"]["cer"] = 0.5
        self.assertEqual(
            SUPERVISOR._validate_metrics_report(report),
            "child_output_invalid",
        )

    def test_metrics_schema_binds_error_lengths_and_split_edit_sums(self):
        wrong_length = consistent_validation_error_report()
        wrong_length["errors"][0]["reference_characters"] = 13
        for role in ("baseline", "second_model"):
            wrong_length["errors"][0][role]["cer"] = round(1 / 13, 6)

        wrong_split_sum = consistent_validation_error_report()
        wrong_split_sum["quality_protocol"]["split_edit_distance"]["validation"][
            "baseline_edit_distance"
        ] = 2

        for report in (wrong_length, wrong_split_sum):
            with self.subTest():
                self.assertEqual(
                    SUPERVISOR._validate_metrics_report(report),
                    "child_output_invalid",
                )

    def test_metrics_schema_binds_identifier_counts_and_nonzero_edits(self):
        provider_category_mismatch = valid_report()
        provider_category_mismatch["quality_protocol"] = quality_protocol_report(
            candidate_overrides={"C01": "W 356"}
        )

        impossible_exact_count = valid_report()
        impossible_exact_count["quality_protocol"] = quality_protocol_report(
            candidate_overrides={"C01": "W 356"}
        )
        impossible_exact_count["second_model"].update(
            cer=round(2 / REFERENCE_CHARACTERS, 6),
            edit_distance=2,
            identifier_exact_accuracy=0.875,
            identifier_correct=7,
        )
        impossible_exact_count["comparison"].update(
            agreement_count=22,
            disagreement_count=2,
            baseline_better_count=2,
            equal_error_count=22,
        )
        impossible_exact_count["error_count"] = 2
        impossible_exact_count["errors"] = []
        for sample_number in (1, 2):
            reference_characters = (
                PROTOCOL.PROTOCOL_REFERENCE_CHARACTERS_BY_SAMPLE_NUMBER[
                    sample_number
                ]
            )
            impossible_exact_count["errors"].append(
                {
                    "sample_number": sample_number,
                    "reference_characters": reference_characters,
                    "baseline": {
                        "edit_distance": 0,
                        "cer": 0.0,
                        "latency_ms": 1.0,
                        "error_type": None,
                        "error_code": None,
                    },
                    "second_model": {
                        "edit_distance": 1,
                        "cer": round(1 / reference_characters, 6),
                        "latency_ms": 1.0,
                        "error_type": None,
                        "error_code": None,
                    },
                }
            )

        for report in (provider_category_mismatch, impossible_exact_count):
            with self.subTest():
                self.assertEqual(
                    SUPERVISOR._validate_metrics_report(report),
                    "child_output_invalid",
                )

    def test_metrics_schema_rejects_calibration_category_exact_conflicts(self):
        for sample_number in (4, 8, 7):
            report = valid_report()
            report["second_model"].update(
                cer=round(1 / REFERENCE_CHARACTERS, 6),
                edit_distance=1,
            )
            report["comparison"].update(
                agreement_count=23,
                disagreement_count=1,
                baseline_better_count=1,
                equal_error_count=23,
            )
            reference_characters = (
                PROTOCOL.PROTOCOL_REFERENCE_CHARACTERS_BY_SAMPLE_NUMBER[
                    sample_number
                ]
            )
            report["error_count"] = 1
            report["errors"] = [
                {
                    "sample_number": sample_number,
                    "reference_characters": reference_characters,
                    "baseline": {
                        "edit_distance": 0,
                        "cer": 0.0,
                        "latency_ms": 1.0,
                        "error_type": None,
                        "error_code": None,
                    },
                    "second_model": {
                        "edit_distance": 1,
                        "cer": round(1 / reference_characters, 6),
                        "latency_ms": 1.0,
                        "error_type": None,
                        "error_code": None,
                    },
                }
            ]
            with self.subTest(sample_number=sample_number):
                self.assertEqual(
                    SUPERVISOR._validate_metrics_report(report),
                    "child_output_invalid",
                )

    def test_candidate_p95_failure_is_reported_without_claiming_readiness(self):
        report = valid_report()
        report["second_model"]["latency_ms"] = {
            "p50": 1.0,
            "p95": SUPERVISOR.P95_LATENCY_LIMIT_MS + 1.0,
        }
        result = Harness(FakeProcess(report)).run()

        self.assertEqual(result.exit_code, 0)
        performance = result.report["performance_resources"]
        self.assertEqual(performance["status"], "fail")
        self.assertEqual(performance["candidate_p95_latency"]["status"], "fail")
        self.assertEqual(performance["stable_rss_drift"]["status"], "pending")

    def test_pressure_cancel_timeout_retains_live_c_objects_and_callbacks(self):
        class FakeDispatch:
            def __init__(self):
                self.cancelled = []
                self.released = []

            def dispatch_source_cancel(self, source):
                self.cancelled.append(source.value)

            def dispatch_release(self, value):
                self.released.append(value.value)

        library = FakeDispatch()
        monitor = SUPERVISOR._DispatchMemoryPressureMonitor(
            registration_timeout_seconds=0.001
        )
        monitor._library = library
        monitor._source = 101
        monitor._queue = 202
        monitor._event_callback = object()
        monitor._registration_callback = object()
        monitor._cancel_callback = object()
        before_retained = len(SUPERVISOR._UNCANCELLED_PRESSURE_MONITORS)

        with self.assertRaises(SUPERVISOR.MonitorUnavailableError):
            monitor.close()

        self.assertEqual(library.cancelled, [101])
        self.assertEqual(library.released, [])
        self.assertEqual(monitor._source, 101)
        self.assertEqual(monitor._queue, 202)
        self.assertIs(monitor._library, library)
        self.assertIsNotNone(monitor._event_callback)
        self.assertEqual(
            len(SUPERVISOR._UNCANCELLED_PRESSURE_MONITORS),
            before_retained + 1,
        )
        SUPERVISOR._UNCANCELLED_PRESSURE_MONITORS.pop()

    def test_swap_probe_locks_to_permission_free_fallback_after_sysctl_denial(self):
        probe = object.__new__(SUPERVISOR.DarwinResourceProbe)
        probe._swap_mode = None
        probe._swapusage_used = mock.Mock(
            side_effect=SUPERVISOR.MonitorUnavailableError("denied")
        )
        probe._cumulative_swapout_bytes = mock.Mock(side_effect=[100, 132])

        self.assertEqual(probe._swap_used(), 100)
        self.assertEqual(probe._swap_used(), 132)
        self.assertEqual(probe._swap_mode, "host_vm_info64_swapouts")
        self.assertEqual(probe._swapusage_used.call_count, 1)

    def test_default_terminator_escalates_from_term_to_kill(self):
        process = FakeProcess(valid_report(), polls=[None, None, None])
        probe = FakeProbe(group_counts=[0])
        clock = FakeClock()
        signals = []

        def killpg(pgid, group_signal):
            signals.append((pgid, group_signal))
            if group_signal == signal.SIGKILL:
                process.returncode = -signal.SIGKILL

        SUPERVISOR._terminate_process_group(
            process,
            process.pid,
            probe,
            replace(
                SUPERVISOR.Limits(),
                terminate_grace_seconds=0.05,
            ),
            killpg=killpg,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )
        self.assertEqual(
            signals,
            [(process.pid, signal.SIGTERM), (process.pid, signal.SIGKILL)],
        )
        self.assertEqual(process.returncode, -signal.SIGKILL)

    def test_monitor_preflight_failure_is_runtime_nonzero(self):
        harness = Harness(
            FakeProcess(valid_report()),
            FakeProbe(baseline=SUPERVISOR.MonitorUnavailableError("private")),
        )
        result = harness.run()
        self.assertEqual(result.exit_code, 3)
        self.assertEqual(result.report["error"]["code"], "resource_monitor_unavailable")

    def test_cli_errors_are_fixed_json_without_argument_echo(self):
        stdout = io.StringIO()
        code = SUPERVISOR.main(
            ["--dataset-root", "/Users/private/audio"],
            stdout=stdout,
        )
        self.assertEqual(code, 2)
        report = json.loads(stdout.getvalue())
        self.assertEqual(report["error"]["code"], "supervisor_arguments_invalid")
        self.assertNotIn("private", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
