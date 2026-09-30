#!/usr/bin/env python3
"""Build sherpa-onnx hotword phrases from a TSV/plain-text lexicon.

X-ASR's SentencePiece vocabulary represents Han characters as word-initial
pieces (for example ``▁一``).  Inserting spaces between Han characters before
SentencePiece encoding avoids ``<unk>`` spans such as ``般现在时``.
"""

from __future__ import annotations

import argparse
import json
import unicodedata
from pathlib import Path


def is_han(char: str) -> bool:
    return unicodedata.name(char, "").startswith("CJK UNIFIED IDEOGRAPH")


def separate_han(text: str) -> str:
    expanded = "".join(f" {char} " if is_han(char) else char for char in text)
    return " ".join(expanded.split())


def load_terms(path: Path, column: int) -> list[str]:
    terms = []
    seen = set()
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line or raw_line.startswith("#"):
            continue
        fields = raw_line.split("\t")
        index = column - 1 if len(fields) >= column else 0
        term = fields[index].strip()
        if term and term not in seen:
            seen.add(term)
            terms.append(term)
    return terms


def fmt_score(score: float) -> str:
    return str(int(score)) if score.is_integer() else f"{score:g}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--column",
        type=int,
        default=2,
        help="1-based canonical-term column for TSV input (default: 2)",
    )
    parser.add_argument("--bpe-model", type=Path)
    parser.add_argument("--rejects", type=Path)
    parser.add_argument(
        "--score",
        type=float,
        help="append an explicit per-phrase :score suffix",
    )
    parser.add_argument("--english-score-cap", type=float, default=2.5)
    parser.add_argument(
        "--case-variants",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="also emit capitalized-first form for lowercase pure-English terms",
    )
    args = parser.parse_args()

    terms = load_terms(args.input, args.column)
    accepted = []
    rejected = []
    processor = None
    if args.bpe_model:
        import sentencepiece as spm

        processor = spm.SentencePieceProcessor(model_file=str(args.bpe_model))

    accepted_set = set()
    for term in terms:
        has_han = any(is_han(char) for char in term)
        variants = [term]
        if args.case_variants and not has_han and term:
            capitalized = term[0].upper() + term[1:]
            if capitalized != term:
                variants.append(capitalized)
        for variant in variants:
            prepared = separate_han(variant)
            pieces = processor.encode(prepared, out_type=str) if processor else []
            ids = processor.encode(prepared, out_type=int) if processor else []
            if processor and processor.unk_id() in ids:
                rejected.append(
                    {"term": term, "prepared": prepared, "pieces": pieces}
                )
                continue
            line = prepared
            if args.score is not None:
                score = args.score if has_han else min(args.score, args.english_score_cap)
                line += " :" + fmt_score(score)
            if line not in accepted_set:
                accepted_set.add(line)
                accepted.append(line)

    args.output.write_text("\n".join(accepted) + "\n", encoding="utf-8")
    if args.rejects:
        args.rejects.write_text(
            json.dumps(rejected, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(
        json.dumps(
            {
                "input_terms": len(terms),
                "accepted_lines": len(accepted),
                "rejected": len(rejected),
                "output": str(args.output),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
