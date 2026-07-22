#!/usr/bin/env python3
"""Run one release tutorial from a credential-free public clone.

This helper is intentionally stdlib-only. It keeps every generated file outside
the cloned repository, proves fresh/resume/cache and negative-control behavior,
and emits the two hashes required by the anonymous-clone release gate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path


FULL_SHA = re.compile(r"[0-9a-f]{40}")
CASES = ("pbmc3k", "visium-mouse-brain")


class AnonymousCaseError(RuntimeError):
    """Raised when one fail-closed anonymous tutorial check fails."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def sanitized_environment() -> dict[str, str]:
    environment = dict(os.environ)
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "GIT_ASKPASS", "SSH_ASKPASS"):
        environment.pop(name, None)
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["PYTHONIOENCODING"] = "utf-8"
    return environment


def run_command(
    command: list[str],
    *,
    cwd: Path,
    label: str,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    print(f"ANONYMOUS_CASE_STEP: {label}", flush=True)
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=sanitized_environment(),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=capture,
        check=False,
    )
    if completed.returncode != 0:
        if capture:
            if completed.stdout:
                sys.stderr.write(completed.stdout)
            if completed.stderr:
                sys.stderr.write(completed.stderr)
        raise AnonymousCaseError(
            f"{label} failed with exit code {completed.returncode}"
        )
    return completed


def assert_json_object(path: Path, label: str) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AnonymousCaseError(f"{label} is not readable JSON: {path.name}") from exc
    if not isinstance(payload, dict):
        raise AnonymousCaseError(f"{label} JSON root is not an object")
    return payload


def capture_bootstrap(
    clone_root: Path,
    evidence_root: Path,
    task_skills: Path,
) -> None:
    install = run_command(
        [
            sys.executable,
            "bootstrap_skills.py",
            "--destination",
            str(task_skills),
        ],
        cwd=clone_root,
        label="task-local skill bootstrap",
        capture=True,
    )
    install_path = evidence_root / "bootstrap-install.json"
    install_path.write_text(install.stdout, encoding="utf-8", newline="\n")
    if assert_json_object(install_path, "bootstrap install").get("ok") is not True:
        raise AnonymousCaseError("bootstrap install did not report ok=true")

    verify = run_command(
        [
            sys.executable,
            "bootstrap_skills.py",
            "--destination",
            str(task_skills),
            "--verify-only",
        ],
        cwd=clone_root,
        label="task-local skill bootstrap verification",
        capture=True,
    )
    verify_path = evidence_root / "bootstrap-verify.json"
    verify_path.write_text(verify.stdout, encoding="utf-8", newline="\n")
    if assert_json_object(verify_path, "bootstrap verification").get("ok") is not True:
        raise AnonymousCaseError("bootstrap verification did not report ok=true")


def snapshot_frozen_files(run_root: Path) -> tuple[dict[str, dict], int, int]:
    intermediate = run_root / "04_intermediate"
    results = run_root / "05_results"
    checkpoints = sorted(
        path
        for path in intermediate.rglob("*")
        if path.is_file() and path.name in {"stage.complete.json", "_checkpoint.json"}
    )
    analysis = sorted(path for path in results.rglob("*") if path.is_file())
    if not checkpoints or not analysis:
        raise AnonymousCaseError(
            "fresh run lacks checkpoint or analysis artifacts required for resume proof"
        )
    records: dict[str, dict] = {}
    for path in checkpoints + analysis:
        relative = path.relative_to(run_root).as_posix()
        records[relative] = {
            "sha256": sha256_file(path),
            "last_write_time_ns": path.stat().st_mtime_ns,
        }
    return records, len(checkpoints), len(analysis)


def assert_snapshot_unchanged(run_root: Path, before: dict[str, dict]) -> None:
    for relative, expected in before.items():
        path = run_root / Path(relative)
        if not path.is_file():
            raise AnonymousCaseError(f"resume removed a frozen artifact: {relative}")
        if (
            sha256_file(path) != expected["sha256"]
            or path.stat().st_mtime_ns != expected["last_write_time_ns"]
        ):
            raise AnonymousCaseError(f"resume rewrote a frozen artifact: {relative}")


