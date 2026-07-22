#!/usr/bin/env python3
"""Create, validate, transition, and checkpoint an auditable analysis run.

This module owns run metadata only.  It never installs packages and never chooses a
scientific method.  Analysis execution is delegated to frozen scripts and the
EnvironmentManager after the compiled plan has no blocking gates.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


SKILL_ROOT = Path(__file__).resolve().parents[1]
ANALYSIS_AGENT_PATH = SKILL_ROOT / "scripts" / "analysis_agent.py"
SPEC = importlib.util.spec_from_file_location("biomedical_analysis_agent", ANALYSIS_AGENT_PATH)
ANALYSIS_AGENT = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(ANALYSIS_AGENT)

LINEAR_STATES = tuple(ANALYSIS_AGENT.STATE_MACHINE)
TERMINAL_STATES = {"DELIVERED", "BLOCKED_INPUT", "BLOCKED_ENV", "FAILED_STAGE", "BLOCKED_QA", "CANCELLED"}
DELIVERY_READY_STAGE_STATUSES = {"completed", "checkpointed", "skipped"}
EXTRA_TRANSITIONS = {
    "CHECKPOINTED": {"RUNNING_STAGE", "ANALYSIS_QA"},
    "AWAITING_AUTHORIZATION": {"ENV_PREPARING", "BLOCKED_INPUT", "CANCELLED"},
    "ENV_PREPARING": {"BLOCKED_ENV", "ENV_LOCKED"},
    "RUNNING_STAGE": {"FAILED_STAGE", "STAGE_VALIDATING"},
    "STAGE_VALIDATING": {"FAILED_STAGE", "CHECKPOINTED"},
    "ANALYSIS_QA": {"BLOCKED_QA", "VISUALIZING", "INTERPRETING"},
    "NATIVE_VISUAL_REVIEW": {"VISUALIZING", "BLOCKED_QA", "INTERPRETING"},
}


class RunError(RuntimeError):
    """Raised when a run-state or artifact invariant is violated."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def input_record(path: Path) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        raise RunError(f"Input does not exist: {resolved}")
    if resolved.is_file():
        return {
            "path": str(resolved),
            "kind": "file",
            "size": resolved.stat().st_size,
            "sha256": sha256_file(resolved),
        }
    files = []
    tree_digest = hashlib.sha256()
    for child in sorted((item for item in resolved.rglob("*") if item.is_file()), key=lambda item: item.relative_to(resolved).as_posix().casefold()):
        relative = child.relative_to(resolved).as_posix()
        digest = sha256_file(child)
        size = child.stat().st_size
        files.append({"relative_path": relative, "size": size, "sha256": digest})
        tree_digest.update(stable_json(files[-1]).encode("utf-8"))
    return {
        "path": str(resolved),
        "kind": "directory",
        "file_count": len(files),
        "size": sum(item["size"] for item in files),
        "sha256": tree_digest.hexdigest(),
        "files": files,
    }


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise RunError(f"Expected a JSON object: {path}")
    return value


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _safe_relative(root: Path, candidate: Path) -> Path:
    resolved_root = root.resolve()
    resolved = candidate.resolve()
    try:
        return resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise RunError(f"Path escapes run root: {resolved}") from exc


def _design_markdown(request: dict[str, Any], plan: dict[str, Any]) -> str:
    route_lines = "\n".join(
        f"- `{route['capability']}` via `{route['skill']}` (score {route['score']}; evidence: {', '.join(route['evidence'])})"
        for route in plan["routes"]
    )
    gate_lines = "\n".join(
        f"- `{gate['id']}`: **{gate['status']}** — {gate['message']}"
        for gate in plan["scientific_gates"]
    )
    return (
        "# Analysis design\n\n"
        f"- Plan: `{plan['plan_id']}`\n"
        f"- Mode: `{plan['mode']}`\n"
        f"- Question: {request['question']}\n"
        f"- Statistical unit: {request.get('statistical_unit', 'UNRESOLVED')}\n\n"
        "## Routed capabilities\n\n"
        f"{route_lines}\n\n"
        "## Scientific gates\n\n"
        f"{gate_lines}\n\n"
        "## Claim boundary\n\n"
        "This compiled design does not itself establish a biological or clinical conclusion. "
        "Only validated artifacts from the frozen workflow may support claims.\n"
    )


