#!/usr/bin/env python3
"""Build and validate a stable, resumable review queue over high-value records.

The queue contains identifiers and evidence contracts only. It never embeds article
text, source code, images, installation commands, or private absolute locators.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Sequence


SKILL_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INDEX = SKILL_ROOT / "assets" / "private-corpus-index"
DOMAIN_ORDER = [
    "可视化图形",
    "单细胞分析",
    "空间转录组",
    "bulk组学分析",
    "R包或Python包工具",
    "统计与机器学习",
    "分析思路与文献解读",
    "流程与规范",
    "数据获取与数据库",
    "其他",
    "unknown",
]
MATURITY_ORDER = {
    "raw-extracted": 0,
    "normalized": 1,
    "parse-verified": 2,
    "fixture-verified": 3,
    "data-verified": 4,
    "native-reviewed": 5,
}


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    count = 0
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
            count += 1
    os.replace(temporary, path)
    return count


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def stable_id(prefix: str, *parts: str) -> str:
    return f"{prefix}-{hashlib.sha256(chr(31).join(parts).encode('utf-8')).hexdigest()[:20]}"


def load_manual_reviews(index: Path) -> tuple[dict[str, dict[str, Any]], list[dict[str, str]]]:
    review_dir = index / "manual-review"
    reviews: dict[str, dict[str, Any]] = {}
    sources: list[dict[str, str]] = []
    for path in sorted(review_dir.glob("gold-review-batch-*.jsonl")) if review_dir.exists() else []:
        sources.append({"file": path.name, "sha256": sha256_file(path)})
        for row in read_jsonl(path):
            bundle_id = str(row.get("bundle_id") or "")
            if not bundle_id or bundle_id in reviews:
                raise ValueError(f"Invalid or duplicate manual review bundle ID: {bundle_id!r}")
            reviews[bundle_id] = row
    return reviews, sources


def _resolve_receipt_review_file(review_dir: Path, raw_path: str) -> Path:
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = review_dir / candidate
    candidate = candidate.resolve()
    try:
        candidate.relative_to(review_dir.resolve())
    except ValueError as exc:
        raise ValueError(f"Receipt review file escapes manual-review directory: {raw_path}") from exc
    return candidate


def load_deep_reviews(index: Path) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Load only COMPLETE record reviews authenticated by successful receipts.

    A receipt is fail-closed: it must explicitly report ``ok=true`` and
    ``errors=[]``; every listed review file must exist beneath ``manual-review``
    and match the receipt SHA-256.  Review completion remains a scientific review
    state only and never authorizes execution.
    """

    review_dir = index / "manual-review"
    reviews: dict[str, dict[str, Any]] = {}
    sources: list[dict[str, Any]] = []
    receipt_paths = (
        sorted(
            path for path in review_dir.glob("high-value-review-batch-*-validation.json")
            if re.fullmatch(r"high-value-review-batch-\d+-validation\.json", path.name)
        )
        if review_dir.exists() else []
    )
    for receipt_path in receipt_paths:
        receipt = read_json(receipt_path)
        if receipt.get("ok") is not True or receipt.get("errors") != []:
            raise ValueError(f"Untrusted deep-review receipt status: {receipt_path.name}")
        review_files = receipt.get("review_files")
        if not isinstance(review_files, dict) or not review_files:
            raise ValueError(f"Deep-review receipt lacks review_files: {receipt_path.name}")
        receipt_complete = 0
        receipt_review_files: list[dict[str, str]] = []
        for raw_path, expected_sha256 in sorted(review_files.items()):
            review_path = _resolve_receipt_review_file(review_dir, str(raw_path))
            if not review_path.is_file():
                raise ValueError(f"Receipt review file missing: {review_path}")
            observed_sha256 = sha256_file(review_path)
            if observed_sha256 != str(expected_sha256):
                raise ValueError(
                    f"Receipt review hash mismatch for {review_path.name}: "
                    f"expected {expected_sha256}, observed {observed_sha256}"
                )
            receipt_review_files.append({"file": review_path.name, "sha256": observed_sha256})
            for row in read_jsonl(review_path):
                if row.get("review_state") != "COMPLETE":
                    continue
                record_id = str(row.get("preprocess_record_id") or "")
                if not record_id or record_id in reviews:
                    raise ValueError(f"Invalid or duplicate complete deep-review record ID: {record_id!r}")
                if row.get("decision", {}).get("automatic_execution_allowed") is not False:
                    raise ValueError(f"Deep review must prohibit automatic execution: {record_id}")
                maturity = str(row.get("maturity") or "")
                if maturity not in MATURITY_ORDER:
                    raise ValueError(f"Invalid deep-review maturity for {record_id}: {maturity!r}")
                reviews[record_id] = row
                receipt_complete += 1
        if receipt.get("complete_records") != receipt_complete:
            raise ValueError(
                f"Deep-review receipt complete count mismatch for {receipt_path.name}: "
                f"declared {receipt.get('complete_records')}, loaded {receipt_complete}"
            )
        sources.append({
            "file": receipt_path.name,
            "sha256": sha256_file(receipt_path),
            "complete_records": receipt_complete,
            "review_files": receipt_review_files,
        })
    return reviews, sources


