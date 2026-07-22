#!/usr/bin/env python3
"""Prove a native R failure before completion-marker promotion is fail-closed."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys


CASE_DIR = Path(__file__).resolve().parent
CHUNK_SIZE = 4 * 1024 * 1024


class FaultError(RuntimeError):
    """Raised when the dedicated negative control does not fail closed."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted((item for item in root.rglob("*") if item.is_file()), key=lambda item: item.as_posix().lower()):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(path.stat().st_size).encode("ascii"))
        digest.update(b"\0")
        digest.update(sha256_file(path).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(path)


def run(args: argparse.Namespace) -> dict:
    canonical = args.canonical_run_root.resolve(strict=True)
    failure = args.failure_run_root.resolve()
    cache = args.cache_root.resolve(strict=True)
    inputs = args.input_cache_root.resolve(strict=True)
    rscript = args.rscript.resolve(strict=True)
    if failure.exists():
        raise FaultError("failure run root must not exist")
    before = tree_fingerprint(canonical)
    command = [
        sys.executable,
        str(CASE_DIR / "prepare_environment.py"),
        "--run-root",
        str(failure),
        "--cache-root",
        str(cache),
        "--input-cache-root",
        str(inputs),
        "--rscript",
        str(rscript),
        "--authorized",
        "--test-fault-before-completion-marker",
    ]
    completed = subprocess.run(
        command,
        cwd=CASE_DIR.parent.parent,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    after = tree_fingerprint(canonical)
    marker = failure / "02_environment" / "environment-provision.complete.json"
    log = failure / "logs" / "environment-prepare.log"
    process_path = failure / "logs" / "environment-prepare.process.json"
    if not log.is_file() or not process_path.is_file():
        raise FaultError("fault subprocess did not preserve its log/process evidence")
    process = json.loads(process_path.read_text(encoding="utf-8"))
    sentinel = "FAULT_INJECTION_BEFORE_COMPLETION_MARKER"
    if (
        completed.returncode == 0
        or marker.exists()
        or sentinel not in log.read_text(encoding="utf-8")
        or process.get("status") != "failed"
        or process.get("native_returncode") == 0
        or before != after
    ):
        raise FaultError("pre-completion-marker fault was not failure-closed")
    return {
        "schema_version": "1.0",
        "case": "visium-mouse-brain",
        "status": "passed",
        "injection": "native_r_failure_after_cache_validation_before_completion_marker",
        "observed_wrapper_returncode": completed.returncode,
        "observed_native_returncode": process.get("native_returncode"),
        "completion_marker_absent": True,
        "dedicated_fault_sentinel_observed": True,
        "canonical_run_modified": False,
        "canonical_run_fingerprint_before": before,
        "canonical_run_fingerprint_after": after,
        "failure_process_evidence_sha256": sha256_file(process_path),
        "failure_log_sha256": sha256_file(log),
        "failure_run_root_basename": failure.name,
        "absolute_paths_included": False,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--canonical-run-root", type=Path, required=True)
    parser.add_argument("--failure-run-root", type=Path, required=True)
    parser.add_argument("--cache-root", type=Path, required=True)
    parser.add_argument("--input-cache-root", type=Path, required=True)
    parser.add_argument("--rscript", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        evidence = run(args)
        write_json(args.output.resolve(), evidence)
        print(json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (FaultError, OSError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        print(f"ENVIRONMENT_FAULT_INJECTION_FAILED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
