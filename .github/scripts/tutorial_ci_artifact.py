#!/usr/bin/env python3
"""Build and verify a minimal, commit-bound tutorial CI evidence bundle.

The bundle intentionally excludes raw inputs, R objects, checkpoints, package
libraries, caches, and unrestricted logs.  It carries the already completed
full-run validator result plus the small files needed to verify its hashes,
plan binding, runtime pin, negative controls, and original/final PNG pairs.
"""

from __future__ import annotations

import argparse
import binascii
import hashlib
import json
import os
import re
import shutil
import struct
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


CASES = {"pbmc3k", "visium-mouse-brain"}
EXPECTED_RUNTIME = {"R": "4.5.3", "Seurat": "5.5.0", "renv": "1.2.2"}
EXPECTED_SKILLS = {
    "biomedical-analysis-agent",
    "scrnaseq-pipeline",
    "spatial-pipeline",
    "bulk-rnaseq",
    "quantitative-proteomics-workflow",
    "multi-omics-pipeline",
    "visualization-2026718-v1",
}
EXPECTED_VISUALIZATION_REPOSITORIES = {
    "https://github.com/anthonyvmppg3yigdt33kp-lang/visualization-2026718-v1",
    "https://github.com/anthonyvmppg3yigdt33kp-lang/visualization-2026718-v1.git",
}
EXPECTED_VISUALIZATION_EXCLUDED_PATHS = [
    "assets/previews-curated",
    "assets/scheme-candidates",
    "assets/source_archive",
    "references/catalog.jsonl",
]
EXPECTED_VISUALIZATION_CAPABILITY_SCOPE = [
    "formal_recipe_adaptation",
    "formal_recipe_composition",
    "formal_recipe_preflight",
    "formal_recipe_rendering",
    "native_visual_review",
]
EXPECTED_VISUALIZATION_OVERLAY_FILES = {
    "SKILL.public-runtime.md": "SKILL.md",
    "manifest.public-runtime.yaml": "manifest.yaml",
}
EXPECTED_VISUALIZATION_RIGHTS_STATUS = (
    "mixed-original-and-third-party-not-relicensed"
)
FULL_SHA = re.compile(r"[0-9a-f]{40}")
SHA256 = re.compile(r"[0-9a-f]{64}")
PRIVATE_PATH = re.compile(
    r"(?i)(?:[A-Z]:[\\/]Users[\\/][^<>\\/\s\"']+|/(?:home|Users)/[^/\s\"']+)"
)
ALLOWED_SUFFIXES = {
    ".csv",
    ".json",
    ".jsonl",
    ".lock",
    ".md",
    ".png",
    ".py",
    ".r",
    ".tsv",
    ".txt",
    ".yaml",
    ".yml",
}
FORBIDDEN_PARTS = {
    ".git",
    ".renv",
    "bootstrap-library",
    "cache",
    "inputs",
    "library",
    "objects",
    "runtime",
}
FORBIDDEN_SUFFIXES = {".dll", ".h5", ".hdf5", ".o", ".rds", ".rdata", ".zip"}
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_BUNDLE_BYTES = 128 * 1024 * 1024
TEXT_SUFFIXES = ALLOWED_SUFFIXES - {".png"}
VISIUM_FORBIDDEN_WARNING_CATEGORIES = {
    "api_compatibility_warning",
    "numerical_integrity_warning",
    "spatial_integrity_warning",
    "unclassified_warning",
}