def verify_deep_review_source(
    review: dict[str, Any], registry_row: dict[str, Any], crosswalk_row: dict[str, Any]
) -> None:
    record_id = str(crosswalk_row["preprocess_record_id"])
    evidence = review.get("source_evidence", {})
    if evidence.get("record_sha256") != registry_row.get("record_sha256"):
        raise ValueError(f"Stale deep-review record hash: {record_id}")
    expected_relation_sha256 = canonical_sha256(crosswalk_row.get("relations", []))
    if evidence.get("source_relation_sha256") != expected_relation_sha256:
        raise ValueError(f"Stale deep-review source relation: {record_id}")


def batch_sort_key(batch_id: str | None, batch_position: int | None, batch_size: int) -> tuple[int, int]:
    match = re.fullmatch(r"high-value-(\d+)", str(batch_id or ""))
    if not match or not isinstance(batch_position, int) or batch_position < 1:
        raise ValueError(f"Invalid historical batch assignment: {batch_id!r}/{batch_position!r}")
    return int(match.group(1)), batch_position


def batch_ordinal(batch_id: str | None, batch_position: int | None, batch_size: int) -> int:
    batch_number, position = batch_sort_key(batch_id, batch_position, batch_size)
    if position > batch_size:
        raise ValueError(f"Historical batch position exceeds batch size: {batch_id}/{position}")
    return (batch_number - 1) * batch_size + position


def highest_review_maturity(reviews: Sequence[dict[str, Any]]) -> str:
    allowed = [str(review.get("maturity") or "raw-extracted") for review in reviews]
    return max(allowed, key=lambda value: MATURITY_ORDER.get(value, -1), default="raw-extracted")


def review_priority(item: dict[str, Any]) -> tuple[int, int, int, int, str]:
    completeness = str(item["code_asset_completeness"])
    completeness_score = 4 if "完整" in completeness else 3 if "多脚本" in completeness else 2 if "单脚本" in completeness else 1 if "片段" in completeness else 0
    mapping_score = 3 if not item["relation_verified"] else 2 if item["review_state"] == "PARTIAL_GOLD_REVIEW" else 1
    return (
        mapping_score,
        completeness_score,
        min(int(item["code_inventory_count"]), 50),
        min(int(item["figure_inventory_count"]), 20),
        item["preprocess_record_id"],
    )


