import importlib.util
import json
import tempfile
import unittest
import zipfile
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "build_public_core.py"
SPEC = importlib.util.spec_from_file_location("build_public_core", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class PublicCoreTests(unittest.TestCase):
    def _minimal_skill(self, root: Path) -> None:
        (root / "references").mkdir(parents=True)
        (root / "scripts").mkdir()
        (root / "assets" / "private-corpus-index").mkdir(parents=True)
        (root / "SKILL.md").write_text("---\nname: test-skill\ndescription: test\n---\n", encoding="utf-8")
        (root / "references" / "corpus-sources.json").write_text('{"path":"D:\\\\private"}', encoding="utf-8")
        (root / "references" / "corpus-sources.example.json").write_text('{"path":"REPLACE_ME"}', encoding="utf-8")
        (root / "assets" / "private-corpus-index" / "raw.jsonl").write_text("private article", encoding="utf-8")
        (root / "scripts" / "engine.py").write_text("print('ok')\n", encoding="utf-8")

    def test_private_payload_and_live_config_are_excluded(self):
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            skill = temp / "skill"
            self._minimal_skill(skill)
            archive = temp / "public.zip"
            report = MODULE.build_public_core(skill, archive, ["private article"])
            self.assertTrue(report["ok"])
            with zipfile.ZipFile(archive) as handle:
                names = set(handle.namelist())
                self.assertFalse(any("private-corpus-index" in name for name in names))
                config = handle.read("biomedical-analysis-agent/references/corpus-sources.json").decode("utf-8")
                self.assertIn("REPLACE_ME", config)
                self.assertNotIn("D:\\private", config)

    def test_leakage_scan_blocks_forbidden_literal(self):
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            skill = temp / "skill"
            self._minimal_skill(skill)
            (skill / "scripts" / "engine.py").write_text("PRIVATE_TOKEN\n", encoding="utf-8")
            with self.assertRaises(MODULE.DistributionError):
                MODULE.build_public_core(skill, temp / "public.zip", ["PRIVATE_TOKEN"])

    def test_private_windows_home_path_is_blocked(self):
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            skill = temp / "skill"
            self._minimal_skill(skill)
            private_home = "C:" + r"\Users\Someone\secret"
            (skill / "scripts" / "engine.py").write_text(private_home, encoding="utf-8")
            with self.assertRaises(MODULE.DistributionError):
                MODULE.build_public_core(skill, temp / "public.zip", [])


if __name__ == "__main__":
    unittest.main()
