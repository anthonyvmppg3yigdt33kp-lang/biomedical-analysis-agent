#!/usr/bin/env python3
"""Export a verified PBMC3K run as a small public teaching reference."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from case_driver import FIGURES, verify_run


CASE_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT = CASE_DIR / "expected-output"
TEXT_SUFFIXES = {".csv", ".json", ".jsonl", ".md", ".txt"}
FORBIDDEN_SUFFIXES = {".dll", ".h5", ".h5ad", ".rda", ".rds", ".zip"}
WINDOWS_ABSOLUTE = re.compile(r"(?i)(?:(?<![a-z0-9])[a-z]:[\\/]|\\\\[^\\/\s]+[\\/])")
FORBIDDEN_PARTS = {"02_environment", "04_intermediate", "cache", "raw", "runtime"}


class ExportError(RuntimeError):
    """Raised when a public-export gate fails."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_text_lf(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    path.write_text(normalized, encoding="utf-8", newline="\n")


def _write_json(path: Path, value: Any) -> None:
    _write_text_lf(
        path,
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def _copy_file(source: Path, target: Path) -> None:
    if not source.is_file() or source.stat().st_size == 0:
        raise ExportError(f"required source artifact is missing or empty: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.suffix.lower() in TEXT_SUFFIXES:
        _write_text_lf(target, source.read_text(encoding="utf-8-sig"))
    else:
        shutil.copyfile(source, target)


def _kind(relative: str) -> str:
    if relative.startswith("figures/"):
        return "figure-review" if "/review/" in relative else "figure"
    if relative.startswith("tables/"):
        return "derived-table"
    if relative.startswith("reports/"):
        return "report"
    if relative.startswith("manifest/"):
        return "provenance"
    return "documentation"


def _payload_files(root: Path, *, include_index: bool) -> list[Path]:
    excluded = {"manifest/artifact_ledger.jsonl"}
    if not include_index:
        excluded.add("ARTIFACT_INDEX.md")
    return sorted(
        (
            path
            for path in root.rglob("*")
            if path.is_file() and path.relative_to(root).as_posix() not in excluded
        ),
        key=lambda path: path.relative_to(root).as_posix(),
    )


def _assert_public_tree(root: Path) -> None:
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        lowered_parts = {part.lower() for part in relative.parts}
        if lowered_parts & FORBIDDEN_PARTS:
            raise ExportError(f"forbidden path in public export: {relative.as_posix()}")
        suffixes = {suffix.lower() for suffix in path.suffixes}
        if suffixes & FORBIDDEN_SUFFIXES or path.name.lower().endswith(".tar.gz"):
            raise ExportError(f"forbidden payload type in public export: {relative.as_posix()}")
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        text = path.read_text(encoding="utf-8-sig")
        if WINDOWS_ABSOLUTE.search(text):
            raise ExportError(f"absolute Windows path leaked into {relative.as_posix()}")
        if re.search(r"(?i)(?:/users/|/home/)[^\s/]+/", text):
            raise ExportError(f"absolute home path leaked into {relative.as_posix()}")


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def export_run(run_root: Path, output: Path) -> dict[str, Any]:
    if not run_root.is_absolute() or not run_root.is_dir():
        raise ExportError("--run-root must be an existing absolute directory")
    if output.resolve(strict=False) != DEFAULT_OUTPUT.resolve(strict=False):
        raise ExportError("public teaching output must target examples/pbmc3k/expected-output")
    if output.exists():
        raise ExportError("expected-output already exists; review and remove it explicitly before regeneration")

    passed, verification = verify_run(run_root)
    if not passed or verification.get("status") != "pass":
        raise ExportError(f"source run verification failed: {verification.get('failures', [])}")
    if verification.get("native_visual_review_pass") is not True:
        raise ExportError("source run has not passed native visual review")

    source_summary = _read_json(run_root / "manifest/execution-summary.json")
    source_manifest = _read_json(run_root / "manifest/run_manifest.json")
    reuse_evidence = _read_json(run_root / "logs/environment-cache-reuse.json")
    if source_summary.get("state") != "DELIVERED" or source_summary.get("maturity") != "native-reviewed":
        raise ExportError("source execution summary is not a delivered native-reviewed run")
    if (
        reuse_evidence.get("reuse") is not True
        or reuse_evidence.get("host_package_required") is not False
        or reuse_evidence.get("shutdown_mode") != "native_exit"
    ):
        raise ExportError("source run lacks explicit task-local environment reuse evidence")
    execution_evidence = source_summary.get("execution_evidence", {})
    if (
        execution_evidence.get("environment_shutdown_mode") != "native_exit"
        or execution_evidence.get("pipeline_shutdown_mode") != "native_exit"
        or any("helper" in str(key).lower() for key in execution_evidence)
    ):
        raise ExportError("source run lacks clean native-exit execution evidence")

    staging = Path(tempfile.mkdtemp(prefix=".expected-output-staging-", dir=CASE_DIR))
    try:
        for name in (
            "canonical_metrics.csv",
            "feature_name_mapping.csv",
            "feature_name_mapping_summary.csv",
            "umap_runtime_contract.json",
            "qc_summary.csv",
            "cluster_sizes.csv",
            "annotation_evidence.csv",
            "cluster_markers.csv",
        ):
            _copy_file(run_root / "05_results/tables" / name, staging / "tables" / name)

        for figure_id in FIGURES:
            for variant in ("original", "final"):
                _copy_file(
                    run_root / f"06_figures/{variant}/{figure_id}.png",
                    staging / f"figures/{variant}/{figure_id}.png",
                )
            review = _read_json(run_root / f"06_figures/review/{figure_id}.review.json")
            if review.get("status") != "native-reviewed" or review["rounds"][-1].get("decision") != "keep":
                raise ExportError(f"review is not terminal keep: {figure_id}")
            latest = review["rounds"][-1]
            latest["original"]["path"] = f"figures/original/{figure_id}.png"
            latest["final"]["path"] = f"figures/final/{figure_id}.png"
            _write_json(staging / f"figures/review/{figure_id}.review.json", review)

        report_replacements = {
            "QA_REPORT.md": (("input_evidence.json", "../manifest/input-evidence.json"),),
            "FIGURE_NOTES.md": (("`06_figures/review/`", "`../figures/review/`"),),
            "RESULTS.md": (),
        }
        for name, replacements in report_replacements.items():
            text = (run_root / "07_reports" / name).read_text(encoding="utf-8")
            for old, new in replacements:
                text = text.replace(old, new)
            target = staging / "reports" / name
            _write_text_lf(target, text)

        _copy_file(CASE_DIR / "input_manifest.json", staging / "manifest/input-manifest.json")
        _copy_file(run_root / "00_request/input_evidence.json", staging / "manifest/input-evidence.json")
        _write_json(staging / "manifest/environment-cache-reuse.json", reuse_evidence)
        _copy_file(
            run_root / "logs/environment-process-evidence.json",
            staging / "manifest/environment-process-evidence.json",
        )
        pipeline_process_evidence = _read_json(
            run_root / "logs/r-pipeline-process-evidence.json"
        )
        normalized_umap_sha256 = _sha256(staging / "tables/umap_runtime_contract.json")
        pipeline_process_evidence["analysis_runtime_contract"]["sha256"] = (
            normalized_umap_sha256
        )
        pipeline_process_path = staging / "manifest/r-pipeline-process-evidence.json"
        _write_json(pipeline_process_path, pipeline_process_evidence)

        exported_evidence_hashes = {
            "environment_process_evidence_sha256": _sha256(
                staging / "manifest/environment-process-evidence.json"
            ),
            "pipeline_process_evidence_sha256": _sha256(pipeline_process_path),
            "umap_runtime_contract_sha256": normalized_umap_sha256,
        }
        for payload in (source_summary, source_manifest, verification):
            payload.setdefault("execution_evidence", {}).update(exported_evidence_hashes)

        _write_json(staging / "manifest/execution-summary.json", source_summary)
        _write_json(staging / "manifest/run-manifest.json", source_manifest)
        _write_json(staging / "manifest/verification-summary.json", verification)

        source_record = {
            "analysis_signature": source_manifest["analysis_signature"],
            "canonical_metrics": source_summary["canonical_metrics"],
            "case_id": "pbmc3k",
            "excluded_from_export": [
                "raw input archive and extracted matrix",
                "RDS objects and stage checkpoints",
                "task-local R libraries, cache and process logs",
                "cell-level metadata and QC audit tables",
            ],
            "raw_data_distributed": False,
            "schema_version": "1.0.0",
            "source_run_id": run_root.name,
            "source_verification_status": verification["status"],
        }
        _write_json(staging / "manifest/source-run.json", source_record)

        readme_lines = [
            "# PBMC3K verified expected output",
            "",
            "This directory is a compact, public, derived teaching reference exported from",
            f"`{run_root.name}` after fresh execution, checkpoint resume, task-local",
            "environment cache reuse, report generation, and native review of all five",
            "original/final figure pairs.",
            "",
            "Canonical result: **2,700 input cells -> 2,638 QC-retained cells -> 9 descriptive",
            "clusters** under R 4.5.3 and Seurat 5.5.0.",
            "The feature-name mapping records the explicit underscore-to-dash normalization",
            "performed before Seurat object creation and proves dimensions/count values unchanged.",
            "",
            "The directory contains derived tables, reports, figures and hash-bound",
            "provenance only. It does **not** contain the 10x archive, extracted matrices,",
            "cell-level exports, RDS objects, checkpoints, package libraries, caches, process",
            "logs, or absolute workstation paths. Native-exit process records retain only",
            "return codes, architecture, forbidden-scan results and cryptographic hashes.",
            "The input data remain separately",
            "attributed to 10x Genomics under CC BY 4.0; repository MIT terms cover only",
            "original code and documentation.",
            "",
            "Verify from the repository root:",
            "",
            "```powershell",
            "python examples/pbmc3k/verify_expected_output.py",
            "```",
            "",
            "`ARTIFACT_INDEX.md` is the human-readable payload index. The append-only-style",
            "`manifest/artifact_ledger.jsonl` binds every other exported file, including the",
            "index, to its byte size and SHA-256.",
            "",
        ]
        _write_text_lf(staging / "README.md", "\n".join(readme_lines))

        index_lines = [
            "# PBMC3K expected-output artifact index",
            "",
            "All listed artifacts are public derived outputs. Raw data, RDS objects, environments and caches are excluded.",
            "",
            "| Path | Kind | Bytes | SHA-256 |",
            "|---|---|---:|---|",
        ]
        for path in _payload_files(staging, include_index=False):
            relative = path.relative_to(staging).as_posix()
            index_lines.append(f"| `{relative}` | {_kind(relative)} | {path.stat().st_size} | `{_sha256(path)}` |")
        index_lines.extend(
            [
                "",
                "The ledger additionally binds this index; the ledger cannot self-hash and therefore excludes only itself.",
            ]
        )
        _write_text_lf(staging / "ARTIFACT_INDEX.md", "\n".join(index_lines) + "\n")

        ledger_path = staging / "manifest/artifact_ledger.jsonl"
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        records = []
        for sequence, path in enumerate(_payload_files(staging, include_index=True), start=1):
            relative = path.relative_to(staging).as_posix()
            records.append(
                {
                    "kind": _kind(relative),
                    "path": relative,
                    "provenance": "verified-public-derived-output",
                    "schema_version": "1.0.0",
                    "sequence": sequence,
                    "sha256": _sha256(path),
                    "size_bytes": path.stat().st_size,
                }
            )
        _write_text_lf(
            ledger_path,
            "".join(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in records),
        )

        _assert_public_tree(staging)
        os.replace(staging, output)
    except Exception:
        if staging.exists() and staging.name.startswith(".expected-output-staging-") and staging.parent == CASE_DIR:
            shutil.rmtree(staging)
        raise

    return {
        "case_id": "pbmc3k",
        "files": sum(1 for path in output.rglob("*") if path.is_file()),
        "output": "examples/pbmc3k/expected-output",
        "source_run_id": run_root.name,
        "status": "exported",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", required=True, type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = export_run(args.run_root, DEFAULT_OUTPUT)
    except (ExportError, OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
        print(f"PBMC3K_EXPECTED_OUTPUT_EXPORT_FAILED: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
