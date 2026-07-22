#!/usr/bin/env python3
"""Materialize and validate manual native-figure review evidence.

Generated queues remain immutable metadata. This script accepts a separately authored
observation file produced after actual ``view_image`` inspection, verifies source and
code hashes, and writes review records distinct from the pending queues.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Sequence

from jsonschema import Draft202012Validator, FormatChecker


SKILL_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INDEX = SKILL_ROOT / "assets" / "private-corpus-index"
DEFAULT_OBSERVATIONS = DEFAULT_INDEX / "manual-review" / "native-figure-review-batch-001-observations.json"
PIXEL_SCHEMA = SKILL_ROOT / "references" / "native-review-schemas" / "pixel-artifact-review.schema.json"
CONTEXT_SCHEMA = SKILL_ROOT / "references" / "native-review-schemas" / "figure-context-review.schema.json"


class NativeReviewError(RuntimeError):
    """Raised when manual review evidence violates source or status contracts."""


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    if path.exists():
        raise FileExistsError(f"Refusing to overwrite manual review evidence: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    count = 0
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
            count += 1
    os.replace(temporary, path)
    return count


def write_json(path: Path, value: Any, *, overwrite: bool = False) -> None:
    if path.exists() and not overwrite:
        raise FileExistsError(f"Refusing to overwrite manual review evidence: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def schema_errors(schema_path: Path, record: dict[str, Any]) -> list[str]:
    validator = Draft202012Validator(read_json(schema_path), format_checker=FormatChecker())
    return [
        f"{'.'.join(str(part) for part in error.absolute_path) or '<root>'}: {error.message}"
        for error in sorted(validator.iter_errors(record), key=lambda item: list(item.absolute_path))
    ]


def load_sources(index: Path) -> dict[str, Any]:
    review_root = index / "native-figure-review"
    return {
        "batch": read_json(review_root / "native-figure-review-batch-001.json"),
        "pixels": {row["pixel_review_id"]: row for row in read_jsonl(review_root / "pixel-artifact-review-queue.jsonl")},
        "contexts": {row["figure_id"]: row for row in read_jsonl(review_root / "figure-context-review-queue.jsonl")},
        "figures": {row["figure_card_id"]: row for row in read_jsonl(index / "figure-cards.jsonl")},
        "bundles": {row["bundle_id"]: row for row in read_jsonl(index / "source-flow-bundles.jsonl")},
    }


def _code_hashes(bundle: dict[str, Any]) -> tuple[set[str], dict[str, tuple[int, int]]]:
    hashes = {str(item.get("sha256")) for item in bundle.get("ordered_code_files", []) if item.get("sha256")}
    spans: dict[str, tuple[int, int]] = {}
    for item in bundle.get("article", {}).get("fenced_code_blocks", []):
        sha = str(item.get("sha256") or "")
        if sha:
            hashes.add(sha)
            span = item.get("source_span", [0, 0])
            spans[sha] = (int(span[0]), int(span[1]))
    return hashes, spans


def _validate_observation(observation: dict[str, Any], batch_item: dict[str, Any], sources: dict[str, Any]) -> None:
    figure_id = batch_item["figure_id"]
    if observation.get("position") != batch_item["position"] or observation.get("figure_id") != figure_id:
        raise NativeReviewError(f"Observation order/identity mismatch at position {batch_item['position']}")
    figure = sources["figures"].get(figure_id)
    context = sources["contexts"].get(figure_id)
    if not figure or not context:
        raise NativeReviewError(f"Unknown FigureCard/context: {figure_id}")
    if observation.get("pixel_sha256") != figure.get("sha256") or observation.get("pixel_sha256") != context.get("pixel_sha256"):
        raise NativeReviewError(f"Pixel hash mismatch: {figure_id}")
    source_path = Path(str(figure.get("private_source_locator") or ""))
    if not source_path.is_file() or sha256_file(source_path) != observation["pixel_sha256"]:
        raise NativeReviewError(f"Native source image missing or changed: {figure_id}")
    required_arrays = ("visual_findings", "directly_visible", "supported_claims", "unsupported_claims")
    for field in required_arrays:
        if not isinstance(observation.get(field), list) or not observation[field] or not all(str(value).strip() for value in observation[field]):
            raise NativeReviewError(f"{figure_id}: {field} must be a non-empty string array")
    status = observation.get("code_binding_status")
    generator = observation.get("generator_code_sha256")
    bundle = sources["bundles"][context["source_bundle_id"]]
    hashes, spans = _code_hashes(bundle)
    if status in {"confirmed", "inferred"}:
        if generator not in hashes:
            raise NativeReviewError(f"{figure_id}: generator code hash is not in the linked bundle")
        if status == "confirmed":
            image_line = int((context.get("markdown_image_reference") or {}).get("source_line") or 0)
            span = spans.get(str(generator))
            if not span or not (0 < image_line - span[1] <= 15):
                raise NativeReviewError(f"{figure_id}: confirmed binding is not an adjacent fenced code block")
    elif status == "not_applicable":
        if generator is not None or context["code_candidates"]["total"] != 0:
            raise NativeReviewError(f"{figure_id}: code binding cannot be not_applicable")
    elif status == "unresolved":
        if generator is not None:
            raise NativeReviewError(f"{figure_id}: unresolved code binding must not name a generator")
    else:
        raise NativeReviewError(f"{figure_id}: unsupported manual code binding status {status!r}")
    if not str(observation.get("code_binding_note") or "").strip():
        raise NativeReviewError(f"{figure_id}: code binding note is required")
    applicability = observation.get("reproduction_applicability")
    reason = observation.get("reproduction_not_applicable_reason")
    if applicability == "not_applicable" and not str(reason or "").strip():
        raise NativeReviewError(f"{figure_id}: reproduction not-applicable reason is required")
    if applicability != "not_applicable" and reason is not None:
        raise NativeReviewError(f"{figure_id}: reproduction reason must be null unless not_applicable")


def build_records(index: Path, observations_path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    sources = load_sources(index)
    observations = read_json(observations_path)
    batch = sources["batch"]
    if observations.get("batch_id") != batch.get("batch_id") or observations.get("viewer") != "codex_view_image":
        raise NativeReviewError("Observation batch/viewer contract mismatch")
    items = observations.get("items", [])
    if len(items) != len(batch.get("items", [])):
        raise NativeReviewError("Observation coverage differs from fixed batch")
    reviewer = str(observations.get("reviewer") or "")
    reviewed_at = str(observations.get("reviewed_at") or "")
    pixel_rows: list[dict[str, Any]] = []
    context_rows: list[dict[str, Any]] = []
    seen_pixels: set[str] = set()
    for observation, batch_item in zip(items, batch["items"]):
        _validate_observation(observation, batch_item, sources)
        figure_id = batch_item["figure_id"]
        figure = sources["figures"][figure_id]
        queued_context = sources["contexts"][figure_id]
        pixel_id = queued_context["pixel_review_id"]
        if pixel_id not in seen_pixels:
            queued_pixel = dict(sources["pixels"][pixel_id])
            queued_pixel.update({
                "inspection_status": "native_reviewed",
                "reviewer": reviewer,
                "reviewed_at": reviewed_at,
                "native_review_evidence": [
                    {
                        "evidence_type": "native_view_image",
                        "detail": f"Opened exact local source with codex view_image: {figure['private_source_locator']}",
                        "artifact_sha256": observation["pixel_sha256"],
                    },
                    {
                        "evidence_type": "checksum",
                        "detail": "Source file SHA-256 matched the FigureCard and pixel-review identity at materialization.",
                        "artifact_sha256": observation["pixel_sha256"],
                    },
                ],
                "visual_findings": observation["visual_findings"],
                "not_applicable_reason": None,
            })
            pixel_rows.append(queued_pixel)
            seen_pixels.add(pixel_id)
        code_status = observation["code_binding_status"]
        generator = observation["generator_code_sha256"]
        evidence = [
            {
                "evidence_type": "native_view_image",
                "detail": "Exact FigureCard pixel payload opened with codex view_image.",
                "artifact_sha256": observation["pixel_sha256"],
            },
            {
                "evidence_type": "pixel_review",
                "detail": f"Linked to completed unique-pixel review {pixel_id}.",
                "artifact_sha256": observation["pixel_sha256"],
            },
            {
                "evidence_type": "code_context",
                "detail": observation["code_binding_note"],
                "artifact_sha256": generator,
            },
        ]
        queued_context = dict(queued_context)
        queued_context.update({
            "code_binding_status": code_status,
            "generator_code_sha256": generator,
            "code_binding_not_applicable_reason": observation["code_binding_note"] if code_status == "not_applicable" else None,
            "native_review_status": "native_reviewed",
            "native_review_not_applicable_reason": None,
            "reviewer": reviewer,
            "reviewed_at": reviewed_at,
            "native_review_evidence": evidence,
            "reproduction_applicability": observation["reproduction_applicability"],
            "reproduction_not_applicable_reason": observation["reproduction_not_applicable_reason"],
            "semantic_review_status": "reviewed",
            "directly_visible": observation["directly_visible"],
            "supported_claims": observation["supported_claims"],
            "unsupported_claims": observation["unsupported_claims"],
            "claim_boundary": observations["claim_boundary"],
        })
        context_rows.append(queued_context)
    return pixel_rows, context_rows


def validate_records(index: Path, observations_path: Path, pixel_path: Path, context_path: Path) -> dict[str, Any]:
    expected_pixels, expected_contexts = build_records(index, observations_path)
    actual_pixels = read_jsonl(pixel_path)
    actual_contexts = read_jsonl(context_path)
    errors: list[str] = []
    if actual_pixels != expected_pixels:
        errors.append("pixel review file differs from source-bound materialization")
    if actual_contexts != expected_contexts:
        errors.append("context review file differs from source-bound materialization")
    for index_value, row in enumerate(actual_pixels, start=1):
        errors.extend(f"pixel[{index_value}] {error}" for error in schema_errors(PIXEL_SCHEMA, row))
    for index_value, row in enumerate(actual_contexts, start=1):
        errors.extend(f"context[{index_value}] {error}" for error in schema_errors(CONTEXT_SCHEMA, row))
    return {
        "ok": not errors,
        "schema_version": "1.0",
        "batch_id": "native-figure-review-001",
        "pixel_reviews": len(actual_pixels),
        "context_reviews": len(actual_contexts),
        "native_reviewed_pixels": sum(row.get("inspection_status") == "native_reviewed" for row in actual_pixels),
        "native_reviewed_contexts": sum(row.get("native_review_status") == "native_reviewed" for row in actual_contexts),
        "code_binding_status": dict(sorted(Counter(row.get("code_binding_status") for row in actual_contexts).items())),
        "reproduction_applicability": dict(sorted(Counter(row.get("reproduction_applicability") for row in actual_contexts).items())),
        "pixel_file_sha256": sha256_file(pixel_path),
        "context_file_sha256": sha256_file(context_path),
        "observations_sha256": sha256_file(observations_path),
        "claim_boundary": "Native viewing and contextual review do not imply source-code execution, data verification, statistical validity, causality or clinical utility.",
        "errors": errors,
    }


def materialize(index: Path, observations: Path, output_dir: Path) -> dict[str, Any]:
    pixel_rows, context_rows = build_records(index, observations)
    pixel_path = output_dir / "native-figure-review-batch-001-pixels.jsonl"
    context_path = output_dir / "native-figure-review-batch-001-contexts.jsonl"
    write_jsonl(pixel_path, pixel_rows)
    write_jsonl(context_path, context_rows)
    report = validate_records(index, observations, pixel_path, context_path)
    write_json(output_dir / "native-figure-review-batch-001-validation.json", report)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=Path, default=DEFAULT_INDEX)
    parser.add_argument("--observations", type=Path, default=DEFAULT_OBSERVATIONS)
    sub = parser.add_subparsers(dest="command", required=True)
    materialize_parser = sub.add_parser("materialize")
    materialize_parser.add_argument("--output-dir", type=Path, required=True)
    validate_parser = sub.add_parser("validate")
    validate_parser.add_argument("--pixel-reviews", type=Path, required=True)
    validate_parser.add_argument("--context-reviews", type=Path, required=True)
    validate_parser.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "materialize":
            result = materialize(args.index.resolve(), args.observations.resolve(), args.output_dir.resolve())
        else:
            result = validate_records(
                args.index.resolve(),
                args.observations.resolve(),
                args.pixel_reviews.resolve(),
                args.context_reviews.resolve(),
            )
            if args.output:
                write_json(args.output.resolve(), result, overwrite=True)
    except (OSError, ValueError, json.JSONDecodeError, NativeReviewError) as exc:
        result = {"ok": False, "errors": [str(exc)]}
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    sys.exit(main())
