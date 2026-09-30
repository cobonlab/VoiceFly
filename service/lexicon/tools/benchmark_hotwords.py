#!/usr/bin/env python3
"""Exercise VoiceFly's X-ASR model with several decoding configurations.

The script is intentionally independent of the installed/live VoiceFly
service.  Run one mode per process when comparing initialization time and RSS.
"""

from __future__ import annotations

import argparse
import json
import resource
import statistics
import time
import wave
from pathlib import Path

import numpy as np
import sherpa_onnx


DEFAULT_MODEL = (
    Path(__file__).resolve().parents[2]
    / "sherpa-onnx-x-asr-480ms-streaming-zipformer-transducer-zh-en-punct-"
    "int8-2026-06-05"
)


def make_recognizer(args: argparse.Namespace) -> sherpa_onnx.OnlineRecognizer:
    decoding_method = "greedy_search" if args.mode == "greedy" else "modified_beam_search"
    options = dict(
        tokens=str(args.model_dir / "tokens.txt"),
        encoder=str(args.model_dir / "encoder.int8.onnx"),
        decoder=str(args.model_dir / "decoder.onnx"),
        joiner=str(args.model_dir / "joiner.int8.onnx"),
        num_threads=args.num_threads,
        sample_rate=16000,
        feature_dim=80,
        decoding_method=decoding_method,
        max_active_paths=args.max_active_paths,
        debug=args.debug,
    )
    if args.mode == "hotwords-file":
        options.update(
            hotwords_file=str(args.hotwords_file),
            hotwords_score=args.hotwords_score,
            modeling_unit=args.modeling_unit,
            bpe_vocab=str(args.bpe_vocab),
        )
    elif args.mode == "hotwords-inline":
        # The model-level tokenizer is also required for per-stream phrases.
        options.update(
            hotwords_score=args.hotwords_score,
            modeling_unit=args.modeling_unit,
            bpe_vocab=str(args.bpe_vocab),
        )
    return sherpa_onnx.OnlineRecognizer.from_transducer(**options)


def load_audio(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as wav_file:
        sample_rate = wav_file.getframerate()
        channels = wav_file.getnchannels()
        sample_width = wav_file.getsampwidth()
        raw = wav_file.readframes(wav_file.getnframes())
    if sample_rate != 16000:
        raise ValueError(f"expected 16 kHz input, got {sample_rate}: {path}")
    if sample_width == 1:
        samples = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128) / 128
    elif sample_width == 2:
        samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768
    elif sample_width == 4:
        samples = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648
    else:
        raise ValueError(f"unsupported PCM sample width {sample_width}: {path}")
    samples = samples.reshape(-1, channels)[:, 0]
    return np.ascontiguousarray(samples), sample_rate


def decode(
    recognizer: sherpa_onnx.OnlineRecognizer,
    args: argparse.Namespace,
    wav: Path,
) -> dict:
    samples, sample_rate = load_audio(wav)
    if args.mode == "hotwords-inline":
        stream = recognizer.create_stream(args.inline_hotwords)
    else:
        stream = recognizer.create_stream()

    started = time.perf_counter()
    decode_calls = 0
    for start in range(0, len(samples), args.chunk_samples):
        stream.accept_waveform(sample_rate, samples[start : start + args.chunk_samples])
        while recognizer.is_ready(stream):
            recognizer.decode_stream(stream)
            decode_calls += 1
    stream.input_finished()
    while recognizer.is_ready(stream):
        recognizer.decode_stream(stream)
        decode_calls += 1
    elapsed = time.perf_counter() - started
    audio_seconds = len(samples) / sample_rate

    result = {
        "wav": str(wav),
        "audio_seconds": round(audio_seconds, 6),
        "decode_seconds": round(elapsed, 6),
        "rtf": round(elapsed / audio_seconds, 6),
        "decode_calls": decode_calls,
        "text": recognizer.get_result(stream),
        "tokens": recognizer.tokens(stream),
        "context_scores": recognizer.context_scores(stream),
    }
    if args.summary_only:
        result.pop("tokens")
        result.pop("context_scores")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        required=True,
        choices=("greedy", "beam", "hotwords-file", "hotwords-inline"),
    )
    parser.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--hotwords-file", type=Path)
    parser.add_argument("--bpe-vocab", type=Path)
    parser.add_argument("--modeling-unit", default="bpe")
    parser.add_argument("--inline-hotwords", default="")
    parser.add_argument("--hotwords-score", type=float, default=1.5)
    parser.add_argument("--max-active-paths", type=int, default=8)
    parser.add_argument("--num-threads", type=int, default=4)
    parser.add_argument("--chunk-samples", type=int, default=1600)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--summary-only", action="store_true")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("wavs", nargs="+", type=Path)
    args = parser.parse_args()

    if args.mode == "hotwords-file" and not args.hotwords_file:
        parser.error("--hotwords-file is required for hotwords-file mode")
    if args.mode.startswith("hotwords") and not args.bpe_vocab:
        parser.error("--bpe-vocab is required for hotword modes")
    if args.mode == "hotwords-inline" and not args.inline_hotwords:
        parser.error("--inline-hotwords is required for hotwords-inline mode")

    initialized = time.perf_counter()
    recognizer = make_recognizer(args)
    init_seconds = time.perf_counter() - initialized
    results = []
    for repeat_index in range(args.repeat):
        for wav in args.wavs:
            result = decode(recognizer, args, wav)
            result["repeat"] = repeat_index + 1
            results.append(result)
    total_audio = sum(item["audio_seconds"] for item in results)
    total_decode = sum(item["decode_seconds"] for item in results)
    report = {
        "sherpa_onnx_version": sherpa_onnx.__version__,
        "mode": args.mode,
        "decoding_method": "greedy_search"
        if args.mode == "greedy"
        else "modified_beam_search",
        "max_active_paths": args.max_active_paths,
        "hotwords_score": args.hotwords_score if args.mode.startswith("hotwords") else None,
        "modeling_unit": args.modeling_unit if args.mode.startswith("hotwords") else None,
        "init_seconds": round(init_seconds, 6),
        "max_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "summary": {
            "samples": len(results),
            "total_audio_seconds": round(total_audio, 6),
            "total_decode_seconds": round(total_decode, 6),
            "overall_rtf": round(total_decode / total_audio, 6),
            "median_sample_rtf": round(
                statistics.median(item["rtf"] for item in results), 6
            ),
        },
        "results": results,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
