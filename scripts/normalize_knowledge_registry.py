#!/usr/bin/env python3
"""Normalize raw corpus records into the shared knowledge-object schemas.

Raw extraction records remain immutable.  This command creates a derived registry and
never promotes maturity beyond ``raw-extracted``.  Articles without code remain useful
MethodCards but are not misrepresented as SourceFlowBundles.
"""

from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable


SKILL_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = SKILL_ROOT / "assets" / "private-corpus-index"
DEFAULT_OUTPUT = DEFAULT_INPUT / "normalized-registry"
SCHEMA_ROOT = SKILL_ROOT / "references" / "schemas"
MATURITY = "raw-extracted"
KIND_TO_DEFINITION = {
    "source-flow-bundle": "SourceFlowBundle",
    "method-card": "MethodCard",
    "package-card": "PackageCard",
    "figure-card": "FigureCard",
    "variant-set": "VariantSet",
}


class NormalizationError(RuntimeError):
    """Raised when a raw record cannot be represented without fabrication."""


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if line.strip():
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise NormalizationError(f"Expected object at {path}:{line_number}")
                records.append(value)
    return records


def write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    count = 0
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
            count += 1
    os.replace(temporary, path)
    return count


def provenance(source_path: str, sha256: str, article_id: str) -> dict[str, Any]:
    return {
        "source_path": source_path,
        "sha256": sha256,
        "article_id": article_id,
        "license": None,
        "private_source": True,
    }


def bundle_provenance(bundle: dict[str, Any]) -> dict[str, Any]:
    article = bundle.get("article", {})
    source = str(article.get("source_locator") or bundle.get("private_source_directory") or bundle["bundle_id"])
    digest = str(article.get("sha256") or bundle.get("flow_integrity", {}).get("flow_fingerprint_sha256") or "")
    if len(digest) != 64:
        raise NormalizationError(f"Bundle lacks a valid provenance hash: {bundle['bundle_id']}")
    return provenance(source, digest, bundle["bundle_id"])


def normalize_source_bundle(bundle: dict[str, Any]) -> dict[str, Any] | None:
    code_files = list(bundle.get("ordered_code_files", []))
    blocks = list(bundle.get("article", {}).get("fenced_code_blocks", []))
    if not code_files and not blocks:
        return None
    selected = code_files or [
        {
            "ordinal": block["ordinal"],
            "normalized_language": block.get("normalized_language", "other"),
            "source_locator": f"{bundle['article']['source_locator']}#block-{block['ordinal']:04d}",
            "sha256": block["sha256"],
            "static_facts": block.get("static_facts", {}),
        }
        for block in blocks
    ]
    ordered_code = []
    for order, item in enumerate(selected, start=1):
        facts = item.get("static_facts", {})
        language = str(item.get("normalized_language", "other")).casefold()
        if language not in {"r", "python", "shell", "other"}:
            language = "other"
        produces = sorted({str(value) for value in facts.get("assignments", []) + facts.get("output_calls", []) if str(value)})
        consumes = sorted({str(value) for value in facts.get("input_calls", []) if str(value)})
        ordered_code.append(
            {
                "order": order,
                "language": language,
                "source_path": str(item.get("source_locator")),
                "sha256": str(item.get("sha256")),
                "produces": produces,
                "consumes": consumes,
            }
        )

    images = [provenance(str(image["source_locator"]), str(image["sha256"]), bundle["bundle_id"]) for image in bundle.get("images", [])]
    spans = [(int(block.get("source_span", [0, 0])[1]), int(block.get("ordinal", 1))) for block in blocks]
    code_links = []
    for image in bundle.get("images", []):
        reference = image.get("article_reference") or {}
        line = int(reference.get("source_line", 0) or 0)
        preceding = [ordinal for end, ordinal in spans if end <= line]
        inferred = max(preceding) if preceding else min(int(image.get("ordinal", 1)), len(ordered_code))
        inferred = max(1, min(inferred, len(ordered_code)))
        code_links.append(
            {
                "image_path": str(image["source_locator"]),
                "code_orders": [inferred],
                "relationship": "illustrates" if reference else "unknown",
                "confidence": "inferred" if reference else "unresolved",
            }
        )

    nodes = [f"code:{item['order']}" for item in ordered_code]
    edges = [[nodes[index], nodes[index + 1]] for index in range(len(nodes) - 1)]
    gaps = list(bundle.get("issues", []))
    if any(item.get("static_facts", {}).get("installer_calls") for item in selected):
        gaps.append("Installer calls are present in source code and must be removed from any derived AnalysisRecipe.")
    if any(item.get("static_facts", {}).get("absolute_paths") for item in selected):
        gaps.append("Hard-coded paths require parameterization before execution.")
    return {
        "bundle_id": bundle["bundle_id"],
        "title": str(bundle.get("title") or f"Untitled source bundle {bundle['bundle_id']}"),
        "provenance": [bundle_provenance(bundle)],
        "ordered_code": ordered_code,
        "images": images,
        "code_image_links": code_links,
        "object_graph": {"nodes": nodes, "edges": edges},
        "packages": sorted({str(value) for value in bundle.get("package_index", {}).get("packages", [])}),
        "gaps": sorted(set(str(value) for value in gaps if str(value))),
        "maturity": MATURITY,
    }


