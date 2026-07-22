#!/usr/bin/env python3
"""Initialize and execute the DEP UbiLength teaching workflow in a frozen R environment."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path


ASSET_ROOT = Path(__file__).resolve().parent
SKILL_ROOT = ASSET_ROOT.parents[2]
ENVIRONMENT_MANAGER = SKILL_ROOT / "scripts" / "environment_manager.py"
EXPECTED_SOURCES = {
    "DEP_1.32.0.tar.gz": ("83f8f160fcd6455229d1da98e2ced80d0b0f60ff509fadf00c31cf7dbe1c92d3", 3601589),
    "extracted/DEP/data/UbiLength.rda": ("8452e42419c79e3f299aad0dafccaacab8a60e5395e46f7dde7ef6f79b09605c", 300409),
    "extracted/DEP/data/UbiLength_ExpDesign.rda": ("0a9f9ac38949822454c31d5c101c1abf8ec4bf689387161f126ec44fcafdc3ee", 266),
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_manager_module():
    spec = importlib.util.spec_from_file_location("biomedical_environment_manager", ENVIRONMENT_MANAGER)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load EnvironmentManager: {ENVIRONMENT_MANAGER}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def parse_environment(module, plan_path: Path):
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    environments = plan.get("environments", [])
    if len(environments) != 1:
        raise RuntimeError("This teaching case requires exactly one frozen R environment")
    payload = environments[0]
    if payload.get("runtime") != "r" or payload.get("backend") != "r-renv":
        raise RuntimeError("Expected a task-local r-renv environment")
    spec = module.EnvironmentSpec(
        env_id=payload["env_id"],
        runtime=payload["runtime"],
        backend=payload["backend"],
        path=payload["path"],
        dependencies=tuple(module.Dependency.from_mapping(item) for item in payload["dependencies"]),
        lock_hash=payload["lock_hash"],
        install_strategy=payload.get("install_strategy", "simultaneous"),
        preinstall=tuple(payload.get("preinstall", ())),
    )
    return plan, spec


def audit_sources(source_root: Path) -> list[dict[str, object]]:
    records = []
    for relative, (expected_hash, expected_size) in EXPECTED_SOURCES.items():
        path = source_root / Path(relative)
        if not path.is_file():
            raise FileNotFoundError(path)
        observed_hash = sha256(path)
        if observed_hash != expected_hash or path.stat().st_size != expected_size:
            raise RuntimeError(f"Frozen source contract failed: {relative}")
        records.append(
            {
                "logical_locator": f"official-dep-source://{relative}",
                "sha256": observed_hash,
                "size_bytes": path.stat().st_size,
            }
        )
    return records


def initialize_run(run_root: Path, source_records: list[dict[str, object]], spec, marker: dict) -> None:
    if run_root.exists():
        raise FileExistsError(f"Refusing to overwrite an existing run: {run_root}")
    for relative in (
        "00_request", "01_plan", "02_environment", "03_scripts/modules", "04_intermediate",
        "05_results/tables", "05_results/objects", "06_figures/original", "06_figures/final",
        "06_figures/review", "07_reports", "logs", "manifest", "_staging",
    ):
        (run_root / relative).mkdir(parents=True, exist_ok=True)

    intent = {
        "case_id": "p0-proteomics-dep-ubilength",
        "mode": "run",
        "statistical_unit": "documented experimental replicate/pull-down sample",
        "contrast": "Ubi6 - Ctrl",
        "install_authorization": "environment was frozen before this runner",
    }
    write_json(run_root / "00_request" / "intent.json", intent)
    write_json(
        run_root / "00_request" / "input_manifest.json",
        {"immutable": True, "private_absolute_locators_omitted": True, "source_files": source_records},
    )
    (run_root / "01_plan" / "ANALYSIS_DESIGN.md").write_text(
        "# Analysis design\n\n"
        "Primary estimand: replicate-level measured protein-group enrichment for Ubi6 minus Ctrl. "
        "The primary limma model uses observed log2 LFQ values without imputation; DEP MinProb is "
        "a separately labelled sensitivity analysis. BH adjustment applies across the one-contrast "
        "tested family. No binding, causal, pathway, patient, or population claim is licensed.\n",
        encoding="utf-8",
    )
    workflow = {
        "workflow_instance": run_root.name,
        "frozen_contrast": "Ubi6 - Ctrl",
        "stages": [
            "P10_input_audit", "P20_preprocessing", "P30_primary_limma_no_imputation",
            "P40_dep_minprob_sensitivity", "P50_figures", "P60_analysis_qa",
        ],
        "automatic_method_fallback": False,
    }
    write_json(run_root / "01_plan" / "workflow.plan.json", workflow)

    env_dir = Path(spec.path).resolve()
    for name in ("renv.lock", "environment.locked.json"):
        source = env_dir / name
        if not source.is_file():
            raise FileNotFoundError(source)
        shutil.copy2(source, run_root / "02_environment" / name)
    environment_manifest = {
        "env_id": spec.env_id,
        "runtime": "R 4.5.3",
        "backend": spec.backend,
        "lock_hash": spec.lock_hash,
        "platform": marker.get("platform"),
        "backend_lock_sha256": sha256(run_root / "02_environment" / "renv.lock"),
        "global_changes": False,
    }
    write_json(run_root / "02_environment" / "environment_manifest.json", environment_manifest)
    shutil.copy2(ASSET_ROOT / "run_pipeline.R", run_root / "03_scripts" / "run_pipeline.R")
    shutil.copy2(ASSET_ROOT / "params.yaml", run_root / "03_scripts" / "params.yaml")
    now = utc_now()
    manifest = {
        "run_id": run_root.name,
        "case_id": "p0-proteomics-dep-ubilength",
        "mode": "run",
        "created_at": now,
        "updated_at": now,
        "current_state": "INTAKE",
        "current_stage": None,
        "terminal": False,
        "checkpoints": [],
        "environment": environment_manifest,
    }
    write_json(run_root / "manifest" / "run_manifest.json", manifest)
    (run_root / "manifest" / "artifact_ledger.jsonl").write_text("", encoding="utf-8")
    (run_root / "manifest" / "state_history.jsonl").write_text("", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--environment-plan", type=Path, required=True)
    parser.add_argument("--windows-exit-helper", type=Path)
    parser.add_argument("--timeout-seconds", type=int, default=7200)
    args = parser.parse_args()

    module = load_manager_module()
    plan, spec = parse_environment(module, args.environment_plan.resolve())
    task_root = Path(plan["task_root"]).resolve()
    run_root = args.run_root.resolve()
    if not run_root.is_relative_to(task_root):
        raise RuntimeError("Run root must remain inside the managed task root")
    marker_path = Path(spec.path).resolve() / "environment.locked.json"
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    if marker.get("lock_hash") != spec.lock_hash:
        raise RuntimeError("Environment marker lock hash mismatch")
    helper = args.windows_exit_helper.resolve() if args.windows_exit_helper else None
    if marker.get("windows_exit_workaround", {}).get("enabled") and helper is None:
        raise RuntimeError("Frozen marker requires --windows-exit-helper")

    manager = module.EnvironmentManager(
        task_root,
        cache_root=Path(plan["cache_root"]),
        windows_exit_helper=helper,
        max_attempts=1,
        cache_key_chars=max(16, len(Path(spec.path).resolve().parent.parent.name)),
    )
    handle = module.EnvironmentHandle(spec=spec, state="frozen", verified=True, frozen=True)
    manager.verify(handle)
    source_root = args.source_root.resolve()
    source_records = audit_sources(source_root)
    initialize_run(run_root, source_records, spec, marker)
    authorization = module.ExecutionAuthorization(mode="run", approved=True)
    result = manager.execute(
        handle,
        run_root / "03_scripts" / "run_pipeline.R",
        authorization,
        args=(str(run_root), str(source_root)),
        cwd=task_root,
        timeout=args.timeout_seconds,
    )
    (run_root / "logs" / "analysis.stdout.log").write_text(result.stdout, encoding="utf-8")
    (run_root / "logs" / "analysis.stderr.log").write_text(result.stderr, encoding="utf-8")
    write_json(
        run_root / "logs" / "analysis.execution.json",
        {"returncode": result.returncode, "completed_at": utc_now()},
    )
    if result.returncode != 0:
        failure = manager.report_failure(
            spec,
            stage="analysis-execute",
            command_record={"command": ["Rscript", "<task-local-run_pipeline.R>"], "returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr},
            attempts=1,
        )
        write_json(run_root / "logs" / "failure_report.json", failure)
        print(json.dumps(failure, ensure_ascii=False, indent=2))
        return 2
    print(result.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
