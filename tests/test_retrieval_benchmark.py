from __future__ import annotations

import sys
import unittest
from collections import Counter
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from evaluate_retrieval_benchmark import evaluate_cases, load_cases  # noqa: E402


class RetrievalBenchmarkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.path = SKILL_ROOT / "references" / "retrieval-benchmark.jsonl"
        cls.cases = load_cases(cls.path)

    def test_fixed_gold_set_shape(self) -> None:
        self.assertEqual(len(self.cases), 180)
        counts = Counter(case["stratum"] for case in self.cases)
        self.assertEqual(
            counts,
            {
                "visualization": 30,
                "single-cell": 30,
                "spatial-transcriptomics": 30,
                "bulk-proteomics": 30,
                "multi-omics": 30,
                "literature-methodology": 30,
            },
        )

    def test_gold_set_is_deterministic_and_unique(self) -> None:
        ids = [case["id"] for case in self.cases]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(self.cases, load_cases(self.path))

    def test_acceptance_thresholds(self) -> None:
        report = evaluate_cases(self.cases)
        overall = report["overall"]
        self.assertGreaterEqual(overall["top1_accuracy"], 0.95)
        self.assertGreaterEqual(overall["top3_accuracy"], 0.99)
        self.assertEqual(overall["forbidden_substitutions"], 0)
        self.assertEqual(overall["top3_allowlist_accuracy"], 1.0)
        self.assertEqual(overall["gate_coverage_accuracy"], 1.0)


if __name__ == "__main__":
    unittest.main()
