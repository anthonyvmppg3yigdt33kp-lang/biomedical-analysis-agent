import json
import tempfile
import unittest
from pathlib import Path

import jsonschema


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
import sys

sys.path.insert(0, str(SCRIPTS))

from validate_p0_teaching_case_executions import validate_execution_registry  # noqa: E402
from register_p0_teaching_case_execution import register_execution  # noqa: E402


REGISTRY = SKILL_ROOT / "assets" / "private-corpus-index" / "p0-teaching-case-executions.json"
SCHEMA = SKILL_ROOT / "references" / "schemas" / "p0-teaching-case-execution.schema.json"
CANDIDATES = SKILL_ROOT / "references" / "p0-teaching-cases.json"
LOCATORS = SKILL_ROOT / "assets" / "private-corpus-index" / "p0-teaching-case-local-availability.json"
PRIVATE_EXECUTION_EVIDENCE_AVAILABLE = REGISTRY.is_file() and LOCATORS.is_file()


def _synthetic_single_cell_execution():
    digest = "a" * 64

    def hashed(relative_path):
        return {"relative_path": relative_path, "sha256": digest, "size_bytes": 10}

    return {
        "schema_version": "candidate-single-cell-execution-1.0",
        "execution_id": "exec-p0-single-cell-synthetic-20260719",
        "case_id": "p0-single-cell-synthetic",
        "domain": "single-cell",
        "distribution": "private-local-only",
        "recorded_on": "2026-07-19",
        "task_root": "private-task-root",
        "public_candidate_registry_immutable": True,
        "authorization": {
            "mode": "run",
            "scope": "task-local",
            "source_access": "read-only",
            "global_changes": False,
            "remote_publication": False,
        },
        "inputs": {
            "source_identifier": "SYNTHETIC",
            "metadata_sidecar_sha256": digest,
            "features_per_matrix": 100,
            "total_filtered_barcodes": 500,
            "total_size_bytes": 50,
            "artifacts": [
                {
                    "locator_ref": f"local:p0.synthetic.input-{number}",
                    "sha256": digest,
                    "size_bytes": 10,
                    "access_mode": "read-only",
                }
                for number in range(1, 6)
            ],
            "reduced_sampling": {
                "method": "deterministic-without-replacement-h5-csc-column-slice",
                "sampled_barcodes": 5000,
                "max_per_donor": 1000,
                "seeds": [1, 2, 3, 4, 5],
                "full_sparse_matrix_loaded_before_sampling": False,
                "membership_hashes_recorded": True,
            },
        },
        "environment": {
            "state": "frozen",
            "backend": "python-relocated-conda-snapshot",
            "platform": "windows-amd64",
            "env_id": "env_python_aaaaaaaaaaaa",
            "lock_hash": digest,
            "source_target_tree_sha256": digest,
            "external_runtime_references": 0,
            "active_editable_or_vcs_locators": 0,
            "generated_bytecode_files": 0,
            "versions": {
                "python": "3",
                "scanpy": "1",
                "anndata": "1",
                "h5py": "1",
                "numpy": "1",
                "scipy": "1",
                "pandas": "1",
                "scikit-learn": "1",
                "matplotlib": "1",
                "igraph": "1",
                "leidenalg": "1",
            },
            "thread_limits": {
                "OMP_NUM_THREADS": "2",
                "OPENBLAS_NUM_THREADS": "2",
                "MKL_NUM_THREADS": "2",
                "NUMEXPR_NUM_THREADS": "2",
                "NUMBA_NUM_THREADS": "2",
            },
            "manifest": hashed("environment/manifest.json"),
            "marker": hashed("environment/locked.json"),
        },
        "execution": {
            "run_id": "synthetic-run",
            "run_root": "synthetic-run",
            "fresh_returncode": 0,
            "fresh_state": "CHECKPOINTED",
            "cached_resume_returncode": 0,
            "cached_resume_state": "CHECKPOINTED",
            "cached_resume_elapsed_seconds": 1.0,
            "restored_from_cache": True,
            "cached_stages_revalidated": ["00-intake", "01-environment", "02-analysis"],
            "analysis_mode": "reduced-real-data-teaching-fixture",
            "analysis_output_tree_sha256": digest,
            "artifact_count": 31,
            "artifact_index_exact_set_verified": True,
            "artifact_hash_or_size_errors": 0,
            "machine_qa": "PASS_WITH_WARNINGS",
            "analysis_summary": {
                "sampled_nuclei": 5000,
                "retained_nuclei": 4500,
                "retained_by_donor": {f"d{number}": 900 for number in range(1, 6)},
                "selected_features": 1000,
                "leiden_clusters": 5,
                "coarse_labels": 3,
                "unknown_or_ambiguous_nuclei": 10,
                "inferential_tests_performed": False,
                "integration_method": "none",
            },
            "doublet_handling": {
                "scoring_all_donors_completed": True,
                "filter_policy": "all-donors-or-none",
                "all_donors_filter_eligible": False,
                "filtering_applied": False,
                "doublet_cleared": False,
                "reason": "synthetic boundary",
            },
            "memory_contract": {
                "donor_processing": "serial",
                "full_gene_merged_matrix_created": False,
                "dense_full_count_matrix_created": False,
                "peak_working_set_gb": 1.0,
                "minimum_observed_system_available_gb": 1.0,
            },
        },
        "native_review": {
            "state": "native-reviewed",
            "decision": "PASS_WITH_MINOR_FINDINGS",
            "reviewed_native_pixels": True,
            "figure_count": 6,
            "blocking_findings": 0,
            "review_source": hashed("review/native.json"),
            "checkpoint": hashed("run/checkpoints/03-native-review/checkpoint.json"),
        },
        "delivery": {
            "state": "DELIVERED",
            "latest_valid_checkpoint": "04-delivery-report",
            "run_manifest": hashed("run/run-manifest.json"),
            "analysis_checkpoint": hashed("run/checkpoints/02-analysis/checkpoint.json"),
            "artifact_index": hashed("run/output/artifact-index.json"),
            "machine_qa": hashed("run/output/qa-machine.json"),
            "qa_report": hashed("run/output/QA_REPORT.md"),
            "analysis_figure_notes": hashed("run/output/FIGURE_NOTES.md"),
            "final_figure_notes": hashed("run/checkpoints/04-delivery-report/FIGURE_NOTES.final.md"),
            "delivery_checkpoint": hashed("run/checkpoints/04-delivery-report/checkpoint.json"),
            "delivery_payload_tree_sha256": digest,
            "read_only_delivery_verify": {
                "ok": True,
                "manifest_unchanged": True,
                "checkpoint_unchanged": True,
            },
        },
        "code": {
            "pipeline": hashed("scripts/pipeline.py"),
            "run_manager": hashed("scripts/run-manager.py"),
            "delivery_binder": hashed("scripts/delivery-binder.py"),
            "input_config": hashed("config/input.json"),
        },
        "maturity": "data-verified",
        "maturity_scope": "Reduced real-data fixture only; full-data execution is unverified.",
        "scientific_claim_ceiling": "Descriptive reduced-fixture execution only.",
        "known_limitations": ["Synthetic schema fixture; no scientific claim."],
    }


