#!/usr/bin/env python3
"""Prepare and validate evidence-bound reviews for a materialized distillation batch.

This tool is deliberately read-only with respect to source corpora. ``prepare`` writes
review skeletons containing immutable evidence snapshots; ``validate`` checks manual
claims against the current queue, crosswalk, bundles and FigureCards. Validation never
promotes a record to fixture/data/native-reviewed maturity.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Sequence

from jsonschema import Draft202012Validator


SKILL_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INDEX = SKILL_ROOT / "assets" / "private-corpus-index"
DEFAULT_SCHEMA = SKILL_ROOT / "references" / "schemas" / "distillation-review-record.schema.json"
REQUIRED_STAGES = [
    "SOURCE_VALIDATED",
    "METHOD_REVIEWED",
    "CODE_REVIEWED",
    "FIGURE_CONTEXT_REVIEWED",
    "SCIENTIFIC_QA",
    "COMPLETE",
]
FORBIDDEN_PLACEHOLDERS = re.compile(r"(?i)(?:\bTODO\b|\bTBD\b|待补|待审|占位符)")
MARKDOWN_IMAGE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
FENCE = re.compile(r"^```([^\r\n]*)\r?\n(.*?)^```\s*$", re.MULTILINE | re.DOTALL)


class ReviewValidationError(RuntimeError):
    """Raised when review inputs cannot be interpreted."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ReviewValidationError(f"Expected object at {path}:{line_number}")
            rows.append(value)
    return rows


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    if path.exists():
        raise FileExistsError(f"Refusing to overwrite review file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    count = 0
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
            count += 1
    os.replace(temporary, path)
    return count


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_id(prefix: str, *parts: str) -> str:
    payload = chr(31).join(parts).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(payload).hexdigest()[:20]}"


