#!/usr/bin/env python3
"""Verify the committed PBMC3K public expected-output teaching package."""

from __future__ import annotations

import csv
import hashlib
import json
import re
import struct
import sys
from pathlib import Path
from typing import Any

from case_driver import FIGURES


CASE_DIR = Path(__file__).resolve().parent
OUTPUT = CASE_DIR / "expected-output"
EXPECTED = {"input_cells": 2700, "qc_retained_cells": 2638, "clusters": 9}
INPUT_SHA256 = "847d6ebd9a1ec9a768f2be7e40ca42cbfe75ebeb6d76a4c24167041699dc28b5"
INPUT_SIZE = 7_621_991
TEXT_SUFFIXES = {".csv", ".json", ".jsonl", ".md", ".txt"}
FORBIDDEN_SUFFIXES = {".dll", ".h5", ".h5ad", ".rda", ".rds", ".zip"}
FORBIDDEN_PARTS = {"02_environment", "04_intermediate", "cache", "raw", "runtime"}
WINDOWS_ABSOLUTE = re.compile(r"(?i)(?:(?<![a-z0-9])[a-z]:[\\/]|\\\\[^\\/\s]+[\\/])")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _png_dimensions(path: Path) -> tuple[int, int]:
    header = path.read_bytes()[:24]
    if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise ValueError(f"invalid PNG header: {path.name}")
    return struct.unpack(">II", header[16:24])


def _required_paths() -> set[str]:
    paths = {
        "README.md",
        "ARTIFACT_INDEX.md",
        "tables/canonical_metrics.csv",
        "tables/feature_name_mapping.csv",
        "tables/feature_name_mapping_summary.csv",
        "tables/umap_runtime_contract.json",
        "tables/qc_summary.csv",
        "tables/cluster_sizes.csv",
        "tables/annotation_evidence.csv",
        "tables/cluster_markers.csv",
        "reports/RESULTS.md",
        "reports/QA_REPORT.md",
        "reports/FIGURE_NOTES.md",
        "manifest/artifact_ledger.jsonl",
        "manifest/environment-cache-reuse.json",
        "manifest/environment-process-evidence.json",
        "manifest/execution-summary.json",
        "manifest/input-evidence.json",
        "manifest/input-manifest.json",
        "manifest/run-manifest.json",
        "manifest/r-pipeline-process-evidence.json",
        "manifest/source-run.json",
        "manifest/verification-summary.json",
    }
    for figure_id in FIGURES:
        paths.update(
            {
                f"figures/original/{figure_id}.png",
                f"figures/final/{figure_id}.png",
                f"figures/review/{figure_id}.review.json",
            }
        )
    return paths


