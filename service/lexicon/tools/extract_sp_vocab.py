#!/usr/bin/env python3
"""Extract a SentencePiece ``.vocab`` text file from a ``.model`` file.

This intentionally implements only the small protobuf subset used by
SentencePiece's ModelProto so the VoiceFly hotword experiment does not need
to install another package into the live ASR environment.
"""

from __future__ import annotations

import argparse
import struct
import unicodedata
from pathlib import Path


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while True:
        if pos >= len(data):
            raise ValueError("truncated protobuf varint")
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, pos
        shift += 7
        if shift >= 70:
            raise ValueError("invalid protobuf varint")


def _fields(data: bytes):
    pos = 0
    while pos < len(data):
        key, pos = _read_varint(data, pos)
        field_number = key >> 3
        wire_type = key & 7
        if wire_type == 0:
            value, pos = _read_varint(data, pos)
        elif wire_type == 1:
            value = data[pos : pos + 8]
            pos += 8
        elif wire_type == 2:
            size, pos = _read_varint(data, pos)
            value = data[pos : pos + size]
            pos += size
        elif wire_type == 5:
            value = data[pos : pos + 4]
            pos += 4
        else:
            raise ValueError(f"unsupported protobuf wire type: {wire_type}")
        if pos > len(data):
            raise ValueError("truncated protobuf field")
        yield field_number, wire_type, value


def _parse_piece(message: bytes) -> tuple[str, float, int]:
    piece = ""
    score = 0.0
    piece_type = 1
    for field_number, wire_type, value in _fields(message):
        if field_number == 1 and wire_type == 2:
            piece = value.decode("utf-8")
        elif field_number == 2 and wire_type == 5:
            score = struct.unpack("<f", value)[0]
        elif field_number == 3 and wire_type == 0:
            piece_type = value
    return piece, score, piece_type


def parse_model(path: Path) -> tuple[list[tuple[str, float, int]], int | None]:
    pieces: list[tuple[str, float, int]] = []
    model_type = None
    for field_number, wire_type, value in _fields(path.read_bytes()):
        if field_number == 1 and wire_type == 2:
            pieces.append(_parse_piece(value))
        elif field_number == 2 and wire_type == 2:
            for trainer_field, trainer_wire, trainer_value in _fields(value):
                if trainer_field == 3 and trainer_wire == 0:
                    model_type = trainer_value
    return pieces, model_type


def _is_han(char: str) -> bool:
    return unicodedata.name(char, "").startswith("CJK UNIFIED IDEOGRAPH")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("model", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--augment-cjk",
        action="store_true",
        help="append bare forms of single-Han ▁X pieces for sherpa BPE bootstrap",
    )
    args = parser.parse_args()

    pieces, model_type = parse_model(args.model)
    if not pieces:
        raise SystemExit("no SentencePiece entries found")
    augmented = 0
    if args.augment_cjk:
        existing = {piece for piece, _, _ in pieces}
        additions = []
        for piece, score, piece_type in pieces:
            if (
                piece.startswith("▁")
                and len(piece) == 2
                and _is_han(piece[1])
                and piece[1] not in existing
            ):
                additions.append((piece[1], score, piece_type))
                existing.add(piece[1])
        pieces.extend(additions)
        augmented = len(additions)
    lines = [f"{piece}\t{score:.9g}" for piece, score, _ in pieces]
    args.output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    names = {1: "unigram", 2: "bpe", 3: "word", 4: "char"}
    print(
        f"model_type={names.get(model_type, model_type)} "
        f"pieces={len(pieces)} augmented_cjk={augmented} output={args.output}"
    )


if __name__ == "__main__":
    main()
