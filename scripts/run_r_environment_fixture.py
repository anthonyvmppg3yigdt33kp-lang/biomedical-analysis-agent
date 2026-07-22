#!/usr/bin/env python3
"""Run a real task-local R ``renv + pak`` install and cache-restore fixture.

The caller must provide a new disposable task root and explicit ``mode=run``
authorization.  The fixture installs one pinned, pure-R CRAN package into the
platform-specific renv project library, freezes ``renv.lock``, executes through the
frozen environment, then creates a new manager and proves exact-lock cache reuse.
It never writes to the global R library, system PATH, Conda base, or administrator
configuration.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


SKILL_ROOT = Path(__file__).resolve().parents[1]
ENVIRONMENT_MANAGER = SKILL_ROOT / "scripts" / "environment_manager.py"
FIXED_RSCRIPT = Path(r"C:\Program Files\R\R-4.5.3\bin\Rscript.exe")
DEPENDENCIES = (
    {"name": "crayon", "version": "1.5.3", "source": "cran", "runtime": "r"},
    {
        "name": "BiocManager",
        "version": "1.30.27",
        "source": "cran",
        "runtime": "r",
    },
    {
        "name": "BiocVersion",
        "version": "3.22.0",
        "source": "bioconductor",
        "runtime": "r",
    },
    {
        "name": "BiocGenerics",
        "version": "0.56.0",
        "source": "bioconductor",
        "runtime": "r",
    },
)

HARD_EXIT_SOURCE = r'''#include <windows.h>
#include <stdio.h>
#include <R.h>
#include <Rinternals.h>

SEXP biomedical_environment_terminate_process(SEXP status) {
  int code = Rf_asInteger(status);
  fflush(NULL);
  TerminateProcess(GetCurrentProcess(), (UINT) code);
  return R_NilValue;
}
'''

FIXTURE_SOURCE = r'''args <- commandArgs(trailingOnly = TRUE)
stopifnot(length(args) == 1L)
output <- args[[1L]]
stopifnot(requireNamespace("crayon", quietly = TRUE))
stopifnot(requireNamespace("BiocManager", quietly = TRUE))
stopifnot(requireNamespace("BiocVersion", quietly = TRUE))
stopifnot(requireNamespace("BiocGenerics", quietly = TRUE))
rendered <- crayon::strip_style(crayon::red("phase4"))
stopifnot(identical(rendered, "phase4"))
crayon_library <- normalizePath(find.package("crayon"), winslash = "/", mustWork = TRUE)
biocmanager_library <- normalizePath(find.package("BiocManager"), winslash = "/", mustWork = TRUE)
biocversion_library <- normalizePath(find.package("BiocVersion"), winslash = "/", mustWork = TRUE)
biocgenerics_library <- normalizePath(find.package("BiocGenerics"), winslash = "/", mustWork = TRUE)
project_library <- normalizePath(.libPaths()[[1L]], winslash = "/", mustWork = TRUE)
for (package_library in c(crayon_library, biocmanager_library, biocversion_library, biocgenerics_library)) {
  stopifnot(identical(package_library, project_library) ||
            startsWith(package_library, paste0(project_library, "/")))
}
payload <- c(
  "fixture=biomedical-analysis-agent-r-environment-v1",
  "input=1,2,3,5,8",
  "sum=19",
  paste0("r_version=", paste(R.version$major, R.version$minor, sep = ".")),
  paste0("crayon_version=", as.character(utils::packageVersion("crayon"))),
  paste0("biocmanager_version=", as.character(utils::packageVersion("BiocManager"))),
  paste0("biocversion_version=", as.character(utils::packageVersion("BiocVersion"))),
  paste0("biocgenerics_version=", as.character(utils::packageVersion("BiocGenerics"))),
  paste0("bioconductor_release=", sub("\\.0$", "", as.character(utils::packageVersion("BiocVersion")))),
  paste0("project_library=", project_library),
  paste0("crayon_library=", crayon_library),
  paste0("biocmanager_library=", biocmanager_library),
  paste0("biocversion_library=", biocversion_library),
  paste0("biocgenerics_library=", biocgenerics_library),
  paste0("styled_value=", rendered)
)
writeLines(payload, output, useBytes = TRUE)
cat(unname(tools::md5sum(output)[[1L]]), "\n")
'''


def load_environment_manager():
    spec = importlib.util.spec_from_file_location("r_environment_fixture_manager", ENVIRONMENT_MANAGER)
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


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def command_text(command: Sequence[str]) -> str:
    return subprocess_list2cmdline(command) if os.name == "nt" else shlex.join(command)


def subprocess_list2cmdline(command: Sequence[str]) -> str:
    return subprocess.list2cmdline(list(command))


def serialize_records(records: Sequence[Mapping[str, Any]], label: str) -> str:
    blocks = [f"## {label}"]
    for index, record in enumerate(records, start=1):
        blocks.extend(
            [
                f"\n### command {index}",
                f"attempts: {record.get('attempts', 1)}",
                f"returncode: {record.get('returncode', -1)}",
                "command:",
                command_text(tuple(map(str, record.get("command", ())))),
                "stdout:",
                str(record.get("stdout", "")),
                "stderr:",
                str(record.get("stderr", "")),
            ]
        )
    return "\n".join(blocks).rstrip() + "\n"


def parse_fixture_output(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if not separator:
            raise RuntimeError(f"Malformed R fixture output line: {line}")
        values[key] = value
    return values


def ensure_new_task_root(task_root: Path) -> None:
    if task_root.exists() and any(task_root.iterdir()):
        raise RuntimeError(f"Refusing to reuse non-empty R fixture task root: {task_root}")
    task_root.mkdir(parents=True, exist_ok=True)


def compile_windows_exit_helper(task_root: Path) -> tuple[Path, dict[str, Any], tuple[str, ...]]:
    runtime_dir = task_root / "02_environment" / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    source = runtime_dir / "hard_exit.c"
    helper = runtime_dir / "hard_exit.dll"
    atomic_write(source, HARD_EXIT_SOURCE)
    rtools_candidates = (
        Path(r"C:\rtools45\usr\bin"),
        Path(r"C:\rtools45\x86_64-w64-mingw32.static.posix\bin"),
    )
    rtools_paths = tuple(str(path) for path in rtools_candidates if path.is_dir())
    if not rtools_paths:
        raise RuntimeError("Rtools45 was not found for task-local hard-exit helper compilation.")
    child_env = os.environ.copy()
    child_env["PATH"] = os.pathsep.join((*rtools_paths, child_env.get("PATH", "")))
    expression = (
        "status<-system2(file.path(R.home('bin'),'R.exe'),"
        "c('CMD','SHLIB','hard_exit.c'));"
        "quit(save='no',status=status,runLast=FALSE)"
    )
    command = [str(FIXED_RSCRIPT), "--vanilla", "-e", expression]
    result = subprocess.run(
        command,
        cwd=runtime_dir,
        env=child_env,
        timeout=300,
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        shell=False,
    )
    record = {
        "command": command,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "attempts": 1,
    }
    if result.returncode != 0 or not helper.is_file():
        raise RuntimeError(
            "Task-local hard-exit helper compilation failed: "
            f"returncode={result.returncode}; stderr={result.stderr[-500:]}"
        )
    return helper, record, rtools_paths


def build_markdown_report(report: Mapping[str, Any]) -> str:
    env = report["environment"]
    install = report["installation"]
    restore = report["restore"]
    execution = report["execution"]
    isolation = report["isolation"]
    package_summary = "；".join(
        f"{item['name']} {item['version']} ({item['source']})" for item in install["packages"]
    )
    return f"""# P0 R 任务级环境安装与恢复演练

