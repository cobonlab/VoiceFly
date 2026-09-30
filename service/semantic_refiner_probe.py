#!/usr/bin/env python3
"""Run one explicit, read-only VoiceFlypy semantic-refiner probe.

The probe reads a caller-supplied PCM WAV into memory, sends the resulting
``AudioSnapshot`` to the configured loopback whisper.cpp endpoint, and runs
the existing ``FinalTextRefiner`` in shadow mode.  It never starts a server,
changes selected text, or writes audio/transcripts to disk.  JSON is written
only to stdout when this module is explicitly executed.

The supplied X-ASR text is deliberately named *baseline*, never reference or
ground truth: it is evidence used by the validator, not an accuracy label.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence, TextIO

import numpy as np

from voice_refiner import (
    AudioSnapshot,
    CandidateValidator,
    FinalTextRefiner,
    FinalizationRequest,
    MAX_CAPTURE_SAMPLE_RATE,
    MAX_REFINER_AUDIO_SECONDS,
    WhisperServerBackend,
)


SCHEMA_VERSION = 1
PROBE_NAME = "voiceflypy_semantic_refiner"
DEFAULT_ENDPOINT = "http://127.0.0.1:18080/inference"
MAX_WAV_CHANNELS = 8
MAX_WAV_PCM_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True)
class ProbeConfig:
    wav_path: Path
    baseline_text: str
    endpoint: str = DEFAULT_ENDPOINT
    language: str = "auto"
    timeout_seconds: float = 6.0
    max_audio_seconds: float = 20.0
    include_transcripts: bool = True


@dataclass(frozen=True)
class LoadedWav:
    snapshot: AudioSnapshot
    source_channels: int
    source_sample_width_bits: int


def _decode_pcm(raw: bytes, sample_width: int) -> np.ndarray:
    """Decode little-endian integer PCM into normalized float32 samples."""
    if sample_width == 1:
        values = np.frombuffer(raw, dtype=np.uint8).astype(np.float32)
        return (values - 128.0) / 128.0
    if sample_width == 2:
        values = np.frombuffer(raw, dtype="<i2").astype(np.float32)
        return values / 32768.0
    if sample_width == 3:
        octets = np.frombuffer(raw, dtype=np.uint8)
        if octets.size % 3:
            raise ValueError("truncated_24_bit_pcm")
        triples = octets.reshape(-1, 3).astype(np.int32)
        values = triples[:, 0] | (triples[:, 1] << 8) | (triples[:, 2] << 16)
        values = (values ^ 0x800000) - 0x800000
        return values.astype(np.float32) / 8388608.0
    if sample_width == 4:
        values = np.frombuffer(raw, dtype="<i4").astype(np.float64)
        return (values / 2147483648.0).astype(np.float32)
    raise ValueError("unsupported_pcm_sample_width")


def load_pcm_wav(path: Path, *, max_audio_seconds: float) -> LoadedWav:
    """Load a bounded PCM WAV without creating temporary or derived files."""
    if not np.isfinite(max_audio_seconds) or max_audio_seconds <= 0:
        raise ValueError("invalid_max_audio_seconds")
    max_audio_seconds = min(max_audio_seconds, MAX_REFINER_AUDIO_SECONDS)
    with wave.open(str(path), "rb") as reader:
        if reader.getcomptype() != "NONE":
            raise ValueError("compressed_wav_not_supported")
        channels = reader.getnchannels()
        sample_width = reader.getsampwidth()
        sample_rate = reader.getframerate()
        frame_count = reader.getnframes()
        if channels <= 0 or sample_rate <= 0 or frame_count <= 0:
            raise ValueError("invalid_wav_header")
        if channels > MAX_WAV_CHANNELS:
            raise ValueError("too_many_wav_channels")
        if sample_rate > MAX_CAPTURE_SAMPLE_RATE:
            raise ValueError("wav_sample_rate_too_high")
        if sample_width not in {1, 2, 3, 4}:
            raise ValueError("unsupported_pcm_sample_width")
        if frame_count / sample_rate > max_audio_seconds:
            raise ValueError("audio_too_long")
        if frame_count * channels * sample_width > MAX_WAV_PCM_BYTES:
            raise ValueError("wav_pcm_too_large")
        raw = reader.readframes(frame_count)

    values = _decode_pcm(raw, sample_width)
    expected_samples = frame_count * channels
    if values.size != expected_samples:
        raise ValueError("truncated_pcm_frames")
    frames = values.reshape(frame_count, channels)
    if channels == 1:
        mono = frames[:, 0].astype(np.float32, copy=True)
    else:
        mono = frames.mean(axis=1, dtype=np.float64).astype(np.float32)
    return LoadedWav(
        snapshot=AudioSnapshot(samples=mono, sample_rate=sample_rate),
        source_channels=channels,
        source_sample_width_bits=sample_width * 8,
    )


def run_probe(config: ProbeConfig, *, backend: Any | None = None) -> dict[str, Any]:
    """Run one in-memory shadow pass and return a JSON-serializable report."""
    if not config.baseline_text.strip():
        raise ValueError("baseline_text_empty")
    loaded = load_pcm_wav(
        config.wav_path,
        max_audio_seconds=config.max_audio_seconds,
    )
    selected_backend = backend or WhisperServerBackend(
        config.endpoint,
        language=config.language,
    )
    refiner = FinalTextRefiner(
        mode="shadow",
        backend=selected_backend,
        validator=CandidateValidator(),
        timeout_seconds=config.timeout_seconds,
        max_audio_seconds=config.max_audio_seconds,
    )
    request = FinalizationRequest(
        session_id="explicit-cli-probe",
        raw_segments=(config.baseline_text,),
        raw_text=config.baseline_text,
        baseline_text=config.baseline_text,
        deadline_monotonic=refiner.deadline(),
        audio=loaded.snapshot,
    )
    result = refiner.refine(request)
    candidate_valid = result.status == "shadow_valid"
    candidate_differs = (
        result.candidate_text is not None
        and result.candidate_text != config.baseline_text
    )
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "probe": PROBE_NAME,
        "mode": "shadow",
        "privacy": {
            "audio_persisted": False,
            "transcripts_persisted": False,
            "input_path_emitted": False,
        },
        "audio": {
            "duration_seconds": round(loaded.snapshot.duration_seconds, 6),
            "sample_rate_hz": loaded.snapshot.sample_rate,
            "source_channels": loaded.source_channels,
            "source_sample_width_bits": loaded.source_sample_width_bits,
        },
        "refinement": {
            "status": result.status,
            "reason": result.reason,
            "backend": result.backend,
            "latency_ms": round(result.latency_ms, 3),
            "confidence": result.confidence,
            "candidate_kind": result.kind,
            "candidate_valid": candidate_valid,
            "candidate_differs_from_baseline": candidate_differs,
            "would_replace_if_active": candidate_valid and candidate_differs,
            "committed_text_source": "x_asr_baseline",
        },
    }
    if config.include_transcripts:
        report["transcripts"] = {
            "x_asr_baseline": {
                "role": "baseline_not_ground_truth",
                "text": config.baseline_text,
            },
            "local_model_candidate": {
                "role": "shadow_candidate",
                "text": result.candidate_text,
            },
        }
    return report


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run one read-only local semantic-refiner shadow probe. "
            "The X-ASR text is a baseline, not ground truth."
        )
    )
    parser.add_argument("--wav", required=True, type=Path, help="input PCM WAV path")
    parser.add_argument(
        "--baseline",
        required=True,
        help="X-ASR baseline text; this is not a ground-truth transcript",
    )
    parser.add_argument(
        "--endpoint",
        default=DEFAULT_ENDPOINT,
        help="loopback whisper.cpp /inference endpoint",
    )
    parser.add_argument("--language", default="auto", help="whisper language code")
    parser.add_argument("--timeout", type=float, default=6.0, help="timeout seconds")
    parser.add_argument(
        "--max-audio-seconds",
        type=float,
        default=20.0,
        help="reject longer WAVs before reading their samples",
    )
    parser.add_argument(
        "--metrics-only",
        action="store_true",
        help="omit both baseline and candidate transcript text from JSON",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    args = build_argument_parser().parse_args(argv)
    config = ProbeConfig(
        wav_path=args.wav,
        baseline_text=args.baseline,
        endpoint=args.endpoint,
        language=args.language,
        timeout_seconds=args.timeout,
        max_audio_seconds=args.max_audio_seconds,
        include_transcripts=not args.metrics_only,
    )
    try:
        report = run_probe(config)
    except Exception as exc:
        # Keep failures machine-readable while avoiding path, audio, or text in
        # diagnostics.  Detailed endpoint failures are represented by the
        # refiner's normal fallback report rather than escaping this block.
        error = {
            "schema_version": SCHEMA_VERSION,
            "probe": PROBE_NAME,
            "error": {
                "code": str(exc) if isinstance(exc, ValueError) else "probe_failed",
                "type": type(exc).__name__,
            },
        }
        print(json.dumps(error, ensure_ascii=False, sort_keys=True), file=stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, sort_keys=True), file=stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
