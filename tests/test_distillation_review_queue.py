import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_distillation_review_queue.py"
SPEC = importlib.util.spec_from_file_location("build_distillation_review_queue", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def write_jsonl(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


class DistillationReviewQueueTests(unittest.TestCase):
    def build_fixture(self, base: Path):
        records = []
        crosswalk = []
        bundles = []
        for index, category in enumerate(["单细胞分析", "空间转录组", "bulk组学分析"]):
            record_id = f"preprocess-{index:020x}"
            bundle_id = f"flow-{index}"
            records.append({
                "preprocess_record_id": record_id,
                "record_sha256": f"{index + 1:064x}",
                "record": {"蒸馏价值": "高"},
            })
            crosswalk.append({
                "preprocess_record_id": record_id,
                "record_sha256": f"{index + 1:064x}",
                "record_title": f"record-{index}",
                "category": category,
                "code_asset_completeness": "完整流程",
                "status": "exact_bundle_path",
                "relation_verified": True,
                "distillation_value": "高",
                "relations": [{"relation_type": "exact_bundle_path", "bundle_id": bundle_id, "verified": True}],
            })
            bundles.append({
                "bundle_id": bundle_id,
                "ordered_code_files": [{"sha256": "a" * 64}],
                "article": {"fenced_code_blocks": []},
                "images": [{"sha256": "b" * 64}],
            })
        write_jsonl(base / "preprocessing-records.jsonl", records)
        write_jsonl(base / "preprocessing-crosswalk.jsonl", crosswalk)
        write_jsonl(base / "source-flow-bundles.jsonl", bundles)
        write_jsonl(base / "manual-review" / "gold-review-batch-a.jsonl", [{
            "bundle_id": "flow-0",
            "maturity": "parse-verified",
        }])

    def write_deep_review_receipt(self, base: Path, queue_row: dict, maturity: str = "normalized"):
        review_path = base / "manual-review" / "high-value-review-batch-001-test.jsonl"
        review = {
            "review_id": "distill-review-record-test",
            "preprocess_record_id": queue_row["preprocess_record_id"],
            "batch_id": queue_row["batch_id"],
            "batch_position": queue_row["batch_position"],
            "review_state": "COMPLETE",
            "maturity": maturity,
            "decision": {"automatic_execution_allowed": False},
            "source_evidence": {
                "record_sha256": queue_row["record_sha256"],
                "source_relation_sha256": queue_row["source_relation_sha256"],
            },
        }
        write_jsonl(review_path, [review])
        receipt = {
            "ok": True,
            "errors": [],
            "complete_records": 1,
            "review_files": {str(review_path.resolve()): MODULE.sha256_file(review_path)},
        }
        receipt_path = base / "manual-review" / "high-value-review-batch-001-validation.json"
        receipt_path.write_text(json.dumps(receipt, ensure_ascii=False), encoding="utf-8")
        return review_path, receipt_path

    def test_build_is_deterministic_and_preserves_completed_gold(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self.build_fixture(base)
            first, manifest_one = MODULE.build_queue(base, batch_size=2)
            second, manifest_two = MODULE.build_queue(base, batch_size=2)
            self.assertEqual(first, second)
            self.assertEqual(manifest_one["queue_sha256"], manifest_two["queue_sha256"])
            complete = next(item for item in first if item["preprocess_record_id"].endswith("0"))
            self.assertEqual(complete["review_state"], "COMPLETE_GOLD_REVIEW")
            self.assertIsNone(complete["batch_id"])
            pending = [item for item in first if item["action_required"]]
            self.assertEqual([item["category"] for item in pending], ["空间转录组", "bulk组学分析"])
            self.assertTrue(all(item["batch_id"] == "high-value-001" for item in pending))

    def test_validate_detects_stale_crosswalk(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self.build_fixture(base)
            result = MODULE.build_and_write(base, batch_size=2)
            self.assertTrue(result["ok"])
            self.assertTrue(MODULE.validate_queue(base)["ok"])
            rows = MODULE.read_jsonl(base / "preprocessing-crosswalk.jsonl")
            rows[1]["relations"][0]["verified"] = False
            write_jsonl(base / "preprocessing-crosswalk.jsonl", rows)
            validation = MODULE.validate_queue(base)
            self.assertFalse(validation["ok"])
            self.assertTrue(any("stale source relation" in error for error in validation["errors"]))

    def test_materialize_batch_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self.build_fixture(base)
            MODULE.build_and_write(base, batch_size=2)
            output = base / "batch.jsonl"
            receipt = MODULE.materialize_batch(base, "high-value-001", output)
            self.assertEqual(receipt["items"], 2)
            with self.assertRaises(FileExistsError):
                MODULE.materialize_batch(base, "high-value-001", output)

    def test_completed_deep_review_preserves_first_batch_positions(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self.build_fixture(base)
            MODULE.build_and_write(base, batch_size=2)
            before = {
                row["preprocess_record_id"]: (row["batch_id"], row["batch_position"])
                for row in MODULE.read_jsonl(base / "distillation-review-queue.jsonl")
            }
            first_pending = next(
                row for row in MODULE.read_jsonl(base / "distillation-review-queue.jsonl")
                if row["action_required"]
            )
            self.write_deep_review_receipt(base, first_pending, maturity="parse-verified")
            result = MODULE.build_and_write(base, batch_size=2)
            self.assertTrue(result["ok"])
            rebuilt = {
                row["preprocess_record_id"]: row
                for row in MODULE.read_jsonl(base / "distillation-review-queue.jsonl")
            }
            deep = rebuilt[first_pending["preprocess_record_id"]]
            self.assertEqual(deep["review_state"], "COMPLETE_DEEP_REVIEW")
            self.assertFalse(deep["action_required"])
            self.assertFalse(deep["automatic_execution_allowed"])
            self.assertEqual(deep["maturity"], "parse-verified")
            self.assertEqual((deep["batch_id"], deep["batch_position"]), before[deep["preprocess_record_id"]])
            for record_id, assignment in before.items():
                if assignment[0] is not None:
                    self.assertEqual(
                        (rebuilt[record_id]["batch_id"], rebuilt[record_id]["batch_position"]),
                        assignment,
                    )
            self.assertEqual(result["completed_batches"], {"high-value-001": 1})
            self.assertEqual(result["pending_batches"], {"high-value-001": 1})
            self.assertTrue(MODULE.validate_queue(base)["ok"])

    def test_tampered_receipt_review_hash_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self.build_fixture(base)
            MODULE.build_and_write(base, batch_size=2)
            first_pending = next(
                row for row in MODULE.read_jsonl(base / "distillation-review-queue.jsonl")
                if row["action_required"]
            )
            review_path, _ = self.write_deep_review_receipt(base, first_pending)
            with review_path.open("a", encoding="utf-8") as handle:
                handle.write("\n")
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                MODULE.build_queue(base, batch_size=2)

    def test_shard_self_validation_is_not_loaded_as_a_batch_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self.build_fixture(base)
            MODULE.build_and_write(base, batch_size=2)
            first_pending = next(
                row for row in MODULE.read_jsonl(base / "distillation-review-queue.jsonl")
                if row["action_required"]
            )
            _, receipt_path = self.write_deep_review_receipt(base, first_pending)
            self_validation = base / "manual-review" / "high-value-review-batch-001-a-self-validation.json"
            self_validation.write_text(receipt_path.read_text(encoding="utf-8"), encoding="utf-8")
            rebuilt, _ = MODULE.build_queue(base, batch_size=2)
            deep = next(row for row in rebuilt if row["preprocess_record_id"] == first_pending["preprocess_record_id"])
            self.assertEqual(deep["review_state"], "COMPLETE_DEEP_REVIEW")


if __name__ == "__main__":
    unittest.main()
