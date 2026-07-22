import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "materialize_native_figure_reviews.py"
SPEC = importlib.util.spec_from_file_location("materialize_native_figure_reviews", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)
INDEX = ROOT / "assets" / "private-corpus-index"
MANUAL = INDEX / "manual-review"
PRIVATE_NATIVE_EVIDENCE_AVAILABLE = (
    (INDEX / "native-figure-review" / "native-figure-review-batch-001.json").is_file()
    and (MANUAL / "native-figure-review-batch-001-observations.json").is_file()
)


@unittest.skipUnless(PRIVATE_NATIVE_EVIDENCE_AVAILABLE, "Private native-review evidence is intentionally absent from a public-core clone")
class ManualNativeFigureReviewTests(unittest.TestCase):
    def test_first_manual_batch_is_source_bound_and_schema_valid(self):
        report = MODULE.validate_records(
            INDEX,
            MANUAL / "native-figure-review-batch-001-observations.json",
            MANUAL / "native-figure-review-batch-001-pixels.jsonl",
            MANUAL / "native-figure-review-batch-001-contexts.jsonl",
        )
        self.assertTrue(report["ok"], report["errors"])
        self.assertEqual(report["native_reviewed_pixels"], 16)
        self.assertEqual(report["code_binding_status"], {"confirmed": 14, "inferred": 1, "not_applicable": 1})

    def test_unknown_generator_hash_is_rejected(self):
        observations = json.loads((MANUAL / "native-figure-review-batch-001-observations.json").read_text(encoding="utf-8"))
        observations["items"][0]["generator_code_sha256"] = "0" * 64
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "observations.json"
            path.write_text(json.dumps(observations, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(MODULE.NativeReviewError):
                MODULE.build_records(INDEX, path)


if __name__ == "__main__":
    unittest.main()
