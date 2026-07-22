#!/usr/bin/env python3
"""Independently verify the committed Visium expected-output snapshot."""

from __future__ import annotations

import argparse
import binascii
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import struct
import sys
from typing import Any


CASE_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT_ROOT = CASE_DIR / "expected-output"
PRIVATE_PATH = re.compile(r"(?im)(?:^|[\s\"'=(:,])(?:[A-Z]:[\\/]|/(?:home|Users)/)")
FORBIDDEN_SUFFIXES = {
    ".dll",
    ".exe",
    ".gz",
    ".h5",
    ".hdf5",
    ".lib",
    ".rdata",
    ".rds",
    ".so",
    ".tar",
    ".zip",
}
FORBIDDEN_PARTS = {".cache", "02_environment", "inputs", "objects", "renv"}
FIGURES = ("spatial_qc.png", "spatial_clusters.png", "spatial_features_hpca_ttr.png")
DIRECTED_DIFFERENCES = {
    "assay_cells_not_image_cells",
    "image_cells_not_assay_cells",
    "assay_cells_not_coordinates",
    "coordinates_not_assay_cells",
    "image_cells_not_coordinates",
    "coordinates_not_image_cells",
}


class VerificationError(RuntimeError):
    """Raised when published expected output violates its contract."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise VerificationError(f"expected JSON object: {path.name}")
    return value


def safe_relative(text: str) -> PurePosixPath:
    relative = PurePosixPath(text)
    if not relative.parts or relative.is_absolute() or ".." in relative.parts or relative.as_posix() != text:
        raise VerificationError(f"unsafe manifest path: {text!r}")
    return relative


def parse_png(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise VerificationError(f"invalid PNG signature: {path.name}")
    offset = 8
    width = height = 0
    saw_ihdr = saw_idat = saw_iend = False
    while offset < len(data):
        if offset + 12 > len(data):
            raise VerificationError(f"truncated PNG: {path.name}")
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        kind = data[offset + 4 : offset + 8]
        end = offset + 12 + length
        if end > len(data):
            raise VerificationError(f"truncated PNG payload: {path.name}")
        payload = data[offset + 8 : offset + 8 + length]
        observed_crc = struct.unpack(">I", data[offset + 8 + length : end])[0]
        if observed_crc != (binascii.crc32(kind + payload) & 0xFFFFFFFF):
            raise VerificationError(f"PNG CRC mismatch: {path.name}")
        if kind == b"IHDR":
            if saw_ihdr or length != 13:
                raise VerificationError(f"malformed PNG IHDR: {path.name}")
            width, height = struct.unpack(">II", payload[:8])
            saw_ihdr = True
        elif kind == b"IDAT":
            saw_idat = True
        elif kind == b"IEND":
            if length != 0 or end != len(data):
                raise VerificationError(f"invalid PNG IEND/trailing data: {path.name}")
            saw_iend = True
            break
        offset = end
    if not (saw_ihdr and saw_idat and saw_iend) or width <= 0 or height <= 0:
        raise VerificationError(f"incomplete PNG: {path.name}")
    return width, height


def validate_inventory(output_root: Path) -> tuple[dict[str, Any], set[str]]:
    manifest_path = output_root / "expected-output-manifest.json"
    manifest = load_json(manifest_path)
    if (
        manifest.get("schema_version") != "1.0"
        or manifest.get("case") != "visium-mouse-brain"
        or manifest.get("status") != "verified_public_teaching_snapshot"
        or manifest.get("distribution_profile") != "derived_artifacts_only"
        or manifest.get("terminal_native_review") != "keep"
        or manifest.get("manifest_self_excluded") is not True
    ):
        raise VerificationError("expected-output manifest identity/status mismatch")
    records = manifest.get("artifacts")
    if not isinstance(records, list) or not records:
        raise VerificationError("expected-output manifest has no artifacts")
    expected = {"expected-output-manifest.json"}
    for record in records:
        if not isinstance(record, dict):
            raise VerificationError("invalid expected-output manifest record")
        relative_text = str(record.get("path", ""))
        relative = safe_relative(relative_text)
        if relative_text in expected:
            raise VerificationError(f"duplicate manifest path: {relative_text}")
        if relative.suffix.lower() in FORBIDDEN_SUFFIXES or any(part in FORBIDDEN_PARTS for part in relative.parts):
            raise VerificationError(f"forbidden distributed artifact: {relative_text}")
        artifact = output_root / Path(relative_text)
        if not artifact.is_file():
            raise VerificationError(f"manifest artifact is missing: {relative_text}")
        if artifact.stat().st_size != int(record.get("size_bytes", -1)):
            raise VerificationError(f"manifest size mismatch: {relative_text}")
        expected_hash = str(record.get("sha256", ""))
        if not re.fullmatch(r"[0-9a-f]{64}", expected_hash) or sha256_file(artifact) != expected_hash:
            raise VerificationError(f"manifest hash mismatch: {relative_text}")
        expected.add(relative_text)
    observed = {
        path.relative_to(output_root).as_posix()
        for path in output_root.rglob("*")
        if path.is_file()
    }
    if observed != expected:
        raise VerificationError(
            "expected-output inventory differs from manifest: "
            f"missing={sorted(expected - observed)}, extra={sorted(observed - expected)}"
        )
    return manifest, observed


def validate_text_sanitization(output_root: Path, inventory: set[str]) -> None:
    for relative in inventory:
        path = output_root / Path(relative)
        if path.suffix.lower() == ".png":
            continue
        try:
            text = path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError as exc:
            raise VerificationError(f"unexpected binary payload: {relative}") from exc
        if PRIVATE_PATH.search(text):
            raise VerificationError(f"private absolute path found in published artifact: {relative}")


def validate_results(output_root: Path) -> dict[str, Any]:
    summary = load_json(output_root / "manifest" / "execution-summary.json")
    observed = summary.get("observed", {})
    expected_counts = {
        "matrix_barcodes": 2695,
        "vendor_in_tissue_barcodes": 2695,
        "loaded_spots": 2695,
        "assay_cells": 2695,
        "image_cells": 2695,
        "coordinate_barcodes": 2695,
        "retained_spots": 2695,
        "expression_spot_clusters": 11,
    }
    if (
        summary.get("case") != "visium-mouse-brain"
        or summary.get("status") != "DELIVERED"
        or summary.get("validation", {}).get("native_visual_review") != "passed"
        or summary.get("sensitive_paths_included") is not False
        or not isinstance(observed, dict)
        or any(observed.get(key) != value for key, value in expected_counts.items())
    ):
        raise VerificationError("execution summary does not match the frozen teaching result")

    reconciliation = load_json(output_root / "05_results" / "tables" / "barcode_set_reconciliation.json")
    directed = reconciliation.get("directed_difference_counts")
    if (
        reconciliation.get("status") != "passed"
        or not isinstance(directed, dict)
        or set(directed) != DIRECTED_DIFFERENCES
        or any(value != 0 for value in directed.values())
        or observed.get("directed_assay_image_coordinate_differences") != directed
    ):
        raise VerificationError("three-party barcode reconciliation is not exact")

    results_text = (output_root / "07_reports" / "RESULTS.md").read_text(encoding="utf-8")
    if "native alignment review remains pending" in results_text or "hash-bound native alignment review passed" not in results_text:
        raise VerificationError("RESULTS.md does not reflect terminal native review")
    return expected_counts


def validate_figures_and_review(output_root: Path) -> dict[str, Any]:
    review_path = output_root / "06_figures" / "review" / "review-round-1.json"
    review = load_json(review_path)
    items = review.get("figure_reviews")
    if (
        review.get("overall_decision") != "keep"
        or review.get("reviewer_method") != "native_local_image_view"
        or review.get("opened_original_and_final") is not True
        or not isinstance(items, list)
        or len(items) != 3
    ):
        raise VerificationError("terminal native-review record is incomplete")
    by_name = {str(item.get("final_path", "")).split("/")[-1]: item for item in items}
    if set(by_name) != set(FIGURES):
        raise VerificationError("terminal native-review figure set mismatch")
    pairs: dict[str, Any] = {}
    for filename in FIGURES:
        original = output_root / "06_figures" / "original" / "round-1" / filename
        final = output_root / "06_figures" / "final" / "round-1" / filename
        item = by_name[filename]
        if (
            item.get("original_path") != original.relative_to(output_root).as_posix()
            or item.get("final_path") != final.relative_to(output_root).as_posix()
            or item.get("original_sha256") != sha256_file(original)
            or item.get("final_sha256") != sha256_file(final)
        ):
            raise VerificationError(f"original/final figure hash mismatch: {filename}")
        if item.get("decision") != "keep" or item.get("findings"):
            raise VerificationError(f"figure review is not terminal keep: {filename}")
        original_dimensions = parse_png(original)
        final_dimensions = parse_png(final)
        if original_dimensions != (3600, 2400) or final_dimensions != (2130, 1425):
            raise VerificationError(f"frozen figure dimensions mismatch: {filename}")
        pairs[filename] = {
            "original_sha256": sha256_file(original),
            "final_sha256": sha256_file(final),
            "original_dimensions": list(original_dimensions),
            "final_dimensions": list(final_dimensions),
        }
    return pairs


def validate_ledger(output_root: Path, inventory: set[str]) -> int:
    ledger_path = output_root / "manifest" / "artifact_ledger.jsonl"
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    records = 0
    for line_number, line in enumerate(ledger_path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise VerificationError(f"ledger line is not an object: {line_number}")
        artifact_id = str(value.get("artifact_id", ""))
        relative_text = str(value.get("path", ""))
        relative = safe_relative(relative_text)
        if artifact_id in seen_ids or not artifact_id or relative_text in seen_paths:
            raise VerificationError(f"duplicate/invalid ledger identity at line {line_number}")
        if relative.suffix.lower() in FORBIDDEN_SUFFIXES or any(part in FORBIDDEN_PARTS for part in relative.parts):
            raise VerificationError(f"forbidden distributed artifact in ledger: {relative_text}")
        artifact = output_root / Path(relative_text)
        if relative_text not in inventory or not artifact.is_file():
            raise VerificationError(f"ledger artifact is not distributed: {relative_text}")
        if artifact.stat().st_size != int(value.get("size_bytes", -1)) or sha256_file(artifact) != value.get("sha256"):
            raise VerificationError(f"ledger artifact hash/size mismatch: {relative_text}")
        seen_ids.add(artifact_id)
        seen_paths.add(relative_text)
        records += 1
    required = {
        "manifest/execution-summary.json",
        "manifest/run_manifest.json",
        "manifest/verification-summary.json",
        "06_figures/review/review-round-1.json",
        "07_reports/RESULTS.md",
        "07_reports/QA_REPORT.md",
        "validation/execution-gates.json",
    }
    required.update(f"06_figures/{kind}/round-1/{name}" for kind in ("original", "final") for name in FIGURES)
    if not required.issubset(seen_paths):
        raise VerificationError("sanitized artifact ledger lacks required coverage")
    return records


def validate_gates_and_provenance(output_root: Path) -> None:
    gates = load_json(output_root / "validation" / "execution-gates.json")
    for key in (
        "fresh_run",
        "checkpoint_resume",
        "environment_cache_reuse",
        "input_cache_reuse",
        "checksum_failure_injection",
        "pre_completion_fault_injection",
        "native_r_shutdown",
        "strict_ci_validator",
    ):
        if gates.get(key, {}).get("status") != "passed":
            raise VerificationError(f"published execution gate did not pass: {key}")
    warning = gates.get("runtime_warning_classification", {})
    if (
        warning.get("status") != "passed"
        or warning.get("blocking_warning_occurrences") != 0
        or warning.get("scientific_parameters_changed") is not False
    ):
        raise VerificationError("runtime warning classification is not release-safe")

    warning_evidence = load_json(output_root / "validation" / "pipeline-warnings.json")
    if (
        warning_evidence.get("status") != "passed"
        or warning_evidence.get("blocking_warning_occurrences") != 0
        or warning_evidence.get("scientific_parameters_changed") is not False
    ):
        raise VerificationError("published pipeline warning ledger is blocked")

    for mode in ("fresh", "resume"):
        native = load_json(
            output_root / "validation" / f"pipeline-{mode}-native-exit-evidence.json"
        )
        if (
            native.get("status") != "passed"
            or native.get("native_returncode") != 0
            or native.get("forbidden_matches") != []
            or native.get("shutdown_mode") != "native_exit"
        ):
            raise VerificationError(f"published {mode} native-exit evidence is blocked")

    resume = load_json(output_root / "validation" / "checkpoint-resume-reuse.json")
    cache = load_json(output_root / "validation" / "environment-cache-reuse.json")
    input_cache = load_json(output_root / "validation" / "input-cache-reuse.json")
    corrupted = load_json(output_root / "validation" / "corrupted-cache-negative-control.json")
    fault = load_json(output_root / "validation" / "fault-before-completion-marker.json")
    if not (
        resume.get("all_checkpoints_reused") is True
        and resume.get("stage_start_observed") is False
        and cache.get("reuse") is True
        and cache.get("fully_validated_before_evidence_write") is True
        and input_cache.get("reuse") is True
        and input_cache.get("fully_validated_before_evidence_write") is True
        and input_cache.get("materialization") == "direct_read_no_copy"
        and input_cache.get("canonical_inputs_modified") is False
        and corrupted.get("failure_closed") is True
        and corrupted.get("canonical_inputs_modified") is False
        and fault.get("completion_marker_absent") is True
        and fault.get("canonical_run_modified") is False
    ):
        raise VerificationError("copied negative-control or reuse evidence is inconsistent")

    input_manifest = load_json(output_root / "provenance" / "input-manifest.json")
    expected_inputs = {
        "filtered_h5": (20554697, "56078d8d6fe6c13de248fdb1c518b691cdef78fb00021b659786b4a47c6656d5"),
        "spatial_archive": (9233573, "5f41a803e2bd69fa4dfca6abc8fa2d4e0d76aeb6c72d7038a5fdcf9cc50a36f8"),
    }
    records = input_manifest.get("files", [])
    observed = {
        item.get("file_id"): (item.get("expected_size_bytes"), item.get("expected_sha256"))
        for item in records
        if isinstance(item, dict)
    }
    if observed != expected_inputs or any(item.get("freeze_policy") != "exact_required" for item in records):
        raise VerificationError("published input provenance does not match exact frozen hashes")


def validate_verification_summary(output_root: Path) -> None:
    summary = load_json(output_root / "manifest" / "verification-summary.json")
    expected_metrics = {
        "matrix_barcodes": 2695,
        "vendor_all_positions": 4992,
        "vendor_in_tissue_barcodes": 2695,
        "loaded_spots": 2695,
        "retained_spots": 2695,
        "expression_spot_clusters": 11,
        "pca_dimensions": 30,
        "pca_non_finite_values": 0,
        "sct_non_finite_values": 0,
    }
    expected_preprocessing = {
        "vst_flavor": "v2",
        "method": "glmGamPoi_offset",
        "glmGamPoi_check": True,
        "sct_variable_features": 3000,
    }
    expected_differences = {key: 0 for key in DIRECTED_DIFFERENCES}
    if (
        summary.get("schema_version") != "1.0.0"
        or summary.get("case_id") != "visium-mouse-brain"
        or summary.get("status") != "pass"
        or summary.get("failures") != []
        or summary.get("canonical_metrics") != expected_metrics
        or summary.get("preprocessing") != expected_preprocessing
        or summary.get("native_visual_review_pass") is not True
    ):
        raise VerificationError("verification-summary identity or canonical metrics mismatch")
    reconciliation = summary.get("barcode_reconciliation", {})
    if (
        reconciliation.get("directed_difference_counts") != expected_differences
        or reconciliation.get("nonzero_difference_count") != 0
    ):
        raise VerificationError("verification-summary barcode reconciliation mismatch")
    if summary.get("cache_reuse") != {
        "input_direct_read_no_copy": True,
        "environment_native_r_revalidation": "passed",
    }:
        raise VerificationError("verification-summary cache reuse is incomplete")
    if summary.get("negative_controls") != {
        "checksum_failure_closed": True,
        "pre_completion_fault_failure_closed": True,
        "canonical_inputs_modified": False,
        "canonical_run_modified": False,
    }:
        raise VerificationError("verification-summary negative controls are incomplete")

    warning = load_json(output_root / "validation" / "pipeline-warnings.json")
    evidence = summary.get("execution_evidence", {})
    expected_hashes = {
        "pipeline_warning_evidence_sha256": sha256_file(
            output_root / "validation" / "pipeline-warnings.json"
        ),
        "fresh_native_exit_evidence_sha256": sha256_file(
            output_root / "validation" / "pipeline-fresh-native-exit-evidence.json"
        ),
        "resume_native_exit_evidence_sha256": sha256_file(
            output_root / "validation" / "pipeline-resume-native-exit-evidence.json"
        ),
        "input_cache_reuse_evidence_sha256": sha256_file(
            output_root / "validation" / "input-cache-reuse.json"
        ),
        "environment_cache_reuse_evidence_sha256": sha256_file(
            output_root / "validation" / "environment-cache-reuse.json"
        ),
        "checksum_negative_control_sha256": sha256_file(
            output_root / "validation" / "corrupted-cache-negative-control.json"
        ),
        "pre_completion_fault_evidence_sha256": sha256_file(
            output_root / "validation" / "fault-before-completion-marker.json"
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
    }
    if any(evidence.get(key) != value for key, value in expected_hashes.items()):
        raise VerificationError("verification-summary evidence hash binding mismatch")
    for key in ("run_pipeline_sha256", "analysis_params_sha256", "renv_lock_sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", str(evidence.get(key, ""))):
            raise VerificationError(f"verification-summary has invalid {key}")
    if (
        evidence.get("run_pipeline_sha256") != warning.get("code_hash")
        or evidence.get("analysis_params_sha256") != warning.get("analysis_config_hash")
        or evidence.get("renv_lock_sha256") != warning.get("environment_lock_hash")
    ):
        raise VerificationError("verification-summary code/config/environment binding mismatch")


def verify(output_root: Path) -> dict[str, Any]:
    if not output_root.exists():
        raise VerificationError(
            "expected-output not published until a fresh Bioconductor 3.21 native-exit run and native review pass"
        )
    output_root = output_root.resolve(strict=True)
    if output_root == Path(output_root.anchor):
        raise VerificationError("output root cannot be a filesystem root")
    _, inventory = validate_inventory(output_root)
    validate_text_sanitization(output_root, inventory)
    counts = validate_results(output_root)
    pairs = validate_figures_and_review(output_root)
    ledger_records = validate_ledger(output_root, inventory)
    validate_gates_and_provenance(output_root)
    validate_verification_summary(output_root)
    return {
        "schema_version": "1.0",
        "ok": True,
        "case": "visium-mouse-brain",
        "status": "expected_output_verified",
        "distributed_files": len(inventory),
        "sanitized_ledger_records": ledger_records,
        "canonical_counts": counts,
        "figure_pairs": pairs,
        "native_visual_review": "keep",
        "private_absolute_paths": "absent",
        "raw_inputs_r_objects_environments": "absent",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = verify(args.output_root)
    except (OSError, ValueError, json.JSONDecodeError, VerificationError) as exc:
        sys.stderr.write(f"EXPECTED_OUTPUT_VERIFICATION_FAILED: {exc}\n")
        return 2
    sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
