import hashlib
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

import voice_fly_service as SERVICE
import voice_refiner as REFINER


class CapturedThread:
    instances = []

    def __init__(self, *, target, args=(), daemon=False):
        self.target = target
        self.args = args
        self.daemon = daemon
        self.started = False
        self.__class__.instances.append(self)

    def start(self):
        self.started = True


class CapturedTimer:
    instances = []

    def __init__(self, interval, function, args=(), kwargs=None):
        self.interval = interval
        self.function = function
        self.args = args
        self.kwargs = kwargs or {}
        self.daemon = False
        self.started = False
        self.cancelled = False
        self.__class__.instances.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        if not self.cancelled:
            return self.function(*self.args, **self.kwargs)
        return None


class FakeRecognizer:
    def __init__(self, inline_error=None, plain_error=None):
        self.inline_error = inline_error
        self.plain_error = plain_error
        self.created_with = []

    def create_stream(self, hotwords=None):
        self.created_with.append(hotwords)
        if hotwords is not None and self.inline_error is not None:
            raise self.inline_error
        if hotwords is None and self.plain_error is not None:
            raise self.plain_error
        return object()


class FakeService:
    def __init__(self, snapshot=None):
        self.lock = threading.RLock()
        self.phase = "idle"
        self.session_id = ""
        self.stream = None
        self.stop_event = threading.Event()
        self.recognizer = FakeRecognizer()
        self.inline_hotword_supported = True
        self.hotword_snapshot = snapshot or SERVICE.InlineHotwordSnapshot(
            "测 试 :2.5\n",
            "snapshot-a",
            1,
            15,
        )
        self.posts = []
        self.post_metadata = []
        self.refinement_results = []
        self.refreshes = 0
        self.terminated_for_recovery = False

    def post(self, name, session_id, text="", message="", **metadata):
        self.posts.append((name, session_id, text, message))
        self.post_metadata.append((name, metadata))

    def log_refinement_result(self, request, result):
        self.refinement_results.append((request, result))

    def safely_log_refinement_result(self, request, result):
        return SERVICE.VoiceService.safely_log_refinement_result(
            self,
            request,
            result,
        )

    def refine_final_text(
        self,
        session_id,
        raw_segments,
        raw_text,
        baseline_text,
        audio_buffer,
    ):
        return SERVICE.VoiceService.refine_final_text(
            self,
            session_id,
            raw_segments,
            raw_text,
            baseline_text,
            audio_buffer,
        )

    def record_worker(self, *_args):
        raise AssertionError("worker should not run in this unit test")

    def schedule_hotword_refresh(self):
        self.refreshes += 1
        return True

    def handle_microphone_open_timeout(self, session_id, attempt):
        return SERVICE.VoiceService.handle_microphone_open_timeout(
            self,
            session_id,
            attempt,
        )

    def terminate_for_audio_recovery(self):
        self.terminated_for_recovery = True

    def request_audio_recovery(self, *args):
        return SERVICE.VoiceService.request_audio_recovery(self, *args)

    def ensure_finalization_deadline(self, *args):
        return SERVICE.VoiceService.ensure_finalization_deadline(self, *args)

    def set_audio_stage(self, *args):
        return SERVICE.VoiceService.set_audio_stage(self, *args)

    def close_microphone(self, *args):
        return SERVICE.VoiceService.close_microphone(self, *args)

