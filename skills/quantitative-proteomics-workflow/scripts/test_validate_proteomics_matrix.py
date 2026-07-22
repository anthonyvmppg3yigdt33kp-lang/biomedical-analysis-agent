#!/usr/bin/env python3
"""Smoke tests for validate_proteomics_matrix.py."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path


SCRIPT = Path(__file__).with_name("validate_proteomics_matrix.py")


def run_case(matrix: str, metadata: str, expected: int, extra: tuple[str, ...] = ()) -> str:
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        matrix_path = root / "matrix.tsv"
        metadata_path = root / "metadata.tsv"
        matrix_path.write_text(matrix, encoding="utf-8")
        metadata_path.write_text(metadata, encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--matrix", str(matrix_path), "--metadata", str(metadata_path), *extra],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != expected:
            raise AssertionError(f"expected {expected}, got {result.returncode}: {result.stdout}{result.stderr}")
        return result.stdout + result.stderr


def main() -> int:
    ok = run_case(
        "protein_id\ts1\ts2\np1\t100\tNA\np2\t50\t60\n",
        "sample_id\tcondition\ns2\tB\ns1\tA\n",
        0,
    )
    assert "OK features=2 samples=2 scale=raw-intensity missing_fraction=0.2500" in ok
    mismatch = run_case(
        "protein_id\ts1\ts2\np1\t100\t90\n",
        "sample_id\tcondition\ns1\tA\ns3\tB\n",
        1,
    )
    assert "sample mismatch" in mismatch
    negative = run_case(
        "protein_id\ts1\np1\t-1\n",
        "sample_id\tcondition\ns1\tA\n",
        1,
    )
    assert "non-negative" in negative
    log2_ok = run_case(
        "protein_id\ts1\np1\t-1\n",
        "sample_id\tcondition\ns1\tA\n",
        0,
        ("--scale", "log2"),
    )
    assert "scale=log2" in log2_ok
    print("4 smoke tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