def _synthetic_spatial_execution():
    digest = "b" * 64

    def hashed(relative_path):
        return {"relative_path": relative_path, "sha256": digest, "size_bytes": 10}

    return {
        "schema_version": "candidate-spatial-execution-1.0",
        "execution_id": "exec-p0-spatial-synthetic-20260719",
        "case_id": "p0-spatial-synthetic",
        "domain": "spatial-transcriptomics",
        "distribution": "private-local-only",
        "recorded_on": "2026-07-19",
        "task_root": "private-task-root",
        "public_candidate_registry_immutable": True,
        "authorization": {
            "mode": "run",
            "scope": "task-local",
            "source_access": "read-only",
            "global_changes": False,
            "remote_publication": False,
        },
        "inputs": {
            "platform": "10x-visium-spatial-3-prime-v1",
            "sample_id": "NCF221-D",
            "section_id": "NCF221-D",
            "section_count": 1,
            "spot_count": 685,
            "gene_count": 32285,
            "image_dimensions_xy": [2600, 4100],
            "coordinate_space": "Space Ranger full-resolution image pixels",
            "total_size_bytes": 232639352,
            "artifacts": [
                {
                    "locator_ref": f"local:p0.spatial.synthetic-{number}",
                    "sha256": digest,
                    "size_bytes": size,
                    "access_mode": "read-only",
                }
                for number, size in enumerate(
                    [10174860, 102353222, 15981048, 104130222], start=1
                )
            ],
        },
        "environment": {
            "state": "frozen",
            "backend": "python-uv-task-owned-runtime",
            "platform": "windows-amd64",
            "env_id": "env_python_bbbbbbbbbbbb",
            "lock_hash": digest,
            "task_owned_runtime": True,
            "external_runtime_references": 0,
            "global_changes": False,
            "runtime_tree_at_lock": {
                "tree_sha256": digest,
                "file_count": 1,
                "size_bytes": 1,
            },
            "versions": {
                "python": "3.13.12",
                "anndata": "1",
                "h5py": "1",
                "matplotlib": "1",
                "numpy": "1",
                "pandas": "1",
                "pillow": "1",
                "scikit-learn": "1",
                "scipy": "1",
            },
            "thread_limits": {
                "OMP_NUM_THREADS": "2",
                "OPENBLAS_NUM_THREADS": "2",
                "MKL_NUM_THREADS": "2",
                "NUMEXPR_NUM_THREADS": "2",
            },
            "manifest": hashed("environment/manifest.json"),
            "marker": hashed("environment/locked.json"),
            "uv_lock": hashed("environment/uv.lock"),
            "requirements_lock": hashed("environment/requirements-lock.txt"),
            "pyproject": hashed("environment/pyproject.toml"),
            "model_probe": hashed("environment/model-probe.json"),
        },
        "execution": {
            "run_id": "run-p0-spatial-synthetic-v1",
            "run_root": "formal-run-v1",
            "fresh_returncode": 0,
            "state": "DELIVERED",
            "analysis_mode": "real-data-descriptive-spatial-core",
            "stage_ids": [
                "S00_INTAKE",
                "S10_INGEST",
                "S20_COORD_IMAGE_QC",
                "S30_UNIT_QC",
                "S40_PREPROCESS",
                "S50_SPATIAL_GRAPH",
                "S60_CORE_DISCOVERY",
                "S70_COMPOSITION_MAPPING",
                "S90_INFERENCE_QA",
                "S95_VISUALIZE_INTERPRET",
            ],
            "checkpoint_count": 10,
            "artifact_count": 102,
            "ledger_entry_count": 102,
            "native_figure_count": 4,
            "analysis_summary": {
                "spot_count": 685,
                "gene_count": 32285,
                "selected_features": 1000,
                "expression_clusters": 3,
                "coordinate_identity_correlation": 0.999,
                "graph_sensitivity_moran_spearman": 0.95,
                "qc_flag_sensitivity_cluster_ari": 0.89,
                "spatial_unit": "spot",
                "sampling_unit": "section",
                "inference_unit": "animal/specimen",
                "independent_inference_units": 1,
                "inferential_tests_performed": False,
                "population_inference_allowed": False,
            },
            "model_branch": {
                "requested_method": "Spotiphy deconvolution/decomposition",
                "status": "blocked",
                "attempted": False,
                "deconvolution_completed": False,
                "installation_attempted_in_this_environment": False,
                "substitution_performed": False,
                "scientifically_non_equivalent_fallback_used": False,
                "source_commit": "8167624a942fec97cf5cdfc8ed3a537622aa78f1",
                "required_future_environment": "new task-local compatible lock",
            },
        },
        "native_review": {
            "state": "native-reviewed",
            "decision": "PASS_WITH_MINOR_FINDINGS",
            "reviewed_native_pixels": True,
            "all_originals_opened": True,
            "all_finals_opened": True,
            "figure_count": 4,
            "blocking_findings": 0,
            "major_findings": 0,
            "review_source": hashed("run/06_figures/review/native.json"),
            "checkpoint": hashed("run/04_intermediate/S95_VISUALIZE_INTERPRET/checkpoint.json"),
        },
        "delivery": {
            "state": "DELIVERED",
            "latest_valid_checkpoint": "S95_VISUALIZE_INTERPRET",
            "run_manifest": hashed("run/manifest/run_manifest.json"),
            "artifact_ledger": hashed("run/manifest/artifact_ledger.jsonl"),
            "artifact_index": hashed("run/07_reports/ARTIFACT_INDEX.json"),
            "qa_report": hashed("run/07_reports/QA_REPORT.md"),
            "figure_notes": hashed("run/07_reports/FIGURE_NOTES.md"),
            "analysis_design": hashed("run/01_plan/ANALYSIS_DESIGN.md"),
            "workflow_plan": hashed("run/01_plan/workflow.plan.yaml"),
            "failure_evidence": [
                hashed("run/07_reports/failure_evidence/a.json"),
                hashed("run/07_reports/failure_evidence/b.json"),
            ],
        },
        "code": {
            "pipeline": hashed("run/03_scripts/run_pipeline.py"),
            "executor": hashed("execute_case.py"),
            "environment_preparer": hashed("prepare_environment.py"),
            "input_config": hashed("input-config.private.json"),
            "finalizer": {"path": "private-finalizer.py", "sha256": digest, "size_bytes": 10},
        },
        "maturity": "data-verified",
        "maturity_scope": "One real section; synthetic schema fixture only.",
        "scientific_claim_ceiling": "Descriptive within-section evidence only.",
        "known_limitations": ["Synthetic schema fixture; no scientific claim."],
    }


