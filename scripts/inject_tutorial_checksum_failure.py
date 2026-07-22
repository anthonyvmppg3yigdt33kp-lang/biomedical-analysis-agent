#!/usr/bin/env python3
"""Prove that each tutorial rejects a deliberately incorrect input checksum."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CASES = {"pbmc3k", "visium-mouse-brain"}
CHECKSUM_SENTINELS = {
    "pbmc3k": re.compile(r"^INPUT_PREPARATION_FAILED: SHA-256 mismatch:", re.MULTILINE),
    "visium-mouse-brain": re.compile(r"^INPUT_ERROR: SHA-256 mismatch for ", re.MULTILINE),
}


class InjectionError(RuntimeError):
    """Raised when the negative checksum control does not fail closed."""


def write_json(path: Path, payload: dict) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def inject(case: str, run_root: Path, cache_root: Path) -> dict:
    run_root = run_root.resolve(strict=True)
    cache_root = cache_root.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix=f"{case}-checksum-negative-") as temporary:
        temporary_root = Path(temporary)
        if case == "pbmc3k":
            case_dir = ROOT / "examples" / "pbmc3k"
            manifest = json.loads((case_dir / "input_manifest.json").read_text(encoding="utf-8"))
            manifest["inputs"][0]["sha256"] = "0" * 64
            bad_manifest = temporary_root / "input_manifest.json"
            write_json(bad_manifest, manifest)
            command = [
                sys.executable,
                str(case_dir / "download_inputs.py"),
                "--manifest",
                str(bad_manifest),
                "--cache-root",
                str(cache_root),
            ]
        elif case == "visium-mouse-brain":
            case_dir = ROOT / "examples" / "visium-mouse-brain"
            input_root = cache_root / "inputs" / case
            if not input_root.is_dir():
                raise InjectionError(
                    "external Visium input cache is missing; expected cache-root/inputs/visium-mouse-brain"
                )
            manifest = json.loads(
                (run_root / "00_request" / "input-manifest.json").read_text(encoding="utf-8")
            )
            resolved_source = run_root / "00_request" / "resolved-inputs.json"
            resolved = json.loads(resolved_source.read_text(encoding="utf-8"))
            if not resolved.get("files"):
                raise InjectionError("resolved Visium input manifest has no files")
            file_id = resolved["files"][0].get("file_id")
            source_records = [item for item in manifest.get("files", []) if item.get("file_id") == file_id]
            if len(source_records) != 1:
                raise InjectionError("cannot bind the selected Visium file to the source manifest")
            # Keep the source/resolved identity internally consistent so validation
            # reaches the byte-level checksum gate rather than an earlier schema gate.
            source_records[0]["expected_sha256"] = "0" * 64
            resolved["files"][0]["sha256"] = "0" * 64
            bad_manifest = temporary_root / "input-manifest.json"
            write_json(bad_manifest, manifest)
            bad_resolved = temporary_root / "resolved-inputs.json"
            write_json(bad_resolved, resolved)
            command = [
                sys.executable,
                str(case_dir / "download_inputs.py"),
                "verify",
                "--manifest",
                str(bad_manifest),
                "--input-root",
                str(input_root),
                "--resolved-manifest",
                str(bad_resolved),
            ]
        else:
            raise InjectionError(f"unsupported case: {case}")
        completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
    if completed.returncode != 2:
        raise InjectionError(
            f"checksum negative control returned {completed.returncode}; expected dedicated input error code 2"
        )
    sentinel = CHECKSUM_SENTINELS[case]
    if not sentinel.search(completed.stderr):
        raise InjectionError(
            "negative control failed for a reason other than the dedicated SHA-256 mismatch gate"
        )
    return {
        "schema_version": "1.0.0",
        "ok": True,
        "case": case,
        "injection": "incorrect_sha256",
        "observed_returncode": completed.returncode,
        "failure_code": "INPUT_CHECKSUM_MISMATCH_REJECTED",
        "stderr_sentinel": sentinel.pattern,
        "stderr_sha256": hashlib.sha256(completed.stderr.encode("utf-8")).hexdigest(),
        "failure_closed": True,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=sorted(CASES), required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--cache-root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = inject(args.case, args.run_root, args.cache_root)
    except (InjectionError, OSError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        sys.stderr.write(f"CHECKSUM_FAILURE_INJECTION_FAILED: {exc}\n")
        return 2
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8", newline="\n")
    sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
