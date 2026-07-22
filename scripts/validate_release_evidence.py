#!/usr/bin/env python3
"""Validate commit-bound evidence before v1.0.0 assets or a tag are created."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
FULL_SHA = re.compile(r"[0-9a-f]{40}")
SHA256 = re.compile(r"[0-9a-f]{64}")
SEMVER_TAG = re.compile(r"v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")
SCHEMA_VERSION = re.compile(r"[1-9][0-9]*\.[0-9]+(?:\.[0-9]+)?")
PRIVATE_PATH = re.compile(r"(?i)(?:[A-Z]:[\\/]Users[\\/][^\\/\s\"']+|/home/[^/\s\"']+)")
EXPECTED_REPOSITORY = "anthonyvmppg3yigdt33kp-lang/biomedical-analysis-agent"
EXPECTED_CLONE_REMOTE = (
    "https://github.com/anthonyvmppg3yigdt33kp-lang/biomedical-analysis-agent.git"
)
EXPECTED_ACTION_WORKFLOWS = {
    "github_actions_ci": "CI",
    "github_actions_real_data": "Real-data release gate",
}
EXPECTED_ACTION_PATHS = {
    "github_actions_ci": ".github/workflows/ci.yml",
    "github_actions_real_data": ".github/workflows/real-data-release-gate.yml",
}
EXPECTED_ACTION_EVENTS = {
    "github_actions_ci": {"push", "workflow_dispatch"},
    "github_actions_real_data": {"workflow_dispatch"},
}
EVIDENCE_ROOT = PurePosixPath("validation/runtime/release-evidence")
MAX_EVIDENCE_BYTES = 64 * 1024 * 1024

REQUIRED_GATES = (
    "local_static",
    "local_pbmc3k",
    "local_visium_mouse_brain",
    "local_native_visual_review",
    "upstream_visualization",
    "github_actions_ci",
    "github_actions_real_data",
    "anonymous_clone",
    "license_and_leak_scan",
)
ACTION_GATES = set(EXPECTED_ACTION_WORKFLOWS)
TUTORIAL_GATES = {"local_pbmc3k", "local_visium_mouse_brain"}
TUTORIAL_CASES = {
    "local_pbmc3k": "pbmc3k",
    "local_visium_mouse_brain": "visium-mouse-brain",
}
TUTORIAL_DETAIL_KEYS = {
    "canonical_summary_sha256",
    "r_version",
    "seurat_version",
    "renv_version",
    "renv_lock_sha256",
    "fresh_run_verified",
    "resume_verified",
    "cache_reuse_verified",
    "checksum_failure_rejected",
    "nonzero_exit_rejected",
}
STATIC_CHECK_KEYS = {
    "test_suite_passed",
    "routing_180_passed",
    "router_confusion_passed",
    "schema_validation_passed",
    "companion_skills_passed",
}
CLONE_COMMAND_KEYS = {
    "clone",
    "bootstrap_install",
    "bootstrap_verify",
    "tests",
    "pbmc3k",
    "visium_mouse_brain",
}
CLONE_CASE_KEYS = {"pbmc3k", "visium-mouse-brain"}
NATIVE_CASE_PAIR_COUNTS = {"pbmc3k": 5, "visium-mouse-brain": 3}
NATIVE_PAIR_KEYS = {
    "pair_id",
    "case",
    "figure_id",
    "original_sha256",
    "final_sha256",
    "opened_original",
    "opened_final",
    "decision",
    "unresolved_blocker_or_major",
}
FIGURE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
ALLOWED_STATUS = {"pending", "passed", "failed", "blocked"}
ALLOWED_EVIDENCE_KINDS = {
    "anonymous-clone",
    "checksum",
    "command-log",
    "file",
    "github-actions",
    "native-review",
}


def _finding(code: str, path: str, message: str) -> dict[str, str]:
    return {"code": code, "path": path, "message": message}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_local_locator(locator: str, repository_root: Path | None) -> Path | None:
    parsed = urlsplit(locator)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment or "\\" in locator:
        return None
    relative = PurePosixPath(locator)
    if not relative.parts or relative.is_absolute() or ".." in relative.parts:
        return None
    if relative.parts[: len(EVIDENCE_ROOT.parts)] != EVIDENCE_ROOT.parts:
        return None
    if len(relative.parts) == len(EVIDENCE_ROOT.parts):
        return None
    if repository_root is None:
        return None
    candidate = (repository_root / Path(*relative.parts)).resolve()
    try:
        candidate.relative_to(repository_root)
    except ValueError:
        return None
    return candidate


def _load_schema_bound_evidence(
    path: Path,
    *,
    kind: str,
) -> tuple[Any | None, str | None]:
    """Return parsed evidence and an error message, if any."""

    if path.is_symlink() or not path.is_file():
        return None, "local evidence does not exist as a regular file"
    if path.stat().st_size <= 0 or path.stat().st_size > MAX_EVIDENCE_BYTES:
        return None, "local evidence has an invalid byte size"
    suffix = path.suffix.casefold()
    if suffix == ".json":
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return None, f"invalid evidence JSON: {exc}"
        if not isinstance(payload, dict):
            return None, "evidence JSON root must be an object"
        if kind != "github-actions" and not SCHEMA_VERSION.fullmatch(
            str(payload.get("schema_version", ""))
        ):
            return None, "evidence JSON requires an explicit schema_version"
        return payload, None
    if suffix == ".jsonl":
        rows = []
        try:
            for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
                if not line.strip():
                    continue
                row = json.loads(line)
                if not isinstance(row, dict):
                    return None, f"JSONL line {line_number} is not an object"
                rows.append(row)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return None, f"invalid evidence JSONL: {exc}"
        if not rows:
            return None, "evidence JSONL is empty"
        return rows, None
    if suffix == ".txt" and kind == "checksum":
        lines = path.read_text(encoding="utf-8").splitlines()
        if not lines or any(not re.fullmatch(r"[0-9a-f]{64}  [^/\\]+", line) for line in lines):
            return None, "checksum evidence is not a strict SHA256SUMS file"
        return lines, None
    return None, "passed evidence must use a schema-valid JSON/JSONL or checksum manifest"


def _validate_actions_snapshot(
    snapshot: Any,
    *,
    gate_name: str,
    details: dict[str, Any],
    commit: str,
) -> list[tuple[str, str]]:
    errors: list[tuple[str, str]] = []
    if not isinstance(snapshot, dict):
        return [("actions-api-schema", "saved GitHub API response must be a JSON object")]
    run_url = str(details.get("run_url", ""))
    match = re.fullmatch(
        r"https://github\.com/([^/]+/[^/]+)/actions/runs/([1-9][0-9]*)", run_url
    )
    if not match or match.group(1).casefold() != EXPECTED_REPOSITORY.casefold():
        errors.append(("actions-url", "run URL must identify the production repository"))
        run_id = ""
    else:
        run_id = match.group(2)
    repository = snapshot.get("repository")
    observed_repository = repository.get("full_name") if isinstance(repository, dict) else None
    expected_workflow = EXPECTED_ACTION_WORKFLOWS[gate_name]
    expected_path = EXPECTED_ACTION_PATHS[gate_name]
    observed_event = snapshot.get("event")
    comparisons = {
        "repository": (observed_repository, EXPECTED_REPOSITORY),
        "workflow": (snapshot.get("name"), expected_workflow),
        "path": (snapshot.get("path"), expected_path),
        "head_sha": (snapshot.get("head_sha"), commit),
        "conclusion": (snapshot.get("conclusion"), "success"),
        "status": (snapshot.get("status"), "completed"),
        "html_url": (snapshot.get("html_url"), run_url),
        "run_id": (str(snapshot.get("id", "")), run_id),
    }
    detail_expected = {
        "repository": EXPECTED_REPOSITORY,
        "workflow": expected_workflow,
        "path": expected_path,
        "event": observed_event,
        "head_sha": commit,
        "conclusion": "success",
        "run_url": run_url,
    }
    if observed_event not in EXPECTED_ACTION_EVENTS[gate_name]:
        errors.append(("actions-api-event", "saved Actions API event is not allowed for this workflow"))
    if details != detail_expected:
        errors.append(("actions-details-binding", "Actions details must exactly bind the API snapshot"))
    for key, (observed, expected) in comparisons.items():
        if observed != expected:
            errors.append((f"actions-api-{key}", f"saved Actions API {key} mismatch"))
    return errors


def _payloads(
    parsed_evidence: list[dict[str, Any]],
    *,
    kind: str | None = None,
    evidence_type: str | None = None,
) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for record in parsed_evidence:
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        if kind is not None and record.get("kind") != kind:
            continue
        if evidence_type is not None and payload.get("evidence_type") != evidence_type:
            continue
        matches.append(record)
    return matches


def _exact_details(
    findings: list[dict[str, str]],
    *,
    path: str,
    details: dict[str, Any],
    expected: dict[str, Any],
    code: str,
) -> None:
    if details != expected:
        findings.append(
            _finding(code, f"{path}.details", "gate details must exactly match hash-verified evidence")
        )


def _positive_int(value: Any) -> bool:
    try:
        return int(value) > 0
    except (TypeError, ValueError, OverflowError):
        return False


def _int_equals(value: Any, expected: int) -> bool:
    try:
        return int(value) == expected
    except (TypeError, ValueError, OverflowError):
        return False


def validate_evidence(
    payload: dict[str, Any],
    *,
    expected_version: str | None = None,
    expected_commit: str | None = None,
    require_passed: bool = True,
    repository_root: Path | None = None,
) -> dict[str, Any]:
    findings: list[dict[str, str]] = []
    if repository_root is not None:
        repository_root = repository_root.resolve()
    if payload.get("schema_version") != "1.0.0":
        findings.append(_finding("schema-version", "schema_version", "expected 1.0.0"))

    release = payload.get("release")
    if not isinstance(release, dict):
        release = {}
        findings.append(_finding("release-object", "release", "release must be an object"))
    version = str(release.get("version", ""))
    commit = str(release.get("commit", ""))
    if not SEMVER_TAG.fullmatch(version):
        findings.append(_finding("version", "release.version", "expected a lowercase v-prefixed SemVer tag"))
    if not FULL_SHA.fullmatch(commit):
        findings.append(_finding("commit", "release.commit", "expected a full lowercase Git SHA"))
    if expected_version is not None and version != expected_version:
        findings.append(_finding("version-mismatch", "release.version", f"expected {expected_version}"))
    if expected_commit is not None and commit != expected_commit:
        findings.append(_finding("commit-mismatch", "release.commit", f"expected {expected_commit}"))

    gates = payload.get("gates")
    if not isinstance(gates, dict):
        gates = {}
        findings.append(_finding("gates-object", "gates", "gates must be an object"))
    for gate_name in REQUIRED_GATES:
        path = f"gates.{gate_name}"
        gate = gates.get(gate_name)
        if not isinstance(gate, dict):
            findings.append(_finding("missing-gate", path, "required release gate is missing"))
            continue
        status = str(gate.get("status", ""))
        if status not in ALLOWED_STATUS:
            findings.append(_finding("gate-status", f"{path}.status", "invalid gate status"))
        if require_passed and status != "passed":
            findings.append(_finding("gate-not-passed", f"{path}.status", f"observed {status or 'missing'}"))
        gate_commit = str(gate.get("commit", ""))
        if status == "passed" and gate_commit != commit:
            findings.append(_finding("gate-commit", f"{path}.commit", "passed gate is not bound to release.commit"))
        evidence = gate.get("evidence")
        if not isinstance(evidence, list):
            findings.append(_finding("evidence-list", f"{path}.evidence", "evidence must be a list"))
            evidence = []
        if status == "passed" and not evidence:
            findings.append(_finding("missing-evidence", f"{path}.evidence", "passed gate requires evidence"))
        parsed_evidence: list[dict[str, Any]] = []
        for index, item in enumerate(evidence):
            evidence_path = f"{path}.evidence[{index}]"
            if not isinstance(item, dict):
                findings.append(_finding("evidence-object", evidence_path, "evidence item must be an object"))
                continue
            kind = str(item.get("kind", ""))
            if kind not in ALLOWED_EVIDENCE_KINDS:
                findings.append(_finding("evidence-kind", f"{evidence_path}.kind", "invalid evidence kind"))
            locator = str(item.get("locator", ""))
            if not locator:
                findings.append(_finding("evidence-locator", f"{evidence_path}.locator", "locator is required"))
                continue
            if PRIVATE_PATH.search(locator):
                findings.append(_finding("private-locator", f"{evidence_path}.locator", "private home locator is forbidden"))
                continue
            digest = str(item.get("sha256", ""))
            if status == "passed" and not SHA256.fullmatch(digest):
                findings.append(_finding("evidence-sha256", f"{evidence_path}.sha256", "passed evidence requires SHA-256"))
                continue
            local_path = _resolve_local_locator(locator, repository_root)
            if local_path is None:
                findings.append(
                    _finding(
                        "evidence-local-locator",
                        f"{evidence_path}.locator",
                        "passed evidence must be repository-relative beneath "
                        "validation/runtime/release-evidence/",
                    )
                )
                continue
            parsed, schema_error = _load_schema_bound_evidence(local_path, kind=kind)
            if schema_error:
                findings.append(_finding("evidence-schema", evidence_path, schema_error))
                continue
            if _sha256_file(local_path) != digest:
                findings.append(_finding("evidence-hash-mismatch", f"{evidence_path}.sha256", "local evidence hash mismatch"))
                continue
            parsed_evidence.append(
                {
                    "kind": kind,
                    "payload": parsed,
                    "sha256": digest,
                    "locator": locator,
                    "local_path": local_path,
                }
            )

        details = gate.get("details", {})
        if not isinstance(details, dict):
            findings.append(_finding("details-object", f"{path}.details", "details must be an object"))
            details = {}
        if status != "passed":
            continue
        if gate_name in ACTION_GATES:
            action_snapshots = [
                record["payload"]
                for record in parsed_evidence
                if record["kind"] == "github-actions"
            ]
            if len(action_snapshots) != 1 or len(parsed_evidence) != 1:
                findings.append(_finding("actions-api-evidence", f"{path}.evidence", "exactly one saved GitHub Actions API response is required"))
            else:
                for code, message in _validate_actions_snapshot(
                    action_snapshots[0], gate_name=gate_name, details=details, commit=commit
                ):
                    findings.append(_finding(code, f"{path}.details", message))
        elif gate_name == "local_static":
            records = _payloads(parsed_evidence, evidence_type="local-static-validation")
            if len(records) != 1 or len(parsed_evidence) != 1:
                findings.append(
                    _finding(
                        "static-evidence",
                        f"{path}.evidence",
                        "exactly one local-static-validation JSON is required",
                    )
                )
            else:
                evidence_payload = records[0]["payload"]
                checks = evidence_payload.get("checks")
                if (
                    evidence_payload.get("ok") is not True
                    or evidence_payload.get("gate") != gate_name
                    or evidence_payload.get("commit") != commit
                    or not isinstance(checks, dict)
                    or set(checks) != STATIC_CHECK_KEYS
                    or any(checks.get(key) is not True for key in STATIC_CHECK_KEYS)
                ):
                    findings.append(
                        _finding(
                            "static-evidence-content",
                            f"{path}.evidence",
                            "static evidence must be commit-bound with every required check passed",
                        )
                    )
                elif isinstance(checks, dict):
                    _exact_details(
                        findings,
                        path=path,
                        details=details,
                        expected=checks,
                        code="static-details-binding",
                    )
        elif gate_name in TUTORIAL_GATES:
            expected_case = TUTORIAL_CASES[gate_name]
            detail_records = _payloads(
                parsed_evidence, evidence_type="tutorial-release-details"
            )
            verification_records = _payloads(
                parsed_evidence, evidence_type="tutorial-bundle-verification"
            )
            if (
                len(detail_records) != 1
                or len(verification_records) != 1
                or len(parsed_evidence) != 2
            ):
                findings.append(
                    _finding(
                        "tutorial-evidence-set",
                        f"{path}.evidence",
                        "exactly one tutorial details JSON and one bundle verification JSON are required",
                    )
                )
            else:
                detail_payload = detail_records[0]["payload"]
                verification = verification_records[0]["payload"]
                expected_keys = set(TUTORIAL_DETAIL_KEYS)
                if expected_case == "visium-mouse-brain":
                    expected_keys.add("runtime_warning_evidence_sha256")
                evidence_details = {
                    key: detail_payload.get(key) for key in expected_keys
                }
                identity_ok = (
                    detail_payload.get("ok") is True
                    and detail_payload.get("case") == expected_case
                    and detail_payload.get("commit") == commit
                    and verification.get("ok") is True
                    and verification.get("case") == expected_case
                    and verification.get("commit") == commit
                    and verification.get("canonical_summary_sha256")
                    == detail_payload.get("canonical_summary_sha256")
                    and verification.get("renv_lock_sha256")
                    == detail_payload.get("renv_lock_sha256")
                    and FULL_SHA.fullmatch(commit) is not None
                    and SHA256.fullmatch(str(verification.get("manifest_sha256", "")))
                    is not None
                    and _positive_int(verification.get("file_count"))
                    and _positive_int(verification.get("total_size_bytes"))
                    and _int_equals(
                        verification.get("original_final_figure_pairs"),
                        5 if expected_case == "pbmc3k" else 3,
                    )
                )
                if expected_case == "visium-mouse-brain":
                    identity_ok = identity_ok and (
                        verification.get("runtime_warning_evidence_sha256")
                        == detail_payload.get("runtime_warning_evidence_sha256")
                        and SHA256.fullmatch(
                            str(detail_payload.get("runtime_warning_evidence_sha256", ""))
                        )
                        is not None
                    )
                runtime_ok = (
                    detail_payload.get("r_version") == "4.5.3"
                    and detail_payload.get("seurat_version") == "5.5.0"
                    and detail_payload.get("renv_version") == "1.2.2"
                    and SHA256.fullmatch(
                        str(detail_payload.get("canonical_summary_sha256", ""))
                    )
                    is not None
                    and SHA256.fullmatch(str(detail_payload.get("renv_lock_sha256", "")))
                    is not None
                    and all(
                        detail_payload.get(key) is True
                        for key in (
                            "fresh_run_verified",
                            "resume_verified",
                            "cache_reuse_verified",
                            "checksum_failure_rejected",
                            "nonzero_exit_rejected",
                        )
                    )
                )
                if not identity_ok or not runtime_ok:
                    findings.append(
                        _finding(
                            "tutorial-evidence-content",
                            f"{path}.evidence",
                            "tutorial evidence is not exact-case, commit, runtime, hash, and control bound",
                        )
                    )
                _exact_details(
                    findings,
                    path=path,
                    details=details,
                    expected=evidence_details,
                    code="tutorial-details-binding",
                )
        elif gate_name == "local_native_visual_review":
            records = _payloads(
                parsed_evidence,
                kind="native-review",
                evidence_type="native-review-registry",
            )
            if len(records) != 1 or len(parsed_evidence) != 1:
                findings.append(
                    _finding(
                        "native-review-evidence",
                        f"{path}.evidence",
                        "exactly one native-review registry JSON is required",
                    )
                )
            else:
                record = records[0]
                evidence_payload = record["payload"]
                pairs = evidence_payload.get("pairs")
                pair_ids: set[str] = set()
                figure_keys: set[tuple[str, str]] = set()
                observed_counts = {case: 0 for case in NATIVE_CASE_PAIR_COUNTS}
                pairs_valid = isinstance(pairs, list) and len(pairs) == sum(
                    NATIVE_CASE_PAIR_COUNTS.values()
                )
                if isinstance(pairs, list):
                    for pair in pairs:
                        if not isinstance(pair, dict) or set(pair) != NATIVE_PAIR_KEYS:
                            pairs_valid = False
                            continue
                        case = pair.get("case")
                        figure_id = pair.get("figure_id")
                        pair_id = pair.get("pair_id")
                        if (
                            case not in NATIVE_CASE_PAIR_COUNTS
                            or not isinstance(figure_id, str)
                            or FIGURE_ID.fullmatch(figure_id) is None
                            or pair_id != f"{case}/{figure_id}"
                            or pair_id in pair_ids
                            or (case, figure_id) in figure_keys
                            or SHA256.fullmatch(str(pair.get("original_sha256", ""))) is None
                            or SHA256.fullmatch(str(pair.get("final_sha256", ""))) is None
                            or pair.get("opened_original") is not True
                            or pair.get("opened_final") is not True
                            or pair.get("decision") != "keep"
                            or pair.get("unresolved_blocker_or_major") is not False
                        ):
                            pairs_valid = False
                            continue
                        pair_ids.add(pair_id)
                        figure_keys.add((case, figure_id))
                        observed_counts[case] += 1
                expected_details = {
                    "review_registry_sha256": record["sha256"],
                    "all_original_final_pairs_opened": True,
                    "original_final_pair_count": 8,
                    "case_pair_counts": NATIVE_CASE_PAIR_COUNTS,
                }
                if (
                    set(evidence_payload)
                    != {
                        "schema_version",
                        "evidence_type",
                        "ok",
                        "commit",
                        "all_original_final_pairs_opened",
                        "original_final_pair_count",
                        "case_pair_counts",
                        "pairs",
                    }
                    or
                    evidence_payload.get("ok") is not True
                    or evidence_payload.get("commit") != commit
                    or evidence_payload.get("all_original_final_pairs_opened") is not True
                    or not _int_equals(evidence_payload.get("original_final_pair_count"), 8)
                    or evidence_payload.get("case_pair_counts") != NATIVE_CASE_PAIR_COUNTS
                    or observed_counts != NATIVE_CASE_PAIR_COUNTS
                    or not pairs_valid
                ):
                    findings.append(
                        _finding(
                            "native-review-content",
                            f"{path}.evidence",
                            "native review registry must be commit-bound and terminal for every pair",
                        )
                    )
                _exact_details(
                    findings,
                    path=path,
                    details=details,
                    expected=expected_details,
                    code="native-review-details-binding",
                )
        elif gate_name == "upstream_visualization":
            records = _payloads(
                parsed_evidence, evidence_type="upstream-visualization-validation"
            )
            if len(records) != 1 or len(parsed_evidence) != 1:
                findings.append(
                    _finding(
                        "upstream-evidence",
                        f"{path}.evidence",
                        "exactly one upstream validation JSON is required",
                    )
                )
            else:
                evidence_payload = records[0]["payload"]
                expected_details = {
                    "pinned_commit": evidence_payload.get("pinned_commit"),
                    "content_sha256": evidence_payload.get("content_sha256"),
                    "strict_validation": evidence_payload.get("strict_validation"),
                    "real_visium_recipe_executed": evidence_payload.get(
                        "real_visium_recipe_executed"
                    ),
                    "source_object_warning_safe": evidence_payload.get(
                        "source_object_warning_safe"
                    ),
                    "native_visual_review": evidence_payload.get(
                        "native_visual_review"
                    ),
                    "recipe_validation_evidence_sha256": evidence_payload.get(
                        "recipe_validation_evidence_sha256"
                    ),
                    "source_warning_evidence_sha256": evidence_payload.get(
                        "source_warning_evidence_sha256"
                    ),
                }
                visium_warning_sha = (
                    gates.get("local_visium_mouse_brain", {})
                    .get("details", {})
                    .get("runtime_warning_evidence_sha256")
                )
                if (
                    evidence_payload.get("ok") is not True
                    or evidence_payload.get("commit") != commit
                    or not FULL_SHA.fullmatch(str(evidence_payload.get("pinned_commit", "")))
                    or not SHA256.fullmatch(str(evidence_payload.get("content_sha256", "")))
                    or evidence_payload.get("strict_validation") is not True
                    or evidence_payload.get("real_visium_recipe_executed") is not True
                    or evidence_payload.get("source_object_warning_safe") is not True
                    or evidence_payload.get("native_visual_review") is not True
                    or not SHA256.fullmatch(
                        str(evidence_payload.get("recipe_validation_evidence_sha256", ""))
                    )
                    or evidence_payload.get("source_warning_evidence_sha256")
                    != visium_warning_sha
                    or not SHA256.fullmatch(str(visium_warning_sha or ""))
                ):
                    findings.append(
                        _finding(
                            "upstream-evidence-content",
                            f"{path}.evidence",
                            "upstream evidence must bind the release commit, pin, strict real-Visium execution, warning-safe source object, native review, and the local Visium warning evidence",
                        )
                    )
                _exact_details(
                    findings,
                    path=path,
                    details=details,
                    expected=expected_details,
                    code="upstream-details-binding",
                )
        elif gate_name == "anonymous_clone":
            records = _payloads(parsed_evidence, kind="anonymous-clone", evidence_type="anonymous-clone-validation")
            if len(records) != 1 or len(parsed_evidence) != 1:
                findings.append(
                    _finding(
                        "clone-evidence",
                        f"{path}.evidence",
                        "exactly one anonymous-clone validation JSON is required",
                    )
                )
            else:
                evidence_payload = records[0]["payload"]
                command_returncodes = evidence_payload.get("command_returncodes")
                canonical_hashes = evidence_payload.get("canonical_summary_sha256")
                bundle_hashes = evidence_payload.get("bundle_verification_sha256")
                expected_details = {
                    "remote_url": EXPECTED_CLONE_REMOTE,
                    "anonymous": True,
                    "credentials_used": False,
                    "head_sha": commit,
                    "command_returncodes": command_returncodes,
                    "canonical_summary_sha256": canonical_hashes,
                    "bundle_verification_sha256": bundle_hashes,
                    "git_status_porcelain": "",
                }
                exact_payload_keys = {
                    "schema_version",
                    "evidence_type",
                    "ok",
                    "commit",
                    *expected_details.keys(),
                }
                if (
                    set(evidence_payload) != exact_payload_keys
                    or
                    evidence_payload.get("ok") is not True
                    or evidence_payload.get("commit") != commit
                    or evidence_payload.get("remote_url") != EXPECTED_CLONE_REMOTE
                    or evidence_payload.get("anonymous") is not True
                    or evidence_payload.get("credentials_used") is not False
                    or evidence_payload.get("head_sha") != commit
                    or not isinstance(command_returncodes, dict)
                    or set(command_returncodes) != CLONE_COMMAND_KEYS
                    or any(type(value) is not int or value != 0 for value in command_returncodes.values())
                    or not isinstance(canonical_hashes, dict)
                    or set(canonical_hashes) != CLONE_CASE_KEYS
                    or any(SHA256.fullmatch(str(value)) is None for value in canonical_hashes.values())
                    or not isinstance(bundle_hashes, dict)
                    or set(bundle_hashes) != CLONE_CASE_KEYS
                    or any(SHA256.fullmatch(str(value)) is None for value in bundle_hashes.values())
                    or evidence_payload.get("git_status_porcelain") != ""
                ):
                    findings.append(
                        _finding(
                            "clone-evidence-content",
                            f"{path}.evidence",
                            "anonymous clone evidence must be commit-bound with every check passed",
                        )
                    )
                _exact_details(
                    findings,
                    path=path,
                    details=details,
                    expected=expected_details,
                    code="clone-details-binding",
                )
        elif gate_name == "license_and_leak_scan":
            bindings = _payloads(
                parsed_evidence, evidence_type="license-and-leak-scan-binding"
            )
            reports = [
                record
                for record in parsed_evidence
                if isinstance(record.get("payload"), dict)
                and record["payload"].get("root") == "biomedical-analysis-agent"
                and "files_scanned" in record["payload"]
                and "findings" in record["payload"]
            ]
            if len(bindings) != 1 or len(reports) != 1 or len(parsed_evidence) != 2:
                findings.append(
                    _finding(
                        "distribution-evidence-set",
                        f"{path}.evidence",
                        "exactly one distribution report and one commit binding JSON are required",
                    )
                )
            else:
                binding = bindings[0]["payload"]
                report_record = reports[0]
                report = report_record["payload"]
                expected_details = {
                    "distribution_ok": report.get("ok"),
                    "distribution_report_sha256": report_record["sha256"],
                }
                if (
                    binding.get("ok") is not True
                    or binding.get("commit") != commit
                    or binding.get("distribution_ok") is not True
                    or binding.get("distribution_report_sha256") != report_record["sha256"]
                    or report.get("ok") is not True
                    or report.get("findings") != []
                    or not _positive_int(report.get("files_scanned"))
                ):
                    findings.append(
                        _finding(
                            "distribution-evidence-content",
                            f"{path}.evidence",
                            "distribution evidence must be clean, hash-bound, and release-commit bound",
                        )
                    )
                _exact_details(
                    findings,
                    path=path,
                    details=details,
                    expected=expected_details,
                    code="distribution-details-binding",
                )

    return {
        "schema_version": "1.0.0",
        "ok": not findings,
        "release_version": version,
        "release_commit": commit,
        "require_passed": require_passed,
        "required_gate_count": len(REQUIRED_GATES),
        "findings": findings,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--expected-version")
    parser.add_argument("--expected-commit")
    parser.add_argument("--repository-root", type=Path, default=ROOT)
    parser.add_argument("--allow-pending", action="store_true")
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload = json.loads(args.evidence.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("evidence root must be an object")
        report = validate_evidence(
            payload,
            expected_version=args.expected_version,
            expected_commit=args.expected_commit,
            require_passed=not args.allow_pending,
            repository_root=args.repository_root,
        )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        sys.stderr.write(f"RELEASE_EVIDENCE_ERROR: {exc}\n")
        return 2
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8", newline="\n")
    else:
        sys.stdout.write(rendered)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
