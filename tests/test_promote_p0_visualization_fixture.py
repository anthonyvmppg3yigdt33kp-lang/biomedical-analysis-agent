import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "promote_p0_visualization_fixture.py"
SPEC = importlib.util.spec_from_file_location("promote_p0_visualization_fixture", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class PromoteP0VisualizationFixtureTests(unittest.TestCase):
    def test_artifact_contract_is_hash_bound_and_scientifically_scoped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            figure = root / "06_figures" / "final" / "composition.png"
            figure.parent.mkdir(parents=True)
            figure.write_bytes(b"native-pixels")
            contract = MODULE.artifact_contract(root, figure)
            self.assertEqual(contract["artifact_type"], "figure")
            self.assertEqual(contract["unit"], "sample")
            self.assertEqual(contract["claim_role"], "descriptive")
            self.assertEqual(contract["relative_path"], "06_figures/final/composition.png")
            self.assertEqual(contract["sha256"], MODULE.sha256_file(figure))

    def test_unsuccessful_fixture_cannot_be_promoted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = root / "fixture"
            fixture.mkdir()
            (fixture / "teaching_case_report.json").write_text(
                json.dumps({"ok": False, "error": "synthetic failure"}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "not successful"):
                MODULE.promote(root / "formal-run", fixture)


if __name__ == "__main__":
    unittest.main()
