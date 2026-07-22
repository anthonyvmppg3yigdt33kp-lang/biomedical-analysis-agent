import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "validate_scrna_design.py"
SPEC = importlib.util.spec_from_file_location("validate_scrna_design", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


def sample(sample_id, donor_id, condition, batch, path, capture_id=None, raw_path=None):
    return {
        "sample_id": sample_id,
        "capture_id": capture_id or sample_id,
        "donor_id": donor_id,
        "condition": condition,
        "batch": batch,
        "input_type": "10x_mtx_dir",
        "path": str(path),
        "raw_droplet_path": str(raw_path) if raw_path else None,
    }


def base_config(samples):
    return {
        "project_id": "fixture",
        "organism": "Homo sapiens",
        "tissue": "peripheral blood",
        "platform": "10x-3prime",
        "reference_build": "GRCh38",
        "feature_namespace": "Ensembl release 110",
        "multiplexing": {"enabled": False, "method": "none", "demultiplexed": True},
        "samples": samples,
        "analysis": {
            "mode": "inferential",
            "condition_de": True,
            "composition": True,
            "ambient_rna": "soupx",
            "doublets": "scdblfinder",
            "normalization": "sctransform",
            "integration": "harmony",
            "trajectory": False,
            "trajectory_root": None,
            "cnv": False,
            "cnv_reference": None,
            "communication": False,
            "repeated_measures": False,
            "de_unit": "donor_pseudobulk",
        },
    }


class ValidateScrnaDesignTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.filtered = []
        self.raw = []
        for index in range(4):
            filtered = self.root / f"filtered_{index}"
            raw = self.root / f"raw_{index}"
            for folder in (filtered, raw):
                folder.mkdir()
                (folder / "matrix.mtx").write_text("%%MatrixMarket matrix coordinate integer general\n0 0 0\n", encoding="utf-8")
                (folder / "barcodes.tsv").write_text("", encoding="utf-8")
                (folder / "features.tsv").write_text("", encoding="utf-8")
            self.filtered.append(filtered)
            self.raw.append(raw)

    def tearDown(self):
        self.temp.cleanup()

    def valid_config(self):
        return base_config(
            [
                sample("A1", "D1", "control", "B1", self.filtered[0], raw_path=self.raw[0]),
                sample("A2", "D2", "control", "B2", self.filtered[1], raw_path=self.raw[1]),
                sample("B1", "D3", "treated", "B1", self.filtered[2], raw_path=self.raw[2]),
                sample("B2", "D4", "treated", "B2", self.filtered[3], raw_path=self.raw[3]),
            ]
        )

    def test_valid_design_compiles_ordered_dag_and_declarations(self):
        report = MODULE.compile_report(self.valid_config(), check_paths=True)
        self.assertEqual(report["status"], "valid_with_warnings")
        self.assertEqual(report["summary"]["errors"], 0)
        instance = report["workflow_instance"]
        ids = [stage["stage_id"] for stage in instance["stages"]]
        self.assertLess(ids.index("SC04_QC_PER_CAPTURE"), ids.index("SC05_DOUBLETS_PER_CAPTURE"))
        self.assertLess(ids.index("SC09_ANNOTATE_AND_REVIEW"), ids.index("SC10_PSEUDOBULK_DE"))
        recipe_text = json.dumps(instance["analysis_recipe"]).lower()
        for forbidden in ("install.packages", "biocmanager::install", "pip install", "conda install", "uv add"):
            self.assertNotIn(forbidden, recipe_text)
        self.assertFalse(instance["analysis_recipe"]["installation_commands_allowed"])

    def test_batch_condition_confounding_is_blocking(self):
        config = self.valid_config()
        config["samples"][0]["batch"] = "control_batch"
        config["samples"][1]["batch"] = "control_batch"
        config["samples"][2]["batch"] = "treated_batch"
        config["samples"][3]["batch"] = "treated_batch"
        report = MODULE.compile_report(config)
        codes = {item["code"] for item in report["issues"] if item["severity"] == "error"}
        self.assertIn("BATCH_CONDITION_CONFOUNDING", codes)
        self.assertNotIn("workflow_instance", report)

    def test_cell_level_de_and_missing_raw_droplets_are_blocking(self):
        config = self.valid_config()
        config["analysis"]["de_unit"] = "cell_level"
        config["samples"][0]["raw_droplet_path"] = None
        report = MODULE.compile_report(config)
        codes = {item["code"] for item in report["issues"] if item["severity"] == "error"}
        self.assertIn("PSEUDOREPLICATION", codes)
        self.assertIn("RAW_DROPLETS_REQUIRED", codes)

    def test_advanced_branches_require_root_and_cnv_reference(self):
        config = self.valid_config()
        config["analysis"]["trajectory"] = True
        config["analysis"]["cnv"] = True
        report = MODULE.compile_report(config)
        codes = {item["code"] for item in report["issues"] if item["severity"] == "error"}
        self.assertIn("TRAJECTORY_ROOT", codes)
        self.assertIn("CNV_REFERENCE", codes)

    def test_multiplexed_capture_requires_demultiplexing(self):
        config = self.valid_config()
        config["samples"][1]["capture_id"] = config["samples"][0]["capture_id"]
        config["multiplexing"] = {"enabled": True, "method": "hto", "demultiplexed": False}
        report = MODULE.compile_report(config)
        codes = {item["code"] for item in report["issues"] if item["severity"] == "error"}
        self.assertIn("DEMULTIPLEX_REQUIRED", codes)

    def test_cli_writes_report_and_returns_zero_for_valid_design(self):
        config_path = self.root / "design.json"
        output_path = self.root / "report.json"
        config_path.write_text(json.dumps(self.valid_config()), encoding="utf-8")
        code = MODULE.main(["--config", str(config_path), "--output", str(output_path), "--check-paths"])
        self.assertEqual(code, 0)
        on_disk = json.loads(output_path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["status"], "valid_with_warnings")


if __name__ == "__main__":
    unittest.main()
