#!/usr/bin/env python3
"""Task-local PBMC3K case driver used by the repository tutorial CLI.

The root CLI performs user-facing authorization.  This driver deliberately
requires a second ``--authorized`` flag for run/resume so an accidental direct
invocation cannot download or execute anything.
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from download_inputs import InputError, prepare_input


CASE_DIR = Path(__file__).resolve().parent
CASE_ID = "pbmc3k"
EXPECTED = {"input_cells": 2700, "qc_retained_cells": 2638, "clusters": 9}
EXPECTED_R_PACKAGES = {
    "Seurat": "5.5.0",
    "SeuratObject": "5.4.0",
    "ggplot2": "4.0.3",
    "patchwork": "1.3.2",
    "jsonlite": "2.0.0",
    "renv": "1.2.2",
}
STAGES = (
    "SC01_IMPORT_AND_IDENTITY",
    "SC04_QC_PER_CAPTURE",
    "SC06_NORMALIZE_AND_HVG_PER_SAMPLE",
    "SC08_GRAPH_CLUSTER_AND_EMBED",
    "SC09_ANNOTATE_AND_REVIEW",
    "SC13_FIGURES_AND_INTERPRETATION",
)
FIGURES = (
    "qc_violin",
    "pca_clusters",
    "umap_clusters",
    "umap_annotation",
    "marker_dotplot",
)
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
RUN_DIRS = (
    "00_request",
    "01_plan",
    "02_environment",
    "03_scripts/modules",
    "04_intermediate",
    "05_results/tables",
    "05_results/objects",
    "06_figures/original",
    "06_figures/final",
    "06_figures/review",
    "07_reports",
    "logs",
    "manifest",
    "_staging",
)


class CaseError(RuntimeError):
    """A reproducibility or validation gate failed."""


def _native_windows_architecture() -> tuple[str, str]:
    """Return the true Windows architecture and its canonical child value."""
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
            raise CaseError("PROCESSOR_ARCHITECTURE conflicts with the native Windows architecture")
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


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _stable_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise CaseError(f"invalid JSON artifact: {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise CaseError(f"expected JSON object: {path.name}")
    return value


def _read_key_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        raise CaseError(f"unable to read completion evidence: {path.name}") from exc
    for line in lines:
        if not line.strip() or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(value, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def _atomic_json(path: Path, value: Any) -> None:
    _atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _copy_frozen(source: Path, target: Path, *, allow_reviewed_existing: bool = False) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if allow_reviewed_existing:
            if not target.is_file() or target.stat().st_size == 0:
                raise CaseError(f"existing run artifact is not a non-empty file: {target.name}")
            return
        if not target.is_file() or _sha256(target) != _sha256(source):
            raise CaseError(f"frozen run artifact differs from teaching source: {target.name}")
        return
    temporary = target.with_name(f".{target.name}.tmp")
    shutil.copy2(source, temporary)
    os.replace(temporary, target)


def _assert_absolute_directory(path: Path, label: str, *, create: bool = False) -> Path:
    if not path.is_absolute():
        raise CaseError(f"{label} must be an absolute path")
    resolved = path.resolve()
    if create:
        resolved.mkdir(parents=True, exist_ok=True)
    if not resolved.is_dir():
        raise CaseError(f"{label} is not a directory: {resolved}")
    return resolved


def _seed_run_tree(run_root: Path) -> None:
    for relative in RUN_DIRS:
        (run_root / relative).mkdir(parents=True, exist_ok=True)
    # A list is used because two source files seed
    # both JSON and JSON-compatible YAML targets.
    copies = (
        (CASE_DIR / "request.json", run_root / "00_request/request.json", True),
        (CASE_DIR / "input_manifest.json", run_root / "00_request/input_manifest.json", False),
        (CASE_DIR / "ANALYSIS_DESIGN.md", run_root / "01_plan/ANALYSIS_DESIGN.md", False),
        (CASE_DIR / "workflow.plan.expected.json", run_root / "01_plan/workflow.plan.json", True),
        (CASE_DIR / "workflow.plan.expected.json", run_root / "01_plan/workflow.plan.yaml", True),
        (CASE_DIR / "environment-spec.json", run_root / "02_environment/environment-spec.json", False),
        (CASE_DIR / "params.json", run_root / "03_scripts/params.json", False),
        (CASE_DIR / "params.json", run_root / "03_scripts/params.yaml", False),
        (CASE_DIR / "run_pipeline.R", run_root / "03_scripts/run_pipeline.R", False),
    )
    for source, target, allow_existing in copies:
        _copy_frozen(source, target, allow_reviewed_existing=allow_existing)
    intent = {
        "schema_version": "1.0.0",
        "case_id": CASE_ID,
        "prompt_source": "examples/pbmc3k/PROMPT.md",
        "mode": "run",
        "authorization": "recorded separately by root tutorial CLI",
        "analysis_scope": "descriptive-only",
    }
    intent_path = run_root / "00_request/intent.yaml"
    if not intent_path.exists():
        _atomic_json(intent_path, intent)


def _analysis_signature(run_root: Path) -> str:
    manifest = _read_json(run_root / "00_request/input_manifest.json")
    item = manifest.get("inputs", [{}])[0]
    signature = _stable_hash(
        {
            "case_id": CASE_ID,
            "input_sha256": item.get("sha256"),
            "params_sha256": _sha256(run_root / "03_scripts/params.json"),
            "pipeline_sha256": _sha256(run_root / "03_scripts/run_pipeline.R"),
            "environment_spec_sha256": _sha256(run_root / "02_environment/environment-spec.json"),
        }
    )
    signature_path = run_root / "03_scripts/analysis_signature.txt"
    if signature_path.exists():
        observed = signature_path.read_text(encoding="utf-8").strip()
        if observed != signature:
            raise CaseError("ANALYSIS_SIGNATURE_MISMATCH: create a new run root")
    else:
        _atomic_text(signature_path, signature + "\n")
    return signature


def _validate_task_local_bootstrap(environment_dir: Path, marker: dict[str, Any]) -> dict[str, str]:
    bootstrap = marker.get("renv_bootstrap")
    if not isinstance(bootstrap, dict):
        raise CaseError("TASK_LOCAL_RENV_BOOTSTRAP_EVIDENCE_MISSING")
    if (
        bootstrap.get("host_package_required") is not False
        or bootstrap.get("version") != "1.2.2"
        or bootstrap.get("archive") != "renv_1.2.2.zip"
        or bootstrap.get("archive_sha256")
        != "bcba2170563c65c6d6ed9328e4a624033ad9c5ee4e5bf9132cff7bcc7327cce5"
        or bootstrap.get("archive_size_bytes") != 2_514_910
    ):
        raise CaseError("TASK_LOCAL_RENV_BOOTSTRAP_EVIDENCE_MISMATCH")
    library = (environment_dir / str(bootstrap.get("library_relative_to_run", ""))).resolve()
    try:
        library.relative_to(environment_dir.resolve())
    except ValueError as exc:
        raise CaseError("TASK_LOCAL_RENV_BOOTSTRAP_PATH_ESCAPE") from exc
    description = library / "renv/DESCRIPTION"
    if not description.is_file():
        raise CaseError("TASK_LOCAL_RENV_BOOTSTRAP_PACKAGE_MISSING")
    fields = {}
    for line in description.read_text(encoding="utf-8-sig").splitlines():
        if ":" in line and not line[:1].isspace():
            key, value = line.split(":", 1)
            fields[key] = value.strip()
    if fields.get("Package") != "renv" or fields.get("Version") != "1.2.2":
        raise CaseError("TASK_LOCAL_RENV_BOOTSTRAP_PACKAGE_MISMATCH")
    if bootstrap.get("description_sha256") != _sha256(description):
        raise CaseError("TASK_LOCAL_RENV_BOOTSTRAP_DESCRIPTION_HASH_MISMATCH")
    return {
        "renv_bootstrap_version": "1.2.2",
        "renv_bootstrap_archive_sha256": str(bootstrap["archive_sha256"]),
        "renv_bootstrap_description_sha256": _sha256(description),
    }


def _validate_environment_lock(run_root: Path, cache_root: Path) -> dict[str, Any]:
    environment_dir = run_root / "02_environment"
    lock_path = environment_dir / "renv.lock"
    marker_path = environment_dir / "environment.locked.json"
    if not lock_path.is_file() or not marker_path.is_file():
        raise CaseError(
            "ENVIRONMENT_NOT_FROZEN: 02_environment/renv.lock and "
            "02_environment/environment.locked.json are required before execution"
        )
    marker = _read_json(marker_path)
    bootstrap_evidence = _validate_task_local_bootstrap(environment_dir, marker)
    backend_lock = marker.get("backend_lock")
    if not isinstance(backend_lock, dict):
        raise CaseError("ENVIRONMENT_LOCK_INVALID: backend_lock evidence is missing")
    relative_path = str(backend_lock.get("path", ""))
    expected_hash = str(backend_lock.get("sha256", ""))
    if not relative_path or not expected_hash:
        raise CaseError("ENVIRONMENT_LOCK_INVALID: backend lock path/hash missing")
    referenced = (environment_dir / relative_path).resolve()
    try:
        referenced.relative_to(environment_dir.resolve())
    except ValueError as exc:
        raise CaseError("ENVIRONMENT_LOCK_INVALID: lock path escapes run environment directory") from exc
    if referenced != lock_path.resolve() or _sha256(referenced) != expected_hash:
        raise CaseError("ENVIRONMENT_LOCK_HASH_MISMATCH")
    if marker.get("frozen") is False or marker.get("verified") is False:
        raise CaseError("ENVIRONMENT_NOT_VERIFIED")
    native_exit = marker.get("native_exit")
    if (
        not isinstance(native_exit, dict)
        or native_exit.get("required") is not True
        or native_exit.get("mode") != "native_exit"
    ):
        raise CaseError("NATIVE_EXIT_EVIDENCE_MISSING")
    completion_record = native_exit.get("completion_marker")
    probe_record = native_exit.get("probe")
    process_record = native_exit.get("process_evidence")
    if not all(isinstance(item, dict) for item in (completion_record, probe_record, process_record)):
        raise CaseError("NATIVE_EXIT_COMPLETION_EVIDENCE_INVALID")
    completion_path = (environment_dir / str(completion_record.get("path", ""))).resolve()
    probe_path = (environment_dir / str(probe_record.get("path", ""))).resolve()
    for evidence_path, label in ((completion_path, "completion"), (probe_path, "probe")):
        try:
            evidence_path.relative_to(environment_dir.resolve())
        except ValueError as exc:
            raise CaseError(f"NATIVE_EXIT_{label.upper()}_PATH_ESCAPE") from exc
        if not evidence_path.is_file():
            raise CaseError(f"NATIVE_EXIT_{label.upper()}_MISSING")
    if _sha256(completion_path) != completion_record.get("sha256"):
        raise CaseError("NATIVE_EXIT_COMPLETION_MARKER_HASH_MISMATCH")
    if _sha256(probe_path) != probe_record.get("sha256"):
        raise CaseError("NATIVE_EXIT_PROBE_HASH_MISMATCH")
    process_evidence = _validate_r_process_evidence(
        run_root,
        str(process_record.get("path", "")),
        stage="environment-provision",
        expected_sha256=str(process_record.get("sha256", "")),
    )
    completion_values = _read_key_values(completion_path)
    expected_completion = {
        "stage": "environment-provision",
        "status": "complete",
        "shutdown_mode": "native_exit",
        "lock_sha256": expected_hash,
        "probe_sha256": str(probe_record.get("sha256", "")),
    }
    if completion_values != expected_completion:
        raise CaseError("NATIVE_EXIT_COMPLETION_CONTENT_MISMATCH")
    relative_library = str(marker.get("library_relative_to_cache", ""))
    if not relative_library:
        raise CaseError("ENVIRONMENT_LOCK_INVALID: task-local library locator missing")
    library = (cache_root / relative_library).resolve()
    try:
        library.relative_to(cache_root.resolve())
    except ValueError as exc:
        raise CaseError("ENVIRONMENT_LOCK_INVALID: library path escapes cache root") from exc
    if not library.is_dir():
        raise CaseError("ENVIRONMENT_CACHE_MISS: frozen task-local library is absent")
    description_records = marker.get("library_descriptions")
    if not isinstance(description_records, dict) or set(description_records) != set(EXPECTED_R_PACKAGES):
        raise CaseError("ENVIRONMENT_LIBRARY_DESCRIPTION_EVIDENCE_MISSING")
    for package, version in EXPECTED_R_PACKAGES.items():
        record = description_records.get(package, {})
        description = (library / str(record.get("path", ""))).resolve()
        try:
            description.relative_to(library)
        except ValueError as exc:
            raise CaseError("ENVIRONMENT_LIBRARY_DESCRIPTION_PATH_ESCAPE") from exc
        if (
            record.get("path") != f"{package}/DESCRIPTION"
            or record.get("version") != version
            or not description.is_file()
            or record.get("sha256") != _sha256(description)
        ):
            raise CaseError(f"ENVIRONMENT_LIBRARY_DESCRIPTION_HASH_MISMATCH: {package}")
    return {
        "lock_sha256": expected_hash,
        "lock_hash": marker.get("lock_hash"),
        "platform": marker.get("platform"),
        "library": library,
        "renv_cache": (cache_root / str(marker.get("renv_cache_relative_to_cache", "r"))).resolve(),
        "environment_process_evidence_sha256": _sha256(
            run_root / str(process_record.get("path", ""))
        ),
        "environment_process_command_sha256": process_evidence["command_fingerprint_sha256"],
        "environment_probe_sha256": str(probe_record.get("sha256")),
        "environment_completion_sha256": str(completion_record.get("sha256")),
        **bootstrap_evidence,
    }


def _sanitize(text: str, replacements: Iterable[tuple[Path, str]]) -> str:
    sanitized = text
    for path, token in replacements:
        raw = str(path)
        sanitized = sanitized.replace(raw, token).replace(raw.replace("\\", "/"), token)
    return sanitized


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
    runtime_contract_path: Path | None = None,
) -> dict[str, Any]:
    matches = _forbidden_r_process_matches(stdout, stderr)
    passed = returncode == 0 and not matches
    evidence = {
        "schema_version": "1.0.0",
        "case_id": CASE_ID,
        "stage": stage,
        "status": "pass" if passed else "fail",
        "shutdown_mode": "native_exit",
        "returncode": returncode,
        "command_fingerprint_sha256": hashlib.sha256("\0".join(command).encode("utf-8")).hexdigest(),
        "stdout": {
            "path": stdout_path.relative_to(path.parent.parent).as_posix(),
            "size_bytes": stdout_path.stat().st_size,
            "sha256": _sha256(stdout_path),
        },
        "stderr": {
            "path": stderr_path.relative_to(path.parent.parent).as_posix(),
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
    if runtime_contract_path is not None:
        runtime_contract = _read_json(runtime_contract_path)
        evidence["analysis_runtime_contract"] = {
            "path": runtime_contract_path.relative_to(path.parent.parent).as_posix(),
            "sha256": _sha256(runtime_contract_path),
            "umap_method": runtime_contract.get("umap_method"),
            "metric": runtime_contract.get("metric"),
            "seed_use": runtime_contract.get("seed_use"),
            "option_name": runtime_contract.get("option_name"),
            "option_value_during_call": runtime_contract.get("option_value_during_call"),
            "option_restored": runtime_contract.get("option_restored"),
            "transition_notice_option_applied": runtime_contract.get("transition_notice_option_applied"),
            "suppress_warnings_used": runtime_contract.get("suppress_warnings_used"),
            "handler_muffling_used": runtime_contract.get("handler_muffling_used"),
            "warning_allowlist_used": runtime_contract.get("warning_allowlist_used"),
            "algorithm_changed": runtime_contract.get("algorithm_changed"),
            "r_warn_option": runtime_contract.get("r_warn_option"),
            "warning_delivery": runtime_contract.get("warning_delivery"),
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
        raise CaseError("R_PROCESS_EVIDENCE_PATH_ESCAPE") from exc
    if not evidence_path.is_file():
        raise CaseError("R_PROCESS_EVIDENCE_MISSING")
    if expected_sha256 is not None and (
        len(expected_sha256) != 64
        or any(character not in "0123456789abcdef" for character in expected_sha256)
        or _sha256(evidence_path) != expected_sha256
    ):
        raise CaseError("R_PROCESS_EVIDENCE_HASH_MISMATCH")
    evidence = _read_json(evidence_path)
    architecture = evidence.get("architecture")
    forbidden_scan = evidence.get("forbidden_scan")
    command_hash = str(evidence.get("command_fingerprint_sha256", ""))
    if (
        evidence.get("case_id") != CASE_ID
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
        raise CaseError("R_PROCESS_EVIDENCE_INVALID")
    if os.name == "nt" and (
        architecture.get("platform") != "windows"
        or architecture.get("native_architecture") != "X64"
        or architecture.get("processor_architecture") != "AMD64"
        or architecture.get("supported_architecture") != "AMD64"
    ):
        raise CaseError("R_PROCESS_ARCHITECTURE_EVIDENCE_INVALID")
    for stream in ("stdout", "stderr"):
        record = evidence.get(stream)
        if not isinstance(record, dict):
            raise CaseError("R_PROCESS_STREAM_EVIDENCE_INVALID")
        log_path = (run_root / str(record.get("path", ""))).resolve()
        try:
            log_path.relative_to(run_root.resolve())
        except ValueError as exc:
            raise CaseError("R_PROCESS_LOG_PATH_ESCAPE") from exc
        if (
            not log_path.is_file()
            or record.get("size_bytes") != log_path.stat().st_size
            or record.get("sha256") != _sha256(log_path)
        ):
            raise CaseError("R_PROCESS_LOG_EVIDENCE_MISMATCH")
    if stage == "pbmc3k-r-pipeline":
        runtime_record = evidence.get("analysis_runtime_contract")
        if not isinstance(runtime_record, dict):
            raise CaseError("R_PROCESS_RUNTIME_CONTRACT_EVIDENCE_MISSING")
        runtime_path = run_root / "05_results/tables/umap_runtime_contract.json"
        runtime_contract = _validate_umap_runtime_contract(run_root)
        if (
            runtime_record.get("path") != "05_results/tables/umap_runtime_contract.json"
            or runtime_record.get("sha256") != _sha256(runtime_path)
            or any(
                runtime_record.get(key) != runtime_contract.get(key)
                for key in (
                    "umap_method",
                    "metric",
                    "seed_use",
                    "option_name",
                    "option_value_during_call",
                    "option_restored",
                    "transition_notice_option_applied",
                    "suppress_warnings_used",
                    "handler_muffling_used",
                    "warning_allowlist_used",
                    "algorithm_changed",
                    "r_warn_option",
                    "warning_delivery",
                )
            )
        ):
            raise CaseError("R_PROCESS_RUNTIME_CONTRACT_EVIDENCE_MISMATCH")
    return evidence


def _write_input_evidence(run_root: Path, evidence: dict[str, Any]) -> None:
    archive = evidence["archive"]
    sanitized = {
        "schema_version": "1.0.0",
        "case_id": CASE_ID,
        "archive_name": Path(str(archive["path"])).name,
        "archive_size_bytes": archive["size_bytes"],
        "archive_sha256": archive["sha256"],
        "data_member_root": "filtered_gene_bc_matrices/hg19",
        "license": "CC BY 4.0",
        "remote_upload": False,
    }
    _atomic_json(run_root / "00_request/input_evidence.json", sanitized)


def _png_dimensions(path: Path) -> tuple[int, int]:
    with path.open("rb") as handle:
        header = handle.read(24)
    if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise CaseError(f"invalid PNG artifact: {path.name}")
    return struct.unpack(">II", header[16:24])


def _review_template(figure_id: str, original: Path, final: Path, signature: str) -> dict[str, Any]:
    original_hash = _sha256(original)
    final_hash = _sha256(final)
    original_size = _png_dimensions(original)
    final_size = _png_dimensions(final)
    return {
        "schema_version": "2.0.0",
        "case_id": CASE_ID,
        "figure_id": figure_id,
        "backend": "r",
        "data_sha256": signature,
        "max_rounds": 3,
        "status": "awaiting_native_review",
        "rounds": [
            {
                "round": 1,
                "decision": "pending",
                "evidence_level": "image_code_data",
                "original": {
                    "path": f"06_figures/original/{figure_id}.png",
                    "sha256": original_hash,
                    "width_px": original_size[0],
                    "height_px": original_size[1],
                },
                "final": {
                    "path": f"06_figures/final/{figure_id}.png",
                    "sha256": final_hash,
                    "width_px": final_size[0],
                    "height_px": final_size[1],
                },
                "native_view": {
                    "method": "native_local_image_view",
                    "tool": None,
                    "opened_original": False,
                    "opened_final": False,
                    "opened_original_sha256": None,
                    "opened_final_sha256": None,
                },
                "visible": [],
                "interpretable": [],
                "confirmed": [],
                "cannot_assert": [
                    "donor-level abundance or expression effects",
                    "population prevalence",
                    "mechanism or causality",
                ],
                "findings": [],
                "visual_parameter_diff": {},
            }
        ],
    }


def _prepare_review_records(run_root: Path, signature: str) -> None:
    for figure_id in FIGURES:
        original = run_root / f"06_figures/original/{figure_id}.png"
        final = run_root / f"06_figures/final/{figure_id}.png"
        if not original.is_file() or not final.is_file():
            raise CaseError(f"figure pair is incomplete: {figure_id}")
        target = run_root / f"06_figures/review/{figure_id}.review.json"
        template = _review_template(figure_id, original, final, signature)
        if not target.exists():
            _atomic_json(target, template)
            continue
        current = _read_json(target)
        rounds = current.get("rounds")
        if current.get("data_sha256") != signature or not isinstance(rounds, list) or not rounds:
            raise CaseError(f"invalid review lineage: {figure_id}")
        latest = rounds[-1]
        hashes_match = (
            latest.get("original", {}).get("sha256") == _sha256(original)
            and latest.get("final", {}).get("sha256") == _sha256(final)
        )
        if hashes_match:
            continue
        native_view = latest.get("native_view", {})
        if current.get("status") == "awaiting_native_review" and not native_view.get("opened_original") and not native_view.get("opened_final"):
            _atomic_json(target, template)
            continue
        raise CaseError(f"reviewed pixels changed without a new review round: {figure_id}")


def _review_gate(run_root: Path) -> tuple[bool, list[str]]:
    failures: list[str] = []
    for figure_id in FIGURES:
        review_path = run_root / f"06_figures/review/{figure_id}.review.json"
        if not review_path.is_file():
            failures.append(f"{figure_id}: review record missing")
            continue
        review = _read_json(review_path)
        rounds = review.get("rounds")
        if not isinstance(rounds, list) or not 1 <= len(rounds) <= 3:
            failures.append(f"{figure_id}: review rounds must be 1..3")
            continue
        latest = rounds[-1]
        if [item.get("round") for item in rounds] != list(range(1, len(rounds) + 1)):
            failures.append(f"{figure_id}: review rounds are not consecutive")
        if review.get("status") != "native-reviewed" or latest.get("decision") != "keep":
            failures.append(f"{figure_id}: native review is not terminal keep")
            continue
        original = run_root / f"06_figures/original/{figure_id}.png"
        final = run_root / f"06_figures/final/{figure_id}.png"
        native = latest.get("native_view", {})
        if (
            native.get("method") != "native_local_image_view"
            or not native.get("tool")
            or native.get("opened_original") is not True
            or native.get("opened_final") is not True
            or native.get("opened_original_sha256") != _sha256(original)
            or native.get("opened_final_sha256") != _sha256(final)
            or latest.get("original", {}).get("sha256") != _sha256(original)
            or latest.get("final", {}).get("sha256") != _sha256(final)
        ):
            failures.append(f"{figure_id}: native-view hash binding failed")
        unresolved = [
            finding
            for finding in latest.get("findings", [])
            if isinstance(finding, dict)
            and finding.get("severity") in {"blocker", "major"}
            and finding.get("status") != "resolved"
        ]
        if unresolved:
            failures.append(f"{figure_id}: unresolved blocker/major visual finding")
    return not failures, failures


def _read_metrics(run_root: Path) -> dict[str, int]:
    path = run_root / "05_results/tables/canonical_metrics.csv"
    if not path.is_file():
        raise CaseError("canonical_metrics.csv is missing")
    values: dict[str, int] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            values[str(row["metric"])] = int(float(row["value"]))
    return values


def _validate_feature_name_mapping(run_root: Path) -> dict[str, int]:
    mapping_path = run_root / "05_results/tables/feature_name_mapping.csv"
    summary_path = run_root / "05_results/tables/feature_name_mapping_summary.csv"
    if not mapping_path.is_file() or not summary_path.is_file():
        raise CaseError("FEATURE_NAME_MAPPING_EVIDENCE_MISSING")
    summary: dict[str, int] = {}
    with summary_path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if row.get("status") != "pass":
                raise CaseError("FEATURE_NAME_MAPPING_SUMMARY_FAILED")
            summary[str(row["metric"])] = int(float(row["value"]))
    required_summary = {
        "input_features": 32738,
        "duplicates_after_rename": 0,
        "matrix_rows_unchanged": 1,
        "matrix_columns_unchanged": 1,
        "count_values_unchanged": 1,
    }
    for metric, expected in required_summary.items():
        if summary.get(metric) != expected:
            raise CaseError(f"FEATURE_NAME_MAPPING_GATE_FAILED: {metric}")
    seen: set[str] = set()
    changed = 0
    rows = 0
    with mapping_path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            rows += 1
            original = str(row.get("original_feature", ""))
            normalized = str(row.get("seurat_feature", ""))
            expected_normalized = original.replace("_", "-")
            expected_changed = original != expected_normalized
            if (
                not original
                or normalized != expected_normalized
                or str(row.get("changed", "")).upper() != ("TRUE" if expected_changed else "FALSE")
                or normalized in seen
            ):
                raise CaseError("FEATURE_NAME_MAPPING_CONTENT_MISMATCH")
            seen.add(normalized)
            changed += int(expected_changed)
    if rows != 32738 or summary.get("renamed_features") != changed:
        raise CaseError("FEATURE_NAME_MAPPING_COUNT_MISMATCH")
    return summary


def _validate_umap_runtime_contract(run_root: Path) -> dict[str, Any]:
    path = run_root / "05_results/tables/umap_runtime_contract.json"
    if not path.is_file():
        raise CaseError("UMAP_RUNTIME_CONTRACT_MISSING")
    contract = _read_json(path)
    params = _read_json(run_root / "03_scripts/params.json").get("umap", {})
    expected = {
        "option_name": "Seurat.warn.umap.uwot",
        "option_value_during_call": False,
        "option_restored": True,
        "umap_method": "uwot",
        "metric": "cosine",
        "seed_use": 42,
        "dims_used": 10,
        "algorithm_changed": False,
        "r_warn_option": 1,
        "warning_delivery": "immediate-stderr-via-options(warn=1)",
        "transition_notice_option_applied": True,
        "suppress_warnings_used": False,
        "handler_muffling_used": False,
        "warning_allowlist_used": False,
        "purpose": "use Seurat's official transition option in the smallest scope to disable only its one-time migration notice for an explicitly configured uwot/cosine call, then restore the prior option state",
    }
    if any(contract.get(key) != value for key, value in expected.items()):
        raise CaseError("UMAP_RUNTIME_CONTRACT_MISMATCH")
    option_params = params.get("transition_warning_option", {}) if isinstance(params, dict) else {}
    warning_params = _read_json(run_root / "03_scripts/params.json").get("r_warning_policy", {})
    if (
        not isinstance(params, dict)
        or params.get("method") != contract.get("umap_method")
        or params.get("metric") != contract.get("metric")
        or params.get("seed_use") != contract.get("seed_use")
        or params.get("dims_used") != contract.get("dims_used")
        or not isinstance(option_params, dict)
        or option_params.get("name") != contract.get("option_name")
        or option_params.get("value_during_call") is not False
        or option_params.get("restore_after_call") is not True
        or not isinstance(warning_params, dict)
        or warning_params.get("warn") != contract.get("r_warn_option")
        or warning_params.get("delivery") != "immediate-stderr"
        or warning_params.get("forbidden_scan") != "fail-closed"
    ):
        raise CaseError("UMAP_RUNTIME_PARAMS_MISMATCH")
    return contract


def _environment_completion_evidence(run_root: Path) -> dict[str, str]:
    environment_dir = run_root / "02_environment"
    lock_path = environment_dir / "renv.lock"
    environment_marker = _read_json(run_root / "02_environment/environment.locked.json")
    bootstrap_evidence = _validate_task_local_bootstrap(environment_dir, environment_marker)
    backend_lock = environment_marker.get("backend_lock", {})
    native_exit = environment_marker.get("native_exit", {})
    lock_hash = str(backend_lock.get("sha256", "")) if isinstance(backend_lock, dict) else ""
    completion_record = native_exit.get("completion_marker", {}) if isinstance(native_exit, dict) else {}
    probe_record = native_exit.get("probe", {}) if isinstance(native_exit, dict) else {}
    process_record = native_exit.get("process_evidence", {}) if isinstance(native_exit, dict) else {}
    completion_path = (environment_dir / str(completion_record.get("path", ""))).resolve()
    probe_path = (environment_dir / str(probe_record.get("path", ""))).resolve()
    for path, label in (
        (completion_path, "COMPLETION"),
        (probe_path, "PROBE"),
    ):
        try:
            path.relative_to(environment_dir.resolve())
        except ValueError as exc:
            raise CaseError(f"NATIVE_EXIT_{label}_PATH_ESCAPE") from exc
        if not path.is_file():
            raise CaseError(f"NATIVE_EXIT_{label}_MISSING")
    if not lock_path.is_file() or not lock_hash or _sha256(lock_path) != lock_hash:
        raise CaseError("NATIVE_EXIT_LOCK_HASH_MISMATCH")
    if _sha256(completion_path) != completion_record.get("sha256"):
        raise CaseError("NATIVE_EXIT_COMPLETION_MARKER_HASH_MISMATCH")
    if _sha256(probe_path) != probe_record.get("sha256"):
        raise CaseError("NATIVE_EXIT_PROBE_HASH_MISMATCH")
    process_evidence = _validate_r_process_evidence(
        run_root,
        str(process_record.get("path", "")),
        stage="environment-provision",
        expected_sha256=str(process_record.get("sha256", "")),
    )
    expected_completion = {
        "stage": "environment-provision",
        "status": "complete",
        "shutdown_mode": "native_exit",
        "lock_sha256": lock_hash,
        "probe_sha256": str(probe_record.get("sha256", "")),
    }
    if _read_key_values(completion_path) != expected_completion:
        raise CaseError("NATIVE_EXIT_COMPLETION_CONTENT_MISMATCH")
    return {
        "renv_lock_sha256": lock_hash,
        "environment_shutdown_mode": "native_exit",
        "environment_process_evidence_sha256": _sha256(
            run_root / str(process_record.get("path", ""))
        ),
        "environment_process_command_sha256": str(process_evidence["command_fingerprint_sha256"]),
        "environment_completion_marker_sha256": _sha256(completion_path),
        "environment_probe_sha256": _sha256(probe_path),
        **bootstrap_evidence,
    }


def _pipeline_completion_evidence(run_root: Path) -> dict[str, str]:
    signature_path = run_root / "03_scripts/analysis_signature.txt"
    marker_path = run_root / "logs/r-pipeline.complete"
    if not signature_path.is_file() or not marker_path.is_file():
        raise CaseError("R_PIPELINE_COMPLETION_EVIDENCE_MISSING")
    signature = signature_path.read_text(encoding="utf-8").strip()
    metrics_path = run_root / "05_results/tables/execution_metrics.json"
    umap_contract_path = run_root / "05_results/tables/umap_runtime_contract.json"
    checkpoint_path = run_root / "04_intermediate/SC13_FIGURES_AND_INTERPRETATION/delivery_checkpoint.rds"
    if not metrics_path.is_file() or not umap_contract_path.is_file() or not checkpoint_path.is_file():
        raise CaseError("R_PIPELINE_COMPLETION_ARTIFACT_MISSING")
    observed = _read_key_values(marker_path)
    expected = {
        "stage": "pbmc3k-r-pipeline",
        "status": "complete",
        "shutdown_mode": "native_exit",
        "analysis_signature": signature,
        "execution_metrics_sha256": _sha256(metrics_path),
        "umap_runtime_contract_sha256": _sha256(umap_contract_path),
        "delivery_checkpoint_sha256": _sha256(checkpoint_path),
    }
    if observed != expected:
        raise CaseError("R_PIPELINE_COMPLETION_EVIDENCE_MISMATCH")
    process_path = run_root / "logs/r-pipeline-process-evidence.json"
    process_evidence = _validate_r_process_evidence(
        run_root,
        "logs/r-pipeline-process-evidence.json",
        stage="pbmc3k-r-pipeline",
    )
    return {
        "pipeline_shutdown_mode": "native_exit",
        "pipeline_process_evidence_sha256": _sha256(process_path),
        "pipeline_process_command_sha256": str(process_evidence["command_fingerprint_sha256"]),
        "completion_marker_sha256": _sha256(marker_path),
        "execution_metrics_sha256": expected["execution_metrics_sha256"],
        "umap_runtime_contract_sha256": expected["umap_runtime_contract_sha256"],
        "delivery_checkpoint_sha256": expected["delivery_checkpoint_sha256"],
    }


def _required_artifacts(run_root: Path) -> list[Path]:
    required = [
        run_root / "00_request/input_evidence.json",
        run_root / "02_environment/renv.lock",
        run_root / "02_environment/environment.locked.json",
        run_root / "02_environment/environment.probe",
        run_root / "02_environment/provision.complete",
        run_root / "logs/environment-process-evidence.json",
        run_root / "03_scripts/analysis_signature.txt",
        run_root / "05_results/tables/canonical_metrics.csv",
        run_root / "05_results/tables/feature_name_mapping.csv",
        run_root / "05_results/tables/feature_name_mapping_summary.csv",
        run_root / "05_results/tables/umap_runtime_contract.json",
        run_root / "05_results/tables/qc_summary.csv",
        run_root / "05_results/tables/cluster_markers.csv",
        run_root / "05_results/tables/annotation_evidence.csv",
        run_root / "05_results/tables/cell_metadata_with_umap.csv",
        run_root / "05_results/objects/pbmc3k_annotated.rds",
        run_root / "logs/r-pipeline.complete",
        run_root / "logs/r-pipeline-process-evidence.json",
    ]
    for stage in STAGES:
        required.append(run_root / f"04_intermediate/{stage}/stage.complete.json")
    for figure_id in FIGURES:
        required.extend(
            [
                run_root / f"06_figures/original/{figure_id}.png",
                run_root / f"06_figures/final/{figure_id}.png",
                run_root / f"06_figures/review/{figure_id}.review.json",
            ]
        )
    return required


def _artifact_role(relative: str) -> str:
    prefix_roles = {
        "00_request/": "input-provenance",
        "01_plan/": "analysis-plan",
        "02_environment/": "environment-evidence",
        "03_scripts/": "executable-source",
        "04_intermediate/": "checkpoint",
        "05_results/tables/": "result-table",
        "05_results/objects/": "result-object",
        "06_figures/original/": "original-figure",
        "06_figures/final/": "final-figure",
        "06_figures/review/": "native-review",
        "07_reports/": "report",
        "logs/": "sanitized-log",
        "manifest/": "run-provenance",
    }
    for prefix, role in prefix_roles.items():
        if relative.startswith(prefix):
            return role
    return "supporting-artifact"


def _iter_indexable(run_root: Path) -> Iterable[Path]:
    excluded = {
        "07_reports/ARTIFACT_INDEX.md",
        "manifest/artifact_ledger.jsonl",
    }
    for path in sorted(run_root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(run_root).as_posix()
        if (
            relative.startswith("_staging/")
            or relative.startswith("02_environment/bootstrap-library/")
            or relative in excluded
        ):
            continue
        yield path


def _write_artifact_index(run_root: Path) -> None:
    lines = [
        "# Artifact index",
        "",
        "All paths are relative to the run root. SHA-256 values are computed from delivered bytes.",
        "",
        "| Path | Role | Bytes | SHA-256 |",
        "|---|---|---:|---|",
    ]
    for path in _iter_indexable(run_root):
        relative = path.relative_to(run_root).as_posix()
        lines.append(f"| `{relative}` | {_artifact_role(relative)} | {path.stat().st_size} | `{_sha256(path)}` |")
    lines.append("")
    _atomic_text(run_root / "07_reports/ARTIFACT_INDEX.md", "\n".join(lines))


def _append_ledger(run_root: Path) -> None:
    ledger_path = run_root / "manifest/artifact_ledger.jsonl"
    existing: list[dict[str, Any]] = []
    if ledger_path.is_file():
        with ledger_path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise CaseError(f"artifact ledger is invalid at line {line_number}") from exc
                if not isinstance(item, dict):
                    raise CaseError(f"artifact ledger entry {line_number} is not an object")
                existing.append(item)
    latest = {str(item.get("path")): str(item.get("sha256")) for item in existing}
    additions: list[dict[str, Any]] = []
    sequence = len(existing)
    for path in _iter_indexable(run_root):
        relative = path.relative_to(run_root).as_posix()
        digest = _sha256(path)
        if latest.get(relative) == digest:
            continue
        sequence += 1
        additions.append(
            {
                "sequence": sequence,
                "case_id": CASE_ID,
                "path": relative,
                "sha256": digest,
                "size_bytes": path.stat().st_size,
                "role": _artifact_role(relative),
                "recorded_at_utc": _utc_now(),
            }
        )
    if additions:
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with ledger_path.open("a", encoding="utf-8", newline="\n") as handle:
            for item in additions:
                handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")


def _build_reports(run_root: Path) -> dict[str, Any]:
    metrics = _read_metrics(run_root)
    feature_mapping = _validate_feature_name_mapping(run_root)
    umap_runtime = _validate_umap_runtime_contract(run_root)
    metric_pass = metrics == EXPECTED
    review_pass, review_failures = _review_gate(run_root)
    environment_evidence = _environment_completion_evidence(run_root)
    pipeline_evidence = _pipeline_completion_evidence(run_root)
    execution_evidence = {**environment_evidence, **pipeline_evidence}
    state = "DELIVERED" if metric_pass and review_pass else "NATIVE_VISUAL_REVIEW"
    maturity = "native-reviewed" if review_pass else "data-verified"
    results = f"""# PBMC3K results

