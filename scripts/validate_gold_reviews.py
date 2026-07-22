#!/usr/bin/env python3
"""Validate manual reviews against the immutable 60-item corpus gold queue."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any


SKILL_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INDEX = SKILL_ROOT / "assets" / "private-corpus-index"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
ALLOWED_MATURITY = {"normalized", "parse-verified"}
REQUIRED_FIELDS = {
    "bundle_id", "title", "domain", "review_evidence", "research_question",
    "data_and_statistical_unit", "method_sequence", "combination_logic",
    "package_usage", "code_completeness", "code_figure_consistency",
    "assumptions", "scientific_risks", "alternatives", "validation_required",
    "claim_ceiling", "decision", "maturity",
}


class GoldReviewError(RuntimeError):
    """Raised for malformed review inputs."""


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise GoldReviewError(f"Expected JSON object at {path}:{line_number}")
            records.append(value)
    return records


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _valid_sha(value: Any) -> bool:
    return bool(SHA256.fullmatch(str(value or "")))


def _reviewed_figures(record: dict[str, Any]) -> list[dict[str, Any]]:
    evidence = record["review_evidence"]
    if isinstance(evidence.get("native_figures"), list):
        return [dict(item) for item in evidence["native_figures"] if isinstance(item, dict)]
    figure_id = evidence.get("native_reviewed_figure_id")
    if figure_id:
        return [
            {
                "figure_id": figure_id,
                "image_sha256": evidence.get("native_reviewed_figure_sha256"),
                "status": evidence.get("figure_review_scope", "native-reviewed"),
            }
        ]
    return []


def _unresolved(record: dict[str, Any], figures: list[dict[str, Any]]) -> bool:
    text = json.dumps(
        {
            "code_figure_consistency": record.get("code_figure_consistency"),
            "figures": figures,
            "figure_review_scope": record.get("review_evidence", {}).get("figure_review_scope"),
        },
        ensure_ascii=False,
    ).casefold()
    return any(token in text for token in ("unresolved", "reference_only", "reference only", "无法对应", "不能对应"))


def _parse_complete(record: dict[str, Any]) -> bool:
    parse_text = str(record.get("review_evidence", {}).get("parse_check", ""))
    fractions = [(int(left), int(right)) for left, right in re.findall(r"(?i)(?:parse|compile)\s+(\d+)\s*/\s*(\d+)", parse_text)]
    return bool(fractions) and all(total > 0 and passed == total for passed, total in fractions)


def validate_reviews(index_dir: Path) -> dict[str, Any]:
    index_dir = index_dir.resolve()
    review_dir = index_dir / "manual-review"
    gold = json.loads((index_dir / "gold-set.json").read_text(encoding="utf-8"))["selected"]
    batches = [review_dir / "gold-review-batch-a.jsonl", review_dir / "gold-review-batch-b.jsonl"]
    batch_records = [read_jsonl(path) for path in batches]
    reviews = batch_records[0] + batch_records[1]
    bundles = {record["bundle_id"]: record for record in read_jsonl(index_dir / "source-flow-bundles.jsonl")}
    figure_cards = {record["figure_card_id"]: record for record in read_jsonl(index_dir / "figure-cards.jsonl")}
    errors: list[str] = []

    expected_halves = ([item["bundle_id"] for item in gold[:30]], [item["bundle_id"] for item in gold[30:60]])
    for batch_index, records in enumerate(batch_records):
        ids = [record.get("bundle_id") for record in records]
        if ids != expected_halves[batch_index]:
            errors.append(f"batch-{batch_index + 1}: bundle order or coverage differs from gold-set")

    ids = [record.get("bundle_id") for record in reviews]
    if len(reviews) != 60:
        errors.append(f"expected 60 reviews, found {len(reviews)}")
    if len(set(ids)) != len(ids):
        errors.append("duplicate review bundle IDs")

    maturity = Counter()
    decisions = Counter()
    figure_status = Counter()
    unresolved = 0
    reviewed_code_items = 0
    for index, (expected, record) in enumerate(zip(gold, reviews), start=1):
        missing = sorted(REQUIRED_FIELDS - set(record))
        if missing:
            errors.append(f"review[{index}] missing fields: {missing}")
            continue
        bundle_id = str(record["bundle_id"])
        bundle = bundles.get(bundle_id)
        if bundle is None:
            errors.append(f"review[{index}] unknown bundle: {bundle_id}")
            continue
        if record["title"] != expected["title"] or record["domain"] != expected["domain"]:
            errors.append(f"review[{index}] title/domain differs from gold-set")
        evidence = record["review_evidence"]
        if not isinstance(evidence, dict) or not evidence:
            errors.append(f"review[{index}] evidence is empty")
            continue
        article_sha = evidence.get("article_sha256")
        if not _valid_sha(article_sha) or article_sha != bundle.get("article", {}).get("sha256"):
            errors.append(f"review[{index}] article hash mismatch")
        flow_sha = evidence.get("flow_fingerprint") or evidence.get("source_flow_fingerprint_sha256")
        if not _valid_sha(flow_sha) or flow_sha != bundle.get("flow_integrity", {}).get("flow_fingerprint_sha256"):
            errors.append(f"review[{index}] flow fingerprint mismatch")

        code_count = evidence.get("code_files_reviewed", evidence.get("reviewed_code_inventory_count"))
        if not isinstance(code_count, int) or code_count < 0:
            errors.append(f"review[{index}] invalid code inventory count")
        else:
            reviewed_code_items += code_count
            expected_count = len(bundle.get("ordered_code_files", []))
            if code_count != expected_count:
                errors.append(f"review[{index}] code inventory count {code_count} != {expected_count}")
        boundary_hashes = evidence.get("code_sha256_first_last")
        if boundary_hashes and bundle.get("ordered_code_files"):
            expected_hashes = [bundle["ordered_code_files"][0]["sha256"], bundle["ordered_code_files"][-1]["sha256"]]
            if list(boundary_hashes) != expected_hashes:
                errors.append(f"review[{index}] boundary code hashes mismatch")

        figures = _reviewed_figures(record)
        if not figures:
            errors.append(f"review[{index}] lacks native figure evidence")
        for figure in figures:
            figure_id = str(figure.get("figure_id", ""))
            source = figure_cards.get(figure_id)
            if source is None or source.get("source_bundle_id") != bundle_id:
                errors.append(f"review[{index}] invalid FigureCard relation: {figure_id}")
                continue
            declared_sha = figure.get("image_sha256")
            if declared_sha and declared_sha != source.get("sha256"):
                errors.append(f"review[{index}] figure hash mismatch: {figure_id}")
            figure_status[str(figure.get("status", "native-reviewed"))] += 1
        if _unresolved(record, figures):
            unresolved += 1

        level = str(record["maturity"])
        maturity[level] += 1
        if level not in ALLOWED_MATURITY:
            errors.append(f"review[{index}] invalid maturity: {level}")
        if level == "parse-verified" and not _parse_complete(record):
            errors.append(f"review[{index}] parse-verified without complete parse evidence")
        if not str(record["decision"]).strip():
            errors.append(f"review[{index}] empty decision")
        decisions[str(record["decision"])] += 1

    declared_figure_evidence = sum(figure_status.values())
    return {
        "ok": not errors,
        "schema_version": "1.0",
        "gold_target": len(gold),
        "reviews": len(reviews),
        "unique_bundle_ids": len(set(ids)),
        "reviewed_code_inventory_items": reviewed_code_items,
        "representative_figures_declared_reviewed": declared_figure_evidence,
        # Backward-compatible alias. The Gold60 files do not uniformly record a
        # named viewer, timestamp, or native-view evidence level, so this must
        # not be interpreted as a verified native-view count.
        "representative_figures_native_viewed": declared_figure_evidence,
        "deprecated_metrics": {
            "representative_figures_native_viewed": {
                "replacement": "representative_figures_declared_reviewed",
                "reason": "Legacy Gold60 evidence is declaration-level and does not satisfy the two-level native review contract.",
            }
        },
        "code_figure_unresolved_records": unresolved,
        "maturity": dict(sorted(maturity.items())),
        "figure_evidence_status": dict(sorted(figure_status.items())),
        "decision_count": len(decisions),
        "batch_sha256": {path.name: sha256_file(path) for path in batches if path.is_file()},
        "scientific_boundary": "Declared figure review evidence does not imply a verified native-view event, fixture/data verification, or workflow-level native review.",
        "errors": errors,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index-dir", type=Path, default=DEFAULT_INDEX)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = validate_reviews(args.index_dir)
    except (OSError, json.JSONDecodeError, GoldReviewError) as exc:
        report = {"ok": False, "errors": [str(exc)]}
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_name(args.output.name + ".tmp")
        temporary.write_text(rendered, encoding="utf-8")
        os.replace(temporary, args.output)
    else:
        print(rendered, end="")
    return 0 if report.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
