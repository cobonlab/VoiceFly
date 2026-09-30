#!/usr/bin/env python3
"""Fail-closed process supervisor for the real metrics-only A/B evaluation.

This wrapper is intentionally separate from ``semantic_refiner_eval.py``.  It
starts the evaluator in a new process group, keeps its two output pipes bounded
in memory, and kills the complete group when a resource or privacy invariant
cannot be proved.  It never writes audio, paths, transcripts, or child output
to disk.
"""

from __future__ import annotations

import ctypes
import errno
import json
import math
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, BinaryIO, Callable, Mapping, Protocol, Sequence, TextIO

from semantic_refiner_protocol_v1 import (
    EXPECTED_REFERENCE_CHARACTERS,
    EXPECTED_SAMPLE_COUNT,
    PROTOCOL_CATEGORY_SAMPLE_NUMBERS,
    PROTOCOL_REFERENCE_CHARACTERS_BY_SAMPLE_NUMBER,
    PROTOCOL_SPLIT_BY_SAMPLE_NUMBER,
    PROTOCOL_VERSION,
    ProtocolValidationError,
    validate_public_report,
)


SCHEMA_VERSION = 1
SUPERVISOR_NAME = "voiceflypy_semantic_refiner_supervisor"
EVALUATION_NAME = "voiceflypy_semantic_refiner_ab"

RSS_LIMIT_BYTES = 5 * 1024**3 // 2
SWAP_GROWTH_LIMIT_BYTES = 512 * 1024**2
OUTPUT_LIMIT_BYTES = 16 * 1024**2
DEFAULT_WALL_TIMEOUT_SECONDS = 20 * 60.0
DEFAULT_POLL_INTERVAL_SECONDS = 0.10
DEFAULT_TERMINATE_GRACE_SECONDS = 1.0
# The dispatch memory-pressure source reports transitions, not an initial
# state.  The synchronous memorystatus headroom check closes that startup gap
# conservatively; subsequent warning/critical dispatch events are still fatal.
MIN_MEMORY_HEADROOM_PERCENT = 20
P95_LATENCY_LIMIT_MS = 6_000.0
SINGLE_CALL_TIMEOUT_SECONDS = 15.0
MAX_AUDIO_SECONDS = 12.0
STABLE_RSS_DRIFT_LIMIT_BYTES = 128 * 1024**2

_SAFE_FAILURE_CODES = frozenset(
    {
        "supervisor_arguments_invalid",
        "transcript_output_forbidden",
        "evaluation_protocol_override_forbidden",
        "controlled_evaluator_argument_forbidden",
        "resource_monitor_unavailable",
        "memory_pressure_not_normal",
        "rss_limit_exceeded",
        "swap_limit_exceeded",
        "output_limit_exceeded",
        "wall_timeout_exceeded",
        "process_spawn_failed",
        "process_group_invalid",
        "process_group_cleanup_failed",
        "descendant_process_survived",
        "child_failed",
        "child_output_invalid",
        "child_report_incomplete",
        "child_report_privacy_invalid",
        "supervisor_interrupted",
    }
)
_SHA256_HEX = frozenset("0123456789abcdef")
_BASELINE_PROVIDER_NAME = "voiceflypy_x_asr"
_CANDIDATE_PROVIDER_NAMES = frozenset(
    {"paraformer", "sense_voice", "qwen3_asr"}
)
# If libdispatch cannot confirm cancellation, retaining the monitor for the
# remaining supervisor lifetime is safer than releasing a live C callback.
_UNCANCELLED_PRESSURE_MONITORS: list[object] = []


class SupervisorError(RuntimeError):
    """Internal fixed-code failure whose message never contains user data."""

    def __init__(self, code: str) -> None:
        if code not in _SAFE_FAILURE_CODES:
            code = "resource_monitor_unavailable"
        super().__init__(code)
        self.code = code


class MonitorUnavailableError(RuntimeError):
    """A fail-closed resource-probe failure."""