def normalize_method_card(card: dict[str, Any], bundles: dict[str, dict[str, Any]]) -> dict[str, Any]:
    source_ids = list(card.get("source_bundle_ids", []))
    provenance_records = [bundle_provenance(bundles[bundle_id]) for bundle_id in source_ids if bundle_id in bundles]
    hints = [str(value) for value in card.get("research_question_hints", []) if str(value)]
    sequence = [str(value) for value in card.get("method_sequence", []) if str(value)]
    applicability = [str(value) for value in card.get("data_types", []) if str(value)] or [str(card.get("category", "unspecified"))]
    logic = sequence or [str(card.get("combination_logic") or "Method order requires manual reconstruction.")]
    return {
        "method_id": str(card.get("method_card_id")),
        "question": hints[0] if hints else str(card.get("title") or "Method question requires review."),
        "applicability": applicability,
        "statistical_unit": str(card.get("analysis_unit") or "unknown_requires_review"),
        "combination_logic": logic,
        "assumptions": [str(value) for value in card.get("assumptions", [])],
        "alternatives": [str(value) for value in card.get("alternatives", [])],
        "limitations": [str(value) for value in card.get("limitations", [])] or ["Primary methods and source data have not been verified."],
        "validation": [str(value) for value in card.get("required_validation", [])] or ["manual_methodology_review"],
        "claim_ceiling": str(card.get("claim_ceiling") or "candidate_only"),
        "provenance": provenance_records,
        "maturity": MATURITY,
    }


def normalize_package_card(card: dict[str, Any], bundles: dict[str, dict[str, Any]]) -> dict[str, Any]:
    language = str(card.get("language", "system")).casefold()
    if language not in {"r", "python", "system"}:
        language = "system"
    raw_source = str(card.get("canonical_source", "unknown")).casefold()
    source_aliases = {"bioc": "bioconductor", "conda-forge": "conda", "bioconda": "conda", "pypi": "pypi", "cran": "cran", "github": "github", "system": "system"}
    source = source_aliases.get(raw_source, "unknown")
    fallbacks = ["lock-restore"]
    if language == "r":
        fallbacks.extend(["pak", "conda"])
    elif language == "python":
        fallbacks.extend(["uv", "conda"])
    else:
        fallbacks.append("conda")
    source_ids = list(card.get("source_bundle_ids", []))
    provenance_records = [bundle_provenance(bundles[bundle_id]) for bundle_id in source_ids if bundle_id in bundles]
    capabilities = [str(value) for value in card.get("capability_hints", []) if str(value)]
    known_failures = []
    if source == "unknown":
        known_failures.append("Canonical package source requires review before environment resolution.")
    if str(card.get("version_constraint", "unknown")) == "unknown":
        known_failures.append("Package version is not pinned.")
    return {
        "package_id": str(card.get("package_card_id")),
        "name": str(card.get("package")),
        "language": language,
        "source": source,
        "version_policy": str(card.get("version_constraint") or "unknown_requires_review"),
        "functions": sorted({str(value) for value in card.get("functions", []) if str(value)}),
        "complete_workflow": capabilities or ["Package workflow requires manual reconstruction from source bundles."],
        "dependencies": [],
        "install_fallbacks": list(dict.fromkeys(fallbacks)),
        "known_failures": known_failures,
        "provenance": provenance_records,
        "maturity": MATURITY,
    }


def normalize_figure_card(card: dict[str, Any], bundles: dict[str, dict[str, Any]], method_by_bundle: dict[str, dict[str, Any]]) -> dict[str, Any]:
    bundle_id = str(card.get("source_bundle_id"))
    bundle = bundles.get(bundle_id, {})
    method = method_by_bundle.get(bundle_id, {})
    dimensions = card.get("dimensions", {})
    visible = [str(value) for value in card.get("visible", []) if str(value)]
    if not visible:
        visible = [f"Image metadata available ({dimensions.get('width', 'unknown')} x {dimensions.get('height', 'unknown')} px); native content not reviewed."]
    source = str(card.get("private_source_locator"))
    digest = str(card.get("sha256"))
    return {
        "figure_id": str(card.get("figure_card_id")),
        "question": str(method.get("question") or bundle.get("title") or "Figure question requires review."),
        "figure_path": source,
        "statistical_unit": str(method.get("statistical_unit") or "unknown_requires_review"),
        "semantics": {
            "dimensions": dimensions,
            "plot_hints": [str(value) for value in card.get("plot_hints", [])],
            "code_link": card.get("code_link"),
            "evidence_level": str(card.get("evidence_level", "image_metadata")),
        },
        "directly_visible": visible,
        "supported_claims": [str(value) for value in card.get("supports", [])],
        "unsupported_claims": [str(value) for value in card.get("does_not_support", [])] or ["No scientific conclusion before native image, code, and data review."],
        "assumptions": ["Code-image correspondence and statistical mapping require manual review."],
        "visual_qa": "block",
        "reproduction_class": "not-applicable",
        "provenance": [provenance(source, digest, bundle_id)],
        "maturity": MATURITY,
    }