def _figure_notes_template() -> str:
    return """# Figure notes

For every final figure record:

- research question;
- data and independent statistical unit;
- directly visible pattern;
- supported and unsupported conclusions;
- statistical and method assumptions;
- scientific-semantic and visual QA decisions;
- reproduction class and provenance.
"""


def initialise_run(request: dict[str, Any]) -> dict[str, Any]:
    plan = ANALYSIS_AGENT.compile_plan(request)
    run_root = Path(plan["run"]["root"]).expanduser().resolve()
    project_root = Path(str(request.get("project_root") or ".")).expanduser().resolve()
    expected_parent = (project_root / "runs").resolve()
    try:
        run_root.relative_to(expected_parent)
    except ValueError as exc:
        raise RunError(f"Compiled run root is outside <project>/runs: {run_root}") from exc
    if run_root.exists():
        raise RunError(f"Refusing to overwrite an existing run: {run_root}")

    input_paths = [Path(str(value)) for value in request.get("inputs", [])]
    input_records = [input_record(path) for path in input_paths]
    for directory in plan["output_contract"]["directories"].values():
        Path(directory).mkdir(parents=True, exist_ok=False)
    (run_root / "_staging").mkdir()

    _write_text(run_root / "00_request" / "intent.yaml", json.dumps(request, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    _write_json(
        run_root / "00_request" / "input_manifest.json",
        {"schema_version": "1.0", "created_at": utc_now(), "source_policy": "read_only", "inputs": input_records},
    )
    _write_text(run_root / "01_plan" / "ANALYSIS_DESIGN.md", _design_markdown(request, plan))
    _write_text(run_root / "01_plan" / "workflow.plan.yaml", json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    _write_json(
        run_root / "02_environment" / "environment_manifest.json",
        {"schema_version": "1.0", "state": "not_prepared", "environments": [], "global_changes": False},
    )
    _write_text(run_root / "03_scripts" / "params.yaml", "{}\n")
    _write_text(run_root / "07_reports" / "FIGURE_NOTES.md", _figure_notes_template())
    _write_text(run_root / "07_reports" / "QA_REPORT.md", "# QA report\n\nStatus: pending\n")
    _write_text(run_root / "07_reports" / "ARTIFACT_INDEX.md", "# Artifact index\n\nNo promoted result artifacts yet.\n")
    _write_text(run_root / "manifest" / "artifact_ledger.jsonl", "")

    state = plan["state"]
    input_contracts = []
    for index, record in enumerate(input_records, start=1):
        input_contracts.append(
            {
                "artifact_id": f"input-{index:03d}",
                "artifact_type": "input",
                "format": "directory" if record["kind"] == "directory" else (Path(record["path"]).suffix.lstrip(".") or "binary"),
                "schema": None,
                "unit": request.get("statistical_unit"),
                "modality": request.get("modality") if isinstance(request.get("modality"), str) else None,
                "producer": "external-read-only-input",
                "consumers": [node["node_id"] for node in plan["workflow"]["nodes"] if node["node_id"].endswith("data-profile")],
                "relative_path": f"00_request/input_manifest.json#inputs/{index - 1}",
                "sha256": record["sha256"],
                "sensitivity": str(request.get("input_sensitivity", "sensitive")),
                "validation": ["source exists", "sha256 recorded"],
                "claim_role": "none",
            }
        )
    run_manifest = {
        "run_id": plan["run"]["run_id"],
        "plan_id": plan["plan_id"],
        "mode": plan["mode"],
        "state": state,
        "request_sha256": plan["request_sha256"],
        "inputs": input_contracts,
        "environments": [],
        "stages": [dict(node, status="pending") for node in plan["workflow"]["nodes"]],
        "commands": [],
        "seeds": {},
        "retry_history": [],
        "exit_code": None,
        "artifacts": [],
        "qa": [],
        "claim_boundaries": ["Compiled plan only; no biological or clinical conclusion has been established."],
    }
    _write_json(run_root / "manifest" / "run_manifest.json", run_manifest)
    return {"ok": True, "run_root": str(run_root), "state": state, "plan_id": plan["plan_id"], "blocking_issues": plan["blocking_issues"]}


def transition_run(run_root: Path, new_state: str) -> dict[str, Any]:
    run_root = run_root.resolve()
    manifest_path = run_root / "manifest" / "run_manifest.json"
    manifest = _load_json(manifest_path)
    current = str(manifest["state"])
    if current in TERMINAL_STATES:
        raise RunError(f"Terminal state cannot transition: {current}")
    allowed = set(EXTRA_TRANSITIONS.get(current, set()))
    if current in LINEAR_STATES:
        index = LINEAR_STATES.index(current)
        if index + 1 < len(LINEAR_STATES):
            allowed.add(LINEAR_STATES[index + 1])
    if new_state not in allowed:
        raise RunError(f"Invalid state transition: {current} -> {new_state}; allowed={sorted(allowed)}")
    manifest["state"] = new_state
    manifest.setdefault("qa", []).append({"kind": "state-transition", "from": current, "to": new_state, "at": utc_now()})
    _write_json(manifest_path, manifest)
    return {"ok": True, "from": current, "to": new_state}


def _artifact_paths(run_root: Path, contracts: Iterable[dict[str, Any]], prefix: str) -> list[tuple[dict[str, Any], Path]]:
    checked = []
    for contract in contracts:
        relative = str(contract.get("relative_path", ""))
        if not relative or Path(relative).is_absolute() or ":" in Path(relative).drive:
            raise RunError(f"Artifact path must be relative: {relative}")
        normalized = Path(relative).as_posix()
        if not normalized.startswith(prefix.rstrip("/") + "/"):
            raise RunError(f"Artifact is outside expected staging prefix {prefix}: {relative}")
        absolute = run_root / Path(relative)
        _safe_relative(run_root, absolute)
        if not absolute.is_file():
            raise RunError(f"Declared artifact is missing or not a file: {absolute}")
        expected = contract.get("sha256")
        actual = sha256_file(absolute)
        if expected and expected != actual:
            raise RunError(f"Artifact hash mismatch: {relative}")
        checked.append((dict(contract, sha256=actual), absolute))
    if not checked:
        raise RunError("A checkpoint requires at least one declared artifact.")
    return checked


def checkpoint_stage(run_root: Path, stage_id: str, contract_path: Path) -> dict[str, Any]:
    run_root = run_root.resolve()
    if not stage_id or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for char in stage_id):
        raise RunError(f"Unsafe stage id: {stage_id}")
    contract_payload = json.loads(contract_path.read_text(encoding="utf-8-sig"))
    contracts = contract_payload.get("artifacts") if isinstance(contract_payload, dict) else contract_payload
    if not isinstance(contracts, list):
        raise RunError("Contract must be a JSON array or {'artifacts': [...]}")
    staging_prefix = f"_staging/{stage_id}"
    staging_dir = run_root / "_staging" / stage_id
    target_dir = run_root / "04_intermediate" / stage_id
    if not staging_dir.is_dir():
        raise RunError(f"Staging directory does not exist: {staging_dir}")
    if target_dir.exists():
        raise RunError(f"Checkpoint target already exists: {target_dir}")
    checked = _artifact_paths(run_root, contracts, staging_prefix)

    manifest_path = run_root / "manifest" / "run_manifest.json"
    manifest = _load_json(manifest_path)
    checkpoint_origin_state = manifest["state"]
    if checkpoint_origin_state not in {"RUNNING_STAGE", "STAGE_VALIDATING", "VISUALIZING"}:
        raise RunError(f"Cannot checkpoint from state {manifest['state']}")
    manifest["state"] = "STAGE_VALIDATING"
    _write_json(manifest_path, manifest)

    target_dir.parent.mkdir(parents=True, exist_ok=True)
    os.replace(staging_dir, target_dir)
    promoted = []
    for contract, old_path in checked:
        suffix = old_path.relative_to(staging_dir)
        new_path = target_dir / suffix
        updated = dict(contract, relative_path=new_path.relative_to(run_root).as_posix())
        promoted.append(updated)

    ledger_path = run_root / "manifest" / "artifact_ledger.jsonl"
    with ledger_path.open("a", encoding="utf-8", newline="\n") as handle:
        for artifact in promoted:
            handle.write(stable_json({"event": "checkpoint-promoted", "stage_id": stage_id, "at": utc_now(), "artifact": artifact}) + "\n")

    manifest = _load_json(manifest_path)
    result_state = "VISUALIZING" if checkpoint_origin_state == "VISUALIZING" else "CHECKPOINTED"
    manifest["state"] = result_state
    manifest["artifacts"].extend(promoted)
    for stage in manifest["stages"]:
        if stage.get("node_id") == stage_id or str(stage.get("node_id", "")).endswith("-" + stage_id):
            stage["status"] = "checkpointed"
    _write_json(manifest_path, manifest)
    return {"ok": True, "stage_id": stage_id, "state": result_state, "artifacts": len(promoted), "target": str(target_dir)}


def validate_run(run_root: Path, verify_hashes: bool = True) -> dict[str, Any]:
    run_root = run_root.resolve()
    errors = []
    plan_path = run_root / "01_plan" / "workflow.plan.yaml"
    manifest_path = run_root / "manifest" / "run_manifest.json"
    if not plan_path.is_file():
        errors.append("missing:01_plan/workflow.plan.yaml")
        plan = None
    else:
        try:
            plan = _load_json(plan_path)
        except (OSError, json.JSONDecodeError, RunError) as exc:
            errors.append(f"invalid-plan:{exc}")
            plan = None
    if not manifest_path.is_file():
        errors.append("missing:manifest/run_manifest.json")
        manifest = None
    else:
        try:
            manifest = _load_json(manifest_path)
        except (OSError, json.JSONDecodeError, RunError) as exc:
            errors.append(f"invalid-manifest:{exc}")
            manifest = None
    if plan:
        for relative in plan["output_contract"]["required_files"]:
            if not (run_root / relative).is_file():
                errors.append(f"missing:{relative}")
    if manifest and verify_hashes:
        for artifact in manifest.get("artifacts", []):
            relative = artifact.get("relative_path", "")
            path = run_root / relative
            try:
                _safe_relative(run_root, path)
            except RunError as exc:
                errors.append(str(exc))
                continue
            if not path.is_file():
                errors.append(f"missing-artifact:{relative}")
            elif artifact.get("sha256") != sha256_file(path):
                errors.append(f"hash-mismatch:{relative}")
    if manifest:
        ledger_path = run_root / "manifest" / "artifact_ledger.jsonl"
        manifest_artifacts: dict[str, dict[str, Any]] = {}
        for artifact in manifest.get("artifacts", []):
            relative = str(artifact.get("relative_path", ""))
            if not relative:
                errors.append("manifest-artifact-missing-relative-path")
                continue
            if relative in manifest_artifacts:
                errors.append(f"duplicate-manifest-artifact:{relative}")
            manifest_artifacts[relative] = artifact
        ledger_artifacts: dict[str, tuple[str, int]] = {}
        if not ledger_path.is_file():
            errors.append("missing:manifest/artifact_ledger.jsonl")
        else:
            with ledger_path.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError as exc:
                        errors.append(f"invalid-ledger-json:{line_number}:{exc.msg}")
                        continue
                    if event.get("event") not in {"checkpoint-promoted", "artifact-registered"}:
                        continue
                    artifact = event.get("artifact")
                    if not isinstance(artifact, dict):
                        errors.append(f"ledger-artifact-missing:{line_number}")
                        continue
                    relative = str(artifact.get("relative_path", ""))
                    digest = str(artifact.get("sha256", ""))
                    if not relative or not digest:
                        errors.append(f"ledger-artifact-incomplete:{line_number}")
                        continue
                    if relative in ledger_artifacts:
                        errors.append(f"duplicate-ledger-artifact:{relative}:lines-{ledger_artifacts[relative][1]}-{line_number}")
                    else:
                        ledger_artifacts[relative] = (digest, line_number)
                    path = run_root / relative
                    try:
                        _safe_relative(run_root, path)
                    except RunError as exc:
                        errors.append(str(exc))
                        continue
                    if not path.is_file():
                        errors.append(f"missing-ledger-artifact:{relative}")
                    elif verify_hashes and digest != sha256_file(path):
                        errors.append(f"ledger-hash-mismatch:{relative}")
        for relative, artifact in manifest_artifacts.items():
            registered = ledger_artifacts.get(relative)
            if registered is None:
                errors.append(f"manifest-artifact-missing-ledger-event:{relative}")
            elif str(artifact.get("sha256", "")) != registered[0]:
                errors.append(f"manifest-ledger-hash-mismatch:{relative}")
        for relative in sorted(set(ledger_artifacts) - set(manifest_artifacts)):
            errors.append(f"ledger-artifact-missing-manifest:{relative}")
        if manifest.get("state") == "DELIVERED":
            if manifest.get("exit_code") != 0:
                errors.append("delivered-run-nonzero-or-missing-exit-code")
            stages = manifest.get("stages", [])
            if not isinstance(stages, list) or not stages:
                errors.append("delivered-run-missing-stages")
                stages = []
            for stage in stages:
                node_id = str(stage.get("node_id", "unknown-stage"))
                status = str(stage.get("status", "pending"))
                if status not in DELIVERY_READY_STAGE_STATUSES:
                    errors.append(f"delivered-stage-not-complete:{node_id}:{status}")
                if stage.get("checkpoint_required") is True and status != "checkpointed":
                    errors.append(f"delivered-required-stage-not-checkpointed:{node_id}:{status}")

            stage_ids = {str(stage.get("node_id", "")) for stage in stages}
            if any(node_id.endswith("native-visual-review") for node_id in stage_ids):
                review_relative = "06_figures/review/native-visual-review.json"
                review_path = run_root / review_relative
                if review_relative not in manifest_artifacts:
                    errors.append(f"delivered-native-review-not-registered:{review_relative}")
                if not review_path.is_file():
                    errors.append(f"delivered-native-review-missing:{review_relative}")
                else:
                    try:
                        review = _load_json(review_path)
                    except (OSError, json.JSONDecodeError, RunError) as exc:
                        errors.append(f"delivered-native-review-invalid:{exc}")
                    else:
                        if review.get("reviewed_native_pixels") is not True or review.get("review_decision") not in {"PASS", "PASS_WITH_BOUNDARIES"}:
                            errors.append("delivered-native-review-not-accepted")
                        for figure in review.get("figures", []):
                            relative = str(figure.get("relative_path", ""))
                            path = run_root / relative
                            try:
                                _safe_relative(run_root, path)
                            except RunError as exc:
                                errors.append(str(exc))
                                continue
                            if not relative.startswith("06_figures/final/"):
                                errors.append(f"delivered-native-review-path-not-formal:{relative}")
                            elif not path.is_file():
                                errors.append(f"delivered-native-review-figure-missing:{relative}")
                            elif verify_hashes and figure.get("sha256") != sha256_file(path):
                                errors.append(f"delivered-native-review-figure-hash-mismatch:{relative}")

            if any(node_id.endswith("interpretation") for node_id in stage_ids):
                notes_relative = "07_reports/FIGURE_NOTES.md"
                notes_path = run_root / notes_relative
                if notes_relative not in manifest_artifacts:
                    errors.append(f"delivered-final-figure-notes-not-registered:{notes_relative}")
                if not notes_path.is_file():
                    errors.append(f"delivered-final-figure-notes-missing:{notes_relative}")
                else:
                    notes = notes_path.read_text(encoding="utf-8")
                    if "Native visual review: pending" in notes:
                        errors.append("delivered-final-figure-notes-review-pending")
    return {"ok": not errors, "run_root": str(run_root), "verify_hashes": verify_hashes, "errors": errors}


def _verify_environment_lock_evidence(run_root: Path, environment: dict[str, Any]) -> list[str]:
    """Verify copied lock evidence rather than trusting a manifest declaration.

    Resume must remain possible after an in-memory EnvironmentHandle disappears. Every
    declared frozen environment therefore points to immutable evidence copied under
    ``02_environment``. A bare ``state=frozen`` or arbitrary lock hash is insufficient.
    """

    errors: list[str] = []
    state = str(environment.get("state", "")).casefold()
    if state not in {"locked", "frozen", "env_locked"}:
        return ["environment-lock-not-verified"]
    declared = environment.get("environments", [])
    if not isinstance(declared, list) or not declared:
        return ["environment-lock-not-verified"]
    evidence_root = (run_root / "02_environment").resolve()
    for index, item in enumerate(declared):
        prefix = f"environment[{index}]"
        if not isinstance(item, dict) or item.get("frozen") is not True:
            errors.append(f"{prefix}-not-frozen")
            continue
        lock_hash = str(item.get("lock_hash") or "")
        if not re.fullmatch(r"[0-9a-f]{64}", lock_hash):
            errors.append(f"{prefix}-invalid-lock-hash")
        marker_relative = str(item.get("marker_relative_path") or "")
        marker_sha = str(item.get("marker_sha256") or "")
        if not marker_relative or not re.fullmatch(r"[0-9a-f]{64}", marker_sha):
            errors.append(f"{prefix}-marker-evidence-missing")
        else:
            marker_path = run_root / marker_relative
            try:
                relative = _safe_relative(run_root, marker_path)
                relative.relative_to(Path("02_environment"))
            except (RunError, ValueError):
                errors.append(f"{prefix}-marker-outside-environment-directory")
            else:
                if not marker_path.is_file():
                    errors.append(f"{prefix}-marker-missing")
                elif sha256_file(marker_path) != marker_sha:
                    errors.append(f"{prefix}-marker-hash-mismatch")
                else:
                    try:
                        marker = _load_json(marker_path)
                    except (OSError, json.JSONDecodeError, RunError) as exc:
                        errors.append(f"{prefix}-marker-invalid:{exc}")
                    else:
                        for field in ("lock_hash", "platform", "backend"):
                            if str(marker.get(field) or "") != str(item.get(field) or ""):
                                errors.append(f"{prefix}-marker-{field}-mismatch")
        lockfiles = item.get("lockfiles", [])
        if not isinstance(lockfiles, list) or not lockfiles:
            errors.append(f"{prefix}-lockfile-evidence-missing")
            continue
        for lock_index, lockfile in enumerate(lockfiles):
            lock_prefix = f"{prefix}-lockfile[{lock_index}]"
            if not isinstance(lockfile, dict):
                errors.append(f"{lock_prefix}-invalid")
                continue
            relative_path = str(lockfile.get("relative_path") or "")
            expected_sha = str(lockfile.get("sha256") or "")
            path = run_root / relative_path
            try:
                relative = _safe_relative(run_root, path)
                relative.relative_to(Path("02_environment"))
            except (RunError, ValueError):
                errors.append(f"{lock_prefix}-outside-environment-directory")
                continue
            if not path.is_file():
                errors.append(f"{lock_prefix}-missing")
            elif not re.fullmatch(r"[0-9a-f]{64}", expected_sha) or sha256_file(path) != expected_sha:
                errors.append(f"{lock_prefix}-hash-mismatch")
    if errors and "environment-lock-not-verified" not in errors:
        errors.insert(0, "environment-lock-not-verified")
    return errors


def audit_resume(run_root: Path, verify_input_hashes: bool = True) -> dict[str, Any]:
    """Identify the latest valid checkpoint without mutating the prior run."""
    run_root = run_root.resolve()
    validation = validate_run(run_root, verify_hashes=True)
    errors = list(validation["errors"])
    manifest = _load_json(run_root / "manifest" / "run_manifest.json")
    input_manifest = _load_json(run_root / "00_request" / "input_manifest.json")
    environment = _load_json(run_root / "02_environment" / "environment_manifest.json")

    if verify_input_hashes:
        for index, prior in enumerate(input_manifest.get("inputs", [])):
            try:
                current = input_record(Path(prior["path"]))
            except (OSError, RunError, KeyError) as exc:
                errors.append(f"input[{index}]-unavailable:{exc}")
                continue
            if current.get("sha256") != prior.get("sha256"):
                errors.append(f"input[{index}]-hash-changed")

    environment_errors = _verify_environment_lock_evidence(run_root, environment)
    environment_locked = not environment_errors
    errors.extend(environment_errors)

    checkpointed = [stage for stage in manifest.get("stages", []) if stage.get("status") == "checkpointed"]
    if not checkpointed:
        errors.append("no-validated-checkpoint")
        latest = None
    else:
        latest = checkpointed[-1].get("node_id")
        checkpoint_dir = run_root / "04_intermediate" / str(latest).split("-", 1)[-1]
        if not checkpoint_dir.is_dir():
            matching = [path for path in (run_root / "04_intermediate").iterdir() if path.is_dir() and str(latest).endswith("-" + path.name)]
            if not matching:
                errors.append(f"checkpoint-directory-missing:{latest}")

    return {
        "ok": not errors,
        "run_root": str(run_root),
        "read_only": True,
        "verify_input_hashes": verify_input_hashes,
        "prior_state": manifest.get("state"),
        "latest_valid_checkpoint": latest,
        "environment_locked": environment_locked,
        "resume_requires_new_workflow_instance": True,
        "errors": errors,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    initialise = commands.add_parser("init")
    initialise.add_argument("--request", type=Path, required=True)
    transition = commands.add_parser("transition")
    transition.add_argument("--run-root", type=Path, required=True)
    transition.add_argument("--state", required=True)
    checkpoint = commands.add_parser("checkpoint")
    checkpoint.add_argument("--run-root", type=Path, required=True)
    checkpoint.add_argument("--stage-id", required=True)
    checkpoint.add_argument("--contract", type=Path, required=True)
    validate = commands.add_parser("validate")
    validate.add_argument("--run-root", type=Path, required=True)
    validate.add_argument("--no-verify-hashes", action="store_true")
    resume = commands.add_parser("resume-audit")
    resume.add_argument("--run-root", type=Path, required=True)
    resume.add_argument("--no-verify-input-hashes", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "init":
            result = initialise_run(_load_json(args.request))
        elif args.command == "transition":
            result = transition_run(args.run_root, args.state)
        elif args.command == "checkpoint":
            result = checkpoint_stage(args.run_root, args.stage_id, args.contract)
        elif args.command == "validate":
            result = validate_run(args.run_root, not args.no_verify_hashes)
        else:
            result = audit_resume(args.run_root, not args.no_verify_input_hashes)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if result.get("ok") else 2
    except (OSError, json.JSONDecodeError, RunError, ANALYSIS_AGENT.RequestError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
