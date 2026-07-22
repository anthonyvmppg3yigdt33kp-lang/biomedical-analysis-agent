#!/usr/bin/env python3
"""Static, data-free contract tests for the Visium teaching case."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile

import pytest


CASE_DIR = Path(__file__).resolve().parent
ROOT = CASE_DIR.parent.parent


def _rscript_for_contract_test() -> str | None:
    discovered = shutil.which("Rscript")
    if discovered:
        return discovered
    r_home = os.environ.get("R_HOME")
    candidates = []
    if r_home:
        candidates.append(Path(r_home) / "bin" / ("Rscript.exe" if os.name == "nt" else "Rscript"))
    if os.name == "nt":
        candidates.append(Path(r"C:\Program Files\R\R-4.5.3\bin\Rscript.exe"))
    return str(next((path for path in candidates if path.is_file()), "")) or None


def _load_driver():
    spec = importlib.util.spec_from_file_location("visium_case_driver", CASE_DIR / "case_driver.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_request_and_compiler_outputs_are_current() -> None:
    request = json.loads((CASE_DIR / "request.json").read_text(encoding="utf-8"))
    assert request["question"] == request["research_question"]
    assert request["spatial_unit"] == "spot"
    for command, filename in (("route", "route.json"), ("compile", "workflow.plan.json")):
        generated = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "analysis_agent.py"), command, "--request", str(CASE_DIR / "request.json")],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout
        assert json.loads(generated) == json.loads((CASE_DIR / filename).read_text(encoding="utf-8"))
    routes = json.loads((CASE_DIR / "route.json").read_text(encoding="utf-8"))
    assert [route["capability"] for route in routes] == ["spatial-transcriptomics", "visualization"]
    assert "single-cell" not in {route["capability"] for route in routes}
    plan = json.loads((CASE_DIR / "workflow.plan.json").read_text(encoding="utf-8"))
    assert plan["run"] == {
        "task_slug": "visium-mouse-brain",
        "run_id": "canonical",
        "root": r"runs\visium-mouse-brain\canonical",
    }
    node_ids = [node["node_id"] for node in plan["workflow"]["nodes"]]
    assert "06-visualization" in node_ids
    assert "07-native-visual-review" in node_ids


def test_exact_windows_binary_environment_contract() -> None:
    spec = json.loads((CASE_DIR / "environment-spec.json").read_text(encoding="utf-8"))
    dependencies = {item["name"]: item.get("version") for item in spec["dependencies"]}
    assert spec["runtime"]["version"] == "4.5.3"
    assert spec["manager"]["cran_repository_snapshot"] == "https://packagemanager.posit.co/cran/2026-04-23"
    assert spec["manager"]["host_renv_required"] is False
    assert spec["runtime"]["child_processor_architecture"] == "AMD64"
    assert spec["runtime"]["shutdown_mode"] == "native_exit"
    assert spec["manager"]["bootstrap_binary"] == {
        "filename": "renv_1.2.2.zip",
        "size_bytes": 2514910,
        "sha256": "bcba2170563c65c6d6ed9328e4a624033ad9c5ee4e5bf9132cff7bcc7327cce5",
        "index_md5_available": False,
    }
    assert dependencies["renv"] == "1.2.2"
    assert dependencies["BiocManager"] == "1.30.27"
    assert dependencies["Seurat"] == "5.5.0"
    assert dependencies["hdf5r"] == "1.3.12"
    assert dependencies["glmGamPoi"] == "1.20.0"
    assert dependencies["SparseArray"] == "1.8.1"
    assert dependencies["BiocVersion"] == "3.21.1"
    assert spec["bioconductor"]["release"] == "3.21"
    assert spec["bioconductor"]["same_release_closure_required"] is True
    assert spec["bioconductor"]["cross_release_packages_allowed"] is False
    assert spec["bioconductor"]["source_compilation_allowed"] is False
    assert spec["manager"]["snapshot_provenance_lookup_package_type"] == "source"
    assert spec["manager"]["installation_package_type"] == "binary"
    assert spec["manager"]["validated_library_search_set"] == [
        "task_local_renv_project_library",
        "exact_R_home_library",
    ]
    assert spec["manager"]["r_home_base_library_asserted"] is True
    assert spec["bioconductor"]["annotation_source_index_gate"] == {
        "repository": "https://bioconductor.org/packages/3.21/data/annotation",
        "contrib_url": "https://bioconductor.org/packages/3.21/data/annotation/src/contrib",
        "package": "GenomeInfoDbData",
        "version": "1.2.14",
        "NeedsCompilation": "no",
    }
    pins = json.loads((CASE_DIR / "config" / "bioconductor-3.21-archive-pins.json").read_text(encoding="utf-8"))
    assert len(pins["archives"]) == 47
    assert pins["requested"] == {"glmGamPoi": "1.20.0", "SparseArray": "1.8.1"}
    assert pins["archive_manifest_sha256"] == "81945d54001b5b756549793aa61c807cd900a4a38edec8c72bf60ff4797638c7"
    r_code = (CASE_DIR / "prepare_environment.R").read_text(encoding="utf-8")
    for required in (
        'type = "binary"',
        "dependencies = NA",
        'bootstrap_library <- file.path(env_root, "bootstrap-library")',
        'pkgs = "renv"',
        "renv_binary_sha256",
        'loadNamespace("renv", lib.loc = bootstrap_library)',
        'installed[["renv"]] != "1.2.2"',
        'installed[["hdf5r"]] != "1.3.12"',
        "Seurat::Read10X_h5",
        "FAULT_INJECTION_BEFORE_COMPLETION_MARKER",
        'shutdown_mode <- "native_exit"',
        'pins$bioconductor_release, "3.21"',
        "all_archives_hash_verified_before_install",
        "GenomeInfoDbData unexpectedly requires compilation",
        "renv status is not synchronized after snapshot",
        "renv restore plan is non-empty immediately after snapshot",
        "provenance_lookup_package_type = 'source'",
        "BioCann source index does not resolve GenomeInfoDbData",
        "library = c(project_library, base_library)",
        "restore_action_count <- nrow(restore_actions)",
        "status_difference_count = length(status_diff)",
        "v4_failed_status_scope_audit",
    ):
        assert required in r_code
    assert "exit-helper" not in r_code
    assert "hard_exit" not in r_code


def test_public_input_manifest_is_exact_and_first_learning_is_prohibited() -> None:
    manifest = json.loads((CASE_DIR / "input-manifest.json").read_text(encoding="utf-8"))
    expected = {
        "filtered_h5": (
            20554697,
            "56078d8d6fe6c13de248fdb1c518b691cdef78fb00021b659786b4a47c6656d5",
        ),
        "spatial_archive": (
            9233573,
            "5f41a803e2bd69fa4dfca6abc8fa2d4e0d76aeb6c72d7038a5fdcf9cc50a36f8",
        ),
    }
    assert {record["file_id"] for record in manifest["files"]} == set(expected)
    for record in manifest["files"]:
        assert (record["expected_size_bytes"], record["expected_sha256"]) == expected[record["file_id"]]
        assert record["freeze_policy"] == "exact_required"
    downloader = (CASE_DIR / "download_inputs.py").read_text(encoding="utf-8")
    assert 'EXACT_FREEZE_POLICY = "exact_required"' in downloader
    assert 'RESOLVED_FREEZE_POLICY = "exact_required_manifest"' in downloader
    assert "first-learning/null input freezes are prohibited" in downloader
    injection = (CASE_DIR / "inject_corrupted_cache.py").read_text(encoding="utf-8")
    assert "flip_first_byte_in_task_local_copy_of_filtered_h5" in injection
    assert "SHA-256 mismatch for filtered_h5" in injection
    assert '"canonical_inputs_modified": False' in injection


def test_native_exit_backend_and_reconciliation_are_fail_closed(monkeypatch) -> None:
    driver = _load_driver()
    monkeypatch.delenv("PROCESSOR_ARCHITECTURE", raising=False)
    parent_before = dict(driver.os.environ)
    child, evidence = driver._r_subprocess_environment(dict(driver.os.environ))
    assert child["PROCESSOR_ARCHITECTURE"] == "AMD64"
    assert evidence["processor_architecture_restored"] is True
    assert dict(driver.os.environ) == parent_before
    with pytest.raises(driver.CaseError):
        driver._r_subprocess_environment({**driver.os.environ, "PROCESSOR_ARCHITECTURE": "ARM64"})
    python_code = (CASE_DIR / "case_driver.py").read_text(encoding="utf-8")
    assert "completion_marker.unlink(missing_ok=True)" in python_code
    assert '"shutdown_mode": "native_exit"' in python_code
    assert '"native_zero_with_forbidden_output_accepted": False' in python_code
    assert "_r_subprocess_environment" in python_code
    assert "GetNativeSystemInfo" in python_code
    assert "run_pipeline_transaction.R" not in python_code
    assert "exit-helper" not in python_code
    assert 'evidence_mode = "fresh" if mode == "run" else "resume"' in python_code
    assert 'f"pipeline-{evidence_mode}.complete.json"' in python_code
    assert 'f"pipeline-{evidence_mode}.process.json"' in python_code
    assert 'f"pipeline-{evidence_mode}-native-exit-evidence.json"' in python_code
    assert "EXPECTED_INPUT_MANIFEST_SHA256" in python_code
    assert "EXPECTED_RENV_LOCK_SHA256" in python_code
    assert 'f"renv_lock={EXPECTED_RENV_LOCK_SHA256}|' in python_code
    assert 'input={input_manifest_hash}|' in python_code
    assert '"materialization": "direct_read_no_copy"' in python_code
    assert "validate_environment_cache.R" in python_code
    exporter_code = (CASE_DIR / "export_expected_output.py").read_text(encoding="utf-8")
    assert '"pipeline-fresh.log"' in exporter_code
    assert '"pipeline-run.log"' not in exporter_code
    r_code = (CASE_DIR / "run_pipeline.R").read_text(encoding="utf-8")
    for required in (
        "options(stringsAsFactors = FALSE, warn = 1L)",
        'identical(getOption("warn"), 1L)',
        'warning_evidence_path <- file.path(run_root, "logs", "pipeline-warnings.json")',
        'category = "sctransform_theta_iteration_limit"',
        'category = "sctransform_glm_nb_alternation_limit"',
        'category = "api_compatibility_warning"',
        'category = "numerical_integrity_warning"',
        'category = "spatial_integrity_warning"',
        'category = "unclassified_warning"',
        "unknown warnings fail closed until explicitly classified",
        "write_warning_evidence()",
        'log_event(paste("warning", stage_key, classification$category, sanitized_message',
        '"Release-blocking warning in "',
        "Stage ",
        "release-blocking warning occurrence(s)",
        "Spatial_assay_cells_equals_image_cells_equals_coordinate_barcodes",
        'c("assay_cells", "image_cells")',
        'c("image_cells", "assay_cells")',
        'c("assay_cells", "coordinates")',
        'c("coordinates", "assay_cells")',
        'c("image_cells", "coordinates")',
        'c("coordinates", "image_cells")',
        "the contract is not relaxed",
        'scale_factors[["lowres"]]',
        'source_space = "vendor_full_resolution_pixels"',
        'target_space = "loaded_low_resolution_image_pixels"',
        'vst.flavor = analysis$sct_vst_flavor',
        'method = analysis$sct_method',
        'model_arguments$method, "glmGamPoi_offset"',
        'model_arguments$vst.flavor, "v2"',
        "model_arguments$glmGamPoi_check",
        'observed_backend_versions[["glmGamPoi"]]',
        'write_json_atomic(list(',
        'shutdown_mode = "native_exit"',
        'warning_allowlist_used = FALSE',
        'parse_finite_numeric_field(positions[[column]], column)',
    ):
        assert required in r_code
    assert "suppressWarnings" not in r_code
    assert "muffleWarning" not in r_code
    for required in (
        '"logs/pipeline-warnings.json"',
        'warning_evidence.get("blocking_warning_occurrences") != 0',
        '"sctransform_theta_iteration_limit"',
        '"sctransform_glm_nb_alternation_limit"',
        "runtime warning ledger contains an API/numerical/spatial/unknown blocker",
    ):
        assert required in python_code
    pre_execution_contract = (
        CASE_DIR / "request.json",
        CASE_DIR / "workflow.plan.json",
        CASE_DIR / "ANALYSIS_DESIGN.md",
        CASE_DIR / "config" / "analysis-params.json",
        CASE_DIR / "run_pipeline.R",
    )
    combined = "\n".join(path.read_text(encoding="utf-8") for path in pre_execution_contract)
    assert "2,695" not in combined and "2,696" not in combined
    assert "2695" not in combined and "2696" not in combined


def test_malformed_coordinate_field_fails_without_warning_suppression() -> None:
    rscript = _rscript_for_contract_test()
    if not rscript:
        pytest.skip("Rscript is unavailable for the executable coordinate negative control")
    r_code = (CASE_DIR / "run_pipeline.R").read_text(encoding="utf-8")
    start = r_code.index("parse_finite_numeric_field <- function")
    end = r_code.index('\ns20 <- run_stage("S20_COORD_IMAGE_QC"', start)
    helper = r_code[start:end]
    with tempfile.TemporaryDirectory(prefix="baa-visium-coordinate-negative-") as temporary:
        script = Path(temporary) / "negative-coordinate.R"
        script.write_text(
            helper + "\nparse_finite_numeric_field(c('1', 'bad-coordinate'), 'array_row')\n",
            encoding="utf-8",
        )
        process = subprocess.run(
            [rscript, "--vanilla", str(script)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
    assert process.returncode != 0
    combined = process.stdout + process.stderr
    assert "Malformed numeric tissue position field array_row: bad-coordinate" in combined
    assert "Warning" not in combined


def test_warning_is_logged_printed_and_blocks_stage_promotion() -> None:
    rscript = _rscript_for_contract_test()
    if not rscript:
        pytest.skip("Rscript is unavailable for the executable warning-gate negative control")
    r_code = (CASE_DIR / "run_pipeline.R").read_text(encoding="utf-8")
    start = r_code.index("execute_stage_producer <- function")
    end = r_code.index("\nrun_stage <- function", start)
    helper = r_code[start:end]
    with tempfile.TemporaryDirectory(prefix="baa-visium-warning-negative-") as temporary:
        root = Path(temporary)
        staging = root / "staging"
        final = root / "final"
        ledger = root / "warning-ledger.txt"
        r_test = "\n".join(
            (
                "options(warn = 1L)",
                helper,
                f"staging_dir <- {json.dumps(staging.as_posix())}",
                f"final_dir <- {json.dumps(final.as_posix())}",
                f"ledger_path <- {json.dumps(ledger.as_posix())}",
                "dir.create(staging_dir, recursive = TRUE)",
                "producer <- function(path) { writeLines('partial', file.path(path, 'partial.txt')); warning('NEGATIVE_WARNING_SENTINEL'); invisible(TRUE) }",
                "recorder <- function(condition, key) writeLines(paste(key, conditionMessage(condition), sep = '|'), ledger_path)",
                "writer <- function() { if (!file.exists(ledger_path)) stop('warning ledger was not written') }",
                "counter <- function(key) 1L",
                "execute_stage_producer(producer, staging_dir, 'NEGATIVE_STAGE', recorder, writer, counter)",
                "dir.create(final_dir)",
            )
        )
        script = root / "warning-negative.R"
        script.write_text(r_test + "\n", encoding="utf-8")
        process = subprocess.run(
            [rscript, "--vanilla", str(script)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        assert process.returncode != 0
        assert "NEGATIVE_WARNING_SENTINEL" in process.stderr
        assert "Stage NEGATIVE_STAGE emitted 1 release-blocking warning occurrence" in process.stderr
        assert ledger.read_text(encoding="utf-8").strip() == "NEGATIVE_STAGE|NEGATIVE_WARNING_SENTINEL"
        assert (staging / "partial.txt").is_file()
        assert not final.exists()


def test_public_expected_output_tooling_is_present() -> None:
    exporter = CASE_DIR / "export_expected_output.py"
    verifier = CASE_DIR / "verify_expected_output.py"
    assert exporter.is_file() and verifier.is_file()
    exporter_code = exporter.read_text(encoding="utf-8")
    verifier_code = verifier.read_text(encoding="utf-8")
    for required in (
        "canonical run was not executed by the current candidate run_pipeline.R",
        "derived_artifacts_only",
        "artifact_ledger.jsonl",
        "fault-before-completion-marker",
        "checkpoint-resume-reuse.json",
        "corrupted-cache-negative-control.json",
        "verification-summary.json",
    ):
        assert required in exporter_code
    for required in (
        "expected-output-manifest.json",
        "original/final figure hash mismatch",
        "private absolute path",
        "forbidden distributed artifact",
        "verification-summary evidence hash binding mismatch",
    ):
        assert required in verifier_code


def test_unpublished_expected_output_fails_with_release_blocker() -> None:
    unpublished = CASE_DIR / "expected-output-not-published-test-fixture"
    assert not unpublished.exists()
    process = subprocess.run(
        [sys.executable, str(CASE_DIR / "verify_expected_output.py"), "--output-root", str(unpublished)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert process.returncode == 2
    assert "expected-output not published until a fresh Bioconductor 3.21 native-exit run and native review pass" in process.stderr


def test_external_input_cache_reuse_is_positive_and_tamper_fails_closed() -> None:
    driver = _load_driver()
    with tempfile.TemporaryDirectory(prefix="baa-visium-input-cache-") as temporary:
        root = Path(temporary)
        input_root = root / "shared-input-cache"
        spatial = input_root / "spatial"
        spatial.mkdir(parents=True)
        h5 = input_root / "fixture.h5"
        archive = input_root / "spatial.tar.gz"
        h5.write_bytes(b"frozen-h5-fixture")
        (spatial / "scalefactors_json.json").write_text("{}\n", encoding="utf-8")
        (spatial / "tissue_hires_image.png").write_bytes(b"hires-fixture")
        (spatial / "tissue_lowres_image.png").write_bytes(b"lowres-fixture")
        with tarfile.open(archive, "w:gz") as bundle:
            bundle.add(spatial, arcname="spatial")

        manifest = {
            "schema_version": "1.0",
            "dataset_id": "fixture",
            "sample_id": "fixture",
            "license": {"spdx": "CC-BY-4.0"},
            "files": [
                {
                    "file_id": "filtered_h5",
                    "role": "matrix",
                    "url": "https://invalid.example/fixture.h5",
                    "filename": h5.name,
                    "expected_size_bytes": h5.stat().st_size,
                    "expected_sha256": driver.sha256_file(h5),
                    "freeze_policy": "exact_required",
                },
                {
                    "file_id": "spatial_archive",
                    "role": "spatial",
                    "url": "https://invalid.example/spatial.tar.gz",
                    "filename": archive.name,
                    "expected_size_bytes": archive.stat().st_size,
                    "expected_sha256": driver.sha256_file(archive),
                    "freeze_policy": "exact_required",
                },
            ],
            "required_extracted_assets": [
                "spatial/scalefactors_json.json",
                "spatial/tissue_hires_image.png",
                "spatial/tissue_lowres_image.png",
            ],
        }

        def make_run(name: str) -> Path:
            run = root / name
            (run / "00_request").mkdir(parents=True)
            (run / "03_scripts").mkdir(parents=True)
            (run / "logs").mkdir(parents=True)
            (run / "00_request" / "input-manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            shutil.copy2(CASE_DIR / "download_inputs.py", run / "03_scripts" / "download_inputs.py")
            return run

        positive_run = make_run("positive")
        evidence = driver.fetch_inputs(positive_run, input_root)
        assert evidence["reuse"] is True
        assert evidence["materialization"] == "direct_read_no_copy"
        assert evidence["download_performed"] is False

        tampered = bytearray(h5.read_bytes())
        tampered[0] ^= 0x01
        h5.write_bytes(tampered)
        tampered_run = make_run("tampered")
        with pytest.raises(driver.CaseError, match="subprocess failed with exit code 2"):
            driver.fetch_inputs(tampered_run, input_root)
        assert "SHA-256 mismatch for filtered_h5" in (
            tampered_run / "logs" / "input-fetch.log"
        ).read_text(encoding="utf-8")


def test_environment_cache_exact_lock_positive_and_tamper_negative(monkeypatch) -> None:
    driver = _load_driver()
    with tempfile.TemporaryDirectory(prefix="baa-visium-env-cache-") as temporary:
        cache_root = Path(temporary)
        env_root = cache_root / "reviewed-cache"
        (env_root / "renv").mkdir(parents=True)
        lock = env_root / "renv.lock"
        lock.write_bytes(b"reviewed-lock")
        expected = driver.sha256_file(lock)
        monkeypatch.setattr(driver, "EXPECTED_RENV_LOCK_SHA256", expected)
        monkeypatch.setattr(driver, "environment_root_for", lambda _cache: env_root)
        for path in (
            env_root / "environment.locked.json",
            env_root / "environment.probe.json",
            env_root / "renv-status.json",
            env_root / ".Rprofile",
            env_root / "renv" / "activate.R",
        ):
            path.write_text("{}\n", encoding="utf-8")
        assert driver._complete_cached_environment_root(cache_root) == env_root
        lock.write_bytes(b"tampered-lock")
        with pytest.raises(driver.CaseError, match="renv.lock differs"):
            driver._complete_cached_environment_root(cache_root)
