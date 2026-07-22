import ast
import hashlib
import json
import re
import tempfile
import unittest
from pathlib import Path

import jsonschema


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
import sys

sys.path.insert(0, str(SCRIPTS))

from validate_p0_teaching_cases import EXPECTED_DOMAINS, validate_registry  # noqa: E402


REGISTRY = SKILL_ROOT / "references" / "p0-teaching-cases.json"
SCHEMA = SKILL_ROOT / "references" / "schemas" / "p0-teaching-case.schema.json"
LOCAL = SKILL_ROOT / "assets" / "private-corpus-index" / "p0-teaching-case-local-availability.json"
GSE185948_METADATA = SKILL_ROOT / "references" / "p0-single-cell-gse185948-metadata.json"
VISUALIZATION_PIPELINE = SKILL_ROOT / "assets" / "teaching-cases" / "p0-visualization-kinneyh" / "run_pipeline.py"
TEACHING_ROOT = SKILL_ROOT / "assets" / "teaching-cases"
BULK_ASSET = TEACHING_ROOT / "p0-bulk-rna-airway"
SINGLE_CELL_ASSET = TEACHING_ROOT / "p0-single-cell-gse185948"
PROTEOMICS_ASSET = TEACHING_ROOT / "p0-proteomics-dep-ubilength"
MULTIOMICS_ASSET = TEACHING_ROOT / "p0-multiomics-cll-mofaflex"