def _synthetic_formal_omics_execution():
    digest = "c" * 64

    def hashed(relative_path):
        return {"relative_path": relative_path, "sha256": digest, "size_bytes": 10}

    return {
        "schema_version": "candidate-formal-omics-execution-1.0",
        "execution_id": "exec-p0-bulk-rna-airway-20260719",
        "case_id": "p0-bulk-rna-airway",
        "domain": "bulk-rna",
        "distribution": "private-local-only",
        "recorded_on": "2026-07-19",
        "task_root": "private-task-root",
        "public_candidate_registry_immutable": True,
        "authorization": {
            "mode": "run",
            "scope": "task-local",
            "source_access": "read-only",
            "global_changes": False,
            "remote_publication": False,
        },
        "inputs": {
            "source_identifier": "synthetic formal omics fixture",
            "statistical_unit": "sample",
            "artifacts": [
                {
                    "locator_ref": "local:p0.bulk.synthetic",
                    "role": "input",
                    "sha256": digest,
                    "size_bytes": 10,
                    "access_mode": "read-only",
                }
            ],
            "input_contract_verified": True,
            "dimensions": {"features": 10, "samples": 4},
        },
        "environment": {
            "state": "frozen",
            "backend": "r-renv",
            "platform": "windows-amd64",
            "runtime": "R 4.5.3",
            "env_id": "env_r_cccccccccccc",
            "lock_hash": digest,
            "package_count": 2,
            "manifest": hashed("run/02_environment/environment_manifest.json"),
            "primary_lock": hashed("run/02_environment/renv.lock"),
            "restore_evidence": {"path": "private/restore.json", "sha256": digest, "size_bytes": 10},
            "exact_restore_verified": True,
            "verification_hashes": [digest, digest],
            "global_changes": False,
            "version_boundaries": ["synthetic boundary"],
        },
        "execution": {
            "run_id": "run-formal",
            "run_root": "runs/run-formal",
            "state": "DELIVERED",
            "exit_code": 0,
            "stage_ids": ["S1", "S2", "S3", "S4"],
            "checkpoint_count": 4,
            "manifest_artifact_count": 4,
            "ledger_entry_count": 4,
            "validation_ok": True,
            "recoverable_checkpoints_verified": True,
            "analysis_summary": {"raw_features": 10, "tested": 8, "calls": 2, "samples": 4},
        },
        "native_review": {
            "state": "native-reviewed",
            "decision": "PASS_WITH_BOUNDARIES",
            "reviewed_native_pixels": True,
            "all_final_figures_opened": True,
            "figure_count": 1,
            "blocking_findings": 0,
            "review_source": hashed("run/06_figures/review/native.json"),
            "figures": [
                {
                    **hashed("run/06_figures/final/figure.png"),
                    "width_px": 1200,
                    "height_px": 1000,
                    "scientific_boundary": "Synthetic schema fixture only.",
                }
            ],
        },
        "delivery": {
            "state": "DELIVERED",
            "output_root": "private-output-root",
            "package_manifest": {"path": "private-output-root/package_manifest.json", "sha256": digest, "size_bytes": 10},
            "package_file_count": 2,
            "package_verification_ok": True,
            "run_manifest": hashed("run/manifest/run_manifest.json"),
            "artifact_ledger": hashed("run/manifest/artifact_ledger.jsonl"),
            "qa_report": hashed("run/07_reports/QA_REPORT.md"),
            "figure_notes": hashed("run/07_reports/FIGURE_NOTES.md"),
            "analysis_design": hashed("run/01_plan/ANALYSIS_DESIGN.md"),
        },
        "code": {
            "pipeline": hashed("run/03_scripts/pipeline.R"),
            "verifier": hashed("run/03_scripts/verify.py"),
        },
        "maturity": "data-verified",
        "maturity_scope": "Synthetic schema fixture only.",
        "scientific_claim_ceiling": "No scientific claim.",
        "known_limitations": ["Synthetic schema fixture."],
    }


