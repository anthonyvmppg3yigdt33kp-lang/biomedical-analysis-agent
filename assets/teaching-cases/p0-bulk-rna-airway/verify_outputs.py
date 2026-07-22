#!/usr/bin/env python3
"""Validate a completed p0-bulk-rna-airway teaching-case output."""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import struct
from pathlib import Path
from typing import TextIO


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def open_text(path: Path) -> TextIO:
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8", newline="")
    return path.open("r", encoding="utf-8-sig", newline="")


def read_tsv(path: Path) -> list[dict[str, str]]:
    with open_text(path) as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def png_dimensions(path: Path) -> tuple[int, int]:
    with path.open("rb") as handle:
        header = handle.read(24)
    if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"not_png:{path}")
    return struct.unpack(">II", header[16:24])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()
    root = args.output_dir.resolve()
    errors: list[str] = []

    required = [
        "STATUS_COMPLETE.txt",
        "tables/sample_metadata.tsv",
        "tables/design_matrix.tsv",
        "tables/design_rank_check.tsv",
        "tables/gene_filter_all_features.tsv.gz",
        "tables/deseq2_results_trt_vs_untrt.tsv.gz",
        "tables/ranked_gene_vector_full_tested_universe.tsv.gz",
        "tables/normalized_counts.tsv.gz",
        "tables/pca_scores.tsv",
        "tables/sample_distance_matrix.tsv",
        "tables/dispersion_estimates.tsv.gz",
        "tables/results_summary.tsv",
        "tables/reproducibility_checks.tsv",
        "objects/airway_official_object.rds",
        "objects/deseq2_fitted_dataset.rds",
        "objects/vst_object.rds",
        "reports/SCIENTIFIC_BOUNDARIES.md",
        "reports/QA_REPORT.md",
        "reports/FIGURE_NOTES.md",
        "provenance/sessionInfo.txt",
        "provenance/citation_airway.txt",
        "provenance/citation_DESeq2.txt",
        "provenance/direct_package_versions.tsv",
        "provenance/method_contract.txt",
        "provenance/artifact_manifest.tsv",
    ]
    for relative in required:
        path = root / relative
        if not path.is_file() or path.stat().st_size == 0:
            errors.append(f"missing_or_empty:{relative}")

    original_pngs = sorted((root / "figures" / "original").glob("*.png"))
    final_pngs = sorted((root / "figures" / "final").glob("*.png"))
    if len(original_pngs) != 6:
        errors.append(f"original_png_count:{len(original_pngs)}")
    if len(final_pngs) != 6:
        errors.append(f"final_png_count:{len(final_pngs)}")
    for path in [*original_pngs, *final_pngs]:
        try:
            if png_dimensions(path) != (2400, 1800):
                errors.append(f"unexpected_png_dimensions:{path.relative_to(root).as_posix()}")
        except (OSError, ValueError) as exc:
            errors.append(str(exc))

    if not errors:
        metadata = read_tsv(root / "tables" / "sample_metadata.tsv")
        if len(metadata) != 8:
            errors.append(f"sample_count:{len(metadata)}")
        pair_counts: dict[tuple[str, str], int] = {}
        for row in metadata:
            key = (row.get("cell", ""), row.get("dex", ""))
            pair_counts[key] = pair_counts.get(key, 0) + 1
        if len({key[0] for key in pair_counts}) != 4 or any(value != 1 for value in pair_counts.values()):
            errors.append("paired_cell_line_structure_invalid")

        design = read_tsv(root / "tables" / "design_rank_check.tsv")
        if len(design) != 1 or design[0].get("formula") != "~ cell + dex":
            errors.append("design_formula_invalid")
        if not design or design[0].get("contrast") != "dex: trt - untrt":
            errors.append("contrast_invalid")
        if not design or design[0].get("full_rank", "").upper() != "TRUE":
            errors.append("design_not_full_rank")

        summary = {row["metric"]: int(float(row["value"])) for row in read_tsv(root / "tables" / "results_summary.tsv")}
        if summary.get("raw_features") != 63677:
            errors.append(f"raw_feature_count:{summary.get('raw_features')}")
        if not 0 < summary.get("wald_tested_universe", 0) <= summary.get("retained_after_prefilter", 0):
            errors.append("tested_universe_invalid")

        ranked_count = len(read_tsv(root / "tables" / "ranked_gene_vector_full_tested_universe.tsv.gz"))
        if ranked_count != summary.get("wald_tested_universe"):
            errors.append(f"ranked_universe_mismatch:{ranked_count}")
        qa = read_tsv(root / "tables" / "reproducibility_checks.tsv")
        if not qa or any(row.get("passed", "").upper() != "TRUE" for row in qa):
            errors.append("reproducibility_check_failed")

        versions = {row["package"]: row["version"] for row in read_tsv(root / "provenance" / "direct_package_versions.tsv")}
        expected_versions = {
            "airway": "1.30.0", "DESeq2": "1.50.2", "BiocVersion": "3.22.0",
            "BiocManager": "1.30.27", "matrixStats": "1.5.0",
        }
        if versions != expected_versions:
            errors.append(f"direct_versions_mismatch:{versions}")

        boundaries = (root / "reports" / "SCIENTIFIC_BOUNDARIES.md").read_text(encoding="utf-8")
        for phrase in ("four paired", "No patient benefit", "causality"):
            if phrase not in boundaries:
                errors.append(f"boundary_phrase_missing:{phrase}")

        manifest_rows = read_tsv(root / "provenance" / "artifact_manifest.tsv")
        declared = {row["relative_path"]: row for row in manifest_rows}
        actual = {
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if path.is_file()
            and path.relative_to(root).as_posix() not in {
                "provenance/artifact_manifest.tsv", "STATUS_COMPLETE.txt"
            }
        }
        if set(declared) != actual:
            errors.append("artifact_manifest_inventory_mismatch")
        for relative, row in declared.items():
            path = root / relative
            if not path.is_file():
                continue
            if int(row["bytes"]) != path.stat().st_size:
                errors.append(f"artifact_size_mismatch:{relative}")
            if row["sha256"].lower() != sha256_file(path):
                errors.append(f"artifact_hash_mismatch:{relative}")

    result = {
        "status": "PASS" if not errors else "FAIL",
        "case_id": "p0-bulk-rna-airway",
        "output_dir": str(root),
        "errors": errors,
        "original_pngs": len(original_pngs),
        "final_pngs": len(final_pngs),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
