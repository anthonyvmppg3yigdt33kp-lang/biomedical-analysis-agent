import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "validate_gold_reviews.py"
SPEC = importlib.util.spec_from_file_location("validate_gold_reviews", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)
PRIVATE_GOLD_INDEX = Path(__file__).parents[1] / "assets" / "private-corpus-index"
PRIVATE_GOLD_EVIDENCE_AVAILABLE = (PRIVATE_GOLD_INDEX / "gold-set.json").is_file()


class GoldReviewHelperTests(unittest.TestCase):
    def test_complete_parse_evidence(self):
        record = {"review_evidence": {"parse_check": "R parse 33/33"}}
        self.assertTrue(MODULE._parse_complete(record))
        record["review_evidence"]["parse_check"] = "R parse 17/38"
        self.assertFalse(MODULE._parse_complete(record))

    def test_two_supported_figure_evidence_shapes(self):
        first = {"review_evidence": {"native_figures": [{"figure_id": "f1", "status": "native-reviewed"}]}}
        second = {"review_evidence": {"native_reviewed_figure_id": "f2", "native_reviewed_figure_sha256": "a" * 64, "figure_review_scope": "viewed_unresolved"}}
        self.assertEqual(MODULE._reviewed_figures(first)[0]["figure_id"], "f1")
        self.assertEqual(MODULE._reviewed_figures(second)[0]["figure_id"], "f2")
        self.assertTrue(MODULE._unresolved({"code_figure_consistency": "unresolved", "review_evidence": {}}, []))

    @unittest.skipUnless(PRIVATE_GOLD_EVIDENCE_AVAILABLE, "Private gold-review evidence is intentionally absent from a public-core clone")
    def test_metric_renamed_and_legacy_alias_is_explicitly_deprecated(self):
        report = MODULE.validate_reviews(PRIVATE_GOLD_INDEX)
        self.assertTrue(report["ok"], report["errors"])
        self.assertEqual(
            report["representative_figures_declared_reviewed"],
            report["representative_figures_native_viewed"],
        )
        deprecated = report["deprecated_metrics"]["representative_figures_native_viewed"]
        self.assertEqual(deprecated["replacement"], "representative_figures_declared_reviewed")
        self.assertIn("declaration", deprecated["reason"].casefold())


if __name__ == "__main__":
    unittest.main()