def normalize_variant_set(module: dict[str, Any], bundles: dict[str, dict[str, Any]]) -> dict[str, Any]:
    variants = []
    for raw in module.get("variants", []):
        classification = str(raw.get("equivalence", "compatible"))
        if classification not in {"exact", "compatible", "alternative_method"}:
            classification = "compatible"
        bundle_id = str(raw.get("source_bundle_id"))
        provenance_records = [bundle_provenance(bundles[bundle_id])] if bundle_id in bundles else []
        differences = [] if classification == "exact" else ["Difference is metadata-derived and requires semantic review."]
        variants.append(
            {
                "variant_id": str(raw.get("variant_id")),
                "classification": classification,
                "implementation_ref": bundle_id,
                "differences": differences,
                "auto_fallback": False,
                "provenance": provenance_records,
            }
        )
    if not variants:
        raise NormalizationError(f"Capability module has no variants: {module.get('capability_module_id')}")
    requested_canonical = str(module.get("canonical_variant_id") or "")
    exact_ids = [item["variant_id"] for item in variants if item["classification"] == "exact"]
    exact = requested_canonical if requested_canonical in set(exact_ids) else (exact_ids[0] if exact_ids else variants[0]["variant_id"])
    return {
        "variant_set_id": str(module.get("capability_module_id")),
        "capability": str(module.get("capability")),
        "canonical_variant": exact,
        "variants": variants,
        "maturity": MATURITY,
    }


def validate_records(kind: str, records: list[dict[str, Any]]) -> None:
    try:
        import jsonschema
    except ImportError as exc:
        raise NormalizationError("jsonschema is required for --validate") from exc
    bundle = json.loads((SCHEMA_ROOT / "knowledge-objects.schema.json").read_text(encoding="utf-8"))
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$ref": f"#/$defs/{KIND_TO_DEFINITION[kind]}",
        "$defs": bundle["$defs"],
    }
    validator = jsonschema.Draft202012Validator(schema)
    failures = []
    for index, record in enumerate(records):
        errors = sorted(validator.iter_errors(record), key=lambda error: list(error.path))
        if errors:
            failures.append(f"{kind}[{index}]: {errors[0].message}")
            if len(failures) == 20:
                break
    if failures:
        raise NormalizationError("Schema validation failed:\n" + "\n".join(failures))


def build_registry(input_dir: Path, output_dir: Path, validate: bool = False) -> dict[str, Any]:
    input_dir = input_dir.resolve()
    output_dir = output_dir.resolve()
    if input_dir == output_dir:
        raise NormalizationError("Output must not overwrite the raw index.")
    bundles_list = read_jsonl(input_dir / "source-flow-bundles.jsonl")
    bundles = {bundle["bundle_id"]: bundle for bundle in bundles_list}
    raw_methods = read_jsonl(input_dir / "method-cards.jsonl")
    normalized_methods = [normalize_method_card(card, bundles) for card in raw_methods]
    method_by_bundle = {}
    for raw, normalized in zip(raw_methods, normalized_methods):
        for bundle_id in raw.get("source_bundle_ids", []):
            method_by_bundle.setdefault(bundle_id, normalized)
    normalized = {
        "source-flow-bundle": [item for bundle in bundles_list if (item := normalize_source_bundle(bundle)) is not None],
        "method-card": normalized_methods,
        "package-card": [normalize_package_card(card, bundles) for card in read_jsonl(input_dir / "package-cards.jsonl")],
        "figure-card": [normalize_figure_card(card, bundles, method_by_bundle) for card in read_jsonl(input_dir / "figure-cards.jsonl")],
        "variant-set": [normalize_variant_set(module, bundles) for module in json.loads((input_dir / "capability-modules.json").read_text(encoding="utf-8"))["modules"]],
    }
    if validate:
        for kind, records in normalized.items():
            validate_records(kind, records)
    output_dir.mkdir(parents=True, exist_ok=True)
    counts = {}
    for kind, records in normalized.items():
        counts[kind] = write_jsonl(output_dir / f"{kind}s.jsonl", records)
    manifest = {
        "schema_version": "1.0",
        "source_index": str(input_dir),
        "source_index_policy": "read_only",
        "maturity": MATURITY,
        "schema_validated": validate,
        "counts": counts,
        "skipped": {"source-flow-bundle-no-code": len(bundles_list) - counts["source-flow-bundle"]},
        "scientific_boundary": "Normalization is structural only; no record is executable or scientifically verified.",
    }
    temporary = output_dir / "registry-manifest.json.tmp"
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, output_dir / "registry-manifest.json")
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--validate", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = build_registry(args.input_dir, args.output_dir, args.validate)
    except (OSError, json.JSONDecodeError, NormalizationError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps({"ok": True, **report}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