class P0ExecutionUnionSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        cls.validator = jsonschema.Draft202012Validator(
            cls.schema,
            format_checker=jsonschema.FormatChecker(),
        )

    def registry(self, execution):
        return {
            "schema_version": "1.0",
            "registry_id": "biomedical-analysis-agent-p0-teaching-case-executions",
            "distribution": "private-local-only",
            "source_mode": "read-only-evidence",
            "public_candidate_registry_ref": "references/p0-teaching-cases.json",
            "executions": [execution],
        }

    def test_single_cell_branch_accepts_only_its_domain_contract(self):
        execution = _synthetic_single_cell_execution()
        self.validator.validate(self.registry(execution))
        execution["domain"] = "visualization"
        self.assertTrue(list(self.validator.iter_errors(self.registry(execution))))

    def test_single_cell_branch_rejects_false_data_verified_counts(self):
        execution = _synthetic_single_cell_execution()
        execution["execution"]["artifact_count"] = 30
        execution["native_review"]["reviewed_native_pixels"] = False
        self.assertTrue(list(self.validator.iter_errors(self.registry(execution))))

    def test_single_cell_branch_requires_native_and_delivery_bindings(self):
        execution = _synthetic_single_cell_execution()
        del execution["native_review"]["checkpoint"]
        del execution["delivery"]["delivery_checkpoint"]
        self.assertTrue(list(self.validator.iter_errors(self.registry(execution))))

    def test_spatial_branch_accepts_only_bounded_descriptive_execution(self):
        execution = _synthetic_spatial_execution()
        self.validator.validate(self.registry(execution))
        execution["execution"]["analysis_summary"]["population_inference_allowed"] = True
        execution["execution"]["model_branch"]["substitution_performed"] = True
        self.assertTrue(list(self.validator.iter_errors(self.registry(execution))))

    def test_spatial_branch_rejects_unreviewed_or_incomplete_delivery(self):
        execution = _synthetic_spatial_execution()
        execution["native_review"]["all_originals_opened"] = False
        execution["delivery"]["state"] = "CHECKPOINTED"
        self.assertTrue(list(self.validator.iter_errors(self.registry(execution))))

    def test_formal_omics_branch_binds_domain_case_restore_and_native_review(self):
        execution = _synthetic_formal_omics_execution()
        self.validator.validate(self.registry(execution))
        execution["case_id"] = "p0-proteomics-dep-ubilength"
        execution["environment"]["exact_restore_verified"] = False
        execution["native_review"]["reviewed_native_pixels"] = False
        self.assertTrue(list(self.validator.iter_errors(self.registry(execution))))

    def test_atomic_registrar_refuses_duplicate_execution(self):
        execution = _synthetic_single_cell_execution()
        empty_registry = self.registry(execution)
        empty_registry["executions"] = []
        public_cases = {
            "cases": [
                {
                    "case_id": execution["case_id"],
                    "domain": execution["domain"],
                    "execution_status": "not-executed",
                }
            ]
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate_path = root / "candidate.json"
            registry_path = root / "registry.json"
            schema_path = root / "schema.json"
            candidates_path = root / "candidates.json"
            candidate_path.write_text(json.dumps(execution), encoding="utf-8")
            registry_path.write_text(json.dumps(empty_registry), encoding="utf-8")
            schema_path.write_text(json.dumps(self.schema), encoding="utf-8")
            candidates_path.write_text(json.dumps(public_cases), encoding="utf-8")
            first = register_execution(candidate_path, registry_path, schema_path, candidates_path)
            self.assertTrue(first["ok"])
            with self.assertRaisesRegex(ValueError, "duplicate_execution_id"):
                register_execution(candidate_path, registry_path, schema_path, candidates_path)
            replaced = register_execution(
                candidate_path,
                registry_path,
                schema_path,
                candidates_path,
                replace_existing=True,
            )
            self.assertEqual(replaced["action"], "replaced")
            persisted = json.loads(registry_path.read_text(encoding="utf-8"))
        self.assertEqual(len(persisted["executions"]), 1)


@unittest.skipUnless(PRIVATE_EXECUTION_EVIDENCE_AVAILABLE, "Private execution evidence is intentionally absent from a public-core clone")
class P0TeachingCaseExecutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
        cls.schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        cls.candidates = json.loads(CANDIDATES.read_text(encoding="utf-8"))

    def test_self_contained_schema_and_live_registry_validate(self):
        self.assertNotIn("knowledge-objects.schema.json", json.dumps(self.schema))
        jsonschema.Draft202012Validator(
            self.schema,
            format_checker=jsonschema.FormatChecker(),
        ).validate(self.registry)
        report = validate_execution_registry(REGISTRY, SCHEMA, CANDIDATES, LOCATORS, verify_live=True)
        self.assertTrue(report["ok"], report["errors"])
        self.assertEqual(report["execution_count"], 6)
        self.assertEqual(report["verified_run_artifacts"], 123)
        self.assertEqual(report["verified_native_figures"], 29)
        self.assertEqual(report["verified_formal_files"], 53)
        self.assertEqual(report["verified_formal_artifacts"], 488)
        self.assertEqual(report["verified_checkpoints"], 34)
        self.assertEqual(
            report["execution_counts_by_domain"],
            {
                "bulk-rna": 1,
                "multi-omics": 1,
                "quantitative-proteomics": 1,
                "single-cell": 1,
                "spatial-transcriptomics": 1,
                "visualization": 1,
            },
        )

    def test_execution_is_overlay_and_literature_case_remains_unexecuted(self):
        execution = self.registry["executions"][0]
        self.assertEqual(execution["case_id"], "p0-visualization-kinneyh-composition")
        self.assertEqual(execution["maturity"], "data-verified")
        self.assertEqual(execution["native_review"]["decision"], "PASS_WITH_BOUNDARIES")
        self.assertEqual(len(execution["reproducibility"]["artifact_hashes"]), 9)
        self.assertEqual(len(execution["native_review"]["figures"]), 2)
        self.assertEqual(execution["formal_run"]["state"], "DELIVERED")
        self.assertTrue(execution["formal_run"]["validation"]["ok"])
        self.assertTrue(execution["formal_run"]["resume_audit"]["ok"])
        self.assertEqual(execution["formal_run"]["resume_audit"]["latest_valid_checkpoint"], "07-interpretation")
        self.assertEqual(execution["formal_run"]["registered_artifact_count"], 28)
        self.assertEqual(execution["formal_run"]["manifest_artifact_count"], 28)
        self.assertEqual(execution["formal_run"]["ledger_entry_count"], 28)
        self.assertEqual(execution["formal_run"]["duplicate_registration_count"], 0)
        self.assertTrue(all(case["execution_status"] == "not-executed" for case in self.candidates["cases"]))
        self.assertTrue(all(case["maturity"] in {"raw-extracted", "normalized"} for case in self.candidates["cases"]))
        self.assertEqual(len(self.candidates["cases"]) - len(self.registry["executions"]), 1)
        self.assertEqual(
            {case["case_id"] for case in self.candidates["cases"]}
            - {item["case_id"] for item in self.registry["executions"]},
            {"p0-literature-rogue-negative-control"},
        )

    def test_single_cell_execution_has_exact_reduced_fixture_delivery_contract(self):
        execution = next(item for item in self.registry["executions"] if item["domain"] == "single-cell")
        self.assertEqual(execution["execution"]["analysis_mode"], "reduced-real-data-teaching-fixture")
        self.assertEqual(execution["execution"]["artifact_count"], 31)
        self.assertEqual(execution["native_review"]["figure_count"], 6)
        self.assertEqual(execution["delivery"]["latest_valid_checkpoint"], "04-delivery-report")
        self.assertEqual(execution["maturity"], "data-verified")
        self.assertFalse(execution["execution"]["analysis_summary"]["inferential_tests_performed"])
        self.assertFalse(execution["execution"]["doublet_handling"]["doublet_cleared"])

    def test_spatial_execution_preserves_model_blocker_and_claim_boundary(self):
        execution = next(
            item for item in self.registry["executions"]
            if item["domain"] == "spatial-transcriptomics"
        )
        self.assertEqual(execution["execution"]["artifact_count"], 102)
        self.assertEqual(execution["execution"]["checkpoint_count"], 10)
        self.assertEqual(execution["native_review"]["figure_count"], 4)
        self.assertEqual(execution["execution"]["model_branch"]["status"], "blocked")
        self.assertFalse(execution["execution"]["model_branch"]["attempted"])
        self.assertFalse(execution["execution"]["model_branch"]["substitution_performed"])
        self.assertEqual(
            execution["execution"]["analysis_summary"]["independent_inference_units"], 1
        )
        self.assertFalse(
            execution["execution"]["analysis_summary"]["population_inference_allowed"]
        )

    def test_three_formal_omics_executions_preserve_scientific_boundaries(self):
        by_domain = {item["domain"]: item for item in self.registry["executions"]}
        bulk = by_domain["bulk-rna"]
        self.assertEqual(bulk["execution"]["analysis_summary"]["raw_features"], 63677)
        self.assertEqual(bulk["execution"]["analysis_summary"]["bh_fdr_lt_0_05"], 4081)
        self.assertEqual(bulk["native_review"]["figure_count"], 6)

        proteomics = by_domain["quantitative-proteomics"]
        self.assertEqual(proteomics["execution"]["analysis_summary"]["primary_tested_protein_groups"], 1358)
        self.assertEqual(proteomics["execution"]["analysis_summary"]["primary_calls_fdr05_abs_lfc1"], 50)
        self.assertEqual(proteomics["execution"]["analysis_summary"]["direction_reversals"], 47)
        self.assertEqual(proteomics["native_review"]["figure_count"], 4)

        multiomics = by_domain["multi-omics"]
        self.assertEqual(multiomics["inputs"]["dimensions"]["patient_union"], 200)
        self.assertEqual(multiomics["execution"]["analysis_summary"]["high_count"], 5)
        self.assertEqual(multiomics["execution"]["analysis_summary"]["moderate_count"], 1)
        self.assertEqual(multiomics["native_review"]["figure_count"], 7)

        for execution in (bulk, proteomics, multiomics):
            self.assertTrue(execution["environment"]["exact_restore_verified"])
            self.assertTrue(execution["delivery"]["package_verification_ok"])
            self.assertEqual(execution["delivery"]["state"], "DELIVERED")

    def test_input_uses_private_locator_not_an_embedded_source_path(self):
        input_evidence = self.registry["executions"][0]["input"]
        self.assertEqual(set(input_evidence), {"locator_ref", "sha256", "size_bytes", "access_mode"})
        self.assertTrue(input_evidence["locator_ref"].startswith("local:p0."))
        public_text = CANDIDATES.read_text(encoding="utf-8")
        private_root = "".join(("D:", "\\", "stereoseq"))
        self.assertNotIn(private_root, public_text)

    def test_semantic_validator_rejects_tampered_artifact_hash(self):
        mutated = json.loads(json.dumps(self.registry))
        mutated["executions"][0]["reproducibility"]["artifact_hashes"][0]["sha256"] = "0" * 64
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "executions.json"
            path.write_text(json.dumps(mutated, ensure_ascii=False), encoding="utf-8")
            report = validate_execution_registry(path, SCHEMA, CANDIDATES, LOCATORS, verify_live=True)
        self.assertFalse(report["ok"])
        self.assertTrue(any("artifact_hashes_mismatch" in error or "reproducibility_hash_mismatch" in error for error in report["errors"]))

    def test_schema_rejects_unreviewed_data_verified_record(self):
        mutated = json.loads(json.dumps(self.registry))
        mutated["executions"][0]["native_review"]["reviewed_native_pixels"] = False
        errors = list(jsonschema.Draft202012Validator(self.schema).iter_errors(mutated))
        self.assertTrue(errors)

    def test_schema_rejects_non_delivered_formal_run(self):
        mutated = json.loads(json.dumps(self.registry))
        mutated["executions"][0]["formal_run"]["state"] = "CHECKPOINTED"
        errors = list(jsonschema.Draft202012Validator(self.schema).iter_errors(mutated))
        self.assertTrue(errors)

    def test_semantic_validator_rejects_tampered_formal_figure_hash(self):
        mutated = json.loads(json.dumps(self.registry))
        mutated["executions"][0]["formal_run"]["final_figures"][0]["sha256"] = "f" * 64
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "executions.json"
            path.write_text(json.dumps(mutated, ensure_ascii=False), encoding="utf-8")
            report = validate_execution_registry(path, SCHEMA, CANDIDATES, LOCATORS, verify_live=True)
        self.assertFalse(report["ok"])
        self.assertTrue(any("formal_final_figure" in error or "formal_final_figures_native" in error for error in report["errors"]))


if __name__ == "__main__":
    unittest.main()
