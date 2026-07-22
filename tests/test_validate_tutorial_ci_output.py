import binascii
import hashlib
import importlib.util
import json
import struct
import zlib
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "validate_tutorial_ci_output.py"
SPEC = importlib.util.spec_from_file_location("validate_tutorial_ci_output", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def png_chunk(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", binascii.crc32(kind + payload) & 0xFFFFFFFF)


def write_png(path: Path, width: int, height: int):
    path.parent.mkdir(parents=True, exist_ok=True)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    raw = (b"\x00" + b"\x00" * width) * height
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", ihdr)
        + png_chunk(b"IDAT", zlib.compress(raw, level=9))
        + png_chunk(b"IEND", b"")
    )


def write_lock(run_root: Path, case: str):
    lock = {
        "R": {"Version": "4.5.3", "Repositories": [{"Name": "CRAN", "URL": MODULE.SNAPSHOT}]},
        "Packages": {"Seurat": {"Version": "5.5.0"}, "renv": {"Version": "1.2.2"}},
    }
    lock_path = run_root / "02_environment" / "renv.lock"
    write_json(lock_path, lock)
    digest = sha256(lock_path)
    if case == "pbmc3k":
        marker = {
            "r_version": "4.5.3",
            "verified": True,
            "frozen": True,
            "packages": {"Seurat": "5.5.0", "renv": "1.2.2"},
            "repository_snapshot": MODULE.SNAPSHOT,
            "package_type": "win.binary",
            "backend_lock": {"path": "renv.lock", "sha256": digest},
        }
    else:
        marker = {
            "status": "frozen",
            "r_version": "4.5.3",
            "seurat_version": "5.5.0",
            "task_local_renv_version": "1.2.2",
            "bootstrap_renv_version": "1.2.2",
            "packages": {"Seurat": "5.5.0", "renv": "1.2.2"},
            "repository": {"snapshot_url": MODULE.SNAPSHOT, "package_type": "binary"},
            "renv_lock_sha256": digest,
        }
    write_json(run_root / "02_environment" / "environment.locked.json", marker)


def write_ledger(run_root: Path, paths: list[Path], *, artifact_ids: bool = False):
    ledger = run_root / "manifest" / "artifact_ledger.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    records = []
    for index, path in enumerate(paths, 1):
        record = {
            "sequence": index,
            "path": path.relative_to(run_root).as_posix(),
            "size_bytes": path.stat().st_size,
            "sha256": sha256(path),
        }
        if artifact_ids:
            record["artifact_id"] = f"fixture-{index}"
        records.append(record)
    ledger.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")


def make_pbmc(run_root: Path):
    summary = {
        "schema_version": "1.0.0",
        "case_id": "pbmc3k",
        "state": "NATIVE_VISUAL_REVIEW",
        "maturity": "data-verified",
        "canonical_metrics": {"input_cells": 2700, "qc_retained_cells": 2638, "clusters": 9},
        "canonical_metrics_pass": True,
    }
    summary_path = run_root / "manifest" / "execution-summary.json"
    write_json(summary_path, summary)
    write_lock(run_root, "pbmc3k")
    write_json(
        run_root / "03_scripts" / "params.json",
        {"visual": {"dpi": 10, "original_width_in": 4, "original_height_in": 3, "final_width_in": 2, "final_height_in": 2}},
    )
    figures = []
    for name in ("qc_violin", "pca_clusters", "umap_clusters", "umap_annotation", "marker_dotplot"):
        original = run_root / "06_figures" / "original" / f"{name}.png"
        final = run_root / "06_figures" / "final" / f"{name}.png"
        write_png(original, 40, 30)
        write_png(final, 20, 20)
        figures.extend((original, final))
    metrics = run_root / "05_results" / "tables" / "canonical_metrics.csv"
    metrics.parent.mkdir(parents=True, exist_ok=True)
    metrics.write_text("metric,value\ninput_cells,2700\n", encoding="utf-8")
    covered = [
        run_root / "02_environment" / "environment.locked.json",
        run_root / "02_environment" / "renv.lock",
        metrics,
        summary_path,
        *figures,
    ]
    write_ledger(run_root, covered)
    return summary_path, figures


def make_visium(run_root: Path):
    directed = {name: 0 for name in MODULE.VISIUM_DIRECTED_DIFFERENCES}
    summary = {
        "schema_version": "1.0",
        "case": "visium-mouse-brain",
        "status": "NATIVE_VISUAL_REVIEW",
        "validation": {"runtime_warnings": "passed"},
        "observed": {
            "matrix_barcodes": 100,
            "loaded_spots": 80,
            "coordinate_barcodes": 80,
            "retained_spots": 70,
            "directed_assay_image_coordinate_differences": directed,
        },
    }
    write_json(run_root / "manifest" / "execution-summary.json", summary)
    write_lock(run_root, "visium-mouse-brain")
    write_json(
        run_root / "03_scripts" / "visual-params.json",
        {
            "render_round": 1,
            "original_export": {"width_in": 4, "height_in": 3, "dpi": 10},
            "final_export": {"width_in": 2, "height_in": 2, "dpi": 10},
        },
    )
    pipeline = run_root / "03_scripts" / "run_pipeline.R"
    pipeline.write_text("options(warn = 1)\n", encoding="utf-8")
    analysis = run_root / "03_scripts" / "analysis-params.json"
    write_json(analysis, {"seed": 20260722})
    warning_evidence = run_root / "logs" / "pipeline-warnings.json"
    write_json(
        warning_evidence,
        {
            "schema_version": "1.0",
            "classification_version": "1.0",
            "case": "visium-mouse-brain",
            "status": "passed",
            "warning_free": True,
            "warning_occurrences": 0,
            "unique_warning_records": 0,
            "blocking_warning_occurrences": 0,
            "scientific_parameters_changed": False,
            "records": [],
            "code_hash": sha256(pipeline),
            "analysis_config_hash": sha256(analysis),
            "environment_lock_hash": sha256(run_root / "02_environment" / "renv.lock"),
            "absolute_paths_included": False,
        },
    )
    reconciliation = run_root / "05_results" / "tables" / "barcode_set_reconciliation.json"
    write_json(reconciliation, {"status": "passed", "directed_difference_counts": directed})
    supporting = [
        run_root / "05_results" / "tables" / name
        for name in (
            "barcode_reconciliation.csv",
            "barcode_set_differences.csv",
            "coordinate_image_qc.json",
            "spot_qc.csv",
            "attrition.csv",
            "cluster_counts.csv",
        )
    ] + [run_root / "05_results" / "objects" / "analysis_final_seurat.rds"]
    for path in supporting:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture\n")
    figures = []
    for name in ("clusters", "hpca", "ttr"):
        original = run_root / "06_figures" / "original" / "round-1" / f"{name}.png"
        final = run_root / "06_figures" / "final" / "round-1" / f"{name}.png"
        write_png(original, 40, 30)
        write_png(final, 20, 20)
        figures.extend((original, final))
    write_ledger(run_root, [reconciliation, *supporting, warning_evidence, *figures], artifact_ids=True)
    return reconciliation, figures, warning_evidence


def test_pbmc_computational_output_is_content_validated_without_native_claim(tmp_path):
    make_pbmc(tmp_path)
    report = MODULE.validate("pbmc3k", tmp_path)
    assert report["ok"] is True
    assert report["runtime"] == {"R": "4.5.3", "Seurat": "5.5.0", "renv": "1.2.2"}
    assert report["native_visual_review"] == "not_asserted_by_ci"
    assert report["original_final_figure_pairs"] == 5


def test_non_allowlisted_terminal_state_is_rejected(tmp_path):
    summary_path, _ = make_pbmc(tmp_path)
    payload = json.loads(summary_path.read_text(encoding="utf-8"))
    payload["state"] = "SUCCESS"
    write_json(summary_path, payload)
    with pytest.raises(MODULE.TutorialOutputError, match="non-allowlisted"):
        MODULE.validate("pbmc3k", tmp_path)


def test_fake_png_and_pair_basename_mismatch_are_rejected(tmp_path):
    _, figures = make_pbmc(tmp_path)
    figures[0].write_bytes(b"fake-png")
    with pytest.raises(MODULE.TutorialOutputError, match="PNG"):
        MODULE.validate("pbmc3k", tmp_path)


def test_visium_requires_exact_six_zero_three_party_differences_and_ledger_coverage(tmp_path):
    reconciliation, figures, warning_evidence = make_visium(tmp_path)
    report = MODULE.validate("visium-mouse-brain", tmp_path)
    assert report["ok"] is True
    assert report["original_final_figure_pairs"] == 3
    assert report["runtime_warning_evidence_sha256"] == sha256(warning_evidence)
    assert report["executed_pipeline_sha256"] == sha256(tmp_path / "03_scripts" / "run_pipeline.R")
    assert report["analysis_config_sha256"] == sha256(tmp_path / "03_scripts" / "analysis-params.json")
    assert report["runtime_warning_occurrences"] == 0
    assert report["runtime_warning_records"] == 0
    assert report["runtime_warning_blockers"] == 0

    payload = json.loads(reconciliation.read_text(encoding="utf-8"))
    payload["directed_difference_counts"]["unexpected_pair"] = 0
    write_json(reconciliation, payload)
    with pytest.raises(MODULE.TutorialOutputError, match="exact six"):
        MODULE.validate("visium-mouse-brain", tmp_path)

    payload["directed_difference_counts"].pop("unexpected_pair")
    write_json(reconciliation, payload)
    ledger = tmp_path / "manifest" / "artifact_ledger.jsonl"
    records = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    records = [record for record in records if record["path"] != figures[0].relative_to(tmp_path).as_posix()]
    ledger.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    with pytest.raises(MODULE.TutorialOutputError, match="lacks required coverage"):
        MODULE.validate("visium-mouse-brain", tmp_path)


def test_visium_warning_evidence_is_mandatory_and_fail_closed(tmp_path):
    _, _, warning_evidence = make_visium(tmp_path)
    warning_evidence.unlink()
    with pytest.raises(MODULE.TutorialOutputError, match="runtime warning evidence is missing"):
        MODULE.validate("visium-mouse-brain", tmp_path)

    _, _, warning_evidence = make_visium(tmp_path)
    payload = json.loads(warning_evidence.read_text(encoding="utf-8"))
    payload.update(
        {
            "status": "blocked",
            "warning_free": False,
            "warning_occurrences": 1,
            "unique_warning_records": 1,
            "blocking_warning_occurrences": 1,
            "records": [
                {
                    "stage_key": "S40_PREPROCESS",
                    "message": "iteration limit reached",
                    "call": "theta.ml(...) ",
                    "count": 1,
                    "category": "sctransform_theta_iteration_limit",
                    "severity": "release_blocker",
                    "allowlisted": False,
                }
            ],
        }
    )
    write_json(warning_evidence, payload)
    with pytest.raises(MODULE.TutorialOutputError, match="release-blocked"):
        MODULE.validate("visium-mouse-brain", tmp_path)
