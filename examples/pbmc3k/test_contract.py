#!/usr/bin/env python3
"""Dependency-free static and router contract tests for the PBMC3K case."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path


CASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = CASE_DIR.parent.parent


def _load_case_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, CASE_DIR / filename)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {filename}")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(CASE_DIR))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


class PBMC3KContractTests(unittest.TestCase):
    def load(self, name: str):
        return json.loads((CASE_DIR / name).read_text(encoding="utf-8-sig"))

    def test_required_teaching_chain_exists(self):
        required = {
            "PROMPT.md",
            "request.json",
            "route.expected.json",
            "workflow.plan.expected.json",
            "ANALYSIS_DESIGN.md",
            "README.md",
            "TROUBLESHOOTING.md",
            "input_manifest.json",
            "environment-spec.json",
            "params.json",
            "download_inputs.py",
            "prepare_environment.py",
            "prepare_environment.R",
            "case_driver.py",
            "run_pipeline.R",
            "export_expected_output.py",
            "verify_expected_output.py",
        }
        self.assertEqual([], sorted(name for name in required if not (CASE_DIR / name).is_file()))

    def test_input_is_official_pinned_and_not_redistributed(self):
        manifest = self.load("input_manifest.json")
        self.assertFalse(manifest["redistribute_binary_input"])
        self.assertEqual(1, len(manifest["inputs"]))
        item = manifest["inputs"][0]
        self.assertEqual(
            "https://cf.10xgenomics.com/samples/cell/pbmc3k/pbmc3k_filtered_gene_bc_matrices.tar.gz",
            item["url"],
        )
        self.assertEqual(7_621_991, item["content_length_bytes"])
        self.assertEqual(
            "847d6ebd9a1ec9a768f2be7e40ca42cbfe75ebeb6d76a4c24167041699dc28b5",
            item["sha256"],
        )
        self.assertEqual("CC BY 4.0", item["license"])

    def test_canonical_values_and_visual_limit_are_frozen(self):
        manifest = self.load("input_manifest.json")
        self.assertEqual(2700, manifest["canonical_checks"]["input_cells"])
        self.assertEqual(2638, manifest["canonical_checks"]["qc_retained_cells"])
        self.assertEqual(9, manifest["canonical_checks"]["clusters"])
        params = self.load("params.json")
        self.assertEqual(3, params["visual"]["max_rounds"])
        self.assertEqual("none", params["visual"]["qc_legend_position"])
        self.assertIs(params["visual"]["annotation_direct_labels"], False)
        self.assertEqual("LogNormalize", params["normalization"]["method"])
        self.assertEqual("vst", params["variable_features"]["method"])
        self.assertEqual(0.5, params["clustering"]["resolution"])
        self.assertEqual("uwot", params["umap"]["method"])
        self.assertEqual("cosine", params["umap"]["metric"])
        self.assertEqual(
            {
                "name": "Seurat.warn.umap.uwot",
                "value_during_call": False,
                "restore_after_call": True,
            },
            params["umap"]["transition_warning_option"],
        )
        self.assertEqual(
            {"warn": 1, "delivery": "immediate-stderr", "forbidden_scan": "fail-closed"},
            params["r_warning_policy"],
        )

    def test_v5_native_review_round_narrative_matches_public_records(self):
        design = (CASE_DIR / "ANALYSIS_DESIGN.md").read_text(encoding="utf-8")
        readme = (CASE_DIR / "README.md").read_text(encoding="utf-8")
        prompt = (CASE_DIR / "PROMPT.md").read_text(encoding="utf-8")
        reports = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted((CASE_DIR / "expected-output/reports").glob("*.md"))
        )
        for text in (design, readme, prompt, reports):
            self.assertNotIn("visual round 2", text.lower())
            self.assertNotIn("round 2 native review", text.lower())
        self.assertIn("单个 hash-bound native-review round 1", readme)
        self.assertIn("只记录一个 hash-bound native-review round 1", design)

        for figure_id in (
            "qc_violin",
            "pca_clusters",
            "umap_clusters",
            "umap_annotation",
            "marker_dotplot",
        ):
            review = self.load(f"expected-output/figures/review/{figure_id}.review.json")
            self.assertEqual("native-reviewed", review["status"])
            self.assertEqual(1, len(review["rounds"]))
            self.assertEqual(1, review["rounds"][0]["round"])
            self.assertEqual("keep", review["rounds"][0]["decision"])
            self.assertFalse(
                review["rounds"][0]["visual_parameter_diff"]["scientific_parameters_changed"]
            )
        annotation = self.load("expected-output/figures/review/umap_annotation.review.json")
        resolved_major = [
            finding
            for finding in annotation["rounds"][0]["findings"]
            if finding.get("severity") == "major" and finding.get("status") == "resolved"
        ]
        self.assertEqual(1, len(resolved_major))

    def test_request_is_descriptive_and_disallows_inference(self):
        request = self.load("request.json")
        self.assertEqual("descriptive-only", request["analysis_scope"])
        self.assertIs(request["inferential"], False)
        notes = request["notes"].lower()
        for forbidden_branch in ("cellchat", "gsea", "donor-level inference", "differential abundance"):
            self.assertIn(forbidden_branch, notes)

    def test_analysis_recipe_contains_no_installer(self):
        pipeline = (CASE_DIR / "run_pipeline.R").read_text(encoding="utf-8")
        forbidden = (
            "install.packages(",
            "BiocManager::install(",
            "renv::install(",
            "pak::pkg_install(",
            "devtools::install_",
            "remotes::install_",
        )
        for token in forbidden:
            self.assertNotIn(token, pipeline)
        self.assertIn('observed_seurat, "5.5.0"', pipeline)
        self.assertIn('observed_r, "4.5.3"', pipeline)

    def test_environment_has_exact_no_fallback_contract(self):
        spec = self.load("environment-spec.json")
        versions = {item["name"]: item["version"] for item in spec["dependencies"]}
        self.assertEqual("5.5.0", versions["Seurat"])
        self.assertEqual("1.2.2", versions["renv"])
        self.assertFalse(spec["policy"]["fallback_to_5_4_0"])
        self.assertTrue(spec["policy"]["task_local_library_only"])
        self.assertFalse(spec["manager_bootstrap"]["host_renv_required"])
        self.assertTrue(spec["policy"]["bootstrap_library_inside_run_root"])
        self.assertTrue(spec["policy"]["library_description_sha256_required"])
        self.assertTrue(spec["policy"]["verified_environment_fast_reuse"])
        self.assertEqual(
            "bcba2170563c65c6d6ed9328e4a624033ad9c5ee4e5bf9132cff7bcc7327cce5",
            spec["manager_bootstrap"]["sha256"],
        )
        preparer = (CASE_DIR / "prepare_environment.R").read_text(encoding="utf-8")
        self.assertNotIn('requireNamespace("renv"', preparer)
        self.assertNotIn("1.2.3", preparer)
        self.assertNotIn("https://cloud.r-project.org", preparer)
        self.assertIn("options(repos = c(CRAN = snapshot_repo))", preparer)
        self.assertIn('repos = NULL', preparer)
        python_preparer = (CASE_DIR / "prepare_environment.py").read_text(encoding="utf-8")
        self.assertIn("environment-cache-reuse.json", python_preparer)
        self.assertIn("FROZEN_ENVIRONMENT_REUSE_LIBRARY_DESCRIPTION_HASH_MISMATCH", python_preparer)
        self.assertIn("renv.lock repository mismatch", python_preparer)
        self.assertIn('[{"Name": "CRAN", "URL": SNAPSHOT_REPOSITORY}]', python_preparer)

    def test_native_exit_and_child_only_architecture_contract(self):
        spec = self.load("environment-spec.json")
        architecture = spec["windows_r_child_environment"]
        self.assertEqual("GetNativeSystemInfo", architecture["architecture_source"])
        self.assertEqual({"X64": "AMD64", "ARM64": "ARM64", "X86": "x86"}, architecture["native_mapping"])
        self.assertEqual("AMD64", architecture["validated_architecture"])
        self.assertEqual("child-process-only", architecture["scope"])
        self.assertFalse(architecture["parent_environment_modified"])
        process = spec["r_process_completion"]
        self.assertEqual("native_exit", process["shutdown_mode"])
        self.assertEqual(0, process["required_returncode"])
        self.assertEqual(1, process["pipeline_r_warn_option"])
        self.assertEqual("immediate-stderr", process["pipeline_warning_delivery"])
        self.assertTrue(process["structured_process_evidence_required"])
        self.assertFalse(process["external_termination_helper_permitted"])
        self.assertEqual(
            [
                "warning:",
                "warning message",
                "stack imbalance",
                "iteration limit reached",
                "alternation limit reached",
                "execution halted",
                "error in ",
                "caught access violation",
                "caught segfault",
                "fatal error",
            ],
            process["stdout_stderr_forbidden_patterns"],
        )

        sources = {
            name: (CASE_DIR / name).read_text(encoding="utf-8")
            for name in ("prepare_environment.py", "prepare_environment.R", "case_driver.py", "run_pipeline.R")
        }
        for name, source in sources.items():
            lowered = source.lower()
            for forbidden in ("hard_exit", "exit-helper", "exit_helper", "helper_sha256", "windows_exit_workaround"):
                self.assertNotIn(forbidden, lowered, name)
        self.assertNotIn("dyn.load(", sources["prepare_environment.R"])
        self.assertNotIn("dyn.load(", sources["run_pipeline.R"])
        self.assertIn("smoke_counts <- Matrix::Matrix(", sources["prepare_environment.R"])
        self.assertIn('gsub("_", "-", original_feature_names, fixed = TRUE)', sources["run_pipeline.R"])
        self.assertIn('run_umap_with_explicit_transition_option <- function(', sources["run_pipeline.R"])
        self.assertIn('umap.method = umap_params$method', sources["run_pipeline.R"])
        self.assertIn('metric = umap_params$metric', sources["run_pipeline.R"])
        self.assertIn('options(warn = 1)', sources["run_pipeline.R"])
        self.assertIn('identical(as.integer(getOption("warn")), 1L)', sources["run_pipeline.R"])
        self.assertIn('on.exit(restore_option(), add = TRUE)', sources["run_pipeline.R"])
        self.assertIn('!identical(option_contract$name, "Seurat.warn.umap.uwot")', sources["run_pipeline.R"])
        self.assertIn('option_name <- option_contract$name', sources["run_pipeline.R"])
        self.assertIn('restore_option()', sources["run_pipeline.R"])
        self.assertIn('option_restored = TRUE', sources["run_pipeline.R"])
        self.assertIn('transition_notice_option_applied = TRUE', sources["run_pipeline.R"])
        self.assertNotIn('suppressWarnings(', sources["run_pipeline.R"])
        self.assertNotIn('withCallingHandlers(', sources["run_pipeline.R"])
        self.assertNotIn('muffleWarning', sources["run_pipeline.R"])
        self.assertIn('"shutdown_mode=native_exit"', sources["prepare_environment.R"])
        self.assertIn('"shutdown_mode=native_exit"', sources["run_pipeline.R"])

    def test_forbidden_scan_blocks_plural_warning_messages(self):
        for module_name, filename in (
            ("pbmc_case_driver_warning_contract", "case_driver.py"),
            ("pbmc_prepare_environment_warning_contract", "prepare_environment.py"),
        ):
            module = _load_case_module(module_name, filename)
            matches = module._forbidden_r_process_matches(
                "",
                "Warning messages:\n1: first warning\n2: second warning\n",
            )
            self.assertIn({"stream": "stderr", "pattern": "warning message"}, matches)

    @unittest.skipUnless(os.name == "nt", "Windows child architecture contract")
    def test_r_child_environment_restores_missing_arch_without_parent_mutation(self):
        original_parent = os.environ.get("PROCESSOR_ARCHITECTURE")
        for module_name, filename in (
            ("pbmc_case_driver_contract", "case_driver.py"),
            ("pbmc_prepare_environment_contract", "prepare_environment.py"),
        ):
            module = _load_case_module(module_name, filename)
            supplied = {"PBMC_CONTRACT_SENTINEL": "unchanged"}
            child, evidence = module._r_subprocess_environment(supplied)
            self.assertEqual({"PBMC_CONTRACT_SENTINEL": "unchanged"}, supplied)
            self.assertEqual("AMD64", child["PROCESSOR_ARCHITECTURE"])
            self.assertEqual("X64", evidence["native_architecture"])
            self.assertEqual("AMD64", evidence["processor_architecture"])
            self.assertTrue(evidence["processor_architecture_restored"])
            self.assertFalse(evidence["parent_environment_modified"])
            with self.assertRaises(module.CaseError if filename == "case_driver.py" else module.EnvironmentError):
                module._r_subprocess_environment({"PROCESSOR_ARCHITECTURE": "ARM64"})
            with self.assertRaises(module.CaseError if filename == "case_driver.py" else module.EnvironmentError):
                module._r_subprocess_environment({"PROCESSOR_ARCHITECTURE": "UNKNOWN"})
            with self.assertRaises(module.CaseError if filename == "case_driver.py" else module.EnvironmentError):
                module._r_subprocess_environment(
                    {"PROCESSOR_ARCHITECTURE": "AMD64", "PROCESSOR_ARCHITEW6432": "ARM64"}
                )
        self.assertEqual(original_parent, os.environ.get("PROCESSOR_ARCHITECTURE"))

    def test_router_selects_only_required_capabilities(self):
        completed = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "scripts/analysis_agent.py"),
                "route",
                "--request",
                str(CASE_DIR / "request.json"),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        capabilities = [item["capability"] for item in json.loads(completed.stdout)]
        self.assertEqual(["single-cell", "visualization"], capabilities)

    def test_case_driver_exposes_root_cli_contract(self):
        for command in ("run", "resume"):
            completed = subprocess.run(
                [sys.executable, str(CASE_DIR / "case_driver.py"), command, "--help"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(0, completed.returncode)
            for option in ("--run-root", "--cache-root", "--rscript", "--authorized"):
                self.assertIn(option, completed.stdout)
        for command in ("verify", "report"):
            completed = subprocess.run(
                [sys.executable, str(CASE_DIR / "case_driver.py"), command, "--help"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(0, completed.returncode)
            self.assertIn("--run-root", completed.stdout)

    def test_resume_preserves_byte_identical_materialized_artifacts(self):
        r_code = (CASE_DIR / "run_pipeline.R").read_text(encoding="utf-8")
        self.assertIn("promote_if_changed <- function", r_code)
        self.assertIn("identical(sha256_file(temporary), sha256_file(path))", r_code)
        self.assertIn("return(invisible(FALSE))", r_code)
        self.assertIn("copy_artifact_if_changed <- function", r_code)
        self.assertNotIn(
            'file.copy(file.path(import_stage, "feature_name_mapping.csv"),',
            r_code,
        )

    def test_committed_expected_output_is_verified(self):
        completed = subprocess.run(
            [sys.executable, str(CASE_DIR / "verify_expected_output.py")],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr or completed.stdout)
        summary = json.loads(completed.stdout)
        self.assertEqual("pass", summary["status"])
        self.assertEqual(38, summary["files_verified"])
        self.assertTrue(summary["native_visual_review_pass"])
        self.assertFalse(summary["raw_data_distributed"])

    def test_committed_expected_output_text_is_lf_canonical(self):
        text_suffixes = {".csv", ".json", ".jsonl", ".md", ".txt"}
        for path in (CASE_DIR / "expected-output").rglob("*"):
            if path.is_file() and path.suffix.lower() in text_suffixes:
                payload = path.read_bytes()
                self.assertNotIn(b"\r", payload, path.relative_to(CASE_DIR).as_posix())

    def test_no_private_absolute_path(self):
        for path in CASE_DIR.iterdir():
            if path.is_file() and path.suffix.lower() in {".md", ".json", ".py", ".r"}:
                text = path.read_text(encoding="utf-8-sig")
                self.assertNotIn("C:" + r"\Users\ExampleUser", text, path.name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
