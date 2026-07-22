#!/usr/bin/env python3
"""Stable case interface for the Seurat Visium mouse-brain tutorial."""

from __future__ import annotations

import argparse
import csv
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
from typing import Any


CASE_DIR = Path(__file__).resolve().parent
CASE_ID = "visium-mouse-brain"
EXPECTED_R = "4.5.3"
EXPECTED_SEURAT = "5.5.0"
EXPECTED_RENV = "1.2.2"
EXPECTED_BOOTSTRAP_RENV = "1.2.2"
EXPECTED_BOOTSTRAP_RENV_BINARY = "renv_1.2.2.zip"
EXPECTED_BOOTSTRAP_RENV_SIZE = 2514910
EXPECTED_BOOTSTRAP_RENV_SHA256 = "bcba2170563c65c6d6ed9328e4a624033ad9c5ee4e5bf9132cff7bcc7327cce5"
EXPECTED_BIOCMANAGER = "1.30.27"
EXPECTED_INPUT_MANIFEST_SHA256 = "6865d5781ae90b703c532f24ae1f967d1b01583eb609e860ee34136ba446259c"
EXPECTED_RENV_LOCK_SHA256 = "b8450521054cd750e93a27fab0b44757519361bd8b694ffbae35636de508cf5b"
LEGACY_ENVIRONMENT_CACHE_KEY = "5324b3ca515435690ada4e70"
PPM_SNAPSHOT = "https://packagemanager.posit.co/cran/2026-04-23"
PPM_BINARY_CONTRIB = f"{PPM_SNAPSHOT}/bin/windows/contrib/4.5"
CHUNK_SIZE = 4 * 1024 * 1024
EXPECTED_BIOC_RELEASE = "3.21"
EXPECTED_BIOC_VERSION = "3.21.1"
EXPECTED_GLMGAMPOI = "1.20.0"
EXPECTED_SPARSEARRAY = "1.8.1"
EXPECTED_BIOC_ANNOTATION_REPOSITORY = "https://bioconductor.org/packages/3.21/data/annotation"
EXPECTED_BIOC_ANNOTATION_CONTRIB = f"{EXPECTED_BIOC_ANNOTATION_REPOSITORY}/src/contrib"
EXPECTED_R_RECOMMENDED = (
    "boot", "class", "cluster", "codetools", "foreign", "KernSmooth", "MASS",
    "mgcv", "nlme", "nnet", "rpart", "spatial", "survival",
)
BIOC_PINS_RELATIVE = Path("config/bioconductor-3.21-archive-pins.json")
FORBIDDEN_R_PROCESS_PATTERNS = (
    "warning:",
    "warning message",
    "stack imbalance",
    "iteration limit reached",
    "alternation limit reached",
    "execution halted",
    "error in ",
    "caught access violation",
    "caught segfault",
    "fatal error",
)
EXPECTED_CHECKPOINT_KEYS = (
    "S10_INGEST",
    "S20_COORD_IMAGE_QC",
    "S30_UNIT_QC",
    "S40_PREPROCESS",
    "S60_CORE_DISCOVERY",
    "S80_ADVANCED/round-1",
    "S90_INFERENCE_QA",
    "S95_VISUALIZE_INTERPRET/round-1",
)


class CaseError(RuntimeError):
    """A contract violation that must produce a non-zero exit."""


def _native_windows_architecture() -> tuple[str, str]:
    """Return (OS architecture label, canonical child env value)."""
    if os.name != "nt":
        raise CaseError("Windows R subprocess architecture gate requires Windows")
    system_info = (ctypes.c_ubyte * 64)()
    ctypes.windll.kernel32.GetNativeSystemInfo(ctypes.byref(system_info))
    architecture_code = int.from_bytes(bytes(system_info[:2]), byteorder="little")
    native_label = {9: "X64", 12: "ARM64", 0: "X86"}.get(architecture_code)
    if native_label is None:
        raise CaseError(f"unsupported Windows native architecture code: {architecture_code}")
    canonical = {"X64": "AMD64", "ARM64": "ARM64", "X86": "x86"}[native_label]
    if canonical != "AMD64":
        raise CaseError(
            f"unsupported Windows native architecture: {native_label}; only AMD64 is validated"
        )
    return native_label, canonical