@dataclass(frozen=True)
class Limits:
    rss_bytes: int = RSS_LIMIT_BYTES
    swap_growth_bytes: int = SWAP_GROWTH_LIMIT_BYTES
    output_bytes: int = OUTPUT_LIMIT_BYTES
    wall_timeout_seconds: float = DEFAULT_WALL_TIMEOUT_SECONDS
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS
    terminate_grace_seconds: float = DEFAULT_TERMINATE_GRACE_SECONDS
    minimum_headroom_percent: int = MIN_MEMORY_HEADROOM_PERCENT

    def validate(self) -> None:
        integer_values = (
            self.rss_bytes,
            self.swap_growth_bytes,
            self.output_bytes,
            self.minimum_headroom_percent,
        )
        if any(type(value) is not int or value <= 0 for value in integer_values):
            raise SupervisorError("supervisor_arguments_invalid")
        if not 1 <= self.minimum_headroom_percent <= 100:
            raise SupervisorError("supervisor_arguments_invalid")
        for value in (
            self.wall_timeout_seconds,
            self.poll_interval_seconds,
            self.terminate_grace_seconds,
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise SupervisorError("supervisor_arguments_invalid")
            if not math.isfinite(float(value)) or value <= 0:
                raise SupervisorError("supervisor_arguments_invalid")


@dataclass(frozen=True)
class SystemSnapshot:
    swap_used_bytes: int
    pressure: str
    memory_headroom_percent: int


@dataclass(frozen=True)
class ResourceSnapshot(SystemSnapshot):
    group_rss_bytes: int
    group_member_count: int


@dataclass(frozen=True)
class SupervisorResult:
    exit_code: int
    report: Mapping[str, Any]


class ProcessLike(Protocol):
    pid: int
    stdout: BinaryIO | None
    stderr: BinaryIO | None
    returncode: int | None

    def poll(self) -> int | None:
        ...

    def wait(self, timeout: float | None = None) -> int:
        ...

    def kill(self) -> None:
        ...


class ResourceProbe(Protocol):
    def preflight(self) -> SystemSnapshot:
        ...

    def snapshot(self, pgid: int) -> ResourceSnapshot:
        ...

    def group_member_count(self, pgid: int) -> int:
        ...

    def close(self) -> None:
        ...


class _ProcTaskInfo(ctypes.Structure):
    _fields_ = [
        ("pti_virtual_size", ctypes.c_uint64),
        ("pti_resident_size", ctypes.c_uint64),
        ("pti_total_user", ctypes.c_uint64),
        ("pti_total_system", ctypes.c_uint64),
        ("pti_threads_user", ctypes.c_uint64),
        ("pti_threads_system", ctypes.c_uint64),
        ("pti_policy", ctypes.c_int32),
        ("pti_faults", ctypes.c_int32),
        ("pti_pageins", ctypes.c_int32),
        ("pti_cow_faults", ctypes.c_int32),
        ("pti_messages_sent", ctypes.c_int32),
        ("pti_messages_received", ctypes.c_int32),
        ("pti_syscalls_mach", ctypes.c_int32),
        ("pti_syscalls_unix", ctypes.c_int32),
        ("pti_csw", ctypes.c_int32),
        ("pti_threadnum", ctypes.c_int32),
        ("pti_numrunning", ctypes.c_int32),
        ("pti_priority", ctypes.c_int32),
    ]


class _SwapUsage(ctypes.Structure):
    _fields_ = [
        ("xsu_total", ctypes.c_uint64),
        ("xsu_avail", ctypes.c_uint64),
        ("xsu_used", ctypes.c_uint64),
        ("xsu_pagesize", ctypes.c_uint32),
        ("xsu_encrypted", ctypes.c_uint32),
    ]


class _VMStatistics64(ctypes.Structure):
    """Darwin ``vm_statistics64_data_t`` through the swapout fields."""

    _fields_ = [
        ("free_count", ctypes.c_uint32),
        ("active_count", ctypes.c_uint32),
        ("inactive_count", ctypes.c_uint32),
        ("wire_count", ctypes.c_uint32),
        ("zero_fill_count", ctypes.c_uint64),
        ("reactivations", ctypes.c_uint64),
        ("pageins", ctypes.c_uint64),
        ("pageouts", ctypes.c_uint64),
        ("faults", ctypes.c_uint64),
        ("cow_faults", ctypes.c_uint64),
        ("lookups", ctypes.c_uint64),
        ("hits", ctypes.c_uint64),
        ("purges", ctypes.c_uint64),
        ("purgeable_count", ctypes.c_uint32),
        ("speculative_count", ctypes.c_uint32),
        ("decompressions", ctypes.c_uint64),
        ("compressions", ctypes.c_uint64),
        ("swapins", ctypes.c_uint64),
        ("swapouts", ctypes.c_uint64),
        ("compressor_page_count", ctypes.c_uint32),
        ("throttled_count", ctypes.c_uint32),
        ("external_page_count", ctypes.c_uint32),
        ("internal_page_count", ctypes.c_uint32),
        ("total_uncompressed_pages_in_compressor", ctypes.c_uint64),
    ]


class _DispatchMemoryPressureMonitor:
    """Receive public macOS memory-pressure transition notifications."""

    _NORMAL = 0x01
    _WARN = 0x02
    _CRITICAL = 0x04
    _CALLBACK = ctypes.CFUNCTYPE(None, ctypes.c_void_p)

    def __init__(self, *, registration_timeout_seconds: float = 2.0) -> None:
        self._registration_timeout_seconds = registration_timeout_seconds
        self._lock = threading.Lock()
        self._registered = threading.Event()
        self._cancelled = threading.Event()
        self._state = "normal"
        self._failed = False
        self._library: Any = None
        self._queue: int | None = None
        self._source: int | None = None
        self._event_callback: Any = None
        self._registration_callback: Any = None
        self._cancel_callback: Any = None
        self._uncancelled = False

    def start(self) -> None:
        if sys.platform != "darwin":
            raise MonitorUnavailableError("darwin_required")
        try:
            library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
            queue_create = library.dispatch_queue_create
            queue_create.argtypes = [ctypes.c_char_p, ctypes.c_void_p]
            queue_create.restype = ctypes.c_void_p
            source_create = library.dispatch_source_create
            source_create.argtypes = [
                ctypes.c_void_p,
                ctypes.c_size_t,
                ctypes.c_ulong,
                ctypes.c_void_p,
            ]
            source_create.restype = ctypes.c_void_p
            library.dispatch_source_set_event_handler_f.argtypes = [
                ctypes.c_void_p,
                self._CALLBACK,
            ]
            library.dispatch_source_set_registration_handler_f.argtypes = [
                ctypes.c_void_p,
                self._CALLBACK,
            ]
            library.dispatch_source_set_cancel_handler_f.argtypes = [
                ctypes.c_void_p,
                self._CALLBACK,
            ]
            library.dispatch_source_get_data.argtypes = [ctypes.c_void_p]
            library.dispatch_source_get_data.restype = ctypes.c_ulong
            library.dispatch_resume.argtypes = [ctypes.c_void_p]
            library.dispatch_source_cancel.argtypes = [ctypes.c_void_p]
            library.dispatch_release.argtypes = [ctypes.c_void_p]

            source_type = ctypes.c_ubyte.in_dll(
                library,
                "_dispatch_source_type_memorypressure",
            )
            queue = queue_create(b"voiceflypy.semantic-refiner.pressure", None)
            if not queue:
                raise MonitorUnavailableError("queue_create_failed")
            source = source_create(
                ctypes.c_void_p(ctypes.addressof(source_type)),
                0,
                self._NORMAL | self._WARN | self._CRITICAL,
                queue,
            )
            if not source:
                library.dispatch_release(queue)
                raise MonitorUnavailableError("source_create_failed")

            self._library = library
            self._queue = int(queue)
            self._source = int(source)
            self._event_callback = self._CALLBACK(self._handle_event)
            self._registration_callback = self._CALLBACK(self._handle_registration)
            self._cancel_callback = self._CALLBACK(self._handle_cancel)
            library.dispatch_source_set_event_handler_f(
                source,
                self._event_callback,
            )
            library.dispatch_source_set_registration_handler_f(
                source,
                self._registration_callback,
            )
            library.dispatch_source_set_cancel_handler_f(
                source,
                self._cancel_callback,
            )
            library.dispatch_resume(source)
            if not self._registered.wait(self._registration_timeout_seconds):
                self.close()
                raise MonitorUnavailableError("registration_timeout")
        except MonitorUnavailableError:
            raise
        except BaseException as exc:
            try:
                self.close()
            except BaseException:
                pass
            raise MonitorUnavailableError("pressure_monitor_start_failed") from exc

    def _handle_registration(self, _context: object) -> None:
        self._registered.set()

    def _handle_cancel(self, _context: object) -> None:
        self._cancelled.set()

    def _handle_event(self, _context: object) -> None:
        try:
            library = self._library
            source = self._source
            if library is None or source is None:
                raise RuntimeError("source_unavailable")
            event = int(library.dispatch_source_get_data(ctypes.c_void_p(source)))
            with self._lock:
                if event & self._CRITICAL:
                    self._state = "critical"
                elif event & self._WARN:
                    self._state = "warning"
                elif event & self._NORMAL:
                    self._state = "normal"
                else:
                    self._failed = True
        except BaseException:
            with self._lock:
                self._failed = True

    def state(self) -> str:
        with self._lock:
            if self._failed:
                raise MonitorUnavailableError("pressure_callback_failed")
            return self._state

    def close(self) -> None:
        if self._uncancelled:
            raise MonitorUnavailableError("pressure_cancel_timeout")
        library = self._library
        source = self._source
        queue = self._queue
        if library is None:
            return
        if source is not None:
            try:
                library.dispatch_source_cancel(ctypes.c_void_p(source))
            except BaseException as exc:
                self._uncancelled = True
                _UNCANCELLED_PRESSURE_MONITORS.append(self)
                raise MonitorUnavailableError("pressure_cancel_failed") from exc
            if not self._cancelled.wait(self._registration_timeout_seconds):
                # Do not release a source, queue, library, or CFUNCTYPE callback
                # that libdispatch may still invoke.  This short-lived process
                # intentionally retains them and fails closed instead.
                self._uncancelled = True
                _UNCANCELLED_PRESSURE_MONITORS.append(self)
                raise MonitorUnavailableError("pressure_cancel_timeout")
        if source is not None:
            library.dispatch_release(ctypes.c_void_p(source))
        if queue is not None:
            library.dispatch_release(ctypes.c_void_p(queue))
        self._source = None
        self._queue = None
        self._library = None
        self._event_callback = None
        self._registration_callback = None
        self._cancel_callback = None


class DarwinResourceProbe:
    """Read whole-process-group RSS, global swap, and macOS pressure state."""

    _PROC_PIDTASKINFO = 4
    _HOST_VM_INFO64 = 4

    def __init__(
        self,
        *,
        pressure_factory: Callable[[], Any] = _DispatchMemoryPressureMonitor,
    ) -> None:
        if sys.platform != "darwin":
            raise MonitorUnavailableError("darwin_required")
        try:
            self._libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            self._libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
            self._libproc.proc_listpgrppids.argtypes = [
                ctypes.c_int,
                ctypes.c_void_p,
                ctypes.c_int,
            ]
            self._libproc.proc_listpgrppids.restype = ctypes.c_int
            self._libproc.proc_pidinfo.argtypes = [
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_uint64,
                ctypes.c_void_p,
                ctypes.c_int,
            ]
            self._libproc.proc_pidinfo.restype = ctypes.c_int
            self._libc.sysctlbyname.argtypes = [
                ctypes.c_char_p,
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_size_t),
                ctypes.c_void_p,
                ctypes.c_size_t,
            ]
            self._libc.sysctlbyname.restype = ctypes.c_int
            self._libc.memorystatus_get_level.argtypes = [
                ctypes.POINTER(ctypes.c_int)
            ]
            self._libc.memorystatus_get_level.restype = ctypes.c_int
            self._libc.mach_host_self.argtypes = []
            self._libc.mach_host_self.restype = ctypes.c_uint32
            self._libc.host_page_size.argtypes = [
                ctypes.c_uint32,
                ctypes.POINTER(ctypes.c_uint32),
            ]
            self._libc.host_page_size.restype = ctypes.c_int
            self._libc.host_statistics64.argtypes = [
                ctypes.c_uint32,
                ctypes.c_int,
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_uint32),
            ]
            self._libc.host_statistics64.restype = ctypes.c_int
        except BaseException as exc:
            raise MonitorUnavailableError("resource_library_unavailable") from exc
        self._pressure = pressure_factory()
        self._started = False
        self._swap_mode: str | None = None

    def _swapusage_used(self) -> int:
        usage = _SwapUsage()
        size = ctypes.c_size_t(ctypes.sizeof(usage))
        ctypes.set_errno(0)
        result = self._libc.sysctlbyname(
            b"vm.swapusage",
            ctypes.byref(usage),
            ctypes.byref(size),
            None,
            0,
        )
        if result != 0 or size.value != ctypes.sizeof(usage):
            raise MonitorUnavailableError("swap_probe_failed")
        return int(usage.xsu_used)

    def _cumulative_swapout_bytes(self) -> int:
        host = self._libc.mach_host_self()
        if not host:
            raise MonitorUnavailableError("swap_probe_failed")
        page_size = ctypes.c_uint32(0)
        if self._libc.host_page_size(host, ctypes.byref(page_size)) != 0:
            raise MonitorUnavailableError("swap_probe_failed")
        if page_size.value <= 0:
            raise MonitorUnavailableError("swap_probe_failed")
        statistics = _VMStatistics64()
        count = ctypes.c_uint32(
            ctypes.sizeof(statistics) // ctypes.sizeof(ctypes.c_int32)
        )
        if (
            self._libc.host_statistics64(
                host,
                self._HOST_VM_INFO64,
                ctypes.byref(statistics),
                ctypes.byref(count),
            )
            != 0
        ):
            raise MonitorUnavailableError("swap_probe_failed")
        required_count = ctypes.sizeof(statistics) // ctypes.sizeof(ctypes.c_int32)
        if count.value < required_count:
            raise MonitorUnavailableError("swap_probe_failed")
        return int(statistics.swapouts) * int(page_size.value)

    def _swap_used(self) -> int:
        """Return one monotonic/comparable byte counter, locking its source."""
        if self._swap_mode == "vm.swapusage_used":
            return self._swapusage_used()
        if self._swap_mode == "host_vm_info64_swapouts":
            return self._cumulative_swapout_bytes()
        try:
            value = self._swapusage_used()
        except MonitorUnavailableError:
            value = self._cumulative_swapout_bytes()
            self._swap_mode = "host_vm_info64_swapouts"
        else:
            self._swap_mode = "vm.swapusage_used"
        return value

    def _memory_headroom_percent(self) -> int:
        level = ctypes.c_int(-1)
        ctypes.set_errno(0)
        if self._libc.memorystatus_get_level(ctypes.byref(level)) != 0:
            raise MonitorUnavailableError("headroom_probe_failed")
        if not 0 <= level.value <= 100:
            raise MonitorUnavailableError("headroom_probe_invalid")
        return int(level.value)

    def preflight(self) -> SystemSnapshot:
        if self._started:
            raise MonitorUnavailableError("probe_reused")
        self._pressure.start()
        self._started = True
        return SystemSnapshot(
            swap_used_bytes=self._swap_used(),
            pressure=self._pressure.state(),
            memory_headroom_percent=self._memory_headroom_percent(),
        )

    def _list_group_pids(self, pgid: int) -> tuple[int, ...]:
        if type(pgid) is not int or pgid <= 1:
            raise MonitorUnavailableError("invalid_process_group")
        ctypes.set_errno(0)
        estimate = self._libproc.proc_listpgrppids(pgid, None, 0)
        if estimate < 0:
            raise MonitorUnavailableError("process_group_enumeration_failed")
        capacity = max(64, estimate + 32)
        for _attempt in range(4):
            values = (ctypes.c_int * capacity)()
            ctypes.set_errno(0)
            count = self._libproc.proc_listpgrppids(
                pgid,
                values,
                ctypes.sizeof(values),
            )
            if count < 0:
                raise MonitorUnavailableError("process_group_enumeration_failed")
            if count < capacity:
                return tuple(sorted({int(pid) for pid in values[:count] if pid > 0}))
            capacity *= 2
        raise MonitorUnavailableError("process_group_enumeration_unstable")

    @staticmethod
    def _still_in_group(pid: int, pgid: int) -> bool:
        try:
            return os.getpgid(pid) == pgid
        except ProcessLookupError:
            return False
        except OSError as exc:
            if exc.errno == errno.ESRCH:
                return False
            raise MonitorUnavailableError("process_group_membership_failed") from exc

    def _resident_bytes(self, pid: int, pgid: int) -> int | None:
        if not self._still_in_group(pid, pgid):
            return None
        info = _ProcTaskInfo()
        ctypes.set_errno(0)
        result = self._libproc.proc_pidinfo(
            pid,
            self._PROC_PIDTASKINFO,
            0,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if result != ctypes.sizeof(info):
            error_number = ctypes.get_errno()
            if error_number == errno.ESRCH or not self._still_in_group(pid, pgid):
                return None
            raise MonitorUnavailableError("process_rss_probe_failed")
        if not self._still_in_group(pid, pgid):
            return None
        return int(info.pti_resident_size)

    def group_member_count(self, pgid: int) -> int:
        return sum(self._still_in_group(pid, pgid) for pid in self._list_group_pids(pgid))

    def snapshot(self, pgid: int) -> ResourceSnapshot:
        pids = self._list_group_pids(pgid)
        rss = 0
        members = 0
        for pid in pids:
            resident = self._resident_bytes(pid, pgid)
            if resident is not None:
                rss += resident
                members += 1
        return ResourceSnapshot(
            group_rss_bytes=rss,
            group_member_count=members,
            swap_used_bytes=self._swap_used(),
            pressure=self._pressure.state(),
            memory_headroom_percent=self._memory_headroom_percent(),
        )

    def close(self) -> None:
        if self._started:
            self._pressure.close()
            self._started = False


class _BoundedOutputCollector:
    def __init__(self, stdout: BinaryIO, stderr: BinaryIO, limit: int) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self._limit = limit
        self._lock = threading.Lock()
        self._stdout_data = bytearray()
        self._total = 0
        self.overflow = threading.Event()
        self._threads = (
            threading.Thread(
                target=self._read,
                args=(stdout, True),
                daemon=True,
                name="semantic-eval-stdout",
            ),
            threading.Thread(
                target=self._read,
                args=(stderr, False),
                daemon=True,
                name="semantic-eval-stderr",
            ),
        )

    def start(self) -> None:
        for thread in self._threads:
            thread.start()

    def _read(self, stream: BinaryIO, retain: bool) -> None:
        try:
            while True:
                chunk = stream.read(64 * 1024)
                if not chunk:
                    return
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8", "strict")
                with self._lock:
                    previous = self._total
                    self._total += len(chunk)
                    allowed = max(0, self._limit - previous)
                    if retain and allowed:
                        self._stdout_data.extend(chunk[:allowed])
                    if self._total > self._limit:
                        self.overflow.set()
        except BaseException:
            self.overflow.set()

    def finish(self, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        for thread in self._threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        if any(thread.is_alive() for thread in self._threads):
            for stream in (self._stdout, self._stderr):
                try:
                    stream.close()
                except BaseException:
                    pass
            for thread in self._threads:
                thread.join(0.1)
        if any(thread.is_alive() for thread in self._threads):
            self.overflow.set()
        with self._lock:
            return bytes(self._stdout_data)


def _failure(code: str, *, exit_code: int = 3) -> SupervisorResult:
    if code not in _SAFE_FAILURE_CODES:
        code = "resource_monitor_unavailable"
    return SupervisorResult(
        exit_code=exit_code,
        report={
            "schema_version": SCHEMA_VERSION,
            "supervisor": SUPERVISOR_NAME,
            "complete": False,
            "error": {"code": code},
        },
    )


def _successful_supervised_report(
    child_report: Mapping[str, Any],
    limits: Limits,
    *,
    peak_group_rss_bytes: int,
    maximum_swap_growth_bytes: int,
    minimum_headroom_percent: int,
    monitor_samples: int,
) -> SupervisorResult:
    """Expose only fixed anonymous aggregates and proof of supervision."""
    baseline = child_report["baseline"]
    candidate = child_report["second_model"]
    candidate_p95_ms = float(candidate["latency_ms"]["p95"])
    latency_status = (
        "pass" if candidate_p95_ms <= P95_LATENCY_LIMIT_MS else "fail"
    )
    performance_status = "fail" if latency_status == "fail" else "pending"

    def provider_summary(value: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "name": value["name"],
            "identity_sha256": value["identity_sha256"],
            "cer": value["cer"],
            "edit_distance": value["edit_distance"],
            "reference_characters": value["reference_characters"],
            "identifier_exact_accuracy": value["identifier_exact_accuracy"],
            "identifier_correct": value["identifier_correct"],
            "identifier_total": value["identifier_total"],
            "successful_count": value["successful_count"],
            "failed_count": value["failed_count"],
            "latency_ms": dict(value["latency_ms"]),
        }

    return SupervisorResult(
        exit_code=0,
        report={
            "schema_version": SCHEMA_VERSION,
            "supervisor": SUPERVISOR_NAME,
            "complete": True,
            "privacy": {
                "metrics_only_enforced": True,
                "paths_emitted": False,
                "transcripts_emitted": False,
                "sample_ids_emitted": False,
                "per_sample_details_emitted": False,
                "child_output_persisted": False,
            },
            "resource_limits": {
                "group_rss_bytes": limits.rss_bytes,
                "swap_growth_bytes": limits.swap_growth_bytes,
                "minimum_memory_headroom_percent": limits.minimum_headroom_percent,
                "wall_timeout_seconds": limits.wall_timeout_seconds,
                "child_output_bytes": limits.output_bytes,
            },
            "resource_observed": {
                "peak_group_rss_bytes": peak_group_rss_bytes,
                "maximum_swap_growth_bytes": maximum_swap_growth_bytes,
                "minimum_memory_headroom_percent": minimum_headroom_percent,
                "memory_pressure_state": "normal",
                "monitor_samples": monitor_samples,
                "monitor_complete": True,
                "rss_measurement": "sampled_process_group_total",
                "swap_measurement": "net_used_or_cumulative_swapouts_fail_closed",
            },
            "performance_resources": {
                "status": performance_status,
                "safety_limits_observed": {
                    "status": "pass",
                    "measurement": "polling_plus_memory_pressure_events",
                    "poll_interval_ms": round(
                        limits.poll_interval_seconds * 1000.0,
                        3,
                    ),
                    "kernel_enforced": False,
                },
                "candidate_p95_latency": {
                    "limit_ms": P95_LATENCY_LIMIT_MS,
                    "observed_ms": candidate_p95_ms,
                    "status": latency_status,
                },
                "single_call_timeout": {
                    "limit_ms": SINGLE_CALL_TIMEOUT_SECONDS * 1000.0,
                    "status": "pass",
                    "measurement": "enforced",
                },
                "stable_rss_drift": {
                    "limit_bytes": STABLE_RSS_DRIFT_LIMIT_BYTES,
                    "observed_bytes": None,
                    "status": "pending",
                    "measurement": "not_measured",
                },
            },
            "evaluation": {
                "name": EVALUATION_NAME,
                "sample_count": EXPECTED_SAMPLE_COUNT,
                "normalization": dict(child_report["normalization"]),
                "providers": {
                    "baseline": provider_summary(baseline),
                    "candidate": provider_summary(candidate),
                },
                "comparison": dict(child_report["comparison"]),
                "quality_protocol": child_report["quality_protocol"],
            },
        },
    )


_SUPERVISOR_OWNED_EVALUATOR_FLAGS = frozenset(
    {
        "--protocol",
        "--timeout",
        "--max-audio-seconds",
        "--num-threads",
        "--language",
    }
)


def _argument_flag(argument: str) -> str:
    return argument.split("=", 1)[0]


def _child_command(arguments: Sequence[str]) -> list[str]:
    if (
        len(arguments) > 64
        or any(
            not isinstance(argument, str)
            or not argument
            or len(argument) > 4096
            or "\x00" in argument
            for argument in arguments
        )
    ):
        raise SupervisorError("supervisor_arguments_invalid")
    flags = {_argument_flag(argument) for argument in arguments}
    if "--include-transcripts" in flags:
        raise SupervisorError("transcript_output_forbidden")
    if "--protocol" in flags:
        raise SupervisorError("evaluation_protocol_override_forbidden")
    if flags & (_SUPERVISOR_OWNED_EVALUATOR_FLAGS - {"--protocol"}):
        raise SupervisorError("controlled_evaluator_argument_forbidden")
    sanitized = [argument for argument in arguments if argument != "--metrics-only"]
    evaluator = Path(__file__).resolve().with_name("semantic_refiner_eval.py")
    return [
        sys.executable,
        os.fspath(evaluator),
        *sanitized,
        "--protocol",
        PROTOCOL_VERSION,
        "--timeout",
        str(SINGLE_CALL_TIMEOUT_SECONDS),
        "--max-audio-seconds",
        str(MAX_AUDIO_SECONDS),
        "--num-threads",
        "1",
        "--language",
        "zh",
        "--metrics-only",
    ]


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and set(value).issubset(_SHA256_HEX)
    )


def _is_nonnegative_int(value: object) -> bool:
    return type(value) is int and value >= 0


def _is_finite_number(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )


def _valid_system_snapshot(snapshot: object) -> bool:
    return (
        isinstance(snapshot, SystemSnapshot)
        and type(snapshot.swap_used_bytes) is int
        and snapshot.swap_used_bytes >= 0
        and type(snapshot.pressure) is str
        and snapshot.pressure in {"normal", "warning", "critical"}
        and type(snapshot.memory_headroom_percent) is int
        and 0 <= snapshot.memory_headroom_percent <= 100
    )


def _valid_resource_snapshot(snapshot: object) -> bool:
    return (
        isinstance(snapshot, ResourceSnapshot)
        and _valid_system_snapshot(snapshot)
        and type(snapshot.group_rss_bytes) is int
        and snapshot.group_rss_bytes >= 0
        and type(snapshot.group_member_count) is int
        and snapshot.group_member_count >= 0
    )


def _validate_provider_report(report: object, *, role: str) -> bool:
    if not isinstance(report, Mapping):
        return False
    required = {
        "name",
        "cer",
        "edit_distance",
        "reference_characters",
        "identifier_exact_accuracy",
        "identifier_correct",
        "identifier_total",
        "successful_count",
        "failed_count",
        "latency_ms",
        "identity_sha256",
    }
    allowed_names = (
        frozenset({_BASELINE_PROVIDER_NAME})
        if role == "baseline"
        else _CANDIDATE_PROVIDER_NAMES
        if role == "candidate"
        else frozenset()
    )
    if set(report) != required or report.get("name") not in allowed_names:
        return False
    if not _is_sha256(report.get("identity_sha256")):
        return False
    for key in (
        "edit_distance",
        "reference_characters",
        "identifier_correct",
        "identifier_total",
        "successful_count",
        "failed_count",
    ):
        if not _is_nonnegative_int(report.get(key)):
            return False
    if (
        report.get("failed_count") != 0
        or report.get("successful_count") != EXPECTED_SAMPLE_COUNT
        or report.get("reference_characters") != EXPECTED_REFERENCE_CHARACTERS
        or report.get("identifier_total") != 8
        or report.get("identifier_correct") > report.get("identifier_total")
        or not _is_finite_number(report.get("cer"))
        or float(report["cer"]) < 0.0
    ):
        return False
    expected_cer = round(
        report["edit_distance"] / report["reference_characters"],
        6,
    )
    if float(report["cer"]) != expected_cer:
        return False
    accuracy = report.get("identifier_exact_accuracy")
    if (
        not _is_finite_number(accuracy)
        or not 0.0 <= float(accuracy) <= 1.0
        or float(accuracy)
        != round(report["identifier_correct"] / report["identifier_total"], 6)
    ):
        return False
    latency = report.get("latency_ms")
    return (
        isinstance(latency, Mapping)
        and set(latency) == {"p50", "p95"}
        and all(_is_finite_number(latency.get(key)) for key in ("p50", "p95"))
        and 0.0 <= float(latency["p50"]) <= float(latency["p95"])
    )


def _validate_error_detail(detail: object) -> bool:
    if not isinstance(detail, Mapping):
        return False
    if set(detail) != {"sample_number", "reference_characters", "baseline", "second_model"}:
        return False
    if (
        not _is_nonnegative_int(detail.get("sample_number"))
        or not 1 <= detail["sample_number"] <= EXPECTED_SAMPLE_COUNT
    ):
        return False
    if (
        not _is_nonnegative_int(detail.get("reference_characters"))
        or detail["reference_characters"]
        != PROTOCOL_REFERENCE_CHARACTERS_BY_SAMPLE_NUMBER[detail["sample_number"]]
    ):
        return False
    for role in ("baseline", "second_model"):
        value = detail.get(role)
        if not isinstance(value, Mapping):
            return False
        if set(value) != {"edit_distance", "cer", "latency_ms", "error_type", "error_code"}:
            return False
        if not _is_nonnegative_int(value.get("edit_distance")):
            return False
        if (
            not _is_finite_number(value.get("cer"))
            or not _is_finite_number(value.get("latency_ms"))
            or float(value["cer"]) < 0.0
            or float(value["latency_ms"]) < 0.0
            or float(value["cer"])
            != round(value["edit_distance"] / detail["reference_characters"], 6)
        ):
            return False
        if value.get("error_type") is not None or value.get("error_code") is not None:
            return False
    return True


def _validate_metrics_report(report: object) -> str | None:
    if not isinstance(report, Mapping):
        return "child_output_invalid"
    required = {
        "schema_version",
        "evaluation",
        "sample_count",
        "privacy",
        "complete",
        "provider_failure_count",
        "normalization",
        "baseline",
        "second_model",
        "comparison",
        "error_count",
        "errors",
        "quality_protocol",
    }
    if set(report) != required:
        return "child_output_invalid"
    if report.get("schema_version") != 1 or report.get("evaluation") != EVALUATION_NAME:
        return "child_output_invalid"
    if report.get("complete") is not True or report.get("provider_failure_count") != 0:
        return "child_report_incomplete"
    if report.get("sample_count") != EXPECTED_SAMPLE_COUNT:
        return "child_output_invalid"
    privacy = report.get("privacy")
    expected_privacy = {
        "audio_persisted": False,
        "transcripts_persisted": False,
        "paths_emitted": False,
        "free_text_included": False,
        "sample_metadata_included": False,
        "raw_provider_metadata_included": False,
        "transcripts_included": False,
        "output": "stdout_only",
    }
    if privacy != expected_privacy:
        return "child_report_privacy_invalid"
    expected_normalization = {
        "cer": "Unicode NFKC code points with whitespace removed",
        "identifier_exact": "Unicode NFKC with outer/collapsed whitespace",
    }
    if report.get("normalization") != expected_normalization:
        return "child_output_invalid"
    baseline_report = report.get("baseline")
    second_report = report.get("second_model")
    if not _validate_provider_report(baseline_report, role="baseline"):
        return "child_output_invalid"
    if not _validate_provider_report(second_report, role="candidate"):
        return "child_output_invalid"
    assert isinstance(baseline_report, Mapping)
    assert isinstance(second_report, Mapping)
    if baseline_report.get("reference_characters") != second_report.get(
        "reference_characters"
    ):
        return "child_output_invalid"
    comparison = report.get("comparison")
    comparison_keys = {
        "agreement_count",
        "disagreement_count",
        "unavailable_count",
        "second_better_count",
        "baseline_better_count",
        "equal_error_count",
    }
    if not isinstance(comparison, Mapping) or set(comparison) != comparison_keys:
        return "child_output_invalid"
    if not all(_is_nonnegative_int(comparison.get(key)) for key in comparison_keys):
        return "child_output_invalid"
    if (
        comparison["agreement_count"]
        + comparison["disagreement_count"]
        + comparison["unavailable_count"]
        != EXPECTED_SAMPLE_COUNT
        or comparison["second_better_count"]
        + comparison["baseline_better_count"]
        + comparison["equal_error_count"]
        != EXPECTED_SAMPLE_COUNT
    ):
        return "child_output_invalid"
    if comparison["unavailable_count"] != 0:
        return "child_output_invalid"
    errors = report.get("errors")
    if not isinstance(errors, list) or not all(_validate_error_detail(item) for item in errors):
        return "child_output_invalid"
    if (
        not _is_nonnegative_int(report.get("error_count"))
        or report["error_count"] > EXPECTED_SAMPLE_COUNT
    ):
        return "child_output_invalid"
    if report.get("error_count") != len(errors):
        return "child_output_invalid"
    sample_numbers = [detail["sample_number"] for detail in errors]
    if sample_numbers != sorted(set(sample_numbers)):
        return "child_output_invalid"
    baseline_error_edits = sum(detail["baseline"]["edit_distance"] for detail in errors)
    candidate_error_edits = sum(
        detail["second_model"]["edit_distance"] for detail in errors
    )
    if (
        baseline_error_edits != baseline_report["edit_distance"]
        or candidate_error_edits != second_report["edit_distance"]
        or any(
            detail["baseline"]["edit_distance"] == 0
            and detail["second_model"]["edit_distance"] == 0
            for detail in errors
        )
    ):
        return "child_output_invalid"
    second_better = sum(
        detail["second_model"]["edit_distance"]
        < detail["baseline"]["edit_distance"]
        for detail in errors
    )
    baseline_better = sum(
        detail["baseline"]["edit_distance"]
        < detail["second_model"]["edit_distance"]
        for detail in errors
    )
    if (
        comparison["second_better_count"] != second_better
        or comparison["baseline_better_count"] != baseline_better
        or comparison["equal_error_count"]
        != EXPECTED_SAMPLE_COUNT - second_better - baseline_better
    ):
        return "child_output_invalid"
    try:
        validate_public_report(report.get("quality_protocol"))
    except ProtocolValidationError:
        return "child_output_invalid"
    quality_completion = report["quality_protocol"]["provider_completion"]
    if (
        quality_completion["baseline_successful"]
        != baseline_report["successful_count"]
        or quality_completion["candidate_successful"]
        != second_report["successful_count"]
    ):
        return "child_output_invalid"
    quality = report["quality_protocol"]
    quality_splits = quality["split_edit_distance"]
    for split in ("validation", "holdout"):
        split_details = [
            detail
            for detail in errors
            if PROTOCOL_SPLIT_BY_SAMPLE_NUMBER[detail["sample_number"]] == split
        ]
        if (
            quality_splits[split]["baseline_edit_distance"]
            != sum(detail["baseline"]["edit_distance"] for detail in split_details)
            or quality_splits[split]["candidate_edit_distance"]
            != sum(
                detail["second_model"]["edit_distance"]
                for detail in split_details
            )
        ):
            return "child_output_invalid"
    for provider_role, detail_role, quality_role in (
        (baseline_report, "baseline", "baseline"),
        (second_report, "second_model", "candidate"),
    ):
        for category_name in (
            "identifiers",
            "technical",
            "positive_homophones",
            "negative_controls",
        ):
            category_numbers = frozenset(
                PROTOCOL_CATEGORY_SAMPLE_NUMBERS[category_name]
            )
            exact_count = quality["categories"][category_name][quality_role][
                "clip_exact"
            ]
            nonzero_edits = sum(
                detail["sample_number"] in category_numbers
                and detail[detail_role]["edit_distance"] > 0
                for detail in errors
            )
            if exact_count > len(category_numbers) - nonzero_edits:
                return "child_output_invalid"
            if (
                category_name == "identifiers"
                and provider_role["identifier_correct"] != exact_count
            ):
                return "child_output_invalid"
    return None


def _terminate_process_group(
    process: ProcessLike,
    pgid: int,
    probe: ResourceProbe,
    limits: Limits,
    *,
    killpg: Callable[[int, int], None] = os.killpg,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    if pgid != process.pid or pgid == os.getpgrp():
        try:
            process.kill()
        except BaseException:
            pass
        raise SupervisorError("process_group_cleanup_failed")

    def send(group_signal: int) -> None:
        try:
            killpg(pgid, group_signal)
        except ProcessLookupError:
            pass
        except OSError as exc:
            if exc.errno != errno.ESRCH:
                raise SupervisorError("process_group_cleanup_failed") from exc

    send(signal.SIGTERM)
    deadline = monotonic() + limits.terminate_grace_seconds
    while monotonic() < deadline:
        try:
            if process.poll() is not None and probe.group_member_count(pgid) == 0:
                break
        except BaseException:
            pass
        sleep(min(0.05, limits.terminate_grace_seconds))
    else:
        send(signal.SIGKILL)
    try:
        process.wait(timeout=limits.terminate_grace_seconds)
    except BaseException:
        send(signal.SIGKILL)
        try:
            process.wait(timeout=limits.terminate_grace_seconds)
        except BaseException as exc:
            raise SupervisorError("process_group_cleanup_failed") from exc
    try:
        if probe.group_member_count(pgid):
            send(signal.SIGKILL)
            raise SupervisorError("process_group_cleanup_failed")
    except MonitorUnavailableError:
        # The group has already received SIGKILL and the leader was reaped.
        pass


def run_supervised(
    eval_args: Sequence[str],
    limits: Limits = Limits(),
    *,
    process_factory: Callable[..., ProcessLike] = subprocess.Popen,
    probe_factory: Callable[[], ResourceProbe] = DarwinResourceProbe,
    getpgid: Callable[[int], int] = os.getpgid,
    group_terminator: Callable[[ProcessLike, int, ResourceProbe, Limits], None]
    | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> SupervisorResult:
    """Run one evaluator subprocess, returning only a safe report object."""
    try:
        limits.validate()
        command = _child_command(tuple(eval_args))
    except SupervisorError as exc:
        return _failure(exc.code, exit_code=2)

    probe: ResourceProbe | None = None
    process: ProcessLike | None = None
    collector: _BoundedOutputCollector | None = None
    pgid: int | None = None
    group_validated = False
    result: SupervisorResult | None = None
    must_terminate = False
    peak_group_rss_bytes = 0
    maximum_swap_growth_bytes = 0
    minimum_headroom_percent = 101
    monitor_samples = 0

    def terminate() -> None:
        nonlocal must_terminate
        nonlocal group_validated
        if not must_terminate or process is None:
            return
        if not group_validated or probe is None or pgid is None:
            try:
                process.kill()
                process.wait(timeout=limits.terminate_grace_seconds)
            except BaseException as exc:
                raise SupervisorError("process_group_cleanup_failed") from exc
            must_terminate = False
            return
        terminator = group_terminator
        if terminator is None:
            _terminate_process_group(
                process,
                pgid,
                probe,
                limits,
                monotonic=monotonic,
                sleep=sleep,
            )
        else:
            terminator(process, pgid, probe, limits)
        must_terminate = False

    try:
        try:
            probe = probe_factory()
            baseline = probe.preflight()
        except BaseException as exc:
            raise SupervisorError("resource_monitor_unavailable") from exc
        if not _valid_system_snapshot(baseline):
            raise SupervisorError("resource_monitor_unavailable")
        if baseline.pressure != "normal":
            raise SupervisorError("memory_pressure_not_normal")
        if baseline.memory_headroom_percent < limits.minimum_headroom_percent:
            raise SupervisorError("memory_pressure_not_normal")
        minimum_headroom_percent = baseline.memory_headroom_percent

        try:
            process = process_factory(
                command,
                shell=False,
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                close_fds=True,
                text=False,
            )
        except BaseException as exc:
            raise SupervisorError("process_spawn_failed") from exc
        must_terminate = True
        pgid = process.pid
        if type(pgid) is not int or pgid <= 1:
            raise SupervisorError("process_group_invalid")
        try:
            observed_group = getpgid(pgid)
        except ProcessLookupError:
            if process.poll() is None:
                raise SupervisorError("process_group_invalid")
            observed_group = pgid
        except OSError as exc:
            if exc.errno == errno.ESRCH and process.poll() is not None:
                observed_group = pgid
            else:
                raise SupervisorError("process_group_invalid") from exc
        if observed_group != pgid or pgid == os.getpgrp():
            raise SupervisorError("process_group_invalid")
        group_validated = True

        if process.stdout is None or process.stderr is None:
            raise SupervisorError("process_spawn_failed")
        collector = _BoundedOutputCollector(
            process.stdout,
            process.stderr,
            limits.output_bytes,
        )
        collector.start()
        deadline = monotonic() + limits.wall_timeout_seconds

        failure_code: str | None = None
        while True:
            if collector.overflow.is_set():
                failure_code = "output_limit_exceeded"
                break
            if monotonic() >= deadline:
                failure_code = "wall_timeout_exceeded"
                break
            try:
                current = probe.snapshot(pgid)
            except SupervisorError as exc:
                failure_code = exc.code
                break
            except BaseException:
                failure_code = "resource_monitor_unavailable"
                break
            if not _valid_resource_snapshot(current):
                failure_code = "resource_monitor_unavailable"
                break
            minimum_headroom_percent = min(
                minimum_headroom_percent,
                current.memory_headroom_percent,
            )
            if current.group_rss_bytes > limits.rss_bytes:
                failure_code = "rss_limit_exceeded"
                break
            swap_growth = max(0, current.swap_used_bytes - baseline.swap_used_bytes)
            maximum_swap_growth_bytes = max(
                maximum_swap_growth_bytes,
                swap_growth,
            )
            if swap_growth > limits.swap_growth_bytes:
                failure_code = "swap_limit_exceeded"
                break
            if current.pressure != "normal":
                failure_code = "memory_pressure_not_normal"
                break
            if current.memory_headroom_percent < limits.minimum_headroom_percent:
                failure_code = "memory_pressure_not_normal"
                break
            if current.group_member_count >= 1:
                monitor_samples += 1
                peak_group_rss_bytes = max(
                    peak_group_rss_bytes,
                    current.group_rss_bytes,
                )

            return_code = process.poll()
            if return_code is not None:
                try:
                    remaining = probe.group_member_count(pgid)
                except BaseException:
                    failure_code = "resource_monitor_unavailable"
                    break
                if remaining:
                    failure_code = "descendant_process_survived"
                    break
                must_terminate = False
                break
            if current.group_member_count < 1:
                failure_code = "resource_monitor_unavailable"
                break
            sleep(limits.poll_interval_seconds)

        if failure_code is not None:
            terminate()
            if collector is not None:
                collector.finish(limits.terminate_grace_seconds)
            result = _failure(failure_code)
        else:
            try:
                return_code = process.wait(timeout=limits.terminate_grace_seconds)
            except BaseException:
                must_terminate = True
                terminate()
                raise SupervisorError("process_group_cleanup_failed")
            output = collector.finish(limits.terminate_grace_seconds)
            if monitor_samples < 1:
                result = _failure("resource_monitor_unavailable")
            elif collector.overflow.is_set():
                result = _failure("output_limit_exceeded")
            elif return_code != 0:
                result = _failure("child_failed", exit_code=4)
            else:
                try:
                    decoded = output.decode("utf-8", "strict")
                    child_report = json.loads(decoded)
                except (UnicodeError, json.JSONDecodeError):
                    result = _failure("child_output_invalid", exit_code=4)
                else:
                    validation_error = _validate_metrics_report(child_report)
                    if validation_error is not None:
                        result = _failure(validation_error, exit_code=4)
                    else:
                        result = _successful_supervised_report(
                            child_report,
                            limits,
                            peak_group_rss_bytes=peak_group_rss_bytes,
                            maximum_swap_growth_bytes=maximum_swap_growth_bytes,
                            minimum_headroom_percent=minimum_headroom_percent,
                            monitor_samples=monitor_samples,
                        )
    except KeyboardInterrupt:
        try:
            terminate()
        except BaseException:
            pass
        result = _failure("supervisor_interrupted", exit_code=130)
    except SupervisorError as exc:
        try:
            terminate()
        except SupervisorError:
            result = _failure("process_group_cleanup_failed")
        if result is None:
            input_error = exc.code in {
                "supervisor_arguments_invalid",
                "transcript_output_forbidden",
                "evaluation_protocol_override_forbidden",
                "controlled_evaluator_argument_forbidden",
            }
            result = _failure(exc.code, exit_code=2 if input_error else 3)
    except BaseException:
        try:
            terminate()
        except BaseException:
            pass
        result = _failure("resource_monitor_unavailable")
    finally:
        if must_terminate:
            try:
                terminate()
            except BaseException:
                result = _failure("process_group_cleanup_failed")
        if collector is not None and process is not None and process.poll() is not None:
            collector.finish(limits.terminate_grace_seconds)
        if probe is not None:
            try:
                probe.close()
            except BaseException:
                if result is None or result.exit_code == 0:
                    result = _failure("resource_monitor_unavailable")
    return result if result is not None else _failure("resource_monitor_unavailable")


def _parse_cli(arguments: Sequence[str]) -> tuple[float, tuple[str, ...]]:
    if "--" not in arguments:
        raise SupervisorError("supervisor_arguments_invalid")
    separator = arguments.index("--")
    supervisor_args = tuple(arguments[:separator])
    eval_args = tuple(arguments[separator + 1 :])
    if not eval_args:
        raise SupervisorError("supervisor_arguments_invalid")
    timeout = DEFAULT_WALL_TIMEOUT_SECONDS
    if supervisor_args:
        if len(supervisor_args) == 2 and supervisor_args[0] == "--wall-timeout":
            raw_timeout = supervisor_args[1]
        elif len(supervisor_args) == 1 and supervisor_args[0].startswith(
            "--wall-timeout="
        ):
            raw_timeout = supervisor_args[0].split("=", 1)[1]
        else:
            raise SupervisorError("supervisor_arguments_invalid")
        try:
            timeout = float(raw_timeout)
        except (TypeError, ValueError) as exc:
            raise SupervisorError("supervisor_arguments_invalid") from exc
        if not math.isfinite(timeout) or not 1 <= timeout <= 3600:
            raise SupervisorError("supervisor_arguments_invalid")
    return timeout, eval_args


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO = sys.stdout,
) -> int:
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    if arguments in (("-h",), ("--help",)):
        print(
            "usage: semantic_refiner_supervisor.py [--wall-timeout SECONDS] "
            "-- EVALUATOR_ARGUMENTS",
            file=stdout,
        )
        return 0
    try:
        wall_timeout, eval_args = _parse_cli(arguments)
    except SupervisorError as exc:
        result = _failure(exc.code, exit_code=2)
    else:
        result = run_supervised(
            eval_args,
            replace(Limits(), wall_timeout_seconds=wall_timeout),
        )
    print(
        json.dumps(
            result.report,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ),
        file=stdout,
    )
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
