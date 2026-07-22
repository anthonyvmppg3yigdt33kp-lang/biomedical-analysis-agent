import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "validate_distillation_reviews.py"
SPEC = importlib.util.spec_from_file_location("validate_distillation_reviews", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def write_jsonl(path: Path, rows):
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


class DistillationReviewRecordTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.index = self.root / "index"
        self.index.mkdir()
        self.record_id = "prep-" + "1" * 24
        self.bundle_id = "flow-" + "2" * 20
        self.article_sha = "3" * 64
        self.flow_sha = "4" * 64
        self.code_sha = "5" * 64
        self.fence_sha = "6" * 64
        self.image_sha = "7" * 64
        self.relation_sha = "8" * 64
        write_jsonl(
            self.index / "preprocessing-records.jsonl",
            [{
                "preprocess_record_id": self.record_id,
                "record_sha256": "9" * 64,
                "record": {"数据类型": "count matrix"},
            }],
        )
        write_jsonl(
            self.index / "preprocessing-crosswalk.jsonl",
            [{"preprocess_record_id": self.record_id, "relations": [{"bundle_id": self.bundle_id}]}],
        )
        write_jsonl(
            self.index / "source-flow-bundles.jsonl",
            [{
                "bundle_id": self.bundle_id,
                "article": {
                    "sha256": self.article_sha,
                    "fenced_code_blocks": [{"sha256": self.fence_sha}],
                },
                "flow_integrity": {"flow_fingerprint_sha256": self.flow_sha},
                "ordered_code_files": [{"sha256": self.code_sha}],
                "images": [{"sha256": self.image_sha}],
            }],
        )
        write_jsonl(
            self.index / "figure-cards.jsonl",
            [{"figure_card_id": "figure-test", "source_bundle_id": self.bundle_id, "sha256": self.image_sha}],
        )
        (self.index / "distillation-review-manifest.json").write_text('{"schema_version":"1.0"}\n', encoding="utf-8")
        self.batch_path = self.root / "batch.jsonl"
        self.queue_item = {
            "batch_id": "high-value-001",
            "batch_position": 1,
            "queue_item_id": "distill-review-" + "a" * 20,
            "preprocess_record_id": self.record_id,
            "record_sha256": "9" * 64,
            "source_relation_sha256": self.relation_sha,
            "record_title": "Test flow",
            "category": "bulk",
            "linked_bundle_ids": [self.bundle_id],
            "external_targets": [],
        }
        write_jsonl(self.batch_path, [self.queue_item])

    def tearDown(self):
        self.temp.cleanup()

    def complete_review(self):
        registry = MODULE.load_index(self.index)
        review = MODULE.build_skeleton(self.queue_item, registry, "unit-test")
        review["research_context"] = {
            "research_question": "Does treatment change expression?",
            "input_modality": "count matrix",
            "cohort_or_sample_structure": "paired samples",
            "descriptive_unit": "gene by sample count",
            "inferential_unit": "donor",
        }
        review["method_sequence"] = [{
            "order": 1,
            "method": "paired differential model",
            "rationale": "preserve donor pairing",
            "inputs": ["counts", "metadata"],
            "outputs": ["effect estimates"],
        }]
        review["combination_logic"] = "Model first, then visualize the unchanged model output."
        review["code_review"].update({
            "languages": ["r"],
            "completeness": "partial_flow",
            "object_chain": "counts and metadata produce a fitted model and result table",
            "required_inputs": ["raw counts", "paired donor metadata"],
            "gaps": ["package versions are absent"],
            "repair_required": True,
        })
        review["figure_context_review"] = {
            "status": "native_sample_reviewed",
            "figures": [{
                "figure_id": "figure-test",
                "image_sha256": self.image_sha,
                "review_scope": "native_pixels",
                "viewer": "codex_view_image",
                "figure_role": "diagnostic display",
                "code_binding": {"status": "inferred_nearby_code", "evidence": [self.code_sha]},
                "visible": ["two sample groups are displayed"],
                "interpretable": ["separation may reflect treatment or pairing"],
                "confirmed": [],
                "cannot_assert": ["the plot alone cannot establish significance"],
                "visual_issues": [],
                "reproduction_class": "semantic_candidate",
            }],
            "unreviewed_figure_count": 0,
            "selection_rationale": "the only source image was inspected",
        }
        review["scientific_review"] = {
            "assumptions": ["paired metadata are correct"],
            "scientific_risks": ["ignoring pairing creates pseudoreplication"],
            "alternatives": ["a mixed model if repeated measures exceed two"],
            "negative_controls": ["permute treatment within donor"],
            "validation_required": ["run a paired fixture with known effects"],
            "claim_ceiling": "association within this cohort; no causal generalization",
        }
        review["stage_evidence"] = [
            {"stage": stage, "status": "passed_with_blockers", "evidence": f"reviewed {stage.lower()} with recorded blockers"}
            for stage in MODULE.REQUIRED_STAGES
        ]
        review["decision"] = {
            "classification": "workflow_candidate",
            "target_objects": ["MethodCard", "WorkflowTemplate"],
            "reason": "retain after a paired fixture and dependency lock",
            "automatic_execution_allowed": False,
        }
        review["maturity"] = "normalized"
        review["maturity_blockers"] = ["not executed on a fixture"]
        review["reviewer"] = {
            "reviewer_id": "unit-test",
            "reviewed_at": "2026-07-19T00:00:00Z",
            "tools": ["source inventory", "codex_view_image"],
        }
        review["review_state"] = "COMPLETE"
        return review

    def test_complete_review_validates(self):
        path = self.root / "review.jsonl"
        write_jsonl(path, [self.complete_review()])
        report = MODULE.validate_reviews(self.index, self.batch_path, [path], require_complete=True)
        self.assertTrue(report["ok"], report["errors"])
        self.assertEqual(report["native_pixel_samples_declared"], 1)

    def test_exact_source_inventory_is_required(self):
        review = self.complete_review()
        review["source_evidence"]["bundles"][0]["ordered_code_sha256"] = []
        path = self.root / "review.jsonl"
        write_jsonl(path, [review])
        report = MODULE.validate_reviews(self.index, self.batch_path, [path], require_complete=True)
        self.assertFalse(report["ok"])
        self.assertTrue(any("exact ordered source inventory" in error for error in report["errors"]))

    def test_materialized_batch_receipt_preserves_historical_manifest_hash(self):
        historical = "b" * 64
        receipt = {
            "ok": True,
            "batch_id": "high-value-001",
            "items": 1,
            "batch_sha256": MODULE.sha256_file(self.batch_path),
            "queue_manifest_sha256": historical,
        }
        self.batch_path.with_suffix(self.batch_path.suffix + ".receipt.json").write_text(
            json.dumps(receipt),
            encoding="utf-8",
        )
        review = self.complete_review()
        review["source_evidence"]["queue_manifest_sha256"] = historical
        path = self.root / "historical-review.jsonl"
        write_jsonl(path, [review])
        report = MODULE.validate_reviews(self.index, self.batch_path, [path], require_complete=True)
        self.assertTrue(report["ok"], report["errors"])

    def test_complete_review_rejects_placeholders_and_blocked_stage(self):
        review = self.complete_review()
        review["combination_logic"] = "TODO: later"
        review["stage_evidence"][2]["status"] = "blocked"
        path = self.root / "review.jsonl"
        write_jsonl(path, [review])
        report = MODULE.validate_reviews(self.index, self.batch_path, [path], require_complete=True)
        self.assertFalse(report["ok"])
        self.assertTrue(any("placeholder" in error for error in report["errors"]))
        self.assertTrue(any("blocked stage" in error for error in report["errors"]))

    def test_parse_verified_requires_all_raw_items_to_pass(self):
        review = self.complete_review()
        review["maturity"] = "parse-verified"
        review["code_review"]["syntax_evidence_ref"] = (
            f"manual-review/syntax-audits/high-value-001-syntax-audit.jsonl"
            f"#preprocess_record_id={self.record_id}"
        )
        audit_dir = self.index / "manual-review" / "syntax-audits"
        audit_dir.mkdir(parents=True)
        audit_path = audit_dir / "high-value-001-syntax-audit.jsonl"
        base = {
            "batch_id": "high-value-001",
            "preprocess_record_id": self.record_id,
            "record_sha256": "9" * 64,
            "hash_verified": True,
            "parse_status": "passed",
        }
        write_jsonl(audit_path, [
            {**base, "audit_item_id": "audit-1", "item_type": "ordered_code_file"},
            {
                **base,
                "audit_item_id": "audit-2",
                "item_type": "article_fenced_block",
                "article_hash_verified": True,
            },
        ])
        path = self.root / "parse-review.jsonl"
        write_jsonl(path, [review])
        report = MODULE.validate_reviews(self.index, self.batch_path, [path], require_complete=True)
        self.assertTrue(report["ok"], report["errors"])
        self.assertEqual(report["parse_verified_records_checked"], 1)

        failed_rows = [
            {**base, "audit_item_id": "audit-1", "item_type": "ordered_code_file"},
            {
                **base,
                "audit_item_id": "audit-2",
                "item_type": "article_fenced_block",
                "article_hash_verified": True,
                "parse_status": "failed",
                "normalization_candidate": {"parse_status": "passed"},
            },
        ]
        write_jsonl(audit_path, failed_rows)
        report = MODULE.validate_reviews(self.index, self.batch_path, [path], require_complete=True)
        self.assertFalse(report["ok"])
        self.assertTrue(any("every raw declared code item" in error for error in report["errors"]))


if __name__ == "__main__":
    unittest.main()
