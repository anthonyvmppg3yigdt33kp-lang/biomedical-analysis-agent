#!/usr/bin/env python3
"""Validate native-pixel review evidence and atomically finalize a spatial run."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any


class FinalizationError(RuntimeError):
    """Raised when review or artifact evidence is incomplete."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(stable_json(value), encoding="utf-8")


def tree_contract(root: Path, *, exclude: set[str] | None = None) -> dict[str, Any]:
    excluded = exclude or set()
    records: list[dict[str, Any]] = []
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        if relative in excluded:
            continue
        record = {"relative_path": relative, "size_bytes": path.stat().st_size, "sha256": sha256_file(path)}
        records.append(record)
        digest.update((json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"))
    return {"tree_sha256": digest.hexdigest(), "file_count": len(records), "records": records}


def stage_for_path(relative: str) -> str:
    if relative.startswith("00_request/"):
        return "S00_INTAKE"
    if relative.startswith("01_plan/") or relative.startswith("02_environment/") or relative.startswith("03_scripts/"):
        return "S00_INTAKE"
    if relative.startswith("04_intermediate/"):
        parts = Path(relative).parts
        return parts[1] if len(parts) > 1 else "unknown"
    if relative.startswith("05_results/"):
        return "S95_VISUALIZE_INTERPRET"
    if relative.startswith("06_figures/") or relative.startswith("07_reports/"):
        return "S95_VISUALIZE_INTERPRET"
    return "DELIVERY"


def validate_review(staging: Path, review: dict[str, Any]) -> list[dict[str, Any]]:
    template = json.loads((staging / "review-template.json").read_text(encoding="utf-8"))
    required = {item["figure_id"]: item for item in template["required_pairs"]}
    if review.get("review_state") != "native-reviewed":
        raise FinalizationError("review_state_not_native_reviewed")
    if review.get("decision") != "PASS_WITH_MINOR_FINDINGS":
        raise FinalizationError("review_decision_not_delivery_ready")
    if review.get("reviewed_native_pixels") is not True:
        raise FinalizationError("native_pixels_not_confirmed")
    if review.get("review_method") != "native_local_image_view" or review.get("review_tool") != "view_image":
        raise FinalizationError("native_view_method_or_tool_invalid")
    if int(review.get("blocking_findings", -1)) != 0 or int(review.get("major_findings", -1)) != 0:
        raise FinalizationError("blocking_or_major_findings_unresolved")
    figures = review.get("figures", [])
    if len(figures) != len(required) or {item.get("figure_id") for item in figures} != set(required):
        raise FinalizationError("review_figure_set_mismatch")
    manifest = json.loads((staging / "figures-manifest.json").read_text(encoding="utf-8"))
    manifested = {item["figure_id"]: item for item in manifest["figures"]}
    for item in figures:
        figure_id = item["figure_id"]
        expected = required[figure_id]
        if item.get("opened_original") is not True or item.get("opened_final") is not True:
            raise FinalizationError(f"figure_views_not_both_opened:{figure_id}")
        if item.get("evidence_level") != "image_code_data":
            raise FinalizationError(f"evidence_level_invalid:{figure_id}")
        if item.get("decision") not in {"keep", "keep-with-minor-findings"}:
            raise FinalizationError(f"figure_decision_not_keep:{figure_id}")
        if not all(isinstance(item.get(field), list) and item[field] for field in ("visible", "interpretable", "confirmed", "cannot_assert")):
            raise FinalizationError(f"review_claim_layers_incomplete:{figure_id}")
        original = staging / manifested[figure_id]["original"]["relative_path"]
        final = staging / manifested[figure_id]["final"]["relative_path"]
        if not original.is_file() or not final.is_file():
            raise FinalizationError(f"figure_file_missing:{figure_id}")
        if (
            sha256_file(original) != expected["original_sha256"]
            or sha256_file(final) != expected["final_sha256"]
            or item.get("original_sha256") != expected["original_sha256"]
            or item.get("final_sha256") != expected["final_sha256"]
        ):
            raise FinalizationError(f"figure_hash_mismatch:{figure_id}")
    return figures


def finalize(run_root: Path, review_path: Path) -> dict[str, Any]:
    run_root = run_root.resolve()
    review_path = review_path.resolve()
    staging = run_root / "_staging" / "S95_VISUALIZE_INTERPRET-attempt1"
    final_stage = run_root / "04_intermediate" / "S95_VISUALIZE_INTERPRET"
    if not staging.is_dir() or final_stage.exists():
        raise FinalizationError("visual_staging_missing_or_final_exists")
    review = json.loads(review_path.read_text(encoding="utf-8"))
    figures = validate_review(staging, review)
    write_json(staging / "review" / "native-visual-review.json", review)

    notes_path = staging / "reports" / "FIGURE_NOTES.md"
    notes = notes_path.read_text(encoding="utf-8")
    if "Native visual review: pending." not in notes:
        raise FinalizationError("figure_notes_pending_marker_missing")
    notes = notes.replace(
        "Native visual review: pending.",
        "Native visual review: PASS_WITH_MINOR_FINDINGS (native_local_image_view / view_image; all original and final-size PNGs opened and hash-bound).",
        1,
    )
    notes += "\n## Native review summary\n\n"
    for item in figures:
        notes += f"- `{item['figure_id']}`: {item['decision']}; " + "; ".join(item.get("minor_findings", []) or ["no unresolved visual finding"]) + ".\n"
    notes_path.write_text(notes, encoding="utf-8")

    qa_path = staging / "reports" / "QA_REPORT.md"
    qa = qa_path.read_text(encoding="utf-8")
    qa = qa.replace("Status: AWAITING_NATIVE_VISUAL_REVIEW", "Status: PASS_WITH_MINOR_FINDINGS")
    qa = qa.replace(
        "- Native visual review: PENDING. Delivery remains incomplete until both original and final-size figures are opened and hash-bound.",
        "- Native visual review: PASS; all original and final-size PNGs were opened with view_image and bound by SHA-256. Minor findings remain documented without changing data or statistical semantics.",
    )
    qa_path.write_text(qa, encoding="utf-8")

    validation = {
        "ok": True,
        "figure_count": len(figures),
        "review_state": "native-reviewed",
        "decision": review["decision"],
        "blocking_findings": 0,
        "major_findings": 0,
        "identity_transform_native_reviewed": review.get("identity_transform_native_reviewed") is True,
        "deconvolution_completed": False,
        "inferential_tests_performed": False,
    }
    if validation["identity_transform_native_reviewed"] is not True:
        raise FinalizationError("identity_transform_not_native_reviewed")
    write_json(staging / "stage-validation.json", validation)
    payload = tree_contract(staging)
    write_json(
        staging / "checkpoint.json",
        {
            "stage_id": "S95_VISUALIZE_INTERPRET",
            "status": "checkpointed",
            "attempt": 1,
            "payload_tree_sha256": payload["tree_sha256"],
            "payload_file_count": payload["file_count"],
            "validation": validation,
        },
    )
    os.replace(staging, final_stage)

    for kind in ("original", "final"):
        source_root = final_stage / "figures" / kind
        target_root = run_root / "06_figures" / kind
        for source in sorted(source_root.glob("*.png")):
            shutil.copy2(source, target_root / source.name)
    shutil.copy2(final_stage / "review" / "native-visual-review.json", run_root / "06_figures" / "review" / "native-visual-review.json")
    shutil.copy2(final_stage / "reports" / "FIGURE_NOTES.md", run_root / "07_reports" / "FIGURE_NOTES.md")
    shutil.copy2(final_stage / "reports" / "QA_REPORT.md", run_root / "07_reports" / "QA_REPORT.md")

    excluded = {
        "07_reports/ARTIFACT_INDEX.json",
        "07_reports/ARTIFACT_INDEX.md",
        "manifest/run_manifest.json",
        "manifest/artifact_ledger.jsonl",
    }
    contract = tree_contract(run_root, exclude=excluded)
    artifacts = []
    for index, item in enumerate(contract["records"], start=1):
        artifacts.append(
            {
                "artifact_id": f"spatial-{index:03d}",
                "stage_id": stage_for_path(item["relative_path"]),
                "relative_path": item["relative_path"],
                "sha256": item["sha256"],
                "size_bytes": item["size_bytes"],
                "assay_unit": "spot" if item["relative_path"].startswith(("04_intermediate/", "05_results/", "06_figures/")) else None,
                "inference_unit": "animal/specimen (n=1; descriptive only)",
                "maturity": "data-verified",
                "conclusion_role": "descriptive-teaching-evidence",
            }
        )
    write_json(run_root / "07_reports" / "ARTIFACT_INDEX.json", {"schema_version": "1.0", "artifact_count": len(artifacts), "artifacts": artifacts})
    markdown = ["# Artifact index", "", f"Registered artifacts: {len(artifacts)}", "", "| ID | Stage | Path | SHA-256 |", "|---|---|---|---|"]
    markdown.extend(f"| {item['artifact_id']} | {item['stage_id']} | `{item['relative_path']}` | `{item['sha256']}` |" for item in artifacts)
    (run_root / "07_reports" / "ARTIFACT_INDEX.md").write_text("\n".join(markdown) + "\n", encoding="utf-8")
    with (run_root / "manifest" / "artifact_ledger.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
        for item in artifacts:
            handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")

    previous = json.loads((run_root / "manifest" / "run_manifest.json").read_text(encoding="utf-8"))
    prior_stages = [item for item in previous.get("stages", []) if item.get("stage_id") != "S95_VISUALIZE_INTERPRET"]
    manifest = {
        **previous,
        "state": "DELIVERED",
        "latest_valid_checkpoint": "S95_VISUALIZE_INTERPRET",
        "stages": prior_stages
        + [
            {
                "stage_id": "S95_VISUALIZE_INTERPRET",
                "status": "checkpointed",
                "payload_tree_sha256": payload["tree_sha256"],
            }
        ],
        "artifact_count": len(artifacts),
        "artifact_index_sha256": sha256_file(run_root / "07_reports" / "ARTIFACT_INDEX.json"),
        "ledger_entry_count": len(artifacts),
        "native_visual_review": {
            "decision": review["decision"],
            "figure_count": len(figures),
            "review_sha256": sha256_file(run_root / "06_figures" / "review" / "native-visual-review.json"),
        },
        "claim_ceiling": "coordinate-faithful descriptive spot-level patterns for one Visium section; no deconvolution or population inference",
    }
    write_json(run_root / "manifest" / "run_manifest.json", manifest)
    result = {
        "ok": True,
        "state": "DELIVERED",
        "run_root": str(run_root),
        "latest_valid_checkpoint": "S95_VISUALIZE_INTERPRET",
        "artifact_count": len(artifacts),
        "ledger_entry_count": len(artifacts),
        "native_reviewed_figure_count": len(figures),
        "decision": review["decision"],
    }
    print(stable_json(result), end="")
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--review", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        finalize(args.run_root, args.review)
    except Exception as exc:
        print(stable_json({"ok": False, "error": f"{type(exc).__name__}:{exc}"}), end="", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
