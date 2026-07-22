#!/usr/bin/env python3
"""Provision and freeze the exact task-local R environment for PBMC3K."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


CASE_DIR = Path(__file__).resolve().parent
EXPECTED_PACKAGES = {
    "Seurat": "5.5.0",
    "SeuratObject": "5.4.0",
    "ggplot2": "4.0.3",
    "patchwork": "1.3.2",
    "jsonlite": "2.0.0",
    "renv": "1.2.2",
}
SNAPSHOT_REPOSITORY = "https://packagemanager.posit.co/cran/2026-04-23"
EXPECTED_BINARY_ARCHIVES = {
    "Seurat": {
        "version": "5.5.0",
        "file": "Seurat_5.5.0.zip",
        "size": 3_088_125,
        "sha256": "2f062fe275f4229954c2d719146db17c05818a76ce0bbcfdf31d30de8b4ffb6b",
    },
    "renv": {
        "version": "1.2.2",
        "file": "renv_1.2.2.zip",
        "size": 2_514_910,
        "sha256": "bcba2170563c65c6d6ed9328e4a624033ad9c5ee4e5bf9132cff7bcc7327cce5",
    },
}
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


class EnvironmentError(RuntimeError):
    """The task-local environment could not be proven frozen."""


def _native_windows_architecture() -> tuple[str, str]:
    """Return the true Windows architecture and its canonical child value."""
    if os.name != "nt":
        raise EnvironmentError("Windows R subprocess architecture gate requires Windows")
    system_info = (ctypes.c_ubyte * 64)()
    ctypes.windll.kernel32.GetNativeSystemInfo(ctypes.byref(system_info))
    architecture_code = int.from_bytes(bytes(system_info[:2]), byteorder="little")
    native_label = {9: "X64", 12: "ARM64", 0: "X86"}.get(architecture_code)
    if native_label is None:
        raise EnvironmentError(f"unsupported Windows native architecture code: {architecture_code}")
    canonical = {"X64": "AMD64", "ARM64": "ARM64", "X86": "x86"}[native_label]
    if canonical != "AMD64":
        raise EnvironmentError(
            f"unsupported Windows native architecture: {native_label}; only AMD64 is validated"
        )
    return native_label, canonical


def _r_subprocess_environment(
    environment: dict[str, str] | None,
) -> tuple[dict[str, str], dict[str, Any]]:
    """Copy an R child environment and restore only its Windows architecture."""
    child = dict(os.environ if environment is None else environment)
    if os.name != "nt":
        return child, {
            "platform": os.name,
            "processor_architecture_restored": False,
            "parent_environment_modified": False,
        }
    native_label, expected = _native_windows_architecture()
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
    observed = child.get("PROCESSOR_ARCHITECTURE")
    restored = observed is None or not observed.strip()
    if not restored:
        normalized = aliases.get(observed.strip().upper())
        if normalized is None or normalized != expected:
            raise EnvironmentError("PROCESSOR_ARCHITECTURE conflicts with the native Windows architecture")
    wow64 = child.get("PROCESSOR_ARCHITEW6432")
    if wow64 and aliases.get(wow64.strip().upper()) != expected:
        raise EnvironmentError("PROCESSOR_ARCHITEW6432 conflicts with the native Windows architecture")
    child["PROCESSOR_ARCHITECTURE"] = expected
    return child, {
        "platform": "windows",
        "native_architecture": native_label,
        "processor_architecture": expected,
        "processor_architecture_restored": restored,
        "supported_architecture": "AMD64",
        "parent_environment_modified": False,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    os.replace(temporary, path)


def _absolute_directory(path: Path, label: str) -> Path:
    if not path.is_absolute():
        raise EnvironmentError(f"{label} must be an absolute path")
    resolved = path.resolve()
    resolved.mkdir(parents=True, exist_ok=True)
    if not resolved.is_dir():
        raise EnvironmentError(f"{label} is not a directory")
    return resolved


def _short_windows_path(path: Path) -> str:
    """Return a real 8.3 alias and fail if Windows cannot provide one."""
    if os.name != "nt":
        return str(path)
    get_short = ctypes.windll.kernel32.GetShortPathNameW
    get_short.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
    get_short.restype = ctypes.c_uint
    required = get_short(str(path), None, 0)
    if required == 0:
        raise EnvironmentError(f"GetShortPathNameW failed for task-local path (winerror={ctypes.get_last_error()})")
    buffer = ctypes.create_unicode_buffer(required + 1)
    written = get_short(str(path), buffer, len(buffer))
    if written == 0 or written >= len(buffer):
        raise EnvironmentError("GetShortPathNameW could not materialize a safe compiler path")
    short = buffer.value
    if len(short) >= len(str(path)) and len(str(path)) > 96:
        raise EnvironmentError("Windows 8.3 alias is unavailable; refusing long-path Seurat compilation")
    return short


def _read_evidence(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip() or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def _decode_output(value: bytes | None) -> str:
    if value is None:
        return ""
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        return value.decode("utf-8", errors="replace")


def _forbidden_r_process_matches(stdout: str, stderr: str) -> list[dict[str, str]]:
    matches: list[dict[str, str]] = []
    for stream_name, value in (("stdout", stdout), ("stderr", stderr)):
        lowered = value.lower()
        for pattern in FORBIDDEN_R_PROCESS_PATTERNS:
            if pattern in lowered:
                matches.append({"stream": stream_name, "pattern": pattern})
    return matches


def _write_r_process_evidence(
    *,
    path: Path,
    stage: str,
    command: list[str],
    returncode: int,
    stdout_path: Path,
    stderr_path: Path,
    stdout: str,
    stderr: str,
    architecture: dict[str, Any],
) -> dict[str, Any]:
    matches = _forbidden_r_process_matches(stdout, stderr)
    passed = returncode == 0 and not matches
    run_root = path.parent.parent
    evidence = {
        "schema_version": "1.0.0",
        "case_id": "pbmc3k",
        "stage": stage,
        "status": "pass" if passed else "fail",
        "shutdown_mode": "native_exit",
        "returncode": returncode,
        "command_fingerprint_sha256": hashlib.sha256("\0".join(command).encode("utf-8")).hexdigest(),
        "stdout": {
            "path": stdout_path.relative_to(run_root).as_posix(),
            "size_bytes": stdout_path.stat().st_size,
            "sha256": _sha256(stdout_path),
        },
        "stderr": {
            "path": stderr_path.relative_to(run_root).as_posix(),
            "size_bytes": stderr_path.stat().st_size,
            "sha256": _sha256(stderr_path),
        },
        "forbidden_scan": {
            "patterns": list(FORBIDDEN_R_PROCESS_PATTERNS),
            "matches": matches,
            "passed": not matches,
        },
        "architecture": architecture,
    }
    _atomic_json(path, evidence)
    return evidence


def _validate_r_process_evidence(
    run_root: Path,
    relative_path: str,
    *,
    stage: str,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    evidence_path = (run_root / relative_path).resolve()
    try:
        evidence_path.relative_to(run_root.resolve())
    except ValueError as exc:
        raise EnvironmentError("R_PROCESS_EVIDENCE_PATH_ESCAPE") from exc
    if not evidence_path.is_file():
        raise EnvironmentError("R_PROCESS_EVIDENCE_MISSING")
    if expected_sha256 is not None and (
        len(expected_sha256) != 64
        or any(character not in "0123456789abcdef" for character in expected_sha256)
        or _sha256(evidence_path) != expected_sha256
    ):
        raise EnvironmentError("R_PROCESS_EVIDENCE_HASH_MISMATCH")
    try:
        evidence = json.loads(evidence_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EnvironmentError("R_PROCESS_EVIDENCE_INVALID") from exc
    architecture = evidence.get("architecture")
    forbidden_scan = evidence.get("forbidden_scan")
    command_hash = str(evidence.get("command_fingerprint_sha256", ""))
    if (
        evidence.get("case_id") != "pbmc3k"
        or evidence.get("stage") != stage
        or evidence.get("status") != "pass"
        or evidence.get("shutdown_mode") != "native_exit"
        or evidence.get("returncode") != 0
        or not isinstance(architecture, dict)
        or architecture.get("parent_environment_modified") is not False
        or not isinstance(forbidden_scan, dict)
        or forbidden_scan.get("patterns") != list(FORBIDDEN_R_PROCESS_PATTERNS)
        or forbidden_scan.get("matches") != []
        or forbidden_scan.get("passed") is not True
        or len(command_hash) != 64
        or any(character not in "0123456789abcdef" for character in command_hash)
    ):
        raise EnvironmentError("R_PROCESS_EVIDENCE_INVALID")
    if os.name == "nt" and (
        architecture.get("platform") != "windows"
        or architecture.get("native_architecture") != "X64"
        or architecture.get("processor_architecture") != "AMD64"
        or architecture.get("supported_architecture") != "AMD64"
    ):
        raise EnvironmentError("R_PROCESS_ARCHITECTURE_EVIDENCE_INVALID")
    for stream in ("stdout", "stderr"):
        record = evidence.get(stream)
        if not isinstance(record, dict):
            raise EnvironmentError("R_PROCESS_STREAM_EVIDENCE_INVALID")
        log_path = (run_root / str(record.get("path", ""))).resolve()
        try:
            log_path.relative_to(run_root.resolve())
        except ValueError as exc:
            raise EnvironmentError("R_PROCESS_LOG_PATH_ESCAPE") from exc
        if (
            not log_path.is_file()
            or record.get("size_bytes") != log_path.stat().st_size
            or record.get("sha256") != _sha256(log_path)
        ):
            raise EnvironmentError("R_PROCESS_LOG_EVIDENCE_MISMATCH")
    return evidence


def _read_description(path: Path) -> dict[str, str]:
    fields: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        raise EnvironmentError(f"package DESCRIPTION is unreadable: {path.name}") from exc
    for line in lines:
        if ":" in line and not line[:1].isspace():
            key, value = line.split(":", 1)
            fields[key] = value.strip()
    return fields


def _library_description_evidence(library: Path) -> dict[str, dict[str, Any]]:
    evidence: dict[str, dict[str, Any]] = {}
    for package, expected_version in EXPECTED_PACKAGES.items():
        description = library / package / "DESCRIPTION"
        fields = _read_description(description)
        if fields.get("Package") != package or fields.get("Version") != expected_version:
            raise EnvironmentError(f"task-local package DESCRIPTION mismatch: {package}")
        evidence[package] = {
            "path": f"{package}/DESCRIPTION",
            "version": expected_version,
            "sha256": _sha256(description),
        }
    return evidence


def _validate_lock(path: Path) -> dict[str, Any]:
    try:
        lock = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EnvironmentError(f"invalid renv.lock: {exc}") from exc
    packages = lock.get("Packages")
    if not isinstance(packages, dict):
        raise EnvironmentError("renv.lock has no Packages object")
    mismatches = []
    for package, expected in EXPECTED_PACKAGES.items():
        observed = packages.get(package, {}).get("Version")
        if observed != expected:
            mismatches.append(f"{package}: expected {expected}, observed {observed}")
    if mismatches:
        raise EnvironmentError("renv.lock version mismatch: " + "; ".join(mismatches))
    r_contract = lock.get("R")
    repositories = r_contract.get("Repositories") if isinstance(r_contract, dict) else None
    if (
        not isinstance(r_contract, dict)
        or r_contract.get("Version") != "4.5.3"
        or repositories != [{"Name": "CRAN", "URL": SNAPSHOT_REPOSITORY}]
    ):
        raise EnvironmentError("renv.lock repository mismatch: reviewed snapshot is not frozen in R.Repositories")
    return lock


def _reuse_frozen_environment(
    *,
    environment_dir: Path,
    logs_dir: Path,
    cache_key: str,
    relative_environment: Path,
    library: Path,
) -> dict[str, Any] | None:
    lock_path = environment_dir / "renv.lock"
    marker_path = environment_dir / "environment.locked.json"
    probe_path = environment_dir / "environment.probe"
    completion_path = environment_dir / "provision.complete"
    manifest_path = environment_dir / "environment_manifest.json"
    process_evidence_path = logs_dir / "environment-process-evidence.json"
    candidates = (lock_path, marker_path, probe_path, completion_path, manifest_path, process_evidence_path)
    if not any(path.exists() for path in candidates):
        return None
    missing = [path.name for path in candidates if not path.is_file()]
    if missing:
        raise EnvironmentError("FROZEN_ENVIRONMENT_REUSE_INCOMPLETE: " + ", ".join(sorted(missing)))

    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8-sig"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EnvironmentError("FROZEN_ENVIRONMENT_REUSE_METADATA_INVALID") from exc
    if not isinstance(marker, dict) or not isinstance(manifest, dict):
        raise EnvironmentError("FROZEN_ENVIRONMENT_REUSE_METADATA_INVALID")
    if (
        marker.get("cache_key") != cache_key
        or marker.get("lock_hash") != cache_key
        or marker.get("library_relative_to_cache") != (relative_environment / "l").as_posix()
        or marker.get("packages") != EXPECTED_PACKAGES
        or marker.get("verified") is not True
        or marker.get("frozen") is not True
        or marker.get("global_library_modified") is not False
    ):
        raise EnvironmentError("FROZEN_ENVIRONMENT_REUSE_MARKER_MISMATCH")

    backend_lock = marker.get("backend_lock")
    if (
        not isinstance(backend_lock, dict)
        or backend_lock.get("path") != "renv.lock"
        or backend_lock.get("sha256") != _sha256(lock_path)
    ):
        raise EnvironmentError("FROZEN_ENVIRONMENT_REUSE_LOCK_HASH_MISMATCH")
    lock = _validate_lock(lock_path)
    baa = lock.get("BAA")
    if not isinstance(baa, dict) or baa.get("RepositorySnapshot") != SNAPSHOT_REPOSITORY:
        raise EnvironmentError("FROZEN_ENVIRONMENT_REUSE_LOCK_PROVENANCE_MISMATCH")
    for package in ("Seurat", "renv"):
        archive = baa.get("Archives", {}).get(package, {})
        expected = EXPECTED_BINARY_ARCHIVES[package]
        if archive != {
            "Version": expected["version"],
            "File": expected["file"],
            "SHA256": expected["sha256"],
            "Size": expected["size"],
        }:
            raise EnvironmentError(f"FROZEN_ENVIRONMENT_REUSE_ARCHIVE_MISMATCH: {package}")

    probe = _read_evidence(probe_path)
    if (
        probe.get("r_version") != "4.5.3"
        or probe.get("repository_snapshot") != SNAPSHOT_REPOSITORY
        or probe.get("package_type") != "binary"
        or probe.get("bootstrap_renv_version") != "1.2.2"
        or probe.get("bootstrap_source") != "task-local-verified-binary"
        or probe.get("host_renv_required") != "false"
        or probe.get("host_renv_namespace_preloaded") != "false"
    ):
        raise EnvironmentError("FROZEN_ENVIRONMENT_REUSE_PROBE_MISMATCH")
    for package, version in EXPECTED_PACKAGES.items():
        if probe.get(package) != version:
            raise EnvironmentError(f"FROZEN_ENVIRONMENT_REUSE_PACKAGE_PROBE_MISMATCH: {package}")

    native_exit = marker.get("native_exit")
    if (
        not isinstance(native_exit, dict)
        or native_exit.get("required") is not True
        or native_exit.get("mode") != "native_exit"
    ):
        raise EnvironmentError("FROZEN_ENVIRONMENT_REUSE_NATIVE_EXIT_EVIDENCE_MISSING")
    completion_record = native_exit.get("completion_marker")
    probe_record = native_exit.get("probe")
    process_record = native_exit.get("process_evidence")
    if not all(isinstance(item, dict) for item in (completion_record, probe_record, process_record)):
        raise EnvironmentError("FROZEN_ENVIRONMENT_REUSE_COMPLETION_METADATA_INVALID")
    if (
        completion_record.get("path") != "provision.complete"
        or completion_record.get("sha256") != _sha256(completion_path)
        or probe_record.get("path") != "environment.probe"
        or probe_record.get("sha256") != _sha256(probe_path)
    ):
        raise EnvironmentError("FROZEN_ENVIRONMENT_REUSE_COMPLETION_HASH_MISMATCH")
    process_evidence = _validate_r_process_evidence(
        environment_dir.parent,
        str(process_record.get("path", "")),
        stage="environment-provision",
        expected_sha256=str(process_record.get("sha256", "")),
    )
    completion = _read_evidence(completion_path)
    expected_completion = {
        "stage": "environment-provision",
        "status": "complete",
        "shutdown_mode": "native_exit",
        "lock_sha256": _sha256(lock_path),
        "probe_sha256": _sha256(probe_path),
    }
    if completion != expected_completion:
        raise EnvironmentError("FROZEN_ENVIRONMENT_REUSE_COMPLETION_CONTENT_MISMATCH")
    if any(completion_record.get(key) != value for key, value in expected_completion.items()):
        raise EnvironmentError("FROZEN_ENVIRONMENT_REUSE_COMPLETION_METADATA_MISMATCH")

    descriptions = _library_description_evidence(library)
    if marker.get("library_descriptions") != descriptions:
        raise EnvironmentError("FROZEN_ENVIRONMENT_REUSE_LIBRARY_DESCRIPTION_HASH_MISMATCH")
    bootstrap = marker.get("renv_bootstrap")
    if not isinstance(bootstrap, dict) or bootstrap.get("host_package_required") is not False:
        raise EnvironmentError("FROZEN_ENVIRONMENT_REUSE_BOOTSTRAP_METADATA_INVALID")
    bootstrap_library = (environment_dir / str(bootstrap.get("library_relative_to_run", ""))).resolve()
    try:
        bootstrap_library.relative_to(environment_dir.resolve())
    except ValueError as exc:
        raise EnvironmentError("FROZEN_ENVIRONMENT_REUSE_BOOTSTRAP_PATH_ESCAPE") from exc
    bootstrap_description = bootstrap_library / "renv/DESCRIPTION"
    bootstrap_fields = _read_description(bootstrap_description)
    if (
        bootstrap_fields.get("Package") != "renv"
        or bootstrap_fields.get("Version") != "1.2.2"
        or bootstrap.get("description_sha256") != _sha256(bootstrap_description)
    ):
        raise EnvironmentError("FROZEN_ENVIRONMENT_REUSE_BOOTSTRAP_DESCRIPTION_MISMATCH")
    if (
        manifest.get("cache_key") != cache_key
        or manifest.get("backend_lock") != backend_lock
        or manifest.get("packages") != EXPECTED_PACKAGES
        or manifest.get("library_descriptions") != descriptions
        or manifest.get("renv_bootstrap") != bootstrap
        or manifest.get("native_exit") != native_exit
        or manifest.get("task_local_only") is not True
        or manifest.get("global_changes") is not False
    ):
        raise EnvironmentError("FROZEN_ENVIRONMENT_REUSE_MANIFEST_MISMATCH")

    reuse_record = {
        "schema_version": "1.0.0",
        "reuse": True,
        "cache_key": cache_key,
        "lock_sha256": _sha256(lock_path),
        "probe_sha256": _sha256(probe_path),
        "completion_sha256": _sha256(completion_path),
        "process_evidence_sha256": _sha256(process_evidence_path),
        "process_command_sha256": process_evidence["command_fingerprint_sha256"],
        "shutdown_mode": "native_exit",
        "library_description_sha256": {
            package: item["sha256"] for package, item in sorted(descriptions.items())
        },
        "bootstrap_description_sha256": _sha256(bootstrap_description),
        "host_package_required": False,
    }
    _atomic_json(logs_dir / "environment-cache-reuse.json", reuse_record)
    return {**manifest, "cache_reuse": reuse_record}


def prepare(args: argparse.Namespace) -> int:
    if args.authorized is not True:
        raise EnvironmentError("ENVIRONMENT_NOT_AUTHORIZED: --authorized is required")
    run_root = _absolute_directory(args.run_root, "run-root")
    cache_root = _absolute_directory(args.cache_root, "cache-root")
    if not args.rscript.is_absolute() or not args.rscript.is_file():
        raise EnvironmentError("rscript must be an existing absolute executable path")
    environment_dir = run_root / "02_environment"
    logs_dir = run_root / "logs"
    environment_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    spec_source = CASE_DIR / "environment-spec.json"
    try:
        spec = json.loads(spec_source.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EnvironmentError("invalid environment-spec.json") from exc
    pinned = spec.get("pinned_binary_archives", {})
    bootstrap_spec = spec.get("manager_bootstrap", {})
    for package, expected_archive in EXPECTED_BINARY_ARCHIVES.items():
        observed_archive = pinned.get(package, {})
        normalized = {
            "version": observed_archive.get("version"),
            "file": observed_archive.get("archive"),
            "size": observed_archive.get("size_bytes"),
            "sha256": observed_archive.get("sha256"),
        }
        if normalized != expected_archive:
            raise EnvironmentError(f"environment spec archive pin mismatch: {package}")
    if (
        bootstrap_spec.get("host_renv_required") is not False
        or bootstrap_spec.get("library_relative_to_run") != "02_environment/bootstrap-library"
        or bootstrap_spec.get("sha256") != EXPECTED_BINARY_ARCHIVES["renv"]["sha256"]
    ):
        raise EnvironmentError("environment spec task-local renv bootstrap contract mismatch")
    spec_target = environment_dir / "environment-spec.json"
    if spec_target.exists() and _sha256(spec_target) != _sha256(spec_source):
        raise EnvironmentError("existing environment specification differs; create a new run root")
    if not spec_target.exists():
        shutil.copy2(spec_source, spec_target)

    cache_key_payload = {
        "spec_sha256": _sha256(spec_source),
        "r_version": "4.5.3",
        "python_platform": platform.platform(),
        "machine": platform.machine(),
    }
    cache_key = hashlib.sha256(
        json.dumps(cache_key_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    # Keep the normalized *long* compiler path below the Windows boundary even
    # if renv/R expands an 8.3 alias. The full 64-char lock hash remains in all
    # evidence; only the physical directory key is shortened to 16 chars.
    relative_environment = Path("e") / cache_key[:16]
    project = cache_root / relative_environment / "p"
    library = cache_root / relative_environment / "l"
    renv_cache = cache_root / "r"
    for path in (project, library, renv_cache):
        path.mkdir(parents=True, exist_ok=True)
    cache_binding = cache_root / relative_environment / "cache-key.json"
    if cache_binding.exists():
        try:
            bound_key = json.loads(cache_binding.read_text(encoding="utf-8-sig")).get("full_cache_key")
        except (OSError, json.JSONDecodeError) as exc:
            raise EnvironmentError("invalid shortened cache-key binding") from exc
        if bound_key != cache_key:
            raise EnvironmentError("shortened cache-key collision; refusing reuse")
    else:
        _atomic_json(cache_binding, {"full_cache_key": cache_key, "directory_key_chars": 16})
    reused = _reuse_frozen_environment(
        environment_dir=environment_dir,
        logs_dir=logs_dir,
        cache_key=cache_key,
        relative_environment=relative_environment,
        library=library,
    )
    if reused is not None:
        print(json.dumps(reused, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    build_tmp = cache_root / relative_environment / "t"
    build_tmp.mkdir(parents=True, exist_ok=True)
    short_project = _short_windows_path(project)
    short_library = _short_windows_path(library)
    short_renv_cache = _short_windows_path(renv_cache)
    short_build_tmp = _short_windows_path(build_tmp)
    short_environment_dir = _short_windows_path(environment_dir)
    bootstrap_library = environment_dir / "bootstrap-library"
    bootstrap_library.mkdir(parents=True, exist_ok=True)
    short_bootstrap_library = _short_windows_path(bootstrap_library)

    temporary_lock = environment_dir / ".renv.lock.provisioning"
    evidence_path = environment_dir / ".environment.probe"
    package_specs = ";".join(f"{name}@{version}" for name, version in EXPECTED_PACKAGES.items())
    expected = ";".join(f"{name}={version}" for name, version in EXPECTED_PACKAGES.items())
    binary_evidence_dir = cache_root / relative_environment / "b"
    binary_evidence_dir.mkdir(parents=True, exist_ok=True)
    short_binary_evidence_dir = _short_windows_path(binary_evidence_dir)
    completion_marker = environment_dir / "provision.complete"
    completion_marker.unlink(missing_ok=True)
    short_completion_marker = str(Path(short_environment_dir) / completion_marker.name)
    command = [
        str(args.rscript),
        "--vanilla",
        str(CASE_DIR / "prepare_environment.R"),
        "--project",
        short_project,
        "--library",
        short_library,
        "--cache",
        short_renv_cache,
        "--lock-out",
        str(Path(short_environment_dir) / temporary_lock.name),
        "--evidence-out",
        str(Path(short_environment_dir) / evidence_path.name),
        "--packages",
        package_specs,
        "--expected",
        expected,
        "--snapshot-repo",
        SNAPSHOT_REPOSITORY,
        "--binary-evidence-dir",
        short_binary_evidence_dir,
        "--bootstrap-library",
        short_bootstrap_library,
        "--seurat-binary-sha256",
        EXPECTED_BINARY_ARCHIVES["Seurat"]["sha256"],
        "--seurat-binary-size",
        str(EXPECTED_BINARY_ARCHIVES["Seurat"]["size"]),
        "--renv-binary-sha256",
        EXPECTED_BINARY_ARCHIVES["renv"]["sha256"],
        "--renv-binary-size",
        str(EXPECTED_BINARY_ARCHIVES["renv"]["size"]),
        "--completion-marker",
        short_completion_marker,
        "--inject-error-before-exit",
        "true" if args.inject_error_before_exit else "false",
    ]
    process_environment = os.environ.copy()
    process_environment["TMP"] = short_build_tmp
    process_environment["TEMP"] = short_build_tmp
    process_environment["TMPDIR"] = short_build_tmp
    process_environment["RENV_PATHS_LIBRARY"] = short_library
    process_environment["RENV_PATHS_CACHE"] = short_renv_cache
    process_environment["R_LIBS_USER"] = short_bootstrap_library
    process_environment["R_LIBS_SITE"] = ""
    process_environment, architecture_evidence = _r_subprocess_environment(process_environment)
    completed = subprocess.run(
        command,
        cwd=short_project,
        env=process_environment,
        capture_output=True,
        text=False,
        check=False,
    )
    replacements = {
        str(run_root): "<RUN_ROOT>",
        str(run_root).replace("\\", "/"): "<RUN_ROOT>",
        str(cache_root): "<CACHE_ROOT>",
        str(cache_root).replace("\\", "/"): "<CACHE_ROOT>",
        short_project: "<SHORT_PROJECT>",
        short_project.replace("\\", "/"): "<SHORT_PROJECT>",
        short_library: "<SHORT_LIBRARY>",
        short_library.replace("\\", "/"): "<SHORT_LIBRARY>",
        short_renv_cache: "<SHORT_RENV_CACHE>",
        short_renv_cache.replace("\\", "/"): "<SHORT_RENV_CACHE>",
        short_build_tmp: "<SHORT_BUILD_TMP>",
        short_build_tmp.replace("\\", "/"): "<SHORT_BUILD_TMP>",
        short_binary_evidence_dir: "<SHORT_BINARY_CACHE>",
        short_binary_evidence_dir.replace("\\", "/"): "<SHORT_BINARY_CACHE>",
        short_bootstrap_library: "<BOOTSTRAP_LIBRARY>",
        short_bootstrap_library.replace("\\", "/"): "<BOOTSTRAP_LIBRARY>",
        str(CASE_DIR.parent.parent): "<REPO_ROOT>",
        str(CASE_DIR.parent.parent).replace("\\", "/"): "<REPO_ROOT>",
    }
    stdout, stderr = _decode_output(completed.stdout), _decode_output(completed.stderr)
    for source, token in replacements.items():
        stdout, stderr = stdout.replace(source, token), stderr.replace(source, token)
    stdout_path = logs_dir / "environment.stdout.log"
    stderr_path = logs_dir / "environment.stderr.log"
    stdout_path.write_text(stdout, encoding="utf-8", newline="\n")
    stderr_path.write_text(stderr, encoding="utf-8", newline="\n")
    process_evidence_path = logs_dir / "environment-process-evidence.json"
    process_evidence = _write_r_process_evidence(
        path=process_evidence_path,
        stage="environment-provision",
        command=command,
        returncode=completed.returncode,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        stdout=stdout,
        stderr=stderr,
        architecture=architecture_evidence,
    )
    if process_evidence["status"] != "pass":
        if args.inject_error_before_exit:
            negative_test = {
                "schema_version": "1.0.0",
                "test": "error-before-native-exit",
                "requested": True,
                "returncode": completed.returncode,
                "shutdown_mode": "native_exit",
                "completion_marker_exists": completion_marker.exists(),
                "sentinel_observed": "INJECTED_ERROR_BEFORE_NATIVE_EXIT" in stderr,
                "process_evidence_sha256": _sha256(process_evidence_path),
                "status": "pass"
                if completed.returncode != 0
                and not completion_marker.exists()
                and "INJECTED_ERROR_BEFORE_NATIVE_EXIT" in stderr
                else "fail",
            }
            _atomic_json(logs_dir / "environment-negative-test.json", negative_test)
            if negative_test["status"] != "pass":
                raise EnvironmentError("injected pre-native-exit error did not fail closed")
        if completed.returncode != 0:
            raise EnvironmentError(f"task-local renv provisioning failed with exit code {completed.returncode}")
        raise EnvironmentError("task-local renv provisioning emitted a forbidden stdout/stderr pattern")
    if args.inject_error_before_exit:
        raise EnvironmentError("injected pre-native-exit error unexpectedly returned zero")
    if not completion_marker.is_file():
        raise EnvironmentError("NATIVE_EXIT_COMPLETION_MARKER_MISSING")
    if not temporary_lock.is_file() or not evidence_path.is_file():
        raise EnvironmentError("environment provisioner did not produce lock/evidence")

    completion = _read_evidence(completion_marker)
    expected_completion = {
        "stage": "environment-provision",
        "status": "complete",
        "shutdown_mode": "native_exit",
        "lock_sha256": _sha256(temporary_lock),
        "probe_sha256": _sha256(evidence_path),
    }
    completion_mismatches = [
        f"{key}: expected {expected}, observed {completion.get(key)}"
        for key, expected in expected_completion.items()
        if completion.get(key) != expected
    ]
    if completion_mismatches:
        raise EnvironmentError("NATIVE_EXIT_COMPLETION_EVIDENCE_MISMATCH: " + "; ".join(completion_mismatches))

    lock = _validate_lock(temporary_lock)
    evidence = _read_evidence(evidence_path)
    if evidence.get("r_version") != "4.5.3":
        raise EnvironmentError(f"R version mismatch: {evidence.get('r_version')}")
    for package, expected_version in EXPECTED_PACKAGES.items():
        if evidence.get(package) != expected_version:
            raise EnvironmentError(f"runtime package mismatch for {package}: {evidence.get(package)}")
    if evidence.get("repository_snapshot") != SNAPSHOT_REPOSITORY or evidence.get("package_type") != "binary":
        raise EnvironmentError("repository snapshot/type evidence mismatch")
    if (
        evidence.get("bootstrap_renv_version") != "1.2.2"
        or evidence.get("bootstrap_source") != "task-local-verified-binary"
        or evidence.get("host_renv_required") != "false"
        or evidence.get("host_renv_namespace_preloaded") != "false"
    ):
        raise EnvironmentError("task-local renv bootstrap probe mismatch")
    archive_evidence: dict[str, dict[str, Any]] = {}
    for package, prefix in (("Seurat", "seurat"), ("renv", "renv")):
        archive_name = str(evidence.get(f"{prefix}_binary_file", ""))
        expected_archive = EXPECTED_BINARY_ARCHIVES[package]
        if not archive_name or Path(archive_name).name != archive_name:
            raise EnvironmentError(f"{package} binary archive probe must contain a basename only")
        archive_path = binary_evidence_dir / archive_name
        if not archive_path.is_file():
            raise EnvironmentError(f"{package} binary archive evidence is missing")
        observed_archive = {
            "version": expected_archive["version"],
            "file": archive_path.name,
            "size": archive_path.stat().st_size,
            "sha256": _sha256(archive_path),
        }
        if observed_archive != expected_archive:
            raise EnvironmentError(f"{package} binary archive pin mismatch")
        if (
            evidence.get(f"{prefix}_binary_sha256") != observed_archive["sha256"]
            or evidence.get(f"{prefix}_binary_size") != str(observed_archive["size"])
        ):
            raise EnvironmentError(f"{package} binary archive probe mismatch")
        archive_evidence[package] = {**observed_archive, "path": archive_path}
    binary_path = archive_evidence["Seurat"]["path"]
    binary_sha256 = archive_evidence["Seurat"]["sha256"]
    binary_size = archive_evidence["Seurat"]["size"]
    baa = lock.get("BAA")
    seurat_archive = baa.get("Archives", {}).get("Seurat", {}) if isinstance(baa, dict) else {}
    renv_archive = baa.get("Archives", {}).get("renv", {}) if isinstance(baa, dict) else {}
    bootstrap = baa.get("Bootstrap", {}) if isinstance(baa, dict) else {}
    expected_archive = {
        "Version": "5.5.0",
        "File": binary_path.name,
        "SHA256": binary_sha256,
        "Size": binary_size,
    }
    expected_renv_archive = {
        "Version": "1.2.2",
        "File": archive_evidence["renv"]["file"],
        "SHA256": archive_evidence["renv"]["sha256"],
        "Size": archive_evidence["renv"]["size"],
    }
    expected_bootstrap = {
        "HostRenvRequired": False,
        "Library": "02_environment/bootstrap-library",
        **expected_renv_archive,
    }
    if (
        not isinstance(baa, dict)
        or baa.get("RepositorySnapshot") != SNAPSHOT_REPOSITORY
        or baa.get("PackageType") != "win.binary"
        or seurat_archive != expected_archive
        or renv_archive != expected_renv_archive
        or bootstrap != expected_bootstrap
    ):
        raise EnvironmentError("renv.lock BAA binary provenance mismatch")

    lock_path = environment_dir / "renv.lock"
    if lock_path.exists():
        if _sha256(lock_path) != _sha256(temporary_lock):
            raise EnvironmentError("existing immutable renv.lock differs; create a new run root")
        temporary_lock.unlink()
    else:
        os.replace(temporary_lock, lock_path)
    probe_path = environment_dir / "environment.probe"
    if probe_path.exists():
        if _sha256(probe_path) != completion["probe_sha256"]:
            raise EnvironmentError("existing immutable environment probe differs; create a new run root")
        evidence_path.unlink()
    else:
        os.replace(evidence_path, probe_path)
    lock_sha256 = _sha256(lock_path)
    library_descriptions = _library_description_evidence(library)
    bootstrap_description = bootstrap_library / "renv/DESCRIPTION"
    bootstrap_fields = _read_description(bootstrap_description)
    if bootstrap_fields.get("Package") != "renv" or bootstrap_fields.get("Version") != "1.2.2":
        raise EnvironmentError("task-local renv bootstrap DESCRIPTION mismatch")
    marker = {
        "schema_version": "1.0.0",
        "backend": "renv",
        "runtime": "r",
        "r_version": "4.5.3",
        "platform": evidence.get("platform"),
        "verified": True,
        "frozen": True,
        "lock_hash": cache_key,
        "backend_lock": {"path": "renv.lock", "sha256": lock_sha256},
        "cache_key": cache_key,
        "cache_directory_key_chars": 16,
        "library_relative_to_cache": (relative_environment / "l").as_posix(),
        "renv_cache_relative_to_cache": "r",
        "build_path_strategy": "windows-8dot3",
        "long_library_path_sha256": hashlib.sha256(str(library).encode("utf-8")).hexdigest(),
        "short_library_path_sha256": hashlib.sha256(short_library.encode("utf-8")).hexdigest(),
        "packages": EXPECTED_PACKAGES,
        "library_descriptions": library_descriptions,
        "repository_snapshot": SNAPSHOT_REPOSITORY,
        "package_type": "win.binary",
        "seurat_binary": {
            "file": binary_path.name,
            "version": "5.5.0",
            "sha256": binary_sha256,
            "size_bytes": binary_size,
        },
        "renv_bootstrap": {
            "host_package_required": False,
            "library_relative_to_run": "bootstrap-library",
            "version": "1.2.2",
            "archive": archive_evidence["renv"]["file"],
            "archive_sha256": archive_evidence["renv"]["sha256"],
            "archive_size_bytes": archive_evidence["renv"]["size"],
            "description_sha256": _sha256(bootstrap_description),
        },
        "native_exit": {
            "required": True,
            "mode": "native_exit",
            "completion_marker": {
                "path": "provision.complete",
                "sha256": _sha256(completion_marker),
                **completion,
            },
            "probe": {
                "path": "environment.probe",
                "sha256": _sha256(probe_path),
            },
            "process_evidence": {
                "path": "logs/environment-process-evidence.json",
                "sha256": _sha256(process_evidence_path),
            },
        },
        "global_library_modified": False,
        "fallback_permitted": False,
    }
    _atomic_json(environment_dir / "environment.locked.json", marker)
    manifest = {
        "schema_version": "1.0.0",
        "case_id": "pbmc3k",
        "state": "frozen",
        "manager": "renv",
        "r_version": "4.5.3",
        "packages": EXPECTED_PACKAGES,
        "library_descriptions": library_descriptions,
        "repository_snapshot": SNAPSHOT_REPOSITORY,
        "package_type": "win.binary",
        "seurat_binary_sha256": binary_sha256,
        "renv_bootstrap": marker["renv_bootstrap"],
        "native_exit": marker["native_exit"],
        "backend_lock": marker["backend_lock"],
        "cache_key": cache_key,
        "task_local_only": True,
        "global_changes": False,
    }
    _atomic_json(environment_dir / "environment_manifest.json", manifest)
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--cache-root", type=Path, required=True)
    parser.add_argument("--rscript", type=Path, required=True)
    parser.add_argument("--authorized", action="store_true")
    parser.add_argument(
        "--inject-error-before-exit",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        return prepare(build_parser().parse_args(argv))
    except (EnvironmentError, OSError, subprocess.SubprocessError) as exc:
        print(f"PBMC3K_ENVIRONMENT_FAILED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
