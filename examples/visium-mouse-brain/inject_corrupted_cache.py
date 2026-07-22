#!/usr/bin/env python3
"""Corrupt a task-local copy of a frozen Visium input and prove fail-closed verification."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


CASE_DIR = Path(__file__).resolve().parent
CHUNK_SIZE = 4 * 1024 * 1024


class InjectionError(RuntimeError):
    """Raised when the deliberately corrupted cache is not rejected."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def inject(run_root: Path, input_root: Path) -> dict:
    run_root = run_root.resolve(strict=True)
    manifest_path = run_root / "00_request" / "input-manifest.json"
    resolved_path = run_root / "00_request" / "resolved-inputs.json"
    input_root = input_root.resolve(strict=True)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    resolved = json.loads(resolved_path.read_text(encoding="utf-8"))
    source = {record["file_id"]: record for record in manifest.get("files", [])}
    locked = {record["file_id"]: record for record in resolved.get("files", [])}
    if set(source) != {"filtered_h5", "spatial_archive"} or set(locked) != set(source):
        raise InjectionError("canonical manifest does not contain the exact two-file Visium inventory")
    for file_id, record in source.items():
        if (
            record.get("freeze_policy") != "exact_required"
            or locked[file_id].get("sha256") != record.get("expected_sha256")
            or locked[file_id].get("size_bytes") != record.get("expected_size_bytes")
        ):
            raise InjectionError(f"canonical exact-freeze evidence is incomplete for {file_id}")
    canonical_hashes_before = {
        record["file_id"]: sha256_file(input_root / record["filename"])
        for record in source.values()
    }

    with tempfile.TemporaryDirectory(
        prefix="visium-corrupted-cache-", dir=run_root.parent
    ) as temporary:
        negative_root = Path(temporary)
        negative_inputs = negative_root / "inputs"
        negative_inputs.mkdir()
        for record in source.values():
            shutil.copy2(input_root / record["filename"], negative_inputs / record["filename"])
        shutil.copytree(input_root / "spatial", negative_inputs / "spatial")
        negative_resolved = negative_root / "resolved-inputs.json"
        shutil.copy2(resolved_path, negative_resolved)

        corrupt_path = negative_inputs / source["filtered_h5"]["filename"]
        original_sha256 = sha256_file(corrupt_path)
        with corrupt_path.open("r+b") as handle:
            first = handle.read(1)
            if not first:
                raise InjectionError("cannot corrupt an empty filtered H5")
            handle.seek(0)
            handle.write(bytes([first[0] ^ 0x01]))
            handle.flush()
        corrupted_sha256 = sha256_file(corrupt_path)
        if corrupted_sha256 == original_sha256:
            raise InjectionError("fault injection did not change the filtered H5 hash")

        command = [
            sys.executable,
            str(CASE_DIR / "download_inputs.py"),
            "verify",
            "--manifest",
            str(manifest_path),
            "--input-root",
            str(negative_inputs),
            "--resolved-manifest",
            str(negative_resolved),
        ]
        completed = subprocess.run(
            command,
            cwd=CASE_DIR,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    expected_error = "SHA-256 mismatch for filtered_h5"
    if completed.returncode == 0 or expected_error not in completed.stderr:
        raise InjectionError(
            "corrupted task-local cache was not rejected by the expected checksum gate; "
            f"returncode={completed.returncode}, stderr={completed.stderr.strip()}"
        )
    canonical_hashes_after = {
        record["file_id"]: sha256_file(input_root / record["filename"])
        for record in source.values()
    }
    if canonical_hashes_after != canonical_hashes_before:
        raise InjectionError("canonical external input cache changed during negative control")
    return {
        "schema_version": "1.0",
        "case": "visium-mouse-brain",
        "status": "passed",
        "injection": "flip_first_byte_in_task_local_copy_of_filtered_h5",
        "canonical_sha256": source["filtered_h5"]["expected_sha256"],
        "pre_injection_sha256": original_sha256,
        "post_injection_sha256": corrupted_sha256,
        "observed_returncode": completed.returncode,
        "observed_error": expected_error,
        "failure_closed": True,
        "null_or_first_learning_freeze_available": False,
        "canonical_inputs_modified": False,
        "input_cache_materialization": "direct_read_no_copy",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--input-cache-root", type=Path)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        input_root = args.input_cache_root or (args.run_root / "inputs")
        report = inject(args.run_root, input_root)
        if args.output:
            write_json(args.output.resolve(), report)
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (InjectionError, OSError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        print(f"CORRUPTED_CACHE_INJECTION_FAILED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
