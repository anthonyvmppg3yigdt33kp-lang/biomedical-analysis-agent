#!/usr/bin/env python3
"""Create a real checkpoint and prove hash-bound resume auditing.

The fixture consumes a previously frozen EnvironmentManager directory, copies lock
evidence into a new run, promotes one staged artifact, detects a tampered lockfile, and
restores the valid prior evidence. It does not execute a biomedical method.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Sequence


SKILL_ROOT = Path(__file__).resolve().parents[1]
RUN_MANAGER_PATH = SKILL_ROOT / "scripts" / "run_manager.py"


def load_run_manager():
    spec = importlib.util.spec_from_file_location("run_manager_resume_fixture", RUN_MANAGER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def run_fixture(task_root: Path, environment_dir: Path) -> dict[str, Any]:
    module = load_run_manager()
    task_root = task_root.resolve()
    environment_dir = environment_dir.resolve()
    marker_source = environment_dir / "environment.locked.json"
    lock_source = environment_dir / "requirements.lock.txt"
    if not marker_source.is_file() or not lock_source.is_file():
        raise FileNotFoundError("Frozen marker and requirements.lock.txt are required")
    marker = json.loads(marker_source.read_text(encoding="utf-8"))
    input_path = task_root / "inputs" / "counts.tsv"
    input_path.parent.mkdir(parents=True, exist_ok=True)
    if input_path.exists() and input_path.read_text(encoding="utf-8") != "gene\tsample_1\ngene_a\t10\n":
        raise RuntimeError(f"Refusing to overwrite changed fixture input: {input_path}")
    input_path.write_text("gene\tsample_1\ngene_a\t10\n", encoding="utf-8", newline="\n")
    request = {
        "mode": "plan",
        "question": "Validate a deterministic bulk RNA checkpoint and resume contract",
        "modality": "bulk-rna",
        "project_root": str(task_root / "project"),
        "task_slug": "checkpoint-fixture",
        "run_id": "run-v1",
        "inputs": [str(input_path)],
        "statistical_unit": "sample",
        "group_column": "group",
        "multiplicity_method": "BH FDR",
        "data_scale": "raw-counts",
    }
    initial = module.initialise_run(request)
    run_root = Path(initial["run_root"])
    for state in ("AWAITING_AUTHORIZATION", "ENV_PREPARING", "ENV_LOCKED", "RUNNING_STAGE"):
        module.transition_run(run_root, state)

    stage_id = "bulk-rna"
    staging = run_root / "_staging" / stage_id
    staging.mkdir()
    result = staging / "profile.tsv"
    result.write_text("metric\tvalue\nrows\t1\ncolumns\t1\n", encoding="utf-8", newline="\n")
    contract = {
        "artifacts": [{
            "artifact_id": "fixture-data-profile",
            "artifact_type": "table",
            "format": "tsv",
            "schema": None,
            "unit": "sample",
            "modality": "bulk-rna",
            "producer": stage_id,
            "consumers": ["resume-fixture"],
            "relative_path": f"_staging/{stage_id}/profile.tsv",
            "sha256": module.sha256_file(result),
            "sensitivity": "internal",
            "validation": ["non-empty", "sha256 recorded"],
            "claim_role": "none",
        }]
    }
    contract_path = task_root / "checkpoint-contract.json"
    atomic_json(contract_path, contract)
    checkpoint = module.checkpoint_stage(run_root, stage_id, contract_path)

    env_dir = run_root / "02_environment"
    marker_target = env_dir / "python.environment.locked.json"
    lock_target = env_dir / "python.requirements.lock.txt"
    shutil.copy2(marker_source, marker_target)
    shutil.copy2(lock_source, lock_target)
    environment_manifest = {
        "schema_version": "1.0",
        "state": "frozen",
        "global_changes": False,
        "environments": [{
            "env_id": marker.get("env_id"),
            "backend": marker.get("backend"),
            "platform": marker.get("platform"),
            "lock_hash": marker.get("lock_hash"),
            "frozen": True,
            "marker_relative_path": marker_target.relative_to(run_root).as_posix(),
            "marker_sha256": module.sha256_file(marker_target),
            "lockfiles": [{
                "relative_path": lock_target.relative_to(run_root).as_posix(),
                "sha256": module.sha256_file(lock_target),
            }],
        }],
    }
    atomic_json(env_dir / "environment_manifest.json", environment_manifest)
    ready = module.audit_resume(run_root)
    if not ready["ok"]:
        raise RuntimeError(f"Valid resume audit failed: {ready['errors']}")

    original_lock = lock_target.read_bytes()
    lock_target.write_bytes(original_lock + b"tampered\n")
    tampered = module.audit_resume(run_root)
    lock_target.write_bytes(original_lock)
    restored = module.audit_resume(run_root)
    if tampered["ok"] or "environment-lock-not-verified" not in tampered["errors"]:
        raise RuntimeError("Tampered lock evidence was not rejected")
    if not restored["ok"]:
        raise RuntimeError(f"Restored resume audit failed: {restored['errors']}")
    return {
        "ok": True,
        "schema_version": "1.0",
        "run_root": str(run_root),
        "initial_state": initial["state"],
        "checkpoint": checkpoint,
        "resume_ready": ready,
        "tampered_lock_detected": True,
        "tampered_errors": tampered["errors"],
        "restored_resume_ready": restored["ok"],
        "input_sha256": module.sha256_file(input_path),
        "checkpoint_sha256": module.sha256_file(run_root / "04_intermediate" / stage_id / "profile.tsv"),
        "environment_marker_sha256": module.sha256_file(marker_target),
        "environment_lockfile_sha256": module.sha256_file(lock_target),
        "scientific_boundary": "This fixture validates checkpoint, immutable input, environment-lock and resume controls only; no biomedical result was generated.",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-root", type=Path, required=True)
    parser.add_argument("--environment-dir", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = run_fixture(args.task_root, args.environment_dir)
    except Exception as exc:
        report = {"ok": False, "error_type": type(exc).__name__, "error": str(exc)}
    atomic_json(args.report.resolve(), report)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
