#!/usr/bin/env python3
"""Verify machine-checkable outputs before native visual review."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


EXPECTED_STAGES = [
    "P10_input_audit", "P20_preprocessing", "P30_primary_limma_no_imputation",
    "P40_dep_minprob_sensitivity", "P50_figures", "P60_analysis_qa",
]


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
        stages = [item.get("stage_id") for item in manifest.get("checkpoints", [])]
        if stages != EXPECTED_STAGES:
            errors.append(f"checkpoint_sequence:{stages}")
        if not all(item.get("validated") is True for item in manifest.get("checkpoints", [])):
            errors.append("unvalidated_checkpoint")

    ledger_path = root / "manifest" / "artifact_ledger.jsonl"
    ledger_count = 0
    if not ledger_path.is_file():
        errors.append("missing_artifact_ledger")
    else:
        for line in ledger_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            ledger_count += 1
            record = json.loads(line)
            path = root / Path(record["path"])
            if not path.is_file():
                errors.append(f"missing_ledger_artifact:{record['path']}")
            elif sha256(path) != record["sha256"]:
                errors.append(f"ledger_hash_mismatch:{record['path']}")

    qa_path = root / "04_intermediate" / "P60_analysis_qa" / "analysis_qa_summary.json"
    if not qa_path.is_file():
        errors.append("missing_analysis_qa_summary")
        qa = {}
    else:
        qa = json.loads(qa_path.read_text(encoding="utf-8"))
        if qa.get("status") != "PASS_PENDING_NATIVE_VISUAL_REVIEW":
            errors.append(f"qa_status:{qa.get('status')}")
        if qa.get("high_severity_scientific_errors") != 0:
            errors.append("high_severity_scientific_error")
        expected_packages = {"DEP": "1.31.0", "QFeatures": "1.20.0", "limma": "3.66.0", "Rcpp": "1.1.1"}
        if qa.get("session", {}).get("packages") != expected_packages:
            errors.append("runtime_package_versions_mismatch")

    figure_root = root / "04_intermediate" / "P50_figures"
    originals = sorted((figure_root / "original").glob("*.png"))
    finals = sorted((figure_root / "final").glob("*.png"))
    if len(originals) != 4 or len(finals) != 4:
        errors.append(f"figure_count:original={len(originals)}:final={len(finals)}")
    result = {
        "status": "PASS_PENDING_NATIVE_VISUAL_REVIEW" if not errors else "FAIL",
        "case_id": "p0-proteomics-dep-ubilength",
        "ledger_artifacts": ledger_count,
        "checkpoints": len(manifest.get("checkpoints", [])),
        "primary_calls": qa.get("primary_significant_calls"),
        "errors": errors,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
