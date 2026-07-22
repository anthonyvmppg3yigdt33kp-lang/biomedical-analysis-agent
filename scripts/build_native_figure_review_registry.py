#!/usr/bin/env python3
"""Build and validate two-level native figure review queues.

The builder is metadata-only. It never opens an image and therefore never emits
``native_reviewed`` in a generated queue. Existing Gold60 statements are kept as
untrusted legacy declarations instead of being promoted into the new model.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from collections import defaultdict
from pathlib import Path
from typing import Any


SKILL_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INDEX = SKILL_ROOT / "assets" / "private-corpus-index"
DEFAULT_OUTPUT = DEFAULT_INDEX / "native-figure-review"
SHA256 = re.compile(r"^[0-9a-f]{64}$")

FIRST_BATCH = [
    ("figure-064aaa4b71a60db04897", "legacy_code_hash_declared"),
    ("figure-a5dcfe91ac48d6ea5c62", "legacy_code_hash_declared"),
    ("figure-94ec9ac343c7989b320a", "legacy_code_hash_declared"),
    ("figure-316c1c01b714cb2b4362", "legacy_code_hash_declared"),
    ("figure-0b40fc63e58d4efb3e42", "legacy_code_hash_declared"),
    ("figure-147cf8b11068035d111e", "legacy_code_hash_declared"),
    ("figure-16b9f5b093879f20478e", "code_binding_required"),
    ("figure-1778db69e4b2326dda99", "code_binding_required"),
    ("figure-2b539d0b0087c65e06ba", "code_binding_required"),
    ("figure-037aff17f9981490518d", "code_binding_required"),
    ("figure-c5ae8d0f1c29d31d7e19", "code_binding_required"),
    ("figure-171ddd9545865b8c6b3b", "code_binding_required"),
    ("figure-5c63bb686097d80e6a33", "code_binding_required"),
    ("figure-6e386a616904cf6f5091", "code_binding_required"),
    ("figure-1f2cf7bfc553649e1ac9", "negative_calibration"),
    ("figure-2712dc788bacd03f2588", "negative_calibration"),
]

PIXEL_CLAIM_BOUNDARY = (
    "Pixel review establishes visible raster content and legibility only; it does not validate "
    "generating code, numerical results, statistical semantics, or scientific claims."
)
CONTEXT_CLAIM_BOUNDARY = (
    "A FigureContextReview remains non-executable evidence until code binding, statistical mapping, "
    "and source assumptions are reviewed; legacy declarations are not new native-view evidence."
)


class NativeFigureRegistryError(RuntimeError):
    """Raised when source metadata or a generated registry is inconsistent."""


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise NativeFigureRegistryError(f"Expected object at {path}:{line_number}")
            records.append(value)
    return records


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_id(prefix: str, identity: str) -> str:
    digest = hashlib.sha256(f"native-figure-review-v1:{prefix}:{identity}".encode("utf-8")).hexdigest()
    return f"{prefix}-{digest[:20]}"


def pixel_review_id(pixel_sha256: str) -> str:
    return stable_id("pixel-review", pixel_sha256)


def context_review_id(figure_id: str) -> str:
    return stable_id("figure-context-review", figure_id)


def _legacy_figure_rows(review: dict[str, Any]) -> list[dict[str, Any]]:
    evidence = review.get("review_evidence", {})
    if not isinstance(evidence, dict):
        return []
    rows = evidence.get("native_figures")
    if isinstance(rows, list):
        return [dict(item) for item in rows if isinstance(item, dict) and item.get("figure_id")]
    figure_id = evidence.get("native_reviewed_figure_id")
    if figure_id:
        return [{
            "figure_id": figure_id,
            "status": evidence.get("figure_review_scope", "declared_reviewed"),
            "code_block_sha256": evidence.get("code_block_sha256"),
        }]
    return []


def load_legacy_declarations(index_dir: Path) -> dict[str, dict[str, Any]]:
    declarations: dict[str, dict[str, Any]] = {}
    review_dir = index_dir / "manual-review"
    for name in ("gold-review-batch-a.jsonl", "gold-review-batch-b.jsonl"):
        path = review_dir / name
        if not path.is_file():
            continue
        for review in read_jsonl(path):
            for row in _legacy_figure_rows(review):
                figure_id = str(row["figure_id"])
                code_sha = row.get("code_block_sha256")
                if code_sha is not None and not SHA256.fullmatch(str(code_sha)):
                    code_sha = None
                declaration = {
                    "source_review_file": f"manual-review/{name}",
                    "source_review_bundle_id": str(review.get("bundle_id", "")),
                    "declared_status": str(row.get("status", "declared_reviewed")),
                    "declared_code_block_sha256": code_sha,
                    "trust_level": "declaration_only",
                }
                prior = declarations.get(figure_id)
                if prior is not None and prior != declaration:
                    raise NativeFigureRegistryError(f"Conflicting legacy declarations for {figure_id}")
                declarations[figure_id] = declaration
    return declarations


def _dimensions(record: dict[str, Any]) -> dict[str, int] | None:
    value = record.get("dimensions")
    if not isinstance(value, dict):
        return None
    width, height = value.get("width"), value.get("height")
    if not isinstance(width, int) or not isinstance(height, int) or width < 1 or height < 1:
        return None
    return {"width": width, "height": height}


def _metadata_flags(records: list[dict[str, Any]], dimensions: list[dict[str, int]]) -> list[str]:
    flags: list[str] = []
    if len(dimensions) < len(records):
        flags.append("missing_dimensions")
    if any(min(item["width"], item["height"]) < 100 for item in dimensions):
        flags.append("short_edge_lt_100")
    if any(max(item["width"] / item["height"], item["height"] / item["width"]) > 5 for item in dimensions):
        flags.append("extreme_aspect_ratio_gt_5")
    if len(records) > 1:
        flags.append("duplicate_pixel_payload")
    return flags


def _code_candidates(bundle: dict[str, Any] | None) -> dict[str, int]:
    if bundle is None:
        ordered = fenced = 0
    else:
        ordered = len(bundle.get("ordered_code_files", []))
        article = bundle.get("article", {})
        fenced = len(article.get("fenced_code_blocks", [])) if isinstance(article, dict) else 0
    return {"ordered_code_files": ordered, "fenced_code_blocks": fenced, "total": ordered + fenced}


def construct_records(index_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    figures = read_jsonl(index_dir / "figure-cards.jsonl")
    bundles = {row["bundle_id"]: row for row in read_jsonl(index_dir / "source-flow-bundles.jsonl")}
    legacy = load_legacy_declarations(index_dir)
    seen_figure_ids: set[str] = set()
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for row in figures:
        figure_id = str(row.get("figure_card_id", ""))
        pixel_sha = str(row.get("sha256", ""))
        if not re.fullmatch(r"figure-[0-9a-f]{20}", figure_id):
            raise NativeFigureRegistryError(f"Invalid FigureCard ID: {figure_id!r}")
        if figure_id in seen_figure_ids:
            raise NativeFigureRegistryError(f"Duplicate FigureCard ID: {figure_id}")
        if not SHA256.fullmatch(pixel_sha):
            raise NativeFigureRegistryError(f"Invalid image SHA-256 for {figure_id}")
        seen_figure_ids.add(figure_id)
        grouped[pixel_sha].append(row)

    pixel_records: list[dict[str, Any]] = []
    for pixel_sha in sorted(grouped):
        rows = sorted(grouped[pixel_sha], key=lambda item: item["figure_card_id"])
        dimensions = []
        for row in rows:
            value = _dimensions(row)
            if value is not None and value not in dimensions:
                dimensions.append(value)
        dimensions.sort(key=lambda item: (item["width"], item["height"]))
        extensions = sorted({Path(str(row.get("private_source_locator", ""))).suffix.casefold() for row in rows if Path(str(row.get("private_source_locator", ""))).suffix})
        figure_ids = [row["figure_card_id"] for row in rows]
        pixel_records.append({
            "schema_version": "1.0",
            "pixel_review_id": pixel_review_id(pixel_sha),
            "pixel_sha256": pixel_sha,
            "representative_figure_id": figure_ids[0],
            "figure_ids": figure_ids,
            "occurrence_count": len(rows),
            "dimensions": dimensions,
            "extensions": extensions,
            "metadata_flags": _metadata_flags(rows, dimensions),
            "inspection_status": "pending",
            "reviewer": None,
            "reviewed_at": None,
            "native_review_evidence": [],
            "visual_findings": [],
            "not_applicable_reason": None,
            "claim_boundary": PIXEL_CLAIM_BOUNDARY,
        })

    context_records: list[dict[str, Any]] = []
    for row in sorted(figures, key=lambda item: item["figure_card_id"]):
        figure_id = row["figure_card_id"]
        source_bundle_id = str(row.get("source_bundle_id", ""))
        bundle = bundles.get(source_bundle_id)
        if bundle is None:
            raise NativeFigureRegistryError(f"Unknown source bundle for {figure_id}: {source_bundle_id}")
        candidates = _code_candidates(bundle)
        no_code = candidates["total"] == 0
        code_link = row.get("code_link") if isinstance(row.get("code_link"), dict) else None
        no_code_reason = "No extracted code candidate exists in the linked SourceFlowBundle."
        context_records.append({
            "schema_version": "1.0",
            "figure_context_review_id": context_review_id(figure_id),
            "figure_id": figure_id,
            "source_bundle_id": source_bundle_id,
            "pixel_review_id": pixel_review_id(row["sha256"]),
            "pixel_sha256": row["sha256"],
            "markdown_image_reference": code_link,
            "code_reference_kind": "markdown_image_reference" if code_link else "none",
            "code_candidates": candidates,
            "code_binding_status": "not_applicable" if no_code else "pending",
            "generator_code_sha256": None,
            "code_binding_not_applicable_reason": no_code_reason if no_code else None,
            "native_review_status": "pending",
            "native_review_not_applicable_reason": None,
            "reviewer": None,
            "reviewed_at": None,
            "native_review_evidence": [],
            "reproduction_applicability": "not_applicable" if no_code else "pending",
            "reproduction_not_applicable_reason": no_code_reason if no_code else None,
            "semantic_review_status": "pending",
            "directly_visible": [],
            "supported_claims": [],
            "unsupported_claims": [],
            "legacy_declaration": legacy.get(figure_id),
            "claim_boundary": CONTEXT_CLAIM_BOUNDARY,
        })

    statistics = {
        "figure_cards": len(figures),
        "unique_pixel_payloads": len(pixel_records),
        "duplicate_clusters": sum(1 for row in pixel_records if row["occurrence_count"] > 1),
        "figures_in_duplicate_clusters": sum(row["occurrence_count"] for row in pixel_records if row["occurrence_count"] > 1),
        "missing_dimensions": sum(1 for row in figures if _dimensions(row) is None),
        "short_edge_lt_100": sum(1 for row in figures if (dim := _dimensions(row)) is not None and min(dim["width"], dim["height"]) < 100),
        "extreme_aspect_ratio_gt_5": sum(1 for row in figures if (dim := _dimensions(row)) is not None and max(dim["width"] / dim["height"], dim["height"] / dim["width"]) > 5),
        "legacy_declarations": len(legacy),
        "legacy_code_hash_declarations": sum(1 for row in legacy.values() if row["declared_code_block_sha256"]),
        "new_native_reviews": 0,
    }
    return pixel_records, context_records, statistics


def construct_first_batch(context_records: list[dict[str, Any]]) -> dict[str, Any]:
    by_id = {row["figure_id"]: row for row in context_records}
    items: list[dict[str, Any]] = []
    for position, (figure_id, role) in enumerate(FIRST_BATCH, start=1):
        context = by_id.get(figure_id)
        if context is None:
            raise NativeFigureRegistryError(f"First-batch FigureCard missing: {figure_id}")
        legacy_code_sha = (context.get("legacy_declaration") or {}).get("declared_code_block_sha256")
        if role == "legacy_code_hash_declared" and not legacy_code_sha:
            raise NativeFigureRegistryError(f"Expected legacy code hash declaration for {figure_id}")
        items.append({
            "position": position,
            "figure_id": figure_id,
            "figure_context_review_id": context["figure_context_review_id"],
            "pixel_review_id": context["pixel_review_id"],
            "selection_role": role,
            "native_review_status": "pending",
            "code_binding_status": context["code_binding_status"],
            "legacy_code_block_sha256": legacy_code_sha,
            "review_required": ["native_pixel_view", "figure_context", "claim_boundary"] + ([] if context["code_binding_status"] == "not_applicable" else ["generator_code_binding"]),
        })
    return {
        "schema_version": "1.0",
        "batch_id": "native-figure-review-001",
        "items": items,
        "claim_boundary": "Batch selection is not viewing evidence; every new native_review_status remains pending.",
    }


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    rendered = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for row in rows)
    _write_text(path, rendered)


def _write_json(path: Path, value: dict[str, Any]) -> None:
    _write_text(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _write_text(path: Path, rendered: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(rendered, encoding="utf-8")
    os.replace(temporary, path)


def build_registry(index_dir: Path, output_dir: Path) -> dict[str, Any]:
    index_dir, output_dir = index_dir.resolve(), output_dir.resolve()
    pixel_records, context_records, statistics = construct_records(index_dir)
    first_batch = construct_first_batch(context_records)
    pixel_path = output_dir / "pixel-artifact-review-queue.jsonl"
    context_path = output_dir / "figure-context-review-queue.jsonl"
    batch_path = output_dir / "native-figure-review-batch-001.json"
    _write_jsonl(pixel_path, pixel_records)
    _write_jsonl(context_path, context_records)
    _write_json(batch_path, first_batch)
    manifest = {
        "schema_version": "1.0",
        "source_mode": "read_only",
        "distribution": "private_local_only",
        "statistics": statistics,
        "outputs": {
            pixel_path.name: {"records": len(pixel_records), "sha256": sha256_file(pixel_path)},
            context_path.name: {"records": len(context_records), "sha256": sha256_file(context_path)},
            batch_path.name: {"records": len(first_batch["items"]), "sha256": sha256_file(batch_path)},
        },
        "status_boundary": "Generated queues contain no new native-reviewed records.",
    }
    _write_json(output_dir / "native-figure-review-manifest.json", manifest)
    return manifest


def validate_registry(index_dir: Path, output_dir: Path) -> dict[str, Any]:
    errors: list[str] = []
    expected_pixels, expected_contexts, statistics = construct_records(index_dir.resolve())
    expected_batch = construct_first_batch(expected_contexts)
    pixel_path = output_dir / "pixel-artifact-review-queue.jsonl"
    context_path = output_dir / "figure-context-review-queue.jsonl"
    batch_path = output_dir / "native-figure-review-batch-001.json"
    try:
        actual_pixels = read_jsonl(pixel_path)
        actual_contexts = read_jsonl(context_path)
        actual_batch = json.loads(batch_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, NativeFigureRegistryError) as exc:
        return {"ok": False, "errors": [str(exc)]}
    if actual_pixels != expected_pixels:
        errors.append("pixel artifact queue differs from deterministic source reconstruction")
    if actual_contexts != expected_contexts:
        errors.append("figure context queue differs from deterministic source reconstruction")
    if actual_batch != expected_batch:
        errors.append("first review batch differs from fixed selection, order, roles, or pending state")
    if any(row.get("inspection_status") == "native_reviewed" for row in actual_pixels):
        errors.append("generated pixel queue must not claim native review")
    if any(row.get("native_review_status") == "native_reviewed" for row in actual_contexts):
        errors.append("generated context queue must not claim native review")
    batch_items = actual_batch.get("items", []) if isinstance(actual_batch, dict) else []
    if any(row.get("native_review_status") != "pending" for row in batch_items if isinstance(row, dict)):
        errors.append("first review batch must remain pending until a separate native-view review")
    return {
        "ok": not errors,
        "schema_version": "1.0",
        "statistics": statistics,
        "first_batch_items": len(batch_items),
        "new_native_reviews": 0,
        "errors": errors,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("build", "validate"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--index-dir", type=Path, default=DEFAULT_INDEX)
        subparser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "build":
            report = build_registry(args.index_dir, args.output_dir)
            report = {"ok": True, **report}
        else:
            report = validate_registry(args.index_dir, args.output_dir)
    except (OSError, json.JSONDecodeError, NativeFigureRegistryError) as exc:
        report = {"ok": False, "errors": [str(exc)]}
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
