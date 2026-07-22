#!/usr/bin/env python3
"""Authorized, idempotent Python wrapper for the case-local R/renv lifecycle."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import case_driver


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--cache-root", type=Path, required=True)
    parser.add_argument("--input-cache-root", type=Path, required=True)
    parser.add_argument("--rscript", type=Path, required=True)
    parser.add_argument("--authorized", action="store_true")
    parser.add_argument(
        "--test-fault-before-completion-marker",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if not args.authorized:
            raise case_driver.CaseError(
                "environment preparation requires explicit --authorized from the authorized root CLI"
            )
        run_root = case_driver.require_absolute(args.run_root, "--run-root")
        cache_root = case_driver.require_absolute(args.cache_root, "--cache-root")
        input_root = case_driver.require_absolute(args.input_cache_root, "--input-cache-root")
        rscript = case_driver.require_absolute(args.rscript, "--rscript")
        if (
            run_root == cache_root
            or case_driver.is_relative_to(run_root, cache_root)
            or case_driver.is_relative_to(cache_root, run_root)
        ):
            raise case_driver.CaseError(
                "--run-root and --cache-root must be separate sibling task-local trees"
            )
        if run_root == case_driver.CASE_DIR or cache_root == case_driver.CASE_DIR:
            raise case_driver.CaseError(
                "run/cache roots must not overwrite the tutorial source directory"
            )
        run_root.mkdir(parents=True, exist_ok=True)
        cache_root.mkdir(parents=True, exist_ok=True)
        is_resume = (run_root / "manifest" / "run_manifest.json").is_file()
        case_driver.initialize_run_tree(run_root, fresh=not is_resume)
        case_driver.probe_rscript(rscript, run_root / "logs" / "r-probe.log")
        if input_root == run_root or case_driver.is_relative_to(input_root, run_root):
            raise case_driver.CaseError("--input-cache-root must be external to the fresh run root")
        input_root.mkdir(parents=True, exist_ok=True)
        case_driver.fetch_inputs(run_root, input_root)
        env_root = case_driver.prepare_environment(
            run_root,
            cache_root,
            rscript,
            input_root,
            fault_injection=(
                "before_completion_marker"
                if args.test_fault_before_completion_marker
                else None
            ),
        )
        marker = case_driver.read_json(
            run_root / "02_environment" / "environment.locked.json"
        )
        result = {
            "status": "frozen",
            "case": case_driver.CASE_ID,
            "cache_key": case_driver.environment_cache_key(),
            "environment_basename": env_root.name,
            "r_version": marker["r_version"],
            "task_local_renv_version": marker["task_local_renv_version"],
            "seurat_version": marker["seurat_version"],
            "hdf5r_version": marker["packages"]["hdf5r"],
            "repository_snapshot": marker["repository"]["snapshot_url"],
            "package_type": marker["repository"]["package_type"],
            "renv_lock_sha256": marker["renv_lock_sha256"],
            "read10x_h5_smoke": marker["verification"]["read10x_h5_smoke"],
            "probe_sha256": marker["probe"]["sha256"],
            "shutdown_mode": marker["shutdown_mode"],
            "bioconductor_release": marker["bioconductor"]["release"],
            "bioconductor_version": marker["bioconductor"]["version"],
            "glmGamPoi_version": marker["packages"]["glmGamPoi"],
            "SparseArray_version": marker["packages"]["SparseArray"],
            "bioconductor_pins_sha256": marker["bioconductor"]["pins_sha256"],
            "smoke_matrix_dimensions": marker["h5_reader_smoke"][
                "matrix_dimensions"
            ],
        }
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (case_driver.CaseError, OSError) as exc:
        print(f"ENVIRONMENT_ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
