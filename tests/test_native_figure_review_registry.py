import copy
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "build_native_figure_review_registry.py"
SPEC = importlib.util.spec_from_file_location("build_native_figure_review_registry", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class NativeFigureReviewRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.index = Path(self.temporary.name) / "index"
        self.output = self.index / "native-figure-review"
        (self.index / "manual-review").mkdir(parents=True)
        self.figure_a = "figure-" + "a" * 20
        self.figure_b = "figure-" + "b" * 20
        self.bundle_id = "flow-" + "c" * 20
        self.pixel_sha = "d" * 64
        figures = [
            {
                "figure_card_id": self.figure_a,
                "source_bundle_id": self.bundle_id,
                "sha256": self.pixel_sha,
                "dimensions": {"width": 800, "height": 600},
                "private_source_locator": "D:\\readonly\\a.png",
                "code_link": {"alt": "figure", "ordinal": 1, "source_line": 10, "target": "images/a.png"},
            },
            {
                "figure_card_id": self.figure_b,
                "source_bundle_id": self.bundle_id,
                "sha256": self.pixel_sha,
                "dimensions": {"width": 800, "height": 600},
                "private_source_locator": "D:\\readonly\\b.png",
                "code_link": {"alt": "figure", "ordinal": 2, "source_line": 20, "target": "images/b.png"},
            },
        ]
        bundle = {
            "bundle_id": self.bundle_id,
            "ordered_code_files": [{"sha256": "e" * 64}],
            "article": {"fenced_code_blocks": [{"sha256": "f" * 64}]},
        }
        self._write_jsonl(self.index / "figure-cards.jsonl", figures)
        self._write_jsonl(self.index / "source-flow-bundles.jsonl", [bundle])
        self._write_jsonl(
            self.index / "manual-review" / "gold-review-batch-a.jsonl",
            [{
                "bundle_id": self.bundle_id,
                "review_evidence": {
                    "native_figures": [{
                        "figure_id": self.figure_a,
                        "status": "native-reviewed",
                        "code_block_sha256": "1" * 64,
                    }]
                },
            }],
        )
        self._write_jsonl(self.index / "manual-review" / "gold-review-batch-b.jsonl", [])

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def _write_jsonl(path, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    def test_groups_duplicate_pixels_but_keeps_contexts_separate(self):
        pixels, contexts, stats = MODULE.construct_records(self.index)
        self.assertEqual(len(pixels), 1)
        self.assertEqual(pixels[0]["occurrence_count"], 2)
        self.assertIn("duplicate_pixel_payload", pixels[0]["metadata_flags"])
        self.assertEqual(len(contexts), 2)
        self.assertEqual({row["pixel_review_id"] for row in contexts}, {pixels[0]["pixel_review_id"]})
        self.assertEqual(stats["duplicate_clusters"], 1)
        self.assertEqual(stats["figures_in_duplicate_clusters"], 2)

    def test_legacy_declaration_does_not_promote_new_review_state(self):
        pixels, contexts, _ = MODULE.construct_records(self.index)
        context = next(row for row in contexts if row["figure_id"] == self.figure_a)
        self.assertEqual(pixels[0]["inspection_status"], "pending")
        self.assertEqual(context["native_review_status"], "pending")
        self.assertEqual(context["code_binding_status"], "pending")
        self.assertEqual(context["legacy_declaration"]["trust_level"], "declaration_only")
        self.assertEqual(context["legacy_declaration"]["declared_code_block_sha256"], "1" * 64)

    def test_schema_and_deterministic_build_validate(self):
        original_batch = MODULE.FIRST_BATCH
        MODULE.FIRST_BATCH = [(self.figure_a, "legacy_code_hash_declared")]
        try:
            MODULE.build_registry(self.index, self.output)
            report = MODULE.validate_registry(self.index, self.output)
            self.assertTrue(report["ok"], report["errors"])
            pixel = MODULE.read_jsonl(self.output / "pixel-artifact-review-queue.jsonl")[0]
            context = MODULE.read_jsonl(self.output / "figure-context-review-queue.jsonl")[0]
            schemas = ROOT / "references" / "native-review-schemas"
            pixel_schema = json.loads((schemas / "pixel-artifact-review.schema.json").read_text(encoding="utf-8"))
            context_schema = json.loads((schemas / "figure-context-review.schema.json").read_text(encoding="utf-8"))
            Draft202012Validator(pixel_schema, format_checker=FormatChecker()).validate(pixel)
            Draft202012Validator(context_schema, format_checker=FormatChecker()).validate(context)

            invalid = copy.deepcopy(context)
            invalid["native_review_status"] = "native_reviewed"
            errors = list(Draft202012Validator(context_schema, format_checker=FormatChecker()).iter_errors(invalid))
            self.assertTrue(errors, "native_reviewed without reviewer/evidence must fail schema validation")
            batch = json.loads((self.output / "native-figure-review-batch-001.json").read_text(encoding="utf-8"))
            self.assertEqual(batch["items"][0]["native_review_status"], "pending")
        finally:
            MODULE.FIRST_BATCH = original_batch

    def test_no_code_is_not_applicable_not_pending(self):
        bundle_path = self.index / "source-flow-bundles.jsonl"
        self._write_jsonl(bundle_path, [{"bundle_id": self.bundle_id, "ordered_code_files": [], "article": {"fenced_code_blocks": []}}])
        _, contexts, _ = MODULE.construct_records(self.index)
        self.assertTrue(all(row["code_binding_status"] == "not_applicable" for row in contexts))
        self.assertTrue(all(row["reproduction_applicability"] == "not_applicable" for row in contexts))
        self.assertTrue(all(row["code_binding_not_applicable_reason"] for row in contexts))


if __name__ == "__main__":
    unittest.main()
