#!/usr/bin/env python3
"""Validate private P0 teaching-case execution evidence against live artifacts."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
from typing import Any

import jsonschema


SKILL_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = SKILL_ROOT / "assets" / "private-corpus-index" / "p0-teaching-case-executions.json"
DEFAULT_SCHEMA = SKILL_ROOT / "references" / "schemas" / "p0-teaching-case-execution.schema.json"
DEFAULT_CANDIDATES = SKILL_ROOT / "references" / "p0-teaching-cases.json"
DEFAULT_LOCATORS = SKILL_ROOT / "assets" / "private-corpus-index" / "p0-teaching-case-local-availability.json"


class ExecutionValidationError(RuntimeError):
    """Raised when an evidence document cannot be loaded."""


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExecutionValidationError(f"cannot_load_json:{path}:{exc}") from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inside(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise ExecutionValidationError(f"path_escapes_task_root:{relative}") from exc
    return candidate


def _verify_file(root: Path, evidence: dict[str, Any], label: str, errors: list[str]) -> Path | None:
    try:
        path = _inside(root, evidence["relative_path"])
    except (KeyError, ExecutionValidationError) as exc:
        errors.append(f"{label}:{exc}")
        return None
    if not path.is_file():
        errors.append(f"{label}:missing:{evidence.get('relative_path')}")
        return None
    size = path.stat().st_size
    if size != evidence.get("size_bytes"):
        errors.append(f"{label}:size_mismatch:{size}")
    digest = _sha256(path)
    if digest != evidence.get("sha256"):
        errors.append(f"{label}:hash_mismatch:{digest}")
    return path


def _verify_external_file(evidence: dict[str, Any], label: str, errors: list[str]) -> Path | None:
    path = Path(evidence.get("path", ""))
    if not path.is_file():
        errors.append(f"{label}:missing")
        return None
    size = path.stat().st_size
    if size != evidence.get("size_bytes"):
        errors.append(f"{label}:size_mismatch:{size}")
    digest = _sha256(path)
    if digest != evidence.get("sha256"):
        errors.append(f"{label}:hash_mismatch:{digest}")
    return path


def _schema_errors(document: Any, schema: Any) -> list[str]:
    validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
    return [
        "schema:" + "/".join(str(part) for part in error.absolute_path) + f":{error.message}"
        for error in sorted(validator.iter_errors(document), key=lambda item: list(item.absolute_path))
    ]


def _tree_contract(root: Path, *, exclude: set[str] | None = None) -> dict[str, Any]:
    """Rebuild the hash contract used by the P0 single-cell run manager."""
    excluded = exclude or set()
    records: list[dict[str, Any]] = []
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        if relative in excluded:
            continue
        record = {
            "relative_path": relative,
            "size_bytes": path.stat().st_size,
            "sha256": _sha256(path),
        }
        records.append(record)
        stable = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest.update((stable + "\n").encode("utf-8"))
    return {"tree_sha256": digest.hexdigest(), "file_count": len(records), "records": records}


def _png_dimensions(path: Path) -> tuple[int, int] | None:
    with path.open("rb") as handle:
        header = handle.read(24)
    if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        return None
    return int.from_bytes(header[16:20], "big"), int.from_bytes(header[20:24], "big")


def _validate_single_cell_declared_inputs(
    execution: dict[str, Any],
    candidate: dict[str, Any],
    locator_by_ref: dict[str, Any],
    prefix: str,
    errors: list[str],
) -> list[tuple[dict[str, Any], dict[str, Any] | None]]:
    inputs = execution.get("inputs", {})
    artifacts = inputs.get("artifacts", []) if isinstance(inputs, dict) else []
    if not isinstance(artifacts, list):
        errors.append(f"{prefix}:single_cell_input_artifacts_invalid")
        return []
    refs = [item.get("locator_ref") for item in artifacts if isinstance(item, dict)]
    expected_refs = candidate.get("availability", {}).get("local_locator_refs", [])
    if len(refs) != len(set(refs)):
        errors.append(f"{prefix}:duplicate_single_cell_input_locator")
    if set(refs) != set(expected_refs) or len(refs) != len(expected_refs):
        errors.append(f"{prefix}:single_cell_input_locator_set_mismatch")
    public_integrity = {
        item.get("locator_ref"): item
        for item in candidate.get("availability", {}).get("integrity_evidence", [])
        if isinstance(item, dict)
    }
    pairs: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
    for item in artifacts:
        if not isinstance(item, dict):
            errors.append(f"{prefix}:single_cell_input_artifact_not_object")
            continue
        locator_ref = item.get("locator_ref")
        locator = locator_by_ref.get(locator_ref)
        public = public_integrity.get(locator_ref)
        if locator is None:
            errors.append(f"{prefix}:missing_private_input_locator:{locator_ref}")
        elif (
            locator.get("expected_sha256") != item.get("sha256")
            or locator.get("expected_size_bytes") != item.get("size_bytes")
        ):
            errors.append(f"{prefix}:input_locator_evidence_mismatch:{locator_ref}")
        if public is None or public.get("sha256") != item.get("sha256") or public.get("size_bytes") != item.get("size_bytes"):
            errors.append(f"{prefix}:input_public_evidence_mismatch:{locator_ref}")
        pairs.append((item, locator))
    expected_total = candidate.get("availability", {}).get("total_size_bytes")
    if inputs.get("total_size_bytes") != expected_total or sum(
        int(item.get("size_bytes", 0)) for item in artifacts if isinstance(item, dict)
    ) != expected_total:
        errors.append(f"{prefix}:single_cell_input_total_size_mismatch")
    public_metadata = candidate.get("public_metadata", {})
    if inputs.get("metadata_sidecar_sha256") != public_metadata.get("sha256"):
        errors.append(f"{prefix}:single_cell_metadata_hash_mismatch")
    return pairs


def _validate_single_cell_execution(
    execution: dict[str, Any],
    candidate: dict[str, Any],
    locator_by_ref: dict[str, Any],
    prefix: str,
    errors: list[str],
    *,
    verify_live: bool,
) -> dict[str, int]:
    counts = {
        "verified_files": 0,
        "verified_native_figures": 0,
        "verified_formal_files": 0,
        "verified_formal_artifacts": 0,
        "verified_checkpoints": 0,
    }
    input_pairs = _validate_single_cell_declared_inputs(
        execution, candidate, locator_by_ref, prefix, errors
    )
    if not verify_live:
        return counts

    root = Path(execution.get("task_root", ""))
    if not root.is_dir():
        errors.append(f"{prefix}:task_root_missing")
        return counts
    formal = execution.get("execution", {})
    try:
        formal_root = _inside(root, formal.get("run_root", ""))
    except ExecutionValidationError as exc:
        errors.append(f"{prefix}:formal_run:{exc}")
        return counts
    if not formal_root.is_dir() or formal.get("run_id") != formal_root.name:
        errors.append(f"{prefix}:single_cell_formal_run_root_invalid")
        return counts

    for evidence, locator in input_pairs:
        if locator is None:
            continue
        source = Path(locator.get("path", ""))
        if not source.is_file():
            errors.append(f"{prefix}:input_file_missing:{evidence.get('locator_ref')}")
            continue
        if source.stat().st_size != evidence.get("size_bytes"):
            errors.append(f"{prefix}:input_size_mismatch:{evidence.get('locator_ref')}")
        if _sha256(source) != evidence.get("sha256"):
            errors.append(f"{prefix}:input_hash_mismatch:{evidence.get('locator_ref')}")
        else:
            counts["verified_files"] += 1

    metadata = candidate.get("public_metadata", {})
    metadata_path = SKILL_ROOT / "references" / str(metadata.get("path", ""))
    if not metadata_path.is_file() or _sha256(metadata_path) != execution.get("inputs", {}).get("metadata_sidecar_sha256"):
        errors.append(f"{prefix}:metadata_sidecar_live_mismatch")
    else:
        counts["verified_files"] += 1

    environment = execution.get("environment", {})
    manifest_path = _verify_file(root, environment.get("manifest", {}), f"{prefix}:environment_manifest", errors)
    marker_path = _verify_file(root, environment.get("marker", {}), f"{prefix}:environment_marker", errors)
    counts["verified_files"] += sum(path is not None for path in [manifest_path, marker_path])
    manifest = _load_json(manifest_path) if manifest_path else {}
    marker = _load_json(marker_path) if marker_path else {}
    manifest_env = manifest.get("environment", {})
    if manifest.get("state") != "frozen" or manifest.get("authorization", {}).get("global_base_package_changes") is not False:
        errors.append(f"{prefix}:relocated_environment_not_frozen_or_task_local")
    if manifest_env.get("lock_hash") != environment.get("lock_hash") or marker.get("lock_hash") != environment.get("lock_hash"):
        errors.append(f"{prefix}:relocated_environment_lock_mismatch")
    if manifest_env.get("source_tree_sha256") != environment.get("source_target_tree_sha256"):
        errors.append(f"{prefix}:relocated_environment_source_tree_mismatch")
    tree_verification = manifest.get("tree_verification", {})
    if (
        tree_verification.get("target_tree_sha256") != environment.get("source_target_tree_sha256")
        or tree_verification.get("source_tree_sha256") != environment.get("source_target_tree_sha256")
        or tree_verification.get("exact") is not True
    ):
        errors.append(f"{prefix}:relocated_environment_target_tree_mismatch")
    if len(tree_verification.get("source_prefix_backrefs", [])) != environment.get("external_runtime_references"):
        errors.append(f"{prefix}:relocated_environment_external_reference_mismatch")
    package_locks = marker.get("package_locks", {})
    if package_locks.get("target_active_external_locator_count") != environment.get("active_editable_or_vcs_locators"):
        errors.append(f"{prefix}:relocated_environment_external_locator_mismatch")
    env_path = Path(manifest_env.get("path", ""))
    if not env_path.is_dir() or not env_path.resolve().is_relative_to(root.resolve()):
        errors.append(f"{prefix}:relocated_environment_path_not_task_local")
    elif sum(1 for item in env_path.rglob("*") if item.is_file() and item.suffix.lower() in {".pyc", ".pyo"}) != environment.get("generated_bytecode_files"):
        errors.append(f"{prefix}:relocated_environment_generated_bytecode_mismatch")

    code_paths: dict[str, Path] = {}
    for name, evidence in execution.get("code", {}).items():
        path = _verify_file(root, evidence, f"{prefix}:code:{name}", errors)
        if path is not None:
            code_paths[name] = path
            counts["verified_files"] += 1

    delivery_paths: dict[str, Path] = {}
    for name, evidence in execution.get("delivery", {}).items():
        if isinstance(evidence, dict) and {"relative_path", "sha256", "size_bytes"}.issubset(evidence):
            path = _verify_file(root, evidence, f"{prefix}:delivery:{name}", errors)
            if path is not None:
                delivery_paths[name] = path
                counts["verified_formal_files"] += 1
    native_paths: dict[str, Path] = {}
    for name in ["review_source", "checkpoint"]:
        evidence = execution.get("native_review", {}).get(name, {})
        path = _verify_file(root, evidence, f"{prefix}:native_review:{name}", errors)
        if path is not None:
            native_paths[name] = path
            counts["verified_formal_files"] += 1

    manifest_path = delivery_paths.get("run_manifest")
    run_manifest = _load_json(manifest_path) if manifest_path else {}
    expected_stages = ["00-intake", "01-environment", "02-analysis", "03-native-review", "04-delivery-report"]
    stages = run_manifest.get("stages", {})
    if (
        run_manifest.get("run_id") != formal.get("run_id")
        or run_manifest.get("mode") != "reduced"
        or run_manifest.get("state") != execution.get("delivery", {}).get("state")
        or run_manifest.get("final_delivery_checkpoint") != execution.get("delivery", {}).get("latest_valid_checkpoint")
        or list(stages) != expected_stages
    ):
        errors.append(f"{prefix}:single_cell_run_manifest_contract_mismatch")
    cached = formal.get("cached_stages_revalidated", [])
    for stage_id in expected_stages:
        stage = stages.get(stage_id, {})
        attempts = stage.get("attempts", [])
        if stage_id in cached:
            if stage.get("status") != "revalidated-cached" or not any(item.get("status") == "revalidated-cached" for item in attempts):
                errors.append(f"{prefix}:cached_stage_not_revalidated:{stage_id}")
        elif stage.get("status") != "checkpointed":
            errors.append(f"{prefix}:delivery_stage_not_checkpointed:{stage_id}")

    checkpoints: dict[str, dict[str, Any]] = {}
    for stage_id in expected_stages:
        stage_root = formal_root / "checkpoints" / stage_id
        checkpoint_path = stage_root / "checkpoint.json"
        if not checkpoint_path.is_file():
            errors.append(f"{prefix}:checkpoint_missing:{stage_id}")
            continue
        checkpoint = _load_json(checkpoint_path)
        checkpoints[stage_id] = checkpoint
        observed = _tree_contract(stage_root, exclude={"checkpoint.json"})
        if (
            checkpoint.get("stage") != stage_id
            or checkpoint.get("payload_tree_sha256") != observed["tree_sha256"]
            or checkpoint.get("payload_file_count") != observed["file_count"]
        ):
            errors.append(f"{prefix}:checkpoint_payload_contract_mismatch:{stage_id}")
        else:
            counts["verified_checkpoints"] += 1

    analysis_checkpoint = checkpoints.get("02-analysis", {})
    validation = analysis_checkpoint.get("validation", {})
    if (
        validation.get("analysis_mode") != formal.get("analysis_mode")
        or validation.get("artifact_count") != formal.get("artifact_count")
        or validation.get("output_tree_sha256") != formal.get("analysis_output_tree_sha256")
        or validation.get("qa_ok") is not True
        or validation.get("thread_environment") != environment.get("thread_limits")
    ):
        errors.append(f"{prefix}:analysis_checkpoint_validation_mismatch")

    output_root = formal_root / "checkpoints" / "02-analysis" / "pipeline-output"
    output_contract = _tree_contract(output_root)
    if output_contract["tree_sha256"] != formal.get("analysis_output_tree_sha256"):
        errors.append(f"{prefix}:analysis_output_tree_hash_mismatch")
    artifact_index_path = delivery_paths.get("artifact_index")
    artifact_index = _load_json(artifact_index_path) if artifact_index_path else {}
    artifacts = artifact_index.get("artifacts", [])
    artifact_by_path = {
        item.get("relative_path"): item for item in artifacts if isinstance(item, dict)
    }
    actual_paths = {
        item.relative_to(output_root).as_posix()
        for item in output_root.rglob("*")
        if item.is_file()
    } - {"reports/artifact-index.json"}
    if (
        len(artifacts) != formal.get("artifact_count")
        or len(artifact_by_path) != len(artifacts)
        or set(artifact_by_path) != actual_paths
    ):
        errors.append(f"{prefix}:artifact_index_exact_set_mismatch")
    for relative, artifact in artifact_by_path.items():
        try:
            path = _inside(output_root, str(relative))
        except ExecutionValidationError as exc:
            errors.append(f"{prefix}:artifact_index:{exc}")
            continue
        if (
            not path.is_file()
            or path.stat().st_size != artifact.get("size_bytes")
            or _sha256(path) != artifact.get("sha256")
        ):
            errors.append(f"{prefix}:artifact_index_integrity_mismatch:{relative}")
        else:
            counts["verified_formal_artifacts"] += 1

    execution_path = formal_root / "checkpoints" / "02-analysis" / "execution.json"
    execution_record = _load_json(execution_path) if execution_path.is_file() else {}
    if (
        execution_record.get("returncode") != formal.get("fresh_returncode")
        or execution_record.get("environment_lock_hash") != environment.get("lock_hash")
        or execution_record.get("pipeline_sha256") != execution.get("code", {}).get("pipeline", {}).get("sha256")
        or execution_record.get("input_config_sha256") != execution.get("code", {}).get("input_config", {}).get("sha256")
        or execution_record.get("thread_environment") != environment.get("thread_limits")
    ):
        errors.append(f"{prefix}:single_cell_execution_record_mismatch")

    report_root = output_root / "reports"
    qa = _load_json(report_root / "qa-machine.json")
    profile = _load_json(report_root / "input-profile.json")
    annotation = _load_json(report_root / "annotation-summary.json")
    doublet = _load_json(report_root / "doublet-decisions.json")
    memory = _load_json(report_root / "memory-safety.json")
    sampling = _load_json(report_root / "sampling-manifest.json")
    versions = _load_json(report_root / "environment-versions.json")
    summary = formal.get("analysis_summary", {})
    qc_lines = (output_root / "tables" / "nucleus-qc.tsv").read_text(encoding="utf-8").splitlines()
    qc_header = qc_lines[0].split("\t") if qc_lines else []
    donor_index = qc_header.index("donor_id") if "donor_id" in qc_header else -1
    retained_index = qc_header.index("retained_final") if "retained_final" in qc_header else -1
    observed_retained = {donor: 0 for donor in summary.get("retained_by_donor", {})}
    if donor_index < 0 or retained_index < 0:
        errors.append(f"{prefix}:nucleus_qc_required_columns_missing")
    else:
        for line in qc_lines[1:]:
            fields = line.split("\t")
            if len(fields) <= max(donor_index, retained_index):
                continue
            donor = fields[donor_index]
            if donor in observed_retained and fields[retained_index].lower() == "true":
                observed_retained[donor] += 1
    if (
        profile.get("analyzed_input_barcodes") != summary.get("sampled_nuclei")
        or profile.get("retained_nuclei") != summary.get("retained_nuclei")
        or observed_retained != summary.get("retained_by_donor")
        or profile.get("selected_features") != summary.get("selected_features")
        or annotation.get("clusters") != summary.get("leiden_clusters")
        or annotation.get("coarse_labels") != summary.get("coarse_labels")
        or annotation.get("unknown_or_ambiguous_nuclei") != summary.get("unknown_or_ambiguous_nuclei")
        or qa.get("inferential_tests_performed") != summary.get("inferential_tests_performed")
    ):
        errors.append(f"{prefix}:single_cell_analysis_summary_mismatch")
    declared_inputs = execution.get("inputs", {})
    profile_files = profile.get("input_files", [])
    if (
        profile.get("metadata_sidecar_sha256") != declared_inputs.get("metadata_sidecar_sha256")
        or profile.get("original_features") != declared_inputs.get("features_per_matrix")
        or profile.get("total_filtered_matrix_barcodes") != declared_inputs.get("total_filtered_barcodes")
        or {item.get("sha256") for item in profile_files} != {item.get("sha256") for item in declared_inputs.get("artifacts", [])}
    ):
        errors.append(f"{prefix}:single_cell_input_profile_mismatch")
    sampling_contract = declared_inputs.get("reduced_sampling", {})
    if (
        sampling.get("method") != sampling_contract.get("method")
        or sum(int(item.get("sampled_barcodes", 0)) for item in sampling.get("donors", [])) != sampling_contract.get("sampled_barcodes")
        or [item.get("seed") for item in sampling.get("donors", [])] != sampling_contract.get("seeds")
        or sampling.get("full_matrix_loaded_before_sampling") != sampling_contract.get("full_sparse_matrix_loaded_before_sampling")
        or any(not item.get("membership_sha256") for item in sampling.get("donors", []))
    ):
        errors.append(f"{prefix}:reduced_sampling_contract_mismatch")
    doublet_contract = formal.get("doublet_handling", {})
    if (
        doublet.get("scoring_all_completed") != doublet_contract.get("scoring_all_donors_completed")
        or doublet.get("filter_policy") != doublet_contract.get("filter_policy")
        or doublet.get("all_donors_filter_eligible") != doublet_contract.get("all_donors_filter_eligible")
        or doublet.get("filtering_applied") != doublet_contract.get("filtering_applied")
        or doublet.get("doublet_cleared") != doublet_contract.get("doublet_cleared")
        or doublet.get("policy_reason") != doublet_contract.get("reason")
    ):
        errors.append(f"{prefix}:doublet_handling_contract_mismatch")
    memory_contract = formal.get("memory_contract", {})
    snapshots = memory.get("snapshots", [])
    if (
        memory.get("donor_processing") != memory_contract.get("donor_processing")
        or memory.get("full_gene_merged_matrix_created") != memory_contract.get("full_gene_merged_matrix_created")
        or memory.get("dense_full_count_matrix_created") != memory_contract.get("dense_full_count_matrix_created")
        or max((item.get("process_peak_working_set_gb", 0) for item in snapshots), default=0) != memory_contract.get("peak_working_set_gb")
        or min((item.get("system_available_gb", float("inf")) for item in snapshots), default=0) != memory_contract.get("minimum_observed_system_available_gb")
        or memory.get("thread_environment_observed") != environment.get("thread_limits")
    ):
        errors.append(f"{prefix}:memory_contract_mismatch")
    version_map = {"scikit-learn": "scikit_learn"}
    for name, declared in environment.get("versions", {}).items():
        observed = versions.get(version_map.get(name, name))
        if observed is not None and observed != declared:
            errors.append(f"{prefix}:environment_version_mismatch:{name}")
    if (
        qa.get("ok") is not True
        or qa.get("qa_status") != formal.get("machine_qa")
        or qa.get("analysis_mode") != formal.get("analysis_mode")
        or qa.get("full_data_analyzed") is not False
        or qa.get("case_control_estimand_available") is not False
        or qa.get("doublet_cleared") is not False
        or qa.get("native_visual_review") != "pending"
    ):
        errors.append(f"{prefix}:machine_qa_boundary_mismatch")

    native_checkpoint = checkpoints.get("03-native-review", {})
    native_review = _load_json(native_paths["review_source"]) if "review_source" in native_paths else {}
    checkpoint_review = formal_root / "checkpoints" / "03-native-review" / "native-visual-review.json"
    if (
        native_checkpoint.get("validation", {}).get("all_images_opened_natively") is not True
        or native_checkpoint.get("validation", {}).get("figure_count") != execution.get("native_review", {}).get("figure_count")
        or native_review.get("review_outcome") != execution.get("native_review", {}).get("decision")
        or native_review.get("all_images_opened_natively") is not True
        or not checkpoint_review.is_file()
        or _sha256(checkpoint_review) != execution.get("native_review", {}).get("review_source", {}).get("sha256")
    ):
        errors.append(f"{prefix}:native_review_binding_mismatch")
    declared_figures = {
        item.get("relative_path"): item
        for item in native_checkpoint.get("declared_inputs", {}).get("figures", [])
    }
    reviewed_figures = {
        item.get("relative_path"): item for item in native_review.get("figures", [])
    }
    indexed_figures = {
        relative: item for relative, item in artifact_by_path.items() if str(relative).startswith("figures/")
    }
    if not (set(declared_figures) == set(reviewed_figures) == set(indexed_figures)) or len(indexed_figures) != 6:
        errors.append(f"{prefix}:native_review_figure_set_mismatch")
    for relative, indexed in indexed_figures.items():
        path = output_root / relative
        declared = declared_figures.get(relative, {})
        reviewed = reviewed_figures.get(relative, {})
        dimensions = _png_dimensions(path) if path.is_file() else None
        if (
            indexed.get("sha256") != declared.get("sha256")
            or indexed.get("sha256") != reviewed.get("sha256")
            or indexed.get("size_bytes") != declared.get("size_bytes")
            or indexed.get("size_bytes") != reviewed.get("size_bytes")
            or dimensions != (declared.get("width_px"), declared.get("height_px"))
            or reviewed.get("opened_natively") is not True
            or not reviewed.get("supports")
            or not reviewed.get("does_not_support")
        ):
            errors.append(f"{prefix}:native_review_figure_binding_mismatch:{relative}")
        else:
            counts["verified_native_figures"] += 1

    delivery_checkpoint = checkpoints.get("04-delivery-report", {})
    binding_path = formal_root / "checkpoints" / "04-delivery-report" / "delivery-binding.json"
    binding = _load_json(binding_path) if binding_path.is_file() else {}
    delivery = execution.get("delivery", {})
    if (
        delivery_checkpoint.get("validation", {}).get("analysis_artifact_count") != formal.get("artifact_count")
        or delivery_checkpoint.get("validation", {}).get("figure_note_sections") != 6
        or delivery_checkpoint.get("validation", {}).get("native_review_outcome") != execution.get("native_review", {}).get("decision")
        or delivery_checkpoint.get("payload_tree_sha256") != delivery.get("delivery_payload_tree_sha256")
        or binding.get("run_id") != formal.get("run_id")
        or binding.get("analysis_output_tree_sha256") != formal.get("analysis_output_tree_sha256")
        or binding.get("analysis_artifact_count") != formal.get("artifact_count")
        or binding.get("native_review_sha256") != execution.get("native_review", {}).get("review_source", {}).get("sha256")
        or binding.get("reviewed_figure_count") != execution.get("native_review", {}).get("figure_count")
        or binding.get("figure_notes_sha256") != delivery.get("final_figure_notes", {}).get("sha256")
    ):
        errors.append(f"{prefix}:delivery_binding_mismatch")
    final_notes_path = delivery_paths.get("final_figure_notes")
    if final_notes_path:
        final_notes = final_notes_path.read_text(encoding="utf-8")
        figure_sections = sum(1 for line in final_notes.splitlines() if line.startswith("## `") and line.endswith(".png`"))
        if (
            figure_sections != 6
            or execution.get("native_review", {}).get("decision") not in final_notes
            or "Native visual review: pending" in final_notes
            or "不能支持" not in final_notes
        ):
            errors.append(f"{prefix}:final_figure_notes_boundary_or_review_mismatch")
    if not execution.get("known_limitations") or "full-data" not in execution.get("maturity_scope", "").lower():
        errors.append(f"{prefix}:data_verified_scope_not_reduced_or_bounded")
    return counts


def _validate_spatial_declared_inputs(
    execution: dict[str, Any],
    candidate: dict[str, Any],
    locator_by_ref: dict[str, Any],
    prefix: str,
    errors: list[str],
) -> list[tuple[dict[str, Any], dict[str, Any] | None]]:
    inputs = execution.get("inputs", {})
    artifacts = inputs.get("artifacts", []) if isinstance(inputs, dict) else []
    if not isinstance(artifacts, list):
        errors.append(f"{prefix}:spatial_input_artifacts_invalid")
        return []
    refs = [item.get("locator_ref") for item in artifacts if isinstance(item, dict)]
    expected_refs = candidate.get("availability", {}).get("local_locator_refs", [])
    if len(refs) != len(set(refs)):
        errors.append(f"{prefix}:duplicate_spatial_input_locator")
    if set(refs) != set(expected_refs) or len(refs) != len(expected_refs):
        errors.append(f"{prefix}:spatial_input_locator_set_mismatch")
    public_integrity = {
        item.get("locator_ref"): item
        for item in candidate.get("availability", {}).get("integrity_evidence", [])
        if isinstance(item, dict)
    }
    pairs: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
    for item in artifacts:
        if not isinstance(item, dict):
            errors.append(f"{prefix}:spatial_input_artifact_not_object")
            continue
        locator_ref = item.get("locator_ref")
        locator = locator_by_ref.get(locator_ref)
        public = public_integrity.get(locator_ref)
        if locator is None:
            errors.append(f"{prefix}:missing_private_input_locator:{locator_ref}")
        elif (
            locator.get("expected_sha256") != item.get("sha256")
            or locator.get("expected_size_bytes") != item.get("size_bytes")
        ):
            errors.append(f"{prefix}:spatial_input_locator_evidence_mismatch:{locator_ref}")
        if (
            public is None
            or public.get("sha256") != item.get("sha256")
            or public.get("size_bytes") != item.get("size_bytes")
        ):
            errors.append(f"{prefix}:spatial_input_public_evidence_mismatch:{locator_ref}")
        pairs.append((item, locator))
    expected_total = candidate.get("availability", {}).get("total_size_bytes")
    if inputs.get("total_size_bytes") != expected_total or sum(
        int(item.get("size_bytes", 0)) for item in artifacts if isinstance(item, dict)
    ) != expected_total:
        errors.append(f"{prefix}:spatial_input_total_size_mismatch")
    return pairs


def _validate_spatial_execution(
    execution: dict[str, Any],
    candidate: dict[str, Any],
    locator_by_ref: dict[str, Any],
    prefix: str,
    errors: list[str],
    *,
    verify_live: bool,
) -> dict[str, int]:
    counts = {
        "verified_files": 0,
        "verified_native_figures": 0,
        "verified_formal_files": 0,
        "verified_formal_artifacts": 0,
        "verified_checkpoints": 0,
    }
    input_pairs = _validate_spatial_declared_inputs(
        execution, candidate, locator_by_ref, prefix, errors
    )
    if not verify_live:
        return counts

    root = Path(execution.get("task_root", ""))
    if not root.is_dir():
        errors.append(f"{prefix}:task_root_missing")
        return counts
    formal = execution.get("execution", {})
    try:
        formal_root = _inside(root, formal.get("run_root", ""))
    except ExecutionValidationError as exc:
        errors.append(f"{prefix}:spatial_formal_run:{exc}")
        return counts
    if not formal_root.is_dir():
        errors.append(f"{prefix}:spatial_formal_run_root_invalid")
        return counts

    for evidence, locator in input_pairs:
        if locator is None:
            continue
        source = Path(locator.get("path", ""))
        locator_ref = evidence.get("locator_ref")
        if not source.is_file():
            errors.append(f"{prefix}:spatial_input_file_missing:{locator_ref}")
            continue
        if source.stat().st_size != evidence.get("size_bytes"):
            errors.append(f"{prefix}:spatial_input_size_mismatch:{locator_ref}")
        if _sha256(source) != evidence.get("sha256"):
            errors.append(f"{prefix}:spatial_input_hash_mismatch:{locator_ref}")
        else:
            counts["verified_files"] += 1

    environment = execution.get("environment", {})
    environment_paths: dict[str, Path] = {}
    for name in ["manifest", "marker", "uv_lock", "requirements_lock", "pyproject", "model_probe"]:
        path = _verify_file(root, environment.get(name, {}), f"{prefix}:spatial_environment:{name}", errors)
        if path is not None:
            environment_paths[name] = path
            counts["verified_files"] += 1
    environment_manifest = _load_json(environment_paths["manifest"]) if "manifest" in environment_paths else {}
    environment_marker = _load_json(environment_paths["marker"]) if "marker" in environment_paths else {}
    model_probe = _load_json(environment_paths["model_probe"]) if "model_probe" in environment_paths else {}
    runtime_tree = environment_manifest.get("runtime_tree", {})
    declared_tree = environment.get("runtime_tree_at_lock", {})
    if (
        environment_manifest.get("state") != "frozen"
        or environment_manifest.get("backend") != environment.get("backend")
        or environment_manifest.get("platform") != environment.get("platform")
        or environment_manifest.get("lock_hash") != environment.get("lock_hash")
        or environment_marker.get("lock_hash") != environment.get("lock_hash")
        or environment_manifest.get("global_changes") is not False
        or environment_manifest.get("external_runtime_references") != 0
        or environment_marker.get("global_changes") is not False
        or environment_marker.get("external_runtime_references") != 0
    ):
        errors.append(f"{prefix}:spatial_environment_freeze_contract_mismatch")
    if runtime_tree != declared_tree or environment_marker.get("runtime_tree_sha256") != declared_tree.get("tree_sha256"):
        errors.append(f"{prefix}:spatial_runtime_tree_at_lock_mismatch")
    if environment_marker.get("environment_manifest_sha256") != environment.get("manifest", {}).get("sha256"):
        errors.append(f"{prefix}:spatial_environment_marker_manifest_hash_mismatch")
    if environment_marker.get("uv_lock_sha256") != environment.get("uv_lock", {}).get("sha256"):
        errors.append(f"{prefix}:spatial_environment_uv_lock_hash_mismatch")
    if environment_marker.get("requirements_lock_sha256") != environment.get("requirements_lock", {}).get("sha256"):
        errors.append(f"{prefix}:spatial_environment_requirements_hash_mismatch")
    if environment_manifest.get("packages") != environment.get("versions"):
        python_value = environment.get("versions", {}).get("python", "")
        package_values = {
            key: value for key, value in environment.get("versions", {}).items() if key != "python"
        }
        if environment_manifest.get("packages") != package_values or not str(environment_manifest.get("python", "")).startswith(str(python_value)):
            errors.append(f"{prefix}:spatial_environment_versions_mismatch")
    for field in ["task_root", "environment_path", "runtime_root", "managed_python", "pyvenv_home", "interpreter"]:
        path_value = Path(environment_manifest.get(field, ""))
        try:
            is_task_local = path_value.resolve().is_relative_to(root.resolve())
        except (OSError, RuntimeError):
            is_task_local = False
        if not is_task_local:
            errors.append(f"{prefix}:spatial_environment_path_not_task_local:{field}")
    interpreter_path = Path(environment_manifest.get("interpreter", ""))
    managed_python_path = Path(environment_manifest.get("managed_python", ""))
    pyvenv_path = Path(environment_manifest.get("environment_path", "")) / "pyvenv.cfg"
    for label, path, expected in [
        ("interpreter", interpreter_path, environment_manifest.get("interpreter_sha256")),
        ("managed_python", managed_python_path, environment_manifest.get("managed_python_sha256")),
        ("pyvenv_cfg", pyvenv_path, environment_manifest.get("pyvenv_cfg_sha256")),
    ]:
        if not path.is_file() or _sha256(path) != expected:
            errors.append(f"{prefix}:spatial_environment_binary_hash_mismatch:{label}")
    if (
        model_probe.get("environment_role") != "descriptive-core-only"
        or model_probe.get("spotiphy_model_ready") is not False
        or model_probe.get("install_attempted_in_this_environment") is not False
        or any(item.get("importable") is not False for item in model_probe.get("imports", {}).values())
    ):
        errors.append(f"{prefix}:spatial_model_probe_boundary_mismatch")

    code_paths: dict[str, Path] = {}
    for name in ["pipeline", "executor", "environment_preparer", "input_config"]:
        path = _verify_file(root, execution.get("code", {}).get(name, {}), f"{prefix}:spatial_code:{name}", errors)
        if path is not None:
            code_paths[name] = path
            counts["verified_files"] += 1
    finalizer_path = _verify_external_file(
        execution.get("code", {}).get("finalizer", {}), f"{prefix}:spatial_code:finalizer", errors
    )
    if finalizer_path is not None:
        counts["verified_files"] += 1

    delivery_paths: dict[str, Path] = {}
    delivery = execution.get("delivery", {})
    for name in ["run_manifest", "artifact_ledger", "artifact_index", "qa_report", "figure_notes", "analysis_design", "workflow_plan"]:
        path = _verify_file(root, delivery.get(name, {}), f"{prefix}:spatial_delivery:{name}", errors)
        if path is not None:
            delivery_paths[name] = path
            counts["verified_formal_files"] += 1
    for index, evidence in enumerate(delivery.get("failure_evidence", [])):
        path = _verify_file(root, evidence, f"{prefix}:spatial_failure_evidence:{index}", errors)
        if path is not None:
            delivery_paths[f"failure_evidence_{index}"] = path
            counts["verified_formal_files"] += 1

    native_paths: dict[str, Path] = {}
    for name in ["review_source", "checkpoint"]:
        path = _verify_file(root, execution.get("native_review", {}).get(name, {}), f"{prefix}:spatial_native_review:{name}", errors)
        if path is not None:
            native_paths[name] = path
            counts["verified_formal_files"] += 1

    run_manifest = _load_json(delivery_paths["run_manifest"]) if "run_manifest" in delivery_paths else {}
    expected_stages = formal.get("stage_ids", [])
    manifest_stages = run_manifest.get("stages", [])
    observed_stage_ids = [item.get("stage_id") for item in manifest_stages if isinstance(item, dict)]
    if (
        run_manifest.get("case_id") != execution.get("case_id")
        or run_manifest.get("run_id") != formal.get("run_id")
        or run_manifest.get("state") != formal.get("state")
        or run_manifest.get("mode") != "run"
        or run_manifest.get("latest_valid_checkpoint") != delivery.get("latest_valid_checkpoint")
        or run_manifest.get("artifact_count") != formal.get("artifact_count")
        or run_manifest.get("ledger_entry_count") != formal.get("ledger_entry_count")
        or run_manifest.get("environment_lock_hash") != environment.get("lock_hash")
        or observed_stage_ids != expected_stages
        or any(item.get("status") != "checkpointed" for item in manifest_stages)
    ):
        errors.append(f"{prefix}:spatial_run_manifest_contract_mismatch")

    checkpoints: dict[str, dict[str, Any]] = {}
    manifest_stage_by_id = {item.get("stage_id"): item for item in manifest_stages if isinstance(item, dict)}
    for stage_id in expected_stages:
        stage_root = formal_root / "04_intermediate" / stage_id
        checkpoint_path = stage_root / "checkpoint.json"
        if not checkpoint_path.is_file():
            errors.append(f"{prefix}:spatial_checkpoint_missing:{stage_id}")
            continue
        checkpoint = _load_json(checkpoint_path)
        checkpoints[stage_id] = checkpoint
        observed = _tree_contract(stage_root, exclude={"checkpoint.json"})
        manifest_stage = manifest_stage_by_id.get(stage_id, {})
        if (
            checkpoint.get("stage_id") != stage_id
            or checkpoint.get("status") != "checkpointed"
            or checkpoint.get("payload_tree_sha256") != observed["tree_sha256"]
            or checkpoint.get("payload_file_count") != observed["file_count"]
            or manifest_stage.get("payload_tree_sha256") != observed["tree_sha256"]
        ):
            errors.append(f"{prefix}:spatial_checkpoint_payload_contract_mismatch:{stage_id}")
        else:
            counts["verified_checkpoints"] += 1

    artifact_index = _load_json(delivery_paths["artifact_index"]) if "artifact_index" in delivery_paths else {}
    indexed_artifacts = artifact_index.get("artifacts", [])
    indexed_by_path = {
        item.get("relative_path"): item for item in indexed_artifacts if isinstance(item, dict)
    }
    indexed_by_id = {
        item.get("artifact_id"): item for item in indexed_artifacts if isinstance(item, dict)
    }
    actual_paths = {
        item.relative_to(formal_root).as_posix()
        for item in formal_root.rglob("*")
        if item.is_file()
    } - {
        "07_reports/ARTIFACT_INDEX.json",
        "07_reports/ARTIFACT_INDEX.md",
        "manifest/artifact_ledger.jsonl",
        "manifest/run_manifest.json",
    }
    if (
        artifact_index.get("artifact_count") != formal.get("artifact_count")
        or len(indexed_artifacts) != formal.get("artifact_count")
        or len(indexed_by_path) != len(indexed_artifacts)
        or len(indexed_by_id) != len(indexed_artifacts)
        or set(indexed_by_path) != actual_paths
    ):
        errors.append(f"{prefix}:spatial_artifact_index_exact_set_mismatch")
    for relative, artifact in indexed_by_path.items():
        try:
            path = _inside(formal_root, str(relative))
        except ExecutionValidationError as exc:
            errors.append(f"{prefix}:spatial_artifact_index:{exc}")
            continue
        if (
            not path.is_file()
            or path.stat().st_size != artifact.get("size_bytes")
            or _sha256(path) != artifact.get("sha256")
        ):
            errors.append(f"{prefix}:spatial_artifact_integrity_mismatch:{relative}")
        else:
            counts["verified_formal_artifacts"] += 1
    if run_manifest.get("artifact_index_sha256") != execution.get("delivery", {}).get("artifact_index", {}).get("sha256"):
        errors.append(f"{prefix}:spatial_manifest_artifact_index_hash_mismatch")

    ledger_entries: list[dict[str, Any]] = []
    ledger_path = delivery_paths.get("artifact_ledger")
    if ledger_path:
        try:
            ledger_entries = [
                json.loads(line)
                for line in ledger_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        except json.JSONDecodeError as exc:
            errors.append(f"{prefix}:spatial_artifact_ledger_json_invalid:{exc}")
    if len(ledger_entries) != formal.get("ledger_entry_count") or ledger_entries != indexed_artifacts:
        errors.append(f"{prefix}:spatial_ledger_artifact_index_mismatch")

    summary = formal.get("analysis_summary", {})
    st_profile_path = formal_root / "04_intermediate" / "S10_INGEST" / "st-matrix-profile.json"
    transform_path = formal_root / "04_intermediate" / "S20_COORD_IMAGE_QC" / "transform-audit.json"
    preprocess_path = formal_root / "04_intermediate" / "S40_PREPROCESS" / "preprocess-profile.json"
    discovery_path = formal_root / "04_intermediate" / "S60_CORE_DISCOVERY" / "core-discovery-summary.json"
    boundary_path = formal_root / "05_results" / "tables" / "estimand-and-claim-boundary.json"
    model_path = formal_root / "05_results" / "tables" / "spotiphy-model-status.json"
    st_profile = _load_json(st_profile_path) if st_profile_path.is_file() else {}
    transform = _load_json(transform_path) if transform_path.is_file() else {}
    preprocess = _load_json(preprocess_path) if preprocess_path.is_file() else {}
    discovery = _load_json(discovery_path) if discovery_path.is_file() else {}
    boundary = _load_json(boundary_path) if boundary_path.is_file() else {}
    model_status = _load_json(model_path) if model_path.is_file() else {}
    if (
        st_profile.get("shape") != [summary.get("spot_count"), summary.get("gene_count")]
        or st_profile.get("sparse") is not True
        or st_profile.get("negative_nonzero_count") != 0
        or st_profile.get("noninteger_nonzero_count") != 0
        or transform.get("identity_bilinear_correlation") != summary.get("coordinate_identity_correlation")
        or transform.get("identity_transform_quantitatively_supported") is not True
        or preprocess.get("selected_feature_count") != summary.get("selected_features")
        or preprocess.get("raw_counts_mutated") is not False
        or discovery.get("selected_k") != summary.get("expression_clusters")
        or discovery.get("lattice_knn_moran_spearman") != summary.get("graph_sensitivity_moran_spearman")
        or discovery.get("permutation_p_values") is not False
        or discovery.get("multiple_testing_fdr") is not False
        or boundary.get("qc_flag_sensitivity_cluster_ari") != summary.get("qc_flag_sensitivity_cluster_ari")
        or boundary.get("spatial_unit") != summary.get("spatial_unit")
        or boundary.get("sampling_unit") != summary.get("sampling_unit")
        or boundary.get("inference_unit") != summary.get("inference_unit")
        or boundary.get("independent_inference_units_available") != summary.get("independent_inference_units")
        or boundary.get("inferential_tests_performed") != summary.get("inferential_tests_performed")
        or boundary.get("population_inference_allowed") != summary.get("population_inference_allowed")
    ):
        errors.append(f"{prefix}:spatial_analysis_summary_mismatch")
    model_contract = formal.get("model_branch", {})
    if any(
        model_status.get(field) != model_contract.get(field)
        for field in [
            "requested_method", "status", "attempted", "deconvolution_completed",
            "substitution_performed", "scientifically_non_equivalent_fallback_used",
            "source_commit", "required_future_environment",
        ]
    ) or model_status.get("environment_probe", {}).get("install_attempted_in_this_environment") != model_contract.get("installation_attempted_in_this_environment"):
        errors.append(f"{prefix}:spatial_model_branch_contract_mismatch")

    native = execution.get("native_review", {})
    native_review = _load_json(native_paths["review_source"]) if "review_source" in native_paths else {}
    native_checkpoint = _load_json(native_paths["checkpoint"]) if "checkpoint" in native_paths else {}
    figures = native_review.get("figures", [])
    if (
        native_review.get("review_state") != native.get("state")
        or native_review.get("decision") != native.get("decision")
        or native_review.get("reviewed_native_pixels") != native.get("reviewed_native_pixels")
        or native_review.get("blocking_findings") != native.get("blocking_findings")
        or native_review.get("major_findings") != native.get("major_findings")
        or len(figures) != native.get("figure_count")
        or any(item.get("opened_original") is not True for item in figures)
        or any(item.get("opened_final") is not True for item in figures)
        or native_checkpoint.get("validation", {}).get("decision") != native.get("decision")
        or native_checkpoint.get("validation", {}).get("figure_count") != native.get("figure_count")
    ):
        errors.append(f"{prefix}:spatial_native_review_contract_mismatch")
    for figure in figures:
        figure_id = figure.get("figure_id")
        original = formal_root / "06_figures" / "original" / f"{figure_id}.png"
        final = formal_root / "06_figures" / "final" / f"{figure_id}.png"
        original_relative = original.relative_to(formal_root).as_posix()
        final_relative = final.relative_to(formal_root).as_posix()
        indexed_original = indexed_by_path.get(original_relative, {})
        indexed_final = indexed_by_path.get(final_relative, {})
        if (
            not original.is_file()
            or not final.is_file()
            or _png_dimensions(original) is None
            or _png_dimensions(final) is None
            or _sha256(original) != figure.get("original_sha256")
            or _sha256(final) != figure.get("final_sha256")
            or indexed_original.get("sha256") != figure.get("original_sha256")
            or indexed_final.get("sha256") != figure.get("final_sha256")
            or not figure.get("visible")
            or not figure.get("interpretable")
            or not figure.get("cannot_assert")
        ):
            errors.append(f"{prefix}:spatial_native_figure_binding_mismatch:{figure_id}")
        else:
            counts["verified_native_figures"] += 1

    evidence_events = set()
    for index in range(2):
        path = delivery_paths.get(f"failure_evidence_{index}")
        if path:
            evidence_events.add(_load_json(path).get("event"))
    if evidence_events != {"cross-task-runtime-bytecode-contamination", "native-visual-review-rejection"}:
        errors.append(f"{prefix}:spatial_failure_evidence_contract_mismatch")
    figure_notes_path = delivery_paths.get("figure_notes")
    if figure_notes_path:
        figure_notes = figure_notes_path.read_text(encoding="utf-8")
        if (
            "PASS_WITH_MINOR_FINDINGS" not in figure_notes
            or "no population" not in figure_notes.lower()
        ):
            errors.append(f"{prefix}:spatial_figure_notes_boundary_mismatch")
    if (
        "one" not in execution.get("maturity_scope", "").lower()
        and "single" not in execution.get("maturity_scope", "").lower()
    ) or not execution.get("known_limitations"):
        errors.append(f"{prefix}:spatial_data_verified_scope_not_bounded")
    return counts


def _read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def _validate_formal_omics_execution(
    execution: dict[str, Any],
    candidate: dict[str, Any],
    locator_by_ref: dict[str, Any],
    prefix: str,
    errors: list[str],
    *,
    verify_live: bool,
) -> dict[str, int]:
    """Validate the shared formal contract used by bulk, proteomics, and multi-omics."""
    counts = {
        "verified_files": 0,
        "verified_run_artifacts": 0,
        "verified_native_figures": 0,
        "verified_formal_files": 0,
        "verified_formal_artifacts": 0,
        "verified_checkpoints": 0,
    }
    inputs = execution.get("inputs", {}).get("artifacts", [])
    refs = [item.get("locator_ref") for item in inputs if isinstance(item, dict)]
    if len(refs) != len(set(refs)):
        errors.append(f"{prefix}:duplicate_input_locator")
    public_evidence = {
        item.get("locator_ref"): item
        for item in candidate.get("availability", {}).get("integrity_evidence", [])
        if isinstance(item, dict)
    }
    input_pairs: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
    for item in inputs:
        if not isinstance(item, dict):
            errors.append(f"{prefix}:input_artifact_not_object")
            continue
        locator_ref = item.get("locator_ref")
        locator = locator_by_ref.get(locator_ref)
        if locator is None:
            errors.append(f"{prefix}:missing_private_input_locator:{locator_ref}")
        elif (
            locator.get("expected_sha256") != item.get("sha256")
            or locator.get("expected_size_bytes") != item.get("size_bytes")
        ):
            errors.append(f"{prefix}:input_locator_evidence_mismatch:{locator_ref}")
        public = public_evidence.get(locator_ref)
        if public is not None and (
            public.get("sha256") != item.get("sha256")
            or public.get("size_bytes") != item.get("size_bytes")
        ):
            errors.append(f"{prefix}:input_public_evidence_mismatch:{locator_ref}")
        input_pairs.append((item, locator))
    if not verify_live:
        return counts

    root = Path(execution.get("task_root", ""))
    if not root.is_dir():
        errors.append(f"{prefix}:task_root_missing")
        return counts
    try:
        formal_root = _inside(root, execution.get("execution", {}).get("run_root", ""))
    except ExecutionValidationError as exc:
        errors.append(f"{prefix}:formal_run:{exc}")
        return counts
    if not formal_root.is_dir() or formal_root.name != execution.get("execution", {}).get("run_id"):
        errors.append(f"{prefix}:formal_run_root_invalid")
        return counts

    for evidence, locator in input_pairs:
        if locator is None:
            continue
        source = Path(locator.get("path", ""))
        if not source.is_file():
            errors.append(f"{prefix}:input_file_missing:{evidence.get('locator_ref')}")
        elif source.stat().st_size != evidence.get("size_bytes"):
            errors.append(f"{prefix}:input_size_mismatch:{evidence.get('locator_ref')}")
        elif _sha256(source) != evidence.get("sha256"):
            errors.append(f"{prefix}:input_hash_mismatch:{evidence.get('locator_ref')}")
        else:
            counts["verified_files"] += 1

    environment = execution.get("environment", {})
    environment_manifest_path = _verify_file(
        formal_root, environment.get("manifest", {}), f"{prefix}:environment_manifest", errors
    )
    primary_lock_path = _verify_file(
        formal_root, environment.get("primary_lock", {}), f"{prefix}:environment_primary_lock", errors
    )
    restore_evidence_path = _verify_external_file(
        environment.get("restore_evidence", {}), f"{prefix}:environment_restore_evidence", errors
    )
    counts["verified_files"] += sum(
        path is not None for path in (environment_manifest_path, primary_lock_path, restore_evidence_path)
    )
    environment_manifest = _load_json(environment_manifest_path) if environment_manifest_path else {}
    manifest_environment = environment_manifest.get("environment", environment_manifest)
    for field in ("env_id", "lock_hash", "backend"):
        if manifest_environment.get(field) != environment.get(field):
            errors.append(f"{prefix}:environment_{field}_mismatch")
    observed_global_changes = environment_manifest.get(
        "global_changes", manifest_environment.get("global_changes", False)
    )
    if observed_global_changes is not False or environment.get("global_changes") is not False:
        errors.append(f"{prefix}:environment_global_changes_not_false")
    if environment.get("exact_restore_verified") is not True or len(environment.get("verification_hashes", [])) < 2:
        errors.append(f"{prefix}:environment_restore_evidence_incomplete")
    if restore_evidence_path:
        restore_evidence = _load_json(restore_evidence_path)
        restore_ok = any(
            value is True
            for value in (
                restore_evidence.get("cache_hit"),
                restore_evidence.get("cache_restore_exact_hash"),
                restore_evidence.get("verification_outputs_equal"),
                restore_evidence.get("environment", {}).get("cache_restored_from_exact_lock"),
            )
        )
        if not restore_ok:
            errors.append(f"{prefix}:environment_restore_evidence_not_true")

    code_paths: dict[str, Path] = {}
    for name, evidence in execution.get("code", {}).items():
        path = _verify_file(formal_root, evidence, f"{prefix}:code:{name}", errors)
        if path is not None:
            counts["verified_files"] += 1
            code_paths[name] = path

    delivery = execution.get("delivery", {})
    delivery_paths: dict[str, Path] = {}
    for name in ("run_manifest", "artifact_ledger", "qa_report", "figure_notes", "analysis_design"):
        path = _verify_file(formal_root, delivery.get(name, {}), f"{prefix}:delivery:{name}", errors)
        if path is not None:
            counts["verified_formal_files"] += 1
            delivery_paths[name] = path
    review_path = _verify_file(
        formal_root,
        execution.get("native_review", {}).get("review_source", {}),
        f"{prefix}:native_review",
        errors,
    )
    if review_path is not None:
        counts["verified_formal_files"] += 1

    run_manifest = _load_json(delivery_paths["run_manifest"]) if "run_manifest" in delivery_paths else {}
    observed_state = run_manifest.get("state", run_manifest.get("current_state"))
    runtime = execution.get("execution", {})
    if (
        run_manifest.get("run_id") != runtime.get("run_id")
        or observed_state != "DELIVERED"
        or run_manifest.get("mode") != "run"
    ):
        errors.append(f"{prefix}:formal_run_manifest_state_invalid")
    if "exit_code" in run_manifest and run_manifest.get("exit_code") != 0:
        errors.append(f"{prefix}:formal_run_nonzero_exit")
    if "terminal" in run_manifest and run_manifest.get("terminal") is not True:
        errors.append(f"{prefix}:formal_run_not_terminal")

    intermediate_root = formal_root / "04_intermediate"
    stage_ids = runtime.get("stage_ids", [])
    observed_stage_ids = sorted(
        path.name for path in intermediate_root.iterdir() if path.is_dir()
    ) if intermediate_root.is_dir() else []
    if sorted(stage_ids) != observed_stage_ids or runtime.get("checkpoint_count") != len(observed_stage_ids):
        errors.append(f"{prefix}:checkpoint_set_mismatch")
    else:
        counts["verified_checkpoints"] += len(observed_stage_ids)

    ledger_path = delivery_paths.get("artifact_ledger")
    ledger_entries: list[dict[str, Any]] = []
    if ledger_path:
        try:
            ledger_entries = [
                json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        except json.JSONDecodeError as exc:
            errors.append(f"{prefix}:ledger_json_invalid:{exc}")
    if len(ledger_entries) != runtime.get("ledger_entry_count"):
        errors.append(f"{prefix}:ledger_entry_count_mismatch")
    for entry in ledger_entries:
        artifact = entry.get("artifact", entry)
        relative = artifact.get("relative_path", artifact.get("path", ""))
        digest = artifact.get("sha256")
        try:
            artifact_path = _inside(formal_root, str(relative))
        except ExecutionValidationError as exc:
            errors.append(f"{prefix}:ledger:{exc}")
            continue
        if not artifact_path.is_file() or _sha256(artifact_path) != digest:
            errors.append(f"{prefix}:ledger_artifact_mismatch:{relative}")
        else:
            counts["verified_formal_artifacts"] += 1
    observed_artifacts = len(run_manifest.get("artifacts", [])) or len(ledger_entries)
    if observed_artifacts != runtime.get("manifest_artifact_count"):
        errors.append(f"{prefix}:manifest_artifact_count_mismatch")
    if runtime.get("recoverable_checkpoints_verified") is not True:
        errors.append(f"{prefix}:recoverable_checkpoint_evidence_missing")

    native = execution.get("native_review", {})
    review = _load_json(review_path) if review_path else {}
    actual_decision = review.get("review_decision", review.get("decision"))
    if (
        actual_decision != native.get("decision")
        or review.get("reviewed_native_pixels") is not True
        or native.get("all_final_figures_opened") is not True
        or native.get("figure_count") != len(native.get("figures", []))
    ):
        errors.append(f"{prefix}:native_review_contract_mismatch")
    reviewed_by_path = {}
    reviewed_by_hash = {}
    for item in review.get("figures", []):
        relative = item.get("relative_path", item.get("file", ""))
        reviewed_by_path[str(relative)] = item
        if item.get("sha256"):
            reviewed_by_hash[str(item.get("sha256"))] = item
    for figure in native.get("figures", []):
        try:
            path = _inside(formal_root, figure.get("relative_path", ""))
        except ExecutionValidationError as exc:
            errors.append(f"{prefix}:native_figure:{exc}")
            continue
        dimensions = _png_dimensions(path) if path.is_file() else None
        if (
            not path.is_file()
            or path.stat().st_size != figure.get("size_bytes")
            or _sha256(path) != figure.get("sha256")
            or dimensions != (figure.get("width_px"), figure.get("height_px"))
        ):
            errors.append(f"{prefix}:native_figure_evidence_mismatch:{figure.get('relative_path')}")
            continue
        reviewed = reviewed_by_path.get(figure.get("relative_path")) or reviewed_by_hash.get(
            figure.get("sha256")
        )
        if reviewed is None or reviewed.get("sha256") != figure.get("sha256"):
            errors.append(f"{prefix}:native_review_figure_binding_mismatch:{figure.get('relative_path')}")
        else:
            counts["verified_native_figures"] += 1

    package_path = _verify_external_file(
        delivery.get("package_manifest", {}), f"{prefix}:package_manifest", errors
    )
    if package_path is not None:
        counts["verified_formal_files"] += 1
        package_root = Path(delivery.get("output_root", "")).resolve()
        if package_path.parent.resolve() != package_root or not package_root.is_dir():
            errors.append(f"{prefix}:package_root_mismatch")
        package_manifest = _load_json(package_path)
        declared = package_manifest.get("files", [])
        actual_files = {path.relative_to(package_root).as_posix() for path in package_root.rglob("*") if path.is_file()}
        expected_files = {str(item.get("relative_path")) for item in declared} | {package_path.name}
        if actual_files != expected_files or len(actual_files) != delivery.get("package_file_count"):
            errors.append(f"{prefix}:package_exact_set_mismatch")
        for item in declared:
            try:
                path = _inside(package_root, str(item.get("relative_path", "")))
            except ExecutionValidationError as exc:
                errors.append(f"{prefix}:package:{exc}")
                continue
            if (
                not path.is_file()
                or path.stat().st_size != item.get("bytes", item.get("size_bytes"))
                or _sha256(path) != item.get("sha256")
            ):
                errors.append(f"{prefix}:package_artifact_mismatch:{item.get('relative_path')}")
            else:
                counts["verified_run_artifacts"] += 1
    if delivery.get("package_verification_ok") is not True:
        errors.append(f"{prefix}:package_verification_not_ok")

    summary = runtime.get("analysis_summary", {})
    domain = execution.get("domain")
    if domain == "bulk-rna":
        rows = _read_tsv(formal_root / "05_results" / "results_summary.tsv")
        expected = {row["metric"]: int(row["value"]) for row in rows}
    elif domain == "quantitative-proteomics":
        primary = _read_tsv(formal_root / "05_results" / "tables" / "protein_results_primary.tsv")
        comparison = _read_tsv(formal_root / "05_results" / "tables" / "primary_vs_minprob_comparison.tsv")
        calls = [row for row in primary if row.get("significant_fdr05_abs_lfc1") == "TRUE"]
        expected = {
            "source_protein_groups": 3006,
            "primary_tested_protein_groups": len(primary),
            "primary_calls_fdr05_abs_lfc1": len(calls),
            "primary_calls_positive": sum(float(row["log2_fc"]) > 0 for row in calls),
            "primary_calls_negative": sum(float(row["log2_fc"]) < 0 for row in calls),
            "direction_reversals": sum(row.get("direction_reversal") == "TRUE" for row in comparison),
            "fdr_class_changes": sum(row.get("fdr_class_change") == "TRUE" for row in comparison),
        }
    else:
        expected = _load_json(formal_root / "04_intermediate" / "04-multi-omics" / "stability_summary.json")
    for key, value in expected.items():
        if summary.get(key) != value:
            errors.append(f"{prefix}:analysis_summary_mismatch:{key}")

    for report_name in ("qa_report", "figure_notes"):
        path = delivery_paths.get(report_name)
        if path and "pending" in path.read_text(encoding="utf-8").lower():
            errors.append(f"{prefix}:{report_name}_contains_pending")
    return counts


def validate_execution_registry(
    registry_path: Path = DEFAULT_REGISTRY,
    schema_path: Path = DEFAULT_SCHEMA,
    candidate_path: Path = DEFAULT_CANDIDATES,
    locator_path: Path = DEFAULT_LOCATORS,
    *,
    verify_live: bool = False,
) -> dict[str, Any]:
    registry = _load_json(registry_path)
    schema = _load_json(schema_path)
    candidates = _load_json(candidate_path)
    locators = _load_json(locator_path)
    errors = _schema_errors(registry, schema)

    candidate_by_id = {case["case_id"]: case for case in candidates.get("cases", [])}
    locator_by_ref = locators.get("locators", {})
    executions = registry.get("executions", []) if isinstance(registry, dict) else []
    execution_ids = [item.get("execution_id") for item in executions if isinstance(item, dict)]
    executed_case_ids = [item.get("case_id") for item in executions if isinstance(item, dict)]
    if len(execution_ids) != len(set(execution_ids)):
        errors.append("duplicate_execution_id")
    if len(executed_case_ids) != len(set(executed_case_ids)):
        errors.append("duplicate_case_execution")

    # Execution evidence is an overlay. The public design registry must remain candidate-only.
    for case_id, case in candidate_by_id.items():
        if case.get("execution_status") != "not-executed":
            errors.append(f"{case_id}:public_execution_status_promoted")
        if case.get("maturity") not in {"raw-extracted", "normalized"}:
            errors.append(f"{case_id}:public_maturity_promoted")
        if case.get("environment", {}).get("install_authorized") is not False:
            errors.append(f"{case_id}:public_install_authorization_changed")

    verified_files = 0
    verified_run_artifacts = 0
    verified_native_figures = 0
    verified_formal_files = 0
    verified_formal_artifacts = 0
    verified_checkpoints = 0
    execution_counts_by_domain: dict[str, int] = {}
    for execution in executions:
        if not isinstance(execution, dict):
            continue
        prefix = execution.get("execution_id", "unknown-execution")
        case_id = execution.get("case_id")
        candidate = candidate_by_id.get(case_id)
        if candidate is None:
            errors.append(f"{prefix}:unknown_public_case:{case_id}")
            continue
        if execution.get("domain") != candidate.get("domain"):
            errors.append(f"{prefix}:domain_mismatch")

        domain = execution.get("domain")
        execution_counts_by_domain[str(domain)] = execution_counts_by_domain.get(str(domain), 0) + 1
        if domain == "single-cell":
            single_cell_counts = _validate_single_cell_execution(
                execution,
                candidate,
                locator_by_ref,
                prefix,
                errors,
                verify_live=verify_live,
            )
            verified_files += single_cell_counts["verified_files"]
            verified_native_figures += single_cell_counts["verified_native_figures"]
            verified_formal_files += single_cell_counts["verified_formal_files"]
            verified_formal_artifacts += single_cell_counts["verified_formal_artifacts"]
            verified_checkpoints += single_cell_counts["verified_checkpoints"]
            continue
        if domain == "spatial-transcriptomics":
            spatial_counts = _validate_spatial_execution(
                execution,
                candidate,
                locator_by_ref,
                prefix,
                errors,
                verify_live=verify_live,
            )
            verified_files += spatial_counts["verified_files"]
            verified_native_figures += spatial_counts["verified_native_figures"]
            verified_formal_files += spatial_counts["verified_formal_files"]
            verified_formal_artifacts += spatial_counts["verified_formal_artifacts"]
            verified_checkpoints += spatial_counts["verified_checkpoints"]
            continue
        if domain in {"bulk-rna", "quantitative-proteomics", "multi-omics"}:
            formal_counts = _validate_formal_omics_execution(
                execution,
                candidate,
                locator_by_ref,
                prefix,
                errors,
                verify_live=verify_live,
            )
            verified_files += formal_counts["verified_files"]
            verified_run_artifacts += formal_counts["verified_run_artifacts"]
            verified_native_figures += formal_counts["verified_native_figures"]
            verified_formal_files += formal_counts["verified_formal_files"]
            verified_formal_artifacts += formal_counts["verified_formal_artifacts"]
            verified_checkpoints += formal_counts["verified_checkpoints"]
            continue
        if domain != "visualization":
            errors.append(f"{prefix}:unsupported_execution_domain:{domain}")
            continue
        visualization_fields = {
            "teaching_report", "authorization", "input", "script", "environment",
            "reproducibility", "native_review", "formal_run",
        }
        if not visualization_fields.issubset(execution):
            errors.append(f"{prefix}:visualization_contract_fields_missing")
            continue

        input_evidence = execution.get("input", {})
        locator = locator_by_ref.get(input_evidence.get("locator_ref"))
        public_integrity = {
            item.get("locator_ref"): item
            for item in candidate.get("availability", {}).get("integrity_evidence", [])
        }.get(input_evidence.get("locator_ref"))
        if locator is None:
            errors.append(f"{prefix}:missing_private_input_locator")
        else:
            if locator.get("expected_sha256") != input_evidence.get("sha256") or locator.get("expected_size_bytes") != input_evidence.get("size_bytes"):
                errors.append(f"{prefix}:input_locator_evidence_mismatch")
        if public_integrity is None or public_integrity.get("sha256") != input_evidence.get("sha256") or public_integrity.get("size_bytes") != input_evidence.get("size_bytes"):
            errors.append(f"{prefix}:input_public_evidence_mismatch")

        if not verify_live:
            continue
        root = Path(execution.get("task_root", ""))
        if not root.is_dir():
            errors.append(f"{prefix}:task_root_missing")
            continue

        report_path = _verify_file(root, execution["teaching_report"], f"{prefix}:teaching_report", errors)
        script_path = _verify_file(root, execution["script"], f"{prefix}:script", errors)
        environment = execution["environment"]
        manifest_path = _verify_file(root, environment["manifest"], f"{prefix}:environment_manifest", errors)
        marker_path = _verify_file(root, environment["marker"], f"{prefix}:environment_marker", errors)
        lock_path = _verify_file(root, environment["requirements_lock"], f"{prefix}:requirements_lock", errors)
        verified_files += sum(path is not None for path in [report_path, script_path, manifest_path, marker_path, lock_path])

        if locator is not None:
            source = Path(locator.get("path", ""))
            if not source.is_file():
                errors.append(f"{prefix}:input_file_missing")
            else:
                if source.stat().st_size != input_evidence.get("size_bytes"):
                    errors.append(f"{prefix}:input_size_mismatch")
                if _sha256(source) != input_evidence.get("sha256"):
                    errors.append(f"{prefix}:input_hash_mismatch")
                else:
                    verified_files += 1

        report = _load_json(report_path) if report_path and report_path.is_file() else {}
        if report.get("ok") is not True:
            errors.append(f"{prefix}:teaching_report_not_ok")
        if report.get("task_root") != str(root):
            errors.append(f"{prefix}:report_task_root_mismatch")
        if report.get("authorization", report.get("environment_manifest", {}).get("authorization")) != execution.get("authorization"):
            errors.append(f"{prefix}:authorization_mismatch")
        report_environment = report.get("environment_manifest", {})
        if manifest_path and _load_json(manifest_path) != report_environment:
            errors.append(f"{prefix}:environment_manifest_report_mismatch")
        report_env = report_environment.get("environment", {})
        for field in ["backend", "env_id", "lock_hash"]:
            if report_env.get(field) != environment.get(field):
                errors.append(f"{prefix}:environment_{field}_mismatch")
        if report_environment.get("state") != environment.get("state"):
            errors.append(f"{prefix}:environment_state_mismatch")
        if report_env.get("marker", {}).get("platform") != environment.get("platform"):
            errors.append(f"{prefix}:environment_platform_mismatch")
        if marker_path and _load_json(marker_path).get("lock_hash") != environment.get("lock_hash"):
            errors.append(f"{prefix}:marker_lock_hash_mismatch")

        reproducibility = execution["reproducibility"]
        if report.get("fresh_returncode") != reproducibility.get("fresh_returncode"):
            errors.append(f"{prefix}:fresh_returncode_mismatch")
        if report.get("restored_returncode") != reproducibility.get("cached_returncode"):
            errors.append(f"{prefix}:cached_returncode_mismatch")
        if report.get("restored_from_cache") != reproducibility.get("restored_from_cache"):
            errors.append(f"{prefix}:cache_restore_mismatch")
        registered_hashes = {item["relative_path"]: item["sha256"] for item in reproducibility["artifact_hashes"]}
        if len(registered_hashes) != 9:
            errors.append(f"{prefix}:reproducibility_requires_nine_unique_artifacts")
        if report.get("deterministic_artifacts") != registered_hashes:
            errors.append(f"{prefix}:report_artifact_hashes_mismatch")
        for relative, expected_hash in registered_hashes.items():
            fresh = _inside(root, f"{reproducibility['fresh_run_dir']}/{relative}")
            cached = _inside(root, f"{reproducibility['cached_run_dir']}/{relative}")
            if not fresh.is_file() or not cached.is_file():
                errors.append(f"{prefix}:reproducibility_artifact_missing:{relative}")
                continue
            fresh_hash, cached_hash = _sha256(fresh), _sha256(cached)
            if fresh_hash != expected_hash or cached_hash != expected_hash or fresh_hash != cached_hash:
                errors.append(f"{prefix}:reproducibility_hash_mismatch:{relative}")
            else:
                verified_run_artifacts += 1

        native = execution["native_review"]
        review_path = _verify_file(root, native["review_file"], f"{prefix}:native_review", errors)
        if review_path:
            verified_files += 1
            review = _load_json(review_path)
            for field, registered_field in [("review_state", "review_state"), ("review_decision", "decision"), ("reviewed_native_pixels", "reviewed_native_pixels")]:
                if review.get(field) != native.get(registered_field):
                    errors.append(f"{prefix}:native_{field}_mismatch")
            if review.get("maturity") != execution.get("maturity"):
                errors.append(f"{prefix}:native_maturity_mismatch")
            rerun = review.get("deterministic_rerun", {})
            if rerun.get("compared_artifact_count") != 9 or rerun.get("all_hashes_equal") is not True:
                errors.append(f"{prefix}:native_review_determinism_evidence_invalid")
            review_figures = {item["relative_path"]: item for item in review.get("figures", [])}
        else:
            review_figures = {}

        for figure in native["figures"]:
            primary = _inside(root, figure["relative_path"])
            cached = _inside(root, figure["cached_copy_relative_path"])
            if not primary.is_file() or not cached.is_file():
                errors.append(f"{prefix}:native_figure_missing:{figure['relative_path']}")
                continue
            primary_hash, cached_hash = _sha256(primary), _sha256(cached)
            if primary.stat().st_size != figure["size_bytes"] or primary_hash != figure["sha256"] or cached_hash != figure["sha256"]:
                errors.append(f"{prefix}:native_figure_evidence_mismatch:{figure['relative_path']}")
                continue
            review_key = "/".join(Path(figure["relative_path"]).parts[1:])
            reviewed = review_figures.get(review_key)
            if reviewed is None or reviewed.get("sha256") != figure["sha256"] or reviewed.get("scientific_boundary") != figure["scientific_boundary"]:
                errors.append(f"{prefix}:native_review_figure_mismatch:{figure['relative_path']}")
            else:
                verified_native_figures += 1
        if report.get("scientific_boundary") != execution.get("scientific_claim_ceiling"):
            errors.append(f"{prefix}:scientific_claim_ceiling_mismatch")

        formal = execution["formal_run"]
        formal_root = Path(formal.get("run_root", ""))
        if not formal_root.is_dir():
            errors.append(f"{prefix}:formal_run_root_missing")
            continue
        promotion_path = _verify_external_file(formal["promotion_report"], f"{prefix}:promotion_report", errors)
        critical_evidence = [
            (formal["run_manifest"], "formal_run_manifest"),
            (formal["artifact_ledger"], "formal_artifact_ledger"),
            (formal["workflow_plan"], "formal_workflow_plan"),
            (formal["environment"]["manifest"], "formal_environment_manifest"),
            (formal["environment"]["marker"], "formal_environment_marker"),
            (formal["environment"]["requirements_lock"], "formal_environment_lock"),
            (formal["reports"]["qa_report"], "formal_qa_report"),
            (formal["reports"]["figure_notes"], "formal_figure_notes"),
        ] + [(item, "formal_final_figure") for item in formal["final_figures"]]
        formal_paths: dict[str, Path] = {}
        for evidence, label in critical_evidence:
            path = _verify_file(formal_root, evidence, f"{prefix}:{label}", errors)
            if path is not None:
                formal_paths[evidence["relative_path"]] = path
                verified_formal_files += 1
        if promotion_path is not None:
            verified_formal_files += 1
            promotion = _load_json(promotion_path)
        else:
            promotion = {}
        if promotion.get("ok") is not True or promotion.get("state") != formal.get("state"):
            errors.append(f"{prefix}:formal_promotion_state_invalid")
        if promotion.get("run_root") != str(formal_root):
            errors.append(f"{prefix}:formal_promotion_root_mismatch")
        if promotion.get("registered_artifacts") != formal.get("registered_artifact_count"):
            errors.append(f"{prefix}:formal_registered_artifact_count_mismatch")
        if promotion.get("artifact_ledger_events") != formal.get("ledger_entry_count"):
            errors.append(f"{prefix}:formal_ledger_event_count_mismatch")
        if promotion.get("maturity") != execution.get("maturity"):
            errors.append(f"{prefix}:formal_maturity_mismatch")
        if promotion.get("input_sha256") != execution.get("input", {}).get("sha256"):
            errors.append(f"{prefix}:formal_input_hash_mismatch")
        if promotion.get("environment_lock_hash") != formal.get("environment", {}).get("lock_hash"):
            errors.append(f"{prefix}:formal_environment_lock_hash_mismatch")
        if promotion.get("native_visual_review") != execution.get("native_review", {}).get("decision"):
            errors.append(f"{prefix}:formal_native_review_mismatch")
        promotion_validation = promotion.get("validation", {})
        if {key: promotion_validation.get(key) for key in ["ok", "verify_hashes", "errors"]} != formal.get("validation"):
            errors.append(f"{prefix}:formal_validation_evidence_mismatch")
        promotion_resume = promotion.get("resume_audit", {})
        resume_fields = [
            "ok", "read_only", "prior_state", "environment_locked", "verify_input_hashes",
            "latest_valid_checkpoint", "resume_requires_new_workflow_instance", "errors",
        ]
        if {key: promotion_resume.get(key) for key in resume_fields} != formal.get("resume_audit"):
            errors.append(f"{prefix}:formal_resume_evidence_mismatch")
        if promotion_resume.get("run_root") != str(formal_root):
            errors.append(f"{prefix}:formal_resume_root_mismatch")

        manifest_path = formal_paths.get(formal["run_manifest"]["relative_path"])
        ledger_path = formal_paths.get(formal["artifact_ledger"]["relative_path"])
        plan_path = formal_paths.get(formal["workflow_plan"]["relative_path"])
        formal_manifest = _load_json(manifest_path) if manifest_path else {}
        if formal_manifest.get("state") != "DELIVERED" or formal_manifest.get("run_id") != formal.get("run_id"):
            errors.append(f"{prefix}:formal_run_manifest_state_invalid")
        if formal_manifest.get("exit_code") != 0 or formal_manifest.get("mode") != "run":
            errors.append(f"{prefix}:formal_run_manifest_execution_invalid")
        formal_stages = formal_manifest.get("stages", [])
        if not formal_stages or any(stage.get("status") != "checkpointed" for stage in formal_stages):
            errors.append(f"{prefix}:formal_stage_completion_split_brain")
        if formal_stages and formal.get("resume_audit", {}).get("latest_valid_checkpoint") != formal_stages[-1].get("node_id"):
            errors.append(f"{prefix}:formal_latest_checkpoint_not_final_stage")
        manifest_artifacts = formal_manifest.get("artifacts", [])
        if len(manifest_artifacts) != formal.get("manifest_artifact_count"):
            errors.append(f"{prefix}:formal_manifest_artifact_count_mismatch")
        manifest_by_id = {item.get("artifact_id"): item for item in manifest_artifacts}
        if len(manifest_by_id) != len(manifest_artifacts):
            errors.append(f"{prefix}:formal_manifest_duplicate_artifact_id")
        for artifact in manifest_artifacts:
            relative = artifact.get("relative_path", "")
            try:
                artifact_path = _inside(formal_root, relative)
            except ExecutionValidationError as exc:
                errors.append(f"{prefix}:formal_manifest:{exc}")
                continue
            if not artifact_path.is_file():
                errors.append(f"{prefix}:formal_manifest_artifact_missing:{relative}")
            elif _sha256(artifact_path) != artifact.get("sha256"):
                errors.append(f"{prefix}:formal_manifest_artifact_hash_mismatch:{relative}")
            else:
                verified_formal_artifacts += 1

        ledger_entries: list[dict[str, Any]] = []
        if ledger_path:
            try:
                ledger_entries = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            except json.JSONDecodeError as exc:
                errors.append(f"{prefix}:formal_artifact_ledger_json_invalid:{exc}")
        if len(ledger_entries) != formal.get("ledger_entry_count"):
            errors.append(f"{prefix}:formal_ledger_entry_count_mismatch")
        duplicate_registrations = 0
        for entry in ledger_entries:
            artifact = entry.get("artifact", {})
            manifested = manifest_by_id.get(artifact.get("artifact_id"))
            if manifested is None:
                same_payload = [
                    item for item in manifest_artifacts
                    if item.get("relative_path") == artifact.get("relative_path")
                    and item.get("sha256") == artifact.get("sha256")
                ]
                if len(same_payload) == 1:
                    duplicate_registrations += 1
                    continue
            if manifested is None or any(
                artifact.get(field) != manifested.get(field)
                for field in ["relative_path", "sha256", "artifact_type", "claim_role"]
            ):
                errors.append(f"{prefix}:formal_ledger_manifest_mismatch:{artifact.get('artifact_id')}")
        if duplicate_registrations != formal.get("duplicate_registration_count"):
            errors.append(f"{prefix}:formal_duplicate_registration_count_mismatch:{duplicate_registrations}")

        formal_plan = _load_json(plan_path) if plan_path else {}
        plan_run = formal_plan.get("run", {})
        if formal_plan.get("mode") != "run" or formal_plan.get("frozen") is not True:
            errors.append(f"{prefix}:formal_workflow_plan_not_frozen_run")
        if plan_run.get("root") != str(formal_root) or plan_run.get("run_id") != formal.get("run_id"):
            errors.append(f"{prefix}:formal_workflow_plan_run_mismatch")
        if any(gate.get("status") != "pass" for gate in formal_plan.get("scientific_gates", [])):
            errors.append(f"{prefix}:formal_workflow_plan_gate_not_passed")

        formal_environment_path = formal_paths.get(formal["environment"]["manifest"]["relative_path"])
        formal_marker_path = formal_paths.get(formal["environment"]["marker"]["relative_path"])
        formal_environment = _load_json(formal_environment_path) if formal_environment_path else {}
        formal_marker = _load_json(formal_marker_path) if formal_marker_path else {}
        if formal_environment.get("state") != "frozen" or formal_environment.get("global_changes") is not False:
            errors.append(f"{prefix}:formal_environment_not_frozen_or_global")
        environments = formal_environment.get("environments", [])
        if len(environments) != 1 or environments[0].get("lock_hash") != formal["environment"]["lock_hash"]:
            errors.append(f"{prefix}:formal_environment_manifest_lock_mismatch")
        if formal_marker.get("lock_hash") != formal["environment"]["lock_hash"]:
            errors.append(f"{prefix}:formal_environment_marker_lock_mismatch")

        native_hashes = {item["sha256"] for item in execution["native_review"]["figures"]}
        formal_figure_hashes = {item["sha256"] for item in formal["final_figures"]}
        if formal_figure_hashes != native_hashes or len(formal_figure_hashes) != 2:
            errors.append(f"{prefix}:formal_final_figures_native_evidence_mismatch")
        manifest_paths = {item.get("relative_path") for item in manifest_artifacts}
        required_registered = {
            formal["reports"]["qa_report"]["relative_path"],
            formal["reports"]["figure_notes"]["relative_path"],
            *(item["relative_path"] for item in formal["final_figures"]),
        }
        if not required_registered.issubset(manifest_paths):
            errors.append(f"{prefix}:formal_required_delivery_not_registered")
        stage_validation_paths = {
            item.get("relative_path")
            for item in manifest_artifacts
            if str(item.get("relative_path", "")).startswith("04_intermediate/")
            and str(item.get("relative_path", "")).endswith("/stage-validation.json")
        }
        if len(stage_validation_paths) != len(formal_stages):
            errors.append(f"{prefix}:formal_stage_checkpoint_evidence_incomplete")

        final_notes_path = formal_paths.get(formal["reports"]["figure_notes"]["relative_path"])
        if final_notes_path:
            final_notes = final_notes_path.read_text(encoding="utf-8")
            if "Native visual review: pending" in final_notes or "PASS_WITH_BOUNDARIES" not in final_notes:
                errors.append(f"{prefix}:formal_final_figure_notes_review_split_brain")

        formal_review_relative = "06_figures/review/native-visual-review.json"
        formal_review_path = formal_root / formal_review_relative
        if formal_review_relative not in manifest_paths or not formal_review_path.is_file():
            errors.append(f"{prefix}:formal_native_review_not_registered")
        else:
            formal_review = _load_json(formal_review_path)
            if formal_review.get("review_decision") != execution.get("native_review", {}).get("decision") or formal_review.get("reviewed_native_pixels") is not True:
                errors.append(f"{prefix}:formal_native_review_decision_invalid")
            for figure in formal_review.get("figures", []):
                relative = str(figure.get("relative_path", ""))
                if not relative.startswith("06_figures/final/"):
                    errors.append(f"{prefix}:formal_native_review_path_invalid:{relative}")
                    continue
                try:
                    figure_path = _inside(formal_root, relative)
                except ExecutionValidationError as exc:
                    errors.append(f"{prefix}:formal_native_review:{exc}")
                    continue
                if not figure_path.is_file() or _sha256(figure_path) != figure.get("sha256"):
                    errors.append(f"{prefix}:formal_native_review_figure_mismatch:{relative}")

    return {
        "ok": not errors,
        "registry": str(registry_path),
        "schema": str(schema_path),
        "execution_count": len(executions),
        "executed_case_ids": sorted(executed_case_ids),
        "public_candidate_count": len(candidate_by_id),
        "unexecuted_public_candidate_count": len(candidate_by_id) - len(set(executed_case_ids)),
        "verify_live": verify_live,
        "verified_files": verified_files,
        "verified_run_artifacts": verified_run_artifacts,
        "verified_native_figures": verified_native_figures,
        "verified_formal_files": verified_formal_files,
        "verified_formal_artifacts": verified_formal_artifacts,
        "verified_checkpoints": verified_checkpoints,
        "execution_counts_by_domain": dict(sorted(execution_counts_by_domain.items())),
        "maturities": sorted({item.get("maturity") for item in executions if isinstance(item, dict)}),
        "errors": errors,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    parser.add_argument("--candidates", type=Path, default=DEFAULT_CANDIDATES)
    parser.add_argument("--local-locators", type=Path, default=DEFAULT_LOCATORS)
    parser.add_argument("--verify-live", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = validate_execution_registry(
            args.registry.resolve(),
            args.schema.resolve(),
            args.candidates.resolve(),
            args.local_locators.resolve(),
            verify_live=args.verify_live,
        )
    except ExecutionValidationError as exc:
        report = {"ok": False, "errors": [str(exc)]}
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