## Outcome

The pinned PBMC3K archive produced **{metrics.get('input_cells', 'NA'):,} input cells**, **{metrics.get('qc_retained_cells', 'NA'):,} QC-retained cells**, and **{metrics.get('clusters', 'NA')} descriptive clusters** under R 4.5.3 and Seurat 5.5.0. Canonical numerical checks: **{'pass' if metric_pass else 'fail'}**.

Both R phases completed by native process exit with return code 0. Their structured evidence binds the AMD64 child architecture, sanitized stdout/stderr and an empty forbidden-pattern scan; no external process-termination helper is used.

Before Seurat object creation, `{feature_mapping['renamed_features']:,}` of 32,738 input feature names were explicitly normalized from `_` to `-`. The mapping artifact proves that this created no duplicates and changed neither matrix dimensions nor count values.

UMAP used the explicitly frozen `uwot` / `cosine` / seed 42 contract. Seurat's official `Seurat.warn.umap.uwot=FALSE` transition option disabled only its one-time migration notice in the smallest scope and was restored afterward. The pipeline fixed `options(warn=1)` so every other R warning was emitted immediately to stderr for fail-closed scanning; it used no `suppressWarnings()`, handler muffling, or warning allowlist, and did not change the algorithm.

Cluster markers and the nine teaching labels are descriptive aids for this one public library. They do not estimate donor-level abundance, condition effects, population prevalence, mechanism, or causality. No CellChat, GSEA, pseudobulk, differential abundance, or advanced branch was run.

