#!/usr/bin/env python3
"""Exercise a real task-local environment lifecycle without network dependencies.

The fixture provisions a Python ``uv`` environment, verifies and freezes it, executes
the same deterministic script twice (fresh handle and cache-restored handle), and
compares output hashes. It never installs into a global/base environment.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence


SKILL_ROOT = Path(__file__).resolve().parents[1]
ENVIRONMENT_MANAGER = SKILL_ROOT / "scripts" / "environment_manager.py"
FIXTURE_SOURCE = """#!/usr/bin/env python3
import hashlib
import json
import platform
import sys
from pathlib import Path

output = Path(sys.argv[1])
payload = {
    "fixture": "biomedical-analysis-agent-environment-v1",
    "input": [1, 2, 3, 5, 8],
    "sum": 19,
    "python_major_minor": list(sys.version_info[:2]),
    "implementation": platform.python_implementation(),
}
rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\\n"
output.write_text(rendered, encoding="utf-8")
print(hashlib.sha256(rendered.encode("utf-8")).hexdigest())
"""


def load_environment_manager():
    spec = importlib.util.spec_from_file_location("environment_manager_fixture", ENVIRONMENT_MANAGER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def ensure_fixture_script(path: Path) -> None:
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        if existing != FIXTURE_SOURCE:
            raise RuntimeError(f"Refusing to overwrite changed fixture script: {path}")
        return
    atomic_write(path, FIXTURE_SOURCE)


def run_fixture(task_root: Path) -> dict[str, Any]:
    module = load_environment_manager()
    task_root = task_root.resolve()
    task_root.mkdir(parents=True, exist_ok=True)
    fixture_script = task_root / "fixture" / "deterministic_fixture.py"
    ensure_fixture_script(fixture_script)
    authorization = module.ExecutionAuthorization(
        mode="run",
        approved=True,
        allow_network_install=False,
        allowed_sources=(),
    )
    manager = module.EnvironmentManager(task_root)
    probe = manager.probe()
    plan = manager.resolve(
        {"mode": "run", "execution_authorized": True, "authorization_scope": "task-local"},
        {"runtimes": ["python"], "dependencies": [], "backend": "python-uv"},
        probe,
    )
    handles = manager.provision(plan, authorization)
    if len(handles) != 1:
        raise RuntimeError(f"Expected one environment, got {len(handles)}")
    first = handles[0]
    manager.verify(first)
    marker = manager.freeze(first, authorization)
    first_output = task_root / "fixture" / "fresh-output.json"
    fresh = manager.execute(first, fixture_script, authorization, args=(str(first_output),), cwd=task_root)
    if fresh.returncode != 0:
        raise RuntimeError(f"Fresh execution failed: {fresh.stderr}")

    # A new manager instance proves restoration from the frozen lock marker rather than
    # reuse of mutable in-memory state.
    restored_manager = module.EnvironmentManager(task_root)
    restored_plan = restored_manager.resolve(
        {"mode": "run", "execution_authorized": True, "authorization_scope": "task-local"},
        {"runtimes": ["python"], "dependencies": [], "backend": "python-uv"},
        restored_manager.probe(),
    )
    restored_handles = restored_manager.provision(restored_plan, authorization)
    restored = restored_handles[0]
    restored_output = task_root / "fixture" / "restored-output.json"
    second = restored_manager.execute(restored, fixture_script, authorization, args=(str(restored_output),), cwd=task_root)
    if second.returncode != 0:
        raise RuntimeError(f"Restored execution failed: {second.stderr}")

    fresh_sha = sha256_file(first_output)
    restored_sha = sha256_file(restored_output)
    lock_dir = Path(first.spec.path)
    report = {
        "ok": fresh_sha == restored_sha,
        "schema_version": "1.0",
        "authorization": {
            "mode": "run",
            "scope": "task-local",
            "network_install": False,
            "global_changes": False,
        },
        "probe": probe,
        "plan_id": plan.plan_id,
        "restored_plan_id": restored_plan.plan_id,
        "plan_ids_equal": plan.plan_id == restored_plan.plan_id,
        "environment": {
            "env_id": first.spec.env_id,
            "backend": first.spec.backend,
            "lock_hash": first.spec.lock_hash,
            "marker": marker,
            "marker_sha256": sha256_file(lock_dir / "environment.locked.json"),
            "requirements_lock_sha256": sha256_file(lock_dir / "requirements.lock.txt"),
            "fresh_state": first.state,
            "restored_state": restored.state,
            "restored_from_cache": restored.frozen and restored.verified,
        },
        "execution": {
            "fresh_returncode": fresh.returncode,
            "restored_returncode": second.returncode,
            "fresh_output_sha256": fresh_sha,
            "restored_output_sha256": restored_sha,
            "outputs_equal": fresh_sha == restored_sha,
        },
        "scientific_boundary": "This fixture verifies environment lifecycle and deterministic execution only; it does not validate a biomedical method or third-party dependency installation.",
    }
    if not report["ok"]:
        raise RuntimeError("Restored output hash differs from fresh output")
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = run_fixture(args.task_root)
    except Exception as exc:  # structured failure for a user-visible exercise
        report = {"ok": False, "error_type": type(exc).__name__, "error": str(exc)}
    atomic_write(args.report.resolve(), json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
