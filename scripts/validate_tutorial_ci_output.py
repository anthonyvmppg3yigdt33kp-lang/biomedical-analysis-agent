#!/usr/bin/env python3
"""Validate computational tutorial output without claiming native visual review."""

from __future__ import annotations

import argparse
import binascii
import hashlib
import json
import re
import struct
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


CASES = {"pbmc3k", "visium-mouse-brain"}
PRIVATE_PATH = re.compile(r"(?i)(?:[A-Z]:[\\/](?:Users|home)[\\/]|/(?:home|Users)/)")
SNAPSHOT = "https://packagemanager.posit.co/cran/2026-04-23"
EXPECTED_RUNTIME = {"R": "4.5.3", "Seurat": "5.5.0", "renv": "1.2.2"}
TERMINAL_STATES = {"NATIVE_VISUAL_REVIEW", "DELIVERED"}
VISIUM_DIRECTED_DIFFERENCES = {
    "assay_cells_not_image_cells",
    "image_cells_not_assay_cells",
    "assay_cells_not_coordinates",
    "coordinates_not_assay_cells",
    "image_cells_not_coordinates",
    "coordinates_not_image_cells",
}
VISIUM_FORBIDDEN_WARNING_CATEGORIES = {
    "api_compatibility_warning",
    "numerical_integrity_warning",
    "sctransform_glm_nb_alternation_limit",
    "sctransform_theta_iteration_limit",
    "spatial_integrity_warning",
    "unclassified_warning",
}


