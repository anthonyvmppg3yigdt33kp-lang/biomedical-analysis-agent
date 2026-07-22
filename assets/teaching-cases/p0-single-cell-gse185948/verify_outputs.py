#!/usr/bin/env python3
"""Validate the GSE185948 reduced teaching output without analysis dependencies."""

from __future__ import annotations

import argparse
import json
import struct
from pathlib import Path


REQUIRED_OUTPUTS = {
    "tables/input-integrity.tsv",
    "tables/nucleus-qc.tsv",
    "tables/donor-qc-thresholds.tsv",
    "tables/integration-diagnostics.tsv",
    "tables/annotation-evidence.tsv",
    "tables/embedding.tsv",
    "objects/processed-selected-feature-log1p.h5ad",
    "figures/qc-distributions-by-donor.png",
    "figures/embedding-donor-cluster.png",
    "figures/donor-mixing-diagnostics.png",
    "figures/coarse-annotation-umap.png",
    "figures/donor-composition-descriptive.png",
    "figures/marker-panel-heatmap.png",
    "reports/qa-machine.json",
    "reports/doublet-decisions.json",
    "reports/memory-safety.json",
    "reports/integration-diagnostics.json",
    "reports/input-profile.json",
    "reports/sampling-manifest.json",
    "reports/annotation-summary.json",
    "reports/QA_REPORT.md",
    "reports/FIGURE_NOTES.md",
    "reports/artifact-index.json",
}


def png_dimensions(path: Path) -> tuple[int, int]:
    with path.open("rb") as handle:
        header = handle.read(24)
    if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise ValueError(f"Invalid PNG: {path}")
    return struct.unpack(">II", header[16:24])


def verify_pipeline_output(output: Path) -> dict[str, object]:
    output = output.resolve()
    errors: list[str] = []
    missing = sorted(relative for relative in REQUIRED_OUTPUTS if not (output / relative).is_file())
    errors.extend(f"missing:{relative}" for relative in missing)
    if errors:
        return {"ok": False, "output": str(output), "errors": errors}

    qa = json.loads((output / "reports" / "qa-machine.json").read_text(encoding="utf-8-sig"))
    profile = json.loads((output / "reports" / "input-profile.json").read_text(encoding="utf-8-sig"))
    sampling = json.loads((output / "reports" / "sampling-manifest.json").read_text(encoding="utf-8-sig"))
    annotation = json.loads((output / "reports" / "annotation-summary.json").read_text(encoding="utf-8-sig"))
    if qa.get("ok") is not True:
        errors.append("qa-machine:not-ok")
    expected_profile = {
        "case_id": "p0-single-cell-gse185948",
        "analysis_mode": "reduced-real-data-teaching-fixture",
        "measurement_unit": "nucleus",
        "total_filtered_matrix_barcodes": 56728,
        "analyzed_input_barcodes": 5000,
        "full_data_analyzed": False,
        "case_control_estimand_available": False,
    }
    for key, expected in expected_profile.items():
        if profile.get(key) != expected:
            errors.append(f"input-profile:{key}:expected={expected!r}:observed={profile.get(key)!r}")
    if profile.get("donors") != ["cont1", "cont2", "cont3", "cont4", "cont5"]:
        errors.append("input-profile:donor-order")
    input_files = profile.get("input_files", [])
    if len(input_files) != 5 or any(item.get("features") != 36601 for item in input_files):
        errors.append("input-profile:five-36601-feature-inputs-required")
    if any(item.get("source_mode") != "read_only" for item in input_files):
        errors.append("input-profile:source-not-read-only")
    if sampling.get("mode") != "reduced-real-data-teaching-fixture":
        errors.append("sampling:mode")
    if sampling.get("full_matrix_loaded_before_sampling") is not False:
        errors.append("sampling:full-matrix-loaded")
    seeds = [item.get("seed") for item in sampling.get("donors", [])]
    if seeds != [20260719, 20260720, 20260721, 20260722, 20260723]:
        errors.append("sampling:donor-seeds")
    if annotation.get("clusters") != 15:
        errors.append("annotation:cluster-count")
    if annotation.get("reference_mapping_performed") is not False:
        errors.append("annotation:unexpected-reference-mapping")
    if qa.get("doublet_filtering_applied") is not False:
        errors.append("qa:unexpected-doublet-filter")
    if qa.get("integration_method") != "no integration; unintegrated PCA retained after confounding review":
        errors.append("qa:integration-contract")

    dimensions: dict[str, list[int]] = {}
    for path in sorted((output / "figures").glob("*.png")):
        width, height = png_dimensions(path)
        dimensions[path.name] = [width, height]
        if width < 900 or height < 600:
            errors.append(f"figure-too-small:{path.name}:{width}x{height}")
    if len(dimensions) != 6:
        errors.append(f"figure-count:expected=6:observed={len(dimensions)}")

    return {
        "ok": not errors,
        "output": str(output),
        "errors": errors,
        "metrics": {
            "donors": len(profile.get("donors", [])),
            "total_filtered_matrix_barcodes": profile.get("total_filtered_matrix_barcodes"),
            "analyzed_input_barcodes": profile.get("analyzed_input_barcodes"),
            "retained_nuclei": profile.get("retained_nuclei"),
            "selected_features": profile.get("selected_features"),
            "clusters": annotation.get("clusters"),
            "figures": len(dimensions),
            "qa_status": qa.get("qa_status"),
        },
        "figure_dimensions": dimensions,
        "scientific_boundary": qa.get("claim_ceiling"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pipeline-output", type=Path, required=True)
    args = parser.parse_args()
    report = verify_pipeline_output(args.pipeline_output)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