def verify_output(root: Path = OUTPUT) -> dict[str, Any]:
    failures: list[str] = []
    umap_runtime: dict[str, Any] = {}
    if not root.is_dir():
        return {"case_id": "pbmc3k", "failures": ["expected-output directory is missing"], "status": "fail"}

    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
    }
    required = _required_paths()
    for relative in sorted(required - actual):
        failures.append(f"required artifact missing: {relative}")
    for relative in sorted(actual - required):
        failures.append(f"unexpected artifact in allow-listed export: {relative}")

    for relative in sorted(actual):
        path = root / relative
        if path.stat().st_size == 0:
            failures.append(f"empty artifact: {relative}")
        lowered_parts = {part.lower() for part in Path(relative).parts}
        if lowered_parts & FORBIDDEN_PARTS:
            failures.append(f"forbidden path: {relative}")
        suffixes = {suffix.lower() for suffix in path.suffixes}
        if suffixes & FORBIDDEN_SUFFIXES or path.name.lower().endswith(".tar.gz"):
            failures.append(f"forbidden payload type: {relative}")
        if path.suffix.lower() in TEXT_SUFFIXES:
            try:
                text = path.read_text(encoding="utf-8-sig")
            except UnicodeDecodeError:
                failures.append(f"text artifact is not UTF-8: {relative}")
                continue
            if WINDOWS_ABSOLUTE.search(text):
                failures.append(f"absolute Windows path leaked: {relative}")
            if re.search(r"(?i)(?:/users/|/home/)[^\s/]+/", text):
                failures.append(f"absolute home path leaked: {relative}")

    try:
        summary = _read_json(root / "manifest/execution-summary.json")
        if summary.get("canonical_metrics") != EXPECTED:
            failures.append("execution-summary canonical metrics mismatch")
        if summary.get("canonical_metrics_pass") is not True:
            failures.append("execution-summary canonical gate is not pass")
        if summary.get("state") != "DELIVERED" or summary.get("maturity") != "native-reviewed":
            failures.append("execution-summary is not DELIVERED/native-reviewed")
        if summary.get("native_visual_review_pass") is not True:
            failures.append("execution-summary native visual review is not pass")
        if summary.get("raw_data_distributed") is not False:
            failures.append("execution-summary raw_data_distributed must be false")
        feature_mapping_summary = summary.get("feature_name_mapping", {})
        if (
            feature_mapping_summary.get("input_features") != 32738
            or feature_mapping_summary.get("duplicates_after_rename") != 0
            or feature_mapping_summary.get("matrix_rows_unchanged") != 1
            or feature_mapping_summary.get("matrix_columns_unchanged") != 1
            or feature_mapping_summary.get("count_values_unchanged") != 1
        ):
            failures.append("execution-summary feature-name mapping gate is not pass")
        umap_runtime = _read_json(root / "tables/umap_runtime_contract.json")
        expected_umap_runtime = {
            "option_name": "Seurat.warn.umap.uwot",
            "option_value_during_call": False,
            "option_restored": True,
            "umap_method": "uwot",
            "metric": "cosine",
            "seed_use": 42,
            "dims_used": 10,
            "algorithm_changed": False,
            "r_warn_option": 1,
            "warning_delivery": "immediate-stderr-via-options(warn=1)",
            "transition_notice_option_applied": True,
            "suppress_warnings_used": False,
            "handler_muffling_used": False,
            "warning_allowlist_used": False,
        }
        if any(umap_runtime.get(key) != value for key, value in expected_umap_runtime.items()):
            failures.append("UMAP runtime contract mismatch")
        if summary.get("umap_runtime_contract") != umap_runtime:
            failures.append("execution-summary UMAP runtime contract mismatch")
        execution_evidence = summary.get("execution_evidence", {})
        if (
            execution_evidence.get("environment_shutdown_mode") != "native_exit"
            or execution_evidence.get("pipeline_shutdown_mode") != "native_exit"
        ):
            failures.append("execution-summary native-exit evidence is missing")
        if any("helper" in str(key).lower() for key in execution_evidence):
            failures.append("execution-summary contains legacy exit-helper evidence")
    except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
        summary = {}
        failures.append(f"execution-summary parse failed: {exc}")

    try:
        run_manifest = _read_json(root / "manifest/run-manifest.json")
        signature = run_manifest.get("analysis_signature", "")
        if not SHA256.fullmatch(signature):
            failures.append("run-manifest analysis signature is invalid")
        if run_manifest.get("canonical_metrics") != EXPECTED:
            failures.append("run-manifest canonical metrics mismatch")
        if run_manifest.get("state") != "DELIVERED" or run_manifest.get("maturity") != "native-reviewed":
            failures.append("run-manifest is not DELIVERED/native-reviewed")
        if run_manifest.get("remote_upload") is not False:
            failures.append("run-manifest remote_upload must be false")
        if run_manifest.get("feature_name_mapping") != summary.get("feature_name_mapping"):
            failures.append("run-manifest feature-name mapping evidence mismatch")
        if run_manifest.get("umap_runtime_contract") != summary.get("umap_runtime_contract"):
            failures.append("run-manifest UMAP runtime contract evidence mismatch")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        run_manifest = {}
        signature = ""
        failures.append(f"run-manifest parse failed: {exc}")

    try:
        verification = _read_json(root / "manifest/verification-summary.json")
        if verification.get("status") != "pass" or verification.get("failures") != []:
            failures.append("source verification-summary is not a clean pass")
        if verification.get("native_visual_review_pass") is not True:
            failures.append("source verification-summary native review is not pass")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        failures.append(f"verification-summary parse failed: {exc}")

    try:
        source = _read_json(root / "manifest/source-run.json")
        if source.get("source_verification_status") != "pass":
            failures.append("source-run verification status is not pass")
        if source.get("raw_data_distributed") is not False:
            failures.append("source-run raw_data_distributed must be false")
        if source.get("analysis_signature") != signature:
            failures.append("source-run analysis signature mismatch")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        failures.append(f"source-run parse failed: {exc}")

    try:
        manifest = _read_json(root / "manifest/input-manifest.json")
        item = manifest["inputs"][0]
        if item.get("provider") != "10x Genomics" or item.get("license") != "CC BY 4.0":
            failures.append("input attribution/license mismatch")
        if item.get("sha256") != INPUT_SHA256 or item.get("content_length_bytes") != INPUT_SIZE:
            failures.append("input manifest size/hash pin mismatch")
        evidence = _read_json(root / "manifest/input-evidence.json")
        if evidence.get("archive_sha256") != INPUT_SHA256 or evidence.get("archive_size_bytes") != INPUT_SIZE:
            failures.append("input execution evidence size/hash mismatch")
        if evidence.get("remote_upload") is not False:
            failures.append("input execution evidence remote_upload must be false")
    except (OSError, KeyError, IndexError, ValueError, json.JSONDecodeError) as exc:
        failures.append(f"input provenance parse failed: {exc}")

    try:
        reuse = _read_json(root / "manifest/environment-cache-reuse.json")
        if (
            reuse.get("reuse") is not True
            or reuse.get("host_package_required") is not False
            or reuse.get("shutdown_mode") != "native_exit"
        ):
            failures.append("environment cache reuse is not explicit task-local reuse")
        for key in (
            "cache_key",
            "lock_sha256",
            "probe_sha256",
            "completion_sha256",
            "process_evidence_sha256",
            "process_command_sha256",
        ):
            if not SHA256.fullmatch(str(reuse.get(key, ""))):
                failures.append(f"environment reuse hash invalid: {key}")
        descriptions = reuse.get("library_description_sha256", {})
        if set(descriptions) != {"Seurat", "SeuratObject", "ggplot2", "jsonlite", "patchwork", "renv"}:
            failures.append("environment reuse package DESCRIPTION inventory mismatch")
        elif any(not SHA256.fullmatch(str(value)) for value in descriptions.values()):
            failures.append("environment reuse package DESCRIPTION hash invalid")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        failures.append(f"environment reuse evidence parse failed: {exc}")

    process_contracts = (
        (
            "environment-process-evidence.json",
            "environment-provision",
            "environment_process_evidence_sha256",
        ),
        (
            "r-pipeline-process-evidence.json",
            "pbmc3k-r-pipeline",
            "pipeline_process_evidence_sha256",
        ),
    )
    for filename, stage, summary_hash_key in process_contracts:
        try:
            path = root / "manifest" / filename
            process = _read_json(path)
            architecture = process.get("architecture", {})
            scan = process.get("forbidden_scan", {})
            if (
                process.get("case_id") != "pbmc3k"
                or process.get("stage") != stage
                or process.get("status") != "pass"
                or process.get("shutdown_mode") != "native_exit"
                or process.get("returncode") != 0
            ):
                failures.append(f"native process evidence failed: {filename}")
            if (
                architecture.get("platform") != "windows"
                or architecture.get("native_architecture") != "X64"
                or architecture.get("processor_architecture") != "AMD64"
                or architecture.get("supported_architecture") != "AMD64"
                or architecture.get("parent_environment_modified") is not False
            ):
                failures.append(f"native process architecture evidence failed: {filename}")
            if scan.get("passed") is not True or scan.get("matches") != []:
                failures.append(f"native process forbidden scan failed: {filename}")
            if not SHA256.fullmatch(str(process.get("command_fingerprint_sha256", ""))):
                failures.append(f"native process command hash invalid: {filename}")
            if summary.get("execution_evidence", {}).get(summary_hash_key) != _sha256(path):
                failures.append(f"native process evidence hash mismatch: {filename}")
            if stage == "pbmc3k-r-pipeline":
                runtime_record = process.get("analysis_runtime_contract", {})
                if (
                    runtime_record.get("path") != "05_results/tables/umap_runtime_contract.json"
                    or runtime_record.get("sha256") != _sha256(root / "tables/umap_runtime_contract.json")
                    or any(
                        runtime_record.get(key) != umap_runtime.get(key)
                        for key in (
                            "umap_method",
                            "metric",
                            "seed_use",
                            "option_name",
                            "option_value_during_call",
                            "option_restored",
                            "transition_notice_option_applied",
                            "suppress_warnings_used",
                            "handler_muffling_used",
                            "warning_allowlist_used",
                            "algorithm_changed",
                            "r_warn_option",
                            "warning_delivery",
                        )
                    )
                ):
                    failures.append("pipeline process UMAP runtime evidence mismatch")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            failures.append(f"native process evidence parse failed for {filename}: {exc}")

    try:
        metric_rows = _read_csv(root / "tables/canonical_metrics.csv")
        observed = {row["metric"]: int(float(row["value"])) for row in metric_rows}
        if observed != EXPECTED or any(row.get("status") != "pass" for row in metric_rows):
            failures.append("canonical_metrics.csv mismatch")
        qc_rows = _read_csv(root / "tables/qc_summary.csv")
        qc = {row["metric"]: int(float(row["value"])) for row in qc_rows}
        if qc != {"input_cells": 2700, "retained_cells": 2638, "excluded_cells": 62}:
            failures.append("qc_summary.csv mismatch")
        mapping_summary_rows = _read_csv(root / "tables/feature_name_mapping_summary.csv")
        if any(row.get("status") != "pass" for row in mapping_summary_rows):
            failures.append("feature_name_mapping_summary.csv status mismatch")
        mapping_summary = {
            row["metric"]: int(float(row["value"])) for row in mapping_summary_rows
        }
        expected_mapping_summary = {
            "input_features": 32738,
            "duplicates_after_rename": 0,
            "matrix_rows_unchanged": 1,
            "matrix_columns_unchanged": 1,
            "count_values_unchanged": 1,
        }
        if any(mapping_summary.get(key) != value for key, value in expected_mapping_summary.items()):
            failures.append("feature_name_mapping_summary.csv gate mismatch")
        mapping_rows = _read_csv(root / "tables/feature_name_mapping.csv")
        normalized_names: set[str] = set()
        changed = 0
        for row in mapping_rows:
            original = row.get("original_feature", "")
            normalized = row.get("seurat_feature", "")
            expected_normalized = original.replace("_", "-")
            expected_changed = original != expected_normalized
            if (
                not original
                or normalized != expected_normalized
                or row.get("changed", "").upper() != ("TRUE" if expected_changed else "FALSE")
                or normalized in normalized_names
            ):
                failures.append("feature_name_mapping.csv content mismatch")
                break
            normalized_names.add(normalized)
            changed += int(expected_changed)
        if (
            len(mapping_rows) != 32738
            or mapping_summary.get("renamed_features") != changed
            or feature_mapping_summary.get("renamed_features") != changed
        ):
            failures.append("feature_name_mapping.csv count mismatch")
        cluster_rows = _read_csv(root / "tables/cluster_sizes.csv")
        if {row["cluster"] for row in cluster_rows} != {str(value) for value in range(9)}:
            failures.append("cluster_sizes.csv cluster inventory mismatch")
        if sum(int(float(row["cells"])) for row in cluster_rows) != 2638:
            failures.append("cluster_sizes.csv cell total mismatch")
        annotation_rows = _read_csv(root / "tables/annotation_evidence.csv")
        if len(annotation_rows) != 9 or {row["cluster"] for row in annotation_rows} != {str(value) for value in range(9)}:
            failures.append("annotation_evidence.csv cluster inventory mismatch")
        if any(row.get("claim_boundary") != "single-library descriptive label; not donor-level inference" for row in annotation_rows):
            failures.append("annotation_evidence.csv claim boundary mismatch")
        marker_rows = _read_csv(root / "tables/cluster_markers.csv")
        if not marker_rows or {row["cluster"] for row in marker_rows} != {str(value) for value in range(9)}:
            failures.append("cluster_markers.csv is empty or lacks a cluster")
    except (OSError, KeyError, ValueError) as exc:
        failures.append(f"derived table validation failed: {exc}")

    for figure_id in FIGURES:
        try:
            original = root / f"figures/original/{figure_id}.png"
            final = root / f"figures/final/{figure_id}.png"
            review = _read_json(root / f"figures/review/{figure_id}.review.json")
            rounds = review.get("rounds", [])
            if review.get("status") != "native-reviewed" or not rounds or rounds[-1].get("decision") != "keep":
                failures.append(f"{figure_id}: review is not native-reviewed terminal keep")
                continue
            if [item.get("round") for item in rounds] != list(range(1, len(rounds) + 1)) or len(rounds) > 3:
                failures.append(f"{figure_id}: review rounds invalid")
            latest = rounds[-1]
            native = latest.get("native_view", {})
            original_hash = _sha256(original)
            final_hash = _sha256(final)
            if latest.get("original", {}).get("path") != f"figures/original/{figure_id}.png":
                failures.append(f"{figure_id}: original relative path mismatch")
            if latest.get("final", {}).get("path") != f"figures/final/{figure_id}.png":
                failures.append(f"{figure_id}: final relative path mismatch")
            if latest.get("original", {}).get("sha256") != original_hash or native.get("opened_original_sha256") != original_hash:
                failures.append(f"{figure_id}: original native-view hash mismatch")
            if latest.get("final", {}).get("sha256") != final_hash or native.get("opened_final_sha256") != final_hash:
                failures.append(f"{figure_id}: final native-view hash mismatch")
            if native.get("method") != "native_local_image_view" or native.get("tool") != "Codex view_image (detail=original)":
                failures.append(f"{figure_id}: native-view method/tool mismatch")
            if native.get("opened_original") is not True or native.get("opened_final") is not True:
                failures.append(f"{figure_id}: original/final pixels not both opened")
            if review.get("data_sha256") != signature:
                failures.append(f"{figure_id}: data signature mismatch")
            if latest.get("visual_parameter_diff", {}).get("scientific_parameters_changed") is not False:
                failures.append(f"{figure_id}: visual-only adjustment assertion missing")
            original_dimensions = _png_dimensions(original)
            final_dimensions = _png_dimensions(final)
            if original_dimensions != (
                latest["original"].get("width_px"),
                latest["original"].get("height_px"),
            ):
                failures.append(f"{figure_id}: original dimensions mismatch")
            if final_dimensions != (latest["final"].get("width_px"), latest["final"].get("height_px")):
                failures.append(f"{figure_id}: final dimensions mismatch")
            unresolved = [
                finding
                for finding in latest.get("findings", [])
                if finding.get("severity") in {"blocker", "major"} and finding.get("status") != "resolved"
            ]
            if unresolved:
                failures.append(f"{figure_id}: unresolved major visual finding")
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            failures.append(f"{figure_id}: figure/review validation failed: {exc}")

    try:
        ledger_lines = (root / "manifest/artifact_ledger.jsonl").read_text(encoding="utf-8").splitlines()
        ledger = [json.loads(line) for line in ledger_lines if line.strip()]
        if [record.get("sequence") for record in ledger] != list(range(1, len(ledger) + 1)):
            failures.append("artifact ledger sequence is not consecutive")
        expected_ledger_paths = actual - {"manifest/artifact_ledger.jsonl"}
        ledger_paths = {str(record.get("path")) for record in ledger}
        if ledger_paths != expected_ledger_paths:
            failures.append("artifact ledger path set mismatch")
        for record in ledger:
            path = root / str(record.get("path"))
            if not path.is_file():
                continue
            if record.get("sha256") != _sha256(path) or record.get("size_bytes") != path.stat().st_size:
                failures.append(f"artifact ledger byte binding mismatch: {record.get('path')}")
        index = (root / "ARTIFACT_INDEX.md").read_text(encoding="utf-8")
        for relative in sorted(actual - {"ARTIFACT_INDEX.md", "manifest/artifact_ledger.jsonl"}):
            if f"`{relative}`" not in index:
                failures.append(f"artifact index missing payload: {relative}")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        failures.append(f"artifact ledger/index validation failed: {exc}")

    for name in ("RESULTS.md", "QA_REPORT.md"):
        path = root / "reports" / name
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            if "2700" not in text.replace(",", "") or "2638" not in text.replace(",", "") or "9" not in text:
                failures.append(f"{name} does not retain canonical teaching numbers")

    return {
        "case_id": "pbmc3k",
        "canonical_metrics": EXPECTED,
        "files_verified": len(actual),
        "failures": failures,
        "native_visual_review_pass": not any("review" in item or "figure" in item for item in failures),
        "raw_data_distributed": False,
        "status": "pass" if not failures else "fail",
    }


def main() -> int:
    result = verify_output()
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["status"] == "pass" else 3


if __name__ == "__main__":
    raise SystemExit(main())
