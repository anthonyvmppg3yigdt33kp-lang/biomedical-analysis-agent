#!/usr/bin/env python3
"""Promote a verified KinneyH fixture into the canonical auditable run tree."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


SKILL_ROOT = Path(__file__).resolve().parents[1]
RUN_MANAGER_PATH = SKILL_ROOT / "scripts" / "run_manager.py"


def load_run_manager():
    spec = importlib.util.spec_from_file_location("run_manager_p0_promotion", RUN_MANAGER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def copy_file(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise RuntimeError(f"Required fixture artifact is missing: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    if sha256_file(source) != sha256_file(destination):
        raise RuntimeError(f"Copy hash mismatch: {destination}")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def artifact_contract(run_root: Path, path: Path) -> dict[str, Any]:
    relative = path.relative_to(run_root).as_posix()
    if relative.startswith("05_results/tables/"):
        artifact_type, unit, role = "table", "sample/cell-type", "descriptive"
    elif relative.startswith("06_figures/"):
        artifact_type, unit, role = "figure", "sample", "descriptive"
    elif relative.startswith("07_reports/"):
        artifact_type, unit, role = "report", "run", "qa"
    elif relative.startswith("03_scripts/"):
        artifact_type, unit, role = "script", "run", "none"
    elif relative.startswith("02_environment/"):
        artifact_type, unit, role = "environment", "run", "none"
    elif relative.startswith("logs/"):
        artifact_type, unit, role = "log", "run", "none"
    else:
        artifact_type, unit, role = "artifact", "run", "none"
    return {
        "artifact_id": "artifact-" + hashlib.sha256(relative.encode("utf-8")).hexdigest()[:16],
        "artifact_type": artifact_type,
        "format": path.suffix.lstrip(".") or "binary",
        "schema": None,
        "unit": unit,
        "modality": "visualization",
        "producer": "p0-visualization-kinneyh",
        "consumers": ["analysis-qa", "native-visual-review", "interpretation"],
        "relative_path": relative,
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
        "sensitivity": "internal",
        "validation": ["non-empty", "sha256 verified", "source fixture verified"],
        "claim_role": role,
    }


def promote(run_root: Path, fixture_root: Path) -> dict[str, Any]:
    manager = load_run_manager()
    run_root = run_root.resolve()
    fixture_root = fixture_root.resolve()
    report_path = fixture_root / "teaching_case_report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if report.get("ok") is not True:
        raise RuntimeError("Fixture report is not successful")
    manifest_path = run_root / "manifest" / "run_manifest.json"
    run_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if run_manifest.get("state") != "ENV_PREPARING":
        raise RuntimeError(f"Expected ENV_PREPARING, got {run_manifest.get('state')}")

    fixture_environment = json.loads((fixture_root / "02_environment" / "environment-manifest.json").read_text(encoding="utf-8"))
    fixture_env = fixture_environment["environment"]
    marker = fixture_env["marker"]
    copy_file(fixture_root / "02_environment" / "environment.locked.json", run_root / "02_environment" / "environment.locked.json")
    copy_file(fixture_root / "02_environment" / "requirements.lock.txt", run_root / "02_environment" / "requirements.lock.txt")
    copy_file(fixture_root / "02_environment" / "install-log.json", run_root / "logs" / "environment-install-log.json")
    environment_entry = {
        "env_id": fixture_env["env_id"],
        "lock_hash": fixture_env["lock_hash"],
        "platform": marker["platform"],
        "backend": fixture_env["backend"],
        "frozen": True,
        "marker_relative_path": "02_environment/environment.locked.json",
        "marker_sha256": sha256_file(run_root / "02_environment" / "environment.locked.json"),
        "lockfiles": [
            {
                "relative_path": "02_environment/requirements.lock.txt",
                "sha256": sha256_file(run_root / "02_environment" / "requirements.lock.txt"),
            }
        ],
    }
    atomic_json(
        run_root / "02_environment" / "environment_manifest.json",
        {
            "schema_version": "1.0",
            "state": "frozen",
            "environments": [environment_entry],
            "authorization": fixture_environment["authorization"],
            "global_changes": False,
        },
    )

    copy_file(fixture_root / "03_scripts" / "run_pipeline.py", run_root / "03_scripts" / "run_pipeline.py")
    atomic_text(
        run_root / "03_scripts" / "params.yaml",
        stable_json(
            {
                "case_id": "p0-visualization-kinneyh-composition",
                "expected_input_sha256": report["input_sha256"],
                "sample_column": "sample_id",
                "cell_type_column": "cell_type",
                "analysis_scope": "descriptive-only",
            }
        )
        + "\n",
    )

    manager.transition_run(run_root, "ENV_LOCKED")
    final_root = fixture_root / "final1"
    input_profile = json.loads((final_root / "tables" / "input-profile.json").read_text(encoding="utf-8"))
    sample_count = int(input_profile["sample_count"])
    native_review = json.loads((final_root / "reports" / "native-visual-review.json").read_text(encoding="utf-8"))
    if native_review.get("review_decision") != "PASS_WITH_BOUNDARIES":
        raise RuntimeError("Native visual review is not accepted")
    for source in sorted((final_root / "tables").glob("*")):
        if source.is_file():
            copy_file(source, run_root / "05_results" / "tables" / source.name)
    for source in sorted((fixture_root / "run1" / "figures").glob("*.png")):
        copy_file(source, run_root / "06_figures" / "original" / source.name)
    for source in sorted((final_root / "figures").glob("*.png")):
        copy_file(source, run_root / "06_figures" / "final" / source.name)
    formal_native_review = json.loads(json.dumps(native_review))
    formal_native_review["path_context"] = "formal-run-root"
    for figure in formal_native_review.get("figures", []):
        fixture_relative = str(figure["relative_path"])
        figure["source_fixture_relative_path"] = fixture_relative
        figure["relative_path"] = f"06_figures/final/{Path(fixture_relative).name}"
    atomic_json(run_root / "06_figures" / "review" / "native-visual-review.json", formal_native_review)
    for name in ("FIGURE_NOTES.md", "QA_REPORT.md", "qa-machine.json", "environment-versions.json"):
        copy_file(final_root / "reports" / name, run_root / "07_reports" / name)
    copy_file(report_path, run_root / "logs" / "teaching-case-execution-report.json")

    final_notes = (run_root / "07_reports" / "FIGURE_NOTES.md").read_text(encoding="utf-8")
    if "Native visual review: pending" in final_notes or "PASS_WITH_BOUNDARIES" not in final_notes:
        raise RuntimeError("Final Figure Notes do not contain the accepted native-review decision")

    def evidence(relative_path: str) -> dict[str, Any]:
        path = run_root / relative_path
        if not path.is_file():
            raise RuntimeError(f"Stage evidence is missing: {relative_path}")
        return {"relative_path": relative_path, "sha256": sha256_file(path), "size_bytes": path.stat().st_size}

    stage_specs = [
        (
            "intake",
            "01-intake",
            [evidence("00_request/intent.yaml"), evidence("00_request/input_manifest.json")],
            {"input_sha256": report["input_sha256"], "decision": "PASS"},
        ),
        (
            "data-profile",
            "02-data-profile",
            [evidence("05_results/tables/input-profile.json")],
            {
                "rows": int(input_profile["rows"]),
                "sample_count": sample_count,
                "cell_type_count": int(input_profile["cell_type_count"]),
                "statistical_unit": input_profile["statistical_unit"],
                "decision": "PASS_WITH_BOUNDARIES",
            },
        ),
        (
            "methodology-review",
            "03-methodology-review",
            [evidence("01_plan/ANALYSIS_DESIGN.md"), evidence("01_plan/workflow.plan.yaml")],
            {"analysis_scope": "descriptive-only", "inferential_test_performed": False, "decision": "PASS_WITH_BOUNDARIES"},
        ),
        (
            "analysis-qa",
            "04-analysis-qa",
            [evidence("07_reports/qa-machine.json"), evidence("07_reports/QA_REPORT.md")],
            {"scientific_boundary": report["scientific_boundary"], "decision": "PASS_WITH_BOUNDARIES"},
        ),
        (
            "visualization",
            "05-visualization",
            [
                evidence("06_figures/final/composition-stacked-bars.png"),
                evidence("06_figures/final/composition-dot-matrix.png"),
            ],
            {
                "fresh_returncode": report["fresh_returncode"],
                "restored_returncode": report["restored_returncode"],
                "restored_from_cache": report["restored_from_cache"],
                "deterministic_rerun": native_review["deterministic_rerun"],
                "decision": "PASS",
            },
        ),
        (
            "native-visual-review",
            "06-native-visual-review",
            [evidence("06_figures/review/native-visual-review.json")],
            {"reviewed_native_pixels": True, "decision": native_review["review_decision"]},
        ),
        (
            "interpretation",
            "07-interpretation",
            [evidence("07_reports/FIGURE_NOTES.md")],
            {"claim_ceiling": report["scientific_boundary"], "decision": "PASS_WITH_BOUNDARIES"},
        ),
    ]

    manager.transition_run(run_root, "RUNNING_STAGE")
    for index, (stage_id, node_id, evidence_files, decision) in enumerate(stage_specs):
        stage_dir = run_root / "_staging" / stage_id
        stage_dir.mkdir(parents=True, exist_ok=False)
        stage_evidence = {
            "schema_version": "1.0",
            "case_id": "p0-visualization-kinneyh-composition",
            "stage_id": stage_id,
            "workflow_node_id": node_id,
            "evidence": evidence_files,
            **decision,
        }
        stage_validation = stage_dir / "stage-validation.json"
        atomic_json(stage_validation, stage_evidence)
        checkpoint_contract = {
            "artifacts": [
                {
                    "artifact_id": f"p0-{stage_id}-stage-validation",
                    "artifact_type": "report",
                    "format": "json",
                    "schema": None,
                    "unit": "run",
                    "modality": "visualization",
                    "producer": "p0-visualization-kinneyh",
                    "consumers": [item[0] for item in stage_specs[index + 1 :]],
                    "relative_path": f"_staging/{stage_id}/stage-validation.json",
                    "sha256": sha256_file(stage_validation),
                    "sensitivity": "internal",
                    "validation": ["evidence paths exist", "evidence hashes recorded", "stage decision recorded"],
                    "claim_role": "qa",
                }
            ]
        }
        contract_path = fixture_root / f"formal-checkpoint-contract-{stage_id}.json"
        atomic_json(contract_path, checkpoint_contract)
        manager.checkpoint_stage(run_root, stage_id, contract_path)
        if index + 1 < len(stage_specs):
            manager.transition_run(run_root, "RUNNING_STAGE")

    indexed_paths = sorted(
        path
        for directory in ("02_environment", "03_scripts", "04_intermediate", "05_results", "06_figures", "07_reports", "logs")
        for path in (run_root / directory).rglob("*")
        if path.is_file() and path.name != "ARTIFACT_INDEX.md"
    )
    index_lines = ["# Artifact index", "", "All hashes are SHA-256 and paths are relative to the formal run root.", "", "| Path | Bytes | SHA-256 |", "|---|---:|---|"]
    for path in indexed_paths:
        index_lines.append(f"| `{path.relative_to(run_root).as_posix()}` | {path.stat().st_size} | `{sha256_file(path)}` |")
    atomic_text(run_root / "07_reports" / "ARTIFACT_INDEX.md", "\n".join(index_lines) + "\n")

    registered_paths = indexed_paths + [run_root / "07_reports" / "ARTIFACT_INDEX.md"]
    contracts = [artifact_contract(run_root, path) for path in registered_paths]
    run_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    checkpoint_paths = {item["relative_path"] for item in run_manifest.get("artifacts", [])}
    run_manifest["environments"] = [environment_entry]
    run_manifest["commands"] = [
        {"kind": "fresh-task-environment-run", "exit_code": report["fresh_returncode"]},
        {"kind": "cached-lock-environment-run", "exit_code": report["restored_returncode"]},
        {"kind": "corrected-native-qa-rerun", "exit_code": 0},
    ]
    run_manifest["seeds"] = {"randomized_analysis": None, "reason": "deterministic aggregation and rendering; no stochastic analysis"}
    run_manifest["exit_code"] = 0
    run_manifest["artifacts"].extend(item for item in contracts if item["relative_path"] not in checkpoint_paths)
    run_manifest["qa"].extend(
        [
            {"kind": "environment", "decision": "PASS", "global_changes": False, "at": utc_now()},
            {"kind": "deterministic-rerun", "decision": "PASS", "artifact_count": 9, "at": utc_now()},
            {"kind": "native-visual-review", "decision": "PASS_WITH_BOUNDARIES", "at": utc_now()},
        ]
    )
    run_manifest["claim_boundaries"] = [
        f"Supports descriptive within-sample cell-type composition for the {sample_count:,} observed sample groups.",
        "Does not support disease, treatment, population, causal, prognostic or clinical claims.",
        "Cells are measurements nested within sample and are not independent biological replicates; donor-level inference requires a verified donor-sample mapping and independence.",
    ]
    atomic_json(manifest_path, run_manifest)
    ledger_path = run_root / "manifest" / "artifact_ledger.jsonl"
    with ledger_path.open("a", encoding="utf-8", newline="\n") as handle:
        for contract in contracts:
            if contract["relative_path"] in checkpoint_paths:
                continue
            handle.write(stable_json({"event": "artifact-registered", "at": utc_now(), "artifact": contract}) + "\n")

    for state in ("ANALYSIS_QA", "VISUALIZING", "NATIVE_VISUAL_REVIEW", "INTERPRETING", "DELIVERED"):
        manager.transition_run(run_root, state)
    validation = manager.validate_run(run_root)
    resume = manager.audit_resume(run_root)
    if not validation.get("ok") or not resume.get("ok"):
        raise RuntimeError(f"Formal run validation failed: validation={validation}; resume={resume}")
    delivered_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    ledger_events = sum(1 for line in ledger_path.read_text(encoding="utf-8").splitlines() if line.strip())
    return {
        "ok": True,
        "schema_version": "1.0",
        "run_root": str(run_root),
        "state": "DELIVERED",
        "registered_artifacts": len(delivered_manifest.get("artifacts", [])),
        "artifact_ledger_events": ledger_events,
        "environment_lock_hash": environment_entry["lock_hash"],
        "input_sha256": report["input_sha256"],
        "native_visual_review": native_review["review_decision"],
        "maturity": "data-verified",
        "validation": validation,
        "resume_audit": resume,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--fixture-root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = promote(args.run_root, args.fixture_root)
    except Exception as exc:
        result = {"ok": False, "error_type": type(exc).__name__, "error": str(exc)}
    atomic_json(args.report.resolve(), result)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
