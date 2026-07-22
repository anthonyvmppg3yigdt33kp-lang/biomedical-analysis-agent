#!/usr/bin/env python3
"""Export a deterministic, derived-artifact-only Visium teaching snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
from typing import Any


CASE_DIR = Path(__file__).resolve().parent
ROOT = CASE_DIR.parent.parent
DEFAULT_RUN_ROOT = ROOT / "runs" / "visium-mouse-brain" / "canonical"
DEFAULT_FAULT_EVIDENCE = (
    ROOT
    / "runs"
    / "visium-mouse-brain"
    / "fault-before-completion-marker"
    / "fault-injection-evidence.json"
)
DEFAULT_OUTPUT_ROOT = CASE_DIR / "expected-output"
PRIVATE_PATH = re.compile(
    r"(?im)(?:^|[\s\"'=(:,])(?:[A-Z]:[\\/]|/(?:home|Users)/)"
)
STAGES = (
    "S10_INGEST",
    "S20_COORD_IMAGE_QC",
    "S30_UNIT_QC",
    "S40_PREPROCESS",
    "S60_CORE_DISCOVERY",
    "S80_ADVANCED/round-1",
    "S90_INFERENCE_QA",
    "S95_VISUALIZE_INTERPRET/round-1",
)


class ExportError(RuntimeError):
    """Raised when the canonical run cannot be published as teaching output."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ExportError(f"expected JSON object: {path.name}")
    return value


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def safe_relative(text: str) -> PurePosixPath:
    relative = PurePosixPath(text)
    if not relative.parts or relative.is_absolute() or ".." in relative.parts or relative.as_posix() != text:
        raise ExportError(f"unsafe output path: {text!r}")
    return relative


def ensure_no_private_path(path: Path) -> None:
    if path.suffix.lower() == ".png":
        return
    text = path.read_text(encoding="utf-8-sig")
    if PRIVATE_PATH.search(text):
        raise ExportError(f"private absolute path found in export candidate: {path.name}")


