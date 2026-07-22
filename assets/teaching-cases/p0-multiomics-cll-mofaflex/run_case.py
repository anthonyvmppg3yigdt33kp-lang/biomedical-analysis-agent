#!/usr/bin/env python3
"""Execute the CLL MOFA-FLEX teaching workflow through a frozen Python environment."""

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


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


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
        raise RuntimeError("This teaching case requires exactly one frozen Python environment")
    payload = environments[0]
    if payload.get("runtime") != "python" or payload.get("backend") != "python-uv":
        raise RuntimeError("Expected a task-local python-uv environment")
    spec = module.EnvironmentSpec(
        env_id=payload["env_id"], runtime=payload["runtime"], backend=payload["backend"], path=payload["path"],
        dependencies=tuple(module.Dependency.from_mapping(item) for item in payload["dependencies"]),
        lock_hash=payload["lock_hash"], install_strategy=payload.get("install_strategy", "simultaneous"),
        preinstall=tuple(payload.get("preinstall", ())),
    )
    return plan, spec


def initialize_run(run_root: Path, data: Path, model: Path, source_spec: Path, spec, marker: dict) -> None:
    if run_root.exists():
        raise FileExistsError(f"Refusing to overwrite an existing run: {run_root}")
    for relative in (
        "00_request", "01_plan", "02_environment", "03_scripts/modules", "04_intermediate",
        "05_results/tables", "05_results/objects", "06_figures/original", "06_figures/final",
        "06_figures/review", "07_reports", "logs", "manifest",
    ):
        (run_root / relative).mkdir(parents=True, exist_ok=True)
    write_json(
        run_root / "00_request" / "intent.json",
        {
            "case_id": "p0-multiomics-cll-mofa", "analysis_id": "p0-multiomics-cll-mofaflex",
            "mode": "run", "statistical_unit": "patient", "patient_policy": "200-patient union",
            "claim_scope": "exploratory and descriptive only",
        },
    )
    write_json(
        run_root / "00_request" / "input_manifest.json",
        {
            "source_policy": "read_only",
            "private_absolute_locators_omitted": True,
            "inputs": [
                {"role": "dataset", "logical_locator": "local:cll.h5mu", "sha256": sha256(data), "size_bytes": data.stat().st_size},
                {"role": "pretrained_model", "logical_locator": "local:cll_model.h5", "sha256": sha256(model), "size_bytes": model.stat().st_size},
                {"role": "source_spec", "logical_locator": "local:source-spec", "sha256": sha256(source_spec), "size_bytes": source_spec.stat().st_size},
            ],
        },
    )
    write_json(
        run_root / "01_plan" / "workflow.plan.json",
        {
            "stages": ["03-methodology-review", "04-multi-omics", "05-analysis-qa", "06-interpretation", "native-visual-review"],
            "automatic_method_fallback": False,
            "factor_matching": "Hungarian maximum absolute Spearman",
        },
    )
    env_dir = Path(spec.path).resolve()
    for name in ("requirements.lock.txt", "environment.locked.json"):
        source = env_dir / name
        if not source.is_file():
            raise FileNotFoundError(source)
        shutil.copy2(source, run_root / "02_environment" / name)
    environment_manifest = {
        "env_id": spec.env_id, "runtime": "python", "backend": spec.backend,
        "lock_hash": spec.lock_hash, "platform": marker.get("platform"),
        "backend_lock_sha256": sha256(run_root / "02_environment" / "requirements.lock.txt"),
        "global_changes": False,
    }
    write_json(run_root / "02_environment" / "environment_manifest.json", environment_manifest)
    for name in ("run_pipeline.py", "render_optimized_figures.py", "params.yaml"):
        shutil.copy2(ASSET_ROOT / name, run_root / "03_scripts" / name)
    now = utc_now()
    write_json(
        run_root / "manifest" / "run_manifest.json",
        {
            "run_id": run_root.name, "case_id": "p0-multiomics-cll-mofa", "mode": "run",
            "created_at": now, "updated_at": now, "current_state": "INTAKE", "terminal": False,
            "checkpoints": [], "environment": environment_manifest,
        },
    )
    (run_root / "manifest" / "artifact_ledger.jsonl").write_text("", encoding="utf-8")


