"""CoreAudio input selection and speech-onset protection for VoiceFly."""

from collections import deque
from dataclasses import dataclass
import math

import numpy as np


# Keep device reads short so releasing the push-to-talk key cannot strand the
# final phoneme in CoreAudio's input buffer.  This is an application read size;
# PortAudio still chooses the native hardware callback quantum.
FRAME_DURATION_SECONDS = 0.04
BUILTIN_MIC_MARKERS = ("macbook", "built-in", "builtin", "内建", "内置")

SPEECH_GATE_ANALYSIS_SECONDS = 0.02
SPEECH_GATE_SETTLE_SECONDS = 0.04
SPEECH_GATE_CALIBRATION_SECONDS = 0.12
SPEECH_GATE_CONFIRMATION_SECONDS = 0.06
SPEECH_GATE_PREROLL_SECONDS = 0.04
SPEECH_GATE_MIN_SNR_DB = 8.0
SPEECH_GATE_MIN_LEVEL_DBFS = -55.0
# During the first calibration window there may be no clean room-noise sample:
# Bluetooth HFP devices often finish switching profiles only after the user has
# already started speaking.  A sustained speech-shaped signal down to this
# level must therefore bypass the relative SNR test.  Confirmation across
# three 20 ms frames still rejects a one-frame profile-switch impulse.
SPEECH_GATE_CLEAR_SPEECH_DBFS = -58.0
SPEECH_GATE_LIVE_SIGNAL_DBFS = -90.0


@dataclass(frozen=True)
class InputDeviceCandidate:
    index: int
    name: str
    sample_rate: int
    read_frames: int
    is_default: bool
    is_builtin: bool


@dataclass
class ActiveInput:
    stream: object
    candidate: InputDeviceCandidate
    selection_reason: str

    def close(self):
        try:
            self.stream.stop()
        except Exception:
            pass
        try:
            self.stream.close()
        except Exception:
            pass