## Visual status

Native visual review: **{'pass' if review_pass else 'pending/blocked'}**. A rendered PNG is not called native-reviewed until both original and final-size pixels have been opened and their hashes bound to a terminal `keep` record.

## Canonical metrics

| Metric | Observed | Expected | Status |
|---|---:|---:|---|
| Input cells | {metrics.get('input_cells', 'NA')} | 2700 | {'pass' if metrics.get('input_cells') == 2700 else 'fail'} |
| QC-retained cells | {metrics.get('qc_retained_cells', 'NA')} | 2638 | {'pass' if metrics.get('qc_retained_cells') == 2638 else 'fail'} |
| Clusters | {metrics.get('clusters', 'NA')} | 9 | {'pass' if metrics.get('clusters') == 9 else 'fail'} |
"""
    _atomic_text(run_root / "07_reports/RESULTS.md", results)

    figure_notes = """# Figure notes

All figures use the R backend and the same frozen, post-QC PBMC3K analysis baseline. Original and final-size PNGs contain identical cells, values, normalization, PCA/UMAP and clusters; final variants change only declared visual parameters.

| Figure | Question | Directly visible | Supported | Not supported |
|---|---|---|---|---|
| `qc_violin` | What are the per-cell QC distributions? | Distributions of detected features, counts and mitochondrial percentage among retained cells. | Descriptive QC overview. | Donor variability or optimality of thresholds. |
| `pca_clusters` | How do cells occupy the PCA representation? | Cluster-colored cell positions in PC1/PC2. | Descriptive representation structure. | Effect magnitude, lineage or time. |
| `umap_clusters` | How do graph clusters occupy UMAP? | Nine cluster labels and their UMAP neighborhoods. | Cell-level descriptive organization. | Metric distance, abundance significance or developmental trajectory. |
| `umap_annotation` | Where are teaching labels located? | Conservative PBMC teaching labels on UMAP. | Reference-guided descriptive labeling. | Clinical classification or donor-level prevalence. |
| `marker_dotplot` | Which canonical markers characterize labels? | Average normalized expression color and percent-expressed dot size. | Marker-pattern review. | Differential expression between conditions, mechanism or causality. |

