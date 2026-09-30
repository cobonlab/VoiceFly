"""Explicit, one-clip recorder for the VoiceFly semantic-refiner dataset.

Importing this module has no microphone side effects.  A microphone is opened
only when :func:`capture_one` is called without an injected ``audio_source`` or
when the command-line entry point is run.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import io
import math
import os
from pathlib import Path
import secrets
import stat
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator
import wave

import numpy as np


SAMPLE_RATE = 16_000
CHANNELS = 1
SAMPLE_WIDTH_BYTES = 2
MIN_DURATION_SECONDS = 1.0
MAX_DURATION_SECONDS = 12.0
DATASET_HARD_LIMIT_BYTES = 16 * 1024 * 1024
ALLOWED_CLIP_IDS = frozenset(
    f"{group}{number:02d}"
    for group in ("C", "V", "H")
    for number in range(1, 9)
)


class CaptureError(RuntimeError):
    """A safe, user-facing capture failure."""


@dataclass(frozen=True, repr=False)
class AudioCapture:
    """Normalized mono samples returned by an audio source."""

    samples: Any
    sample_rate: int = SAMPLE_RATE
    device_name: str = "injected-audio-source"

    def __repr__(self) -> str:
        try:
            frames = int(np.asarray(self.samples).size)
        except Exception:
            frames = 0
        return (
            f"AudioCapture(frames={frames}, sample_rate={self.sample_rate!r}, "
            f"device_name={self.device_name!r})"
        )


AudioSource = Callable[[int, int], AudioCapture | np.ndarray]


@dataclass(frozen=True)
class _PreparedOutputRoot:
    path: Path
    device: int
    inode: int


def _validate_clip_id(clip_id: Any) -> str:
    if not isinstance(clip_id, str) or clip_id not in ALLOWED_CLIP_IDS:
        raise CaptureError("clip ID must be exactly C01..C08, V01..V08, or H01..H08")
    return clip_id


def _validate_duration(duration_seconds: Any) -> tuple[float, int]:
    if isinstance(duration_seconds, bool):
        raise CaptureError("duration must be a finite number from 1 to 12 seconds")
    try:
        duration = float(duration_seconds)
    except (TypeError, ValueError) as exc:
        raise CaptureError(
            "duration must be a finite number from 1 to 12 seconds"
        ) from exc
    if (
        not math.isfinite(duration)
        or duration < MIN_DURATION_SECONDS
        or duration > MAX_DURATION_SECONDS
    ):
        raise CaptureError("duration must be a finite number from 1 to 12 seconds")
    frames = int(round(duration * SAMPLE_RATE))
    if not SAMPLE_RATE <= frames <= int(MAX_DURATION_SECONDS * SAMPLE_RATE):
        raise CaptureError("duration produces an invalid frame count")
    return duration, frames


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def _validate_existing_root(root: Path) -> _PreparedOutputRoot:
    try:
        metadata = root.lstat()
    except OSError as exc:
        raise CaptureError(f"cannot inspect output root: {type(exc).__name__}") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise CaptureError("output root must not be a symbolic link")
    if not stat.S_ISDIR(metadata.st_mode):
        raise CaptureError("output root must be a directory")
    if metadata.st_uid != os.geteuid():
        raise CaptureError("output root must be owned by the current user")
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        raise CaptureError("output root permissions must be exactly 0700")
    try:
        resolved = root.resolve(strict=True)
        resolved_metadata = os.stat(resolved, follow_symlinks=False)
    except (OSError, RuntimeError) as exc:
        raise CaptureError("cannot resolve output root") from exc
    if (metadata.st_dev, metadata.st_ino) != (
        resolved_metadata.st_dev,
        resolved_metadata.st_ino,
    ):
        raise CaptureError("output root changed while it was being validated")
    return _PreparedOutputRoot(
        path=resolved,
        device=resolved_metadata.st_dev,
        inode=resolved_metadata.st_ino,
    )


def _prepare_output_root(output_root: str | os.PathLike[str]) -> _PreparedOutputRoot:
    try:
        supplied = Path(output_root)
    except (TypeError, ValueError) as exc:
        raise CaptureError("output root is invalid") from exc
    if not supplied.is_absolute():
        raise CaptureError("output root must be an absolute path")
    if ".." in supplied.parts:
        raise CaptureError("output root must not contain parent traversal")

    try:
        supplied.lstat()
    except FileNotFoundError:
        try:
            parent = supplied.parent.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise CaptureError("output root parent must already exist") from exc
        if not parent.is_dir():
            raise CaptureError("output root parent must be a directory")
        candidate = parent / supplied.name
        try:
            candidate.mkdir(mode=0o700)
            os.chmod(candidate, 0o700, follow_symlinks=False)
        except FileExistsError:
            # Another process may have created it.  Validate instead of using it
            # blindly, especially if the new entry is a symlink.
            pass
        except OSError as exc:
            raise CaptureError(f"cannot create output root: {type(exc).__name__}") from exc
        return _validate_existing_root(candidate)
    except OSError as exc:
        raise CaptureError(f"cannot inspect output root: {type(exc).__name__}") from exc
    return _validate_existing_root(supplied)


@contextmanager
def _open_output_root(
    output_root: str | os.PathLike[str],
) -> Iterator[tuple[Path, int]]:
    """Validate and bind the output directory to one stable descriptor."""
    prepared = _prepare_output_root(output_root)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = -1
    try:
        descriptor = os.open(prepared.path, flags)
        metadata = os.fstat(descriptor)
        if (metadata.st_dev, metadata.st_ino) != (prepared.device, prepared.inode):
            raise CaptureError("output root changed before it could be opened")
        if not stat.S_ISDIR(metadata.st_mode):
            raise CaptureError("output root must be a directory")
        if metadata.st_uid != os.geteuid():
            raise CaptureError("output root must be owned by the current user")
        if stat.S_IMODE(metadata.st_mode) != 0o700:
            raise CaptureError("output root permissions must be exactly 0700")
        yield prepared.path, descriptor
    except CaptureError:
        raise
    except OSError as exc:
        raise CaptureError(
            f"cannot securely open output root: {type(exc).__name__}"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _directory_usage_bytes(root_descriptor: int) -> int:
    """Count regular files recursively beneath an already-bound directory."""
    total = 0
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    pending: list[int] = []
    try:
        try:
            pending.append(os.dup(root_descriptor))
        except OSError as exc:
            raise CaptureError(
                f"cannot inspect dataset directory: {type(exc).__name__}"
            ) from exc

        while pending:
            directory_descriptor = pending.pop()
            try:
                try:
                    with os.scandir(directory_descriptor) as entries:
                        for entry in entries:
                            try:
                                metadata = os.stat(
                                    entry.name,
                                    dir_fd=directory_descriptor,
                                    follow_symlinks=False,
                                )
                            except OSError as exc:
                                raise CaptureError(
                                    "cannot inspect dataset entry: "
                                    f"{type(exc).__name__}"
                                ) from exc
                            if stat.S_ISLNK(metadata.st_mode):
                                raise CaptureError(
                                    "dataset directory must not contain symbolic links"
                            )
                            if stat.S_ISDIR(metadata.st_mode):
                                child_descriptor = -1
                                try:
                                    child_descriptor = os.open(
                                        entry.name,
                                        directory_flags,
                                        dir_fd=directory_descriptor,
                                    )
                                    child_metadata = os.fstat(child_descriptor)
                                except OSError as exc:
                                    if child_descriptor >= 0:
                                        os.close(child_descriptor)
                                    raise CaptureError(
                                        "cannot inspect dataset directory: "
                                        f"{type(exc).__name__}"
                                    ) from exc
                                if (
                                    not stat.S_ISDIR(child_metadata.st_mode)
                                    or (child_metadata.st_dev, child_metadata.st_ino)
                                    != (metadata.st_dev, metadata.st_ino)
                                ):
                                    os.close(child_descriptor)
                                    raise CaptureError(
                                        "dataset directory changed while being inspected"
                                    )
                                pending.append(child_descriptor)
                            elif stat.S_ISREG(metadata.st_mode):
                                total += metadata.st_size
                            else:
                                raise CaptureError(
                                    "dataset directory contains a non-regular entry"
                                )
                            if total > DATASET_HARD_LIMIT_BYTES:
                                return total
                except CaptureError:
                    raise
                except OSError as exc:
                    raise CaptureError(
                        f"cannot inspect dataset directory: {type(exc).__name__}"
                    ) from exc
            finally:
                os.close(directory_descriptor)
        return total
    finally:
        for descriptor in pending:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _target_path(root: Path, clip_id: str) -> Path:
    target = root / f"{clip_id}.wav"
    if target.parent != root or target.name != f"{clip_id}.wav":
        raise CaptureError("output path escapes the dataset directory")
    return target


def _require_target_missing(root_descriptor: int, target_name: str) -> None:
    try:
        os.stat(target_name, dir_fd=root_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise CaptureError(f"cannot inspect output target: {type(exc).__name__}") from exc
    raise CaptureError(f"{target_name} already exists; refusing to overwrite it")


def _normalize_source_result(
    result: AudioCapture | np.ndarray,
    *,
    expected_frames: int,
) -> np.ndarray:
    capture = result if isinstance(result, AudioCapture) else AudioCapture(result)
    if isinstance(capture.sample_rate, bool) or capture.sample_rate != SAMPLE_RATE:
        raise CaptureError("audio source must return exactly 16000 Hz samples")
    try:
        source = np.asarray(capture.samples)
    except Exception as exc:
        raise CaptureError("audio source returned invalid samples") from exc
    if source.ndim == 2 and source.shape[1:] == (1,):
        source = source[:, 0]
    if source.ndim != 1:
        raise CaptureError("audio source must return mono samples")
    if source.size != expected_frames:
        raise CaptureError("audio source returned an unexpected frame count")
    if source.size > int(MAX_DURATION_SECONDS * SAMPLE_RATE):
        raise CaptureError("audio source exceeded the single-clip memory bound")
    try:
        samples = np.array(source, dtype=np.float32, copy=True)
    except (TypeError, ValueError, MemoryError) as exc:
        raise CaptureError("audio source returned invalid samples") from exc
    if not np.isfinite(samples).all():
        raise CaptureError("audio source returned non-finite samples")
    if np.any(samples < -1.0) or np.any(samples > 1.0):
        raise CaptureError("audio source samples must be normalized to [-1, 1]")
    return samples


def _wav_bytes(samples: np.ndarray) -> bytes:
    scaled = np.rint(samples.astype(np.float64) * 32767.0)
    scaled[samples <= -1.0] = -32768
    pcm16 = scaled.astype("<i2", copy=False)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(CHANNELS)
        output.setsampwidth(SAMPLE_WIDTH_BYTES)
        output.setframerate(SAMPLE_RATE)
        output.writeframes(pcm16.tobytes())
    payload = buffer.getvalue()
    if len(payload) > DATASET_HARD_LIMIT_BYTES:
        raise CaptureError("one WAV exceeds the dataset hard limit")
    return payload


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short write while saving WAV")
        view = view[written:]


def _same_file(first: os.stat_result, second: os.stat_result) -> bool:
    return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


def _unlink_if_same(
    root_descriptor: int,
    name: str,
    expected: os.stat_result,
) -> None:
    """Remove only the directory entry that still names ``expected``."""
    try:
        current = os.stat(name, dir_fd=root_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError:
        return
    if not _same_file(current, expected):
        return
    try:
        os.unlink(name, dir_fd=root_descriptor)
    except OSError:
        pass


def _create_temporary_wav(root_descriptor: int, clip_id: str) -> tuple[int, str]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    for _ in range(128):
        name = f".{clip_id}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
        try:
            return os.open(name, flags, 0o600, dir_fd=root_descriptor), name
        except FileExistsError:
            continue
        except OSError as exc:
            raise CaptureError(
                f"cannot create temporary WAV: {type(exc).__name__}"
            ) from exc
    raise CaptureError("cannot allocate a unique temporary WAV")


def _atomic_publish(root_descriptor: int, target_name: str, payload: bytes) -> None:
    descriptor = -1
    temporary_name: str | None = None
    temporary_metadata: os.stat_result | None = None
    published_metadata: os.stat_result | None = None
    locked = False
    try:
        fcntl.flock(root_descriptor, fcntl.LOCK_EX)
        locked = True
        root_metadata = os.fstat(root_descriptor)
        if not stat.S_ISDIR(root_metadata.st_mode):
            raise CaptureError("output root changed before publish")
        if root_metadata.st_uid != os.geteuid() or stat.S_IMODE(root_metadata.st_mode) != 0o700:
            raise CaptureError("output root ownership or permissions changed")
        _require_target_missing(root_descriptor, target_name)

        descriptor, temporary_name = _create_temporary_wav(
            root_descriptor,
            target_name.removesuffix(".wav"),
        )
        temporary_metadata = os.fstat(descriptor)
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        temporary_metadata = os.fstat(descriptor)

        # The completed temporary WAV is inside root and therefore participates
        # in the same 16 MiB accounting as every existing dataset artifact.
        if _directory_usage_bytes(root_descriptor) > DATASET_HARD_LIMIT_BYTES:
            raise CaptureError("dataset directory would exceed the 16 MiB hard limit")
        try:
            # link() is an atomic no-replace publish on the same filesystem.
            # Unlike os.replace(), it cannot overwrite a file created in a race.
            os.link(
                temporary_name,
                target_name,
                src_dir_fd=root_descriptor,
                dst_dir_fd=root_descriptor,
                follow_symlinks=False,
            )
        except FileExistsError as exc:
            raise CaptureError(
                f"{target_name} already exists; refusing to overwrite it"
            ) from exc
        published_metadata = temporary_metadata
        current_published_metadata = os.stat(
            target_name,
            dir_fd=root_descriptor,
            follow_symlinks=False,
        )
        if not _same_file(temporary_metadata, current_published_metadata):
            raise CaptureError("published WAV identity changed unexpectedly")
        published_metadata = current_published_metadata
        os.unlink(temporary_name, dir_fd=root_descriptor)
        temporary_name = None
        os.fsync(root_descriptor)
    except CaptureError:
        if published_metadata is not None:
            _unlink_if_same(root_descriptor, target_name, published_metadata)
        raise
    except OSError as exc:
        if published_metadata is not None:
            _unlink_if_same(root_descriptor, target_name, published_metadata)
        raise CaptureError(f"cannot save WAV atomically: {type(exc).__name__}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_name is not None and temporary_metadata is not None:
            _unlink_if_same(root_descriptor, temporary_name, temporary_metadata)
        if locked:
            try:
                fcntl.flock(root_descriptor, fcntl.LOCK_UN)
            except OSError:
                pass


def _sounddevice_audio_source(
    frame_count: int,
    sample_rate: int,
    *,
    countdown_seconds: int = 3,
    emit: Callable[[str], None] = print,
    sounddevice_module: Any | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> AudioCapture:
    """Open one input lazily, announce it, then record exactly one clip."""
    try:
        if sounddevice_module is None:
            import sounddevice as sounddevice_module
        from voice_audio import input_device_candidates

        devices = list(sounddevice_module.query_devices())
        default_device = sounddevice_module.default.device
        candidates = input_device_candidates(devices, default_device)
    except Exception as exc:
        raise CaptureError(
            f"无法初始化麦克风设备：{type(exc).__name__}"
        ) from exc
    if not candidates:
        raise CaptureError("系统没有可用的麦克风输入设备")

    failures: list[str] = []
    for candidate in candidates:
        stream = None
        try:
            stream = sounddevice_module.InputStream(
                device=candidate.index,
                channels=CHANNELS,
                dtype="float32",
                samplerate=sample_rate,
                blocksize=0,
            )
            emit(
                f"已选输入设备：{candidate.name} "
                f"(device={candidate.index}, {sample_rate}Hz mono)"
            )
            for remaining in range(int(countdown_seconds), 0, -1):
                emit(f"倒计时 {remaining}…")
                sleep(1.0)
            stream.start()
            emit("● 开始录音，请现在朗读")
            samples, overflowed = stream.read(frame_count)
            if overflowed:
                raise CaptureError("录音输入溢出，本条未保存")
            emit("■ 录音结束")
            return AudioCapture(
                samples=samples,
                sample_rate=sample_rate,
                device_name=candidate.name,
            )
        except CaptureError:
            raise
        except Exception as exc:
            failures.append(f"device-{candidate.index}: {type(exc).__name__}")
        finally:
            if stream is not None:
                try:
                    stream.stop()
                except Exception:
                    pass
                try:
                    stream.close()
                except Exception:
                    pass
    raise CaptureError("所有麦克风均无法以 16000Hz mono 录音；" + "，".join(failures))


def capture_one(
    clip_id: str,
    output_root: str | os.PathLike[str],
    duration_seconds: float,
    *,
    audio_source: AudioSource | None = None,
    countdown_seconds: int = 3,
    emit: Callable[[str], None] = print,
) -> Path:
    """Capture and atomically publish exactly one fixed-ID PCM16 WAV."""
    canonical_id = _validate_clip_id(clip_id)
    duration, frame_count = _validate_duration(duration_seconds)
    if isinstance(countdown_seconds, bool) or not isinstance(countdown_seconds, int):
        raise CaptureError("countdown must be an integer from 0 to 10")
    if not 0 <= countdown_seconds <= 10:
        raise CaptureError("countdown must be an integer from 0 to 10")

    with _open_output_root(output_root) as (root, root_descriptor):
        target = _target_path(root, canonical_id)
        _require_target_missing(root_descriptor, target.name)
        estimated_wav_bytes = 44 + frame_count * SAMPLE_WIDTH_BYTES
        if (
            _directory_usage_bytes(root_descriptor) + estimated_wav_bytes
            > DATASET_HARD_LIMIT_BYTES
        ):
            raise CaptureError("dataset directory would exceed the 16 MiB hard limit")

        emit(
            f"即将录制单条样本 {canonical_id}：{duration:g} 秒，"
            "16000Hz mono PCM16。"
        )
        if audio_source is None:
            result = _sounddevice_audio_source(
                frame_count,
                SAMPLE_RATE,
                countdown_seconds=countdown_seconds,
                emit=emit,
            )
        else:
            result = audio_source(frame_count, SAMPLE_RATE)
        samples = _normalize_source_result(result, expected_frames=frame_count)
        payload = _wav_bytes(samples)
        _atomic_publish(root_descriptor, target.name, payload)
        digest = hashlib.sha256(payload).hexdigest()
        emit(
            f"已安全保存 {target.name}；sha256={digest}；"
            "未播放、未上传、未写入真值 manifest。"
        )
        return target


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Record exactly one local VoiceFly semantic-refiner clip."
    )
    parser.add_argument("clip_id", choices=sorted(ALLOWED_CLIP_IDS))
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--duration", required=True, type=float)
    parser.add_argument("--countdown", type=int, default=3)
    return parser


def main(
    argv: list[str] | None = None,
    *,
    audio_source: AudioSource | None = None,
    emit: Callable[[str], None] = print,
) -> int:
    arguments = _parser().parse_args(argv)
    try:
        capture_one(
            arguments.clip_id,
            arguments.output_root,
            arguments.duration,
            audio_source=audio_source,
            countdown_seconds=arguments.countdown,
            emit=emit,
        )
    except CaptureError as exc:
        print(f"录音失败：{exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