class BundleError(RuntimeError):
    """Raised when an Actions evidence bundle is unsafe or incomplete."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise BundleError(f"expected a JSON object: {path}")
    return payload


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _safe_relative(value: str) -> PurePosixPath:
    relative = PurePosixPath(value)
    if (
        not relative.parts
        or relative.is_absolute()
        or ".." in relative.parts
        or relative.as_posix() != value
        or any(part.casefold() in FORBIDDEN_PARTS for part in relative.parts)
        or relative.suffix.casefold() in FORBIDDEN_SUFFIXES
    ):
        raise BundleError(f"unsafe or forbidden bundle path: {value}")
    if relative.suffix.casefold() not in ALLOWED_SUFFIXES:
        raise BundleError(f"non-allowlisted bundle file type: {value}")
    return relative


def _assert_regular_source(path: Path, root: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise BundleError(f"source is not a regular file: {path}")
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
    except ValueError as exc:
        raise BundleError(f"source escapes its declared root: {path}") from exc
    size = path.stat().st_size
    if size <= 0 or size > MAX_FILE_BYTES:
        raise BundleError(f"source has an invalid byte size: {path}")


def _copy(source: Path, source_root: Path, destination: Path, relative: str) -> None:
    safe = _safe_relative(relative)
    _assert_regular_source(source, source_root)
    target = destination / Path(*safe.parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target, follow_symlinks=False)


def _iter_regular_files(root: Path) -> Iterable[Path]:
    if not root.exists():
        return
    for current, directories, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in list(directories):
            candidate = current_path / name
            if candidate.is_symlink():
                raise BundleError(f"symlinked directory is forbidden: {candidate}")
        for name in filenames:
            candidate = current_path / name
            if candidate.is_symlink() or not candidate.is_file():
                raise BundleError(f"non-regular source is forbidden: {candidate}")
            yield candidate


def _copy_tree(source: Path, source_root: Path, destination: Path, prefix: str) -> int:
    count = 0
    if not source.is_dir():
        return count
    for path in sorted(_iter_regular_files(source), key=lambda item: item.as_posix().casefold()):
        relative = path.relative_to(source).as_posix()
        _copy(path, source_root, destination, f"{prefix}/{relative}")
        count += 1
    return count


def _validate_visualization_profile(
    value: object, dependency: dict[str, Any]
) -> dict[str, Any]:
    expected_keys = {
        "schema_version",
        "profile_id",
        "purpose",
        "capability_scope",
        "overlay_files",
        "included_rights_boundary",
        "excluded_paths",
        "exclusion_reasons",
        "raw_third_party_data_included",
        "raw_extracted_source_code_included",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise BundleError("visualization distribution profile evidence is invalid")
    if (
        value.get("schema_version") != "1.0.0"
        or value.get("profile_id") != "biomedical-public-runtime-v1"
        or not isinstance(value.get("purpose"), str)
        or not value["purpose"].strip()
        or value.get("capability_scope") != EXPECTED_VISUALIZATION_CAPABILITY_SCOPE
        or value.get("overlay_files") != EXPECTED_VISUALIZATION_OVERLAY_FILES
        or value.get("included_rights_boundary")
        != {
            "original_code_license": dependency.get("original_code_license"),
            "third_party_rights_status": dependency.get("rights_status"),
            "notice_file": dependency.get("third_party_notice_file"),
        }
        or value.get("excluded_paths") != EXPECTED_VISUALIZATION_EXCLUDED_PATHS
        or value.get("raw_third_party_data_included") is not False
        or value.get("raw_extracted_source_code_included") is not False
    ):
        raise BundleError("visualization distribution profile evidence is invalid")
    reasons = value.get("exclusion_reasons")
    if (
        not isinstance(reasons, dict)
        or set(reasons) != set(EXPECTED_VISUALIZATION_EXCLUDED_PATHS)
        or any(not isinstance(reason, str) or not reason.strip() for reason in reasons.values())
    ):
        raise BundleError("visualization distribution profile evidence is invalid")
    return value


def _validate_bootstrap(
    raw_install: Path,
    raw_verify: Path,
    skills_lock: Path,
    commit: str,
) -> dict[str, Any]:
    install = load_json(raw_install)
    verify = load_json(raw_verify)
    for label, payload in (("install", install), ("verify", verify)):
        if (
            payload.get("schema_version") != "1.0.0"
            or payload.get("ok") is not True
            or payload.get("global_skills_modified") is not False
        ):
            raise BundleError(f"{label} bootstrap report is not terminal/task-local")
        skills = payload.get("skills")
        if not isinstance(skills, dict) or set(skills) != EXPECTED_SKILLS:
            raise BundleError(f"{label} bootstrap report has an unexpected skill set")
        for name, record in skills.items():
            if not isinstance(record, dict) or not SHA256.fullmatch(str(record.get("sha256", ""))):
                raise BundleError(f"{label} bootstrap hash is invalid for {name}")
    if any(record.get("status") != "verified-existing" for record in verify["skills"].values()):
        raise BundleError("verify-only bootstrap did not verify every existing task-local skill")
    install_hashes = {name: record["sha256"] for name, record in install["skills"].items()}
    verify_hashes = {name: record["sha256"] for name, record in verify["skills"].items()}
    if install_hashes != verify_hashes:
        raise BundleError("install and verify-only bootstrap hashes differ")
    lock = load_json(skills_lock)
    dependency = lock.get("dependencies", {}).get("visualization-2026718-v1", {})
    pinned_commit = str(dependency.get("commit", ""))
    pinned_content = str(dependency.get("content_sha256", ""))
    pinned_repository = str(dependency.get("repository", ""))
    pinned_subdirectory = str(dependency.get("subdirectory", ""))
    parsed_subdirectory = PurePosixPath(pinned_subdirectory)
    if (
        not FULL_SHA.fullmatch(pinned_commit)
        or not SHA256.fullmatch(pinned_content)
        or install_hashes["visualization-2026718-v1"] != pinned_content
        or "license" in dependency
        or "notice_file" in dependency
        or dependency.get("original_code_license") != "MIT"
        or dependency.get("license_file") != "LICENSE"
        or dependency.get("third_party_notice_file") != "NOTICE.md"
        or dependency.get("rights_status") != EXPECTED_VISUALIZATION_RIGHTS_STATUS
        or dependency.get("distribution_profile_file")
        != "public-install-profile.json"
        or dependency.get("excluded_paths") != EXPECTED_VISUALIZATION_EXCLUDED_PATHS
        or pinned_repository not in EXPECTED_VISUALIZATION_REPOSITORIES
        or not parsed_subdirectory.parts
        or parsed_subdirectory.is_absolute()
        or ".." in parsed_subdirectory.parts
        or parsed_subdirectory.as_posix() != pinned_subdirectory
    ):
        raise BundleError("task-local visualization bootstrap differs from skills.lock.json")
    install_profile = _validate_visualization_profile(
        install.get("visualization_distribution_profile"), dependency
    )
    verify_profile = _validate_visualization_profile(
        verify.get("visualization_distribution_profile"), dependency
    )
    if install_profile != verify_profile:
        raise BundleError("install and verify visualization profiles differ")
    for label, payload in (("install", install), ("verify", verify)):
        if (
            payload.get("visualization_excluded_paths_absent")
            != EXPECTED_VISUALIZATION_EXCLUDED_PATHS
            or payload.get("visualization_overlay_targets_verified")
            != EXPECTED_VISUALIZATION_OVERLAY_FILES
            or payload.get("visualization_runtime_manifest_exclusions_absent")
            != EXPECTED_VISUALIZATION_EXCLUDED_PATHS
        ):
            raise BundleError(
                f"{label} bootstrap did not verify visualization public exclusions"
            )
    return {
        "schema_version": "1.0.0",
        "ok": True,
        "commit": commit,
        "task_local_destination": True,
        "destination_disclosed": False,
        "global_skills_modified": False,
        "install_verified": True,
        "verify_only_verified": True,
        "skills": {name: install_hashes[name] for name in sorted(install_hashes)},
        "visualization_dependency": {
            "repository": pinned_repository,
            "commit": pinned_commit,
            "subdirectory": pinned_subdirectory,
            "content_sha256": pinned_content,
            "original_code_license": "MIT",
            "license_file": "LICENSE",
            "third_party_notice_file": "NOTICE.md",
            "rights_status": EXPECTED_VISUALIZATION_RIGHTS_STATUS,
            "distribution_profile_file": "public-install-profile.json",
            "excluded_paths": EXPECTED_VISUALIZATION_EXCLUDED_PATHS,
        },
        "visualization_distribution_profile": install_profile,
        "visualization_excluded_paths_absent": EXPECTED_VISUALIZATION_EXCLUDED_PATHS,
        "visualization_overlay_targets_verified": EXPECTED_VISUALIZATION_OVERLAY_FILES,
        "visualization_runtime_manifest_exclusions_absent": (
            EXPECTED_VISUALIZATION_EXCLUDED_PATHS
        ),
    }


def _parse_png(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise BundleError(f"invalid PNG signature: {path}")
    offset = 8
    dimensions = (0, 0)
    saw_idat = saw_iend = False
    while offset < len(data):
        if offset + 12 > len(data):
            raise BundleError(f"truncated PNG: {path}")
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        kind = data[offset + 4 : offset + 8]
        end = offset + 12 + length
        if end > len(data):
            raise BundleError(f"truncated PNG chunk: {path}")
        payload = data[offset + 8 : offset + 8 + length]
        crc = struct.unpack(">I", data[offset + 8 + length : end])[0]
        if crc != (binascii.crc32(kind + payload) & 0xFFFFFFFF):
            raise BundleError(f"PNG CRC mismatch: {path}")
        if offset == 8:
            if kind != b"IHDR" or length != 13:
                raise BundleError(f"PNG IHDR is missing: {path}")
            dimensions = struct.unpack(">II", payload[:8])
        if kind == b"IDAT":
            saw_idat = True
        if kind == b"IEND":
            saw_iend = length == 0 and end == len(data)
            break
        offset = end
    if min(dimensions) <= 0 or not saw_idat or not saw_iend:
        raise BundleError(f"incomplete PNG: {path}")
    return dimensions


def _ledger_records(path: Path) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        record = json.loads(line)
        if not isinstance(record, dict):
            raise BundleError(f"ledger line {number} is not an object")
        relative = str(record.get("path", ""))
        parsed = PurePosixPath(relative)
        if not parsed.parts or parsed.is_absolute() or ".." in parsed.parts or parsed.as_posix() != relative:
            raise BundleError(f"unsafe ledger path at line {number}")
        latest[relative] = record
    if not latest:
        raise BundleError("artifact ledger is empty")
    return latest


def _require_gate(path: Path, *, case: str, check: str | None = None) -> dict[str, Any]:
    payload = load_json(path)
    if payload.get("schema_version") not in {"1.0", "1.0.0"} or payload.get("ok") is not True:
        raise BundleError(f"gate evidence is not terminal-passed: {path.name}")
    if payload.get("case") != case:
        raise BundleError(f"gate evidence case mismatch: {path.name}")
    if check is not None and payload.get("check") != check:
        raise BundleError(f"gate evidence check mismatch: {path.name}")
    return payload


def _semantic_verify(bundle: Path, case: str, commit: str) -> dict[str, Any]:
    run = bundle / "run"
    gate = bundle / "gate-evidence"
    ci = load_json(run / "manifest" / "ci-validation-summary.json")
    if (
        ci.get("schema_version") != "1.0.0"
        or ci.get("ok") is not True
        or ci.get("case") != case
        or ci.get("computational_execution") != "passed"
        or ci.get("native_visual_review") != "not_asserted_by_ci"
        or ci.get("runtime") != EXPECTED_RUNTIME
    ):
        raise BundleError("computational validation summary is not a passing non-native CI result")

    summary = run / "manifest" / "execution-summary.json"
    ledger = run / "manifest" / "artifact_ledger.jsonl"
    lock = run / "02_environment" / "renv.lock"
    if ci.get("execution_summary_sha256") != sha256_file(summary):
        raise BundleError("execution summary hash does not match CI validation")
    if ci.get("artifact_ledger_sha256") != sha256_file(ledger):
        raise BundleError("artifact ledger hash does not match CI validation")
    if ci.get("renv_lock_sha256") != sha256_file(lock):
        raise BundleError("renv.lock hash does not match CI validation")
    warning_result: dict[str, Any] = {}
    if case == "visium-mouse-brain":
        pipeline = run / "03_scripts" / "run_pipeline.R"
        analysis_config = run / "03_scripts" / "analysis-params.json"
        warning_path = run / "logs" / "pipeline-warnings.json"
        warning = load_json(warning_path)
        records = warning.get("records")
        warning_checks = (
            warning.get("schema_version") == "1.0",
            warning.get("classification_version") == "1.0",
            warning.get("case") == case,
            warning.get("status") == "passed",
            warning.get("blocking_warning_occurrences") == 0,
            warning.get("scientific_parameters_changed") is False,
            warning.get("absolute_paths_included") is False,
            warning.get("code_hash") == sha256_file(pipeline),
            warning.get("analysis_config_hash") == sha256_file(analysis_config),
            warning.get("environment_lock_hash") == sha256_file(lock),
            isinstance(records, list),
            ci.get("runtime_warning_evidence_sha256") == sha256_file(warning_path),
            ci.get("executed_pipeline_sha256") == sha256_file(pipeline),
            ci.get("analysis_config_sha256") == sha256_file(analysis_config),
            ci.get("runtime_warning_occurrences") == int(warning.get("warning_occurrences", 0)),
            ci.get("runtime_warning_records") == len(records) if isinstance(records, list) else False,
            ci.get("runtime_warning_blockers") == 0,
        )
        if not all(warning_checks):
            raise BundleError("Visium warning evidence is not code/config/environment/CI bound")
        for record in records:
            if (
                not isinstance(record, dict)
                or record.get("category") in VISIUM_FORBIDDEN_WARNING_CATEGORIES
                or record.get("severity") == "release_blocker"
                or record.get("allowlisted") is not True
                or int(record.get("count", 0)) <= 0
            ):
                raise BundleError("Visium warning evidence contains a release blocker")
        warning_result = {"runtime_warning_evidence_sha256": sha256_file(warning_path)}

    compiled = gate / "compiled-plan.json"
    bound = run / "01_plan" / "root-compiled-plan.json"
    compiled_sha = sha256_file(compiled)
    if compiled.read_bytes() != bound.read_bytes():
        raise BundleError("compiled and executed root plans differ")

    authorization = _require_gate(
        gate / "authorization-negative.json",
        case=case,
        check="unauthorized_run_blocked_before_writes",
    )
    if authorization.get("run_root_created") is not False or int(authorization.get("observed_returncode", 0)) == 0:
        raise BundleError("unauthorized-run negative control is incomplete")

    resume = _require_gate(
        gate / "resume-cache-evidence.json",
        case=case,
        check="plan_bound_fresh_resume_shared_cache_and_immutable_artifacts",
    )
    resume_checks = (
        resume.get("fresh_returncode") == 0,
        resume.get("resume_returncode") == 0,
        resume.get("shared_cache_root") is True,
        resume.get("explicit_environment_cache_reuse") is True,
        resume.get("task_local_bootstrap") is True,
        resume.get("host_package_required") is False,
        resume.get("immutable_lock") is True,
        resume.get("immutable_checkpoint_and_analysis_artifacts") is True,
        resume.get("compiled_plan_sha256") == compiled_sha,
        resume.get("executed_plan_sha256") == compiled_sha,
        resume.get("fresh_renv_lock_sha256") == sha256_file(lock),
        resume.get("resume_renv_lock_sha256") == sha256_file(lock),
        int(resume.get("checkpoint_records", 0)) > 0,
        int(resume.get("analysis_artifacts", 0)) > 0,
        int(resume.get("checkpoint_reuse_log_records", 0)) >= int(resume.get("checkpoint_records", 0)),
    )
    if not all(resume_checks):
        raise BundleError("fresh/resume/cache immutability evidence is incomplete")
    if case == "visium-mouse-brain" and not all(
        (
            resume.get("explicit_input_cache_reuse") is True,
            resume.get("input_cache_direct_read_no_copy") is True,
            resume.get("input_cache_external_to_run_root") is True,
            resume.get("canonical_inputs_modified") is False,
        )
    ):
        raise BundleError("Visium input cache-reuse evidence is incomplete")

    checksum = _require_gate(gate / "checksum-negative.json", case=case)
    if (
        checksum.get("failure_closed") is not True
        or checksum.get("failure_code") != "INPUT_CHECKSUM_MISMATCH_REJECTED"
        or checksum.get("observed_returncode") != 2
        or not SHA256.fullmatch(str(checksum.get("stderr_sha256", "")))
    ):
        raise BundleError("checksum negative control is incomplete")

    nonzero = _require_gate(
        gate / "nonzero-negative.json",
        case=case,
        check="nonzero_environment_exit_blocked",
    )
    if (
        int(nonzero.get("observed_returncode", 0)) == 0
        or nonzero.get("completion_marker_created") is not False
        or nonzero.get("dedicated_fault_sentinel_observed") is not True
    ):
        raise BundleError("environment non-zero negative control is incomplete")

    bootstrap = load_json(gate / "skill-bootstrap-evidence.json")
    skills_lock = load_json(gate / "skills.lock.json")
    dependency = skills_lock.get("dependencies", {}).get("visualization-2026718-v1", {})
    profile = _validate_visualization_profile(
        bootstrap.get("visualization_distribution_profile"), dependency
    )
    dependency_subdirectory = str(dependency.get("subdirectory", ""))
    parsed_dependency_subdirectory = PurePosixPath(dependency_subdirectory)
    if (
        bootstrap.get("schema_version") != "1.0.0"
        or bootstrap.get("ok") is not True
        or bootstrap.get("commit") != commit
        or bootstrap.get("global_skills_modified") is not False
        or set(bootstrap.get("skills", {})) != EXPECTED_SKILLS
        or bootstrap.get("visualization_dependency") != {
            "repository": dependency.get("repository"),
            "commit": dependency.get("commit"),
            "subdirectory": dependency_subdirectory,
            "content_sha256": dependency.get("content_sha256"),
            "original_code_license": dependency.get("original_code_license"),
            "license_file": dependency.get("license_file"),
            "third_party_notice_file": dependency.get("third_party_notice_file"),
            "rights_status": dependency.get("rights_status"),
            "distribution_profile_file": dependency.get("distribution_profile_file"),
            "excluded_paths": dependency.get("excluded_paths"),
        }
        or bootstrap.get("skills", {}).get("visualization-2026718-v1")
        != dependency.get("content_sha256")
        or not FULL_SHA.fullmatch(str(dependency.get("commit", "")))
        or not SHA256.fullmatch(str(dependency.get("content_sha256", "")))
        or dependency.get("repository") not in EXPECTED_VISUALIZATION_REPOSITORIES
        or "license" in dependency
        or "notice_file" in dependency
        or dependency.get("original_code_license") != "MIT"
        or dependency.get("license_file") != "LICENSE"
        or dependency.get("third_party_notice_file") != "NOTICE.md"
        or dependency.get("rights_status") != EXPECTED_VISUALIZATION_RIGHTS_STATUS
        or dependency.get("distribution_profile_file")
        != "public-install-profile.json"
        or dependency.get("excluded_paths") != EXPECTED_VISUALIZATION_EXCLUDED_PATHS
        or bootstrap.get("visualization_excluded_paths_absent")
        != EXPECTED_VISUALIZATION_EXCLUDED_PATHS
        or bootstrap.get("visualization_overlay_targets_verified")
        != EXPECTED_VISUALIZATION_OVERLAY_FILES
        or bootstrap.get("visualization_runtime_manifest_exclusions_absent")
        != EXPECTED_VISUALIZATION_EXCLUDED_PATHS
        or profile.get("excluded_paths") != EXPECTED_VISUALIZATION_EXCLUDED_PATHS
        or not parsed_dependency_subdirectory.parts
        or parsed_dependency_subdirectory.is_absolute()
        or ".." in parsed_dependency_subdirectory.parts
        or parsed_dependency_subdirectory.as_posix() != dependency_subdirectory
    ):
        raise BundleError("sanitized task-local skill bootstrap evidence is incomplete")

    details = load_json(gate / "release-evidence-details.json")
    expected_details = {
        "schema_version": "1.0.0",
        "ok": True,
        "case": case,
        "commit": commit,
        "canonical_summary_sha256": sha256_file(summary),
        "r_version": "4.5.3",
        "seurat_version": "5.5.0",
        "renv_version": "1.2.2",
        "renv_lock_sha256": sha256_file(lock),
        "fresh_run_verified": True,
        "resume_verified": True,
        "cache_reuse_verified": True,
        "checksum_failure_rejected": True,
        "nonzero_exit_rejected": True,
    }
    if case == "visium-mouse-brain":
        expected_details["runtime_warning_evidence_sha256"] = warning_result[
            "runtime_warning_evidence_sha256"
        ]
    for key, expected in expected_details.items():
        if details.get(key) != expected:
            raise BundleError(f"release evidence detail mismatch: {key}")

    ledger_latest = _ledger_records(ledger)
    ledger_required_prefixes = ("05_results/tables/", "06_figures/")
    included_run_files = [
        path
        for path in _iter_regular_files(run)
        if path.relative_to(run).as_posix().startswith(ledger_required_prefixes)
    ]
    if not included_run_files:
        raise BundleError("bundle has no ledger-bound result/figure evidence")
    for path in included_run_files:
        relative = path.relative_to(run).as_posix()
        record = ledger_latest.get(relative)
        if not isinstance(record, dict):
            raise BundleError(f"included result is not ledger-bound: {relative}")
        if record.get("sha256") != sha256_file(path) or int(record.get("size_bytes", -1)) != path.stat().st_size:
            raise BundleError(f"included result differs from the ledger: {relative}")
    if case == "visium-mouse-brain":
        warning_path = run / "logs" / "pipeline-warnings.json"
        warning_record = ledger_latest.get("logs/pipeline-warnings.json")
        if (
            not isinstance(warning_record, dict)
            or warning_record.get("sha256") != sha256_file(warning_path)
            or int(warning_record.get("size_bytes", -1)) != warning_path.stat().st_size
        ):
            raise BundleError("Visium warning evidence is not ledger-bound")
    reports = [
        path
        for path in _iter_regular_files(run)
        if path.relative_to(run).as_posix().startswith(("07_reports/", "08_report/"))
    ]
    if not reports:
        raise BundleError("bundle has no generated report evidence")

    originals = sorted((run / "06_figures" / "original").rglob("*.png"))
    finals = sorted((run / "06_figures" / "final").rglob("*.png"))
    original_names = [path.relative_to(run / "06_figures" / "original").as_posix() for path in originals]
    final_names = [path.relative_to(run / "06_figures" / "final").as_posix() for path in finals]
    if original_names != final_names or len(originals) != int(ci.get("original_final_figure_pairs", 0)):
        raise BundleError("original/final PNG pair set differs from the CI validation summary")
    if not originals:
        raise BundleError("no original/final PNG pairs were bundled")
    for path in (*originals, *finals):
        _parse_png(path)
    return {
        "case": case,
        "commit": commit,
        "files": len(list(_iter_regular_files(bundle))),
        "original_final_figure_pairs": len(originals),
        "canonical_summary_sha256": sha256_file(summary),
        "renv_lock_sha256": sha256_file(lock),
        **warning_result,
    }


def verify_bundle(bundle: Path, expected_case: str, expected_commit: str) -> dict[str, Any]:
    if expected_case not in CASES or not FULL_SHA.fullmatch(expected_commit):
        raise BundleError("expected case/commit is invalid")
    bundle = bundle.resolve(strict=True)
    if not bundle.is_dir() or bundle == Path(bundle.anchor):
        raise BundleError("bundle root is invalid")
    manifest_path = bundle / "bundle-manifest.json"
    manifest = load_json(manifest_path)
    if (
        manifest.get("schema_version") != "1.0.0"
        or manifest.get("case") != expected_case
        or manifest.get("commit") != expected_commit
        or manifest.get("raw_inputs_included") is not False
        or manifest.get("r_objects_included") is not False
        or manifest.get("checkpoints_included") is not False
        or manifest.get("environment_libraries_included") is not False
        or manifest.get("unrestricted_logs_included") is not False
        or manifest.get("native_visual_review_asserted") is not False
    ):
        raise BundleError("bundle manifest identity/safety assertions are invalid")
    declared = manifest.get("files")
    if not isinstance(declared, list) or not declared:
        raise BundleError("bundle manifest has no files")
    if manifest.get("file_count") != len(declared):
        raise BundleError("bundle manifest file count is inconsistent")
    declared_by_path: dict[str, dict[str, Any]] = {}
    for record in declared:
        if not isinstance(record, dict):
            raise BundleError("bundle file record is not an object")
        relative = str(record.get("path", ""))
        _safe_relative(relative)
        if relative == "bundle-manifest.json" or relative in declared_by_path:
            raise BundleError(f"duplicate/reserved bundle manifest path: {relative}")
        declared_by_path[relative] = record
    actual = {
        path.relative_to(bundle).as_posix(): path
        for path in _iter_regular_files(bundle)
        if path != manifest_path
    }
    if set(actual) != set(declared_by_path):
        raise BundleError("bundle file set differs from its exact manifest")
    total = 0
    for relative, path in actual.items():
        _safe_relative(relative)
        size = path.stat().st_size
        total += size
        record = declared_by_path[relative]
        if size <= 0 or size > MAX_FILE_BYTES or int(record.get("size_bytes", -1)) != size:
            raise BundleError(f"bundle file size mismatch: {relative}")
        digest = sha256_file(path)
        if record.get("sha256") != digest or not SHA256.fullmatch(digest):
            raise BundleError(f"bundle file hash mismatch: {relative}")
        if path.suffix.casefold() in TEXT_SUFFIXES and relative != "verify_tutorial_ci_artifact.py":
            text = path.read_text(encoding="utf-8-sig", errors="strict")
            if PRIVATE_PATH.search(text):
                raise BundleError(f"private absolute path found in bundle text: {relative}")
    if total > MAX_BUNDLE_BYTES or int(manifest.get("total_size_bytes", -1)) != total:
        raise BundleError("bundle total size is invalid")
    semantic = _semantic_verify(bundle, expected_case, expected_commit)
    return {
        "schema_version": "1.0.0",
        "evidence_type": "tutorial-bundle-verification",
        "ok": True,
        "case": expected_case,
        "commit": expected_commit,
        "manifest_sha256": sha256_file(manifest_path),
        "file_count": len(actual),
        "total_size_bytes": total,
        **semantic,
    }


def build_bundle(
    *,
    case: str,
    commit: str,
    run_root: Path,
    evidence_root: Path,
    skills_lock: Path,
    output: Path,
) -> dict[str, Any]:
    if case not in CASES or not FULL_SHA.fullmatch(commit):
        raise BundleError("case/commit is invalid")
    run_root = run_root.resolve(strict=True)
    evidence_root = evidence_root.resolve(strict=True)
    skills_lock = skills_lock.resolve(strict=True)
    output = output.resolve()
    if output.exists() and any(output.iterdir()):
        raise BundleError("output bundle directory must be absent or empty")
    output.mkdir(parents=True, exist_ok=True)

    run_files = (
        "01_plan/root-compiled-plan.json",
        "02_environment/environment.locked.json",
        "02_environment/renv.lock",
        "manifest/execution-summary.json",
        "manifest/artifact_ledger.jsonl",
        "manifest/ci-validation-summary.json",
    )
    for relative in run_files:
        _copy(run_root / Path(relative), run_root, output, f"run/{relative}")
    visual_config = "03_scripts/params.json" if case == "pbmc3k" else "03_scripts/visual-params.json"
    _copy(run_root / Path(visual_config), run_root, output, f"run/{visual_config}")
    if case == "visium-mouse-brain":
        for relative in (
            "03_scripts/run_pipeline.R",
            "03_scripts/analysis-params.json",
            "logs/pipeline-warnings.json",
        ):
            _copy(run_root / Path(relative), run_root, output, f"run/{relative}")

    table_count = _copy_tree(run_root / "05_results" / "tables", run_root, output, "run/05_results/tables")
    original_count = _copy_tree(run_root / "06_figures" / "original", run_root, output, "run/06_figures/original")
    final_count = _copy_tree(run_root / "06_figures" / "final", run_root, output, "run/06_figures/final")
    report_count = _copy_tree(run_root / "07_reports", run_root, output, "run/07_reports")
    report_count += _copy_tree(run_root / "08_report", run_root, output, "run/08_report")
    if min(table_count, original_count, final_count, report_count) <= 0:
        raise BundleError("minimal bundle requires tables, reports, and original/final figures")

    for name in (
        "authorization-negative.json",
        "compiled-plan.json",
        "resume-cache-evidence.json",
        "checksum-negative.json",
        "nonzero-negative.json",
    ):
        _copy(evidence_root / name, evidence_root, output, f"gate-evidence/{name}")

    _copy(skills_lock, skills_lock.parent, output, "gate-evidence/skills.lock.json")

    bootstrap = _validate_bootstrap(
        evidence_root / "bootstrap-install.json",
        evidence_root / "bootstrap-verify.json",
        skills_lock,
        commit,
    )
    write_json(output / "gate-evidence" / "skill-bootstrap-evidence.json", bootstrap)

    ci = load_json(run_root / "manifest" / "ci-validation-summary.json")
    details = {
        "schema_version": "1.0.0",
        "evidence_type": "tutorial-release-details",
        "ok": True,
        "case": case,
        "commit": commit,
        "canonical_summary_sha256": ci.get("execution_summary_sha256"),
        "r_version": ci.get("runtime", {}).get("R"),
        "seurat_version": ci.get("runtime", {}).get("Seurat"),
        "renv_version": ci.get("runtime", {}).get("renv"),
        "renv_lock_sha256": ci.get("renv_lock_sha256"),
        "fresh_run_verified": True,
        "resume_verified": True,
        "cache_reuse_verified": True,
        "checksum_failure_rejected": True,
        "nonzero_exit_rejected": True,
    }
    if case == "visium-mouse-brain":
        details["runtime_warning_evidence_sha256"] = ci.get(
            "runtime_warning_evidence_sha256"
        )
    write_json(output / "gate-evidence" / "release-evidence-details.json", details)

    verifier = Path(__file__).resolve(strict=True)
    shutil.copy2(verifier, output / "verify_tutorial_ci_artifact.py", follow_symlinks=False)
    readme = (
        "This is a commit-bound computational evidence bundle. It excludes raw inputs, RDS files, "
        "checkpoints, package libraries, caches, and unrestricted logs. It does not assert native "
        "visual review.\n\n"
        f"Verify: python verify_tutorial_ci_artifact.py verify --bundle-root . "
        f"--expected-case {case} --expected-commit {commit}\n"
    )
    (output / "README.txt").write_text(readme, encoding="utf-8", newline="\n")

    files = []
    total = 0
    for path in sorted(_iter_regular_files(output), key=lambda item: item.as_posix().casefold()):
        if path.name == "bundle-manifest.json":
            continue
        relative = path.relative_to(output).as_posix()
        _safe_relative(relative)
        size = path.stat().st_size
        total += size
        files.append({"path": relative, "size_bytes": size, "sha256": sha256_file(path)})
    if total > MAX_BUNDLE_BYTES:
        raise BundleError("minimal evidence bundle exceeds the size cap")
    manifest = {
        "schema_version": "1.0.0",
        "case": case,
        "commit": commit,
        "raw_inputs_included": False,
        "r_objects_included": False,
        "checkpoints_included": False,
        "environment_libraries_included": False,
        "unrestricted_logs_included": False,
        "native_visual_review_asserted": False,
        "file_count": len(files),
        "total_size_bytes": total,
        "files": files,
    }
    write_json(output / "bundle-manifest.json", manifest)
    return verify_bundle(output, case, commit)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("--case", choices=sorted(CASES), required=True)
    build.add_argument("--commit", required=True)
    build.add_argument("--run-root", type=Path, required=True)
    build.add_argument("--evidence-root", type=Path, required=True)
    build.add_argument("--skills-lock", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--bundle-root", type=Path, required=True)
    verify.add_argument("--expected-case", choices=sorted(CASES), required=True)
    verify.add_argument("--expected-commit", required=True)
    verify.add_argument("--output-report", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "build":
            report = build_bundle(
                case=args.case,
                commit=args.commit,
                run_root=args.run_root,
                evidence_root=args.evidence_root,
                skills_lock=args.skills_lock,
                output=args.output,
            )
        else:
            report = verify_bundle(args.bundle_root, args.expected_case, args.expected_commit)
    except (BundleError, OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        sys.stderr.write(f"TUTORIAL_CI_ARTIFACT_FAILED: {exc}\n")
        return 2
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.command == "verify" and args.output_report:
        args.output_report.parent.mkdir(parents=True, exist_ok=True)
        args.output_report.write_text(rendered, encoding="utf-8", newline="\n")
    sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