def _skill_root(skill_name: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", skill_name):
        raise ReviewValidationError(f"Unsafe external skill name: {skill_name!r}")
    return SKILL_ROOT.parent / skill_name


def _external_article(target: dict[str, Any]) -> Path:
    root = _skill_root(str(target["target_skill"])).resolve()
    path = (root / str(target["target_relative_path"])).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ReviewValidationError(f"External target escapes skill root: {path}") from exc
    return path


def _external_inventory(target: dict[str, Any]) -> dict[str, Any]:
    path = _external_article(target)
    text = path.read_text(encoding="utf-8-sig")
    fences = [match.group(2) for match in FENCE.finditer(text)]
    image_paths: list[Path] = []
    archive_dir = path.parent / "assets" / path.stem
    if archive_dir.is_dir():
        image_paths = sorted(item for item in archive_dir.rglob("*") if item.is_file())
    else:
        # Fallback for archives that retained direct markdown-relative assets.
        for raw_target in MARKDOWN_IMAGE.findall(text):
            raw_target = raw_target.strip().strip("<>").split("#", 1)[0].split("?", 1)[0]
            candidate = (path.parent / raw_target).resolve()
            if candidate.is_file():
                image_paths.append(candidate)
    return {
        "path": path,
        "fenced_code_count": len(fences),
        "fenced_code_sha256": [hashlib.sha256(value.encode("utf-8")).hexdigest() for value in fences],
        "image_paths": image_paths,
        "image_sha256": [sha256_file(value) for value in image_paths],
    }


def expected_bundle_evidence(bundle: dict[str, Any]) -> dict[str, Any]:
    article = bundle.get("article", {})
    ordered = bundle.get("ordered_code_files", [])
    fenced = article.get("fenced_code_blocks", [])
    images = bundle.get("images", [])
    return {
        "bundle_id": bundle["bundle_id"],
        "article_sha256": article.get("sha256"),
        "flow_fingerprint_sha256": bundle.get("flow_integrity", {}).get("flow_fingerprint_sha256"),
        "ordered_code_count": len(ordered),
        "fenced_code_count": len(fenced),
        "ordered_code_sha256": [item.get("sha256") for item in ordered],
        "fenced_code_sha256": [item.get("sha256") for item in fenced],
        "figure_count": len(images),
        "figure_sha256": [item.get("sha256") for item in images],
    }


def expected_external_evidence(target: dict[str, Any]) -> dict[str, Any]:
    inventory = _external_inventory(target)
    return {
        "target_skill": target.get("target_skill"),
        "target_relative_path": target.get("target_relative_path"),
        "sha256": target.get("sha256"),
        "fenced_code_count": inventory["fenced_code_count"],
        "image_reference_count": len(inventory["image_paths"]),
    }


def load_index(index: Path) -> dict[str, Any]:
    return {
        "records": {row["preprocess_record_id"]: row for row in read_jsonl(index / "preprocessing-records.jsonl")},
        "crosswalk": {row["preprocess_record_id"]: row for row in read_jsonl(index / "preprocessing-crosswalk.jsonl")},
        "bundles": {row["bundle_id"]: row for row in read_jsonl(index / "source-flow-bundles.jsonl")},
        "figures": {row["figure_card_id"]: row for row in read_jsonl(index / "figure-cards.jsonl")},
        "queue_manifest_sha256": sha256_file(index / "distillation-review-manifest.json"),
    }


def _batch_queue_manifest_sha256(batch_path: Path, batch: Sequence[dict[str, Any]], current_sha256: str) -> str:
    """Bind review evidence to the immutable materialized-batch receipt when present."""

    receipt_path = batch_path.with_suffix(batch_path.suffix + ".receipt.json")
    if not receipt_path.exists():
        return current_sha256
    receipt = read_json(receipt_path)
    expected_batch_id = next((str(row.get("batch_id") or "") for row in batch if row.get("batch_id")), "")
    if receipt.get("ok") is not True:
        raise ReviewValidationError(f"Materialized batch receipt is not successful: {receipt_path}")
    if receipt.get("batch_id") != expected_batch_id:
        raise ReviewValidationError(f"Materialized batch receipt batch_id mismatch: {receipt_path}")
    if receipt.get("items") != len(batch):
        raise ReviewValidationError(f"Materialized batch receipt item count mismatch: {receipt_path}")
    if receipt.get("batch_sha256") != sha256_file(batch_path):
        raise ReviewValidationError(f"Materialized batch receipt hash mismatch: {receipt_path}")
    manifest_sha256 = str(receipt.get("queue_manifest_sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", manifest_sha256):
        raise ReviewValidationError(f"Materialized batch receipt lacks queue manifest SHA-256: {receipt_path}")
    return manifest_sha256


def build_skeleton(queue_item: dict[str, Any], registry: dict[str, Any], reviewer_id: str) -> dict[str, Any]:
    record_id = queue_item["preprocess_record_id"]
    record = registry["records"][record_id]
    bundle_evidence = [expected_bundle_evidence(registry["bundles"][bundle_id]) for bundle_id in queue_item["linked_bundle_ids"]]
    external_evidence = [expected_external_evidence(target) for target in queue_item.get("external_targets", [])]
    expected_ordered = sum(item["ordered_code_count"] for item in bundle_evidence)
    expected_fenced = sum(item["fenced_code_count"] for item in bundle_evidence) + sum(item["fenced_code_count"] for item in external_evidence)
    expected_figures = sum(item["figure_count"] for item in bundle_evidence) + sum(item["image_reference_count"] for item in external_evidence)
    modality = str(record.get("record", {}).get("数据类型") or "TODO: inspect source modality")
    no_code = expected_ordered + expected_fenced == 0
    return {
        "schema_version": "1.0",
        "review_id": stable_id("distill-review-record", record_id, "v1"),
        "batch_id": queue_item["batch_id"],
        "batch_position": queue_item["batch_position"],
        "queue_item_id": queue_item["queue_item_id"],
        "preprocess_record_id": record_id,
        "record_title": queue_item["record_title"],
        "category": queue_item["category"],
        "source_evidence": {
            "record_sha256": queue_item["record_sha256"],
            "source_relation_sha256": queue_item["source_relation_sha256"],
            "queue_manifest_sha256": registry["queue_manifest_sha256"],
            "bundles": bundle_evidence,
            "external_targets": external_evidence,
            "source_mode": "read_only",
        },
        "research_context": {
            "research_question": "TODO: state the exact research question",
            "input_modality": modality,
            "cohort_or_sample_structure": "TODO: distinguish examples from a real cohort",
            "descriptive_unit": "TODO: define plotted or summarized unit",
            "inferential_unit": "TODO: define independent experimental unit",
        },
        "method_sequence": [{"order": 1, "method": "TODO: method", "rationale": "TODO: rationale", "inputs": [], "outputs": []}],
        "combination_logic": "TODO: explain why the methods are combined and where they are not interchangeable",
        "package_usage": [],
        "code_review": {
            "review_scope": "no_code_available" if no_code else ("external_article_full" if not bundle_evidence else "full_inventory"),
            "ordered_code_items_reviewed": expected_ordered,
            "fenced_code_items_reviewed": expected_fenced,
            "languages": [],
            "syntax_evidence_ref": None,
            "completeness": "no_code" if no_code else "partial_flow",
            "object_chain": "TODO: summarize object production and consumption",
            "required_inputs": [],
            "installers_found": [],
            "hardcoded_paths": [],
            "undefined_objects": [],
            "gaps": ["TODO: record every reproducibility gap"],
            "source_code_immutable": True,
            "repair_required": not no_code,
            "executable_eligibility": "blocked",
        },
        "figure_context_review": {
            "status": "not_applicable_no_source_figure" if expected_figures == 0 else "unresolved",
            "figures": [],
            "unreviewed_figure_count": expected_figures,
            "selection_rationale": "TODO: explain figure selection or absence",
        },
        "scientific_review": {
            "assumptions": ["TODO: assumption"],
            "scientific_risks": ["TODO: scientific risk"],
            "alternatives": ["TODO: scientifically distinct alternative"],
            "negative_controls": ["TODO: negative control or falsification check"],
            "validation_required": ["TODO: required validation"],
            "claim_ceiling": "TODO: state what the method cannot establish",
        },
        "stage_evidence": [
            {"stage": stage, "status": "blocked", "evidence": "TODO: complete manual review"}
            for stage in REQUIRED_STAGES
        ],
        "decision": {
            "classification": "blocked_evidence",
            "target_objects": [],
            "reason": "TODO: adjudicate after review",
            "automatic_execution_allowed": False,
        },
        "maturity": "raw-extracted",
        "maturity_blockers": ["TODO: review not complete"],
        "reviewer": {"reviewer_id": reviewer_id, "reviewed_at": utc_now(), "tools": ["source hash inventory"]},
        "review_state": "IN_PROGRESS",
    }


def prepare_reviews(index: Path, batch_path: Path, output: Path, positions: set[int] | None, reviewer_id: str) -> dict[str, Any]:
    registry = load_index(index)
    batch = read_jsonl(batch_path)
    registry["queue_manifest_sha256"] = _batch_queue_manifest_sha256(
        batch_path,
        batch,
        registry["queue_manifest_sha256"],
    )
    selected = [row for row in batch if positions is None or int(row["batch_position"]) in positions]
    if not selected:
        raise ReviewValidationError("No batch records selected")
    count = write_jsonl(output, (build_skeleton(row, registry, reviewer_id) for row in selected))
    return {"ok": True, "records": count, "output": str(output), "sha256": sha256_file(output)}


def _schema_errors(schema: dict[str, Any], record: dict[str, Any]) -> list[str]:
    validator = Draft202012Validator(schema)
    return [f"{'.'.join(str(v) for v in error.absolute_path) or '<root>'}: {error.message}" for error in sorted(validator.iter_errors(record), key=lambda item: list(item.absolute_path))]


def _contains_placeholder(record: dict[str, Any]) -> bool:
    return bool(FORBIDDEN_PLACEHOLDERS.search(json.dumps(record, ensure_ascii=False)))


def _expected_figure_hashes(queue_item: dict[str, Any], registry: dict[str, Any]) -> set[str]:
    hashes: set[str] = set()
    for bundle_id in queue_item["linked_bundle_ids"]:
        hashes.update(str(item["sha256"]) for item in registry["bundles"][bundle_id].get("images", []))
    for target in queue_item.get("external_targets", []):
        hashes.update(_external_inventory(target)["image_sha256"])
    return hashes


def _expected_code_counts(queue_item: dict[str, Any], registry: dict[str, Any]) -> tuple[int, int]:
    ordered = fenced = 0
    for bundle_id in queue_item["linked_bundle_ids"]:
        bundle = registry["bundles"][bundle_id]
        ordered += len(bundle.get("ordered_code_files", []))
        fenced += len(bundle.get("article", {}).get("fenced_code_blocks", []))
    for target in queue_item.get("external_targets", []):
        fenced += _external_inventory(target)["fenced_code_count"]
    return ordered, fenced


def _parse_verified_errors(
    index: Path,
    review: dict[str, Any],
    queue_item: dict[str, Any],
    expected_count: int,
    cache: dict[Path, list[dict[str, Any]]],
) -> list[str]:
    """Require all raw declared code items to pass the referenced syntax audit."""

    record_id = str(review.get("preprocess_record_id") or "")
    reference = review.get("code_review", {}).get("syntax_evidence_ref")
    if not isinstance(reference, str) or not reference.strip():
        return ["parse-verified lacks syntax_evidence_ref"]
    match = re.fullmatch(r"(?P<path>[^#]+)#preprocess_record_id=(?P<record>prep-[0-9a-f]{24})", reference.strip())
    if not match:
        return ["parse-verified syntax_evidence_ref has invalid format"]
    if match.group("record") != record_id:
        return ["parse-verified syntax_evidence_ref names a different record"]
    relative = Path(match.group("path").replace("\\", "/"))
    if relative.is_absolute():
        return ["parse-verified syntax evidence path must be index-relative"]
    audit_root = (index / "manual-review" / "syntax-audits").resolve()
    audit_path = (index / relative).resolve()
    try:
        audit_path.relative_to(audit_root)
    except ValueError:
        return ["parse-verified syntax evidence escapes manual-review/syntax-audits"]
    if not audit_path.is_file():
        return ["parse-verified syntax evidence file is missing"]
    try:
        if audit_path not in cache:
            cache[audit_path] = read_jsonl(audit_path)
        rows = cache[audit_path]
    except (OSError, ValueError, json.JSONDecodeError, ReviewValidationError) as exc:
        return [f"parse-verified syntax evidence is unreadable: {type(exc).__name__}"]
    record_rows = [row for row in rows if row.get("preprocess_record_id") == record_id]
    errors: list[str] = []
    if expected_count < 1:
        errors.append("parse-verified requires at least one declared code item")
    if len(record_rows) != expected_count:
        errors.append(f"parse-verified syntax evidence count {len(record_rows)} != {expected_count}")
    audit_ids = [str(row.get("audit_item_id") or "") for row in record_rows]
    if not all(audit_ids) or len(audit_ids) != len(set(audit_ids)):
        errors.append("parse-verified syntax evidence has missing or duplicate audit_item_id")
    if any(row.get("record_sha256") != queue_item.get("record_sha256") for row in record_rows):
        errors.append("parse-verified syntax evidence record hash mismatch")
    if any(row.get("batch_id") != queue_item.get("batch_id") for row in record_rows):
        errors.append("parse-verified syntax evidence batch mismatch")
    if any(row.get("parse_status") != "passed" for row in record_rows):
        errors.append("parse-verified requires every raw declared code item to pass; candidate normalization does not count")
    if any(row.get("hash_verified") is not True for row in record_rows):
        errors.append("parse-verified syntax evidence contains an unverified code hash")
    if any(
        row.get("item_type") == "article_fenced_block" and row.get("article_hash_verified") is not True
        for row in record_rows
    ):
        errors.append("parse-verified article source hash is not verified")
    if any(
        row.get("item_type") == "external_markdown_fenced_block" and row.get("external_article_hash_verified") is not True
        for row in record_rows
    ):
        errors.append("parse-verified external article hash is not verified")
    return errors


def validate_reviews(index: Path, batch_path: Path, review_paths: Sequence[Path], require_complete: bool = False) -> dict[str, Any]:
    registry = load_index(index)
    schema = read_json(DEFAULT_SCHEMA)
    batch = read_jsonl(batch_path)
    registry["queue_manifest_sha256"] = _batch_queue_manifest_sha256(
        batch_path,
        batch,
        registry["queue_manifest_sha256"],
    )
    expected = {row["preprocess_record_id"]: row for row in batch}
    reviews: list[dict[str, Any]] = []
    errors: list[str] = []
    for path in review_paths:
        reviews.extend(read_jsonl(path))
    seen: set[str] = set()
    maturity: dict[str, int] = {}
    native_pixel_samples = 0
    figure_context_samples = 0
    parse_verified_records = 0
    syntax_cache: dict[Path, list[dict[str, Any]]] = {}

    for ordinal, review in enumerate(reviews, start=1):
        record_id = str(review.get("preprocess_record_id") or "")
        prefix = f"review[{ordinal}] {record_id or '<missing>'}"
        if record_id in seen:
            errors.append(f"{prefix}: duplicate preprocess_record_id")
        seen.add(record_id)
        queue_item = expected.get(record_id)
        if queue_item is None:
            errors.append(f"{prefix}: record is not in materialized batch")
            continue
        for error in _schema_errors(schema, review):
            errors.append(f"{prefix}: schema {error}")
        if _contains_placeholder(review):
            errors.append(f"{prefix}: unresolved TODO/TBD placeholder")
        scalar_expected = {
            "review_id": stable_id("distill-review-record", record_id, "v1"),
            "batch_id": queue_item["batch_id"],
            "batch_position": queue_item["batch_position"],
            "queue_item_id": queue_item["queue_item_id"],
            "record_title": queue_item["record_title"],
            "category": queue_item["category"],
        }
        for field, value in scalar_expected.items():
            if review.get(field) != value:
                errors.append(f"{prefix}: {field} differs from batch")
        source = review.get("source_evidence", {})
        for field, value in {
            "record_sha256": queue_item["record_sha256"],
            "source_relation_sha256": queue_item["source_relation_sha256"],
            "queue_manifest_sha256": registry["queue_manifest_sha256"],
            "source_mode": "read_only",
        }.items():
            if source.get(field) != value:
                errors.append(f"{prefix}: source_evidence.{field} mismatch")
        expected_bundles = [expected_bundle_evidence(registry["bundles"][bundle_id]) for bundle_id in queue_item["linked_bundle_ids"]]
        if source.get("bundles") != expected_bundles:
            errors.append(f"{prefix}: bundle evidence does not cover the exact ordered source inventory")
        expected_external = [expected_external_evidence(target) for target in queue_item.get("external_targets", [])]
        if source.get("external_targets") != expected_external:
            errors.append(f"{prefix}: external target evidence mismatch")

        ordered_count, fenced_count = _expected_code_counts(queue_item, registry)
        code_review = review.get("code_review", {})
        if code_review.get("ordered_code_items_reviewed") != ordered_count:
            errors.append(f"{prefix}: standalone code review count mismatch")
        if code_review.get("fenced_code_items_reviewed") != fenced_count:
            errors.append(f"{prefix}: fenced code review count mismatch")
        if ordered_count + fenced_count == 0 and code_review.get("completeness") != "no_code":
            errors.append(f"{prefix}: no source code exists but completeness is not no_code")

        expected_hashes = _expected_figure_hashes(queue_item, registry)
        figure_context = review.get("figure_context_review", {})
        figure_reviews = figure_context.get("figures", []) if isinstance(figure_context, dict) else []
        reviewed_hashes = {str(item.get("image_sha256") or "") for item in figure_reviews}
        unknown_hashes = reviewed_hashes - expected_hashes
        if unknown_hashes:
            errors.append(f"{prefix}: figure hashes not present in linked evidence: {sorted(unknown_hashes)}")
        expected_unreviewed = max(0, len(expected_hashes) - len(reviewed_hashes & expected_hashes))
        if figure_context.get("unreviewed_figure_count") != expected_unreviewed:
            errors.append(f"{prefix}: unreviewed_figure_count {figure_context.get('unreviewed_figure_count')} != {expected_unreviewed}")
        if expected_hashes and not figure_reviews:
            errors.append(f"{prefix}: source figures exist but no representative figure review was recorded")
        if not expected_hashes and figure_context.get("status") != "not_applicable_no_source_figure":
            errors.append(f"{prefix}: no source figures exist; status must be not_applicable_no_source_figure")
        native_for_record = sum(1 for item in figure_reviews if item.get("review_scope") == "native_pixels")
        native_pixel_samples += native_for_record
        figure_context_samples += len(figure_reviews)
        if figure_context.get("status") == "native_sample_reviewed" and native_for_record == 0:
            errors.append(f"{prefix}: native_sample_reviewed lacks native_pixels evidence")
        if native_for_record and figure_context.get("status") != "native_sample_reviewed":
            errors.append(f"{prefix}: native_pixels evidence requires native_sample_reviewed status")

        stages = review.get("stage_evidence", [])
        if [item.get("stage") for item in stages] != REQUIRED_STAGES:
            errors.append(f"{prefix}: stage_evidence order/coverage mismatch")
        if review.get("review_state") == "COMPLETE" and any(item.get("status") == "blocked" for item in stages):
            errors.append(f"{prefix}: COMPLETE review contains a blocked stage")
        if require_complete and review.get("review_state") != "COMPLETE":
            errors.append(f"{prefix}: review is not COMPLETE")
        level = str(review.get("maturity") or "")
        maturity[level] = maturity.get(level, 0) + 1
        if level == "parse-verified":
            parse_verified_records += 1
            for error in _parse_verified_errors(index, review, queue_item, ordered_count + fenced_count, syntax_cache):
                errors.append(f"{prefix}: {error}")

    expected_ids = set(expected)
    if require_complete and seen != expected_ids:
        missing = sorted(expected_ids - seen)
        extra = sorted(seen - expected_ids)
        errors.append(f"batch coverage mismatch: missing={missing}, extra={extra}")
    return {
        "ok": not errors,
        "schema_version": "1.0",
        "batch_id": batch[0].get("batch_id") if batch else None,
        "batch_records": len(batch),
        "reviews": len(reviews),
        "unique_records": len(seen),
        "complete_records": sum(1 for row in reviews if row.get("review_state") == "COMPLETE"),
        "maturity": dict(sorted(maturity.items())),
        "figure_context_records": figure_context_samples,
        "native_pixel_samples_declared": native_pixel_samples,
        "parse_verified_records_checked": parse_verified_records,
        "review_files": {str(path): sha256_file(path) for path in review_paths},
        "scientific_boundary": "Record review completion does not imply fixture/data/native-reviewed maturity or execution authorization.",
        "errors": errors,
    }


def _positions(value: str | None) -> set[int] | None:
    if not value:
        return None
    result: set[int] = set()
    for token in value.split(","):
        token = token.strip()
        if "-" in token:
            left, right = token.split("-", 1)
            result.update(range(int(left), int(right) + 1))
        else:
            result.add(int(token))
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=Path, default=DEFAULT_INDEX)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--batch", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--positions", help="Comma-separated positions or ranges, e.g. 1-10,15")
    prepare.add_argument("--reviewer-id", required=True)
    validate = sub.add_parser("validate")
    validate.add_argument("--batch", type=Path, required=True)
    validate.add_argument("--reviews", type=Path, nargs="+", required=True)
    validate.add_argument("--require-complete", action="store_true")
    validate.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare_reviews(args.index.resolve(), args.batch.resolve(), args.output.resolve(), _positions(args.positions), args.reviewer_id)
        elif args.command == "validate":
            result = validate_reviews(args.index.resolve(), args.batch.resolve(), [path.resolve() for path in args.reviews], args.require_complete)
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                temporary = args.output.with_suffix(args.output.suffix + ".tmp")
                temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                os.replace(temporary, args.output)
        else:
            raise AssertionError(args.command)
    except (OSError, ValueError, json.JSONDecodeError, ReviewValidationError) as exc:
        result = {"ok": False, "errors": [str(exc)]}
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    sys.exit(main())