def stratified_pending_order(items: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        grouped[item["category"]].append(item)
    for values in grouped.values():
        values.sort(key=review_priority, reverse=True)
    ordered: list[dict[str, Any]] = []
    domains = DOMAIN_ORDER + sorted(set(grouped) - set(DOMAIN_ORDER))
    while any(grouped.values()):
        for domain in domains:
            if grouped.get(domain):
                ordered.append(grouped[domain].pop(0))
    return ordered


def build_queue(index: Path, batch_size: int = 30) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    registry = {row["preprocess_record_id"]: row for row in read_jsonl(index / "preprocessing-records.jsonl")}
    crosswalk = [row for row in read_jsonl(index / "preprocessing-crosswalk.jsonl") if row.get("distillation_value") == "高"]
    bundles = {row["bundle_id"]: row for row in read_jsonl(index / "source-flow-bundles.jsonl")}
    reviews, review_sources = load_manual_reviews(index)
    deep_reviews, deep_review_sources = load_deep_reviews(index)
    existing_queue_path = index / "distillation-review-queue.jsonl"
    existing_manifest_path = index / "distillation-review-manifest.json"
    existing_rows = read_jsonl(existing_queue_path) if existing_queue_path.exists() else []
    existing_by_id: dict[str, dict[str, Any]] = {}
    historical_assignments: set[tuple[str, int]] = set()
    historical_max_ordinal = 0
    if existing_rows:
        if existing_manifest_path.exists():
            existing_manifest = read_json(existing_manifest_path)
            if existing_manifest.get("batch_size") != batch_size:
                raise ValueError(
                    f"Refusing to reinterpret historical batches with batch_size={batch_size}; "
                    f"existing batch_size={existing_manifest.get('batch_size')}"
                )
        for existing in existing_rows:
            record_id = str(existing.get("preprocess_record_id") or "")
            if not record_id or record_id in existing_by_id:
                raise ValueError(f"Invalid or duplicate existing queue record ID: {record_id!r}")
            existing_by_id[record_id] = existing
            if existing.get("batch_id") is not None or existing.get("batch_position") is not None:
                ordinal = batch_ordinal(existing.get("batch_id"), existing.get("batch_position"), batch_size)
                assignment = (str(existing["batch_id"]), int(existing["batch_position"]))
                if assignment in historical_assignments:
                    raise ValueError(f"Duplicate historical batch assignment: {assignment}")
                historical_assignments.add(assignment)
                historical_max_ordinal = max(historical_max_ordinal, ordinal)
    items: list[dict[str, Any]] = []
    for row in crosswalk:
        record_id = row["preprocess_record_id"]
        registry_row = registry[record_id]
        linked_bundle_ids = sorted({
            relation["bundle_id"]
            for relation in row.get("relations", [])
            if relation.get("bundle_id")
        })
        linked_reviews = [reviews[bundle_id] for bundle_id in linked_bundle_ids if bundle_id in reviews]
        all_linked_reviewed = bool(linked_bundle_ids) and len(linked_reviews) == len(linked_bundle_ids)
        if all_linked_reviewed:
            review_state = "COMPLETE_GOLD_REVIEW"
        elif linked_reviews:
            review_state = "PARTIAL_GOLD_REVIEW"
        else:
            review_state = "QUEUED"
        deep_review = deep_reviews.get(record_id)
        if deep_review:
            verify_deep_review_source(deep_review, registry_row, row)
            review_state = "COMPLETE_DEEP_REVIEW"
        code_count = sum(
            len(bundles[bundle_id].get("ordered_code_files", []))
            + len(bundles[bundle_id].get("article", {}).get("fenced_code_blocks", []))
            for bundle_id in linked_bundle_ids
            if bundle_id in bundles
        )
        figure_count = sum(
            len(bundles[bundle_id].get("images", []))
            for bundle_id in linked_bundle_ids
            if bundle_id in bundles
        )
        action_required = review_state not in {"COMPLETE_GOLD_REVIEW", "COMPLETE_DEEP_REVIEW"}
        historical = existing_by_id.get(record_id, {})
        historical_batch_id = historical.get("batch_id")
        historical_batch_position = historical.get("batch_position")
        if review_state == "COMPLETE_GOLD_REVIEW":
            # Gold coverage remains unbatched.  Its completion is bundle-based and
            # must not acquire or retain a pending batch assignment.
            historical_batch_id = None
            historical_batch_position = None
        elif review_state == "COMPLETE_DEEP_REVIEW":
            review_batch_id = deep_review.get("batch_id")
            review_batch_position = deep_review.get("batch_position")
            if historical_batch_id is not None:
                if (historical_batch_id, historical_batch_position) != (review_batch_id, review_batch_position):
                    raise ValueError(f"Deep review batch assignment changed for {record_id}")
            else:
                historical_batch_id = review_batch_id
                historical_batch_position = review_batch_position
            batch_ordinal(historical_batch_id, historical_batch_position, batch_size)
        item = {
            "schema_version": "1.0",
            "queue_item_id": stable_id("distill-review", record_id, "v1"),
            "preprocess_record_id": record_id,
            "record_sha256": registry_row["record_sha256"],
            "record_title": row["record_title"],
            "category": row["category"],
            "code_asset_completeness": row["code_asset_completeness"],
            "crosswalk_status": row["status"],
            "relation_verified": bool(row["relation_verified"]),
            "source_relation_sha256": canonical_sha256(row.get("relations", [])),
            "linked_bundle_ids": linked_bundle_ids,
            "external_targets": [
                {
                    "target_skill": relation.get("target_skill"),
                    "target_relative_path": relation.get("target_relative_path"),
                    "sha256": relation.get("sha256"),
                }
                for relation in row.get("relations", [])
                if relation.get("target_relative_path")
            ],
            "code_inventory_count": code_count,
            "figure_inventory_count": figure_count,
            "manual_reviewed_bundle_ids": sorted(bundle_id for bundle_id in linked_bundle_ids if bundle_id in reviews),
            "review_state": review_state,
            "action_required": action_required,
            "required_stages": [
                "SOURCE_VALIDATED",
                "METHOD_REVIEWED",
                "CODE_REVIEWED",
                "FIGURE_CONTEXT_REVIEWED",
                "SCIENTIFIC_QA",
                "COMPLETE",
            ],
            "batch_id": historical_batch_id,
            "batch_position": historical_batch_position,
            "maturity": (
                str(deep_review["maturity"])
                if deep_review
                else highest_review_maturity(linked_reviews) if all_linked_reviewed else "raw-extracted"
            ),
            "manual_deep_review_id": str(deep_review.get("review_id")) if deep_review else None,
            "automatic_execution_allowed": False,
            "claim_boundary": "Review completion does not imply fixture/data verification or executable eligibility.",
        }
        items.append(item)
    unassigned_pending = stratified_pending_order([
        item for item in items if item["action_required"] and item.get("batch_id") is None
    ])
    preserved_ordinals = [
        batch_ordinal(item.get("batch_id"), item.get("batch_position"), batch_size)
        for item in items
        if item.get("batch_id") is not None
    ]
    next_ordinal = max([historical_max_ordinal, *preserved_ordinals])
    for item in unassigned_pending:
        next_ordinal += 1
        item["batch_id"] = f"high-value-{(next_ordinal - 1) // batch_size + 1:03d}"
        item["batch_position"] = (next_ordinal - 1) % batch_size + 1
    assigned = [item for item in items if item.get("batch_id") is not None]
    assignment_pairs = [(str(item["batch_id"]), int(item["batch_position"])) for item in assigned]
    if len(assignment_pairs) != len(set(assignment_pairs)):
        raise ValueError("Duplicate batch assignment after queue overlay")
    assigned.sort(key=lambda item: (*batch_sort_key(item["batch_id"], item["batch_position"], batch_size), item["preprocess_record_id"]))
    unbatched = sorted([item for item in items if item.get("batch_id") is None], key=lambda value: value["preprocess_record_id"])
    ordered = assigned + unbatched
    queue_sha256 = canonical_sha256(ordered)
    state_counts = Counter(item["review_state"] for item in ordered)
    domain_counts = Counter(item["category"] for item in ordered if item["action_required"])
    batch_counts = Counter(item["batch_id"] for item in ordered if item["batch_id"])
    pending_batch_counts = Counter(item["batch_id"] for item in ordered if item["batch_id"] and item["action_required"])
    completed_batch_counts = Counter(item["batch_id"] for item in ordered if item["batch_id"] and not item["action_required"])
    manifest = {
        "schema_version": "1.0",
        "generated_at": utc_now(),
        "queue_sha256": queue_sha256,
        "queue_version": "high-value-v1",
        "batch_size": batch_size,
        "records": len(ordered),
        "unique_record_ids": len({item["preprocess_record_id"] for item in ordered}),
        "state_counts": dict(sorted(state_counts.items())),
        "pending_by_domain": dict(sorted(domain_counts.items())),
        "batches": dict(sorted(batch_counts.items())),
        "pending_batches": dict(sorted(pending_batch_counts.items())),
        "completed_batches": dict(sorted(completed_batch_counts.items())),
        "manual_review_sources": review_sources,
        "deep_review_receipts": deep_review_sources,
        "source_hashes": {
            "preprocessing_records": sha256_file(index / "preprocessing-records.jsonl"),
            "preprocessing_crosswalk": sha256_file(index / "preprocessing-crosswalk.jsonl"),
            "source_flow_bundles": sha256_file(index / "source-flow-bundles.jsonl"),
        },
        "state_machine": [
            "QUEUED",
            "SOURCE_VALIDATED",
            "METHOD_REVIEWED",
            "CODE_REVIEWED",
            "FIGURE_CONTEXT_REVIEWED",
            "SCIENTIFIC_QA",
            "COMPLETE",
        ],
        "terminal_review_states": ["COMPLETE_GOLD_REVIEW", "COMPLETE_DEEP_REVIEW"],
        "scientific_boundary": "Queue state is review workflow evidence only; maturity promotion requires the separate maturity contract.",
    }
    return ordered, manifest


def build_and_write(index: Path, batch_size: int) -> dict[str, Any]:
    rows, manifest = build_queue(index, batch_size)
    write_jsonl(index / "distillation-review-queue.jsonl", rows)
    write_json(index / "distillation-review-manifest.json", manifest)
    ledger = index / "manual-review" / "distillation-review-events.jsonl"
    if not ledger.exists():
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text("", encoding="utf-8")
    return {"ok": True, **manifest}


def validate_queue(index: Path) -> dict[str, Any]:
    queue_path = index / "distillation-review-queue.jsonl"
    manifest_path = index / "distillation-review-manifest.json"
    errors: list[str] = []
    if not queue_path.exists() or not manifest_path.exists():
        return {"ok": False, "errors": ["queue or manifest missing"]}
    rows = read_jsonl(queue_path)
    manifest = read_json(manifest_path)
    registry = {row["preprocess_record_id"]: row for row in read_jsonl(index / "preprocessing-records.jsonl")}
    crosswalk = {
        row["preprocess_record_id"]: row
        for row in read_jsonl(index / "preprocessing-crosswalk.jsonl")
        if row.get("distillation_value") == "高"
    }
    try:
        deep_reviews, deep_review_sources = load_deep_reviews(index)
    except ValueError as exc:
        deep_reviews = {}
        deep_review_sources = []
        errors.append(f"invalid deep-review receipt: {exc}")
    ids = [row.get("preprocess_record_id") for row in rows]
    if len(ids) != len(set(ids)):
        errors.append("duplicate preprocess_record_id in queue")
    if set(ids) != set(crosswalk):
        errors.append("queue does not cover the complete high-value crosswalk")
    if manifest.get("records") != len(rows):
        errors.append("manifest record count differs from queue")
    if manifest.get("queue_sha256") != canonical_sha256(rows):
        errors.append("queue SHA-256 differs from manifest")
    if manifest.get("deep_review_receipts") != deep_review_sources:
        errors.append("deep-review receipt hashes differ from manifest")
    pending_batch_counts = Counter(
        row.get("batch_id") for row in rows if row.get("batch_id") and row.get("action_required")
    )
    completed_batch_counts = Counter(
        row.get("batch_id") for row in rows if row.get("batch_id") and not row.get("action_required")
    )
    if manifest.get("pending_batches") != dict(sorted(pending_batch_counts.items())):
        errors.append("manifest pending batch counts differ from queue")
    if manifest.get("completed_batches") != dict(sorted(completed_batch_counts.items())):
        errors.append("manifest completed batch counts differ from queue")
    seen_assignments: set[tuple[str, int]] = set()
    for row in rows:
        record_id = row["preprocess_record_id"]
        current = crosswalk.get(record_id)
        if current and row.get("source_relation_sha256") != canonical_sha256(current.get("relations", [])):
            errors.append(f"stale source relation for {record_id}")
        if record_id in registry and row.get("record_sha256") != registry[record_id].get("record_sha256"):
            errors.append(f"stale record hash for {record_id}")
        if row.get("automatic_execution_allowed") is not False:
            errors.append(f"automatic execution not prohibited: {record_id}")
        if row.get("action_required") and not row.get("batch_id"):
            errors.append(f"pending row lacks batch: {record_id}")
        if row.get("batch_id") is not None:
            try:
                batch_sort_key(row.get("batch_id"), row.get("batch_position"), int(manifest.get("batch_size", 0)))
                assignment = (str(row["batch_id"]), int(row["batch_position"]))
                if assignment in seen_assignments:
                    errors.append(f"duplicate batch assignment: {assignment}")
                seen_assignments.add(assignment)
            except (TypeError, ValueError) as exc:
                errors.append(f"invalid batch assignment for {record_id}: {exc}")
        if not row.get("action_required") and row.get("batch_id") and row.get("review_state") != "COMPLETE_DEEP_REVIEW":
            errors.append(f"non-deep complete row assigned to batch: {record_id}")
        if row.get("review_state") == "COMPLETE_DEEP_REVIEW":
            review = deep_reviews.get(record_id)
            if not review:
                errors.append(f"deep-review queue row lacks trusted review: {record_id}")
            else:
                try:
                    verify_deep_review_source(review, registry[record_id], current)
                except (KeyError, TypeError, ValueError) as exc:
                    errors.append(str(exc))
                if row.get("action_required"):
                    errors.append(f"deep-review completion still requires action: {record_id}")
                if row.get("maturity") != review.get("maturity"):
                    errors.append(f"deep-review maturity mismatch: {record_id}")
                if (row.get("batch_id"), row.get("batch_position")) != (
                    review.get("batch_id"), review.get("batch_position")
                ):
                    errors.append(f"deep-review historical batch changed: {record_id}")
        elif record_id in deep_reviews:
            errors.append(f"trusted deep review not overlaid: {record_id}")
        if row.get("maturity") not in MATURITY_ORDER:
            errors.append(f"invalid maturity: {row.get('maturity')}")
    source_hashes = manifest.get("source_hashes", {})
    for key, filename in {
        "preprocessing_records": "preprocessing-records.jsonl",
        "preprocessing_crosswalk": "preprocessing-crosswalk.jsonl",
        "source_flow_bundles": "source-flow-bundles.jsonl",
    }.items():
        if source_hashes.get(key) != sha256_file(index / filename):
            errors.append(f"source hash changed: {filename}")
    return {
        "ok": not errors,
        "records_checked": len(rows),
        "batches_checked": len({row.get("batch_id") for row in rows if row.get("batch_id")}),
        "deep_review_records": sum(row.get("review_state") == "COMPLETE_DEEP_REVIEW" for row in rows),
        "pending_records": sum(bool(row.get("action_required")) for row in rows),
        "errors": errors,
    }


def materialize_batch(index: Path, batch_id: str, output: Path) -> dict[str, Any]:
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite batch file: {output}")
    rows = [row for row in read_jsonl(index / "distillation-review-queue.jsonl") if row.get("batch_id") == batch_id]
    if not rows:
        raise ValueError(f"Unknown or empty batch: {batch_id}")
    write_jsonl(output, rows)
    receipt = {
        "ok": True,
        "batch_id": batch_id,
        "items": len(rows),
        "batch_sha256": sha256_file(output),
        "queue_manifest_sha256": sha256_file(index / "distillation-review-manifest.json"),
        "output": str(output),
    }
    write_json(output.with_suffix(output.suffix + ".receipt.json"), receipt)
    return receipt


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=Path, default=DEFAULT_INDEX)
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build")
    build.add_argument("--batch-size", type=int, default=30)
    sub.add_parser("validate")
    batch = sub.add_parser("materialize-batch")
    batch.add_argument("--batch-id", required=True)
    batch.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    index = args.index.resolve()
    if args.command == "build":
        result = build_and_write(index, args.batch_size)
    elif args.command == "validate":
        result = validate_queue(index)
    elif args.command == "materialize-batch":
        result = materialize_batch(index, args.batch_id, args.output.resolve())
    else:
        raise AssertionError(args.command)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