class AdaptiveSpeechStartGate:
    """Hold audio until a sustained speech onset is confirmed.

    The gate is deliberately session-local and adaptive.  It learns the noise
    floor of the microphone that CoreAudio selected for this recording instead
    of applying one global volume threshold to built-in and Bluetooth devices.
    A short startup-settle window rejects profile-switch/AGC impulses, while a
    small pre-roll preserves the leading edge of the first real syllable.

    Once opened, the gate remains open for the rest of the push-to-talk
    session.  Endpointing and ordinary pauses therefore remain the ASR
    decoder's responsibility and cannot chop a sentence in the middle.
    """

    def __init__(
        self,
        sample_rate,
        *,
        analysis_seconds=SPEECH_GATE_ANALYSIS_SECONDS,
        settle_seconds=SPEECH_GATE_SETTLE_SECONDS,
        calibration_seconds=SPEECH_GATE_CALIBRATION_SECONDS,
        confirmation_seconds=SPEECH_GATE_CONFIRMATION_SECONDS,
        preroll_seconds=SPEECH_GATE_PREROLL_SECONDS,
        min_snr_db=SPEECH_GATE_MIN_SNR_DB,
        min_level_dbfs=SPEECH_GATE_MIN_LEVEL_DBFS,
        clear_speech_dbfs=SPEECH_GATE_CLEAR_SPEECH_DBFS,
        live_signal_dbfs=SPEECH_GATE_LIVE_SIGNAL_DBFS,
    ):
        rate = int(round(float(sample_rate)))
        if rate <= 0:
            raise ValueError("sample_rate must be positive")
        if analysis_seconds <= 0:
            raise ValueError("analysis_seconds must be positive")
        self.sample_rate = rate
        self.frame_samples = max(1, int(round(rate * analysis_seconds)))
        self.settle_frames = max(1, int(math.ceil(settle_seconds / analysis_seconds)))
        self.calibration_frames = max(
            1,
            int(math.ceil(calibration_seconds / analysis_seconds)),
        )
        self.confirmation_frames = max(
            1,
            int(math.ceil(confirmation_seconds / analysis_seconds)),
        )
        self.preroll_frames = max(
            0,
            int(math.ceil(preroll_seconds / analysis_seconds)),
        )
        self.min_snr_db = float(min_snr_db)
        self.min_level_dbfs = float(min_level_dbfs)
        self.clear_speech_dbfs = float(clear_speech_dbfs)
        self.live_signal_dbfs = float(live_signal_dbfs)

        self.opened = False
        self.calibrated = False
        self.noise_floor_dbfs = None
        self.trigger_level_dbfs = None
        self.peak_level_dbfs = -160.0
        self.speech_like_frames = 0
        self.open_after_ms = None
        self._pending = np.empty(0, dtype=np.float32)
        self._startup_frames = []
        self._startup_levels = []
        self._preroll = deque(maxlen=self.preroll_frames or 1)
        self._candidate_audio = []
        self._candidate_count = 0
        self._observed_samples = 0
        self._discarded_samples = 0
        self._signal_ready = False

    @staticmethod
    def _level_dbfs(frame):
        finite = np.nan_to_num(
            np.asarray(frame, dtype=np.float32),
            copy=False,
            nan=0.0,
            posinf=1.0,
            neginf=-1.0,
        )
        rms = float(np.sqrt(np.mean(np.square(finite, dtype=np.float64))))
        return 20.0 * math.log10(max(rms, 1e-8))

    @property
    def suppressed_ms(self):
        samples = self._discarded_samples if self.opened else self._observed_samples
        return samples * 1000.0 / self.sample_rate

    @property
    def policy(self):
        return (
            f"adaptive-snr/{self.min_snr_db:.1f}dB "
            f"signal-ready={self.live_signal_dbfs:.0f}dBFS "
            f"startup-clear={self.clear_speech_dbfs:.0f}dBFS "
            f"settle={self.settle_frames * self.frame_samples * 1000 / self.sample_rate:.0f}ms "
            f"calibrate={self.calibration_frames * self.frame_samples * 1000 / self.sample_rate:.0f}ms "
            f"confirm={self.confirmation_frames * self.frame_samples * 1000 / self.sample_rate:.0f}ms"
        )

    @staticmethod
    def _looks_speech_like(frame):
        centered = np.asarray(frame, dtype=np.float32) - float(np.mean(frame))
        if float(np.sqrt(np.mean(np.square(centered, dtype=np.float64)))) < 1e-6:
            return False
        signs = centered >= 0
        zero_crossing_rate = float(np.mean(signs[1:] != signs[:-1]))
        return 0.01 <= zero_crossing_rate <= 0.45

    def _is_speech(self, frame, level_dbfs, *, allow_clear_speech=False):
        threshold = max(
            self.min_level_dbfs,
            float(self.noise_floor_dbfs) + self.min_snr_db,
        )
        looks_speech_like = self._looks_speech_like(frame)
        return looks_speech_like and (
            level_dbfs >= threshold
            or (allow_clear_speech and level_dbfs >= self.clear_speech_dbfs)
        )

    def _adapt_noise_floor(self, level_dbfs):
        # Only non-speech frames reach this method.  Follow a quieter room
        # quickly and a rising steady background slowly, without learning a
        # spoken onset into the baseline.
        alpha = 0.85 if level_dbfs < self.noise_floor_dbfs else 0.98
        self.noise_floor_dbfs = (
            alpha * float(self.noise_floor_dbfs) + (1.0 - alpha) * level_dbfs
        )

    def _observe_calibrated_frame(
        self,
        frame,
        level_dbfs,
        *,
        allow_clear_speech=False,
    ):
        if self._is_speech(
            frame,
            level_dbfs,
            allow_clear_speech=allow_clear_speech,
        ):
            if self._candidate_count == 0:
                self._candidate_audio = list(self._preroll)
            self._candidate_audio.append(frame)
            self._candidate_count += 1
            if self._candidate_count >= self.confirmation_frames:
                self.opened = True
                self.trigger_level_dbfs = level_dbfs
                self.open_after_ms = self._observed_samples * 1000.0 / self.sample_rate
                emitted = self._candidate_audio
                emitted_samples = sum(chunk.size for chunk in emitted)
                self._discarded_samples = max(
                    0,
                    self._observed_samples - emitted_samples,
                )
                self._candidate_audio = []
                self._preroll.clear()
                return emitted
            return None

        self._candidate_count = 0
        self._candidate_audio = []
        if self.preroll_frames:
            self._preroll.append(frame)
        self._adapt_noise_floor(level_dbfs)
        return None

    def _finish_calibration(self):
        calibration_levels = self._startup_levels[self.settle_frames :]
        calibration_audio = self._startup_frames[self.settle_frames :]
        # A low percentile is robust to a user beginning near the end of the
        # calibration window.  The absolute clear-speech escape hatch handles
        # a user who starts immediately and fills the whole window.
        self.noise_floor_dbfs = float(np.percentile(calibration_levels, 20.0))
        self.calibrated = True
        emitted = None
        for index, (frame, level_dbfs) in enumerate(
            zip(calibration_audio, calibration_levels)
        ):
            emitted = self._observe_calibrated_frame(
                frame,
                level_dbfs,
                allow_clear_speech=True,
            )
            if emitted is not None:
                # Calibration is replayed only after the complete startup
                # window has arrived.  Preserve the later frames from that
                # same window when onset was found near its beginning.
                emitted = [*emitted, *calibration_audio[index + 1 :]]
                break
        self._startup_frames = []
        self._startup_levels = []
        return emitted

    def process(self, samples):
        """Return accepted mono float32 audio, or ``None`` before speech."""
        audio = np.nan_to_num(
            np.asarray(samples, dtype=np.float32).reshape(-1),
            copy=True,
            nan=0.0,
            posinf=1.0,
            neginf=-1.0,
        )
        if audio.size == 0:
            return None
        if self.opened:
            return audio

        if self._pending.size:
            audio = np.concatenate((self._pending, audio))
        full_count = audio.size // self.frame_samples
        if full_count == 0:
            self._pending = audio
            return None

        consumed = full_count * self.frame_samples
        frames = [
            audio[offset : offset + self.frame_samples].copy()
            for offset in range(0, consumed, self.frame_samples)
        ]
        remainder = audio[consumed:].copy()
        self._pending = remainder

        startup_total = self.settle_frames + self.calibration_frames
        for index, frame in enumerate(frames):
            self._observed_samples += frame.size
            level_dbfs = self._level_dbfs(frame)
            emitted = None
            if not self._signal_ready:
                # Bluetooth microphones commonly emit exact digital zero while
                # CoreAudio switches profiles.  That is not a usable room-noise
                # measurement: calibrating against it makes the later, normal
                # microphone hiss look like speech.  Start the settle/calibrate
                # clock only after the capture path produces a live signal.
                if level_dbfs < self.live_signal_dbfs:
                    continue
                self._signal_ready = True
            self.peak_level_dbfs = max(self.peak_level_dbfs, level_dbfs)
            if self._looks_speech_like(frame):
                self.speech_like_frames += 1
            if not self.calibrated:
                self._startup_frames.append(frame)
                self._startup_levels.append(level_dbfs)
                if len(self._startup_frames) >= startup_total:
                    emitted = self._finish_calibration()
            else:
                emitted = self._observe_calibrated_frame(frame, level_dbfs)

            if emitted is not None:
                trailing = frames[index + 1 :]
                parts = [*emitted, *trailing]
                if remainder.size:
                    parts.append(remainder)
                self._pending = np.empty(0, dtype=np.float32)
                return np.concatenate(parts).astype(np.float32, copy=False)
        return None