class InlineSnapshotTests(unittest.TestCase):
    def decoder_for(self, path, enabled=True):
        return types.SimpleNamespace(enabled=enabled, hotwords_file=path)

    def test_snapshot_is_single_read_validated_and_content_hashed(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "hotwords.txt"
            data = "测 试 :2.5\n语 音 :3\n".encode()
            path.write_bytes(data)
            snapshot = SERVICE.load_inline_hotword_snapshot(self.decoder_for(path))

        self.assertEqual(snapshot.text, data.decode())
        self.assertEqual(snapshot.sha256, hashlib.sha256(data).hexdigest())
        self.assertEqual(snapshot.entries, 2)
        self.assertEqual(snapshot.byte_count, len(data))

    def test_snapshot_rejects_reserved_separator_malformed_and_limits(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "hotwords.txt"
            path.write_text("测/试 :2.5\n", encoding="utf-8")
            with self.assertRaisesRegex(OSError, "reserved"):
                SERVICE.load_inline_hotword_snapshot(self.decoder_for(path))

            path.write_text("broken\n", encoding="utf-8")
            with self.assertRaisesRegex(OSError, "validation"):
                SERVICE.load_inline_hotword_snapshot(self.decoder_for(path))

            path.write_text("测 试 :2.5\n语 音 :3\n", encoding="utf-8")
            with mock.patch.object(SERVICE, "MAX_INLINE_HOTWORD_ENTRIES", 1):
                with self.assertRaisesRegex(OSError, "entry limit"):
                    SERVICE.load_inline_hotword_snapshot(self.decoder_for(path))
            with mock.patch.object(SERVICE, "MAX_INLINE_HOTWORD_BYTES", 2):
                with self.assertRaisesRegex(OSError, "byte limit"):
                    SERVICE.load_inline_hotword_snapshot(self.decoder_for(path))

    def test_disabled_decoder_has_canonical_empty_snapshot(self):
        snapshot = SERVICE.load_inline_hotword_snapshot(self.decoder_for(None, enabled=False))
        self.assertEqual(snapshot, SERVICE.EMPTY_HOTWORD_SNAPSHOT)


class ServiceInitializationTests(unittest.TestCase):
    def test_model_is_built_once_without_a_bound_hotword_file(self):
        requested = types.SimpleNamespace(enabled=True)
        active = types.SimpleNamespace(enabled=True)
        snapshot = SERVICE.InlineHotwordSnapshot("测 试 :2.5\n", "digest", 1, 15)
        fake = mock.Mock()
        fake.base_recognizer_options = {"tokens": "tokens.txt"}

        with (
            mock.patch.object(
                SERVICE,
                "create_recognizer_with_fallback",
                return_value=("recognizer", active),
            ) as factory,
            mock.patch.object(
                SERVICE,
                "load_inline_hotword_snapshot",
                return_value=snapshot,
            ),
        ):
            SERVICE.VoiceService.initialize_recognizer(fake, requested)

        self.assertEqual(fake.recognizer, "recognizer")
        self.assertIs(fake.recognizer_capability_config, active)
        self.assertEqual(fake.hotword_snapshot, snapshot)
        self.assertFalse(factory.call_args.kwargs["include_hotwords_file"])


class VoiceStartTests(unittest.TestCase):
    def setUp(self):
        CapturedThread.instances.clear()

    def test_start_binds_snapshot_to_stream_and_acknowledges(self):
        fake = FakeService()
        with mock.patch.object(SERVICE.threading, "Thread", CapturedThread):
            SERVICE.VoiceService.start_recording(fake, "session-1")

        self.assertEqual(fake.phase, "recording")
        self.assertEqual(fake.session_id, "session-1")
        self.assertEqual(fake.recognizer.created_with, [fake.hotword_snapshot.text])
        self.assertEqual(fake.posts, [(SERVICE.STARTED_NAME, "session-1", "", "")])
        self.assertEqual(len(CapturedThread.instances), 1)
        self.assertTrue(CapturedThread.instances[0].started)

    def test_inline_stream_failure_falls_back_to_plain_stream(self):
        fake = FakeService()
        fake.recognizer = FakeRecognizer(inline_error=RuntimeError("bad inline graph"))
        with mock.patch.object(SERVICE.threading, "Thread", CapturedThread):
            SERVICE.VoiceService.start_recording(fake, "session-fallback")

        self.assertEqual(fake.phase, "recording")
        self.assertEqual(fake.recognizer.created_with, [fake.hotword_snapshot.text, None])
        self.assertEqual(fake.posts[0][0], SERVICE.STARTED_NAME)

    def test_both_stream_attempts_failing_leave_service_idle(self):
        fake = FakeService()
        fake.recognizer = FakeRecognizer(
            inline_error=RuntimeError("bad inline"),
            plain_error=RuntimeError("bad plain"),
        )
        SERVICE.VoiceService.start_recording(fake, "session-failed")

        self.assertEqual(fake.phase, "idle")
        self.assertEqual(fake.session_id, "")
        self.assertIsNone(fake.stream)
        self.assertEqual(fake.posts[-1][0], SERVICE.ERROR_NAME)

    def test_thread_start_failure_rolls_back_and_posts_error(self):
        class FailingThread:
            def __init__(self, **_kwargs):
                pass

            def start(self):
                raise RuntimeError("thread unavailable")

        fake = FakeService()
        with mock.patch.object(SERVICE.threading, "Thread", FailingThread):
            SERVICE.VoiceService.start_recording(fake, "session-thread")

        self.assertEqual(fake.phase, "idle")
        self.assertEqual(
            [item[0] for item in fake.posts],
            [SERVICE.STARTED_NAME, SERVICE.ERROR_NAME],
        )

    def test_busy_start_never_creates_another_stream(self):
        fake = FakeService()
        fake.phase = "finalizing"
        SERVICE.VoiceService.start_recording(fake, "session-busy")
        self.assertEqual(fake.recognizer.created_with, [])
        self.assertEqual(fake.posts[-1][0], SERVICE.ERROR_NAME)


class InlineRefreshTests(unittest.TestCase):
    def make_service(self):
        fake = mock.Mock()
        fake.lock = threading.RLock()
        fake.hotword_refresh_epoch = 3
        fake.hotword_refresh_requested = False
        fake.inline_hotword_supported = True
        fake.decoder_config = types.SimpleNamespace(enabled=True)
        fake.recognizer_capability_config = types.SimpleNamespace(
            enabled=True,
            decoding_method="modified_beam_search",
            max_active_paths=8,
            modeling_unit="bpe",
            bpe_vocab=Path("bpe.vocab"),
            hotwords_score=1.5,
        )
        fake.hotword_snapshot = SERVICE.InlineHotwordSnapshot("old :1\n", "old", 1, 7)
        return fake

    def requested_decoder(self):
        return types.SimpleNamespace(
            enabled=True,
            resource_error=None,
            decoding_method="modified_beam_search",
            max_active_paths=8,
            modeling_unit="bpe",
            bpe_vocab=Path("bpe.vocab"),
            hotwords_score=1.5,
        )

    def test_refresh_swaps_snapshot_without_constructing_a_recognizer(self):
        fake = self.make_service()
        requested = self.requested_decoder()
        new = SERVICE.InlineHotwordSnapshot("new :2\n", "new", 1, 7)
        with (
            mock.patch.object(SERVICE, "resolve_decoder_config", return_value=requested),
            mock.patch.object(SERVICE, "load_inline_hotword_snapshot", return_value=new),
            mock.patch.object(
                SERVICE.sherpa_onnx.OnlineRecognizer,
                "from_transducer",
            ) as factory,
        ):
            changed = SERVICE.VoiceService.refresh_inline_hotwords(fake)

        self.assertTrue(changed)
        self.assertEqual(fake.hotword_snapshot, new)
        factory.assert_not_called()

    def test_invalid_or_structurally_changed_snapshot_preserves_last_good(self):
        fake = self.make_service()
        requested = self.requested_decoder()
        with (
            mock.patch.object(SERVICE, "resolve_decoder_config", return_value=requested),
            mock.patch.object(
                SERVICE,
                "load_inline_hotword_snapshot",
                side_effect=OSError("malformed"),
            ),
        ):
            self.assertFalse(SERVICE.VoiceService.refresh_inline_hotwords(fake))
        self.assertEqual(fake.hotword_snapshot.sha256, "old")

        requested.hotwords_score = 4.0
        with mock.patch.object(SERVICE, "resolve_decoder_config", return_value=requested):
            self.assertFalse(SERVICE.VoiceService.refresh_inline_hotwords(fake))
        self.assertEqual(fake.hotword_snapshot.sha256, "old")

    def test_newer_epoch_rejects_stale_snapshot_and_requeues(self):
        fake = self.make_service()
        requested = self.requested_decoder()
        new = SERVICE.InlineHotwordSnapshot("new :2\n", "new", 1, 7)

        def advance_epoch(_decoder):
            fake.hotword_refresh_epoch += 1
            return new

        with (
            mock.patch.object(SERVICE, "resolve_decoder_config", return_value=requested),
            mock.patch.object(
                SERVICE,
                "load_inline_hotword_snapshot",
                side_effect=advance_epoch,
            ),
        ):
            self.assertFalse(SERVICE.VoiceService.refresh_inline_hotwords(fake))

        self.assertEqual(fake.hotword_snapshot.sha256, "old")
        self.assertTrue(fake.hotword_refresh_requested)

    def test_greedy_startup_never_receives_inline_hotwords(self):
        fake = self.make_service()
        fake.inline_hotword_supported = False
        requested = self.requested_decoder()
        with (
            mock.patch.object(SERVICE, "resolve_decoder_config", return_value=requested),
            mock.patch.object(SERVICE, "load_inline_hotword_snapshot") as loader,
        ):
            self.assertFalse(SERVICE.VoiceService.refresh_inline_hotwords(fake))
        loader.assert_not_called()


class RefreshSchedulingTests(unittest.TestCase):
    def setUp(self):
        CapturedThread.instances.clear()
        CapturedTimer.instances.clear()

    def make_service(self):
        fake = mock.Mock()
        fake.lock = threading.RLock()
        fake.hotword_refresh_epoch = 4
        fake.hotword_refresh_timer = None
        fake.hotword_refresh_in_progress = False
        fake.hotword_refresh_requested = False
        fake.run_debounced_hotword_refresh = lambda epoch: (
            SERVICE.VoiceService.run_debounced_hotword_refresh(fake, epoch)
        )
        return fake

    def test_notifications_debounce_to_latest_epoch(self):
        fake = self.make_service()
        with mock.patch.object(SERVICE.threading, "Timer", CapturedTimer):
            SERVICE.VoiceService.lexicon_changed(fake)
            first = CapturedTimer.instances[-1]
            SERVICE.VoiceService.lexicon_changed(fake)
            second = CapturedTimer.instances[-1]

        self.assertTrue(first.cancelled)
        self.assertTrue(second.started)
        self.assertEqual(fake.hotword_refresh_epoch, 6)
        second.fire()
        fake.schedule_hotword_refresh.assert_called_once_with()

    def test_schedule_starts_one_daemon_worker(self):
        fake = self.make_service()
        with mock.patch.object(SERVICE.threading, "Thread", CapturedThread):
            scheduled = SERVICE.VoiceService.schedule_hotword_refresh(fake)

        self.assertTrue(scheduled)
        self.assertTrue(fake.hotword_refresh_in_progress)
        self.assertEqual(len(CapturedThread.instances), 1)
        self.assertTrue(CapturedThread.instances[0].daemon)
        self.assertTrue(CapturedThread.instances[0].started)
        fake.refresh_inline_hotwords.assert_not_called()

    def test_refresh_thread_start_failure_rolls_back_flag(self):
        class FailingThread:
            def __init__(self, **_kwargs):
                pass

            def start(self):
                raise RuntimeError("thread unavailable")

        fake = self.make_service()
        with mock.patch.object(SERVICE.threading, "Thread", FailingThread):
            self.assertFalse(SERVICE.VoiceService.schedule_hotword_refresh(fake))
        self.assertFalse(fake.hotword_refresh_in_progress)


class ScriptedRecognizer:
    def __init__(self, frame_results, endpoints, final_result=""):
        self.frame_results = list(frame_results)
        self.endpoints = list(endpoints)
        self.final_result = final_result
        self.index = -1

    def is_ready(self, _stream):
        return False

    def decode_stream(self, _stream):
        raise AssertionError("is_ready is false")

    def get_result(self, _stream):
        if 0 <= self.index < len(self.frame_results):
            return self.frame_results[self.index]
        return self.final_result

    def is_endpoint(self, _stream):
        return 0 <= self.index < len(self.endpoints) and self.endpoints[self.index]

    def reset(self, _stream):
        pass


class PassThroughSpeechStartGate:
    def __init__(self, _sample_rate):
        self.opened = True
        self.calibrated = True
        self.open_after_ms = 0.0
        self.suppressed_ms = 0.0
        self.noise_floor_dbfs = -60.0
        self.trigger_level_dbfs = -20.0
        self.policy = "test-pass-through"

    def process(self, samples):
        return np.asarray(samples, dtype=np.float32).reshape(-1)


class RecordingLifecycleTests(unittest.TestCase):
    def test_microphone_open_budget_covers_bluetooth_mode_switch(self):
        self.assertEqual(SERVICE.MICROPHONE_OPEN_TIMEOUT_SECONDS, 6.0)

    def test_release_tail_budget_is_short_and_bounded(self):
        self.assertGreaterEqual(SERVICE.AUDIO_RELEASE_TAIL_SECONDS, 0.12)
        self.assertLessEqual(SERVICE.AUDIO_RELEASE_TAIL_SECONDS, 0.20)

    def run_recording(
        self,
        frame_results,
        endpoints,
        final_result="",
        overflowed=False,
        refiner=None,
        shadow_runner=None,
        speech_gate=None,
        close_behavior=None,
        read_behavior=None,
    ):
        fake = FakeService(snapshot=SERVICE.EMPTY_HOTWORD_SNAPSHOT)
        if refiner is not None:
            fake.final_refiner = refiner
        if shadow_runner is not None:
            fake.shadow_refinement_runner = shadow_runner
        fake.phase = "recording"
        fake.session_id = "recording-session"
        stream = mock.Mock()
        fake.stream = stream
        recognizer = ScriptedRecognizer(frame_results, endpoints, final_result)
        stream.accept_waveform.side_effect = lambda _rate, samples: setattr(
            recognizer,
            "index",
            int(samples[0]),
        )
        stream.input_finished.side_effect = lambda: setattr(
            recognizer,
            "index",
            len(frame_results),
        )

        class ScriptedMic:
            def __init__(self):
                self.index = 0

            def read(self, _frame_samples):
                if read_behavior is not None:
                    return read_behavior(fake)
                if self.index == len(frame_results) - 1:
                    fake.stop_event.set()
                buf = np.full((8, 1), self.index, dtype=np.float32)
                self.index += 1
                return buf, overflowed

        active_input = mock.Mock()
        active_input.stream = ScriptedMic()
        active_input.candidate = types.SimpleNamespace(
            sample_rate=48000,
            read_frames=1920,
            name="test microphone",
        )
        active_input.selection_reason = "system-default"
        if close_behavior is not None:
            active_input.close.side_effect = lambda: close_behavior(fake)
        gate = speech_gate or PassThroughSpeechStartGate(48000)
        with (
            mock.patch.object(
                SERVICE,
                "open_first_available_input",
                return_value=active_input,
            ) as opener,
            mock.patch.object(
                SERVICE,
                "AdaptiveSpeechStartGate",
                return_value=gate,
            ),
            mock.patch.object(SERVICE, "AUDIO_RELEASE_TAIL_SECONDS", 0.0),
        ):
            SERVICE.VoiceService.record_worker(
                fake,
                "recording-session",
                recognizer,
                stream,
            )
        opener.assert_called_once_with(
            SERVICE.sd,
            prefer_builtin=SERVICE.PREFER_BUILTIN_MIC,
        )
        return fake, stream, active_input

    def test_final_text_is_delivered_before_close_and_service_stays_busy(self):
        def closing(fake):
            finals = [p for p in fake.posts if p[0] == SERVICE.FINAL_NAME]
            self.assertEqual(len(finals), 1)
            self.assertEqual(finals[0][2], "ready ")
            self.assertNotEqual(fake.phase, "idle")
            SERVICE.VoiceService.start_recording(fake, "next-session")
            self.assertEqual(fake.posts[-1][0], SERVICE.ERROR_NAME)
        with mock.patch.object(SERVICE, "normalize", side_effect=lambda text: text):
            fake, _, active = self.run_recording(["ready"], [False], close_behavior=closing)
        self.assertEqual(fake.phase, "idle")
        active.close.assert_called_once()

    def test_hung_close_triggers_recovery_without_discarding_final_text(self):
        release = threading.Event()
        observed = []
        def closing(fake):
            observed.append(fake)
            release.wait(0.15)
        with (
            mock.patch.object(SERVICE, "normalize", side_effect=lambda text: text),
            mock.patch.object(SERVICE, "MICROPHONE_CLOSE_TIMEOUT_SECONDS", 0.025),
            mock.patch.object(SERVICE, "write_audio_recovery"),
            mock.patch.object(SERVICE, "write_service_status"),
        ):
            fake, _, active = self.run_recording(["ready"], [False], close_behavior=closing)
        self.assertTrue(fake.terminated_for_recovery)
        self.assertEqual(fake.phase, "recovering")
        self.assertEqual([p[2] for p in fake.posts if p[0] == SERVICE.FINAL_NAME], ["ready "])
        active.close.assert_called_once()
        fake.finalization_deadline.finish()

    def test_failed_close_does_not_report_idle(self):
        def closing(_fake):
            raise RuntimeError("cleanup failed")
        with (
            mock.patch.object(SERVICE, "normalize", side_effect=lambda text: text),
            mock.patch.object(SERVICE, "write_audio_recovery"),
            mock.patch.object(SERVICE, "write_service_status"),
        ):
            fake, _, active = self.run_recording(["ready"], [False], close_behavior=closing)
        self.assertTrue(fake.terminated_for_recovery)
        self.assertEqual(fake.phase, "recovering")
        active.close.assert_called_once()
        fake.finalization_deadline.finish()

    def test_finished_session_cancels_deadline_and_old_timeout_cannot_restart_new_one(self):
        fake = FakeService(snapshot=SERVICE.EMPTY_HOTWORD_SNAPSHOT)
        fake.phase = "finalizing"
        fake.session_id = "new-session"
        with mock.patch.object(SERVICE, "write_audio_recovery") as diagnostic:
            self.assertFalse(fake.request_audio_recovery("old-session", "finalizing"))
        diagnostic.assert_not_called()
        self.assertFalse(fake.terminated_for_recovery)

    def test_finalization_timeout_recovers_when_worker_never_returns(self):
        fake = FakeService(snapshot=SERVICE.EMPTY_HOTWORD_SNAPSHOT)
        fake.phase = "finalizing"
        fake.session_id = "blocked-session"
        recovered = threading.Event()
        fake.terminate_for_audio_recovery = recovered.set
        with (
            mock.patch.object(SERVICE, "FINALIZATION_TIMEOUT_SECONDS", 0.025),
            mock.patch.object(SERVICE, "write_audio_recovery"),
            mock.patch.object(SERVICE, "write_service_status"),
        ):
            fake.ensure_finalization_deadline("blocked-session")
            self.assertTrue(recovered.wait(1))
        self.assertEqual(fake.phase, "recovering")
        self.assertEqual(fake.posts[-1][0], SERVICE.ERROR_NAME)
        fake.finalization_deadline.finish()

    def test_stalled_capture_recovers_without_closing_device_under_active_reader(self):
        def reading(fake):
            fake.stop_event.set()
            threading.Event().wait(0.2)
            return np.zeros((8, 1), dtype=np.float32), False
        with (
            mock.patch.object(SERVICE, "FINALIZATION_TIMEOUT_SECONDS", 0.025),
            mock.patch.object(SERVICE, "write_audio_recovery"),
            mock.patch.object(SERVICE, "write_service_status"),
        ):
            fake, _, active = self.run_recording(["unused"], [False], read_behavior=reading)
        self.assertTrue(fake.terminated_for_recovery)
        self.assertEqual(fake.phase, "recovering")
        active.close.assert_not_called()
        self.assertFalse(any(p[0] == SERVICE.FINAL_NAME for p in fake.posts))
        fake.finalization_deadline.finish()

    def test_prespeech_audio_never_reaches_the_recognizer(self):
        class ScriptedGate(PassThroughSpeechStartGate):
            def __init__(self):
                super().__init__(48000)
                self.opened = False
                self.calls = 0

            def process(self, samples):
                self.calls += 1
                if self.calls == 1:
                    return None
                self.opened = True
                self.open_after_ms = 80.0
                self.suppressed_ms = 40.0
                return np.asarray(samples, dtype=np.float32).reshape(-1)

        with mock.patch.object(SERVICE, "normalize", side_effect=lambda text: text):
            fake, stream, _input = self.run_recording(
                ["开头幻觉", "真实语音"],
                [False, False],
                speech_gate=ScriptedGate(),
            )

        stream.accept_waveform.assert_called_once()
        partials = [item[2] for item in fake.posts if item[0] == SERVICE.PARTIAL_NAME]
        self.assertEqual(partials, ["真实语音"])

    def assert_baseline_final_without_error(self, fake, baseline="基线文本 "):
        finals = [item for item in fake.posts if item[0] == SERVICE.FINAL_NAME]
        errors = [item for item in fake.posts if item[0] == SERVICE.ERROR_NAME]
        self.assertEqual(
            finals,
            [(SERVICE.FINAL_NAME, "recording-session", baseline, "")],
        )
        self.assertEqual(errors, [])

    def test_active_refiner_receives_same_audio_and_can_replace_final_text(self):
        class Backend:
            name = "test-model"

            def __init__(self):
                self.calls = []

            def refine(self, request, *, timeout_seconds):
                self.calls.append((request, timeout_seconds))
                return REFINER.BackendCandidate(
                    text="我是一个紧凶型的牌手",
                    backend=self.name,
                    confidence=0.98,
                )

        backend = Backend()
        refiner = REFINER.FinalTextRefiner(mode="active", backend=backend)
        with mock.patch.object(SERVICE, "normalize", side_effect=lambda text: text):
            fake, _stream, _input = self.run_recording(
                ["我是一个紧胸型的牌手"],
                [False],
                refiner=refiner,
            )

        finals = [item for item in fake.posts if item[0] == SERVICE.FINAL_NAME]
        self.assertEqual(
            finals,
            [(SERVICE.FINAL_NAME, "recording-session", "我是一个紧凶型的牌手 ", "")],
        )
        self.assertEqual(len(backend.calls), 1)
        captured_request, timeout_seconds = backend.calls[0]
        self.assertEqual(captured_request.audio.sample_rate, 48000)
        self.assertEqual(captured_request.audio.samples.size, 8)
        self.assertGreater(timeout_seconds, 0)
        self.assertEqual(fake.refinement_results[0][1].status, "accepted")

    def test_shadow_refiner_captures_audio_without_changing_final_text(self):
        class CapturingRunner:
            def __init__(self):
                self.requests = []

            def submit(self, request):
                self.requests.append(request)
                return True

        runner = CapturingRunner()
        refiner = REFINER.FinalTextRefiner(
            mode="shadow",
            backend=mock.Mock(name="test-model"),
        )
        with mock.patch.object(SERVICE, "normalize", side_effect=lambda text: text):
            fake, _stream, _input = self.run_recording(
                ["基线文本"],
                [False],
                refiner=refiner,
                shadow_runner=runner,
            )

        finals = [item for item in fake.posts if item[0] == SERVICE.FINAL_NAME]
        self.assertEqual(
            finals,
            [(SERVICE.FINAL_NAME, "recording-session", "基线文本 ", "")],
        )
        self.assertEqual(len(runner.requests), 1)
        self.assertEqual(runner.requests[0].audio.sample_rate, 48000)
        self.assertEqual(runner.requests[0].audio.samples.size, 8)
        self.assertEqual(fake.refinement_results, [])

    def test_refiner_audio_overflow_fails_back_without_calling_model(self):
        backend = mock.Mock(name="test-model")
        backend.name = "test-model"
        refiner = REFINER.FinalTextRefiner(
            mode="active",
            backend=backend,
            max_audio_seconds=0.0001,
        )
        with mock.patch.object(SERVICE, "normalize", side_effect=lambda text: text):
            fake, _stream, _input = self.run_recording(
                ["基线文本"],
                [False],
                refiner=refiner,
            )

        finals = [item for item in fake.posts if item[0] == SERVICE.FINAL_NAME]
        self.assertEqual(
            finals,
            [(SERVICE.FINAL_NAME, "recording-session", "基线文本 ", "")],
        )
        backend.refine.assert_not_called()
        self.assertEqual(fake.refinement_results[0][1].status, "unavailable")
        self.assertEqual(
            fake.refinement_results[0][1].reason,
            "duration_limit",
        )

    def test_refiner_audio_buffer_construction_failure_keeps_baseline_final(self):
        backend = mock.Mock(name="test-model")
        backend.name = "test-model"
        refiner = REFINER.FinalTextRefiner(mode="active", backend=backend)
        with (
            mock.patch.object(SERVICE, "normalize", side_effect=lambda text: text),
            mock.patch.object(
                SERVICE,
                "BoundedAudioBuffer",
                side_effect=RuntimeError("buffer unavailable"),
            ),
        ):
            fake, _stream, _input = self.run_recording(
                ["基线文本"],
                [False],
                refiner=refiner,
            )

        self.assert_baseline_final_without_error(fake)
        backend.refine.assert_not_called()

    def test_refiner_audio_append_failure_keeps_baseline_final(self):
        class FailingAppendBuffer:
            unavailable_reason = ""

            def append(self, _samples, _sample_rate):
                raise RuntimeError("append failed")

        backend = mock.Mock(name="test-model")
        backend.name = "test-model"
        refiner = REFINER.FinalTextRefiner(mode="active", backend=backend)
        with (
            mock.patch.object(SERVICE, "normalize", side_effect=lambda text: text),
            mock.patch.object(
                SERVICE,
                "BoundedAudioBuffer",
                return_value=FailingAppendBuffer(),
            ),
        ):
            fake, _stream, _input = self.run_recording(
                ["基线文本"],
                [False],
                refiner=refiner,
            )

        self.assert_baseline_final_without_error(fake)
        backend.refine.assert_not_called()

    def test_refiner_audio_snapshot_failure_keeps_baseline_final(self):
        class FailingSnapshotBuffer:
            unavailable_reason = ""

            def append(self, _samples, _sample_rate):
                return True

            def snapshot(self):
                raise RuntimeError("snapshot failed")

            def clear(self):
                pass

        backend = mock.Mock(name="test-model")
        backend.name = "test-model"
        refiner = REFINER.FinalTextRefiner(mode="active", backend=backend)
        with (
            mock.patch.object(SERVICE, "normalize", side_effect=lambda text: text),
            mock.patch.object(
                SERVICE,
                "BoundedAudioBuffer",
                return_value=FailingSnapshotBuffer(),
            ),
        ):
            fake, _stream, _input = self.run_recording(
                ["基线文本"],
                [False],
                refiner=refiner,
            )

        self.assert_baseline_final_without_error(fake)
        backend.refine.assert_not_called()

    def test_refiner_exception_keeps_baseline_final(self):
        refiner = mock.Mock()
        refiner.mode = "active"
        refiner.needs_audio = True
        refiner.max_audio_seconds = 20.0
        refiner.deadline.return_value = 999999.0
        refiner.refine.side_effect = RuntimeError("model failed")
        with mock.patch.object(SERVICE, "normalize", side_effect=lambda text: text):
            fake, _stream, _input = self.run_recording(
                ["基线文本"],
                [False],
                refiner=refiner,
            )

        self.assert_baseline_final_without_error(fake)
        refiner.refine.assert_called_once()

    def test_refinement_logger_exception_keeps_baseline_final(self):
        class Backend:
            name = "test-model"

            def refine(self, _request, *, timeout_seconds):
                self.timeout_seconds = timeout_seconds
                return REFINER.BackendCandidate(
                    text="基线文字",
                    backend=self.name,
                    confidence=0.98,
                )

        backend = Backend()
        refiner = REFINER.FinalTextRefiner(mode="active", backend=backend)
        with (
            mock.patch.object(SERVICE, "normalize", side_effect=lambda text: text),
            mock.patch.object(
                FakeService,
                "log_refinement_result",
                side_effect=RuntimeError("logger failed"),
            ) as logger,
        ):
            fake, _stream, _input = self.run_recording(
                ["基线文本"],
                [False],
                refiner=refiner,
            )

        self.assert_baseline_final_without_error(fake)
        self.assertGreater(backend.timeout_seconds, 0)
        logger.assert_called_once()

    def test_shadow_runner_submit_exception_keeps_baseline_final(self):
        runner = mock.Mock()
        runner.submit.side_effect = RuntimeError("thread unavailable")
        refiner = REFINER.FinalTextRefiner(
            mode="shadow",
            backend=mock.Mock(name="test-model"),
        )
        with mock.patch.object(SERVICE, "normalize", side_effect=lambda text: text):
            fake, _stream, _input = self.run_recording(
                ["基线文本"],
                [False],
                refiner=refiner,
                shadow_runner=runner,
            )

        self.assert_baseline_final_without_error(fake)
        runner.submit.assert_called_once()

    def test_empty_endpoint_keeps_last_nonempty_partial(self):
        with mock.patch.object(SERVICE, "normalize", side_effect=lambda text: text):
            fake, _stream, _input = self.run_recording(
                ["甲", "甲", ""],
                [False, False, True],
                final_result="",
            )
        finals = [item for item in fake.posts if item[0] == SERVICE.FINAL_NAME]
        self.assertEqual(finals, [(SERVICE.FINAL_NAME, "recording-session", "甲 ", "")])

    def test_multiple_endpoints_and_final_are_each_committed_once(self):
        with mock.patch.object(SERVICE, "normalize", side_effect=lambda text: text):
            fake, _stream, _input = self.run_recording(
                ["甲", "", "乙", ""],
                [False, True, False, True],
                final_result="丙",
            )
        finals = [item for item in fake.posts if item[0] == SERVICE.FINAL_NAME]
        self.assertEqual(
            finals,
            [(SERVICE.FINAL_NAME, "recording-session", "甲乙丙 ", "")],
        )
        partials = [item for item in fake.posts if item[0] == SERVICE.PARTIAL_NAME]
        self.assertEqual(
            partials,
            [
                (SERVICE.PARTIAL_NAME, "recording-session", "甲", ""),
                (SERVICE.PARTIAL_NAME, "recording-session", "甲乙", ""),
            ],
        )

    def test_segment_joining_removes_only_han_to_han_seams(self):
        cases = {
            ("语", "言输入现在还是有个小问题"): "语言输入现在还是有个小问题",
            ("第一段，", "第二段"): "第一段， 第二段",
            ("我在看", "H U D", "数据"): "我在看 H U D 数据",
            ("HELLO", "WORLD"): "HELLO WORLD",
            ("版本二点", "三点一"): "版本二点三点一",
            ("C++", "code"): "C++ code",
            ("50%", "discount"): "50% discount",
            ("hello", "世界"): "hello 世界",
            ("世界", "hello"): "世界 hello",
        }
        for segments, expected in cases.items():
            with self.subTest(segments=segments):
                self.assertEqual(
                    SERVICE.join_recognition_segments(segments),
                    expected,
                )

    def test_leading_single_han_restart_is_repaired_without_touching_real_repetition(self):
        repaired = {
            "我 我觉得这个思路正确": "我觉得这个思路正确",
            "老 老朋友我们继续": "老朋友我们继续",
        }
        preserved = (
            "好好学习",
            "我们 我们继续",
            "中间有 我 我觉得",
            "A A plan",
            "一 一",
        )
        for source, expected in repaired.items():
            with self.subTest(source=source):
                self.assertEqual(SERVICE.collapse_leading_han_restart(source), expected)
        for source in preserved:
            with self.subTest(source=source):
                self.assertEqual(SERVICE.collapse_leading_han_restart(source), source)

    def test_spelled_acronym_can_cross_acoustic_endpoints(self):
        passthrough_itn = mock.Mock()
        passthrough_itn.normalize_with_mapping.side_effect = (
            lambda _text: types.SimpleNamespace(mappings=())
        )
        with mock.patch.object(SERVICE, "_itn", passthrough_itn):
            fake, _stream, _input = self.run_recording(
                ["H", "", "U D", ""],
                [False, True, False, True],
                final_result="",
            )
        finals = [item for item in fake.posts if item[0] == SERVICE.FINAL_NAME]
        self.assertEqual(
            finals,
            [(SERVICE.FINAL_NAME, "recording-session", "HUD ", "")],
        )
        partials = [item[2] for item in fake.posts if item[0] == SERVICE.PARTIAL_NAME]
        self.assertEqual(partials, ["H", "HUD"])

    def test_repeated_raw_partial_runs_one_normalization_then_final(self):
        with (
            mock.patch.object(
                SERVICE,
                "normalize_partial",
                side_effect=lambda text: SERVICE.StreamingNormalization(text),
            ) as partial_normalizer,
            mock.patch.object(
                SERVICE,
                "normalize",
                side_effect=lambda text: text,
            ) as final_normalizer,
        ):
            fake, stream, _input = self.run_recording(
                ["测试", "测试", "测试"],
                [False, False, False],
                final_result="",
                overflowed=True,
            )
        partials = [item for item in fake.posts if item[0] == SERVICE.PARTIAL_NAME]
        finals = [item for item in fake.posts if item[0] == SERVICE.FINAL_NAME]
        self.assertEqual(partials, [(SERVICE.PARTIAL_NAME, "recording-session", "测试", "")])
        self.assertEqual(finals, [(SERVICE.FINAL_NAME, "recording-session", "测试 ", "")])
        self.assertEqual(partial_normalizer.call_count, 1)
        self.assertEqual(final_normalizer.call_count, 1)
        self.assertEqual(stream.accept_waveform.call_count, 3)

    def test_partial_notifications_carry_order_and_stability_contract(self):
        with mock.patch.object(SERVICE, "normalize", side_effect=lambda text: text):
            fake, _stream, _input = self.run_recording(
                ["现在", "现在有", "现在有三个", "现在有三个问题", "现在有三个问题了"],
                [False, False, False, False, False],
                final_result="",
            )

        partial_metadata = [
            metadata
            for name, metadata in fake.post_metadata
            if name == SERVICE.PARTIAL_NAME
        ]
        self.assertEqual(
            [metadata["sequence"] for metadata in partial_metadata],
            [1, 2, 3, 4, 5],
        )
        self.assertTrue(all("stable_utf16" in item for item in partial_metadata))
        self.assertTrue(all("revision_utf16" in item for item in partial_metadata))
        self.assertTrue(all("raw_revision_utf16" in item for item in partial_metadata))
        self.assertTrue(all("raw_rollback" in item for item in partial_metadata))
        self.assertTrue(all("itn_reclassified" in item for item in partial_metadata))
        self.assertGreaterEqual(partial_metadata[-1]["stable_utf16"], 1)

    def test_streaming_numeric_tail_waits_for_context_without_stable_rollback(self):
        passthrough_itn = mock.Mock()
        passthrough_itn.normalize_with_mapping.side_effect = (
            lambda _text: types.SimpleNamespace(mappings=())
        )
        with mock.patch.object(SERVICE, "_itn", passthrough_itn):
            fake, _stream, _input = self.run_recording(
                ["T 幺", "T 幺零", "T 幺零机", "T 幺零机柜"],
                [False, False, False, False],
                final_result="",
            )

        partials = [
            item[2]
            for item in fake.posts
            if item[0] == SERVICE.PARTIAL_NAME
        ]
        self.assertEqual(partials, ["T 幺", "T 幺零", "T10机", "T10机柜"])
        partial_metadata = [
            metadata
            for name, metadata in fake.post_metadata
            if name == SERVICE.PARTIAL_NAME
        ]
        self.assertEqual([item["stable_utf16"] for item in partial_metadata[:2]], [0, 0])

    def test_stop_before_worker_skips_microphone_and_returns_idle(self):
        fake = FakeService(snapshot=SERVICE.EMPTY_HOTWORD_SNAPSHOT)
        fake.phase = "finalizing"
        fake.session_id = "short-session"
        fake.stop_event.set()
        stream = mock.Mock()
        with mock.patch.object(SERVICE, "open_first_available_input") as opener:
            SERVICE.VoiceService.record_worker(
                fake,
                "short-session",
                mock.Mock(),
                stream,
            )
        opener.assert_not_called()
        self.assertEqual(fake.posts[-1], (SERVICE.FINAL_NAME, "short-session", "", ""))
        self.assertEqual(fake.phase, "idle")

    def test_blank_normalization_does_not_construct_itn(self):
        with (
            mock.patch.object(SERVICE, "_itn", None),
            mock.patch.object(SERVICE, "InverseNormalizer") as constructor,
        ):
            self.assertEqual(SERVICE.normalize("  \t\n"), "")
        constructor.assert_not_called()

    def test_streaming_normalization_runs_explicit_personal_phonetic_repair(self):
        passthrough_itn = mock.Mock()
        passthrough_itn.normalize_with_mapping.side_effect = (
            lambda _text: types.SimpleNamespace(mappings=())
        )
        with (
            mock.patch.object(SERVICE, "_itn", passthrough_itn),
            mock.patch.object(
                SERVICE,
                "apply_personal_phonetic_corrections",
                return_value="我是一个紧凶型的牌手",
            ) as repair,
        ):
            result = SERVICE.normalize("我是一个紧胸型的牌手")

        self.assertEqual(result, "我是一个紧凶型的牌手")
        repair.assert_called_once_with("我是1个紧胸型的牌手")

    def test_microphone_timeout_posts_error_and_requests_process_recovery(self):
        fake = FakeService(snapshot=SERVICE.EMPTY_HOTWORD_SNAPSHOT)
        fake.phase = "recording"
        fake.session_id = "stuck-session"
        attempt = SERVICE.MicrophoneOpenAttempt()

        timed_out = SERVICE.VoiceService.handle_microphone_open_timeout(
            fake,
            "stuck-session",
            attempt,
        )

        self.assertTrue(timed_out)
        self.assertEqual(fake.posts[-1][0], SERVICE.ERROR_NAME)
        self.assertTrue(fake.terminated_for_recovery)

    def test_watchdog_rechecks_ready_after_waiting_for_service_lock(self):
        fake = FakeService(snapshot=SERVICE.EMPTY_HOTWORD_SNAPSHOT)
        fake.phase = "recording"
        fake.session_id = "racing-session"
        attempt = SERVICE.MicrophoneOpenAttempt()

        class RacingLock:
            def __enter__(self):
                attempt.settled.set()

            def __exit__(self, *_args):
                return False

        fake.lock = RacingLock()
        timed_out = SERVICE.VoiceService.handle_microphone_open_timeout(
            fake,
            "racing-session",
            attempt,
        )

        self.assertFalse(timed_out)
        self.assertFalse(fake.terminated_for_recovery)
        self.assertFalse(fake.posts)


class NormalizationConcurrencyTests(unittest.TestCase):
    def test_shadow_and_streaming_normalization_are_serialized(self):
        first_entered = threading.Event()
        release_first = threading.Event()
        second_entered = threading.Event()
        results = []

        def normalize_nonempty(text, *, apply_personal_phonetics):
            if text == "first":
                first_entered.set()
                release_first.wait(1)
            else:
                second_entered.set()
            return text

        with mock.patch.object(
            SERVICE,
            "_normalize_nonempty",
            side_effect=normalize_nonempty,
        ):
            first = threading.Thread(
                target=lambda: results.append(SERVICE.normalize("first")),
            )
            second = threading.Thread(
                target=lambda: results.append(SERVICE.normalize("second")),
            )
            first.start()
            self.assertTrue(first_entered.wait(0.2))
            second.start()
            self.assertFalse(second_entered.wait(0.02))
            release_first.set()
            first.join(0.2)
            second.join(0.2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertTrue(second_entered.is_set())
        self.assertCountEqual(results, ["first", "second"])

if __name__ == "__main__":
    unittest.main()