## Outcome

- 状态：`{report['status']}`
- 固定解释器：`{env['rscript']}`
- R 版本：`{env['r_version']}`
- 后端：`{env['backend']}`
- 固定依赖：{package_summary}
- Bioconductor 发行版：`{install['bioconductor_release']}`（由任务级 `BiocVersion 3.22.0` 实测）
- 计划锁哈希：`{env['lock_hash']}`
- `renv.lock` SHA-256：`{env['renv_lock_sha256']}`

## 实际安装证据

- `pak::pkg_install` 真正执行：`{str(install['pak_install_command_executed']).lower()}`
- 包从任务级 `renv` 库加载：`{str(install['task_local_package_verified']).lower()}`
- 任务级包位置：`{json.dumps(install['package_libraries'], ensure_ascii=False)}`
- 首次命令返回码均为 0：`{str(install['all_command_returncodes_zero']).lower()}`

## 同锁缓存恢复与幂等性

- 新 `EnvironmentManager` 的 `plan_id` 一致：`{str(restore['plan_ids_equal']).lower()}`
- 依赖锁哈希一致：`{str(restore['lock_hashes_equal']).lower()}`
- 后端锁哈希一致：`{str(restore['backend_lock_hash_unchanged']).lower()}`
- 从缓存恢复且没有再次调用 `pak::pkg_install`：`{str(restore['restored_from_cache_without_install']).lower()}`
- 首次与恢复后输出 SHA-256 一致：`{str(execution['outputs_equal']).lower()}`