def validate_source(run_root: Path, fault_evidence: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    source_pipeline = CASE_DIR / "run_pipeline.R"
    executed_pipeline = run_root / "03_scripts" / "run_pipeline.R"
    if sha256_file(source_pipeline) != sha256_file(executed_pipeline):
        raise ExportError(
            "canonical run was not executed by the current candidate run_pipeline.R; "
            "a new fresh run is required before export"
        )
    summary = load_json(run_root / "manifest" / "execution-summary.json")
    if summary.get("case") != "visium-mouse-brain" or summary.get("status") != "DELIVERED":
        raise ExportError("canonical execution summary is not DELIVERED")
    if summary.get("sensitive_paths_included") is not False:
        raise ExportError("canonical execution summary is not explicitly sanitized")
    if summary.get("validation", {}).get("native_visual_review") != "passed":
        raise ExportError("canonical native visual review has not passed")

    review = load_json(run_root / "06_figures" / "review" / "review-round-1.json")
    if (
        review.get("overall_decision") != "keep"
        or review.get("reviewer_method") != "native_local_image_view"
        or review.get("opened_original_and_final") is not True
        or len(review.get("figure_reviews", [])) != 3
    ):
        raise ExportError("terminal native review is incomplete")

    fresh_log = (run_root / "logs" / "pipeline-fresh.log").read_text(encoding="utf-8-sig")
    for stage in STAGES:
        if f"\tstage_start\t{stage}" not in fresh_log or f"\tstage_checkpointed\t{stage}" not in fresh_log:
            raise ExportError(f"fresh-run log lacks a completed stage: {stage}")
    if "pipeline_complete_awaiting_native_review" not in fresh_log:
        raise ExportError("fresh-run log lacks its computational terminal state")

    resume = load_json(run_root / "logs" / "checkpoint-resume-reuse.json")
    cache = load_json(run_root / "logs" / "environment-cache-reuse.json")
    input_cache = load_json(run_root / "logs" / "input-cache-reuse.json")
    checksum_failure = load_json(run_root / "logs" / "corrupted-cache-negative-control.json")
    native_fresh = load_json(run_root / "logs" / "pipeline-fresh-native-exit-evidence.json")
    native_resume = load_json(run_root / "logs" / "pipeline-resume-native-exit-evidence.json")
    warning_evidence = load_json(run_root / "logs" / "pipeline-warnings.json")
    fault = load_json(fault_evidence)
    if not (
        resume.get("status") == "passed"
        and resume.get("all_checkpoints_reused") is True
        and resume.get("stage_start_observed") is False
        and cache.get("status") == "passed"
        and cache.get("reuse") is True
        and cache.get("fully_validated_before_evidence_write") is True
        and input_cache.get("status") == "passed"
        and input_cache.get("reuse") is True
        and input_cache.get("fully_validated_before_evidence_write") is True
        and input_cache.get("materialization") == "direct_read_no_copy"
        and input_cache.get("canonical_inputs_modified") is False
        and checksum_failure.get("status") == "passed"
        and checksum_failure.get("failure_closed") is True
        and checksum_failure.get("canonical_inputs_modified") is False
        and fault.get("status") == "passed"
        and fault.get("completion_marker_absent") is True
        and fault.get("canonical_run_modified") is False
        and native_fresh.get("status") == "passed"
        and native_fresh.get("native_returncode") == 0
        and native_fresh.get("forbidden_matches") == []
        and native_resume.get("status") == "passed"
        and native_resume.get("native_returncode") == 0
        and native_resume.get("forbidden_matches") == []
        and warning_evidence.get("status") == "passed"
        and warning_evidence.get("blocking_warning_occurrences") == 0
        and warning_evidence.get("code_hash") == sha256_file(executed_pipeline)
        and warning_evidence.get("analysis_config_hash")
        == sha256_file(run_root / "03_scripts" / "analysis-params.json")
        and warning_evidence.get("environment_lock_hash")
        == sha256_file(run_root / "02_environment" / "renv.lock")
    ):
        raise ExportError("one or more execution/resume/failure-closed gates did not pass")

    validator = ROOT / "scripts" / "validate_tutorial_ci_output.py"
    process = subprocess.run(
        [
            sys.executable,
            str(validator),
            "--case",
            "visium-mouse-brain",
            "--run-root",
            str(run_root),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if process.returncode != 0:
        raise ExportError("strict tutorial validator failed: " + process.stderr.strip())
    ci_report = json.loads(process.stdout)
    if ci_report.get("ok") is not True or ci_report.get("computational_execution") != "passed":
        raise ExportError("strict tutorial validator returned a non-passing report")

    gates = {
        "schema_version": "1.0",
        "case": "visium-mouse-brain",
        "status": "passed",
        "fresh_run": {
            "status": "passed",
            "all_stages_started_and_checkpointed": True,
            "pipeline_log_sha256": sha256_file(run_root / "logs" / "pipeline-fresh.log"),
        },
        "checkpoint_resume": {
            "status": "passed",
            "all_checkpoints_reused": True,
            "stage_start_observed": False,
            "evidence_sha256": sha256_file(run_root / "logs" / "checkpoint-resume-reuse.json"),
        },
        "environment_cache_reuse": {
            "status": "passed",
            "fully_validated_before_reuse": True,
            "evidence_sha256": sha256_file(run_root / "logs" / "environment-cache-reuse.json"),
        },
        "input_cache_reuse": {
            "status": "passed",
            "direct_read_no_copy": True,
            "fully_validated_before_reuse": True,
            "evidence_sha256": sha256_file(run_root / "logs" / "input-cache-reuse.json"),
        },
        "checksum_failure_injection": {
            "status": "passed",
            "canonical_inputs_modified": False,
            "evidence_sha256": sha256_file(run_root / "logs" / "corrupted-cache-negative-control.json"),
        },
        "pre_completion_fault_injection": {
            "status": "passed",
            "completion_marker_absent": True,
            "canonical_run_modified": False,
            "evidence_sha256": sha256_file(fault_evidence),
        },
        "native_r_shutdown": {
            "status": "passed",
            "fresh_native_returncode": 0,
            "resume_native_returncode": 0,
            "forbidden_matches": [],
            "fresh_evidence_sha256": sha256_file(run_root / "logs" / "pipeline-fresh-native-exit-evidence.json"),
            "resume_evidence_sha256": sha256_file(run_root / "logs" / "pipeline-resume-native-exit-evidence.json"),
        },
        "strict_ci_validator": {
            "status": "passed",
            "native_visual_review_asserted_by_this_gate": False,
            "native_review_is_asserted_by_terminal_review_record": True,
        },
        "runtime_warning_classification": {
            "status": "passed",
            "warning_occurrences": warning_evidence.get("warning_occurrences"),
            "unique_warning_records": warning_evidence.get("unique_warning_records"),
            "blocking_warning_occurrences": 0,
            "evidence_sha256": sha256_file(run_root / "logs" / "pipeline-warnings.json"),
            "scientific_parameters_changed": False,
        },
        "derived_artifacts_only": True,
    }
    return ci_report, gates


def role_for(relative: str) -> tuple[str, str]:
    if relative.startswith("05_results/tables/"):
        return "result_table", "data-verified"
    if relative.startswith("06_figures/original/") or relative.startswith("06_figures/final/"):
        return "figure", "native-reviewed"
    if relative.startswith("06_figures/review/"):
        return "native_visual_review", "native-reviewed"
    if relative.startswith("07_reports/"):
        return "report", "native-reviewed"
    if relative.startswith("manifest/"):
        return "manifest", "native-reviewed"
    if relative.startswith("validation/"):
        return "validation_evidence", "verified"
    return "provenance", "verified"


def export(args: argparse.Namespace) -> dict[str, Any]:
    run_root = args.run_root.resolve(strict=True)
    fault_evidence = args.fault_evidence.resolve(strict=True)
    output_root = args.output_root.resolve()
    if output_root.parent != CASE_DIR.resolve() or output_root.name != "expected-output":
        raise ExportError("output root must be this case's expected-output directory")
    if output_root.exists():
        raise ExportError("expected-output already exists; remove it only after preserving/reviewing the prior snapshot")

    ci_report, gates = validate_source(run_root, fault_evidence)
    source_files = {
        "05_results/tables/attrition.csv": run_root / "05_results/tables/attrition.csv",
        "05_results/tables/barcode_reconciliation.csv": run_root / "05_results/tables/barcode_reconciliation.csv",
        "05_results/tables/barcode_set_differences.csv": run_root / "05_results/tables/barcode_set_differences.csv",
        "05_results/tables/barcode_set_reconciliation.json": run_root / "05_results/tables/barcode_set_reconciliation.json",
        "05_results/tables/cluster_counts.csv": run_root / "05_results/tables/cluster_counts.csv",
        "05_results/tables/coordinate_image_qc.json": run_root / "05_results/tables/coordinate_image_qc.json",
        "05_results/tables/spot_qc.csv": run_root / "05_results/tables/spot_qc.csv",
        "06_figures/original/round-1/spatial_qc.png": run_root / "06_figures/original/round-1/spatial_qc.png",
        "06_figures/original/round-1/spatial_clusters.png": run_root / "06_figures/original/round-1/spatial_clusters.png",
        "06_figures/original/round-1/spatial_features_hpca_ttr.png": run_root / "06_figures/original/round-1/spatial_features_hpca_ttr.png",
        "06_figures/final/round-1/spatial_qc.png": run_root / "06_figures/final/round-1/spatial_qc.png",
        "06_figures/final/round-1/spatial_clusters.png": run_root / "06_figures/final/round-1/spatial_clusters.png",
        "06_figures/final/round-1/spatial_features_hpca_ttr.png": run_root / "06_figures/final/round-1/spatial_features_hpca_ttr.png",
        "06_figures/review/review-round-1.json": run_root / "06_figures/review/review-round-1.json",
        "06_figures/review/visual-params-round-1.json": run_root / "06_figures/review/visual-params-round-1.json",
        "06_figures/review/visual-review-state-round-1-reviewed.json": run_root / "06_figures/review/visual-review-state-round-1-reviewed.json",
        "07_reports/RESULTS.md": run_root / "07_reports/RESULTS.md",
        "07_reports/FIGURE_NOTES.md": run_root / "07_reports/FIGURE_NOTES.md",
        "07_reports/QA_REPORT.md": run_root / "07_reports/QA_REPORT.md",
        "manifest/execution-summary.json": run_root / "manifest/execution-summary.json",
        "manifest/run_manifest.json": run_root / "manifest/run_manifest.json",
        "validation/checkpoint-resume-reuse.json": run_root / "logs/checkpoint-resume-reuse.json",
        "validation/corrupted-cache-negative-control.json": run_root / "logs/corrupted-cache-negative-control.json",
        "validation/environment-cache-reuse.json": run_root / "logs/environment-cache-reuse.json",
        "validation/input-cache-reuse.json": run_root / "logs/input-cache-reuse.json",
        "validation/pipeline-fresh-native-exit-evidence.json": run_root / "logs/pipeline-fresh-native-exit-evidence.json",
        "validation/pipeline-resume-native-exit-evidence.json": run_root / "logs/pipeline-resume-native-exit-evidence.json",
        "validation/pipeline-warnings.json": run_root / "logs/pipeline-warnings.json",
        "validation/fault-before-completion-marker.json": fault_evidence,
        "provenance/input-manifest.json": CASE_DIR / "input-manifest.json",
        "provenance/DATA_LICENSE.md": CASE_DIR / "DATA_LICENSE.md",
    }
    missing = [relative for relative, source in source_files.items() if not source.is_file()]
    if missing:
        raise ExportError("source artifacts are missing: " + ", ".join(missing))

    output_root.mkdir(parents=False)
    for relative, source in source_files.items():
        safe_relative(relative)
        destination = output_root / Path(relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        ensure_no_private_path(destination)

    write_json(output_root / "validation" / "strict-ci-validation.json", ci_report)
    write_json(output_root / "validation" / "execution-gates.json", gates)

    execution_summary = load_json(output_root / "manifest" / "execution-summary.json")
    observed = execution_summary.get("observed", {})
    preprocessing = observed.get("preprocessing", {})
    directed_differences = observed.get(
        "directed_assay_image_coordinate_differences", {}
    )
    verification_summary = {
        "schema_version": "1.0.0",
        "case_id": "visium-mouse-brain",
        "status": "pass",
        "failures": [],
        "canonical_metrics": {
            "matrix_barcodes": observed.get("matrix_barcodes"),
            "vendor_all_positions": observed.get("vendor_all_positions"),
            "vendor_in_tissue_barcodes": observed.get("vendor_in_tissue_barcodes"),
            "loaded_spots": observed.get("loaded_spots"),
            "retained_spots": observed.get("retained_spots"),
            "expression_spot_clusters": observed.get("expression_spot_clusters"),
            "pca_dimensions": preprocessing.get("pca_dimensions"),
            "pca_non_finite_values": preprocessing.get("pca_non_finite_values"),
            "sct_non_finite_values": preprocessing.get("sct_non_finite_values"),
        },
        "barcode_reconciliation": {
            "directed_difference_counts": directed_differences,
            "nonzero_difference_count": sum(
                1 for value in directed_differences.values() if value != 0
            ),
        },
        "preprocessing": {
            "vst_flavor": preprocessing.get("vst_flavor"),
            "method": preprocessing.get("method"),
            "glmGamPoi_check": preprocessing.get("glmGamPoi_check"),
            "sct_variable_features": preprocessing.get("sct_variable_features"),
        },
        "cache_reuse": {
            "input_direct_read_no_copy": execution_summary.get("cache_reuse", {})
            .get("input", {})
            .get("direct_read_no_copy"),
            "environment_native_r_revalidation": execution_summary.get(
                "cache_reuse", {}
            )
            .get("environment", {})
            .get("current_root_native_r_revalidation"),
        },
        "negative_controls": {
            "checksum_failure_closed": True,
            "pre_completion_fault_failure_closed": True,
            "canonical_inputs_modified": False,
            "canonical_run_modified": False,
        },
        "native_visual_review_pass": True,
        "execution_evidence": {
            "run_pipeline_sha256": sha256_file(CASE_DIR / "run_pipeline.R"),
            "analysis_params_sha256": sha256_file(
                run_root / "03_scripts" / "analysis-params.json"
            ),
            "renv_lock_sha256": execution_summary.get("environment", {}).get(
                "renv_lock_sha256"
            ),
            "pipeline_warning_evidence_sha256": sha256_file(
                output_root / "validation" / "pipeline-warnings.json"
            ),
            "fresh_native_exit_evidence_sha256": sha256_file(
                output_root
                / "validation"
                / "pipeline-fresh-native-exit-evidence.json"
            ),
            "resume_native_exit_evidence_sha256": sha256_file(
                output_root
                / "validation"
                / "pipeline-resume-native-exit-evidence.json"
            ),
            "input_cache_reuse_evidence_sha256": sha256_file(
                output_root / "validation" / "input-cache-reuse.json"
            ),
            "environment_cache_reuse_evidence_sha256": sha256_file(
                output_root / "validation" / "environment-cache-reuse.json"
            ),
            "checksum_negative_control_sha256": sha256_file(
                output_root
                / "validation"
                / "corrupted-cache-negative-control.json"
            ),
            "pre_completion_fault_evidence_sha256": sha256_file(
                output_root
                / "validation"
                / "fault-before-completion-marker.json"
            ),
            "native_review_sha256": sha256_file(
                output_root / "06_figures" / "review" / "review-round-1.json"
            ),
            "strict_ci_validation_sha256": sha256_file(
                output_root / "validation" / "strict-ci-validation.json"
            ),
            "execution_gates_sha256": sha256_file(
                output_root / "validation" / "execution-gates.json"
            ),
        },
    }
    write_json(
        output_root / "manifest" / "verification-summary.json",
        verification_summary,
    )

    ledger_records: list[dict[str, Any]] = []
    ledger_candidates = sorted(
        path
        for path in output_root.rglob("*")
        if path.is_file() and path.name not in {"artifact_ledger.jsonl", "expected-output-manifest.json"}
    )
    for path in ledger_candidates:
        relative = path.relative_to(output_root).as_posix()
        role, maturity = role_for(relative)
        ledger_records.append(
            {
                "artifact_id": "public-visium-" + re.sub(r"[^a-z0-9]+", "-", relative.lower()).strip("-"),
                "path": relative,
                "sha256": sha256_file(path),
                "size_bytes": path.stat().st_size,
                "role": role,
                "maturity": maturity,
                "validation": "passed",
            }
        )
    ledger_path = output_root / "manifest" / "artifact_ledger.jsonl"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(
        "".join(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in ledger_records),
        encoding="utf-8",
        newline="\n",
    )

    index_lines = [
        "# Sanitized artifact index",
        "",
        "This index covers only distributed, derived teaching artifacts. Raw inputs, R objects and runtime libraries are excluded.",
        "",
        "| Artifact | Role | Maturity | SHA-256 | Size (bytes) |",
        "|---|---|---|---|---:|",
    ]
    for record in ledger_records:
        index_lines.append(
            f"| `{record['path']}` | {record['role']} | {record['maturity']} | "
            f"`{record['sha256']}` | {record['size_bytes']} |"
        )
    index_lines.append("")
    (output_root / "07_reports" / "ARTIFACT_INDEX.md").write_text(
        "\n".join(index_lines), encoding="utf-8", newline="\n"
    )

    readme = """# Verified Visium Mouse Brain teaching output

This directory is a deterministic snapshot from the real Seurat 5.5.0 Mouse Brain Sagittal-Anterior run. It contains only compliant derived tables, reports, original/final PNG pairs, terminal native-review evidence, and sanitized validation/provenance records.

Observed in this single-section descriptive run: 2,695 matrix/in-tissue/loaded/retained spots, zero across all six directed assay/image/coordinate barcode differences, and 11 expression-derived spot clusters. These clusters are not cell types or population-level effects.

The 10x input files, Seurat R object, task-local renv library, binaries, caches and checkpoints are deliberately not distributed. Data attribution and frozen downloader hashes are recorded under `provenance/`; the original 10x data remain CC BY 4.0 and are not relicensed by the repository MIT license.

The snapshot can only be exported from a fresh run of the exact current candidate code. Its structured warning ledger must be bound to the executed code/config/environment and contain zero release blockers; unknown, API, numerical and spatial-integrity warnings fail closed.

From the repository root, verify the exact inventory, hashes, PNG containers/dimensions, barcode reconciliation, native-review bindings, failure injections and path sanitization with:

```powershell
python examples/visium-mouse-brain/verify_expected_output.py
```
"""
    (output_root / "README.md").write_text(readme, encoding="utf-8", newline="\n")

    artifacts = []
    for path in sorted(path for path in output_root.rglob("*") if path.is_file()):
        if path.name == "expected-output-manifest.json":
            continue
        relative = path.relative_to(output_root).as_posix()
        artifacts.append(
            {"path": relative, "sha256": sha256_file(path), "size_bytes": path.stat().st_size}
        )
    manifest = {
        "schema_version": "1.0",
        "case": "visium-mouse-brain",
        "dataset_id": "10x-V1-Mouse-Brain-Sagittal-Anterior",
        "status": "verified_public_teaching_snapshot",
        "distribution_profile": "derived_artifacts_only",
        "terminal_native_review": "keep",
        "manifest_self_excluded": True,
        "artifacts": artifacts,
        "excluded_payload_classes": [
            "raw_10x_inputs",
            "analysis_objects",
            "task_local_environment",
            "runtime_binaries",
            "cache_and_checkpoints",
        ],
    }
    write_json(output_root / "expected-output-manifest.json", manifest)

    verifier = CASE_DIR / "verify_expected_output.py"
    verified = subprocess.run(
        [sys.executable, str(verifier), "--output-root", str(output_root)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if verified.returncode != 0:
        raise ExportError("independent expected-output verification failed: " + verified.stderr.strip())
    return json.loads(verified.stdout)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, default=DEFAULT_RUN_ROOT)
    parser.add_argument("--fault-evidence", type=Path, default=DEFAULT_FAULT_EVIDENCE)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = export(args)
    except (OSError, json.JSONDecodeError, ExportError) as exc:
        sys.stderr.write(f"EXPECTED_OUTPUT_EXPORT_FAILED: {exc}\n")
        return 2
    sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