def count_checkpoint_reuse(case_id: str, run_root: Path) -> int:
    if case_id == "pbmc3k":
        path = run_root / "logs" / "r-pipeline.stderr.log"
        needle = "checkpoint reuse:"
    else:
        path = run_root / "logs" / "pipeline-resume.log"
        needle = "resume_reuse\t"
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        raise AnonymousCaseError("resume log is missing") from exc
    return text.count(needle)


def run_case(
    *,
    case_id: str,
    clone_root: Path,
    base_root: Path,
    commit: str,
    rscript: Path,
) -> dict:
    if case_id not in CASES:
        raise AnonymousCaseError(f"unsupported case: {case_id}")
    if not FULL_SHA.fullmatch(commit):
        raise AnonymousCaseError("commit must be a full lowercase Git SHA")
    clone_root = clone_root.resolve(strict=True)
    rscript = rscript.resolve(strict=True)
    base_root = base_root.resolve(strict=False)
    if (clone_root / ".git").is_dir() is False:
        raise AnonymousCaseError("clone root is not a Git checkout")
    if base_root.exists():
        raise AnonymousCaseError("anonymous case root must not already exist")

    head = run_command(
        ["git", "rev-parse", "HEAD"],
        cwd=clone_root,
        label="release commit identity",
        capture=True,
    ).stdout.strip()
    if head != commit:
        raise AnonymousCaseError(f"clone HEAD mismatch: expected {commit}, observed {head}")

    run_root = base_root / "run"
    cache_root = base_root / "cache"
    input_cache_root = cache_root / "inputs" / case_id
    evidence_root = base_root / "evidence"
    task_skills = base_root / "task-skills"
    bundle_root = base_root / "bundle"
    evidence_root.mkdir(parents=True)

    capture_bootstrap(clone_root, evidence_root, task_skills)

    unauthorized_root = base_root / "unauthorized"
    unauthorized = subprocess.run(
        [
            sys.executable,
            "tutorial_cli.py",
            "run",
            "--case",
            case_id,
            "--run-root",
            str(unauthorized_root),
        ],
        cwd=clone_root,
        env=sanitized_environment(),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if unauthorized.returncode == 0 or unauthorized_root.exists():
        raise AnonymousCaseError("unauthorized execution was not blocked before writes")
    write_json(
        evidence_root / "authorization-negative.json",
        {
            "schema_version": "1.0.0",
            "case": case_id,
            "check": "unauthorized_run_blocked_before_writes",
            "observed_returncode": unauthorized.returncode,
            "run_root_created": False,
            "ok": True,
        },
    )

    compiled_plan = evidence_root / "compiled-plan.json"
    run_command(
        [
            sys.executable,
            "tutorial_cli.py",
            "plan",
            "--case",
            case_id,
            "--output",
            str(compiled_plan),
        ],
        cwd=clone_root,
        label="frozen plan compilation",
    )

    common = [
        "--case",
        case_id,
        "--authorize-run",
        "--rscript",
        str(rscript),
        "--run-root",
        str(run_root),
        "--cache-root",
        str(cache_root),
        "--input-cache-root",
        str(input_cache_root),
    ]
    run_command(
        [sys.executable, "tutorial_cli.py", "run", *common],
        cwd=clone_root,
        label="fresh real-data tutorial",
    )

    bound_plan = run_root / "01_plan" / "root-compiled-plan.json"
    if sha256_file(compiled_plan) != sha256_file(bound_plan):
        raise AnonymousCaseError("executed plan differs from independently compiled plan")
    lock_path = run_root / "02_environment" / "renv.lock"
    fresh_lock_sha = sha256_file(lock_path)
    before, checkpoint_count, analysis_count = snapshot_frozen_files(run_root)

    run_command(
        [sys.executable, "tutorial_cli.py", "resume", *common],
        cwd=clone_root,
        label="checkpoint resume",
    )
    resume_lock_sha = sha256_file(lock_path)
    if resume_lock_sha != fresh_lock_sha:
        raise AnonymousCaseError("resume changed the frozen renv lock")
    assert_snapshot_unchanged(run_root, before)

    environment_reuse = assert_json_object(
        run_root / "logs" / "environment-cache-reuse.json",
        "environment cache-reuse evidence",
    )
    if (
        environment_reuse.get("reuse") is not True
        or environment_reuse.get("host_package_required") is not False
    ):
        raise AnonymousCaseError("environment cache-reuse evidence is incomplete")

    is_visium = case_id == "visium-mouse-brain"
    if is_visium:
        input_reuse = assert_json_object(
            run_root / "logs" / "input-cache-reuse.json",
            "Visium input cache-reuse evidence",
        )
        if (
            input_reuse.get("status") != "passed"
            or input_reuse.get("reuse") is not True
            or input_reuse.get("materialization") != "direct_read_no_copy"
            or input_reuse.get("cache_scope") != "task_local_external_to_run_root"
            or input_reuse.get("canonical_inputs_modified") is not False
        ):
            raise AnonymousCaseError("Visium input cache-reuse evidence is incomplete")

    reuse_records = count_checkpoint_reuse(case_id, run_root)
    if reuse_records < checkpoint_count:
        raise AnonymousCaseError(
            f"resume log proves only {reuse_records} reuses for {checkpoint_count} checkpoints"
        )
    plan_sha = sha256_file(compiled_plan)
    write_json(
        evidence_root / "resume-cache-evidence.json",
        {
            "schema_version": "1.0.0",
            "case": case_id,
            "check": "plan_bound_fresh_resume_shared_cache_and_immutable_artifacts",
            "fresh_returncode": 0,
            "resume_returncode": 0,
            "shared_cache_root": True,
            "explicit_environment_cache_reuse": True,
            "explicit_input_cache_reuse": is_visium,
            "input_cache_direct_read_no_copy": is_visium,
            "input_cache_external_to_run_root": is_visium,
            "canonical_inputs_modified": False,
            "task_local_bootstrap": True,
            "host_package_required": False,
            "compiled_plan_sha256": plan_sha,
            "executed_plan_sha256": sha256_file(bound_plan),
            "fresh_renv_lock_sha256": fresh_lock_sha,
            "resume_renv_lock_sha256": resume_lock_sha,
            "immutable_lock": True,
            "immutable_checkpoint_and_analysis_artifacts": True,
            "checkpoint_records": checkpoint_count,
            "analysis_artifacts": analysis_count,
            "checkpoint_reuse_log_records": reuse_records,
            "before_resume": before,
            "ok": True,
        },
    )

    if case_id == "pbmc3k":
        run_command(
            [
                sys.executable,
                "tutorial_cli.py",
                "report",
                "--case",
                case_id,
                "--run-root",
                str(run_root),
            ],
            cwd=clone_root,
            label="PBMC report finalization",
        )
    validation_summary = run_root / "manifest" / "ci-validation-summary.json"
    run_command(
        [
            sys.executable,
            "scripts/validate_tutorial_ci_output.py",
            "--case",
            case_id,
            "--run-root",
            str(run_root),
            "--output",
            str(validation_summary),
        ],
        cwd=clone_root,
        label="computational output validation",
    )

    checksum_path = evidence_root / "checksum-negative.json"
    run_command(
        [
            sys.executable,
            "scripts/inject_tutorial_checksum_failure.py",
            "--case",
            case_id,
            "--run-root",
            str(run_root),
            "--cache-root",
            str(cache_root),
            "--output",
            str(checksum_path),
        ],
        cwd=clone_root,
        label="input-checksum negative control",
    )
    checksum = assert_json_object(checksum_path, "checksum negative control")
    if (
        checksum.get("ok") is not True
        or checksum.get("failure_closed") is not True
        or checksum.get("failure_code") != "INPUT_CHECKSUM_MISMATCH_REJECTED"
        or checksum.get("observed_returncode") != 2
    ):
        raise AnonymousCaseError("checksum negative-control evidence is incomplete")

    failure_root = base_root / "nonzero"
    if case_id == "pbmc3k":
        failure_cache = base_root / "nonzero-cache"
        failed = subprocess.run(
            [
                sys.executable,
                "examples/pbmc3k/prepare_environment.py",
                "--run-root",
                str(failure_root),
                "--cache-root",
                str(failure_cache),
                "--rscript",
                str(rscript),
                "--authorized",
                "--inject-error-before-exit",
            ],
            cwd=clone_root,
            env=sanitized_environment(),
            check=False,
        )
        nonzero_code = failed.returncode
        if nonzero_code == 0:
            raise AnonymousCaseError("injected PBMC environment failure returned zero")
        negative = assert_json_object(
            failure_root / "logs" / "environment-negative-test.json",
            "PBMC environment negative control",
        )
        if (
            negative.get("status") != "pass"
            or negative.get("completion_marker_exists") is not False
            or negative.get("sentinel_observed") is not True
        ):
            raise AnonymousCaseError("PBMC environment negative control is incomplete")
        canonical_modified = False
    else:
        fault_evidence = failure_root / "fault-injection-evidence.json"
        run_command(
            [
                sys.executable,
                "examples/visium-mouse-brain/inject_environment_fault.py",
                "--canonical-run-root",
                str(run_root),
                "--failure-run-root",
                str(failure_root),
                "--cache-root",
                str(cache_root),
                "--input-cache-root",
                str(input_cache_root),
                "--rscript",
                str(rscript),
                "--output",
                str(fault_evidence),
            ],
            cwd=clone_root,
            label="Visium environment non-zero negative control",
        )
        negative = assert_json_object(fault_evidence, "Visium environment fault evidence")
        if (
            negative.get("status") != "passed"
            or negative.get("observed_native_returncode") == 0
            or negative.get("observed_wrapper_returncode") == 0
            or negative.get("completion_marker_absent") is not True
            or negative.get("dedicated_fault_sentinel_observed") is not True
            or negative.get("canonical_run_modified") is not False
        ):
            raise AnonymousCaseError("Visium environment negative control is incomplete")
        nonzero_code = int(negative["observed_wrapper_returncode"])
        canonical_modified = False
    write_json(
        evidence_root / "nonzero-negative.json",
        {
            "schema_version": "1.0.0",
            "case": case_id,
            "check": "nonzero_environment_exit_blocked",
            "observed_returncode": nonzero_code,
            "completion_marker_created": False,
            "dedicated_fault_sentinel_observed": True,
            "canonical_run_modified": canonical_modified,
            "ok": True,
        },
    )

    run_command(
        [
            sys.executable,
            ".github/scripts/tutorial_ci_artifact.py",
            "build",
            "--case",
            case_id,
            "--commit",
            commit,
            "--run-root",
            str(run_root),
            "--evidence-root",
            str(evidence_root),
            "--skills-lock",
            "skills.lock.json",
            "--output",
            str(bundle_root),
        ],
        cwd=clone_root,
        label="minimal evidence-bundle build",
    )
    verification_path = evidence_root / "bundle-verification.json"
    run_command(
        [
            sys.executable,
            str(bundle_root / "verify_tutorial_ci_artifact.py"),
            "verify",
            "--bundle-root",
            str(bundle_root),
            "--expected-case",
            case_id,
            "--expected-commit",
            commit,
            "--output-report",
            str(verification_path),
        ],
        cwd=clone_root,
        label="independent evidence-bundle verification",
    )
    verification = assert_json_object(verification_path, "bundle verification")
    if verification.get("ok") is not True:
        raise AnonymousCaseError("bundle verification did not report ok=true")
    result = {
        "schema_version": "1.0.0",
        "case": case_id,
        "returncode": 0,
        "canonical_summary_sha256": verification["canonical_summary_sha256"],
        "bundle_verification_sha256": sha256_file(verification_path),
        "checkpoint_reuse_records": reuse_records,
        "frozen_files_compared": len(before),
    }
    if is_visium:
        result["runtime_warning_evidence_sha256"] = verification[
            "runtime_warning_evidence_sha256"
        ]
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", choices=CASES, required=True)
    parser.add_argument("--clone-root", type=Path, required=True)
    parser.add_argument("--base-root", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--rscript", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = run_case(
            case_id=args.case,
            clone_root=args.clone_root,
            base_root=args.base_root,
            commit=args.commit,
            rscript=args.rscript,
        )
        write_json(args.output, result)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (AnonymousCaseError, OSError, KeyError, ValueError) as exc:
        sys.stderr.write(f"ANONYMOUS_CASE_ERROR: {exc}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
