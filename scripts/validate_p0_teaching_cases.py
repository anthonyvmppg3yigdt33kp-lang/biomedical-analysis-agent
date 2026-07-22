#!/usr/bin/env python3
"""Validate the P0 teaching-case registry and its optional private locator map."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import jsonschema


SKILL_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = SKILL_ROOT / "references" / "p0-teaching-cases.json"
DEFAULT_SCHEMA = SKILL_ROOT / "references" / "schemas" / "p0-teaching-case.schema.json"
DEFAULT_LOCATORS = SKILL_ROOT / "assets" / "private-corpus-index" / "p0-teaching-case-local-availability.json"
EXPECTED_DOMAINS = {
    "visualization",
    "single-cell",
    "spatial-transcriptomics",
    "bulk-rna",
    "quantitative-proteomics",
    "multi-omics",
    "literature-methodology",
}
PRIVATE_PATH = re.compile(r"(?i)(?:[a-z]:[\\/]|\\users\\|/home/)")
EXPECTED_GSE185948_SAMPLES = {
    "GSM5627690",
    "GSM5627691",
    "GSM5627692",
    "GSM5627693",
    "GSM5627694",
}


class RegistryValidationError(RuntimeError):
    """Raised when the teaching-case registry violates a semantic contract."""


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RegistryValidationError(f"Cannot load JSON {path}: {exc}") from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _walk_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _walk_strings(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _walk_strings(item)


def _validate_stage_graph(case: dict[str, Any], errors: list[str]) -> None:
    stages = case["workflow_plan"]["stages"]
    stage_ids = [stage["stage_id"] for stage in stages]
    if len(stage_ids) != len(set(stage_ids)):
        errors.append(f"{case['case_id']}:duplicate_stage_id")
        return
    position = {stage_id: index for index, stage_id in enumerate(stage_ids)}
    for stage in stages:
        for dependency in stage["depends_on"]:
            if dependency not in position:
                errors.append(f"{case['case_id']}:{stage['stage_id']}:unknown_dependency:{dependency}")
            elif position[dependency] >= position[stage["stage_id"]]:
                errors.append(f"{case['case_id']}:{stage['stage_id']}:dependency_not_prior:{dependency}")

    checkpoints = case["checkpoints"]
    checkpoint_ids = [item["checkpoint_id"] for item in checkpoints]
    if len(checkpoint_ids) != len(set(checkpoint_ids)):
        errors.append(f"{case['case_id']}:duplicate_checkpoint_id")
    declared = {item["checkpoint_id"]: item["after_stage"] for item in checkpoints}
    for stage in stages:
        checkpoint_id = stage["checkpoint_id"]
        if declared.get(checkpoint_id) != stage["stage_id"]:
            errors.append(f"{case['case_id']}:{stage['stage_id']}:checkpoint_mismatch:{checkpoint_id}")
    if set(declared) != set(stage["checkpoint_id"] for stage in stages):
        errors.append(f"{case['case_id']}:orphan_or_missing_checkpoint")


def _validate_public_metadata_binding(
    case: dict[str, Any],
    registry_path: Path,
    errors: list[str],
) -> bool:
    binding = case.get("public_metadata")
    if binding is None:
        return False

    starting_error_count = len(errors)
    case_id = case.get("case_id")
    metadata_path = (registry_path.parent / binding.get("path", "")).resolve()
    if metadata_path.parent != registry_path.parent.resolve():
        errors.append(f"{case_id}:public_metadata_path_outside_registry_directory")
        return False
    if not metadata_path.is_file():
        errors.append(f"{case_id}:public_metadata_missing:{binding.get('path')}")
        return False
    actual_sha256 = _sha256(metadata_path)
    if actual_sha256 != binding.get("sha256"):
        errors.append(f"{case_id}:public_metadata_hash_mismatch:{actual_sha256}")
        return False

    try:
        metadata = _load_json(metadata_path)
    except RegistryValidationError as exc:
        errors.append(f"{case_id}:public_metadata_invalid:{exc}")
        return False
    if metadata.get("metadata_id") != binding.get("metadata_id"):
        errors.append(f"{case_id}:public_metadata_id_mismatch")
    if metadata.get("case_id") != case_id:
        errors.append(f"{case_id}:public_metadata_case_id_mismatch")

    if case_id == "p0-single-cell-gse185948":
        series = metadata.get("series", {})
        samples = metadata.get("samples", [])
        sample_accessions = {
            sample.get("sample_accession") for sample in samples if isinstance(sample, dict)
        }
        citation = metadata.get("citation", {})
        governance = metadata.get("governance", {})
        if series.get("accession") != "GSE185948":
            errors.append(f"{case_id}:public_metadata_wrong_series_accession")
        if series.get("assay") != "single-nucleus RNA-seq":
            errors.append(f"{case_id}:public_metadata_wrong_assay")
        if series.get("source_material") != "nuclear RNA":
            errors.append(f"{case_id}:public_metadata_wrong_source_material")
        if series.get("genome_build") != "GRCh38":
            errors.append(f"{case_id}:public_metadata_wrong_genome_build")
        if "--include-introns" not in series.get("processing", ""):
            errors.append(f"{case_id}:public_metadata_missing_include_introns")
        if sample_accessions != EXPECTED_GSE185948_SAMPLES or len(samples) != 5:
            errors.append(f"{case_id}:public_metadata_sample_set_mismatch")
        if any(sample.get("assay") != "single-nucleus RNA-seq" for sample in samples):
            errors.append(f"{case_id}:public_metadata_sample_assay_mismatch")
        if citation.get("pmid") != "36310237" or citation.get("doi") != "10.1038/s41467-022-34255-z":
            errors.append(f"{case_id}:public_metadata_citation_mismatch")
        if citation.get("verification_status") != "verified":
            errors.append(f"{case_id}:public_metadata_citation_not_verified")
        if governance.get("license_status") != "pending" or governance.get("redistribution_status") != "pending":
            errors.append(f"{case_id}:public_metadata_governance_boundary_mismatch")
    return len(errors) == starting_error_count


def validate_registry(
    registry_path: Path,
    schema_path: Path,
    locator_path: Path | None = None,
    *,
    verify_local: bool = False,
) -> dict[str, Any]:
    registry = _load_json(registry_path)
    schema = _load_json(schema_path)
    validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
    schema_errors = sorted(validator.iter_errors(registry), key=lambda item: list(item.absolute_path))
    errors = [
        "schema:" + "/".join(str(part) for part in error.absolute_path) + f":{error.message}"
        for error in schema_errors
    ]

    if any(PRIVATE_PATH.search(text) for text in _walk_strings(registry)):
        errors.append("public_registry_contains_absolute_or_private_home_path")

    cases = registry.get("cases", []) if isinstance(registry, dict) else []
    case_ids = [case.get("case_id") for case in cases if isinstance(case, dict)]
    domains = [case.get("domain") for case in cases if isinstance(case, dict)]
    if len(case_ids) != len(set(case_ids)):
        errors.append("duplicate_case_id")
    if set(domains) != EXPECTED_DOMAINS or len(domains) != len(EXPECTED_DOMAINS):
        errors.append(f"domain_coverage_mismatch:{sorted(set(domains))}")
    rendered_registry = json.dumps(registry, ensure_ascii=False).lower()
    if "gse185809" in rendered_registry:
        errors.append("stale_wrong_accession_gse185809")
    if case_ids.count("p0-single-cell-gse185948") != 1:
        errors.append("p0_single_cell_gse185948_case_missing_or_duplicated")

    public_refs: set[str] = set()
    metadata_binding_count = 0
    verified_metadata_files = 0
    for case in cases:
        if not isinstance(case, dict) or "workflow_plan" not in case:
            continue
        if case.get("execution_status") != "not-executed":
            errors.append(f"{case.get('case_id')}:execution_status_must_be_not_executed")
        if case.get("maturity") not in {"raw-extracted", "normalized"}:
            errors.append(f"{case.get('case_id')}:candidate_maturity_too_high")
        if case.get("environment", {}).get("install_authorized") is not False:
            errors.append(f"{case.get('case_id')}:install_authorized_must_be_false")
        if case.get("case_id") == "p0-single-cell-gse185948" and case.get("workflow_plan", {}).get("allowed_mode") != "plan":
            errors.append("p0-single-cell-gse185948:allowed_mode_must_be_plan")
        _validate_stage_graph(case, errors)
        if case.get("public_metadata") is not None:
            metadata_binding_count += 1
            if _validate_public_metadata_binding(case, registry_path, errors):
                verified_metadata_files += 1
        availability = case.get("availability", {})
        refs = availability.get("local_locator_refs", [])
        public_refs.update(refs)
        integrity_refs = {item.get("locator_ref") for item in availability.get("integrity_evidence", [])}
        if set(refs) != integrity_refs:
            errors.append(f"{case.get('case_id')}:availability_ref_integrity_mismatch")

    locator_count = 0
    execution_only_locator_count = 0
    verified_files = 0
    locator_map: dict[str, Any] = {}
    if locator_path is not None:
        local = _load_json(locator_path)
        if local.get("distribution") != "private-local-only" or local.get("source_mode") != "read_only":
            errors.append("private_locator_contract_invalid")
        locator_map = local.get("locators", {})
        locator_count = len(locator_map)
        execution_only_refs = set(local.get("execution_only_locator_refs", []))
        execution_only_locator_count = len(execution_only_refs)
        missing = sorted(public_refs - set(locator_map))
        extra = sorted(set(locator_map) - public_refs)
        if missing:
            errors.append("missing_private_locators:" + ",".join(missing))
        undeclared_extra = sorted(set(extra) - execution_only_refs)
        stale_execution_only = sorted(execution_only_refs - set(extra))
        if undeclared_extra:
            errors.append("unreferenced_private_locators:" + ",".join(undeclared_extra))
        if stale_execution_only:
            errors.append("stale_execution_only_locator_refs:" + ",".join(stale_execution_only))

        evidence_by_ref = {
            evidence["locator_ref"]: evidence
            for case in cases
            for evidence in case.get("availability", {}).get("integrity_evidence", [])
        }
        for locator_ref, locator in locator_map.items():
            evidence = evidence_by_ref.get(locator_ref)
            if evidence and (
                locator.get("expected_sha256") != evidence.get("sha256")
                or locator.get("expected_size_bytes") != evidence.get("size_bytes")
            ):
                errors.append(f"{locator_ref}:public_private_integrity_mismatch")
            if not verify_local:
                continue
            path = Path(locator.get("path", ""))
            if not path.is_file():
                errors.append(f"{locator_ref}:local_file_missing")
                continue
            size = path.stat().st_size
            if size != locator.get("expected_size_bytes"):
                errors.append(f"{locator_ref}:local_size_mismatch:{size}")
                continue
            digest = _sha256(path)
            if digest != locator.get("expected_sha256"):
                errors.append(f"{locator_ref}:local_hash_mismatch:{digest}")
                continue
            verified_files += 1

        audits = local.get("not_cached_audits", {})
        for case in cases:
            if case.get("availability", {}).get("status") == "not-cached" and case.get("case_id") not in audits:
                errors.append(f"{case.get('case_id')}:missing_not_cached_audit")

    return {
        "ok": not errors,
        "registry": str(registry_path),
        "schema": str(schema_path),
        "case_count": len(cases),
        "domains": sorted(set(domains)),
        "candidate_maturity": sorted(set(case.get("maturity") for case in cases if isinstance(case, dict))),
        "execution_statuses": sorted(set(case.get("execution_status") for case in cases if isinstance(case, dict))),
        "public_locator_ref_count": len(public_refs),
        "public_metadata_binding_count": metadata_binding_count,
        "verified_public_metadata_files": verified_metadata_files,
        "private_locator_count": locator_count,
        "execution_only_private_locator_count": execution_only_locator_count,
        "verify_local": verify_local,
        "verified_local_files": verified_files,
        "errors": errors,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    parser.add_argument("--local-locators", type=Path, default=DEFAULT_LOCATORS)
    parser.add_argument("--no-local-locators", action="store_true")
    parser.add_argument("--verify-local", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    locator_path = None if args.no_local_locators else args.local_locators
    try:
        report = validate_registry(
            args.registry.resolve(),
            args.schema.resolve(),
            locator_path.resolve() if locator_path else None,
            verify_local=args.verify_local,
        )
    except RegistryValidationError as exc:
        report = {"ok": False, "errors": [str(exc)]}
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