class P0TeachingCaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
        cls.schema = json.loads(SCHEMA.read_text(encoding="utf-8"))

    def test_self_contained_schema_and_registry_validate(self):
        self.assertNotIn("knowledge-objects.schema.json", json.dumps(self.schema))
        jsonschema.Draft202012Validator(
            self.schema,
            format_checker=jsonschema.FormatChecker(),
        ).validate(self.registry)
        report = validate_registry(REGISTRY, SCHEMA, None)
        self.assertTrue(report["ok"], report["errors"])
        self.assertEqual(report["case_count"], 7)
        self.assertEqual(report["public_metadata_binding_count"], 1)
        self.assertEqual(report["verified_public_metadata_files"], 1)

    def test_exact_p0_domain_coverage_and_no_promoted_maturity(self):
        cases = self.registry["cases"]
        self.assertEqual({case["domain"] for case in cases}, EXPECTED_DOMAINS)
        self.assertEqual(len({case["case_id"] for case in cases}), 7)
        self.assertTrue(all(case["execution_status"] == "not-executed" for case in cases))
        self.assertTrue(all(case["maturity"] in {"raw-extracted", "normalized"} for case in cases))
        self.assertTrue(all(case["environment"]["install_authorized"] is False for case in cases))

    def test_public_registry_contains_only_stable_locator_refs(self):
        rendered = json.dumps(self.registry, ensure_ascii=False)
        self.assertIsNone(re.search(r"(?i)(?:[a-z]:[\\/]|\\users\\|/home/)", rendered))
        refs = {
            ref
            for case in self.registry["cases"]
            for ref in case["availability"]["local_locator_refs"]
        }
        self.assertTrue(refs)
        self.assertTrue(all(ref.startswith("local:") for ref in refs))

    def test_private_locator_map_matches_public_integrity_when_available(self):
        if not LOCAL.is_file():
            self.skipTest("Private locator map is intentionally absent from a public-core clone")
        report = validate_registry(REGISTRY, SCHEMA, LOCAL, verify_local=False)
        self.assertTrue(report["ok"], report["errors"])
        self.assertEqual(
            report["private_locator_count"],
            report["public_locator_ref_count"] + report["execution_only_private_locator_count"],
        )

    def test_gse185948_accession_assay_and_plan_only_boundary_are_frozen(self):
        rendered = json.dumps(self.registry, ensure_ascii=False)
        self.assertNotIn("GSE185809", rendered)
        case = next(item for item in self.registry["cases"] if item["domain"] == "single-cell")
        self.assertEqual(case["case_id"], "p0-single-cell-gse185948")
        self.assertIn("GSE185948", case["governance"]["source_identifiers"])
        self.assertEqual(case["governance"]["citation_status"], "verified")
        self.assertEqual(case["governance"]["license_status"], "pending")
        self.assertEqual(case["governance"]["redistribution_status"], "pending")
        self.assertEqual(case["workflow_plan"]["allowed_mode"], "plan")
        self.assertEqual(case["execution_status"], "not-executed")
        self.assertFalse(case["environment"]["install_authorized"])
        self.assertEqual(case["statistical_contract"]["measurement_unit"], "nucleus")
        qc_stage = next(stage for stage in case["workflow_plan"]["stages"] if stage["stage_id"] == "platform-aware-qc")
        self.assertIn("nucleus-aware", qc_stage["purpose"])
        self.assertIn("without imposing canned whole-cell mitochondrial cutoffs", qc_stage["purpose"])

    def test_gse185948_public_metadata_sidecar_is_hash_bound_and_complete(self):
        case = next(item for item in self.registry["cases"] if item["case_id"] == "p0-single-cell-gse185948")
        metadata = json.loads(GSE185948_METADATA.read_text(encoding="utf-8"))
        digest = hashlib.sha256(GSE185948_METADATA.read_bytes()).hexdigest()
        self.assertEqual(case["public_metadata"]["sha256"], digest)
        self.assertEqual(metadata["case_id"], case["case_id"])
        self.assertEqual(metadata["series"]["accession"], "GSE185948")
        self.assertEqual(metadata["series"]["assay"], "single-nucleus RNA-seq")
        self.assertEqual(metadata["series"]["source_material"], "nuclear RNA")
        self.assertEqual(metadata["series"]["processing"], "Cell Ranger 6.0.0 with --include-introns")
        self.assertEqual(metadata["series"]["genome_build"], "GRCh38")
        self.assertEqual({sample["sample_accession"] for sample in metadata["samples"]}, {f"GSM562769{i}" for i in range(5)})
        self.assertEqual([(sample["age_years"], sample["sex"], sample["egfr_ml_min_1_73m2"]) for sample in metadata["samples"]], [(54, "male", 58), (62, "male", 61), (61, "female", 69), (50, "male", 78), (52, "female", 98)])
        self.assertEqual(metadata["citation"]["pmid"], "36310237")
        self.assertEqual(metadata["citation"]["doi"], "10.1038/s41467-022-34255-z")

    def test_semantic_validator_rejects_public_metadata_hash_mismatch(self):
        mutated = json.loads(json.dumps(self.registry))
        case = next(item for item in mutated["cases"] if item["case_id"] == "p0-single-cell-gse185948")
        case["public_metadata"]["sha256"] = "0" * 64
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            registry_path = temporary_path / "p0-teaching-cases.json"
            registry_path.write_text(json.dumps(mutated, ensure_ascii=False), encoding="utf-8")
            (temporary_path / GSE185948_METADATA.name).write_bytes(GSE185948_METADATA.read_bytes())
            report = validate_registry(registry_path, SCHEMA, None)
        self.assertFalse(report["ok"])
        self.assertTrue(any("public_metadata_hash_mismatch" in error for error in report["errors"]))

    def test_semantic_validator_rejects_nonplan_gse185948_case(self):
        mutated = json.loads(json.dumps(self.registry))
        case = next(item for item in mutated["cases"] if item["case_id"] == "p0-single-cell-gse185948")
        case["workflow_plan"]["allowed_mode"] = "explain"
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            registry_path = temporary_path / "p0-teaching-cases.json"
            registry_path.write_text(json.dumps(mutated, ensure_ascii=False), encoding="utf-8")
            (temporary_path / GSE185948_METADATA.name).write_bytes(GSE185948_METADATA.read_bytes())
            report = validate_registry(registry_path, SCHEMA, None)
        self.assertFalse(report["ok"])
        self.assertIn("p0-single-cell-gse185948:allowed_mode_must_be_plan", report["errors"])

    def test_private_locator_names_track_corrected_snrna_accession(self):
        if not LOCAL.is_file():
            self.skipTest("Private locator map is intentionally absent from a public-core clone")
        local = json.loads(LOCAL.read_text(encoding="utf-8"))
        refs = set(local["locators"])
        self.assertFalse(any("gse185809" in ref for ref in refs))
        self.assertEqual(len([ref for ref in refs if ref.startswith("local:p0.snrna.gse185948.")]), 5)

    def test_semantic_validator_rejects_forward_dependency(self):
        mutated = json.loads(json.dumps(self.registry))
        first = mutated["cases"][0]["workflow_plan"]["stages"][0]
        first["depends_on"] = [mutated["cases"][0]["workflow_plan"]["stages"][1]["stage_id"]]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "registry.json"
            path.write_text(json.dumps(mutated, ensure_ascii=False), encoding="utf-8")
            report = validate_registry(path, SCHEMA, None)
        self.assertFalse(report["ok"])
        self.assertTrue(any("dependency_not_prior" in error for error in report["errors"]))

    def test_schema_rejects_false_data_verified_promotion(self):
        mutated = json.loads(json.dumps(self.registry))
        mutated["cases"][0]["maturity"] = "data-verified"
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "registry.json"
            path.write_text(json.dumps(mutated, ensure_ascii=False), encoding="utf-8")
            report = validate_registry(path, SCHEMA, None)
        self.assertFalse(report["ok"])
        self.assertTrue(any("maturity" in error for error in report["errors"]))

    def test_visualization_figure_notes_are_input_derived(self):
        source = VISUALIZATION_PIPELINE.read_text(encoding="utf-8")
        self.assertNotIn("across five samples", source)
        self.assertNotIn("56,179 measured cells", source)
        self.assertIn('sample_count = int(profile["sample_count"])', source)
        self.assertIn('measured_cell_count = int(profile["rows"])', source)
        self.assertIn('"figure_notes_derived_from_input_profile": True', source)

    def test_bulk_teaching_asset_uses_materialized_airway_dimensions(self):
        rendered = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (BULK_ASSET / "README.md", BULK_ASSET / "case-spec.yaml", BULK_ASSET / "run_pipeline.R", BULK_ASSET / "verify_outputs.py")
        )
        self.assertIn("63677", rendered)
        self.assertIn("63,677", rendered)
        self.assertNotIn("64102", rendered)
        self.assertNotIn("64,102", rendered)

    def test_single_cell_teaching_asset_is_complete_and_preserves_scientific_boundaries(self):
        required = {
            "README.md", "case-spec.yaml", "input-config.example.json",
            "run_case.py", "run_pipeline.py", "verify_outputs.py",
        }
        self.assertTrue(required.issubset({path.name for path in SINGLE_CELL_ASSET.iterdir() if path.is_file()}))
        spec = (SINGLE_CELL_ASSET / "case-spec.yaml").read_text(encoding="utf-8")
        example = json.loads((SINGLE_CELL_ASSET / "input-config.example.json").read_text(encoding="utf-8"))
        pipeline = (SINGLE_CELL_ASSET / "run_pipeline.py").read_text(encoding="utf-8")
        self.assertIn("total_filtered_barcodes: 56728", spec)
        self.assertIn("analyzed_input_barcodes: 5000", spec)
        self.assertIn("doublet_filter_policy: all-donors-or-none", spec)
        self.assertIn("donor, library and batch are one-to-one", spec)
        self.assertEqual(len(example["inputs"]), 5)
        self.assertEqual(sum(item["expected_barcodes"] for item in example["inputs"]), 56728)
        self.assertEqual({item["expected_features"] for item in example["inputs"]}, {36601})
        self.assertIn('FIGURE_SCOPE_SUFFIX = ""', pipeline)
        self.assertIn('"no integration; unintegrated PCA retained after confounding review"', pipeline)
        for name in ("run_case.py", "run_pipeline.py", "verify_outputs.py"):
            ast.parse((SINGLE_CELL_ASSET / name).read_text(encoding="utf-8"), filename=name)

    def test_proteomics_teaching_asset_is_complete_and_version_boundary_is_explicit(self):
        required = {"README.md", "case-spec.yaml", "params.yaml", "run_case.py", "run_pipeline.R", "verify_outputs.py"}
        self.assertTrue(required.issubset({path.name for path in PROTEOMICS_ASSET.iterdir() if path.is_file()}))
        readme = (PROTEOMICS_ASSET / "README.md").read_text(encoding="utf-8")
        spec = (PROTEOMICS_ASSET / "case-spec.yaml").read_text(encoding="utf-8")
        pipeline = (PROTEOMICS_ASSET / "run_pipeline.R").read_text(encoding="utf-8")
        self.assertIn("DEP 1.32.0 source", readme)
        self.assertIn("DEP 1.31.0", readme)
        self.assertIn("source_runtime_version_identical: false", spec)
        self.assertIn('expected_versions <- c(DEP = "1.31.0"', pipeline)
        self.assertIn('private_materialized <- file.path(p10, "source-materialized")', pipeline)
        for installer in ("install.packages(", "BiocManager::install(", "pak::pkg_install("):
            self.assertNotIn(installer, pipeline)
        for name in ("run_case.py", "verify_outputs.py"):
            ast.parse((PROTEOMICS_ASSET / name).read_text(encoding="utf-8"), filename=name)

    def test_multiomics_teaching_asset_is_complete_and_preserves_patient_union(self):
        required = {
            "README.md", "case-spec.yaml", "params.yaml", "source-spec.example.json",
            "run_case.py", "run_pipeline.py", "render_optimized_figures.py", "verify_outputs.py",
        }
        self.assertTrue(required.issubset({path.name for path in MULTIOMICS_ASSET.iterdir() if path.is_file()}))
        spec = (MULTIOMICS_ASSET / "case-spec.yaml").read_text(encoding="utf-8")
        pipeline = (MULTIOMICS_ASSET / "run_pipeline.py").read_text(encoding="utf-8")
        self.assertIn("patient_union: 200", spec)
        self.assertIn("complete_four_view_patients: 121", spec)
        self.assertIn('EXPECTED_DATA_SHA = "1da99d3967f8616adcee2bcea0157c30acf34c46884902343518e9634ec2f7ee"', pipeline)
        self.assertIn("Hungarian assignment maximizing absolute Spearman", pipeline)
        self.assertNotIn("tokens truncated", pipeline)
        for name in ("run_case.py", "run_pipeline.py", "render_optimized_figures.py", "verify_outputs.py"):
            ast.parse((MULTIOMICS_ASSET / name).read_text(encoding="utf-8"), filename=name)

    def test_new_teaching_assets_are_path_independent_and_install_free(self):
        for root in (SINGLE_CELL_ASSET, PROTEOMICS_ASSET, MULTIOMICS_ASSET):
            rendered = "\n".join(
                path.read_text(encoding="utf-8")
                for path in root.rglob("*")
                if path.is_file() and path.suffix.lower() in {".md", ".py", ".r", ".json", ".yaml"}
            )
            self.assertIsNone(re.search(r"(?i)(?<![a-z])(?:[a-z]:[\\/]|\\users\\|/home/)", rendered))
        for path in (
            SINGLE_CELL_ASSET / "run_pipeline.py",
            PROTEOMICS_ASSET / "run_pipeline.R",
            MULTIOMICS_ASSET / "run_pipeline.py",
        ):
            source = path.read_text(encoding="utf-8")
            for installer in ("install.packages(", "BiocManager::install(", "pak::pkg_install(", "conda install", "pip install"):
                self.assertNotIn(installer, source)


if __name__ == "__main__":
    unittest.main()