def register_machine_checkpoints(run_root: Path) -> None:
    stage_ids = ("03-methodology-review", "04-multi-omics", "05-analysis-qa", "06-interpretation")
    ledger: list[dict[str, object]] = []
    checkpoints: list[dict[str, object]] = []
    for stage_id in stage_ids:
        stage = run_root / "04_intermediate" / stage_id
        complete = stage / "stage.complete.json"
        if not complete.is_file():
            raise RuntimeError(f"Missing stage completion contract: {stage_id}")
        files = sorted(path for path in stage.rglob("*") if path.is_file())
        checkpoints.append({"stage_id": stage_id, "validated": True, "artifact_count": len(files)})
        for path in files:
            ledger.append(
                {
                    "stage_id": stage_id,
                    "path": path.relative_to(run_root).as_posix(),
                    "sha256": sha256(path),
                    "size_bytes": path.stat().st_size,
                    "maturity": "data-verified",
                }
            )
    for layer in ("original", "final-revision-2"):
        for path in sorted((run_root / "06_figures" / layer).glob("*.png")):
            ledger.append(
                {
                    "stage_id": "candidate-visualization",
                    "path": path.relative_to(run_root).as_posix(),
                    "sha256": sha256(path),
                    "size_bytes": path.stat().st_size,
                    "maturity": "data-verified-pending-native-review",
                }
            )
    ledger_path = run_root / "manifest" / "artifact_ledger.jsonl"
    ledger_path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in ledger), encoding="utf-8")
    manifest_path = run_root / "manifest" / "run_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update(
        {
            "updated_at": utc_now(), "current_state": "NATIVE_VISUAL_REVIEW", "terminal": False,
            "checkpoints": checkpoints, "registered_artifacts": len(ledger),
        }
    )
    write_json(manifest_path, manifest)


def execute_or_report(manager, handle, authorization, script: Path, script_args: tuple[str, ...], task_root: Path, timeout: int, log_prefix: Path) -> bool:
    result = manager.execute(handle, script, authorization, args=script_args, cwd=task_root, timeout=timeout)
    log_prefix.parent.mkdir(parents=True, exist_ok=True)
    log_prefix.with_suffix(".stdout.log").write_text(result.stdout, encoding="utf-8")
    log_prefix.with_suffix(".stderr.log").write_text(result.stderr, encoding="utf-8")
    if result.returncode == 0:
        return True
    failure = manager.report_failure(
        handle.spec, stage="analysis-execute", command_record={"command": ["python", "<task-local-script>"], "returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}, attempts=1,
    )
    write_json(log_prefix.with_suffix(".failure.json"), failure)
    print(json.dumps(failure, ensure_ascii=False, indent=2))
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--pretrained-model", type=Path, required=True)
    parser.add_argument("--source-spec", type=Path, required=True)
    parser.add_argument("--environment-plan", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=7200)
    args = parser.parse_args()

    data, model, source_spec = (args.data.resolve(), args.pretrained_model.resolve(), args.source_spec.resolve())
    for path in (data, model, source_spec):
        if not path.is_file():
            raise FileNotFoundError(path)
    module = load_manager_module()
    plan, spec = parse_environment(module, args.environment_plan.resolve())
    task_root = Path(plan["task_root"]).resolve()
    run_root = args.run_root.resolve()
    if not run_root.is_relative_to(task_root):
        raise RuntimeError("Run root must remain inside the managed task root")
    marker = json.loads((Path(spec.path).resolve() / "environment.locked.json").read_text(encoding="utf-8"))
    if marker.get("lock_hash") != spec.lock_hash:
        raise RuntimeError("Environment marker lock hash mismatch")
    manager = module.EnvironmentManager(task_root, cache_root=Path(plan["cache_root"]), max_attempts=1)
    handle = module.EnvironmentHandle(spec=spec, state="frozen", verified=True, frozen=True)
    manager.verify(handle)
    initialize_run(run_root, data, model, source_spec, spec, marker)
    authorization = module.ExecutionAuthorization(mode="run", approved=True)
    if not execute_or_report(
        manager, handle, authorization, run_root / "03_scripts" / "run_pipeline.py",
        ("--run-root", str(run_root), "--data", str(data), "--pretrained-model", str(model), "--source-spec", str(source_spec)),
        task_root, args.timeout_seconds, run_root / "logs" / "analysis",
    ):
        return 2
    if not execute_or_report(
        manager, handle, authorization, run_root / "03_scripts" / "render_optimized_figures.py",
        ("--run-root", str(run_root)), task_root, 900, run_root / "logs" / "render",
    ):
        return 3
    register_machine_checkpoints(run_root)
    print(json.dumps({"ok": True, "run_root": str(run_root), "state": "NATIVE_VISUAL_REVIEW"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