class TutorialOutputError(RuntimeError):
    """Raised when a computational run cannot serve as CI evidence."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise TutorialOutputError(f"expected JSON object: {path}")
    return payload


def strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from strings(child)


def parse_png(path: Path) -> tuple[int, int]:
    """Validate the PNG container and return its IHDR dimensions."""

    data = path.read_bytes()
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise TutorialOutputError(f"invalid PNG signature: {path}")
    offset = 8
    width = height = 0
    saw_ihdr = saw_idat = saw_iend = False
    while offset < len(data):
        if offset + 12 > len(data):
            raise TutorialOutputError(f"truncated PNG chunk: {path}")
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        kind = data[offset + 4 : offset + 8]
        chunk_end = offset + 12 + length
        if chunk_end > len(data):
            raise TutorialOutputError(f"truncated PNG payload: {path}")
        payload = data[offset + 8 : offset + 8 + length]
        observed_crc = struct.unpack(">I", data[offset + 8 + length : chunk_end])[0]
        expected_crc = binascii.crc32(kind + payload) & 0xFFFFFFFF
        if observed_crc != expected_crc:
            raise TutorialOutputError(f"PNG CRC mismatch: {path}")
        if not saw_ihdr:
            if kind != b"IHDR" or length != 13:
                raise TutorialOutputError(f"PNG IHDR is missing or malformed: {path}")
            width, height = struct.unpack(">II", payload[:8])
            if width <= 0 or height <= 0 or width > 100_000 or height > 100_000:
                raise TutorialOutputError(f"invalid PNG dimensions: {path}")
            saw_ihdr = True
        elif kind == b"IHDR":
            raise TutorialOutputError(f"duplicate PNG IHDR: {path}")
        if kind == b"IDAT":
            saw_idat = True
        if kind == b"IEND":
            if length != 0 or chunk_end != len(data):
                raise TutorialOutputError(f"invalid PNG IEND/trailing bytes: {path}")
            saw_iend = True
            break
        offset = chunk_end
    if not (saw_ihdr and saw_idat and saw_iend):
        raise TutorialOutputError(f"incomplete PNG container: {path}")
    return width, height


def _package_version(lock: dict[str, Any], package: str) -> str:
    packages = lock.get("Packages")
    if not isinstance(packages, dict) or not isinstance(packages.get(package), dict):
        return ""
    return str(packages[package].get("Version", ""))


def validate_environment(case: str, run_root: Path) -> tuple[str, dict[str, str]]:
    marker_path = run_root / "02_environment" / "environment.locked.json"
    lock_path = run_root / "02_environment" / "renv.lock"
    marker = load_json(marker_path)
    lock = load_json(lock_path)
    lock_sha = sha256_file(lock_path)
    observed_runtime = {
        "R": str(lock.get("R", {}).get("Version", "")),
        "Seurat": _package_version(lock, "Seurat"),
        "renv": _package_version(lock, "renv"),
    }
    if observed_runtime != EXPECTED_RUNTIME:
        raise TutorialOutputError(f"renv.lock runtime mismatch: {observed_runtime}")
    repositories = lock.get("R", {}).get("Repositories")
    if not isinstance(repositories, list) or not any(
        isinstance(item, dict) and item.get("URL") == SNAPSHOT for item in repositories
    ):
        raise TutorialOutputError("renv.lock is not bound to the reviewed package snapshot")

    if case == "pbmc3k":
        marker_packages = marker.get("packages", {})
        backend_lock = marker.get("backend_lock", {})
        checks = (
            marker.get("r_version") == "4.5.3",
            marker.get("verified") is True,
            marker.get("frozen") is True,
            isinstance(marker_packages, dict),
            marker_packages.get("Seurat") == "5.5.0",
            marker_packages.get("renv") == "1.2.2",
            marker.get("repository_snapshot") == SNAPSHOT,
            marker.get("package_type") == "win.binary",
            isinstance(backend_lock, dict),
            backend_lock.get("path") == "renv.lock",
            backend_lock.get("sha256") == lock_sha,
        )
    else:
        marker_packages = marker.get("packages", {})
        repository = marker.get("repository", {})
        checks = (
            marker.get("status") == "frozen",
            marker.get("r_version") == "4.5.3",
            marker.get("seurat_version") == "5.5.0",
            marker.get("task_local_renv_version") == "1.2.2",
            marker.get("bootstrap_renv_version") == "1.2.2",
            isinstance(marker_packages, dict),
            marker_packages.get("Seurat") == "5.5.0",
            marker_packages.get("renv") == "1.2.2",
            isinstance(repository, dict),
            repository.get("snapshot_url") == SNAPSHOT,
            repository.get("package_type") == "binary",
            marker.get("renv_lock_sha256") == lock_sha,
        )
    if not all(checks):
        raise TutorialOutputError("environment.locked.json violates the exact R/Seurat/renv contract")
    return lock_sha, observed_runtime


def validate_ledger(run_root: Path, required_paths: set[str]) -> tuple[int, str]:
    ledger_path = run_root / "manifest" / "artifact_ledger.jsonl"
    if not ledger_path.is_file():
        raise TutorialOutputError("artifact ledger is missing")
    latest: dict[str, dict[str, Any]] = {}
    lines = 0
    artifact_ids: set[str] = set()
    for line_number, line in enumerate(ledger_path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        lines += 1
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise TutorialOutputError(f"invalid ledger JSON at line {line_number}: {exc}") from exc
        if not isinstance(record, dict):
            raise TutorialOutputError(f"ledger line {line_number} is not an object")
        relative_text = str(record.get("path", ""))
        relative = PurePosixPath(relative_text)
        if (
            not relative.parts
            or relative.is_absolute()
            or ".." in relative.parts
            or relative.as_posix() != relative_text
        ):
            raise TutorialOutputError(f"unsafe ledger path at line {line_number}: {relative_text}")
        artifact_id = record.get("artifact_id")
        if artifact_id is not None:
            if not isinstance(artifact_id, str) or not artifact_id or artifact_id in artifact_ids:
                raise TutorialOutputError(f"duplicate/invalid ledger artifact_id at line {line_number}")
            artifact_ids.add(artifact_id)
        latest[relative_text] = record
    if not lines:
        raise TutorialOutputError("artifact ledger is empty")
    missing_coverage = sorted(required_paths - set(latest))
    if missing_coverage:
        raise TutorialOutputError("artifact ledger lacks required coverage: " + ", ".join(missing_coverage))
    for relative_text, record in latest.items():
        artifact = run_root / Path(relative_text)
        if not artifact.is_file():
            raise TutorialOutputError(f"ledger artifact is missing: {relative_text}")
        try:
            expected_size = int(record["size_bytes"])
        except (KeyError, TypeError, ValueError) as exc:
            raise TutorialOutputError(f"ledger size is missing/invalid: {relative_text}") from exc
        if artifact.stat().st_size != expected_size:
            raise TutorialOutputError(f"ledger size mismatch: {relative_text}")
        expected = str(record.get("sha256", ""))
        if not re.fullmatch(r"[0-9a-f]{64}", expected) or sha256_file(artifact) != expected:
            raise TutorialOutputError(f"ledger hash mismatch: {relative_text}")
    return lines, sha256_file(ledger_path)


def _expected_figures(case: str, run_root: Path) -> tuple[list[Path], list[Path], tuple[int, int], tuple[int, int]]:
    if case == "pbmc3k":
        visual = load_json(run_root / "03_scripts" / "params.json").get("visual", {})
        if not isinstance(visual, dict):
            raise TutorialOutputError("PBMC3K visual parameters are missing")
        dpi = int(visual.get("dpi", 0))
        original_dimensions = (
            round(float(visual.get("original_width_in", 0)) * dpi),
            round(float(visual.get("original_height_in", 0)) * dpi),
        )
        final_dimensions = (
            round(float(visual.get("final_width_in", 0)) * dpi),
            round(float(visual.get("final_height_in", 0)) * dpi),
        )
        original_root = run_root / "06_figures" / "original"
        final_root = run_root / "06_figures" / "final"
        expected_count = 5
    else:
        visual = load_json(run_root / "03_scripts" / "visual-params.json")
        round_number = int(visual.get("render_round", 0))
        if round_number not in (1, 2, 3):
            raise TutorialOutputError("Visium render_round must be 1..3")
        original_export = visual.get("original_export", {})
        final_export = visual.get("final_export", {})
        original_dimensions = (
            round(float(original_export.get("width_in", 0)) * int(original_export.get("dpi", 0))),
            round(float(original_export.get("height_in", 0)) * int(original_export.get("dpi", 0))),
        )
        final_dimensions = (
            round(float(final_export.get("width_in", 0)) * int(final_export.get("dpi", 0))),
            round(float(final_export.get("height_in", 0)) * int(final_export.get("dpi", 0))),
        )
        original_root = run_root / "06_figures" / "original" / f"round-{round_number}"
        final_root = run_root / "06_figures" / "final" / f"round-{round_number}"
        expected_count = 3
    original = sorted(original_root.glob("*.png"))
    final = sorted(final_root.glob("*.png"))
    original_names = [path.name for path in original]
    final_names = [path.name for path in final]
    if len(original) != expected_count or len(final) != expected_count:
        raise TutorialOutputError(
            f"expected {expected_count} original/final figures; found {len(original)}/{len(final)}"
        )
    if len(set(original_names)) != len(original_names) or original_names != final_names:
        raise TutorialOutputError("original/final PNG basenames do not match exactly")
    if min(*original_dimensions, *final_dimensions) <= 0:
        raise TutorialOutputError("declared PNG dimensions are invalid")
    for path in original:
        if parse_png(path) != original_dimensions:
            raise TutorialOutputError(f"original PNG dimensions differ from frozen visual config: {path}")
    for path in final:
        if parse_png(path) != final_dimensions:
            raise TutorialOutputError(f"final PNG dimensions differ from frozen visual config: {path}")
    return original, final, original_dimensions, final_dimensions


def validate(case: str, run_root: Path) -> dict[str, Any]:
    if case not in CASES:
        raise TutorialOutputError(f"unsupported case: {case}")
    run_root = run_root.resolve(strict=True)
    if run_root == Path(run_root.anchor):
        raise TutorialOutputError("run root cannot be a filesystem root")
    summary_path = run_root / "manifest" / "execution-summary.json"
    if not summary_path.is_file():
        raise TutorialOutputError("execution-summary.json is missing")
    summary = load_json(summary_path)
    warning_summary: dict[str, Any] = {}
    if case == "pbmc3k":
        if summary.get("schema_version") != "1.0.0" or summary.get("case_id") != case:
            raise TutorialOutputError("PBMC3K execution summary identity/schema mismatch")
        state = summary.get("state")
        if state not in TERMINAL_STATES:
            raise TutorialOutputError(f"PBMC3K summary has a non-allowlisted state: {state!r}")
        expected_maturity = "native-reviewed" if state == "DELIVERED" else "data-verified"
        if summary.get("maturity") != expected_maturity:
            raise TutorialOutputError("PBMC3K state/maturity mismatch")
    else:
        if summary.get("schema_version") != "1.0" or summary.get("case") != case:
            raise TutorialOutputError("Visium execution summary identity/schema mismatch")
        state = summary.get("status")
        if state not in TERMINAL_STATES:
            raise TutorialOutputError(f"Visium summary has a non-allowlisted status: {state!r}")
    leaks = sorted({value for value in strings(summary) if PRIVATE_PATH.search(value)})
    if leaks:
        raise TutorialOutputError("execution summary contains a private absolute path")

    lock_sha, runtime = validate_environment(case, run_root)
    required_ledger_paths: set[str] = set()
    if case == "pbmc3k":
        metrics = summary.get("canonical_metrics", {})
        expected = {"input_cells": 2700, "qc_retained_cells": 2638, "clusters": 9}
        mismatches = {key: metrics.get(key) for key, value in expected.items() if metrics.get(key) != value}
        if summary.get("canonical_metrics_pass") is not True or mismatches:
            raise TutorialOutputError(f"PBMC3K canonical metric mismatch: {mismatches}")
        required_ledger_paths.update(
            {
                "02_environment/environment.locked.json",
                "02_environment/renv.lock",
                "05_results/tables/canonical_metrics.csv",
                "manifest/execution-summary.json",
            }
        )
    else:
        observed = summary.get("observed", {})
        positive_counts = ("matrix_barcodes", "loaded_spots", "coordinate_barcodes", "retained_spots")
        try:
            counts_ok = isinstance(observed, dict) and all(int(observed.get(key, 0)) > 0 for key in positive_counts)
        except (TypeError, ValueError):
            counts_ok = False
        if not counts_ok:
            raise TutorialOutputError("Visium summary lacks positive spot/barcode counts")
        reconciliation_relative = "05_results/tables/barcode_set_reconciliation.json"
        reconciliation = load_json(run_root / reconciliation_relative)
        directed = reconciliation.get("directed_difference_counts")
        if not isinstance(directed, dict) or set(directed) != VISIUM_DIRECTED_DIFFERENCES:
            raise TutorialOutputError("Visium reconciliation lacks the exact six directed three-party set differences")
        try:
            directed_ok = all(int(value) == 0 for value in directed.values())
        except (TypeError, ValueError):
            directed_ok = False
        if reconciliation.get("status") != "passed" or not directed_ok:
            raise TutorialOutputError("Visium assay/image/coordinate barcode reconciliation is not terminal-passed")
        if observed.get("directed_assay_image_coordinate_differences") != directed:
            raise TutorialOutputError("Visium summary/reconciliation directed differences disagree")
        warning_relative = "logs/pipeline-warnings.json"
        warning_path = run_root / warning_relative
        if not warning_path.is_file():
            raise TutorialOutputError("Visium runtime warning evidence is missing")
        warning_evidence = load_json(warning_path)
        warning_records = warning_evidence.get("records", [])
        if (
            warning_evidence.get("schema_version") != "1.0"
            or warning_evidence.get("classification_version") != "1.0"
            or warning_evidence.get("status") != "passed"
            or warning_evidence.get("blocking_warning_occurrences") != 0
            or warning_evidence.get("scientific_parameters_changed") is not False
            or warning_evidence.get("absolute_paths_included") is not False
            or warning_evidence.get("code_hash") != sha256_file(run_root / "03_scripts" / "run_pipeline.R")
            or warning_evidence.get("analysis_config_hash") != sha256_file(run_root / "03_scripts" / "analysis-params.json")
            or warning_evidence.get("environment_lock_hash") != lock_sha
            or not isinstance(warning_records, list)
            or summary.get("validation", {}).get("runtime_warnings") != "passed"
        ):
            raise TutorialOutputError("Visium runtime warning evidence is missing, unbound, or release-blocked")
        for record in warning_records:
            if (
                not isinstance(record, dict)
                or record.get("category") in VISIUM_FORBIDDEN_WARNING_CATEGORIES
                or record.get("severity") == "release_blocker"
                or record.get("allowlisted") is not True
                or int(record.get("count", 0)) <= 0
            ):
                raise TutorialOutputError("Visium warning ledger contains an API/numerical/spatial/unknown blocker")
        warning_summary = {
            "runtime_warning_evidence_sha256": sha256_file(warning_path),
            "executed_pipeline_sha256": sha256_file(run_root / "03_scripts" / "run_pipeline.R"),
            "analysis_config_sha256": sha256_file(run_root / "03_scripts" / "analysis-params.json"),
            "runtime_warning_occurrences": int(warning_evidence.get("warning_occurrences", 0)),
            "runtime_warning_records": len(warning_records),
            "runtime_warning_blockers": 0,
        }
        required_ledger_paths.update(
            {
                reconciliation_relative,
                "05_results/tables/barcode_reconciliation.csv",
                "05_results/tables/barcode_set_differences.csv",
                "05_results/tables/coordinate_image_qc.json",
                "05_results/tables/spot_qc.csv",
                "05_results/tables/attrition.csv",
                "05_results/tables/cluster_counts.csv",
                "05_results/objects/analysis_final_seurat.rds",
                warning_relative,
            }
        )

    original, final, original_dimensions, final_dimensions = _expected_figures(case, run_root)
    required_ledger_paths.update(path.relative_to(run_root).as_posix() for path in (*original, *final))
    ledger_records, ledger_sha = validate_ledger(run_root, required_ledger_paths)
    return {
        "schema_version": "1.0.0",
        "ok": True,
        "case": case,
        "computational_execution": "passed",
        "native_visual_review": "not_asserted_by_ci",
        "terminal_state": state,
        "runtime": runtime,
        "renv_lock_sha256": lock_sha,
        "execution_summary_sha256": sha256_file(summary_path),
        "artifact_ledger_sha256": ledger_sha,
        "artifact_ledger_records": ledger_records,
        "original_final_figure_pairs": len(original),
        "original_png_dimensions": list(original_dimensions),
        "final_png_dimensions": list(final_dimensions),
        **warning_summary,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=sorted(CASES), required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = validate(args.case, args.run_root)
    except (OSError, json.JSONDecodeError, TutorialOutputError) as exc:
        sys.stderr.write(f"TUTORIAL_CI_VALIDATION_FAILED: {exc}\n")
        return 2
    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8", newline="\n")
    sys.stdout.write(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