def _default_input_index(default_device):
    if isinstance(default_device, (tuple, list)):
        default_device = default_device[0] if default_device else -1
    elif not isinstance(default_device, (str, bytes)):
        try:
            default_device = default_device[0]
        except (IndexError, KeyError, TypeError):
            pass
    try:
        index = int(default_device)
    except (TypeError, ValueError):
        return -1
    return index if index >= 0 else -1


def input_device_candidates(devices, default_device, *, prefer_builtin=False):
    """Return usable inputs in default, built-in, then remaining order."""
    default_index = _default_input_index(default_device)
    candidates = []
    for index, device in enumerate(devices):
        try:
            channels = int(device.get("max_input_channels", 0))
            sample_rate = float(device.get("default_samplerate", 0))
        except (AttributeError, TypeError, ValueError):
            continue
        if channels < 1 or not math.isfinite(sample_rate) or sample_rate <= 0:
            continue
        name = str(device.get("name", f"input-{index}"))
        rate = int(round(sample_rate))
        lowered = name.casefold()
        candidates.append(
            InputDeviceCandidate(
                index=index,
                name=name,
                sample_rate=rate,
                read_frames=max(1, int(round(rate * FRAME_DURATION_SECONDS))),
                is_default=index == default_index,
                is_builtin=any(marker in lowered for marker in BUILTIN_MIC_MARKERS),
            )
        )

    def priority(candidate):
        if prefer_builtin and candidate.is_builtin:
            return (0, candidate.index)
        if candidate.is_default:
            return (0 if not prefer_builtin else 1, candidate.index)
        if candidate.is_builtin:
            return (1, candidate.index)
        return (2, candidate.index)

    return sorted(candidates, key=priority)


