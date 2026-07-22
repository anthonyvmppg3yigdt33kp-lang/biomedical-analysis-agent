#!/usr/bin/env python3
"""Unified entry point for the two public Seurat teaching workflows.

Planning is read-only. Run and resume require an explicit authorization flag
and delegate to a case-local driver that applies the fixed run-tree contract.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from analysis_agent import compile_plan  # noqa: E402


CASES: dict[str, dict[str, str]] = {
    "pbmc3k": {
        "directory": "pbmc3k",
        "expected_primary": "single-cell",
    },
    "visium-mouse-brain": {
        "directory": "visium-mouse-brain",
        "expected_primary": "spatial-transcriptomics",
    },
}

EXPECTED_R_VERSION = "4.5.3"
EXPECTED_SEURAT_VERSION = "5.5.0"
RSCRIPT_VERSION_PATTERN = re.compile(
    r"(?:R scripting front-end version|Rscript \(R\) version)\s+([0-9.]+)",
    re.IGNORECASE,
)


class TutorialError(RuntimeError):
    """A deterministic tutorial preflight or delegated command failed."""


def _r_child_environment(environment: dict[str, str] | None = None) -> dict[str, str]:
    """Return a child-only R environment bound to the native Windows architecture."""
    child = dict(os.environ if environment is None else environment)
    if os.name != "nt":
        return child
    system_info = (ctypes.c_ubyte * 64)()
    ctypes.windll.kernel32.GetNativeSystemInfo(ctypes.byref(system_info))
    architecture_code = int.from_bytes(bytes(system_info[:2]), byteorder="little")
    native_label = {9: "X64", 12: "ARM64", 0: "X86"}.get(architecture_code)
    canonical = {"X64": "AMD64", "ARM64": "ARM64", "X86": "x86"}.get(native_label or "")
    if canonical != "AMD64":
        raise TutorialError(
            f"unsupported Windows native architecture {native_label or architecture_code}; only AMD64 is validated"
        )
    aliases = {
        "AMD64": "AMD64", "X64": "AMD64", "X86_64": "AMD64",
        "ARM64": "ARM64", "AARCH64": "ARM64", "X86": "x86",
        "I386": "x86", "I686": "x86",
    }
    observed = child.get("PROCESSOR_ARCHITECTURE", "").strip()
    if observed and aliases.get(observed.upper()) != canonical:
        raise TutorialError("PROCESSOR_ARCHITECTURE conflicts with native Windows architecture")
    wow64 = child.get("PROCESSOR_ARCHITEW6432", "").strip()
    if wow64 and aliases.get(wow64.upper()) != canonical:
        raise TutorialError("PROCESSOR_ARCHITEW6432 conflicts with native Windows architecture")
    child["PROCESSOR_ARCHITECTURE"] = canonical
    return child


def _case_dir(case: str) -> Path:
    path = ROOT / "examples" / CASES[case]["directory"]
    if not path.is_dir():
        raise TutorialError(f"Tutorial case is missing: {path}")
    return path


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TutorialError(f"Cannot read JSON {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise TutorialError(f"Expected a JSON object: {path}")
    return payload


def build_case_plan(case: str) -> dict[str, Any]:
    request = _load_json(_case_dir(case) / "request.json")
    request["mode"] = "plan"
    request["execution_authorized"] = False
    request.pop("authorization_scope", None)
    request["project_root"] = str(ROOT)
    plan = compile_plan(request)
    actual = plan["routes"][0]["capability"]
    expected = CASES[case]["expected_primary"]
    if actual != expected:
        raise TutorialError(
            f"Routing regression for {case}: expected {expected}, observed {actual}"
        )
    return plan


def _default_rscript() -> Path:
    configured = os.environ.get("R_SCRIPT")
    if configured:
        return Path(configured)
    windows = Path(r"C:\Program Files\R\R-4.5.3\bin\Rscript.exe")
    if windows.is_file():
        return windows
    located = shutil.which("Rscript")
    if located:
        return Path(located)
    raise TutorialError("Rscript was not found; pass --rscript explicitly")


def _verify_rscript(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise TutorialError(f"Rscript does not exist: {resolved}")
    result = subprocess.run(
        [str(resolved), "--version"],
        env=_r_child_environment(),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    version_text = f"{result.stdout}\n{result.stderr}"
    forbidden = (
        "warning:", "stack imbalance", "iteration limit reached",
        "alternation limit reached", "execution halted", "error in ",
        "caught access violation", "caught segfault", "fatal error",
    )
    forbidden_matches = [item for item in forbidden if item in version_text.lower()]
    match = RSCRIPT_VERSION_PATTERN.search(version_text)
    if result.returncode != 0 or not match or forbidden_matches:
        raise TutorialError(f"Rscript preflight failed: {version_text.strip()}")
    if match.group(1) != EXPECTED_R_VERSION:
        raise TutorialError(
            f"R {EXPECTED_R_VERSION} is required; observed {match.group(1)}"
        )
    return resolved


def _run_root(args: argparse.Namespace) -> Path:
    if args.run_root:
        return args.run_root.expanduser().resolve()
    return (ROOT / "runs" / args.case / args.run_id).resolve()


def _cache_root(args: argparse.Namespace) -> Path:
    if getattr(args, "cache_root", None):
        return args.cache_root.expanduser().resolve()
    return (ROOT / ".cache" / "tutorials").resolve()


def _input_cache_root(args: argparse.Namespace) -> Path:
    if args.input_cache_root:
        return args.input_cache_root.expanduser().resolve()
    return (_cache_root(args) / "inputs" / args.case).resolve()


def _plan_payload(case: str) -> tuple[dict[str, Any], bytes]:
    plan = build_case_plan(case)
    rendered = (
        json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    return plan, rendered


def _bind_case_plan(case: str, run_root: Path, *, resume: bool) -> dict[str, str]:
    plan, rendered = _plan_payload(case)
    destination = run_root / "01_plan" / "root-compiled-plan.json"
    expected_sha256 = hashlib.sha256(rendered).hexdigest()
    if destination.is_file():
        observed = destination.read_bytes()
        if observed != rendered:
            raise TutorialError(
                "the current root-compiled plan differs from the plan bound to this run; "
                "start a fresh run root"
            )
    elif resume:
        raise TutorialError("resume requires the root-compiled plan bound by the fresh run")
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.tmp")
        temporary.write_bytes(rendered)
        os.replace(temporary, destination)
    return {
        "path": destination.relative_to(run_root).as_posix(),
        "sha256": expected_sha256,
        "plan_id": str(plan["plan_id"]),
        "request_sha256": str(plan["request_sha256"]),
    }


def _delegate(args: argparse.Namespace) -> int:
    if args.command in {"run", "resume"} and not args.authorize_run:
        raise TutorialError(
            f"{args.command} requires --authorize-run; no files or environments were changed"
        )
    case_dir = _case_dir(args.case)
    driver = case_dir / "case_driver.py"
    if not driver.is_file():
        raise TutorialError(f"Case driver is missing: {driver}")
    run_root = _run_root(args)
    command = [
        sys.executable,
        str(driver),
        args.command,
        "--run-root",
        str(run_root),
    ]
    if args.case == "visium-mouse-brain":
        command.extend(["--input-cache-root", str(_input_cache_root(args))])
    if args.command in {"run", "resume"}:
        rscript = _verify_rscript(args.rscript or _default_rscript())
        _bind_case_plan(
            args.case,
            run_root,
            resume=args.command == "resume",
        )
        preparer = case_dir / "prepare_environment.py"
        if not preparer.is_file():
            raise TutorialError(f"Environment preparer is missing: {preparer}")
        prepare_command = [
            sys.executable,
            str(preparer),
            "--run-root",
            str(run_root),
            "--cache-root",
            str(_cache_root(args)),
            "--rscript",
            str(rscript),
            "--authorized",
        ]
        if args.case == "visium-mouse-brain":
            prepare_command.extend(["--input-cache-root", str(_input_cache_root(args))])
        prepared = subprocess.run(prepare_command, cwd=ROOT, check=False)
        if prepared.returncode != 0:
            return prepared.returncode
        command.extend(
            [
                "--cache-root",
                str(_cache_root(args)),
                "--rscript",
                str(rscript),
                "--authorized",
            ]
        )
    result = subprocess.run(command, cwd=ROOT, check=False)
    return result.returncode


def _plan(args: argparse.Namespace) -> int:
    _plan, rendered = _plan_payload(args.case)
    payload = rendered.decode("utf-8")
    if args.output:
        output = args.output.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name(f".{output.name}.tmp")
        temporary.write_bytes(rendered)
        os.replace(temporary, output)
    sys.stdout.write(payload)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan", help="Compile a read-only frozen plan")
    plan.add_argument("--case", choices=sorted(CASES), required=True)
    plan.add_argument("--output", type=Path)
    plan.set_defaults(handler=_plan)

    for command in ("run", "resume", "verify", "report"):
        child = subparsers.add_parser(command)
        child.add_argument("--case", choices=sorted(CASES), required=True)
        child.add_argument("--run-root", type=Path)
        child.add_argument("--run-id", default="canonical")
        child.add_argument("--input-cache-root", type=Path)
        if command in {"run", "resume"}:
            child.add_argument("--authorize-run", action="store_true")
            child.add_argument("--rscript", type=Path)
            child.add_argument("--cache-root", type=Path)
        child.set_defaults(handler=_delegate)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except (OSError, subprocess.SubprocessError, TutorialError) as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