See the hash-bound JSON files in `06_figures/review/` for native review evidence and decisions.
"""
    _atomic_text(run_root / "07_reports/FIGURE_NOTES.md", figure_notes)

    qa_rows = [
        ("Pinned archive hash and size", "pass", "Recorded in input_evidence.json"),
        ("Feature-name mapping", "pass", f"{feature_mapping['renamed_features']} underscore-to-dash names; dimensions/counts unchanged"),
        ("UMAP runtime contract", "pass", "uwot/cosine/seed 42; warn=1; official scoped transition option restored; no suppressWarnings/handler muffling/allowlist"),
        ("R 4.5.3 / Seurat 5.5.0 runtime", "pass", "Pipeline exact-version gate passed"),
        ("Native R exit and clean stdout/stderr", "pass", "AMD64 child evidence; return code 0; no forbidden matches"),
        ("2700 / 2638 / 9 canonical values", "pass" if metric_pass else "fail", str(metrics)),
        ("Single-library descriptive claim boundary", "pass", "No inferential or advanced branch"),
        ("Original/final figure pairs", "pass", f"{len(FIGURES)} paired figures"),
        ("Native visual review", "pass" if review_pass else "blocked", "; ".join(review_failures) or "hash-bound terminal keep"),
    ]
    qa_lines = [
        "# QA report",
        "",
        "| Gate | Status | Evidence |",
        "|---|---|---|",
        *[f"| {gate} | **{status}** | {evidence} |" for gate, status, evidence in qa_rows],
        "",
        "A blocked native-review gate is reported honestly and prevents release qualification; it does not erase a successful data execution.",
        "",
    ]
    _atomic_text(run_root / "07_reports/QA_REPORT.md", "\n".join(qa_lines))

    manifest = {
        "schema_version": "1.0.0",
        "case_id": CASE_ID,
        "analysis_signature": (run_root / "03_scripts/analysis_signature.txt").read_text(encoding="utf-8").strip(),
        "state": state,
        "maturity": maturity,
        "canonical_metrics": metrics,
        "feature_name_mapping": feature_mapping,
        "umap_runtime_contract": umap_runtime,
        "canonical_metrics_pass": metric_pass,
        "native_visual_review_pass": review_pass,
        "review_findings": review_failures,
        "execution_evidence": execution_evidence,
        "claim_boundary": "single-library descriptive teaching result; no donor-level inference",
        "remote_upload": False,
    }
    _atomic_json(run_root / "manifest/run_manifest.json", manifest)
    execution_summary = {
        "schema_version": "1.0.0",
        "case_id": CASE_ID,
        "state": state,
        "maturity": maturity,
        "canonical_metrics": metrics,
        "feature_name_mapping": feature_mapping,
        "umap_runtime_contract": umap_runtime,
        "canonical_metrics_pass": metric_pass,
        "native_visual_review_pass": review_pass,
        "execution_evidence": execution_evidence,
        "raw_data_distributed": False,
        "data_license": "CC BY 4.0",
        "claim_boundary": manifest["claim_boundary"],
    }
    _atomic_json(run_root / "manifest/execution-summary.json", execution_summary)
    _write_artifact_index(run_root)
    _append_ledger(run_root)
    return execution_summary


def _verify_ledger(run_root: Path) -> list[str]:
    failures: list[str] = []
    path = run_root / "manifest/artifact_ledger.jsonl"
    if not path.is_file():
        return ["artifact ledger missing"]
    sequences: list[int] = []
    latest: dict[str, dict[str, Any]] = {}
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                failures.append(f"ledger line {line_number} is invalid JSON")
                continue
            sequences.append(item.get("sequence"))
            latest[str(item.get("path"))] = item
    if sequences != list(range(1, len(sequences) + 1)):
        failures.append("ledger sequences are not append-only consecutive integers")
    for relative, item in latest.items():
        artifact = run_root / relative
        if not artifact.is_file():
            failures.append(f"ledger artifact missing: {relative}")
        elif _sha256(artifact) != item.get("sha256"):
            failures.append(f"ledger hash mismatch: {relative}")
    return failures


def verify_run(run_root: Path) -> tuple[bool, dict[str, Any]]:
    failures: list[str] = []
    for path in _required_artifacts(run_root):
        if not path.is_file() or path.stat().st_size == 0:
            failures.append(f"required artifact missing/empty: {path.relative_to(run_root).as_posix()}")
    try:
        metrics = _read_metrics(run_root)
        if metrics != EXPECTED:
            failures.append(f"canonical metrics mismatch: {metrics}")
    except CaseError as exc:
        failures.append(str(exc))
        metrics = {}
    try:
        feature_mapping = _validate_feature_name_mapping(run_root)
    except CaseError as exc:
        failures.append(str(exc))
        feature_mapping = {}
    try:
        umap_runtime = _validate_umap_runtime_contract(run_root)
    except CaseError as exc:
        failures.append(str(exc))
        umap_runtime = {}
    try:
        environment_evidence = _environment_completion_evidence(run_root)
        pipeline_evidence = _pipeline_completion_evidence(run_root)
    except CaseError as exc:
        failures.append(str(exc))
        environment_evidence = {}
        pipeline_evidence = {}
    signature_path = run_root / "03_scripts/analysis_signature.txt"
    signature = signature_path.read_text(encoding="utf-8").strip() if signature_path.is_file() else ""
    for stage in STAGES:
        marker_path = run_root / f"04_intermediate/{stage}/stage.complete.json"
        if marker_path.is_file():
            marker = _read_json(marker_path)
            if marker.get("analysis_signature") != signature or marker.get("status") != "checkpointed":
                failures.append(f"checkpoint marker mismatch: {stage}")
    review_pass, review_failures = _review_gate(run_root)
    failures.extend(review_failures)
    failures.extend(_verify_ledger(run_root))
    summary = {
        "schema_version": "1.0.0",
        "case_id": CASE_ID,
        "status": "pass" if not failures else "fail",
        "canonical_metrics": metrics,
        "feature_name_mapping": feature_mapping,
        "umap_runtime_contract": umap_runtime,
        "native_visual_review_pass": review_pass,
        "execution_evidence": {**environment_evidence, **pipeline_evidence},
        "failures": failures,
    }
    return not failures, summary


def execute_case(args: argparse.Namespace) -> int:
    if args.authorized is not True:
        raise CaseError("EXECUTION_NOT_AUTHORIZED: pass --authorized only after root CLI authorization")
    run_root = _assert_absolute_directory(args.run_root, "run-root", create=True)
    cache_root = _assert_absolute_directory(args.cache_root, "cache-root", create=True)
    rscript = args.rscript
    if not rscript.is_absolute() or not rscript.is_file():
        raise CaseError("rscript must be an existing absolute executable path")
    _seed_run_tree(run_root)
    environment_evidence = _validate_environment_lock(run_root, cache_root)
    signature = _analysis_signature(run_root)
    input_evidence = prepare_input(CASE_DIR / "input_manifest.json", cache_root)
    _write_input_evidence(run_root, input_evidence)
    pipeline_completion = run_root / "logs/r-pipeline.complete"
    pipeline_completion.unlink(missing_ok=True)
    command = [
        str(rscript),
        str(run_root / "03_scripts/run_pipeline.R"),
        "--data-root",
        str(input_evidence["data_root"]),
        "--run-root",
        str(run_root),
        "--params",
        str(run_root / "03_scripts/params.json"),
        "--signature",
        str(run_root / "03_scripts/analysis_signature.txt"),
        "--completion-marker",
        str(pipeline_completion),
    ]
    process_environment = os.environ.copy()
    process_environment["R_LIBS_USER"] = str(environment_evidence["library"])
    process_environment["RENV_PATHS_LIBRARY"] = str(environment_evidence["library"])
    process_environment["RENV_PATHS_CACHE"] = str(environment_evidence["renv_cache"])
    process_environment, architecture_evidence = _r_subprocess_environment(process_environment)
    completed = subprocess.run(
        command,
        cwd=run_root,
        env=process_environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    replacements = ((run_root, "<RUN_ROOT>"), (cache_root, "<CACHE_ROOT>"), (CASE_DIR.parent.parent, "<REPO_ROOT>"))
    sanitized_stdout = _sanitize(completed.stdout, replacements)
    sanitized_stderr = _sanitize(completed.stderr, replacements)
    stdout_path = run_root / "logs/r-pipeline.stdout.log"
    stderr_path = run_root / "logs/r-pipeline.stderr.log"
    _atomic_text(stdout_path, sanitized_stdout)
    _atomic_text(stderr_path, sanitized_stderr)
    process_evidence = _write_r_process_evidence(
        path=run_root / "logs/r-pipeline-process-evidence.json",
        stage="pbmc3k-r-pipeline",
        command=command,
        returncode=completed.returncode,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        stdout=sanitized_stdout,
        stderr=sanitized_stderr,
        architecture=architecture_evidence,
        runtime_contract_path=(
            run_root / "05_results/tables/umap_runtime_contract.json"
            if (run_root / "05_results/tables/umap_runtime_contract.json").is_file()
            else None
        ),
    )
    if process_evidence["status"] != "pass":
        failure = {
            "schema_version": "1.0.0",
            "case_id": CASE_ID,
            "status": "failed",
            "stage": "r-pipeline",
            "returncode": completed.returncode,
            "shutdown_mode": "native_exit",
            "process_evidence_sha256": _sha256(run_root / "logs/r-pipeline-process-evidence.json"),
            "forbidden_matches": process_evidence["forbidden_scan"]["matches"],
            "completion_marker_exists": pipeline_completion.exists(),
            "partial_outputs_are_success": False,
        }
        _atomic_json(run_root / "manifest/execution-summary.json", failure)
        print(json.dumps(failure, ensure_ascii=False, indent=2), file=sys.stderr)
        return completed.returncode or 2
    _pipeline_completion_evidence(run_root)
    _prepare_review_records(run_root, signature)
    summary = _build_reports(run_root)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def report_case(run_root: Path) -> int:
    run_root = _assert_absolute_directory(run_root, "run-root")
    summary = _build_reports(run_root)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def verify_case(run_root: Path) -> int:
    run_root = _assert_absolute_directory(run_root, "run-root")
    passed, summary = verify_run(run_root)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if passed else 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "resume"):
        command = commands.add_parser(name)
        command.add_argument("--run-root", type=Path, required=True)
        command.add_argument("--cache-root", type=Path, required=True)
        command.add_argument("--rscript", type=Path, required=True)
        command.add_argument("--authorized", action="store_true")
    for name in ("verify", "report"):
        command = commands.add_parser(name)
        command.add_argument("--run-root", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command in {"run", "resume"}:
            return execute_case(args)
        if args.command == "verify":
            return verify_case(args.run_root)
        return report_case(args.run_root)
    except (CaseError, InputError, OSError, subprocess.SubprocessError) as exc:
        print(f"PBMC3K_CASE_FAILED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