def refresh_device_registry(sounddevice_module):
    """Refresh PortAudio before resolving the current macOS default input.

    CoreAudio changes its default device when a Bluetooth headset connects or
    disconnects.  PortAudio snapshots that default when it initializes, so a
    long-running service can otherwise keep opening the microphone that was
    selected days earlier.  sounddevice exposes these lifecycle hooks for its
    own cleanup; refresh only while no stream is open, immediately before a
    new recording session.

    Test doubles and alternate sounddevice-compatible modules may not expose
    the hooks.  In that case their query API remains the source of truth.
    """
    terminate = getattr(sounddevice_module, "_terminate", None)
    initialize = getattr(sounddevice_module, "_initialize", None)
    if not callable(terminate) or not callable(initialize):
        return False
    terminate()
    initialize()
    return True


def open_first_available_input(sounddevice_module, *, prefer_builtin=False):
    """Open and start the first viable input at its native sample rate."""
    refresh_device_registry(sounddevice_module)
    devices = list(sounddevice_module.query_devices())
    default_device = sounddevice_module.default.device
    candidates = input_device_candidates(
        devices,
        default_device,
        prefer_builtin=prefer_builtin,
    )
    if not candidates:
        raise RuntimeError("系统没有可用的麦克风输入设备")

    failures = []
    for candidate in candidates:
        stream = None
        try:
            stream = sounddevice_module.InputStream(
                device=candidate.index,
                channels=1,
                dtype="float32",
                samplerate=candidate.sample_rate,
                # Let CoreAudio/PortAudio choose its native callback quantum.
                # The 40 ms application read size is intentionally separate:
                # forcing it as the hardware block size causes continuous
                # paInputOverflowed results on Bluetooth headset microphones.
                blocksize=0,
            )
            stream.start()
            if prefer_builtin and candidate.is_builtin:
                reason = "builtin-override"
            elif candidate.is_default:
                reason = "system-default"
            elif candidate.is_builtin:
                reason = "builtin-fallback"
            else:
                reason = "alternate-fallback"
            return ActiveInput(
                stream=stream,
                candidate=candidate,
                selection_reason=reason,
            )
        except Exception as exc:
            failures.append(f"{candidate.name}@{candidate.sample_rate}Hz: {exc}")
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
    raise RuntimeError("所有麦克风均无法打开；" + "；".join(failures))