def _r_subprocess_environment(
    environment: dict[str, str] | None,
) -> tuple[dict[str, str], dict[str, Any]]:
    """Create a child-only Windows R environment with strict architecture evidence."""
    child = dict(os.environ if environment is None else environment)
    if os.name != "nt":
        return child, {
            "platform": os.name,
            "processor_architecture_restored": False,
            "parent_environment_modified": False,
        }
    native_label, expected = _native_windows_architecture()
    observed = child.get("PROCESSOR_ARCHITECTURE")
    aliases = {
        "AMD64": "AMD64",
        "X64": "AMD64",
        "X86_64": "AMD64",
        "ARM64": "ARM64",
        "AARCH64": "ARM64",
        "X86": "x86",
        "I386": "x86",
        "I686": "x86",
    }
    restored = observed is None or not observed.strip()
    if not restored:
        normalized = aliases.get(observed.strip().upper())
        if normalized is None or normalized != expected:
            raise CaseError(
                "PROCESSOR_ARCHITECTURE conflicts with the native Windows architecture"
            )
    wow64 = child.get("PROCESSOR_ARCHITEW6432")
    if wow64 and aliases.get(wow64.strip().upper()) != expected:
        raise CaseError("PROCESSOR_ARCHITEW6432 conflicts with the native Windows architecture")
    child["PROCESSOR_ARCHITECTURE"] = expected
    return child, {
        "platform": "windows",
        "native_architecture": native_label,
        "processor_architecture": expected,
        "processor_architecture_restored": restored,
        "supported_architecture": "AMD64",
        "parent_environment_modified": False,
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CaseError(f"cannot read JSON {path}: {exc}") from exc


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def write_text_atomic(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(value, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def _run_process_status(
    command: list[str],
    *,
    cwd: Path,
    log_path: Path,
    env: dict[str, str] | None = None,
) -> int:
    """Run a subprocess and fail closed on R stderr/stdout integrity signals.

    Every R subprocess receives a child-only canonical Windows architecture
    environment.  Its stdout and stderr are captured separately, hash-bound,
    scanned, and then mirrored to the human-readable log.  A native zero exit
    with a forbidden diagnostic is not accepted as success.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    executable = Path(command[0]).name.lower()
    is_rscript = executable in {"rscript", "rscript.exe"}
    if not is_rscript:
        with log_path.open("a", encoding="utf-8", newline="\n") as log:
            log.write("COMMAND_FINGERPRINT " + hashlib.sha256("\0".join(command).encode()).hexdigest() + "\n")
            log.flush()
            completed = subprocess.run(
                command,
                cwd=cwd,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
            )
        return completed.returncode

    child_env, architecture = _r_subprocess_environment(env)
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=child_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    matches = [
        pattern
        for pattern in FORBIDDEN_R_PROCESS_PATTERNS
        if pattern in f"{stdout}\n{stderr}".lower()
    ]
    with log_path.open("a", encoding="utf-8", newline="\n") as log:
        log.write("COMMAND_FINGERPRINT " + hashlib.sha256("\0".join(command).encode()).hexdigest() + "\n")
        log.write("STDOUT_BEGIN\n")
        log.write(stdout)
        if stdout and not stdout.endswith("\n"):
            log.write("\n")
        log.write("STDOUT_END\nSTDERR_BEGIN\n")
        log.write(stderr)
        if stderr and not stderr.endswith("\n"):
            log.write("\n")
        log.write("STDERR_END\n")
    evidence = {
        "schema_version": "1.0",
        "status": "passed" if completed.returncode == 0 and not matches else "failed",
        "native_returncode": completed.returncode,
        "native_exit_zero": completed.returncode == 0,
        "forbidden_patterns": list(FORBIDDEN_R_PROCESS_PATTERNS),
        "forbidden_matches": matches,
        "stdout_sha256": hashlib.sha256(stdout.encode("utf-8")).hexdigest(),
        "stdout_size_bytes": len(stdout.encode("utf-8")),
        "stderr_sha256": hashlib.sha256(stderr.encode("utf-8")).hexdigest(),
        "stderr_size_bytes": len(stderr.encode("utf-8")),
        "command_fingerprint": hashlib.sha256("\0".join(command).encode()).hexdigest(),
        "architecture": architecture,
        "parent_environment_modified": False,
        "native_zero_with_forbidden_output_accepted": False,
        "absolute_paths_included": False,
    }
    write_json_atomic(log_path.with_suffix(".process.json"), evidence)
    if completed.returncode == 0 and matches:
        return 86
    return completed.returncode


def _raise_subprocess_error(returncode: int, log_path: Path) -> None:
    tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-30:]
    raise CaseError(
        f"subprocess failed with exit code {returncode}; log={log_path}\n" + "\n".join(tail)
    )


def require_absolute(path: Path, label: str) -> Path:
    if not path.is_absolute():
        raise CaseError(f"{label} must be an absolute path: {path}")
    resolved = path.resolve()
    if resolved == Path(resolved.anchor):
        raise CaseError(f"{label} must not be a filesystem root: {resolved}")
    return resolved


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def copy_immutable(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise CaseError(f"tutorial source is missing: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if not destination.is_file() or sha256_file(source) != sha256_file(destination):
            raise CaseError(f"refusing to overwrite a different immutable file: {destination}")
        return
    shutil.copy2(source, destination)


def initialize_run_tree(run_root: Path, *, fresh: bool) -> None:
    if fresh and (run_root / "manifest" / "run_manifest.json").exists():
        raise CaseError("run refuses an existing run manifest; use resume")
    if fresh and (run_root / "04_intermediate").exists():
        existing = [item for item in (run_root / "04_intermediate").rglob("*") if item.is_file()]
        if existing:
            raise CaseError("run refuses existing intermediate artifacts; use resume")

    directories = [
        "00_request",
        "01_plan",
        "02_environment",
        "03_scripts",
        "04_intermediate/_staging",
        "05_results/tables",
        "05_results/objects",
        "06_figures/original",
        "06_figures/final",
        "06_figures/review",
        "07_reports",
        "inputs",
        "logs",
        "manifest",
    ]
    for relative in directories:
        (run_root / relative).mkdir(parents=True, exist_ok=True)

    copies = {
        "PROMPT.md": "00_request/PROMPT.md",
        "request.json": "00_request/request.json",
        "input-manifest.json": "00_request/input-manifest.json",
        "DATA_LICENSE.md": "00_request/DATA_LICENSE.md",
        "ANALYSIS_DESIGN.md": "01_plan/ANALYSIS_DESIGN.md",
        "route.json": "01_plan/route.json",
        "workflow.plan.json": "01_plan/workflow.plan.json",
        "environment-spec.json": "02_environment/environment-spec.json",
        "config/bioconductor-3.21-archive-pins.json": "02_environment/bioconductor-3.21-archive-pins.json",
        "run_pipeline.R": "03_scripts/run_pipeline.R",
        "prepare_environment.R": "03_scripts/prepare_environment.R",
        "validate_environment_cache.R": "03_scripts/validate_environment_cache.R",
        "prepare_environment.py": "03_scripts/prepare_environment.py",
        "download_inputs.py": "03_scripts/download_inputs.py",
        "case_driver.py": "03_scripts/case_driver.py",
        "config/analysis-params.json": "03_scripts/analysis-params.json",
    }
    for source, destination in copies.items():
        copy_immutable(CASE_DIR / source, run_root / destination)
    visual_destination = run_root / "03_scripts" / "visual-params.json"
    if fresh:
        copy_immutable(CASE_DIR / "config" / "visual-params.json", visual_destination)
    elif not visual_destination.is_file():
        raise CaseError("resume requires the prior visual-params.json; it cannot be reconstructed silently")


def run_process(command: list[str], *, cwd: Path, log_path: Path, env: dict[str, str] | None = None) -> None:
    returncode = _run_process_status(command, cwd=cwd, log_path=log_path, env=env)
    if returncode != 0:
        _raise_subprocess_error(returncode, log_path)


def probe_rscript(rscript: Path, log_path: Path) -> None:
    if not rscript.is_file():
        raise CaseError(f"Rscript does not exist: {rscript}")
    command = [
        str(rscript),
        "--vanilla",
        "-e",
        (
            "v <- as.character(getRversion()); "
            f"if (v != '{EXPECTED_R}') stop(sprintf('required R {EXPECTED_R}; found %s', v)); "
            "cat(v, '\\n')"
        ),
    ]
    run_process(command, cwd=CASE_DIR, log_path=log_path)


def environment_cache_key() -> str:
    spec_hash = sha256_file(CASE_DIR / "environment-spec.json")
    provisioner_hash = sha256_file(CASE_DIR / "prepare_environment.R")
    pins_hash = sha256_file(CASE_DIR / BIOC_PINS_RELATIVE)
    input_manifest_hash = sha256_file(CASE_DIR / "input-manifest.json")
    if input_manifest_hash != EXPECTED_INPUT_MANIFEST_SHA256:
        raise CaseError("input manifest changed without an explicit environment cache-key review")
    return hashlib.sha256(
        (
            f"{spec_hash}|{provisioner_hash}|{pins_hash}|input={input_manifest_hash}|"
            f"renv_lock={EXPECTED_RENV_LOCK_SHA256}|R={EXPECTED_R}|"
            f"Seurat={EXPECTED_SEURAT}|Bioc={EXPECTED_BIOC_RELEASE}"
        ).encode()
    ).hexdigest()[:24]


def environment_root_for(cache_root: Path) -> Path:
    return cache_root / f"{CASE_ID}-r{EXPECTED_R.replace('.', '')}-seurat{EXPECTED_SEURAT.replace('.', '')}-{environment_cache_key()}"


def _validate_environment_completion(
    *,
    env_root: Path,
    locked_output: Path,
    probe_output: Path,
    completion_marker: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Validate a native-exit, same-cohort Bioconductor environment transaction."""
    lock_path = env_root / "renv.lock"
    status_path = env_root / "renv-status.json"
    required_files = (lock_path, status_path, locked_output, probe_output, completion_marker)
    if not all(path.is_file() for path in required_files):
        missing = [path.name for path in required_files if not path.is_file()]
        raise CaseError("environment completion evidence is incomplete: " + ", ".join(missing))
    locked = read_json(locked_output)
    probe = read_json(probe_output)
    status = read_json(status_path)
    completion = read_json(completion_marker)
    if completion.get("stage") != "environment-provision" or completion.get("status") != "complete":
        raise CaseError("environment completion marker is not terminal")
    pins_path = CASE_DIR / BIOC_PINS_RELATIVE
    pins_sha256 = sha256_file(pins_path)
    pins = read_json(pins_path)
    expected_hashes = {
        "renv_lock_sha256": sha256_file(lock_path),
        "renv_status_sha256": sha256_file(status_path),
        "probe_sha256": sha256_file(probe_output),
        "environment_marker_sha256": sha256_file(locked_output),
        "bioconductor_pins_sha256": pins_sha256,
        "bioconductor_archive_manifest_sha256": pins["archive_manifest_sha256"],
    }
    for key, expected in expected_hashes.items():
        if completion.get(key) != expected:
            raise CaseError(f"environment completion marker {key} mismatch")
    if completion.get("shutdown_mode") != "native_exit":
        raise CaseError("environment completion marker shutdown mode mismatch")
    bioc = locked.get("bioconductor", {})
    if (
        bioc.get("release") != EXPECTED_BIOC_RELEASE
        or bioc.get("version") != EXPECTED_BIOC_VERSION
        or bioc.get("pins_sha256") != pins_sha256
        or bioc.get("archive_manifest_sha256") != pins["archive_manifest_sha256"]
        or bioc.get("glmGamPoi_version") != EXPECTED_GLMGAMPOI
        or bioc.get("SparseArray_version") != EXPECTED_SPARSEARRAY
        or bioc.get("same_release_closure") is not True
        or bioc.get("source_compilation_allowed") is not False
        or bioc.get("cross_release_packages_detected") is not False
        or bioc.get("lock_closure_version_match") is not True
        or bioc.get("restore_plan_empty") is not True
    ):
        raise CaseError("environment marker violates the frozen Bioconductor 3.21 closure")
    if locked.get("shutdown_mode") != "native_exit":
        raise CaseError("environment marker does not require native R exit")
    annotation_gate = locked.get("repository", {}).get("annotation_source_index_gate", {})
    if (
        annotation_gate.get("repository") != EXPECTED_BIOC_ANNOTATION_REPOSITORY
        or annotation_gate.get("contrib_url") != EXPECTED_BIOC_ANNOTATION_CONTRIB
        or annotation_gate.get("package") != "GenomeInfoDbData"
        or annotation_gate.get("version") != "1.2.14"
        or annotation_gate.get("NeedsCompilation") != "no"
        or not re.fullmatch(r"[0-9a-f]{64}", str(annotation_gate.get("sha256", "")))
    ):
        raise CaseError("environment marker lacks the exact BioCann source-index metadata gate")
    if locked.get("probe", {}).get("sha256") != expected_hashes["probe_sha256"]:
        raise CaseError("environment marker probe hash mismatch")
    if locked.get("renv_lock_sha256") != expected_hashes["renv_lock_sha256"]:
        raise CaseError("environment marker renv.lock hash mismatch")
    if (
        locked.get("renv_status", {}).get("sha256") != expected_hashes["renv_status_sha256"]
        or locked.get("renv_status", {}).get("synchronized") is not True
        or locked.get("renv_status", {}).get("sources_checked") is not True
        or locked.get("renv_status", {}).get("status_difference_count") != 0
        or locked.get("renv_status", {}).get("restore_action_count") != 0
        or locked.get("renv_status", {}).get("installation_package_type") != "binary"
        or locked.get("renv_status", {}).get("provenance_lookup_package_type") != "source"
        or locked.get("renv_status", {}).get("base_library_role") != "exact_R_recommended_packages"
        or locked.get("renv_status", {}).get("project_library_role") != "task_local_renv_project_library"
        or locked.get("renv_status", {}).get("validated_library_search_set") != [
            "task_local_renv_project_library", "exact_R_home_library"
        ]
        or locked.get("renv_status", {}).get("r_home_base_library_asserted") is not True
        or locked.get("renv_status", {}).get("project_only_mismatch_difference_count") != 13
        or tuple(locked.get("renv_status", {}).get("project_only_mismatch_packages", [])) != EXPECTED_R_RECOMMENDED
        or status.get("status") != "passed"
        or status.get("synchronized") is not True
        or status.get("sources_checked") is not True
        or status.get("status_difference_count") != 0
        or status.get("status_differences") != []
        or status.get("restore_action_count") != 0
        or status.get("installation_package_type") != "binary"
        or status.get("provenance_lookup_package_type") != "source"
        or status.get("base_library_role") != "exact_R_recommended_packages"
        or status.get("project_library_role") != "task_local_renv_project_library"
        or status.get("validated_library_search_set") != [
            "task_local_renv_project_library", "exact_R_home_library"
        ]
        or status.get("r_home_base_library_asserted") is not True
        or status.get("project_only_mismatch_difference_count") != 13
        or tuple(
            record.get("package")
            for record in status.get("project_only_mismatch_differences", [])
        ) != EXPECTED_R_RECOMMENDED
        or any(
            record.get("action") != "install"
            for record in status.get("project_only_mismatch_differences", [])
        )
        or status.get("project_only_mismatch_provenance") != "v4_failed_status_scope_audit"
        or status.get("bioconductor_release") != EXPECTED_BIOC_RELEASE
    ):
        raise CaseError("environment renv status evidence is not synchronized")
    if (
        probe.get("status") != "passed"
        or probe.get("renv_lock_sha256") != expected_hashes["renv_lock_sha256"]
        or probe.get("shutdown_mode") != "native_exit"
        or probe.get("bioconductor_pins_sha256") != pins_sha256
        or probe.get("glmGamPoi_version") != EXPECTED_GLMGAMPOI
        or probe.get("SparseArray_version") != EXPECTED_SPARSEARRAY
    ):
        raise CaseError("environment probe is not passed or is not bound to renv.lock")
    return locked, probe, completion


def prepared_environment_if_valid(run_root: Path, cache_root: Path) -> Path | None:
    env_root = environment_root_for(cache_root)
    run_marker = run_root / "02_environment" / "environment.locked.json"
    run_lock = run_root / "02_environment" / "renv.lock"
    run_probe = run_root / "02_environment" / "environment.probe.json"
    run_status = run_root / "02_environment" / "renv-status.json"
    completion_marker = run_root / "02_environment" / "environment-provision.complete.json"
    cache_marker = env_root / "environment.locked.json"
    cache_lock = env_root / "renv.lock"
    cache_probe = env_root / "environment.probe.json"
    cache_status = env_root / "renv-status.json"
    evidence_path = run_root / "02_environment" / "environment-evidence.json"
    run_evidence = (run_marker, run_lock, run_probe, run_status, completion_marker, evidence_path)
    if not any(path.exists() for path in run_evidence):
        # A prior failed binary transaction may leave a partial task-local cache.
        # It is safe to re-enter the exact same snapshot/version plan; nothing is
        # reusable until a complete run-local marker and lock are copied.
        return None
    if not all(path.is_file() for path in run_evidence):
        raise CaseError("partial run-local environment evidence exists; refusing silent reuse")
    candidates = (
        run_marker,
        run_lock,
        cache_marker,
        cache_lock,
        cache_probe,
        cache_status,
        evidence_path,
        env_root / ".Rprofile",
        env_root / "renv" / "activate.R",
    )
    if not all(path.is_file() for path in candidates):
        raise CaseError("partial environment lock/cache evidence exists; refusing silent reuse or overwrite")
    marker = read_json(run_marker)
    cache_marker_payload = read_json(cache_marker)
    evidence = read_json(evidence_path)
    locked_checked, probe_checked, completion_checked = _validate_environment_completion(
        env_root=env_root,
        locked_output=cache_marker,
        probe_output=cache_probe,
        completion_marker=completion_marker,
    )
    resolved = read_json(run_root / "00_request" / "resolved-inputs.json")
    h5_records = [item for item in resolved.get("files", []) if item.get("file_id") == "filtered_h5"]
    if len(h5_records) != 1:
        raise CaseError("cannot validate cached environment without one frozen filtered_h5")
    required = (
        marker.get("status") == "frozen"
        and marker.get("r_version") == EXPECTED_R
        and marker.get("task_local_renv_version") == EXPECTED_RENV
        and marker.get("seurat_version") == EXPECTED_SEURAT
        and marker.get("packages", {}).get("renv") == EXPECTED_RENV
        and marker.get("packages", {}).get("BiocManager") == EXPECTED_BIOCMANAGER
        and marker.get("bootstrap_renv_version") == EXPECTED_BOOTSTRAP_RENV
        and marker.get("bootstrap_renv_role") == "task_local_snapshot_bootstrap_not_host"
        and marker.get("bootstrap", {}).get("host_renv_required") is False
        and marker.get("bootstrap", {}).get("binary_version") == EXPECTED_BOOTSTRAP_RENV
        and marker.get("bootstrap", {}).get("binary_basename") == EXPECTED_BOOTSTRAP_RENV_BINARY
        and marker.get("bootstrap", {}).get("binary_size_bytes") == EXPECTED_BOOTSTRAP_RENV_SIZE
        and marker.get("bootstrap", {}).get("expected_binary_size_bytes") == EXPECTED_BOOTSTRAP_RENV_SIZE
        and marker.get("bootstrap", {}).get("binary_sha256") == EXPECTED_BOOTSTRAP_RENV_SHA256
        and marker.get("bootstrap", {}).get("expected_binary_sha256") == EXPECTED_BOOTSTRAP_RENV_SHA256
        and marker.get("bootstrap", {}).get("binary_index_repository") == PPM_BINARY_CONTRIB
        and marker.get("bootstrap", {}).get("binary_index_md5_available") is False
        and marker.get("packages", {}).get("hdf5r") == "1.3.12"
        and marker.get("packages", {}).get("BiocVersion") == EXPECTED_BIOC_VERSION
        and marker.get("packages", {}).get("glmGamPoi") == EXPECTED_GLMGAMPOI
        and marker.get("packages", {}).get("SparseArray") == EXPECTED_SPARSEARRAY
        and marker.get("shutdown_mode") == "native_exit"
        and marker.get("repository", {}).get("snapshot_url") == PPM_SNAPSHOT
        and marker.get("repository", {}).get("package_type") == "binary"
        and marker.get("repository", {}).get("provenance_lookup_package_type") == "source"
        and marker.get("repository", {}).get("annotation_source_index_gate", {}).get("version") == "1.2.14"
        and marker.get("repository", {}).get("annotation_source_index_gate", {}).get("NeedsCompilation") == "no"
        and marker.get("repository", {}).get("dependencies_argument") == "NA"
        and marker.get("verification", {}).get("exact_task_local_renv") is True
        and marker.get("verification", {}).get("exact_bootstrap_renv") is True
        and marker.get("verification", {}).get("bootstrap_renv_excluded_from_run_library") is True
        and marker.get("verification", {}).get("host_renv_not_required") is True
        and marker.get("verification", {}).get("bootstrap_binary_index_version_before_install") is True
        and marker.get("verification", {}).get("bootstrap_binary_index_repository_before_install") is True
        and marker.get("verification", {}).get("bootstrap_binary_pinned_sha256_before_install") is True
        and marker.get("verification", {}).get("windows_binary_snapshot_gate") is True
        and marker.get("verification", {}).get("binary_only_install") is True
        and marker.get("verification", {}).get("renv_lock_snapshot_url") is True
        and marker.get("verification", {}).get("exact_BiocManager_1_30_27") is True
        and marker.get("verification", {}).get("renv_status_synchronized") is True
        and marker.get("verification", {}).get("renv_restore_plan_empty") is True
        and marker.get("verification", {}).get("provenance_source_lookup_only") is True
        and marker.get("verification", {}).get("annotation_source_index_resolved") is True
        and marker.get("verification", {}).get("read10x_h5_smoke") is True
        and marker.get("h5_reader_smoke", {}).get("input_sha256") == h5_records[0].get("sha256")
        and marker.get("renv_lock_sha256") == sha256_file(run_lock)
        and sha256_file(run_lock) == sha256_file(cache_lock)
        and sha256_file(run_marker) == sha256_file(cache_marker)
        and sha256_file(run_probe) == sha256_file(cache_probe)
        and sha256_file(run_status) == sha256_file(cache_status)
        and cache_marker_payload == marker
        and locked_checked == marker
        and probe_checked == read_json(run_probe)
        and completion_checked.get("status") == "complete"
        and evidence.get("cache_key") == environment_cache_key()
        and evidence.get("run_lock_sha256") == sha256_file(run_lock)
        and evidence.get("probe_sha256") == sha256_file(run_probe)
        and evidence.get("renv_status_sha256") == sha256_file(run_status)
        and evidence.get("completion_marker_sha256") == sha256_file(completion_marker)
        and evidence.get("native_returncode") == 0
        and evidence.get("shutdown_mode") == "native_exit"
        and evidence.get("bioconductor_pins_sha256") == sha256_file(CASE_DIR / BIOC_PINS_RELATIVE)
        and evidence.get("read10x_h5_smoke") is True
    )
    if not required:
        raise CaseError("environment lock/cache marker failed exact-version, hash, or H5-smoke validation")
    # Write reuse evidence only after every run/cache marker, lock, probe,
    # completion marker, native-exit, H5-smoke, and exact-version gates passed.
    reuse_evidence = {
        "schema_version": "1.0",
        "status": "passed",
        "reuse": True,
        "cache_key": environment_cache_key(),
        "environment_root_basename": env_root.name,
        "validation_function": "prepared_environment_if_valid",
        "fully_validated_before_evidence_write": True,
        "task_local_bootstrap": True,
        "host_package_required": False,
        "host_renv_required": False,
        "run_lock_sha256": sha256_file(run_lock),
        "cache_lock_sha256": sha256_file(cache_lock),
        "run_probe_sha256": sha256_file(run_probe),
        "cache_probe_sha256": sha256_file(cache_probe),
        "run_renv_status_sha256": sha256_file(run_status),
        "cache_renv_status_sha256": sha256_file(cache_status),
        "run_marker_sha256": sha256_file(run_marker),
        "cache_marker_sha256": sha256_file(cache_marker),
        "completion_marker_sha256": sha256_file(completion_marker),
        "environment_evidence_sha256": sha256_file(evidence_path),
        "shutdown_mode": "native_exit",
        "native_returncode": 0,
        "bioconductor_pins_sha256": sha256_file(CASE_DIR / BIOC_PINS_RELATIVE),
        "absolute_paths_included": False,
    }
    write_json_atomic(run_root / "logs" / "environment-cache-reuse.json", reuse_evidence)
    return env_root


def _complete_cached_environment_root(cache_root: Path) -> Path | None:
    env_root = environment_root_for(cache_root)
    legacy_root = cache_root / (
        f"{CASE_ID}-r{EXPECTED_R.replace('.', '')}-seurat"
        f"{EXPECTED_SEURAT.replace('.', '')}-{LEGACY_ENVIRONMENT_CACHE_KEY}"
    )
    for candidate in (env_root, legacy_root):
        required = (
            candidate / "environment.locked.json",
            candidate / "environment.probe.json",
            candidate / "renv-status.json",
            candidate / "renv.lock",
            candidate / ".Rprofile",
            candidate / "renv" / "activate.R",
        )
        present = [path.is_file() for path in required]
        if not any(present):
            continue
        if not all(present):
            missing = [path.name for path, exists in zip(required, present) if not exists]
            raise CaseError("partial environment cache cannot be reused: " + ", ".join(missing))
        if sha256_file(candidate / "renv.lock") != EXPECTED_RENV_LOCK_SHA256:
            raise CaseError("environment cache renv.lock differs from the reviewed exact lock")
        if candidate == legacy_root and legacy_root != env_root:
            if env_root.exists():
                raise CaseError("reviewed and legacy environment cache roots conflict")
            os.replace(legacy_root, env_root)
        return env_root
    return None


def reuse_complete_environment_cache(
    run_root: Path,
    cache_root: Path,
    input_root: Path,
    rscript: Path,
    fault_injection: str | None = None,
) -> Path | None:
    """Validate a complete cache in native R, then bind it to a fresh run."""
    env_root = _complete_cached_environment_root(cache_root)
    if env_root is None:
        return None
    cache_marker = env_root / "environment.locked.json"
    cache_probe = env_root / "environment.probe.json"
    cache_status = env_root / "renv-status.json"
    cache_lock = env_root / "renv.lock"
    marker = read_json(cache_marker)
    status = read_json(cache_status)
    probe = read_json(cache_probe)
    pins_path = run_root / "02_environment" / "bioconductor-3.21-archive-pins.json"
    if (
        marker.get("status") != "frozen"
        or marker.get("r_version") != EXPECTED_R
        or marker.get("seurat_version") != EXPECTED_SEURAT
        or marker.get("renv_lock_sha256") != sha256_file(cache_lock)
        or sha256_file(cache_lock) != EXPECTED_RENV_LOCK_SHA256
        or marker.get("bioconductor", {}).get("pins_sha256") != sha256_file(pins_path)
        or status.get("status") != "passed"
        or status.get("synchronized") is not True
        or status.get("status_difference_count") != 0
        or status.get("restore_action_count") != 0
        or probe.get("status") != "passed"
        or probe.get("renv_lock_sha256") != sha256_file(cache_lock)
    ):
        raise CaseError("complete environment cache failed its immutable marker/lock/status/probe precheck")

    resolved = read_json(run_root / "00_request" / "resolved-inputs.json")
    h5_records = [item for item in resolved.get("files", []) if item.get("file_id") == "filtered_h5"]
    if len(h5_records) != 1:
        raise CaseError("cached environment validation requires one frozen filtered_h5")
    smoke_h5 = input_root / h5_records[0]["filename"]
    if not smoke_h5.is_file() or sha256_file(smoke_h5) != h5_records[0]["sha256"]:
        raise CaseError("cached environment validation H5 is missing or hash-mismatched")

    run_marker = run_root / "02_environment" / "environment.locked.json"
    run_probe = run_root / "02_environment" / "environment.probe.json"
    run_status = run_root / "02_environment" / "renv-status.json"
    run_lock = run_root / "02_environment" / "renv.lock"
    copy_immutable(cache_marker, run_marker)
    copy_immutable(cache_probe, run_probe)
    copy_immutable(cache_status, run_status)
    copy_immutable(cache_lock, run_lock)

    validation_probe = run_root / "02_environment" / "environment-cache-reuse.probe.json"
    validation_log = run_root / "logs" / "environment-prepare.log"
    process_env = os.environ.copy()
    process_env["RENV_PROJECT"] = str(env_root)
    process_env["RENV_PATHS_ROOT"] = str(cache_root / "renv-cache")
    process_env["RENV_CONFIG_SANDBOX_ENABLED"] = "TRUE"
    command = [
        str(rscript),
        "--no-save",
        "--no-restore",
        str(run_root / "03_scripts" / "validate_environment_cache.R"),
        "--env-root",
        str(env_root),
        "--marker",
        str(run_marker),
        "--lock",
        str(run_lock),
        "--status",
        str(run_status),
        "--smoke-h5",
        str(smoke_h5),
        "--output",
        str(validation_probe),
        "--fault-injection",
        fault_injection or "none",
    ]
    native_returncode = _run_process_status(
        command, cwd=env_root, log_path=validation_log, env=process_env
    )
    if native_returncode != 0:
        _raise_subprocess_error(native_returncode, validation_log)
    validation = read_json(validation_probe)
    if (
        validation.get("status") != "passed"
        or validation.get("validation_mode") != "fresh_run_cache_reuse"
        or validation.get("renv_lock_sha256") != sha256_file(run_lock)
        or validation.get("h5_input_sha256") != h5_records[0]["sha256"]
        or validation.get("shutdown_mode") != "native_exit"
    ):
        raise CaseError("native R cache-reuse validation probe is incomplete or hash-mismatched")

    completion_marker = run_root / "02_environment" / "environment-provision.complete.json"
    completion = {
        "schema_version": "1.0",
        "stage": "environment-provision",
        "status": "complete",
        "shutdown_mode": "native_exit",
        "provision_mode": "validated_cache_reuse",
        "renv_lock_sha256": sha256_file(run_lock),
        "renv_status_sha256": sha256_file(run_status),
        "probe_sha256": sha256_file(run_probe),
        "environment_marker_sha256": sha256_file(run_marker),
        "bioconductor_pins_sha256": sha256_file(pins_path),
        "bioconductor_archive_manifest_sha256": marker.get("bioconductor", {}).get("archive_manifest_sha256"),
        "fault_injection": "none",
    }
    write_json_atomic(completion_marker, completion)
    locked_checked, probe_checked, completion_checked = _validate_environment_completion(
        env_root=env_root,
        locked_output=run_marker,
        probe_output=run_probe,
        completion_marker=completion_marker,
    )
    process_evidence_path = validation_log.with_suffix(".process.json")
    process_evidence = read_json(process_evidence_path)
    evidence = {
        "schema_version": "1.0",
        "status": "verified",
        "cache_key": environment_cache_key(),
        "provision_mode": "validated_cache_reuse",
        "run_lock_sha256": sha256_file(run_lock),
        "marker_sha256": sha256_file(run_marker),
        "probe_sha256": sha256_file(run_probe),
        "renv_status_sha256": sha256_file(run_status),
        "completion_marker_sha256": sha256_file(completion_marker),
        "completion_marker_status": completion_checked.get("status"),
        "exact_r": EXPECTED_R,
        "exact_task_local_renv": EXPECTED_RENV,
        "exact_BiocManager": EXPECTED_BIOCMANAGER,
        "exact_bootstrap_renv": EXPECTED_BOOTSTRAP_RENV,
        "bootstrap_renv_excluded_from_run_library": True,
        "host_renv_required": False,
        "bootstrap_renv_binary_sha256": locked_checked.get("bootstrap", {}).get("binary_sha256"),
        "exact_seurat": EXPECTED_SEURAT,
        "exact_hdf5r": "1.3.12",
        "read10x_h5_smoke": True,
        "repository_snapshot": PPM_SNAPSHOT,
        "package_type": "binary",
        "provenance_lookup_package_type": "source",
        "smoke_h5_sha256": h5_records[0]["sha256"],
        "probe_status": probe_checked.get("status"),
        "native_returncode": native_returncode,
        "shutdown_mode": "native_exit",
        "r_process_evidence_sha256": sha256_file(process_evidence_path),
        "r_process_forbidden_matches": process_evidence.get("forbidden_matches"),
        "processor_architecture": process_evidence.get("architecture", {}).get("processor_architecture"),
        "processor_architecture_restored": process_evidence.get("architecture", {}).get("processor_architecture_restored"),
        "parent_environment_modified": False,
        "bioconductor_release": EXPECTED_BIOC_RELEASE,
        "bioconductor_version": EXPECTED_BIOC_VERSION,
        "bioconductor_pins_sha256": sha256_file(pins_path),
        "bioconductor_archive_manifest_sha256": locked_checked.get("bioconductor", {}).get("archive_manifest_sha256"),
        "global_library_used_for_analysis": False,
        "cache_validation_probe_sha256": sha256_file(validation_probe),
    }
    write_json_atomic(run_root / "02_environment" / "environment-evidence.json", evidence)
    return env_root


def prepare_environment(
    run_root: Path,
    cache_root: Path,
    rscript: Path,
    input_root: Path,
    *,
    fault_injection: str | None = None,
) -> Path:
    if fault_injection not in {None, "before_completion_marker"}:
        raise CaseError("unsupported environment fault injection")
    prepared = prepared_environment_if_valid(run_root, cache_root)
    if prepared is not None:
        return prepared
    reused = reuse_complete_environment_cache(
        run_root,
        cache_root,
        input_root,
        rscript,
        fault_injection=fault_injection,
    )
    if reused is not None:
        prepared = prepared_environment_if_valid(run_root, cache_root)
        if prepared is None:
            raise CaseError("validated environment cache was not bound to the fresh run")
        return prepared
    env_root = environment_root_for(cache_root)
    renv_cache = cache_root / "renv-cache"
    env_root.mkdir(parents=True, exist_ok=True)
    renv_cache.mkdir(parents=True, exist_ok=True)
    locked_cache = env_root / "environment.locked.json"
    probe_cache = env_root / "environment.probe.json"
    status_cache = env_root / "renv-status.json"
    completion_marker = run_root / "02_environment" / "environment-provision.complete.json"
    resolved_inputs = read_json(run_root / "00_request" / "resolved-inputs.json")
    h5_records = [item for item in resolved_inputs.get("files", []) if item.get("file_id") == "filtered_h5"]
    if len(h5_records) != 1:
        raise CaseError("resolved input manifest must contain exactly one filtered_h5")
    smoke_h5 = input_root / h5_records[0]["filename"]
    if not smoke_h5.is_file() or sha256_file(smoke_h5) != h5_records[0]["sha256"]:
        raise CaseError("filtered H5 is missing or does not match its frozen hash before environment smoke")
    process_env = os.environ.copy()
    process_env["RENV_PATHS_ROOT"] = str(renv_cache)
    process_env["RENV_CONFIG_SANDBOX_ENABLED"] = "TRUE"
    process_env["RENV_CONFIG_SYNCHRONIZED_CHECK"] = "TRUE"
    base_command = [
        str(rscript),
        "--vanilla",
        str(run_root / "03_scripts" / "prepare_environment.R"),
        "--env-root",
        str(env_root),
        "--locked-output",
        str(locked_cache),
        "--expected-r",
        EXPECTED_R,
        "--seurat-version",
        EXPECTED_SEURAT,
        "--repository-snapshot",
        PPM_SNAPSHOT,
        "--smoke-h5",
        str(smoke_h5),
        "--probe-output",
        str(probe_cache),
        "--completion-marker",
        str(completion_marker),
        "--bioconductor-pins",
        str(run_root / "02_environment" / "bioconductor-3.21-archive-pins.json"),
        "--fault-injection",
        fault_injection or "none",
    ]
    log_path = run_root / "logs" / "environment-prepare.log"
    completion_marker.unlink(missing_ok=True)
    native_returncode = _run_process_status(
        base_command, cwd=run_root, log_path=log_path, env=process_env
    )
    if native_returncode != 0:
        completion_marker.unlink(missing_ok=True)
        _raise_subprocess_error(native_returncode, log_path)

    renv_lock = env_root / "renv.lock"
    locked, probe, completion = _validate_environment_completion(
        env_root=env_root,
        locked_output=locked_cache,
        probe_output=probe_cache,
        completion_marker=completion_marker,
    )
    if locked.get("status") != "frozen":
        raise CaseError("environment marker is not frozen")
    if locked.get("r_version") != EXPECTED_R or locked.get("seurat_version") != EXPECTED_SEURAT:
        raise CaseError("environment marker violates exact R/Seurat pins")
    if locked.get("task_local_renv_version") != EXPECTED_RENV or locked.get("packages", {}).get("renv") != EXPECTED_RENV:
        raise CaseError("environment marker violates exact task-local renv 1.2.2 pin")
    if locked.get("packages", {}).get("BiocManager") != EXPECTED_BIOCMANAGER:
        raise CaseError("environment marker violates exact BiocManager 1.30.27 pin")
    if locked.get("bootstrap_renv_version") != EXPECTED_BOOTSTRAP_RENV:
        raise CaseError("environment marker violates exact task-local bootstrap renv 1.2.2 pin")
    bootstrap = locked.get("bootstrap", {})
    if (
        locked.get("bootstrap_renv_role") != "task_local_snapshot_bootstrap_not_host"
        or bootstrap.get("host_renv_required") is not False
        or bootstrap.get("binary_version") != EXPECTED_BOOTSTRAP_RENV
        or bootstrap.get("binary_basename") != EXPECTED_BOOTSTRAP_RENV_BINARY
        or bootstrap.get("binary_size_bytes") != EXPECTED_BOOTSTRAP_RENV_SIZE
        or bootstrap.get("expected_binary_size_bytes") != EXPECTED_BOOTSTRAP_RENV_SIZE
        or bootstrap.get("binary_sha256") != EXPECTED_BOOTSTRAP_RENV_SHA256
        or bootstrap.get("expected_binary_sha256") != EXPECTED_BOOTSTRAP_RENV_SHA256
        or bootstrap.get("binary_index_repository") != PPM_BINARY_CONTRIB
        or bootstrap.get("binary_index_md5_available") is not False
    ):
        raise CaseError("environment marker lacks hash-bound task-local bootstrap evidence")
    if locked.get("packages", {}).get("hdf5r") != "1.3.12":
        raise CaseError("environment marker violates exact hdf5r 1.3.12 pin")
    if (
        locked.get("packages", {}).get("BiocVersion") != EXPECTED_BIOC_VERSION
        or locked.get("packages", {}).get("glmGamPoi") != EXPECTED_GLMGAMPOI
        or locked.get("packages", {}).get("SparseArray") != EXPECTED_SPARSEARRAY
    ):
        raise CaseError("environment marker violates exact Bioconductor 3.21 key package pins")
    repository = locked.get("repository", {})
    if (
        repository.get("snapshot_url") != PPM_SNAPSHOT
        or repository.get("package_type") != "binary"
        or repository.get("provenance_lookup_package_type") != "source"
        or repository.get("dependencies_argument") != "NA"
    ):
        raise CaseError("environment marker violates the reviewed Windows binary snapshot contract")
    annotation_gate = repository.get("annotation_source_index_gate", {})
    if (
        annotation_gate.get("repository") != EXPECTED_BIOC_ANNOTATION_REPOSITORY
        or annotation_gate.get("contrib_url") != EXPECTED_BIOC_ANNOTATION_CONTRIB
        or annotation_gate.get("version") != "1.2.14"
        or annotation_gate.get("NeedsCompilation") != "no"
    ):
        raise CaseError("environment marker lacks the reviewed BioCann source-index gate")
    verification = locked.get("verification", {})
    if not all(
        verification.get(key) is True
        for key in (
            "exact_task_local_renv",
            "exact_bootstrap_renv",
            "bootstrap_renv_excluded_from_run_library",
            "host_renv_not_required",
            "bootstrap_binary_index_version_before_install",
            "bootstrap_binary_index_repository_before_install",
            "bootstrap_binary_pinned_sha256_before_install",
            "windows_binary_snapshot_gate",
            "binary_only_install",
            "renv_lock_snapshot_url",
            "exact_BiocManager_1_30_27",
            "renv_status_synchronized",
            "renv_restore_plan_empty",
            "provenance_source_lookup_only",
            "annotation_source_index_resolved",
            "exact_bioconductor_3_21",
            "exact_glmGamPoi_1_20_0",
            "exact_SparseArray_1_8_1",
            "complete_same_release_closure",
            "all_archives_hash_verified_before_install",
            "all_archive_descriptions_verified_before_install",
            "annotation_data_requires_no_compilation",
            "no_cross_release_packages",
            "native_r_shutdown",
            "child_processor_architecture_amd64",
        )
    ):
        raise CaseError("environment marker lacks binary snapshot/lock verification gates")
    if locked.get("h5_reader_smoke", {}).get("input_sha256") != h5_records[0]["sha256"]:
        raise CaseError("environment marker is not bound to the frozen H5 smoke input")
    if not locked.get("verification", {}).get("read10x_h5_smoke"):
        raise CaseError("environment marker lacks a successful Read10X_h5 smoke test")
    if locked.get("renv_lock_sha256") != sha256_file(renv_lock):
        raise CaseError("cached renv.lock hash does not match environment marker")
    if sha256_file(renv_lock) != EXPECTED_RENV_LOCK_SHA256:
        raise CaseError("generated renv.lock differs from the reviewed exact lock")
    copy_immutable(locked_cache, run_root / "02_environment" / "environment.locked.json")
    copy_immutable(renv_lock, run_root / "02_environment" / "renv.lock")
    copy_immutable(probe_cache, run_root / "02_environment" / "environment.probe.json")
    copy_immutable(status_cache, run_root / "02_environment" / "renv-status.json")
    process_evidence = read_json(log_path.with_suffix(".process.json"))
    evidence = {
        "schema_version": "1.0",
        "status": "verified",
        "cache_key": environment_cache_key(),
        "run_lock_sha256": sha256_file(run_root / "02_environment" / "renv.lock"),
        "marker_sha256": sha256_file(run_root / "02_environment" / "environment.locked.json"),
        "probe_sha256": sha256_file(run_root / "02_environment" / "environment.probe.json"),
        "renv_status_sha256": sha256_file(run_root / "02_environment" / "renv-status.json"),
        "completion_marker_sha256": sha256_file(completion_marker),
        "completion_marker_status": completion.get("status"),
        "exact_r": EXPECTED_R,
        "exact_task_local_renv": EXPECTED_RENV,
        "exact_BiocManager": EXPECTED_BIOCMANAGER,
        "exact_bootstrap_renv": EXPECTED_BOOTSTRAP_RENV,
        "bootstrap_renv_excluded_from_run_library": True,
        "host_renv_required": False,
        "bootstrap_renv_binary_sha256": locked.get("bootstrap", {}).get("binary_sha256"),
        "exact_seurat": EXPECTED_SEURAT,
        "exact_hdf5r": "1.3.12",
        "read10x_h5_smoke": True,
        "repository_snapshot": PPM_SNAPSHOT,
        "package_type": "binary",
        "provenance_lookup_package_type": "source",
        "smoke_h5_sha256": h5_records[0]["sha256"],
        "probe_status": probe.get("status"),
        "native_returncode": native_returncode,
        "shutdown_mode": "native_exit",
        "r_process_evidence_sha256": sha256_file(log_path.with_suffix(".process.json")),
        "r_process_forbidden_matches": process_evidence.get("forbidden_matches"),
        "processor_architecture": process_evidence.get("architecture", {}).get("processor_architecture"),
        "processor_architecture_restored": process_evidence.get("architecture", {}).get("processor_architecture_restored"),
        "parent_environment_modified": False,
        "bioconductor_release": EXPECTED_BIOC_RELEASE,
        "bioconductor_version": EXPECTED_BIOC_VERSION,
        "bioconductor_pins_sha256": sha256_file(CASE_DIR / BIOC_PINS_RELATIVE),
        "bioconductor_archive_manifest_sha256": locked.get("bioconductor", {}).get("archive_manifest_sha256"),
        "global_library_used_for_analysis": False,
    }
    evidence_path = run_root / "02_environment" / "environment-evidence.json"
    if evidence_path.exists() and read_json(evidence_path) != evidence:
        raise CaseError("environment evidence changed within the same run")
    if not evidence_path.exists():
        write_json_atomic(evidence_path, evidence)
    return env_root


def fetch_inputs(run_root: Path, input_root: Path) -> dict[str, Any]:
    manifest = read_json(run_root / "00_request" / "input-manifest.json")
    source_records = manifest.get("files", [])
    required_assets = manifest.get("required_extracted_assets", [])
    cache_was_complete = bool(source_records) and all(
        (input_root / record["filename"]).is_file()
        and (input_root / record["filename"]).stat().st_size == int(record["expected_size_bytes"])
        and sha256_file(input_root / record["filename"]) == record["expected_sha256"]
        for record in source_records
    ) and all((input_root / relative).is_file() for relative in required_assets)
    command = [
        sys.executable,
        str(run_root / "03_scripts" / "download_inputs.py"),
        "fetch",
        "--manifest",
        str(run_root / "00_request" / "input-manifest.json"),
        "--input-root",
        str(input_root),
        "--resolved-manifest",
        str(run_root / "00_request" / "resolved-inputs.json"),
    ]
    run_process(command, cwd=run_root, log_path=run_root / "logs" / "input-fetch.log")
    resolved = read_json(run_root / "00_request" / "resolved-inputs.json")
    evidence = {
        "schema_version": "1.0",
        "status": "passed",
        "reuse": cache_was_complete,
        "fully_validated_before_evidence_write": True,
        "cache_scope": "task_local_external_to_run_root",
        "materialization": "direct_read_no_copy",
        "download_performed": not cache_was_complete,
        "canonical_inputs_modified": False,
        "resolved_manifest_sha256": sha256_file(run_root / "00_request" / "resolved-inputs.json"),
        "files": [
            {
                "file_id": record["file_id"],
                "size_bytes": record["size_bytes"],
                "sha256": record["sha256"],
            }
            for record in sorted(resolved.get("files", []), key=lambda item: item["file_id"])
        ],
        "absolute_paths_included": False,
    }
    write_json_atomic(run_root / "logs" / "input-cache-reuse.json", evidence)
    return evidence


def execute_pipeline(
    run_root: Path,
    cache_root: Path,
    input_root: Path,
    rscript: Path,
    mode: str,
) -> None:
    if mode not in {"run", "resume"}:
        raise CaseError(f"unsupported pipeline mode: {mode}")
    evidence_mode = "fresh" if mode == "run" else "resume"
    probe_rscript(rscript, run_root / "logs" / "r-probe.log")
    fetch_inputs(run_root, input_root)
    env_root = prepare_environment(run_root, cache_root, rscript, input_root)
    process_env = os.environ.copy()
    process_env["RENV_PROJECT"] = str(env_root)
    process_env["RENV_PATHS_ROOT"] = str(cache_root / "renv-cache")
    process_env["RENV_CONFIG_SANDBOX_ENABLED"] = "TRUE"
    environment_lock = read_json(run_root / "02_environment" / "environment.locked.json")
    if environment_lock.get("shutdown_mode") != "native_exit":
        raise CaseError("pipeline requires a native-exit environment lock")
    completion_marker = run_root / "logs" / f"pipeline-{evidence_mode}.complete.json"
    completion_marker.unlink(missing_ok=True)
    command = [
        str(rscript),
        "--no-save",
        "--no-restore",
        str(run_root / "03_scripts" / "run_pipeline.R"),
        "--run-root",
        str(run_root),
        "--input-root",
        str(input_root),
        "--resolved-manifest",
        str(run_root / "00_request" / "resolved-inputs.json"),
        "--analysis-params",
        str(run_root / "03_scripts" / "analysis-params.json"),
        "--visual-params",
        str(run_root / "03_scripts" / "visual-params.json"),
        "--environment-lock",
        str(run_root / "02_environment" / "environment.locked.json"),
        "--mode",
        mode,
        "--completion-marker",
        str(completion_marker),
    ]
    run_process(
        command,
        cwd=env_root,
        log_path=run_root / "logs" / f"pipeline-{evidence_mode}.log",
        env=process_env,
    )
    if not completion_marker.is_file():
        raise CaseError("pipeline returned zero without a native completion marker")
    completion = read_json(completion_marker)
    expected = {
        "stage": "pipeline",
        "status": "complete",
        "mode": mode,
        "shutdown_mode": "native_exit",
        "code_hash": sha256_file(run_root / "03_scripts" / "run_pipeline.R"),
        "analysis_config_hash": sha256_file(run_root / "03_scripts" / "analysis-params.json"),
        "visual_config_hash": sha256_file(run_root / "03_scripts" / "visual-params.json"),
        "environment_lock_hash": environment_lock.get("renv_lock_sha256"),
    }
    for key, value in expected.items():
        if completion.get(key) != value:
            raise CaseError(f"pipeline native completion marker {key} mismatch")
    process_evidence_path = run_root / "logs" / f"pipeline-{evidence_mode}.process.json"
    process_evidence = read_json(process_evidence_path)
    write_json_atomic(
        run_root / "logs" / f"pipeline-{evidence_mode}-native-exit-evidence.json",
        {
            "schema_version": "1.0",
            "status": "passed",
            "mode": evidence_mode,
            "pipeline_mode": mode,
            "shutdown_mode": "native_exit",
            "native_returncode": process_evidence.get("native_returncode"),
            "forbidden_matches": process_evidence.get("forbidden_matches"),
            "process_evidence_sha256": sha256_file(process_evidence_path),
            "completion_marker_sha256": sha256_file(completion_marker),
            "processor_architecture": process_evidence.get("architecture", {}).get("processor_architecture"),
            "processor_architecture_restored": process_evidence.get("architecture", {}).get("processor_architecture_restored"),
            "parent_environment_modified": False,
            "absolute_paths_included": False,
        },
    )


def validate_resume_checkpoint_reuse(run_root: Path) -> dict[str, Any]:
    """Require the resume subprocess log to prove reuse of every checkpoint."""
    log_path = run_root / "logs" / "pipeline-resume.log"
    if not log_path.is_file():
        raise CaseError("resume checkpoint evidence log is missing")
    normalized = log_path.read_text(encoding="utf-8", errors="strict").replace("\\", "/")
    observed = [
        key for key in EXPECTED_CHECKPOINT_KEYS if f"\tresume_reuse\t{key}" in normalized
    ]
    if tuple(observed) != EXPECTED_CHECKPOINT_KEYS:
        missing = [key for key in EXPECTED_CHECKPOINT_KEYS if key not in observed]
        raise CaseError("resume did not reuse every expected checkpoint: " + ", ".join(missing))
    if "\tstage_start\t" in normalized:
        raise CaseError("resume unexpectedly started a computational stage instead of reusing all checkpoints")
    evidence = {
        "schema_version": "1.0",
        "status": "passed",
        "all_checkpoints_reused": True,
        "expected_checkpoint_keys": list(EXPECTED_CHECKPOINT_KEYS),
        "observed_resume_reuse_keys": observed,
        "stage_start_observed": False,
        "pipeline_resume_log_sha256": sha256_file(log_path),
        "absolute_paths_included": False,
    }
    write_json_atomic(run_root / "logs" / "checkpoint-resume-reuse.json", evidence)
    return evidence


def parse_png_dimensions(path: Path) -> tuple[int, int]:
    with path.open("rb") as handle:
        header = handle.read(24)
    if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise CaseError(f"invalid PNG header: {path}")
    return struct.unpack(">II", header[16:24])


def validate_review(run_root: Path, *, require_keep: bool) -> dict[str, Any]:
    state_path = run_root / "06_figures" / "review" / "visual-review-state.json"
    if not state_path.is_file():
        raise CaseError("visual-review-state.json is missing")
    state = read_json(state_path)
    round_number = int(state.get("current_round", 0))
    if round_number not in (1, 2, 3):
        raise CaseError("invalid visual review round")
    review_path = run_root / "06_figures" / "review" / f"review-round-{round_number}.json"
    if not review_path.is_file():
        raise CaseError(f"native review is pending: {review_path.name} is missing")
    review = read_json(review_path)
    if review.get("round") != round_number:
        raise CaseError("native review round does not match review state")
    if review.get("reviewer_method") != "native_local_image_view":
        raise CaseError("reviewer_method must be native_local_image_view")
    if not review.get("opened_original_and_final"):
        raise CaseError("review must assert that original and final-size images were opened")
    reviewer_tool = review.get("reviewer_tool")
    if not isinstance(reviewer_tool, str) or not reviewer_tool or reviewer_tool.startswith("REQUIRED_"):
        raise CaseError("review must name the actual native image-viewing tool")
    if review.get("evidence_level") not in {"pixels_only", "image_metadata", "image_code", "image_code_data"}:
        raise CaseError("invalid review evidence_level")
    registered = {item["figure_id"]: item for item in state.get("renders", [])}
    reviewed = {item["figure_id"]: item for item in review.get("figure_reviews", [])}
    if not registered or set(registered) != set(reviewed):
        raise CaseError("reviewed figure IDs do not exactly match registered figures")
    for figure_id, expected in registered.items():
        item = reviewed[figure_id]
        for kind in ("original", "final"):
            path = run_root / expected[f"{kind}_path"]
            if not path.is_file():
                raise CaseError(f"registered figure is missing: {path}")
            actual_hash = sha256_file(path)
            if expected[f"{kind}_sha256"] != actual_hash or item[f"{kind}_sha256"] != actual_hash:
                raise CaseError(f"native review hash mismatch for {figure_id} {kind}")
        decision = item.get("decision")
        if decision not in {"keep", "revise", "reselect", "blocked"}:
            raise CaseError(f"invalid figure decision for {figure_id}")
        if require_keep and decision != "keep":
            raise CaseError(f"figure {figure_id} is not kept: {decision}")
        for finding in item.get("findings", []):
            if not isinstance(finding, dict):
                raise CaseError(f"finding for {figure_id} must be an object")
            if require_keep and finding.get("severity") in {"blocker", "major"} and finding.get("status") != "resolved":
                raise CaseError(f"unresolved {finding.get('severity')} finding for {figure_id}")
    overall = review.get("overall_decision")
    if overall not in {"keep", "revise", "reselect", "blocked"}:
        raise CaseError("invalid overall native review decision")
    if require_keep and overall != "keep":
        raise CaseError(f"native review is not delivery-ready: {overall}")
    return {"path": review_path, "review": review, "state": state}


def append_ledger_once(run_root: Path, record: dict[str, Any]) -> None:
    ledger = run_root / "manifest" / "artifact_ledger.jsonl"
    existing_ids: set[str] = set()
    if ledger.exists():
        for line in ledger.read_text(encoding="utf-8").splitlines():
            if line.strip():
                existing_ids.add(json.loads(line)["artifact_id"])
    if record["artifact_id"] in existing_ids:
        return
    with ledger.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")


def preserve_reviewed_manifest_across_resume(source: Path, destination: Path) -> None:
    """Preserve the first reviewed manifest while allowing only run/resume mode drift."""
    if not destination.exists() or sha256_file(source) == sha256_file(destination):
        copy_immutable(source, destination)
        return
    current = read_json(source)
    reviewed = read_json(destination)
    current_without_mode = {key: value for key, value in current.items() if key != "mode"}
    reviewed_without_mode = {key: value for key, value in reviewed.items() if key != "mode"}
    if current_without_mode != reviewed_without_mode:
        raise CaseError(
            "reviewed run manifest changed beyond the expected run/resume mode field"
        )


def report_run(run_root: Path) -> dict[str, Any]:
    evidence = validate_review(run_root, require_keep=False)
    review = evidence["review"]
    state = evidence["state"]
    review_path: Path = evidence["path"]
    overall = review["overall_decision"]
    state["status"] = overall
    state["terminal_review_path"] = review_path.relative_to(run_root).as_posix()
    state["terminal_review_sha256"] = sha256_file(review_path)
    write_json_atomic(run_root / "06_figures" / "review" / "visual-review-state.json", state)

    summary_path = run_root / "manifest" / "execution-summary.json"
    manifest_path = run_root / "manifest" / "run_manifest.json"
    summary = read_json(summary_path)
    manifest = read_json(manifest_path)
    input_cache = read_json(run_root / "logs" / "input-cache-reuse.json")
    environment_cache = read_json(run_root / "logs" / "environment-cache-reuse.json")
    environment_lock = read_json(run_root / "02_environment" / "environment.locked.json")
    environment_evidence = read_json(run_root / "02_environment" / "environment-evidence.json")
    summary["cache_reuse"] = {
        "input": {
            "status": "passed" if input_cache.get("reuse") is True else "not_reused",
            "direct_read_no_copy": input_cache.get("materialization") == "direct_read_no_copy",
            "evidence_sha256": sha256_file(run_root / "logs" / "input-cache-reuse.json"),
        },
        "environment": {
            "status": "passed" if environment_cache.get("reuse") is True else "not_reused",
            "native_validated": environment_cache.get("fully_validated_before_evidence_write") is True,
            "current_cache_key": environment_cache.get("cache_key"),
            "current_root_basename": environment_cache.get("environment_root_basename"),
            "historical_build_root_basename": environment_lock.get("environment_root_basename"),
            "historical_build_root_role": "build_time_origin_only",
            "migration_status": "exact_lock_hash_bound_cache_key_adoption",
            "current_root_native_r_revalidation": (
                "passed"
                if environment_evidence.get("provision_mode") == "validated_cache_reuse"
                and environment_evidence.get("native_returncode") == 0
                and environment_evidence.get("r_process_forbidden_matches") == []
                else "failed"
            ),
            "native_revalidation_process_sha256": environment_evidence.get("r_process_evidence_sha256"),
            "evidence_sha256": sha256_file(run_root / "logs" / "environment-cache-reuse.json"),
        },
    }
    summary["validation"]["input_cache_reuse"] = summary["cache_reuse"]["input"]["status"]
    summary["validation"]["environment_cache_reuse"] = summary["cache_reuse"]["environment"]["status"]
    summary["validation"]["native_visual_review"] = "passed" if overall == "keep" else overall
    summary["status"] = "DELIVERED" if overall == "keep" else f"VISUAL_REVIEW_{overall.upper()}"
    manifest["native_visual_review"] = overall
    manifest["state"] = "DELIVERED" if overall == "keep" else f"VISUAL_REVIEW_{overall.upper()}"
    write_json_atomic(summary_path, summary)
    write_json_atomic(manifest_path, manifest)

    qa_path = run_root / "07_reports" / "QA_REPORT.md"
    qa = qa_path.read_text(encoding="utf-8")
    replacement = (
        f"| Native coordinate alignment and visual review | {'pass' if overall == 'keep' else overall} | "
        f"`{review_path.name}` SHA-256 `{sha256_file(review_path)}` |"
    )
    qa = re.sub(r"\| Native coordinate alignment and visual review \|.*", replacement, qa)
    cache_row = (
        "| Migrated environment cache provenance | pass | historical build basename `"
        f"{environment_lock.get('environment_root_basename')}` retained only as origin; current hash-bound "
        f"cache key `{environment_cache.get('cache_key')}`, current basename "
        f"`{environment_cache.get('environment_root_basename')}`, native R revalidation rc=0 with no forbidden output |"
    )
    if "| Migrated environment cache provenance |" not in qa:
        qa = qa.replace(replacement, cache_row + "\n" + replacement)
    qa = qa.replace(
        "A pending native review is a release blocker. Generated PNGs alone do not satisfy native review.",
        (
            "Native review opened every registered original/final pair and reached a delivery-ready keep decision."
            if overall == "keep"
            else f"Native review reached `{overall}`; the case is not delivery-ready."
        ),
    )
    write_text_atomic(qa_path, qa)

    visible_lines: list[str] = []
    cannot_lines: list[str] = []
    for item in review["figure_reviews"]:
        for statement in item.get("visible", []):
            visible_lines.append(f"- `{item['figure_id']}`: {statement}")
        for statement in item.get("cannot_assert", []):
            cannot_lines.append(f"- `{item['figure_id']}`: {statement}")
    results_path = run_root / "07_reports" / "RESULTS.md"
    results = results_path.read_text(encoding="utf-8")
    results = results.replace(
        "Vendor image assets and positive finite scale factors passed structural QC; native alignment review remains pending.",
        (
            "Vendor image assets and positive finite scale factors passed structural QC; "
            "the hash-bound native alignment review passed."
            if overall == "keep"
            else "Vendor image assets and positive finite scale factors passed structural QC; "
            f"the hash-bound native alignment review concluded `{overall}`."
        ),
    )
    marker = "\n## Native visual review\n"
    if marker not in results:
        block = [
            "",
            "## Native visual review",
            "",
            f"Overall decision: `{overall}` (round {review['round']}).",
            "",
            "### Pixel-grounded observations",
            "",
            *(visible_lines or ["- No pixel-grounded biological statement was entered by the reviewer."]),
            "",
            "### Cannot assert",
            "",
            *(cannot_lines or ["- Claim limits remain those stated in the analysis design."]),
            "",
        ]
        results = results.rstrip() + "\n" + "\n".join(block)
    cache_marker = "\n## Environment cache provenance\n"
    if cache_marker not in results:
        cache_block = [
            "",
            "## Environment cache provenance",
            "",
            (
                f"The cached environment was originally built under basename `"
                f"{environment_lock.get('environment_root_basename')}`; that value is retained only as "
                "historical build provenance."
            ),
            (
                f"This run used current hash-bound cache key `{environment_cache.get('cache_key')}` and "
                f"current basename `{environment_cache.get('environment_root_basename')}` only after a "
                "fresh native R validation of exact package versions, the reviewed renv.lock, zero status "
                "differences, an empty restore plan, and the frozen H5 smoke input."
            ),
            "",
        ]
        results = results.rstrip() + "\n" + "\n".join(cache_block)
    write_text_atomic(results_path, results)

    reviewed_report_dir = run_root / "07_reports" / f"round-{review['round']}-reviewed"
    reviewed_report_dir.mkdir(parents=True, exist_ok=True)
    for name in ("RESULTS.md", "FIGURE_NOTES.md", "QA_REPORT.md"):
        copy_immutable(run_root / "07_reports" / name, reviewed_report_dir / name)
    reviewed_summary_path = run_root / "manifest" / f"execution-summary-round-{review['round']}-reviewed.json"
    reviewed_manifest_path = run_root / "manifest" / f"run-manifest-round-{review['round']}-reviewed.json"
    reviewed_state_path = run_root / "06_figures" / "review" / f"visual-review-state-round-{review['round']}-reviewed.json"
    copy_immutable(summary_path, reviewed_summary_path)
    preserve_reviewed_manifest_across_resume(manifest_path, reviewed_manifest_path)
    copy_immutable(run_root / "06_figures" / "review" / "visual-review-state.json", reviewed_state_path)

    lock = read_json(run_root / "02_environment" / "environment.locked.json")
    review_record = {
        "artifact_id": f"visium-native-review-round-{review['round']}",
        "stage_id": "S95_VISUALIZE_INTERPRET",
        "role": "native_visual_review",
        "type": "file",
        "format": "json",
        "path": review_path.relative_to(run_root).as_posix(),
        "sha256": sha256_file(review_path),
        "size_bytes": review_path.stat().st_size,
        "producer": {"recipe_id": "native-local-image-review", "code_version": "human-or-agent-review-record"},
        "consumers": ["QA_REPORT.md", "RESULTS.md", "execution-summary.json"],
        "environment_lock_hash": lock["renv_lock_sha256"],
        "validation": {"status": "passed" if overall == "keep" else "pending", "rules": ["hash-bound original/final views", "actual native viewer named"]},
        "maturity": "native-reviewed" if overall == "keep" else "data-verified",
        "conclusion_role": "visual_quality_and_coordinate_alignment_review",
    }
    append_ledger_once(run_root, review_record)
    if overall == "keep":
        for figure in review["figure_reviews"]:
            for kind in ("original", "final"):
                figure_path = run_root / figure[f"{kind}_path"]
                append_ledger_once(
                    run_root,
                    {
                        "artifact_id": (
                            f"visium-native-reviewed-round-{review['round']}-"
                            f"{figure['figure_id']}-{kind}"
                        ),
                        "stage_id": "S95_VISUALIZE_INTERPRET",
                        "role": "figure",
                        "type": "file",
                        "format": "png",
                        "path": figure_path.relative_to(run_root).as_posix(),
                        "sha256": sha256_file(figure_path),
                        "size_bytes": figure_path.stat().st_size,
                        "producer": {
                            "recipe_id": "visium-mouse-brain-seurat-v1",
                            "code_version": sha256_file(
                                run_root / "03_scripts" / "run_pipeline.R"
                            ),
                        },
                        "consumers": [review_path.relative_to(run_root).as_posix()],
                        "environment_lock_hash": lock["renv_lock_sha256"],
                        "units": {
                            "assay_unit": "spot",
                            "spatial_unit": "Visium spot",
                            "sampling_unit": "one tissue section",
                            "inference_unit": "not_applicable_descriptive",
                        },
                        "spatial_frame": {
                            "coordinate_system": "10x_visium_vendor_image_coordinates",
                            "coordinate_unit": "pixel",
                            "transform_id": "vendor_scalefactors_json_lowres",
                        },
                        "validation": {
                            "status": "passed",
                            "rules": [
                                "registered hash opened with native_local_image_view",
                                "original/final pair reviewed",
                                "no unresolved blocker or major finding",
                            ],
                        },
                        "maturity": "native-reviewed",
                        "conclusion_role": "descriptive_spatial_overview",
                    },
                )
    final_artifacts = [
        (f"visium-round-{review['round']}-results-report", reviewed_report_dir / "RESULTS.md", "report", "md"),
        (f"visium-round-{review['round']}-figure-notes", reviewed_report_dir / "FIGURE_NOTES.md", "report", "md"),
        (f"visium-round-{review['round']}-qa-report", reviewed_report_dir / "QA_REPORT.md", "report", "md"),
        (f"visium-round-{review['round']}-execution-summary", reviewed_summary_path, "manifest", "json"),
        (f"visium-round-{review['round']}-run-manifest", reviewed_manifest_path, "manifest", "json"),
        (
            f"visium-round-{review['round']}-visual-review-state",
            reviewed_state_path,
            "review_state",
            "json",
        ),
    ]
    for artifact_id, path, role, file_format in final_artifacts:
        append_ledger_once(
            run_root,
            {
                "artifact_id": artifact_id,
                "stage_id": "S95_VISUALIZE_INTERPRET",
                "role": role,
                "type": "file",
                "format": file_format,
                "path": path.relative_to(run_root).as_posix(),
                "sha256": sha256_file(path),
                "size_bytes": path.stat().st_size,
                "producer": {"recipe_id": "visium-case-report-v1", "code_version": sha256_file(Path(__file__))},
                "consumers": [],
                "environment_lock_hash": lock["renv_lock_sha256"],
                "validation": {"status": "passed", "rules": ["readable", "sha256-bound", "native-review-propagated"]},
                "maturity": "native-reviewed" if overall == "keep" else "data-verified",
                "conclusion_role": "auditable_final_delivery",
            },
        )

    ledger_records = [
        json.loads(line)
        for line in (run_root / "manifest" / "artifact_ledger.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    index_lines = [
        "# ARTIFACT INDEX",
        "",
        "| Artifact ID | Stage | Role | Path | SHA-256 | Size (bytes) | Maturity |",
        "|---|---|---|---|---|---:|---|",
    ]
    for record in ledger_records:
        index_lines.append(
            f"| {record['artifact_id']} | {record['stage_id']} | {record['role']} | "
            f"`{record['path']}` | `{record['sha256']}` | {record['size_bytes']} | {record['maturity']} |"
        )
    index_lines.extend(["", f"Native visual review decision: `{overall}`.", ""])
    write_text_atomic(run_root / "07_reports" / "ARTIFACT_INDEX.md", "\n".join(index_lines))
    return {"status": summary["status"], "overall_decision": overall, "review": review_path.name}


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def verify_run(
    run_root: Path,
    input_root: Path,
    *,
    require_native: bool = True,
) -> dict[str, Any]:
    required = [
        "00_request/request.json",
        "00_request/input-manifest.json",
        "00_request/resolved-inputs.json",
        "01_plan/ANALYSIS_DESIGN.md",
        "01_plan/route.json",
        "01_plan/workflow.plan.json",
        "02_environment/environment.locked.json",
        "02_environment/renv.lock",
        "02_environment/environment.probe.json",
        "02_environment/renv-status.json",
        "02_environment/environment-provision.complete.json",
        "02_environment/environment-evidence.json",
        "02_environment/bioconductor-3.21-archive-pins.json",
        "03_scripts/run_pipeline.R",
        "05_results/tables/barcode_reconciliation.csv",
        "05_results/tables/barcode_set_reconciliation.json",
        "05_results/tables/barcode_set_differences.csv",
        "05_results/tables/attrition.csv",
        "05_results/tables/cluster_counts.csv",
        "05_results/objects/analysis_final_seurat.rds",
        "04_intermediate/S40_PREPROCESS/preprocess_summary.json",
        "logs/pipeline-warnings.json",
        "logs/input-cache-reuse.json",
        "logs/pipeline-fresh.complete.json",
        "logs/pipeline-fresh.process.json",
        "logs/pipeline-fresh-native-exit-evidence.json",
        "07_reports/RESULTS.md",
        "07_reports/QA_REPORT.md",
        "07_reports/ARTIFACT_INDEX.md",
        "manifest/run_manifest.json",
        "manifest/artifact_ledger.jsonl",
        "manifest/execution-summary.json",
    ]
    missing = [item for item in required if not (run_root / item).is_file()]
    if missing:
        raise CaseError("required run artifacts are missing: " + ", ".join(missing))

    env_lock = read_json(run_root / "02_environment" / "environment.locked.json")
    if env_lock.get("status") != "frozen" or env_lock.get("r_version") != EXPECTED_R or env_lock.get("seurat_version") != EXPECTED_SEURAT:
        raise CaseError("environment lock violates exact version contract")
    if env_lock.get("task_local_renv_version") != EXPECTED_RENV or env_lock.get("packages", {}).get("renv") != EXPECTED_RENV:
        raise CaseError("environment lock does not pin exact task-local renv 1.2.2")
    if env_lock.get("packages", {}).get("BiocManager") != EXPECTED_BIOCMANAGER:
        raise CaseError("environment lock does not pin exact BiocManager 1.30.27")
    if env_lock.get("bootstrap_renv_version") != EXPECTED_BOOTSTRAP_RENV:
        raise CaseError("environment lock does not pin task-local bootstrap renv 1.2.2")
    bootstrap = env_lock.get("bootstrap", {})
    if (
        env_lock.get("bootstrap_renv_role") != "task_local_snapshot_bootstrap_not_host"
        or bootstrap.get("host_renv_required") is not False
        or bootstrap.get("binary_version") != EXPECTED_BOOTSTRAP_RENV
        or bootstrap.get("binary_basename") != EXPECTED_BOOTSTRAP_RENV_BINARY
        or bootstrap.get("binary_size_bytes") != EXPECTED_BOOTSTRAP_RENV_SIZE
        or bootstrap.get("expected_binary_size_bytes") != EXPECTED_BOOTSTRAP_RENV_SIZE
        or bootstrap.get("binary_sha256") != EXPECTED_BOOTSTRAP_RENV_SHA256
        or bootstrap.get("expected_binary_sha256") != EXPECTED_BOOTSTRAP_RENV_SHA256
        or bootstrap.get("binary_index_repository") != PPM_BINARY_CONTRIB
        or bootstrap.get("binary_index_md5_available") is not False
    ):
        raise CaseError("environment lock lacks task-local bootstrap binary evidence")
    if env_lock.get("packages", {}).get("hdf5r") != "1.3.12":
        raise CaseError("environment lock does not pin exact hdf5r 1.3.12")
    if (
        env_lock.get("packages", {}).get("BiocVersion") != EXPECTED_BIOC_VERSION
        or env_lock.get("packages", {}).get("glmGamPoi") != EXPECTED_GLMGAMPOI
        or env_lock.get("packages", {}).get("SparseArray") != EXPECTED_SPARSEARRAY
    ):
        raise CaseError("environment lock does not pin the exact Bioconductor 3.21 backend")
    bioc = env_lock.get("bioconductor", {})
    pins_path = run_root / "02_environment" / "bioconductor-3.21-archive-pins.json"
    pins = read_json(pins_path)
    if (
        env_lock.get("shutdown_mode") != "native_exit"
        or bioc.get("release") != EXPECTED_BIOC_RELEASE
        or bioc.get("version") != EXPECTED_BIOC_VERSION
        or bioc.get("pins_sha256") != sha256_file(pins_path)
        or bioc.get("archive_manifest_sha256") != pins.get("archive_manifest_sha256")
        or bioc.get("same_release_closure") is not True
        or bioc.get("source_compilation_allowed") is not False
        or bioc.get("cross_release_packages_detected") is not False
        or bioc.get("lock_closure_version_match") is not True
        or bioc.get("restore_plan_empty") is not True
    ):
        raise CaseError("environment lock violates native-exit Bioconductor 3.21 closure policy")
    repository = env_lock.get("repository", {})
    if (
        repository.get("snapshot_url") != PPM_SNAPSHOT
        or repository.get("package_type") != "binary"
        or repository.get("provenance_lookup_package_type") != "source"
        or repository.get("dependencies_argument") != "NA"
    ):
        raise CaseError("environment lock does not freeze the reviewed Windows binary snapshot")
    annotation_gate = repository.get("annotation_source_index_gate", {})
    if (
        annotation_gate.get("repository") != EXPECTED_BIOC_ANNOTATION_REPOSITORY
        or annotation_gate.get("contrib_url") != EXPECTED_BIOC_ANNOTATION_CONTRIB
        or annotation_gate.get("version") != "1.2.14"
        or annotation_gate.get("NeedsCompilation") != "no"
    ):
        raise CaseError("environment lock lacks the reviewed BioCann source-index gate")
    verification = env_lock.get("verification", {})
    if not all(
        verification.get(key) is True
        for key in (
            "exact_task_local_renv",
            "exact_bootstrap_renv",
            "bootstrap_renv_excluded_from_run_library",
            "host_renv_not_required",
            "bootstrap_binary_index_version_before_install",
            "bootstrap_binary_index_repository_before_install",
            "bootstrap_binary_pinned_sha256_before_install",
            "windows_binary_snapshot_gate",
            "binary_only_install",
            "renv_lock_snapshot_url",
            "exact_BiocManager_1_30_27",
            "renv_status_synchronized",
            "renv_restore_plan_empty",
            "provenance_source_lookup_only",
            "annotation_source_index_resolved",
            "exact_bioconductor_3_21",
            "exact_glmGamPoi_1_20_0",
            "exact_SparseArray_1_8_1",
            "complete_same_release_closure",
            "all_archives_hash_verified_before_install",
            "all_archive_descriptions_verified_before_install",
            "annotation_data_requires_no_compilation",
            "no_cross_release_packages",
            "native_r_shutdown",
            "child_processor_architecture_amd64",
        )
    ):
        raise CaseError("environment lock lacks binary snapshot verification evidence")
    if not env_lock.get("verification", {}).get("read10x_h5_smoke"):
        raise CaseError("environment lock lacks a successful Read10X_h5 smoke test")
    if env_lock.get("renv_lock_sha256") != sha256_file(run_root / "02_environment" / "renv.lock"):
        raise CaseError("run-local renv.lock hash mismatch")
    renv_status_path = run_root / "02_environment" / "renv-status.json"
    if (
        env_lock.get("renv_status", {}).get("sha256") != sha256_file(renv_status_path)
        or read_json(renv_status_path).get("synchronized") is not True
    ):
        raise CaseError("run-local renv status evidence is not synchronized")
    # Completion evidence is validated against run-local immutable copies here;
    # cache reuse performs the corresponding cache-root validation earlier.
    run_probe = run_root / "02_environment" / "environment.probe.json"
    completion = read_json(run_root / "02_environment" / "environment-provision.complete.json")
    environment_evidence = read_json(run_root / "02_environment" / "environment-evidence.json")
    completion_hashes = {
        "renv_lock_sha256": sha256_file(run_root / "02_environment" / "renv.lock"),
        "renv_status_sha256": sha256_file(renv_status_path),
        "probe_sha256": sha256_file(run_probe),
        "environment_marker_sha256": sha256_file(run_root / "02_environment" / "environment.locked.json"),
        "bioconductor_pins_sha256": sha256_file(pins_path),
        "bioconductor_archive_manifest_sha256": pins.get("archive_manifest_sha256"),
    }
    if completion.get("status") != "complete" or completion.get("shutdown_mode") != "native_exit" or any(
        completion.get(key) != expected for key, expected in completion_hashes.items()
    ):
        raise CaseError("run-local environment completion marker hash mismatch")
    if environment_evidence.get("completion_marker_sha256") != sha256_file(
        run_root / "02_environment" / "environment-provision.complete.json"
    ) or environment_evidence.get("native_returncode") != 0 or environment_evidence.get("shutdown_mode") != "native_exit":
        raise CaseError("environment wrapper evidence does not prove native R shutdown")

    input_command = [
        sys.executable,
        str(run_root / "03_scripts" / "download_inputs.py"),
        "verify",
        "--manifest",
        str(run_root / "00_request" / "input-manifest.json"),
        "--input-root",
        str(input_root),
        "--resolved-manifest",
        str(run_root / "00_request" / "resolved-inputs.json"),
    ]
    run_process(input_command, cwd=run_root, log_path=run_root / "logs" / "input-verify.log")

    resolved_inputs = read_json(run_root / "00_request" / "resolved-inputs.json")
    h5_records = [item for item in resolved_inputs.get("files", []) if item.get("file_id") == "filtered_h5"]
    if len(h5_records) != 1 or env_lock.get("h5_reader_smoke", {}).get("input_sha256") != h5_records[0].get("sha256"):
        raise CaseError("Read10X_h5 smoke evidence is not bound to the frozen filtered H5")

    summary = read_json(run_root / "manifest" / "execution-summary.json")
    pipeline_completion = read_json(run_root / "logs" / "pipeline-fresh.complete.json")
    pipeline_process = read_json(run_root / "logs" / "pipeline-fresh.process.json")
    pipeline_native = read_json(run_root / "logs" / "pipeline-fresh-native-exit-evidence.json")
    if (
        pipeline_completion.get("status") != "complete"
        or pipeline_completion.get("shutdown_mode") != "native_exit"
        or pipeline_process.get("native_returncode") != 0
        or pipeline_process.get("forbidden_matches") != []
        or pipeline_native.get("status") != "passed"
        or pipeline_native.get("native_returncode") != 0
        or pipeline_native.get("forbidden_matches") != []
        or pipeline_native.get("completion_marker_sha256")
        != sha256_file(run_root / "logs" / "pipeline-fresh.complete.json")
        or pipeline_native.get("process_evidence_sha256")
        != sha256_file(run_root / "logs" / "pipeline-fresh.process.json")
        or pipeline_completion.get("bioconductor_pins_sha256") != sha256_file(pins_path)
        or pipeline_completion.get("warning_evidence_sha256")
        != sha256_file(run_root / "logs" / "pipeline-warnings.json")
        or pipeline_completion.get("warning_occurrences") != 0
    ):
        raise CaseError("fresh pipeline lacks clean native-exit process evidence")
    warning_evidence = read_json(run_root / "logs" / "pipeline-warnings.json")
    input_cache_evidence = read_json(run_root / "logs" / "input-cache-reuse.json")
    if (
        input_cache_evidence.get("status") != "passed"
        or input_cache_evidence.get("fully_validated_before_evidence_write") is not True
        or input_cache_evidence.get("materialization") != "direct_read_no_copy"
        or input_cache_evidence.get("canonical_inputs_modified") is not False
        or input_cache_evidence.get("absolute_paths_included") is not False
    ):
        raise CaseError("input cache evidence is missing, unvalidated, or not direct-read")
    if require_native and input_cache_evidence.get("reuse") is not True:
        raise CaseError("terminal delivery requires a proved task-local input-cache reuse pass")
    warning_records = warning_evidence.get("records", [])
    if (
        warning_evidence.get("schema_version") != "1.0"
        or warning_evidence.get("classification_version") != "1.0"
        or warning_evidence.get("status") != "passed"
        or warning_evidence.get("warning_occurrences") != 0
        or warning_evidence.get("unique_warning_records") != 0
        or warning_evidence.get("blocking_warning_occurrences") != 0
        or warning_evidence.get("warning_allowlist_used") is not False
        or warning_evidence.get("scientific_parameters_changed") is not False
        or warning_evidence.get("absolute_paths_included") is not False
        or warning_evidence.get("code_hash") != sha256_file(run_root / "03_scripts" / "run_pipeline.R")
        or warning_evidence.get("analysis_config_hash") != sha256_file(run_root / "03_scripts" / "analysis-params.json")
        or warning_evidence.get("environment_lock_hash") != env_lock.get("renv_lock_sha256")
        or not isinstance(warning_records, list)
    ):
        raise CaseError("runtime warning evidence is missing, unbound, or release-blocked")
    forbidden_warning_categories = {
        "api_compatibility_warning",
        "numerical_integrity_warning",
        "sctransform_glm_nb_alternation_limit",
        "sctransform_theta_iteration_limit",
        "spatial_integrity_warning",
        "unclassified_warning",
    }
    if warning_records:
        raise CaseError(
            "runtime warning ledger contains an API/numerical/spatial/unknown blocker; "
            "this tutorial has no allowlist path"
        )
    if summary.get("validation", {}).get("runtime_warnings") != "passed":
        raise CaseError("execution summary does not propagate the runtime warning gate")
    preprocess = read_json(
        run_root / "04_intermediate" / "S40_PREPROCESS" / "preprocess_summary.json"
    )
    layers = preprocess.get("sct_layers", {})
    if (
        preprocess.get("status") != "passed"
        or preprocess.get("requested_vst_flavor") != "v2"
        or preprocess.get("requested_method") != "glmGamPoi_offset"
        or preprocess.get("actual_vst_flavor") != "v2"
        or preprocess.get("actual_method") != "glmGamPoi_offset"
        or preprocess.get("actual_glmGamPoi_check") is not True
        or preprocess.get("glmGamPoi_version") != EXPECTED_GLMGAMPOI
        or preprocess.get("BiocVersion") != EXPECTED_BIOC_VERSION
        or preprocess.get("SparseArray_version") != EXPECTED_SPARSEARRAY
        or preprocess.get("input_spots") != preprocess.get("retained_spots")
        or preprocess.get("sct_variable_features") != 3000
        or preprocess.get("pca_dimensions") != 30
        or preprocess.get("sct_non_finite_values") != 0
        or preprocess.get("pca_non_finite_values") != 0
        or set(layers) != {"counts", "data", "scale.data"}
        or any(record.get("non_finite_values") != 0 for record in layers.values())
        or preprocess.get("code_hash") != sha256_file(run_root / "03_scripts" / "run_pipeline.R")
        or preprocess.get("analysis_config_hash") != sha256_file(run_root / "03_scripts" / "analysis-params.json")
        or preprocess.get("environment_lock_hash") != env_lock.get("renv_lock_sha256")
        or preprocess.get("bioconductor_pins_sha256") != sha256_file(pins_path)
    ):
        raise CaseError("S40 does not prove the frozen glmGamPoi_offset/v2 finite backend")
    observed_preprocess = summary.get("observed", {}).get("preprocessing", {})
    if (
        observed_preprocess.get("method") != "glmGamPoi_offset"
        or observed_preprocess.get("vst_flavor") != "v2"
        or observed_preprocess.get("glmGamPoi_check") is not True
        or observed_preprocess.get("glmGamPoi_version") != EXPECTED_GLMGAMPOI
        or observed_preprocess.get("sct_non_finite_values") != 0
        or observed_preprocess.get("pca_non_finite_values") != 0
    ):
        raise CaseError("execution summary does not propagate the S40 backend evidence")
    reconciliation = {row["set"]: int(row["count"]) for row in read_csv_rows(run_root / "05_results" / "tables" / "barcode_reconciliation.csv")}
    set_reconciliation = read_json(run_root / "05_results" / "tables" / "barcode_set_reconciliation.json")
    set_differences = read_csv_rows(run_root / "05_results" / "tables" / "barcode_set_differences.csv")
    directed_differences = set_reconciliation.get("directed_difference_counts", {})
    if set_reconciliation.get("status") != "passed" or set_differences or not directed_differences:
        raise CaseError("assay/image/coordinate barcode reconciliation is not terminal-passed")
    if any(int(value) != 0 for value in directed_differences.values()):
        raise CaseError("assay/image/coordinate directed barcode differences are non-zero")
    attrition = read_csv_rows(run_root / "05_results" / "tables" / "attrition.csv")
    clusters = read_csv_rows(run_root / "05_results" / "tables" / "cluster_counts.csv")
    observed = summary.get("observed", {})
    comparisons = {
        "matrix_barcodes": reconciliation["matrix_barcodes"],
        "vendor_all_positions": reconciliation["vendor_all_positions"],
        "vendor_in_tissue_barcodes": reconciliation["vendor_in_tissue"],
        "loaded_spots": reconciliation["loaded_object"],
        "assay_cells": reconciliation["assay_cells"],
        "image_cells": reconciliation["image_cells"],
        "coordinate_barcodes": reconciliation["coordinates"],
        "retained_spots": int(attrition[-1]["count"]),
        "expression_spot_clusters": len(clusters),
    }
    for key, expected in comparisons.items():
        if int(observed.get(key, -1)) != expected:
            raise CaseError(f"execution summary mismatch for {key}: {observed.get(key)} != {expected}")
    observed_differences = observed.get("directed_assay_image_coordinate_differences", {})
    if observed_differences != directed_differences:
        raise CaseError("execution summary directed barcode differences do not match reconciliation evidence")

    visual = read_json(run_root / "03_scripts" / "visual-params.json")
    round_number = int(visual["render_round"])
    expected_dimensions = {
        "original": (
            round(float(visual["original_export"]["width_in"]) * int(visual["original_export"]["dpi"])),
            round(float(visual["original_export"]["height_in"]) * int(visual["original_export"]["dpi"])),
        ),
        "final": (
            round(float(visual["final_export"]["width_in"]) * int(visual["final_export"]["dpi"])),
            round(float(visual["final_export"]["height_in"]) * int(visual["final_export"]["dpi"])),
        ),
    }
    for kind in ("original", "final"):
        figures = sorted((run_root / "06_figures" / kind / f"round-{round_number}").glob("*.png"))
        if len(figures) != 3:
            raise CaseError(f"expected three {kind} figures, found {len(figures)}")
        for path in figures:
            if parse_png_dimensions(path) != expected_dimensions[kind]:
                raise CaseError(f"unexpected PNG dimensions for {path}")

    ledger_path = run_root / "manifest" / "artifact_ledger.jsonl"
    ledger_ids: set[str] = set()
    native_reviewed_figure_records = 0
    for line_number, line in enumerate(ledger_path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CaseError(f"invalid ledger JSON at line {line_number}: {exc}") from exc
        artifact_id = record.get("artifact_id")
        if not artifact_id or artifact_id in ledger_ids:
            raise CaseError(f"missing/duplicate ledger artifact_id at line {line_number}")
        ledger_ids.add(artifact_id)
        if record.get("role") == "figure" and record.get("maturity") == "native-reviewed":
            native_reviewed_figure_records += 1
        relative = Path(record["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise CaseError(f"unsafe ledger path: {relative}")
        artifact = run_root / relative
        if not artifact.is_file() or artifact.stat().st_size != int(record["size_bytes"]) or sha256_file(artifact) != record["sha256"]:
            raise CaseError(f"ledger artifact verification failed: {relative}")

    def scan_strings(value: Any) -> list[str]:
        if isinstance(value, str):
            return [value]
        if isinstance(value, dict):
            return [item for child in value.values() for item in scan_strings(child)]
        if isinstance(value, list):
            return [item for child in value for item in scan_strings(child)]
        return []

    # Require a drive-letter path to begin the text or follow a JSON/text
    # delimiter; otherwise the final ``s:/`` in ``https://`` is a false hit.
    sensitive_pattern = re.compile(
        r"(?:^|[\"'\s])[A-Za-z]:[\\/]|/home/|/Users/", re.IGNORECASE
    )
    leaked = [value for value in scan_strings(summary) if sensitive_pattern.search(value)]
    if leaked:
        raise CaseError("execution-summary.json contains an absolute/sensitive path")

    if require_native:
        cache_reuse = summary.get("cache_reuse", {})
        if (
            cache_reuse.get("input", {}).get("status") != "passed"
            or cache_reuse.get("input", {}).get("direct_read_no_copy") is not True
            or cache_reuse.get("environment", {}).get("status") != "passed"
            or cache_reuse.get("environment", {}).get("native_validated") is not True
            or cache_reuse.get("environment", {}).get("current_cache_key") != environment_cache_key()
            or cache_reuse.get("environment", {}).get("migration_status")
            != "exact_lock_hash_bound_cache_key_adoption"
            or cache_reuse.get("environment", {}).get("historical_build_root_role")
            != "build_time_origin_only"
            or cache_reuse.get("environment", {}).get("current_root_native_r_revalidation")
            != "passed"
        ):
            raise CaseError("terminal execution summary does not prove input/environment cache reuse")
        validate_review(run_root, require_keep=True)
        if native_reviewed_figure_records != 6:
            raise CaseError(
                "expected six native-reviewed figure artifact records (three original/final pairs)"
            )
        manifest = read_json(run_root / "manifest" / "run_manifest.json")
        if summary.get("status") != "DELIVERED" or manifest.get("state") != "DELIVERED":
            raise CaseError("terminal native review has not been propagated through report")
    return {
        "status": "verified" if not require_native else "delivered_verified",
        "case": CASE_ID,
        "observed": comparisons,
        "figure_pairs": 3,
        "native_review": "required_and_passed" if require_native else summary["validation"]["native_visual_review"],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("run", "resume"):
        sub = subparsers.add_parser(command)
        sub.add_argument("--authorized", action="store_true")
        sub.add_argument("--run-root", type=Path, required=True)
        sub.add_argument("--cache-root", type=Path, required=True)
        sub.add_argument("--input-cache-root", type=Path, required=True)
        sub.add_argument("--rscript", type=Path, required=True)
    for command in ("verify", "report"):
        sub = subparsers.add_parser(command)
        sub.add_argument("--run-root", type=Path, required=True)
        sub.add_argument("--input-cache-root", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        run_root = require_absolute(args.run_root, "--run-root")
        if args.command in {"run", "resume"}:
            if not args.authorized:
                raise CaseError("run/resume requires explicit --authorized from the authorized root CLI")
            cache_root = require_absolute(args.cache_root, "--cache-root")
            input_root = require_absolute(args.input_cache_root, "--input-cache-root")
            rscript = require_absolute(args.rscript, "--rscript")
            if run_root == cache_root or is_relative_to(run_root, cache_root) or is_relative_to(cache_root, run_root):
                raise CaseError("--run-root and --cache-root must be separate sibling task-local trees")
            if run_root == CASE_DIR or cache_root == CASE_DIR:
                raise CaseError("run/cache roots must not overwrite the tutorial source directory")
            if input_root == run_root or is_relative_to(input_root, run_root):
                raise CaseError("--input-cache-root must be external to the run root")
            if args.command == "resume" and not run_root.exists():
                raise CaseError("resume requires an existing run root")
            run_root.mkdir(parents=True, exist_ok=True)
            cache_root.mkdir(parents=True, exist_ok=True)
            initialize_run_tree(run_root, fresh=args.command == "run")
            input_root.mkdir(parents=True, exist_ok=True)
            execute_pipeline(run_root, cache_root, input_root, rscript, args.command)
            if args.command == "resume":
                validate_resume_checkpoint_reuse(run_root)
            result = verify_run(run_root, input_root, require_native=False)
            result["status"] = "awaiting_native_review"
        elif args.command == "report":
            if not run_root.is_dir():
                raise CaseError("report requires an existing run root")
            result = report_run(run_root)
        else:
            if not run_root.is_dir():
                raise CaseError("verify requires an existing run root")
            input_root = require_absolute(args.input_cache_root, "--input-cache-root")
            result = verify_run(run_root, input_root, require_native=True)
        print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))
        return 0
    except (CaseError, OSError, subprocess.SubprocessError) as exc:
        print(f"CASE_ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
