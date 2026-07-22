#!/usr/bin/env python3
"""Verify the CLL MOFA-FLEX run before native visual review."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_root", type=Path)
    args = parser.parse_args()
    root = args.run_root.resolve()
    errors: list[str] = []
    manifest_path = root / "manifest" / "run_manifest.json"
    if not manifest_path.is_file():
        errors.append("missing_run_manifest")
        manifest = {}
    else:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("current_state") != "NATIVE_VISUAL_REVIEW":
            errors.append(f"unexpected_state:{manifest.get('current_state')}")
        if [row.get("stage_id") for row in manifest.get("checkpoints", [])] != ["03-methodology-review", "04-multi-omics", "05-analysis-qa", "06-interpretation"]:
            errors.append("checkpoint_sequence_mismatch")

    ledger_count = 0
    ledger_path = root / "manifest" / "artifact_ledger.jsonl"
    if not ledger_path.is_file():
        errors.append("missing_artifact_ledger")
    else:
        for line in ledger_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            ledger_count += 1
            row = json.loads(line)
            path = root / Path(row["path"])
            if not path.is_file():
                errors.append(f"missing_ledger_artifact:{row['path']}")
            elif sha256(path) != row["sha256"]:
                errors.append(f"ledger_hash_mismatch:{row['path']}")

    methodology_path = root / "04_intermediate" / "03-methodology-review" / "methodology_summary.json"
    stability_path = root / "04_intermediate" / "04-multi-omics" / "stability_summary.json"
    if not methodology_path.is_file() or not stability_path.is_file():
        errors.append("missing_scientific_summary")
        methodology, stability = {}, {}
    else:
        methodology = json.loads(methodology_path.read_text(encoding="utf-8"))
        stability = json.loads(stability_path.read_text(encoding="utf-8"))
        if methodology.get("patient_union") != 200 or methodology.get("complete_all_views") != 121:
            errors.append("patient_set_contract_failed")
        expected = {"matched_factors": 6, "high_count": 5, "moderate_count": 1, "low_count": 0}
        if any(stability.get(key) != value for key, value in expected.items()):
            errors.append("stability_count_contract_failed")
        if abs(float(stability.get("median_absolute_score_rho", 0)) - 0.9799467486687168) > 1e-9:
            errors.append("stability_metric_drift")

    originals = sorted((root / "06_figures" / "original").glob("*.png"))
    candidates = sorted((root / "06_figures" / "final-revision-2").glob("*.png"))
    if len(originals) != 7 or len(candidates) != 7:
        errors.append(f"figure_count:original={len(originals)}:candidate={len(candidates)}")
    result = {
        "status": "PASS_PENDING_NATIVE_VISUAL_REVIEW" if not errors else "FAIL",
        "case_id": "p0-multiomics-cll-mofa",
        "ledger_artifacts": ledger_count,
        "patient_union": methodology.get("patient_union"),
        "complete_all_views": methodology.get("complete_all_views"),
        "stability": stability,
        "errors": errors,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