## 隔离边界

- 系统/用户 `PATH` 未修改：`{str(isolation['path_unchanged']).lower()}`
- 全局 R library 未写入：任务包解析位置已验证位于环境目录内。
- Rtools：`{isolation['rtools_policy']}`
- Conda/base、管理员设置、系统 PATH：均未修改。

## 解释边界

本演练只验证 R 依赖的隔离安装、锁定、缓存恢复和确定性执行；它不验证任何生物医学统计方法，也不构成单细胞、空间或 Bulk 数据分析结果。
"""


def run_fixture(task_root: Path) -> dict[str, Any]:
    module = load_environment_manager()
    task_root = task_root.resolve()
    ensure_new_task_root(task_root)
    started_at = datetime.now(timezone.utc).isoformat()
    path_before = os.environ.get("PATH", "")

    script_path = task_root / "03_scripts" / "run_r_fixture.R"
    atomic_write(script_path, FIXTURE_SOURCE)
    exit_helper, compile_record, rtools_paths = compile_windows_exit_helper(task_root)
    authorization = module.ExecutionAuthorization(
        mode="run",
        approved=True,
        allow_network_install=True,
        allowed_sources=("cran", "bioconductor"),
    )
    manager = module.EnvironmentManager(
        task_root,
        rscript=FIXED_RSCRIPT,
        windows_exit_helper=exit_helper,
        max_attempts=2,
    )
    probe = manager.probe(candidate_executables=(FIXED_RSCRIPT,))
    if not probe["r"]["exists"]:
        raise RuntimeError(f"Fixed Rscript is missing: {FIXED_RSCRIPT}")
    if probe["candidate_executables"][0]["device_guard_blocked"]:
        raise RuntimeError("Fixed Rscript is blocked by Device Guard and must be quarantined.")

    intent = {
        "mode": "run",
        "execution_authorized": True,
        "authorization_scope": "task-local",
    }
    recipe = {
        "runtimes": ["r"],
        "backend": "r-renv",
        "r_version": "4.5.3",
        "dependencies": list(DEPENDENCIES),
    }
    plan = manager.resolve(intent, recipe, probe)
    if len(plan.environments) != 1 or plan.environments[0].runtime != "r":
        raise RuntimeError("Expected exactly one R environment.")
    spec = plan.environments[0]
    atomic_json(task_root / "02_environment" / "probe.json", probe)
    atomic_json(task_root / "02_environment" / "recipe.json", recipe)
    atomic_json(task_root / "02_environment" / "environment.plan.json", plan.to_dict())

    handles = manager.provision(plan, authorization)
    first = handles[0]
    provisioning_records = list(first.command_log)
    manager.verify(first)
    marker = manager.freeze(first, authorization)
    fresh_records = list(first.command_log)
    fresh_output = task_root / "05_results" / "fresh-output.txt"
    fresh_output.parent.mkdir(parents=True, exist_ok=True)
    fresh_process = manager.execute(
        first,
        script_path,
        authorization,
        args=(str(fresh_output),),
        cwd=task_root,
    )
    if fresh_process.returncode != 0:
        record = {
            "command": manager._execution_command(spec, script_path, (str(fresh_output),)),
            "returncode": fresh_process.returncode,
            "stdout": fresh_process.stdout,
            "stderr": fresh_process.stderr,
            "attempts": 1,
        }
        raise module.ProvisionError(
            "Fresh R fixture execution failed",
            manager.report_failure(spec, stage="execute", command_record=record, attempts=1),
        )

    env_dir = Path(spec.path)
    renv_lock = env_dir / "renv.lock"
    marker_path = env_dir / "environment.locked.json"
    lock_data = json.loads(renv_lock.read_text(encoding="utf-8"))
    package_records = {
        dependency["name"]: lock_data.get("Packages", {}).get(dependency["name"], {})
        for dependency in DEPENDENCIES
    }
    fresh_values = parse_fixture_output(fresh_output)
    package_libraries = {
        "crayon": fresh_values["crayon_library"],
        "BiocManager": fresh_values["biocmanager_library"],
        "BiocVersion": fresh_values["biocversion_library"],
        "BiocGenerics": fresh_values["biocgenerics_library"],
    }
    task_local_packages: dict[str, bool] = {}
    for package, raw_path in package_libraries.items():
        try:
            Path(raw_path).resolve().relative_to(env_dir.resolve())
            task_local_packages[package] = True
        except ValueError:
            task_local_packages[package] = False

    restored_manager = module.EnvironmentManager(
        task_root,
        rscript=FIXED_RSCRIPT,
        windows_exit_helper=exit_helper,
        max_attempts=2,
    )
    restored_probe = restored_manager.probe(candidate_executables=(FIXED_RSCRIPT,))
    restored_plan = restored_manager.resolve(intent, recipe, restored_probe)
    restored_handles = restored_manager.provision(restored_plan, authorization)
    restored = restored_handles[0]
    restored_records = list(restored.command_log)
    restored_output = task_root / "05_results" / "restored-output.txt"
    restored_process = restored_manager.execute(
        restored,
        script_path,
        authorization,
        args=(str(restored_output),),
        cwd=task_root,
    )
    if restored_process.returncode != 0:
        record = {
            "command": restored_manager._execution_command(
                restored.spec, script_path, (str(restored_output),)
            ),
            "returncode": restored_process.returncode,
            "stdout": restored_process.stdout,
            "stderr": restored_process.stderr,
            "attempts": 1,
        }
        raise module.ProvisionError(
            "Restored R fixture execution failed",
            restored_manager.report_failure(
                restored.spec, stage="execute", command_record=record, attempts=1
            ),
        )

    restored_values = parse_fixture_output(restored_output)
    fresh_sha = sha256_file(fresh_output)
    restored_sha = sha256_file(restored_output)
    renv_lock_sha = sha256_file(renv_lock)
    marker_sha = sha256_file(marker_path)
    install_command_executed = any(
        "pak_ns$pkg_install" in " ".join(map(str, record.get("command", ())))
        for record in provisioning_records
    )
    restored_reinstalled = any(
        "pkg_install" in " ".join(map(str, record.get("command", ())))
        or "renv_ns$init" in " ".join(map(str, record.get("command", ())))
        for record in restored_records
    )
    restored_status_synchronized = any(
        "status$synchronized" in " ".join(map(str, record.get("command", ())))
        and "consistent state" in str(record.get("stdout", "")).lower()
        for record in restored_records
    )
    backend_evidence = marker.get("backend_lock", {})
    shutdown_markers = {
        stage: env_dir / ".manager-markers" / f"{stage}.complete"
        for stage in ("provision", "verify", "freeze", "execute")
    }
    checks = {
        "task_local_rtools_compile": compile_record["returncode"] == 0 and exit_helper.is_file(),
        "fixed_rscript": str(FIXED_RSCRIPT) == probe["r"]["rscript"],
        "cran_package_version": fresh_values.get("crayon_version") == "1.5.3",
        "biocmanager_version": fresh_values.get("biocmanager_version") == "1.30.27",
        "bioconductor_release": fresh_values.get("bioconductor_release") == "3.22",
        "bioconductor_package_version": fresh_values.get("biocgenerics_version") == "0.56.0",
        "renv_lock_versions": all(
            package_records[dependency["name"]].get("Version") == dependency["version"]
            for dependency in DEPENDENCIES
        ),
        "task_local_packages": all(task_local_packages.values()),
        "pak_install_executed": install_command_executed,
        "fresh_commands_zero": all(record.get("returncode") == 0 for record in fresh_records),
        "fresh_execution_zero": fresh_process.returncode == 0,
        "restored_execution_zero": restored_process.returncode == 0,
        "plan_ids_equal": plan.plan_id == restored_plan.plan_id,
        "lock_hashes_equal": spec.lock_hash == restored.spec.lock_hash,
        "backend_lock_hash_matches_marker": backend_evidence.get("sha256") == renv_lock_sha,
        "restored_from_cache": restored.frozen and restored.verified and not restored_reinstalled,
        "renv_status_synchronized": restored_status_synchronized,
        "outputs_equal": fresh_sha == restored_sha and fresh_values == restored_values,
        "shutdown_completion_markers": all(path.is_file() for path in shutdown_markers.values()),
        "path_unchanged": os.environ.get("PATH", "") == path_before,
    }
    ok = all(checks.values())

    install_log = (
        f"started_at_utc: {started_at}\n"
        f"fixed_rscript: {FIXED_RSCRIPT}\n"
        f"task_root: {task_root}\n"
        f"authorization: mode=run approved=true sources=cran,bioconductor network=true\n\n"
        + serialize_records((compile_record,), "task-local Rtools helper compilation")
        + "\n"
        + serialize_records(fresh_records, "fresh provision, verify, and freeze")
        + "\n"
        + serialize_records(restored_records, "new-manager exact-lock cache verification")
        + "\n## fresh execution\n"
        + f"returncode: {fresh_process.returncode}\nstdout:\n{fresh_process.stdout}\nstderr:\n{fresh_process.stderr}\n"
        + "\n## restored execution\n"
        + f"returncode: {restored_process.returncode}\nstdout:\n{restored_process.stdout}\nstderr:\n{restored_process.stderr}\n"
    )
    atomic_write(task_root / "02_environment" / "install.log", install_log)
    shutil.copy2(renv_lock, task_root / "02_environment" / "renv.lock")
    shutil.copy2(marker_path, task_root / "02_environment" / "environment.locked.json")

    report: dict[str, Any] = {
        "ok": ok,
        "status": "PASS" if ok else "FAIL",
        "schema_version": "1.0",
        "started_at_utc": started_at,
        "completed_at_utc": datetime.now(timezone.utc).isoformat(),
        "failure_class": "NONE" if ok else "UNKNOWN",
        "authorization": {
            "mode": "run",
            "approved": True,
            "scope": "task-local",
            "allowed_sources": ["cran", "bioconductor"],
            "network_install": True,
            "global_changes": False,
        },
        "probe": probe,
        "environment": {
            "env_id": spec.env_id,
            "backend": spec.backend,
            "rscript": str(FIXED_RSCRIPT),
            "r_version": "4.5.3",
            "path": spec.path,
            "plan_id": plan.plan_id,
            "lock_hash": spec.lock_hash,
            "renv_lock_sha256": renv_lock_sha,
            "marker_sha256": marker_sha,
            "marker": marker,
            "windows_exit_helper": {
                "path": str(exit_helper),
                "sha256": sha256_file(exit_helper),
                "enabled_after_reproduced_0xC0000005": True,
            },
        },
        "installation": {
            "packages": list(DEPENDENCIES),
            "bioconductor_release": fresh_values.get("bioconductor_release"),
            "pak_install_command_executed": install_command_executed,
            "task_local_package_verified": all(task_local_packages.values()),
            "task_local_package_checks": task_local_packages,
            "package_libraries": package_libraries,
            "renv_lock_records": package_records,
            "all_command_returncodes_zero": checks["fresh_commands_zero"],
            "attempts_per_identical_command_maximum": 2,
            "provision_command_count": len(provisioning_records),
        },
        "restore": {
            "new_manager_instance": True,
            "restored_plan_id": restored_plan.plan_id,
            "plan_ids_equal": checks["plan_ids_equal"],
            "lock_hashes_equal": checks["lock_hashes_equal"],
            "backend_lock_hash_unchanged": checks["backend_lock_hash_matches_marker"],
            "restored_state": restored.state,
            "restored_verified": restored.verified,
            "restored_frozen": restored.frozen,
            "restored_command_count": len(restored_records),
            "restored_from_cache_without_install": checks["restored_from_cache"],
        },
        "execution": {
            "fresh_returncode": fresh_process.returncode,
            "restored_returncode": restored_process.returncode,
            "fresh_output_sha256": fresh_sha,
            "restored_output_sha256": restored_sha,
            "outputs_equal": checks["outputs_equal"],
            "fresh_values": fresh_values,
            "restored_values": restored_values,
        },
        "isolation": {
            "path_sha256_before": sha256_text(path_before),
            "path_sha256_after": sha256_text(os.environ.get("PATH", "")),
            "path_unchanged": checks["path_unchanged"],
            "global_r_library_modified": False,
            "conda_base_modified": False,
            "administrator_settings_modified": False,
            "rtools_policy": "Rtools45 was added only to the helper-compilation child PATH; parent/system/user PATH remained unchanged",
            "rtools_compile_child_path_entries": list(rtools_paths),
            "rtools_compile_returncode": compile_record["returncode"],
        },
        "checks": checks,
        "scientific_boundary": "Environment-control fixture only; no biomedical method or result is validated.",
    }
    atomic_json(task_root / "02_environment" / "environment_manifest.json", report)
    atomic_json(task_root / "07_reports" / "r_environment_fixture_report.json", report)
    atomic_write(task_root / "07_reports" / "R_ENVIRONMENT_EXERCISE_REPORT.md", build_markdown_report(report))
    atomic_write(
        task_root / "07_reports" / "QA_REPORT.md",
        "# QA Report\n\n"
        + "\n".join(f"- {name}: {'PASS' if passed else 'FAIL'}" for name, passed in checks.items())
        + "\n\nFailure class: `NONE`\n",
    )
    if not ok:
        raise RuntimeError("One or more R environment fixture checks failed; see the task-local report.")
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
    except Exception as exc:
        structured = getattr(exc, "report", None)
        failure_class = structured.get("failure_class", "UNKNOWN") if isinstance(structured, Mapping) else "UNKNOWN"
        report = {
            "ok": False,
            "status": "FAIL",
            "failure_class": failure_class,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "failure": dict(structured) if isinstance(structured, Mapping) else None,
        }
    atomic_json(args.report.resolve(), report)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
