#!/usr/bin/env python3
"""Validate a protein-by-sample matrix and sample metadata without third-party packages."""

from __future__ import annotations

import argparse
import csv
import math
from pathlib import Path


def delimiter(path: Path) -> str:
    return "," if path.suffix.lower() == ".csv" else "\t"


def read_rows(path: Path) -> tuple[list[str], list[list[str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle, delimiter=delimiter(path)))
    if not rows:
        raise ValueError(f"empty file: {path}")
    width = len(rows[0])
    if width < 2 or any(len(row) != width for row in rows[1:]):
        raise ValueError(f"ragged or underspecified table: {path}")
    return rows[0], rows[1:]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--matrix", required=True, type=Path, help="Feature rows; first column feature ID; remaining columns samples")
    parser.add_argument("--metadata", required=True, type=Path)
    parser.add_argument("--sample-column", default="sample_id")
    parser.add_argument("--scale", choices=("raw-intensity", "log2"), default="raw-intensity")
    parser.add_argument("--max-missing-fraction", type=float, default=1.0)
    args = parser.parse_args()
    if not 0 <= args.max_missing_fraction <= 1:
        raise SystemExit("--max-missing-fraction must be within [0, 1]")

    m_header, m_rows = read_rows(args.matrix)
    d_header, d_rows = read_rows(args.metadata)
    if len(set(m_header)) != len(m_header):
        raise SystemExit("matrix has duplicate column names")
    if args.sample_column not in d_header:
        raise SystemExit(f"metadata lacks sample column: {args.sample_column}")
    sample_idx = d_header.index(args.sample_column)
    matrix_samples = m_header[1:]
    metadata_samples = [row[sample_idx].strip() for row in d_rows]
    if any(not x for x in metadata_samples) or len(set(metadata_samples)) != len(metadata_samples):
        raise SystemExit("metadata sample IDs must be non-empty and unique")
    if set(matrix_samples) != set(metadata_samples):
        missing_meta = sorted(set(matrix_samples) - set(metadata_samples))
        missing_matrix = sorted(set(metadata_samples) - set(matrix_samples))
        raise SystemExit(f"sample mismatch; matrix-only={missing_meta}, metadata-only={missing_matrix}")

    feature_ids: set[str] = set()
    missing = 0
    total = 0
    for line_no, row in enumerate(m_rows, start=2):
        feature = row[0].strip()
        if not feature or feature in feature_ids:
            raise SystemExit(f"feature IDs must be non-empty and unique (line {line_no})")
        feature_ids.add(feature)
        for value in row[1:]:
            text = value.strip()
            total += 1
            if text == "" or text.upper() in {"NA", "NAN", "NULL"}:
                missing += 1
                continue
            try:
                number = float(text)
            except ValueError as exc:
                raise SystemExit(f"non-numeric intensity at line {line_no}: {text!r}") from exc
            if not math.isfinite(number):
                raise SystemExit(f"intensity must be finite at line {line_no}")
            if args.scale == "raw-intensity" and number < 0:
                raise SystemExit(f"raw intensity must be non-negative at line {line_no}")

    fraction = missing / total if total else 0.0
    if fraction > args.max_missing_fraction:
        raise SystemExit(f"missing fraction {fraction:.4f} exceeds limit {args.max_missing_fraction:.4f}")
    print(f"OK features={len(feature_ids)} samples={len(matrix_samples)} scale={args.scale} missing_fraction={fraction:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
