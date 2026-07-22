#!/usr/bin/env python3
"""Run the complete GSE185948 teaching workflow in a frozen Python environment."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path


ASSET_ROOT = Path(__file__).resolve().parent
SKILL_ROOT = ASSET_ROOT.parents[2]
ENVIRONMENT_MANAGER = SKILL_ROOT / "scripts" / "environment_manager.py"
METADATA_SIDECAR = SKILL_ROOT / "references" / "p0-single-cell-gse185948-metadata.json"
EXPECTED_METADATA_SHA256 = "32794f6119df63e811accb41a823788e4d5b98b3d8ec94a4313818ae19925a93"
EXPECTED_SAMPLES = {
    "GSM5627690": ("cont1", 26385307, "bc4f985540176f7acc49b01bc99b8c32b75d6efc424943dd239155433ce9fc99", 8951),
    "GSM5627691": ("cont2", 60596538, "e7f61e0e281b7a4427d16352989e2478c74fec6154e4976ed328b958369e3936", 15493),
    "GSM5627692": ("cont3", 26000386, "6c1ee9e2748cf1d04addf3e361b256b3d2a5d509a7780a4598e538d6e730072c", 9349),
    "GSM5627693": ("cont4", 54076325, "2ee83d0750324c404ab0ec630f4c2320f2d1bed768beca7c32aad685808a1ff9", 13243),
    "GSM5627694": ("cont5", 22425529, "e2b6625ba206e0619ae498b14b7613f585e20ff9cbd1a0ebdef31ed2a5084b4c", 9692),
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def parse_environment(module, plan_path: Path):
    plan = json.loads(plan_path.read_text(encoding="utf-8-sig"))
    environments = plan.get("environments", [])
    if len(environments) != 1:
        raise RuntimeError("This teaching case requires exactly one frozen Python environment")
    payload = environments[0]
    if payload.get("runtime") != "python" or payload.get("backend") != "python-uv":
        raise RuntimeError("Expected a task-local python-uv environment")
    spec = module.EnvironmentSpec(
        env_id=payload["env_id"],
        runtime=payload["runtime"],
        backend=payload["backend"],
        path=payload["path"],
        dependencies=tuple(module.Dependency.from_mapping(item) for item in payload["dependencies"]),
        lock_hash=payload["lock_hash"],
        install_strategy=payload.get("install_strategy", "simultaneous"),
        preinstall=tuple(payload.get("preinstall", ())),
    )
    return plan, spec


def validate_inputs(config_path: Path, metadata_path: Path) -> dict[str, object]:
    if sha256(metadata_path) != EXPECTED_METADATA_SHA256:
        raise RuntimeError("Metadata sidecar hash mismatch")
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    if config.get("source_mode") != "read_only":
        raise RuntimeError("Input configuration must declare source_mode=read_only")
    sampling = config.get("sampling", {})
    if sampling.get("method") != "deterministic-without-replacement-h5-csc-column-slice":
        raise RuntimeError("Sampling method contract mismatch")
    if int(sampling.get("seed", -1)) != 20260719 or int(sampling.get("max_nuclei_per_donor", -1)) != 1000:
        raise RuntimeError("Sampling seed or per-donor limit differs from the teaching contract")
    if sampling.get("full_matrix_loaded_before_sampling") is not False:
        raise RuntimeError("Sampling contract must prohibit full-matrix loading before selection")
    rows = config.get("inputs", [])
    if len(rows) != 5 or {row.get("sample_accession") for row in rows} != set(EXPECTED_SAMPLES):
        raise RuntimeError("Exactly the five frozen GSE185948 samples are required")
    verified = []
    for row in rows:
        accession = str(row["sample_accession"])
        donor, size, digest, barcodes = EXPECTED_SAMPLES[accession]
        expected = {
            "donor_id": donor,
            "size_bytes": size,
            "sha256": digest,
            "expected_features": 36601,
            "expected_barcodes": barcodes,
        }
        for key, value in expected.items():
            if row.get(key) != value:
                raise RuntimeError(f"{accession} {key} differs from the frozen input contract")
        path = Path(row["path"]).resolve()
        if not path.is_file():
            raise FileNotFoundError(path)
        if path.stat().st_size != size or sha256(path) != digest:
            raise RuntimeError(f"Source integrity mismatch: {accession}")
        verified.append({"sample_accession": accession, "donor_id": donor, "path": str(path), "size_bytes": size, "sha256": digest})
    return {"config": config, "verified_inputs": verified}


def initialize_run(run_root: Path, input_config: Path, environment_plan: Path, spec, marker: dict, verified: dict) -> None:
    if run_root.exists():
        raise FileExistsError(f"Refusing to overwrite an existing run: {run_root}")
    for relative in (
        "00_request", "01_plan", "02_environment", "03_scripts/modules", "04_intermediate",
        "05_results/tables", "05_results/objects", "06_figures/original", "06_figures/final",
        "06_figures/review", "07_reports", "logs", "manifest", "_staging",
    ):
        (run_root / relative).mkdir(parents=True, exist_ok=True)
    write_json(
        run_root / "00_request" / "intent.json",
        {
            "case_id": "p0-single-cell-gse185948",
            "analysis_id": "p0-single-cell-gse185948-reduced-real-data-fixture",
            "mode": "run",
            "measurement_unit": "nucleus",
            "future_inference_unit": "donor",
            "claim_scope": "reduced real-data descriptive teaching fixture only",
        },
    )
    write_json(
        run_root / "00_request" / "input_manifest.json",
        {
            "source_policy": "read_only",
            "private_absolute_locators_omitted_from_public_asset": True,
            "input_config_sha256": sha256(input_config),
            "metadata_sidecar_sha256": sha256(METADATA_SIDECAR),
            "inputs": verified["verified_inputs"],
        },
    )
    write_json(
        run_root / "01_plan" / "workflow.plan.json",
        {
            "stages": ["00-intake", "01-environment", "02-analysis", "03-native-review"],
            "automatic_method_fallback": False,
            "doublet_filter_policy": "all-donors-or-none",
            "integration_decision": "retain unintegrated PCA because donor, library and batch are confounded",
        },
    )
    environment_dir = Path(spec.path).resolve()
    for name in ("requirements.lock.txt", "environment.locked.json"):
        source = environment_dir / name
        if not source.is_file():
            raise FileNotFoundError(source)
        shutil.copy2(source, run_root / "02_environment" / name)
    shutil.copy2(environment_plan, run_root / "02_environment" / "environment.plan.json")
    environment_manifest = {
        "env_id": spec.env_id,
        "runtime": "python",
        "backend": spec.backend,
        "lock_hash": spec.lock_hash,
        "platform": marker.get("platform"),
        "backend_lock_sha256": sha256(run_root / "02_environment" / "requirements.lock.txt"),
        "verified": True,
        "frozen": True,
        "global_changes": False,
    }
    write_json(run_root / "02_environment" / "environment_manifest.json", environment_manifest)
    for name in ("run_pipeline.py", "verify_outputs.py", "case-spec.yaml"):
        shutil.copy2(ASSET_ROOT / name, run_root / "03_scripts" / name)
    shutil.copy2(input_config, run_root / "03_scripts" / "input-config.json")
    shutil.copy2(METADATA_SIDECAR, run_root / "03_scripts" / METADATA_SIDECAR.name)
    now = utc_now()
    write_json(
        run_root / "manifest" / "run_manifest.json",
        {
            "run_id": run_root.name,
            "case_id": "p0-single-cell-gse185948",
            "mode": "run",
            "created_at": now,
            "updated_at": now,
            "current_state": "ENV_LOCKED",
            "terminal": False,
            "checkpoints": [
                {"stage_id": "00-intake", "validated": True, "artifact_count": 2},
                {"stage_id": "01-environment", "validated": True, "artifact_count": 4},
            ],
            "environment": environment_manifest,
        },
    )
    (run_root / "manifest" / "artifact_ledger.jsonl").write_text("", encoding="utf-8")


def register_output(run_root: Path, validation: dict[str, object]) -> None:
    analysis_root = run_root / "04_intermediate" / "02-analysis" / "pipeline-output"
    for source in sorted((analysis_root / "figures").glob("*.png")):
        shutil.copy2(source, run_root / "06_figures" / "original" / source.name)
        shutil.copy2(source, run_root / "06_figures" / "final" / source.name)
    shutil.copy2(analysis_root / "reports" / "FIGURE_NOTES.md", run_root / "07_reports" / "FIGURE_NOTES.md")
    shutil.copy2(analysis_root / "reports" / "QA_REPORT.md", run_root / "07_reports" / "QA_REPORT.md")
    write_json(run_root / "07_reports" / "teaching-case-verification.json", validation)

    ledger: list[dict[str, object]] = []
    for path in sorted(item for item in run_root.rglob("*") if item.is_file()):
        relative = path.relative_to(run_root).as_posix()
        if relative == "manifest/artifact_ledger.jsonl":
            continue
        maturity = "data-verified-pending-native-review" if relative.startswith("06_figures/") else "data-verified"
        ledger.append(
            {
                "stage_id": "02-analysis" if relative.startswith("04_intermediate/") else "delivery",
                "path": relative,
                "sha256": sha256(path),
                "size_bytes": path.stat().st_size,
                "maturity": maturity,
            }
        )
    ledger_path = run_root / "manifest" / "artifact_ledger.jsonl"
    ledger_path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in ledger), encoding="utf-8")
    manifest_path = run_root / "manifest" / "run_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    manifest.update(
        {
            "updated_at": utc_now(),
            "current_state": "NATIVE_VISUAL_REVIEW",
            "terminal": False,
            "registered_artifacts": len(ledger),
        }
    )
    manifest["checkpoints"].append(
        {
            "stage_id": "02-analysis",
            "validated": True,
            "artifact_count": len(list(analysis_root.rglob("*"))),
            "metrics": validation.get("metrics"),
        }
    )
    write_json(manifest_path, manifest)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--input-config", type=Path, required=True)
    parser.add_argument("--environment-plan", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=7200)
    parser.add_argument("--seed", type=int, default=20260719)
    args = parser.parse_args()

    input_config = args.input_config.resolve()
    environment_plan = args.environment_plan.resolve()
    for path in (input_config, environment_plan, METADATA_SIDECAR):
        if not path.is_file():
            raise FileNotFoundError(path)
    verified = validate_inputs(input_config, METADATA_SIDECAR)
    manager_module = load_module(ENVIRONMENT_MANAGER, "biomedical_environment_manager")
    plan, spec = parse_environment(manager_module, environment_plan)
    task_root = Path(plan["task_root"]).resolve()
    run_root = args.run_root.resolve()
    if not run_root.is_relative_to(task_root):
        raise RuntimeError("Run root must remain inside the managed task root")
    marker_path = Path(spec.path).resolve() / "environment.locked.json"
    marker = json.loads(marker_path.read_text(encoding="utf-8-sig"))
    if marker.get("lock_hash") != spec.lock_hash:
        raise RuntimeError("Environment marker lock hash mismatch")
    manager = manager_module.EnvironmentManager(task_root, cache_root=Path(plan["cache_root"]), max_attempts=1)
    handle = manager_module.EnvironmentHandle(spec=spec, state="frozen", verified=True, frozen=True)
    manager.verify(handle)
    initialize_run(run_root, input_config, environment_plan, spec, marker, verified)

    stage = run_root / "_staging" / "02-analysis"
    stage.mkdir(parents=True, exist_ok=False)
    pipeline_output = stage / "pipeline-output"
    authorization = manager_module.ExecutionAuthorization(mode="run", approved=True)
    result = manager.execute(
        handle,
        run_root / "03_scripts" / "run_pipeline.py",
        authorization,
        args=(
            "--input-config", str(run_root / "03_scripts" / "input-config.json"),
            "--metadata-sidecar", str(run_root / "03_scripts" / METADATA_SIDECAR.name),
            "--expected-metadata-sha256", EXPECTED_METADATA_SHA256,
            "--output-dir", str(pipeline_output),
            "--seed", str(args.seed),
        ),
        cwd=task_root,
        timeout=args.timeout_seconds,
    )
    (run_root / "logs" / "analysis.stdout.log").write_text(result.stdout, encoding="utf-8")
    (run_root / "logs" / "analysis.stderr.log").write_text(result.stderr, encoding="utf-8")
    if result.returncode != 0:
        failure = manager.report_failure(
            handle.spec,
            stage="single-cell-analysis",
            command_record={
                "command": ["python", "<task-local-run_pipeline.py>"],
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            },
            attempts=1,
        )
        write_json(run_root / "logs" / "analysis.failure.json", failure)
        manifest_path = run_root / "manifest" / "run_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        manifest.update({"updated_at": utc_now(), "current_state": "FAILED", "terminal": True})
        write_json(manifest_path, manifest)
        print(json.dumps(failure, ensure_ascii=False, indent=2))
        return 2

    verifier = load_module(run_root / "03_scripts" / "verify_outputs.py", "gse185948_teaching_verifier")
    validation = verifier.verify_pipeline_output(pipeline_output)
    if not validation["ok"]:
        write_json(run_root / "logs" / "output-contract.failure.json", validation)
        print(json.dumps(validation, ensure_ascii=False, indent=2))
        return 3
    destination = run_root / "04_intermediate" / "02-analysis"
    os.replace(stage, destination)
    register_output(run_root, validation)
    print(
        json.dumps(
            {
                "ok": True,
                "run_root": str(run_root),
                "state": "NATIVE_VISUAL_REVIEW",
                "metrics": validation.get("metrics"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

