#!/usr/bin/env python3
"""Provision, freeze, run and cache-restore the KinneyH visualization teaching case."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Sequence


SKILL_ROOT = Path(__file__).resolve().parents[1]
MANAGER_PATH = SKILL_ROOT / "scripts" / "environment_manager.py"
PIPELINE_TEMPLATE = SKILL_ROOT / "assets" / "teaching-cases" / "p0-visualization-kinneyh" / "run_pipeline.py"
PINNED_DEPENDENCIES = (
    {"name": "pandas", "version": "2.3.3", "source": "pypi", "runtime": "python"},
    {"name": "matplotlib", "version": "3.10.8", "source": "pypi", "runtime": "python"},
    {"name": "seaborn", "version": "0.13.2", "source": "pypi", "runtime": "python"},
)


def load_manager():
    spec = importlib.util.spec_from_file_location("environment_manager_p0_visualization", MANAGER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def artifact_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): sha256_file(path)
        for path in sorted(item for item in root.rglob("*") if item.is_file())
    }


def run_fixture(task_root: Path, input_path: Path, expected_sha256: str) -> dict[str, Any]:
    module = load_manager()
    task_root = task_root.resolve()
    input_path = input_path.resolve()
    if task_root.exists() and any(task_root.iterdir()):
        raise RuntimeError(f"Refusing to reuse a non-empty task root: {task_root}")
    task_root.mkdir(parents=True, exist_ok=True)
    if sha256_file(input_path) != expected_sha256:
        raise RuntimeError("Input hash does not match registered teaching-case evidence")
    pipeline = task_root / "03_scripts" / "run_pipeline.py"
    pipeline.parent.mkdir(parents=True)
    shutil.copy2(PIPELINE_TEMPLATE, pipeline)
    authorization = module.ExecutionAuthorization(
        mode="run",
        approved=True,
        allow_network_install=True,
        allowed_sources=("pypi",),
    )
    manager = module.EnvironmentManager(task_root)
    probe = manager.probe()
    recipe = {"runtimes": ["python"], "backend": "python-uv", "dependencies": list(PINNED_DEPENDENCIES)}
    plan = manager.resolve(
        {"mode": "run", "execution_authorized": True, "authorization_scope": "task-local"},
        recipe,
        probe,
    )
    handles = manager.provision(plan, authorization)
    handle = handles[0]
    manager.verify(handle)
    marker = manager.freeze(handle, authorization)
    first_output = task_root / "run1"
    args = (
        "--input", str(input_path),
        "--output-dir", str(first_output),
        "--expected-sha256", expected_sha256,
    )
    first = manager.execute(handle, pipeline, authorization, args=args, cwd=task_root, timeout=3_600)
    if first.returncode != 0:
        raise RuntimeError(f"Teaching pipeline failed: {first.stderr}")
    first_hashes = artifact_hashes(first_output)

    restored_manager = module.EnvironmentManager(task_root)
    restored_plan = restored_manager.resolve(
        {"mode": "run", "execution_authorized": True, "authorization_scope": "task-local"},
        recipe,
        restored_manager.probe(),
    )
    restored_handle = restored_manager.provision(restored_plan, authorization)[0]
    second_output = task_root / "run2"
    second_args = list(args)
    second_args[3] = str(second_output)
    second = restored_manager.execute(restored_handle, pipeline, authorization, args=tuple(second_args), cwd=task_root, timeout=3_600)
    if second.returncode != 0:
        raise RuntimeError(f"Restored teaching pipeline failed: {second.stderr}")
    second_hashes = artifact_hashes(second_output)
    non_index_first = {key: value for key, value in first_hashes.items() if key != "reports/artifact-index.json"}
    non_index_second = {key: value for key, value in second_hashes.items() if key != "reports/artifact-index.json"}
    if non_index_first != non_index_second:
        differences = sorted(set(non_index_first) ^ set(non_index_second) | {key for key in set(non_index_first) & set(non_index_second) if non_index_first[key] != non_index_second[key]})
        raise RuntimeError(f"Fresh/restored artifact hashes differ: {differences}")
    environment_dir = task_root / "02_environment"
    environment_dir.mkdir()
    env_path = Path(handle.spec.path)
    shutil.copy2(env_path / "environment.locked.json", environment_dir / "environment.locked.json")
    shutil.copy2(env_path / "requirements.lock.txt", environment_dir / "requirements.lock.txt")
    install_log = {
        "provision_commands": handle.command_log,
        "restore_commands": restored_handle.command_log,
        "global_changes": False,
    }
    atomic_json(environment_dir / "install-log.json", install_log)
    manifest = {
        "schema_version": "1.0",
        "state": "frozen",
        "plan_id": plan.plan_id,
        "restored_plan_id": restored_plan.plan_id,
        "plan_ids_equal": plan.plan_id == restored_plan.plan_id,
        "environment": {
            "env_id": handle.spec.env_id,
            "backend": handle.spec.backend,
            "lock_hash": handle.spec.lock_hash,
            "marker": marker,
            "requirements_lock_sha256": sha256_file(environment_dir / "requirements.lock.txt"),
            "marker_sha256": sha256_file(environment_dir / "environment.locked.json"),
        },
        "authorization": {"mode": "run", "scope": "task-local", "network_install": True, "allowed_sources": ["pypi"], "global_changes": False},
    }
    atomic_json(environment_dir / "environment-manifest.json", manifest)
    return {
        "ok": True,
        "schema_version": "1.0",
        "task_root": str(task_root),
        "input_sha256": expected_sha256,
        "environment_manifest": manifest,
        "fresh_returncode": first.returncode,
        "restored_returncode": second.returncode,
        "restored_from_cache": restored_handle.frozen and restored_handle.verified,
        "deterministic_artifacts": non_index_first,
        "artifact_count": len(first_hashes),
        "scientific_boundary": "This real teaching run validates sample-level descriptive composition and rendering only; it performs no group inference and establishes no disease or clinical effect.",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-root", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--report", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = run_fixture(args.task_root, args.input, args.expected_sha256)
    except Exception as exc:
        report = {"ok": False, "error_type": type(exc).__name__, "error": str(exc)}
    atomic_json(args.report.resolve(), report)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
