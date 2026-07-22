import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "run_manager.py"
SPEC = importlib.util.spec_from_file_location("run_manager", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class RunManagerTests(unittest.TestCase):
    def _request(self, project: Path, input_path: Path) -> dict:
        return {
            "mode": "plan",
            "question": "对bulk RNA原始count矩阵进行DESeq2差异表达并绘制火山图",
            "modality": "bulk-rna",
            "project_root": str(project),
            "task_slug": "toy-bulk",
            "run_id": "run-test",
            "inputs": [str(input_path)],
            "statistical_unit": "patient",
            "group_column": "group",
            "multiplicity_method": "BH FDR",
            "data_scale": "raw-counts",
        }

    def test_init_and_validate_are_auditable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_path = root / "counts.tsv"
            input_path.write_text("gene\ts1\nsymbol\t1\n", encoding="utf-8")
            report = MODULE.initialise_run(self._request(root / "project", input_path))
            run_root = Path(report["run_root"])
            self.assertEqual(report["state"], "PLAN_COMPILED")
            self.assertTrue(MODULE.validate_run(run_root)["ok"])
            manifest = json.loads((run_root / "manifest" / "run_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["inputs"][0]["sha256"], MODULE.sha256_file(input_path))
            with self.assertRaises(MODULE.RunError):
                MODULE.initialise_run(self._request(root / "project", input_path))

    def test_checkpoint_promotes_only_hash_verified_artifacts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_path = root / "counts.tsv"
            input_path.write_text("gene\ts1\ng\t1\n", encoding="utf-8")
            report = MODULE.initialise_run(self._request(root / "project", input_path))
            run_root = Path(report["run_root"])
            MODULE.transition_run(run_root, "AWAITING_AUTHORIZATION")
            MODULE.transition_run(run_root, "ENV_PREPARING")
            MODULE.transition_run(run_root, "ENV_LOCKED")
            MODULE.transition_run(run_root, "RUNNING_STAGE")
            stage = run_root / "_staging" / "bulk-rna"
            stage.mkdir()
            table = stage / "result.tsv"
            table.write_text("gene\tlog2FC\ng\t1.0\n", encoding="utf-8")
            contract = {
                "artifacts": [
                    {
                        "artifact_id": "bulk-de-table",
                        "artifact_type": "table",
                        "format": "tsv",
                        "schema": None,
                        "unit": "gene",
                        "modality": "bulk-rna",
                        "producer": "bulk-rna",
                        "consumers": ["analysis-qa"],
                        "relative_path": "_staging/bulk-rna/result.tsv",
                        "sha256": MODULE.sha256_file(table),
                        "sensitivity": "internal",
                        "validation": ["non-empty", "schema checked"],
                        "claim_role": "exploratory",
                    }
                ]
            }
            contract_path = root / "contract.json"
            contract_path.write_text(json.dumps(contract), encoding="utf-8")
            result = MODULE.checkpoint_stage(run_root, "bulk-rna", contract_path)
            self.assertEqual(result["state"], "CHECKPOINTED")
            self.assertTrue((run_root / "04_intermediate" / "bulk-rna" / "result.tsv").is_file())
            self.assertTrue(MODULE.validate_run(run_root)["ok"])

    def test_bad_hash_does_not_promote_stage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_path = root / "counts.tsv"
            input_path.write_text("x", encoding="utf-8")
            report = MODULE.initialise_run(self._request(root / "project", input_path))
            run_root = Path(report["run_root"])
            for state in ("AWAITING_AUTHORIZATION", "ENV_PREPARING", "ENV_LOCKED", "RUNNING_STAGE"):
                MODULE.transition_run(run_root, state)
            stage = run_root / "_staging" / "bulk-rna"
            stage.mkdir()
            (stage / "bad.tsv").write_text("changed", encoding="utf-8")
            contract_path = root / "contract.json"
            contract_path.write_text(json.dumps({"artifacts": [{"relative_path": "_staging/bulk-rna/bad.tsv", "sha256": "0" * 64}]}), encoding="utf-8")
            with self.assertRaises(MODULE.RunError):
                MODULE.checkpoint_stage(run_root, "bulk-rna", contract_path)
            self.assertTrue(stage.is_dir())
            self.assertFalse((run_root / "04_intermediate" / "bulk-rna").exists())

    def test_native_review_can_iterate_and_checkpoint_visual_revision(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_path = root / "counts.tsv"
            input_path.write_text("gene\ts1\ng\t1\n", encoding="utf-8")
            report = MODULE.initialise_run(self._request(root / "project", input_path))
            run_root = Path(report["run_root"])
            for state in (
                "AWAITING_AUTHORIZATION", "ENV_PREPARING", "ENV_LOCKED", "RUNNING_STAGE",
                "STAGE_VALIDATING", "CHECKPOINTED", "ANALYSIS_QA", "VISUALIZING",
                "NATIVE_VISUAL_REVIEW", "VISUALIZING",
            ):
                MODULE.transition_run(run_root, state)
            stage = run_root / "_staging" / "visual-revision"
            stage.mkdir()
            figure = stage / "figure.png"
            figure.write_bytes(b"png-fixture")
            contract_path = root / "visual-contract.json"
            contract_path.write_text(json.dumps({"artifacts": [{
                "artifact_id": "visual-revision-figure",
                "artifact_type": "figure",
                "format": "png",
                "schema": None,
                "unit": "figure",
                "modality": "bulk-rna",
                "producer": "visual-revision",
                "consumers": ["native-visual-review"],
                "relative_path": "_staging/visual-revision/figure.png",
                "sha256": MODULE.sha256_file(figure),
                "sensitivity": "internal",
                "validation": ["native review pending"],
                "claim_role": "visual-evidence",
            }]}), encoding="utf-8")
            result = MODULE.checkpoint_stage(run_root, "visual-revision", contract_path)
            self.assertEqual(result["state"], "VISUALIZING")
            manifest = json.loads((run_root / "manifest" / "run_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["state"], "VISUALIZING")
            self.assertTrue((run_root / "04_intermediate" / "visual-revision" / "figure.png").is_file())

    def test_validate_rejects_duplicate_artifact_registration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_path = root / "counts.tsv"
            input_path.write_text("gene\ts1\ng\t1\n", encoding="utf-8")
            report = MODULE.initialise_run(self._request(root / "project", input_path))
            run_root = Path(report["run_root"])
            for state in ("AWAITING_AUTHORIZATION", "ENV_PREPARING", "ENV_LOCKED", "RUNNING_STAGE"):
                MODULE.transition_run(run_root, state)
            stage = run_root / "_staging" / "bulk-rna"
            stage.mkdir()
            result_file = stage / "result.tsv"
            result_file.write_text("gene\tlog2FC\ng\t1.0\n", encoding="utf-8")
            contract_path = root / "contract.json"
            contract_path.write_text(json.dumps({"artifacts": [{"artifact_id": "result", "artifact_type": "table", "format": "tsv", "schema": None, "unit": "gene", "modality": "bulk-rna", "producer": "bulk-rna", "consumers": [], "relative_path": "_staging/bulk-rna/result.tsv", "sha256": MODULE.sha256_file(result_file), "sensitivity": "internal", "validation": ["hash"], "claim_role": "exploratory"}]}), encoding="utf-8")
            MODULE.checkpoint_stage(run_root, "bulk-rna", contract_path)
            ledger = run_root / "manifest" / "artifact_ledger.jsonl"
            first_event = ledger.read_text(encoding="utf-8").strip()
            with ledger.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(first_event + "\n")
            validation = MODULE.validate_run(run_root)
            self.assertFalse(validation["ok"])
            self.assertTrue(any(error.startswith("duplicate-ledger-artifact:") for error in validation["errors"]))

    def test_delivered_run_rejects_pending_stage_and_review_split_brain(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_path = root / "counts.tsv"
            input_path.write_text("gene\ts1\ng\t1\n", encoding="utf-8")
            report = MODULE.initialise_run(self._request(root / "project", input_path))
            run_root = Path(report["run_root"])
            manifest_path = run_root / "manifest" / "run_manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["state"] = "DELIVERED"
            manifest["exit_code"] = 0
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            validation = MODULE.validate_run(run_root)
            self.assertFalse(validation["ok"])
            self.assertTrue(any(error.startswith("delivered-stage-not-complete:") for error in validation["errors"]))
            self.assertIn("delivered-final-figure-notes-not-registered:07_reports/FIGURE_NOTES.md", validation["errors"])

    def test_resume_audit_requires_same_inputs_lock_and_checkpoint(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_path = root / "counts.tsv"
            input_path.write_text("x", encoding="utf-8")
            report = MODULE.initialise_run(self._request(root / "project", input_path))
            run_root = Path(report["run_root"])
            blocked = MODULE.audit_resume(run_root)
            self.assertFalse(blocked["ok"])
            self.assertIn("environment-lock-not-verified", blocked["errors"])
            self.assertIn("no-validated-checkpoint", blocked["errors"])

            for state in ("AWAITING_AUTHORIZATION", "ENV_PREPARING", "ENV_LOCKED", "RUNNING_STAGE"):
                MODULE.transition_run(run_root, state)
            stage = run_root / "_staging" / "bulk-rna"
            stage.mkdir()
            result_file = stage / "result.tsv"
            result_file.write_text("ok", encoding="utf-8")
            contract_path = root / "contract.json"
            contract_path.write_text(json.dumps({"artifacts": [{"artifact_id": "result", "artifact_type": "table", "format": "tsv", "schema": None, "unit": "gene", "modality": "bulk-rna", "producer": "bulk-rna", "consumers": [], "relative_path": "_staging/bulk-rna/result.tsv", "sha256": MODULE.sha256_file(result_file), "sensitivity": "internal", "validation": ["hash"], "claim_role": "exploratory"}]}), encoding="utf-8")
            MODULE.checkpoint_stage(run_root, "bulk-rna", contract_path)
            environment_path = run_root / "02_environment" / "environment_manifest.json"
            marker_path = run_root / "02_environment" / "environment.locked.json"
            marker = {"lock_hash": "a" * 64, "platform": "windows-amd64", "backend": "python-uv"}
            marker_path.write_text(json.dumps(marker), encoding="utf-8")
            lock_path = run_root / "02_environment" / "requirements.lock.txt"
            lock_path.write_text("example==1.0\n", encoding="utf-8")
            environment_path.write_text(json.dumps({
                "state": "frozen",
                "environments": [{
                    "lock_hash": "a" * 64,
                    "platform": "windows-amd64",
                    "backend": "python-uv",
                    "frozen": True,
                    "marker_relative_path": "02_environment/environment.locked.json",
                    "marker_sha256": MODULE.sha256_file(marker_path),
                    "lockfiles": [{
                        "relative_path": "02_environment/requirements.lock.txt",
                        "sha256": MODULE.sha256_file(lock_path),
                    }],
                }],
            }), encoding="utf-8")
            ready = MODULE.audit_resume(run_root)
            self.assertTrue(ready["ok"], ready)
            self.assertTrue(ready["latest_valid_checkpoint"].endswith("bulk-rna"))

            marker_path.write_text(json.dumps(dict(marker, lock_hash="b" * 64)), encoding="utf-8")
            forged = MODULE.audit_resume(run_root)
            self.assertFalse(forged["ok"])
            self.assertIn("environment-lock-not-verified", forged["errors"])
            marker_path.write_text(json.dumps(marker), encoding="utf-8")

            input_path.write_text("changed", encoding="utf-8")
            changed = MODULE.audit_resume(run_root)
            self.assertFalse(changed["ok"])
            self.assertIn("input[0]-hash-changed", changed["errors"])


if __name__ == "__main__":
    unittest.main()
