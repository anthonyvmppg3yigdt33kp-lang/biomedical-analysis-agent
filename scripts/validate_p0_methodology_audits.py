#!/usr/bin/env python3
"""Validate private explain-mode P0 methodology audits against live evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import jsonschema


SKILL_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = SKILL_ROOT / "assets" / "private-corpus-index" / "p0-methodology-audits.json"
DEFAULT_SCHEMA = SKILL_ROOT / "references" / "schemas" / "p0-methodology-audit.schema.json"
DEFAULT_CANDIDATES = SKILL_ROOT / "references" / "p0-teaching-cases.json"
DEFAULT_LOCATORS = SKILL_ROOT / "assets" / "private-corpus-index" / "p0-teaching-case-local-availability.json"


class AuditValidationError(RuntimeError):
    """Raised when a methodology-audit evidence document cannot be read safely."""


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuditValidationError(f"cannot_load_json:{path}:{exc}") from exc


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
        raise AuditValidationError(f"path_escapes_root:{relative}") from exc
    return candidate


def _verify_file(root: Path, evidence: dict[str, Any], label: str, errors: list[str]) -> Path | None:
    try:
        path = _inside(root, str(evidence["relative_path"]))
    except (KeyError, AuditValidationError) as exc:
        errors.append(f"{label}:{exc}")
        return None
    if not path.is_file():
        errors.append(f"{label}:missing:{evidence.get('relative_path')}")
        return None
    if path.stat().st_size != evidence.get("size_bytes"):
        errors.append(f"{label}:size_mismatch")
    if _sha256(path) != evidence.get("sha256"):
        errors.append(f"{label}:hash_mismatch")
    return path


def _verify_external(evidence: dict[str, Any], label: str, errors: list[str]) -> Path | None:
    path = Path(str(evidence.get("path", "")))
    if not path.is_file():
        errors.append(f"{label}:missing")
        return None
    if path.stat().st_size != evidence.get("size_bytes"):
        errors.append(f"{label}:size_mismatch")
    if _sha256(path) != evidence.get("sha256"):
        errors.append(f"{label}:hash_mismatch")
    return path


def _tree_contract(root: Path, *, exclude: set[str] | None = None) -> dict[str, Any]:
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


def _schema_errors(document: Any, schema: Any) -> list[str]:
    validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
    return [
        "schema:" + "/".join(str(part) for part in error.absolute_path) + f":{error.message}"
        for error in sorted(validator.iter_errors(document), key=lambda item: list(item.absolute_path))
    ]


def _validate_delivery_index(output_root: Path, index_path: Path, prefix: str, errors: list[str]) -> int:
    text = index_path.read_text(encoding="utf-8")
    indexed: dict[str, tuple[int, str]] = {}
    pattern = re.compile(r"^\| `([^`]+)` \| ([0-9]+) \| `([0-9a-f]{64})` \|$")
    for line in text.splitlines():
        match = pattern.match(line)
        if match:
            indexed[match.group(1)] = (int(match.group(2)), match.group(3))
    actual = {
        path.relative_to(output_root).as_posix(): path
        for path in output_root.rglob("*")
        if path.is_file() and path.resolve() != index_path.resolve()
    }
    if set(indexed) != set(actual):
        errors.append(f"{prefix}:delivery_artifact_index_exact_set_mismatch")
    verified = 0
    for relative, path in actual.items():
        size, digest = indexed.get(relative, (-1, ""))
        if path.stat().st_size != size or _sha256(path) != digest:
            errors.append(f"{prefix}:delivery_artifact_integrity_mismatch:{relative}")
        else:
            verified += 1
    return verified


def validate_methodology_audits(
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

    candidate_by_id = {item["case_id"]: item for item in candidates.get("cases", [])}
    locator_by_ref = locators.get("locators", {})
    audits = registry.get("audits", []) if isinstance(registry, dict) else []
    audit_ids = [item.get("audit_id") for item in audits if isinstance(item, dict)]
    case_ids = [item.get("case_id") for item in audits if isinstance(item, dict)]
    if len(audit_ids) != len(set(audit_ids)):
        errors.append("duplicate_audit_id")
    if len(case_ids) != len(set(case_ids)):
        errors.append("duplicate_case_audit")

    # The private explain audit is an overlay and cannot promote the public design record.
    for case in candidate_by_id.values():
        if case.get("execution_status") != "not-executed":
            errors.append(f"{case.get('case_id')}:public_execution_status_promoted")
        if case.get("maturity") not in {"raw-extracted", "normalized"}:
            errors.append(f"{case.get('case_id')}:public_maturity_promoted")
        if case.get("environment", {}).get("install_authorized") is not False:
            errors.append(f"{case.get('case_id')}:public_install_authorization_changed")

    verified_source_files = 0
    verified_formal_files = 0
    verified_formal_artifacts = 0
    verified_delivery_files = 0
    verified_checkpoints = 0
    verified_native_figures = 0

    for audit in audits:
        if not isinstance(audit, dict):
            continue
        prefix = str(audit.get("audit_id", "unknown-audit"))
        candidate = candidate_by_id.get(audit.get("case_id"))
        if candidate is None:
            errors.append(f"{prefix}:unknown_public_case")
            continue
        if candidate.get("domain") != audit.get("domain"):
            errors.append(f"{prefix}:domain_mismatch")
        if candidate.get("workflow_plan", {}).get("allowed_mode") != "explain":
            errors.append(f"{prefix}:public_case_not_explain_only")

        inputs = audit.get("inputs", {})
        artifacts = inputs.get("artifacts", []) if isinstance(inputs, dict) else []
        refs = [item.get("locator_ref") for item in artifacts if isinstance(item, dict)]
        expected_refs = candidate.get("availability", {}).get("local_locator_refs", [])
        if len(refs) != len(set(refs)) or set(refs) != set(expected_refs):
            errors.append(f"{prefix}:input_locator_set_mismatch")
        public_integrity = {
            item.get("locator_ref"): item
            for item in candidate.get("availability", {}).get("integrity_evidence", [])
            if isinstance(item, dict)
        }
        if sum(int(item.get("size_bytes", 0)) for item in artifacts) != inputs.get("total_size_bytes"):
            errors.append(f"{prefix}:input_total_size_mismatch")
        for item in artifacts:
            locator_ref = item.get("locator_ref")
            public = public_integrity.get(locator_ref)
            private = locator_by_ref.get(locator_ref)
            if public is None or public.get("sha256") != item.get("sha256") or public.get("size_bytes") != item.get("size_bytes"):
                errors.append(f"{prefix}:public_input_evidence_mismatch:{locator_ref}")
            if private is None or private.get("expected_sha256") != item.get("sha256") or private.get("expected_size_bytes") != item.get("size_bytes"):
                errors.append(f"{prefix}:private_input_evidence_mismatch:{locator_ref}")

        if not verify_live:
            continue

        source_hashes: set[str] = set()
        for item in artifacts:
            locator_ref = item.get("locator_ref")
            private = locator_by_ref.get(locator_ref, {})
            path = _verify_external(
                {"path": private.get("path", ""), "sha256": item.get("sha256"), "size_bytes": item.get("size_bytes")},
                f"{prefix}:source:{locator_ref}",
                errors,
            )
            if path is not None:
                source_hashes.add(str(item.get("sha256")))
                verified_source_files += 1

        source_figure = inputs.get("source_figure", {})
        figure_path = _verify_external(source_figure, f"{prefix}:source_figure", errors)
        if figure_path is not None:
            if _png_dimensions(figure_path) != tuple(source_figure.get("dimensions_xy", [])):
                errors.append(f"{prefix}:source_figure_dimensions_mismatch")
            else:
                verified_native_figures += 1
            source_hashes.add(str(source_figure.get("sha256")))
            verified_source_files += 1

        task_root = Path(str(audit.get("task_root", "")))
        if not task_root.is_dir():
            errors.append(f"{prefix}:task_root_missing")
            continue
        run = audit.get("audit_run", {})
        try:
            run_root = _inside(task_root, str(run.get("run_root", "")))
        except AuditValidationError as exc:
            errors.append(f"{prefix}:run_root:{exc}")
            continue
        if not run_root.is_dir():
            errors.append(f"{prefix}:run_root_missing")
            continue

        evidence_paths: dict[str, Path] = {}
        for name, evidence in audit.get("evidence", {}).items():
            path = _verify_file(task_root, evidence, f"{prefix}:evidence:{name}", errors)
            if path is not None:
                evidence_paths[name] = path
                verified_formal_files += 1

        run_manifest = _load_json(evidence_paths["run_manifest"]) if "run_manifest" in evidence_paths else {}
        stages = run_manifest.get("stages", [])
        stage_ids = [item.get("stage_id") for item in stages if isinstance(item, dict)]
        expected_stage_ids = run.get("stage_ids", [])
        if (
            run_manifest.get("run_id") != run.get("run_id")
            or run_manifest.get("mode") != run.get("mode")
            or run_manifest.get("state") != run.get("state")
            or run_manifest.get("source_code_executed") is not False
            or run_manifest.get("package_installation_performed") is not False
            or run_manifest.get("latest_valid_checkpoint") != run.get("latest_valid_checkpoint")
            or run_manifest.get("artifact_count") != run.get("artifact_count")
            or run_manifest.get("ledger_entry_count") != run.get("ledger_entry_count")
            or stage_ids != expected_stage_ids
        ):
            errors.append(f"{prefix}:run_manifest_contract_mismatch")

        stage_by_id = {item.get("stage_id"): item for item in stages if isinstance(item, dict)}
        for stage_id in expected_stage_ids:
            stage_root = run_root / "04_intermediate" / stage_id
            checkpoint_path = stage_root / "checkpoint.json"
            if not checkpoint_path.is_file():
                errors.append(f"{prefix}:checkpoint_missing:{stage_id}")
                continue
            checkpoint = _load_json(checkpoint_path)
            observed = _tree_contract(stage_root, exclude={"checkpoint.json"})
            manifest_stage = stage_by_id.get(stage_id, {})
            if (
                checkpoint.get("stage_id") != stage_id
                or checkpoint.get("status") != "checkpointed"
                or checkpoint.get("payload_tree_sha256") != observed["tree_sha256"]
                or checkpoint.get("payload_file_count") != observed["file_count"]
                or manifest_stage.get("payload_tree_sha256") != observed["tree_sha256"]
                or manifest_stage.get("payload_file_count") != observed["file_count"]
            ):
                errors.append(f"{prefix}:checkpoint_contract_mismatch:{stage_id}")
            else:
                verified_checkpoints += 1

        artifact_index = _load_json(evidence_paths["artifact_index"]) if "artifact_index" in evidence_paths else {}
        indexed = artifact_index.get("artifacts", [])
        indexed_by_path = {
            item.get("relative_path"): item for item in indexed if isinstance(item, dict)
        }
        actual_paths = {
            path.relative_to(run_root).as_posix()
            for path in run_root.rglob("*")
            if path.is_file()
        } - {
            "07_reports/ARTIFACT_INDEX.json",
            "07_reports/ARTIFACT_INDEX.md",
            "manifest/artifact_ledger.jsonl",
            "manifest/run_manifest.json",
        }
        if (
            artifact_index.get("artifact_count") != run.get("artifact_count")
            or len(indexed) != run.get("artifact_count")
            or len(indexed_by_path) != len(indexed)
            or set(indexed_by_path) != actual_paths
        ):
            errors.append(f"{prefix}:formal_artifact_index_exact_set_mismatch")
        for relative, item in indexed_by_path.items():
            try:
                path = _inside(run_root, str(relative))
            except AuditValidationError as exc:
                errors.append(f"{prefix}:artifact:{exc}")
                continue
            if not path.is_file() or path.stat().st_size != item.get("size_bytes") or _sha256(path) != item.get("sha256"):
                errors.append(f"{prefix}:formal_artifact_integrity_mismatch:{relative}")
            else:
                verified_formal_artifacts += 1
        ledger_entries: list[dict[str, Any]] = []
        if "artifact_ledger" in evidence_paths:
            try:
                ledger_entries = [
                    json.loads(line)
                    for line in evidence_paths["artifact_ledger"].read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
            except json.JSONDecodeError as exc:
                errors.append(f"{prefix}:artifact_ledger_invalid:{exc}")
        if ledger_entries != indexed or len(ledger_entries) != run.get("ledger_entry_count"):
            errors.append(f"{prefix}:artifact_ledger_index_mismatch")

        input_manifest_path = run_root / "00_request" / "input_manifest.json"
        input_manifest = _load_json(input_manifest_path) if input_manifest_path.is_file() else {}
        manifest_sources = input_manifest.get("sources", {})
        for role, locator_ref in (("article", "local:p0.literature.rogue.article"), ("code", "local:p0.literature.rogue.code")):
            declared = next((item for item in artifacts if item.get("locator_ref") == locator_ref), {})
            observed = manifest_sources.get(role, {})
            if (
                observed.get("locator_ref") != locator_ref
                or observed.get("sha256") != declared.get("sha256")
                or observed.get("size_bytes") != declared.get("size_bytes")
                or observed.get("copied_into_run") is not False
            ):
                errors.append(f"{prefix}:input_manifest_source_mismatch:{role}")
        manifest_figure = manifest_sources.get("figure", {})
        if (
            manifest_figure.get("sha256") != source_figure.get("sha256")
            or manifest_figure.get("dimensions_xy") != source_figure.get("dimensions_xy")
            or manifest_figure.get("copied_into_run") is not False
        ):
            errors.append(f"{prefix}:input_manifest_figure_mismatch")

        sourceflow = _load_json(evidence_paths["sourceflow_audit"]) if "sourceflow_audit" in evidence_paths else {}
        if (
            sourceflow.get("bundle_id") != inputs.get("bundle_id")
            or sourceflow.get("preprocessing_record_id") != inputs.get("preprocessing_record_id")
            or sourceflow.get("source_code_executed") is not False
            or sourceflow.get("source_content_redistributed") is not False
            or sourceflow.get("representative_native_image_reviewed") != source_figure.get("source_figure_id")
        ):
            errors.append(f"{prefix}:sourceflow_audit_contract_mismatch")

        risk_text = evidence_paths.get("risk_ledger", Path()).read_text(encoding="utf-8") if "risk_ledger" in evidence_paths else ""
        required_risks = {
            "installer-in-recipe", "working-directory-mutation", "hard-coded-conda",
            "random-pseudo-samples", "pseudoreplicate-plot", "dense-count-conversion",
            "platform-parallelism", "no-inferential-model",
        }
        observed_risks = {
            line.split("\t", 1)[0]
            for line in risk_text.splitlines()[1:]
            if line.strip() and "\t" in line
        }
        if not required_risks.issubset(observed_risks):
            errors.append(f"{prefix}:required_code_risks_missing")

        method_card = _load_json(evidence_paths["method_card"]) if "method_card" in evidence_paths else {}
        if (
            method_card.get("maturity") != "normalized"
            or method_card.get("statistical_unit", {}).get("inference") != "independent donor/sample"
            or not any("Random cell-to-pseudo-sample" in item for item in method_card.get("negative_controls", []))
            or not any("real sample/donor" in item for item in method_card.get("combination_logic", []))
            or method_card.get("primary_reference", {}).get("doi") != "10.1038/s41467-020-16904-3"
        ):
            errors.append(f"{prefix}:method_card_scientific_contract_mismatch")
        package_card = _load_json(evidence_paths["package_card"]) if "package_card" in evidence_paths else {}
        if (
            package_card.get("name") != "ROGUE"
            or "unlocked" not in str(package_card.get("version", "")).lower()
            or "never run source installer lines" not in str(package_card.get("installation_policy", ""))
        ):
            errors.append(f"{prefix}:package_card_boundary_mismatch")
        figure_card = _load_json(evidence_paths["figure_card"]) if "figure_card" in evidence_paths else {}
        if (
            figure_card.get("maturity") != "native-reviewed"
            or figure_card.get("source_sha256") != source_figure.get("sha256")
            or figure_card.get("dimensions_xy") != source_figure.get("dimensions_xy")
            or figure_card.get("visual_qa") != "PASS_FOR_STYLE_ONLY_BLOCK_FOR_SCIENTIFIC_RESULT"
            or figure_card.get("reproduction_class") != "exact_plot_semantics_invalid_input"
            or len(figure_card.get("cannot_assert", [])) < 3
            or figure_card.get("reuse_policy") != "retain style as a variant; reject the source input construction and all biological conclusions"
        ):
            errors.append(f"{prefix}:figure_card_claim_boundary_mismatch")
        native_review = _load_json(evidence_paths["native_review"]) if "native_review" in evidence_paths else {}
        declared_native = audit.get("native_review", {})
        if (
            native_review.get("review_state") != declared_native.get("state")
            or native_review.get("decision") != declared_native.get("decision")
            or native_review.get("reviewed_native_pixels") is not True
            or native_review.get("source_sha256") != source_figure.get("sha256")
            or native_review.get("dimensions_xy") != source_figure.get("dimensions_xy")
            or native_review.get("source_image_copied") is not False
            or native_review.get("blocking_scientific_findings", 0) < 1
        ):
            errors.append(f"{prefix}:native_review_binding_mismatch")
        qa_text = evidence_paths.get("qa_report", Path()).read_text(encoding="utf-8") if "qa_report" in evidence_paths else ""
        if (
            "PASS_AS_METHODOLOGY_NEGATIVE_CONTROL_ONLY" not in qa_text
            or "NOT PERFORMED" not in qa_text
            or "No biological or clinical conclusion was generated." not in qa_text
        ):
            errors.append(f"{prefix}:qa_claim_boundary_mismatch")

        delivery = audit.get("delivery", {})
        output_root = Path(str(delivery.get("output_root", "")))
        delivery_index = _verify_external(delivery.get("artifact_index", {}), f"{prefix}:delivery_index", errors)
        if not output_root.is_dir() or delivery_index is None or delivery_index.parent.resolve() != output_root.resolve():
            errors.append(f"{prefix}:delivery_root_or_index_invalid")
        else:
            actual_file_count = sum(1 for item in output_root.rglob("*") if item.is_file())
            if actual_file_count != delivery.get("file_count"):
                errors.append(f"{prefix}:delivery_file_count_mismatch")
            verified_delivery_files += _validate_delivery_index(output_root, delivery_index, prefix, errors)

        # Raw article/code/figure bytes must not appear in the formal run or delivery.
        for root in [run_root, output_root]:
            if not root.is_dir():
                continue
            for path in (item for item in root.rglob("*") if item.is_file()):
                if path.stat().st_size in {10116, 14347, 66844} and _sha256(path) in source_hashes:
                    errors.append(f"{prefix}:source_bytes_redistributed:{path}")
        if "methodology" not in str(audit.get("maturity_scope", "")).lower() or not audit.get("known_limitations"):
            errors.append(f"{prefix}:audit_maturity_scope_not_bounded")
        if "does not support" not in str(audit.get("scientific_claim_ceiling", "")).lower():
            errors.append(f"{prefix}:scientific_claim_ceiling_too_broad")

    report = {
        "ok": not errors,
        "verify_live": verify_live,
        "audit_count": len(audits),
        "audited_case_count": len(set(case_ids)),
        "verified_source_files": verified_source_files,
        "verified_formal_files": verified_formal_files,
        "verified_formal_artifacts": verified_formal_artifacts,
        "verified_delivery_files": verified_delivery_files,
        "verified_checkpoints": verified_checkpoints,
        "verified_native_figures": verified_native_figures,
        "errors": errors,
    }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    parser.add_argument("--candidates", type=Path, default=DEFAULT_CANDIDATES)
    parser.add_argument("--locators", type=Path, default=DEFAULT_LOCATORS)
    parser.add_argument("--verify-live", action="store_true")
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()
    report = validate_methodology_audits(
        args.registry,
        args.schema,
        args.candidates,
        args.locators,
        verify_live=args.verify_live,
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(rendered + "\n", encoding="utf-8")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
