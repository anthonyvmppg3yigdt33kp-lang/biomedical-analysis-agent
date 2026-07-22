import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "analysis_agent.py"
SPEC = importlib.util.spec_from_file_location("analysis_agent", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class RouterTests(unittest.TestCase):
    def test_explicit_modality_and_visualization_are_composed(self):
        request = {
            "mode": "plan",
            "question": "单细胞差异分析并绘制UMAP结果图",
            "modality": "single-cell",
        }
        routes = MODULE.route_request(request)
        self.assertEqual(routes[0]["capability"], "single-cell")
        self.assertIn("visualization", {item["capability"] for item in routes})
        self.assertEqual(routes, MODULE.route_request(dict(reversed(list(request.items())))))

    def test_unknown_domain_uses_safe_methodology_default(self):
        routes = MODULE.route_request({"mode": "plan", "question": "Evaluate this unusual assay"})
        self.assertEqual(routes[0]["capability"], "literature-methodology")

    def test_multiomics_route_uses_installed_skill_name(self):
        routes = MODULE.route_request({"mode": "plan", "question": "Run MOFA2 integration", "modality": "multi-omics"})
        selected = next(item for item in routes if item["capability"] == "multi-omics")
        self.assertEqual(selected["skill"], "multi-omics-pipeline")

    def test_visualization_modality_and_plural_keywords_route(self):
        explicit = MODULE.route_request({
            "mode": "plan",
            "question": "Prepare publication outputs",
            "modality": "visualization",
        })
        self.assertEqual(explicit[0]["capability"], "visualization")
        for wording in ("Prepare publication figures", "Compare diagnostic plots"):
            with self.subTest(wording=wording):
                routes = MODULE.route_request({"mode": "plan", "question": wording})
                self.assertEqual(routes[0]["capability"], "visualization")

    def test_spatial_transcriptomics_does_not_imply_bulk_rna(self):
        routes = MODULE.route_request({
            "mode": "plan",
            "question": "空间转录组结果图方法选择",
            "modality": "spatial",
        })
        self.assertNotIn("bulk-rna", {item["capability"] for item in routes})

    def test_invalid_mode_is_rejected(self):
        with self.assertRaises(MODULE.RequestError):
            MODULE.compile_plan({"mode": "install", "question": "x"})


class CompilerTests(unittest.TestCase):
    def test_plan_is_deterministic_and_frozen(self):
        request = {
            "mode": "plan",
            "question": "Use DESeq2 for bulk RNA differential expression",
            "modality": "bulk-rna",
            "project_root": "D:\\analysis",
        }
        first = MODULE.compile_plan(request)
        second = MODULE.compile_plan(json.loads(json.dumps(request)))
        self.assertEqual(first, second)
        self.assertTrue(first["frozen"])
        self.assertTrue(first["workflow"]["frozen"])
        self.assertEqual(first["workflow"]["plan_id"], first["plan_id"])
        self.assertEqual(first["workflow"]["request_sha256"], first["request_sha256"])
        self.assertTrue(all("recipe_id" in node and "skill" in node for node in first["workflow"]["nodes"]))
        self.assertEqual(first["state"], "PLAN_COMPILED")
        self.assertTrue(any(gate["status"] == "deferred" for gate in first["scientific_gates"]))
        self.assertTrue(first["run"]["root"].startswith("D:\\analysis\\runs\\"))

    def test_complete_authorized_run_reaches_environment_preparation(self):
        request = {
            "mode": "run",
            "question": "单细胞 donor-aware 差异分析",
            "modality": "single-cell",
            "inputs": ["D:/data/object.rds"],
            "execution_authorized": True,
            "authorization_scope": "task-local",
            "statistical_unit": "donor",
            "donor_id_column": "donor_id",
            "group_column": "condition",
            "contrast": ["case", "control"],
            "multiplicity_method": "BH FDR",
        }
        plan = MODULE.compile_plan(request)
        self.assertEqual(plan["blocking_issues"], [])
        self.assertEqual(plan["state"], "ENV_PREPARING")

    def test_unauthorized_run_is_blocked(self):
        plan = MODULE.compile_plan({
            "mode": "run",
            "question": "Make a descriptive bulk RNA QC plot",
            "modality": "bulk-rna",
            "inputs": ["D:/data/counts.tsv"],
        })
        ids = {item["id"] for item in plan["blocking_issues"]}
        self.assertIn("execution-authorization", ids)
        self.assertEqual(plan["state"], "AWAITING_AUTHORIZATION")

    def test_count_model_rejects_normalized_values(self):
        plan = MODULE.compile_plan({
            "mode": "run",
            "question": "Run DESeq2 differential expression",
            "modality": "bulk-rna",
            "inputs": ["D:/data/expression.tsv"],
            "execution_authorized": True,
            "authorization_scope": "task-local",
            "statistical_unit": "patient",
            "group_column": "group",
            "multiplicity_method": "BH FDR",
            "data_scale": "TPM",
        })
        ids = {item["id"] for item in plan["blocking_issues"]}
        self.assertIn("count-scale", ids)

    def test_ml_random_row_split_is_blocked(self):
        plan = MODULE.compile_plan({
            "mode": "run",
            "question": "Train a machine learning classifier",
            "inputs": ["D:/data/features.tsv"],
            "execution_authorized": True,
            "authorization_scope": "task-local",
            "statistical_unit": "patient",
            "multiplicity_method": "BH FDR",
            "outcome": "response",
            "split_strategy": "random row split",
        })
        self.assertIn("ml-validation", {item["id"] for item in plan["blocking_issues"]})

    def test_explicit_descriptive_contract_suppresses_generic_inference_words(self):
        requests = [
            {
                "mode": "plan",
                "question": "Create figures and plots without inferential claims about associations or effects",
                "modality": "visualization",
                "inferential": False,
            },
            {
                "mode": "plan",
                "question": "Plot associations and effects as descriptive annotations only",
                "analysis_scope": "descriptive-only",
            },
        ]
        for request in requests:
            with self.subTest(request=request):
                gate_ids = {item["id"] for item in MODULE.compile_plan(request)["scientific_gates"]}
                self.assertNotIn("statistical-unit", gate_ids)
                self.assertNotIn("multiple-testing", gate_ids)

    def test_descriptive_contract_cannot_bypass_differential_or_ml_gates(self):
        cases = [
            (
                {
                    "mode": "plan",
                    "question": "Run DESeq2 differential expression",
                    "inferential": False,
                    "data_scale": "counts",
                },
                {"statistical-unit", "multiple-testing", "contrast", "count-scale"},
            ),
            (
                {
                    "mode": "plan",
                    "question": "Train a machine learning classifier",
                    "analysis_scope": "descriptive-only",
                },
                {"statistical-unit", "multiple-testing", "ml-outcome", "ml-validation"},
            ),
        ]
        for request, expected in cases:
            with self.subTest(request=request):
                gate_ids = {item["id"] for item in MODULE.compile_plan(request)["scientific_gates"]}
                self.assertTrue(expected <= gate_ids)

    def test_explicit_inferential_true_forces_core_inference_gates(self):
        plan = MODULE.compile_plan({
            "mode": "plan",
            "question": "Create descriptive figures",
            "modality": "visualization",
            "inferential": True,
        })
        gate_ids = {item["id"] for item in plan["scientific_gates"]}
        self.assertIn("statistical-unit", gate_ids)
        self.assertIn("multiple-testing", gate_ids)

    def test_conflicting_or_invalid_scope_contract_is_rejected(self):
        invalid = [
            {"mode": "plan", "question": "x", "inferential": "false"},
            {"mode": "plan", "question": "x", "analysis_scope": "descriptive-only", "inferential": True},
            {"mode": "plan", "question": "x", "analysis_scope": "unsupported"},
        ]
        for request in invalid:
            with self.subTest(request=request):
                with self.assertRaises(MODULE.RequestError):
                    MODULE.compile_plan(request)

    def test_mode_specific_gates_exist(self):
        cases = {
            "resume": "resume-checkpoint",
            "reproduce-figure": "reference-figure",
            "explain": "explain-artifact",
        }
        for mode, gate_id in cases.items():
            with self.subTest(mode=mode):
                plan = MODULE.compile_plan({"mode": mode, "question": "audit result"})
                self.assertIn(gate_id, {item["id"] for item in plan["scientific_gates"]})

    def test_state_machine_and_output_contract_are_complete(self):
        plan = MODULE.compile_plan({"mode": "plan", "question": "文献方法学分析"})
        self.assertEqual(tuple(plan["state_machine"]), MODULE.STATE_MACHINE)
        self.assertEqual(set(plan["output_contract"]["directories"]), set(MODULE.RUN_TREE))
        self.assertIn("manifest/artifact_ledger.jsonl", plan["output_contract"]["required_files"])


class CliTests(unittest.TestCase):
    def test_compile_cli_writes_utf8_json(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            request_path = directory / "request.json"
            output_path = directory / "plan.json"
            request_path.write_text(
                json.dumps({"mode": "plan", "question": "空间转录组可视化"}, ensure_ascii=False),
                encoding="utf-8",
            )
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "compile", "--request", str(request_path), "--output", str(output_path)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["mode"], "plan")


if __name__ == "__main__":
    unittest.main()
