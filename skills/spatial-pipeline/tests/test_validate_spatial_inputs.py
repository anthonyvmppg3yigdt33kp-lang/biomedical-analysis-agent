from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = SKILL_ROOT / "scripts" / "validate_spatial_inputs.py"
sys.path.insert(0, str(SCRIPT.parent))

from validate_spatial_inputs import validate_manifest, validate_table  # noqa: E402


class ManifestValidationTests(unittest.TestCase):
    def write_manifest(self, root: Path, payload: dict) -> Path:
        path = root / "manifest.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def base_manifest(self) -> dict:
        return {
            "schema_version": "1.0",
            "platform": "visium",
            "assay_class": "capture",
            "assay_unit": "spot",
            "species": "human",
            "coordinate_unit": "pixel",
            "requested_modules": ["domains", "svg"],
            "samples": [
                {
                    "sample_id": "S01",
                    "subject_id": "P01",
                    "section_id": "secA",
                    "input_root": "vendor/S01",
                }
            ],
        }

    def test_valid_visium_manifest_without_path_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = validate_manifest(self.write_manifest(root, self.base_manifest()))
            self.assertTrue(result.ok, result.to_dict())
            self.assertEqual(result.summary["subjects"], 1)

    def test_path_probe_resolves_relative_to_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "vendor" / "S01").mkdir(parents=True)
            result = validate_manifest(
                self.write_manifest(root, self.base_manifest()), check_paths=True
            )
            self.assertTrue(result.ok, result.to_dict())

    def test_platform_and_unit_mismatch_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = self.base_manifest()
            payload.update({"platform": "xenium", "assay_class": "capture", "assay_unit": "spot"})
            result = validate_manifest(self.write_manifest(root, payload))
            codes = {item.code for item in result.issues}
            self.assertFalse(result.ok)
            self.assertIn("assay_class_mismatch", codes)
            self.assertIn("assay_unit_mismatch", codes)

    def test_imaging_cell_deconvolution_is_not_silent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = self.base_manifest()
            payload.update(
                {
                    "platform": "xenium",
                    "assay_class": "imaging",
                    "assay_unit": "cell",
                    "requested_modules": ["deconvolution"],
                }
            )
            result = validate_manifest(self.write_manifest(root, payload))
            self.assertFalse(result.ok)
            self.assertIn("deconvolution_unit_mismatch", {item.code for item in result.issues})

    def test_group_contrast_requires_subjects_groups_and_group_labels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = self.base_manifest()
            payload["requested_modules"] = ["group_contrast"]
            result = validate_manifest(self.write_manifest(root, payload))
            codes = {item.code for item in result.issues}
            self.assertFalse(result.ok)
            self.assertTrue(
                {"missing_group", "insufficient_independent_units", "insufficient_groups"}.issubset(codes)
            )

    def test_explicit_overlay_requires_image_and_transform(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = self.base_manifest()
            payload["requested_modules"] = ["image_overlay"]
            payload["samples"][0].pop("input_root")
            payload["samples"][0]["matrix_path"] = "matrix.csv"
            payload["samples"][0]["coordinates_path"] = "coords.csv"
            result = validate_manifest(self.write_manifest(root, payload))
            self.assertFalse(result.ok)
            missing = [item for item in result.issues if item.code == "missing_overlay_input"]
            self.assertEqual(len(missing), 2)

    def test_path_probe_also_validates_explicit_coordinate_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "matrix.csv").write_text("feature,A\nG1,1\n", encoding="utf-8")
            (root / "coordinates.csv").write_text(
                "sample_id,unit_id,x,y\nS01,A,1,2\n", encoding="utf-8"
            )
            payload = self.base_manifest()
            payload["samples"][0].pop("input_root")
            payload["samples"][0]["matrix_path"] = "matrix.csv"
            payload["samples"][0]["coordinates_path"] = "coordinates.csv"
            result = validate_manifest(self.write_manifest(root, payload), check_paths=True)
            self.assertFalse(result.ok)
            self.assertIn(
                "coordinates_missing_columns", {item.code for item in result.issues}
            )


class TableValidationTests(unittest.TestCase):
    def test_valid_coordinate_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "coordinates.csv"
            path.write_text(
                "sample_id,unit_id,x,y,coordinate_system\nS01,A,1.0,2.5,image_px\nS01,B,3,4,image_px\n",
                encoding="utf-8",
            )
            result = validate_table(path, "coordinates")
            self.assertTrue(result.ok, result.to_dict())
            self.assertEqual(result.summary["rows"], 2)

    def test_coordinate_duplicates_and_nonfinite_values_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "coordinates.tsv"
            path.write_text(
                "sample_id\tunit_id\tx\ty\tcoordinate_system\n"
                "S01\tA\tNaN\t2\tarray\n"
                "S01\tA\t1\tinf\tarray\n",
                encoding="utf-8",
            )
            result = validate_table(path, "coordinates")
            codes = {item.code for item in result.issues}
            self.assertFalse(result.ok)
            self.assertIn("duplicate_identity", codes)
            self.assertIn("non_finite_coordinate", codes)

    def test_sample_metadata_requires_subject_and_section(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metadata.csv"
            path.write_text("sample_id,group\nS01,case\n", encoding="utf-8")
            result = validate_table(path, "metadata")
            self.assertFalse(result.ok)
            self.assertIn("metadata_level_ambiguous", {item.code for item in result.issues})

    def test_cli_returns_nonzero_and_json_for_invalid_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.csv"
            path.write_text("sample_id,unit_id,x,y\nS01,A,1,2\n", encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, str(SCRIPT), "table", str(path), "--kind", "coordinates", "--json"],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 1)
            payload = json.loads(completed.stdout)
            self.assertFalse(payload["ok"])


class BundledContractTests(unittest.TestCase):
    def test_artifact_schema_is_valid_json_and_covers_all_stages(self) -> None:
        schema_path = SKILL_ROOT / "references" / "spatial-artifact-contract.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        stages = schema["properties"]["stage_id"]["enum"]
        self.assertEqual(stages[0], "S00_INTAKE")
        self.assertEqual(stages[-1], "S95_VISUALIZE_INTERPRET")
        self.assertEqual(len(stages), 11)
        self.assertIn("sha256", schema["required"])
        self.assertFalse(schema["additionalProperties"])


if __name__ == "__main__":
    unittest.main()
