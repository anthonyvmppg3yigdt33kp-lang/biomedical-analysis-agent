import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
CI = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
REAL = (ROOT / ".github" / "workflows" / "real-data-release-gate.yml").read_text(encoding="utf-8")
ARTIFACT_HELPER = ROOT / ".github" / "scripts" / "tutorial_ci_artifact.py"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


ARTIFACT = load_module("tutorial_ci_artifact", ARTIFACT_HELPER)
CI_VALIDATOR = load_module(
    "tutorial_ci_validator_fixture",
    ROOT / "scripts" / "validate_tutorial_ci_output.py",
)
CI_FIXTURE = load_module(
    "tutorial_ci_test_fixture",
    ROOT / "tests" / "test_validate_tutorial_ci_output.py",
)


def write_json(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")


def visualization_profile():
    return {
        "schema_version": "1.0.0",
        "profile_id": "biomedical-public-runtime-v1",
        "purpose": "Public formal-Recipe runtime fixture.",
        "capability_scope": ARTIFACT.EXPECTED_VISUALIZATION_CAPABILITY_SCOPE,
        "overlay_files": ARTIFACT.EXPECTED_VISUALIZATION_OVERLAY_FILES,
        "included_rights_boundary": {
            "original_code_license": "MIT",
            "third_party_rights_status": ARTIFACT.EXPECTED_VISUALIZATION_RIGHTS_STATUS,
            "notice_file": "NOTICE.md",
        },
        "excluded_paths": ARTIFACT.EXPECTED_VISUALIZATION_EXCLUDED_PATHS,
        "exclusion_reasons": {
            path: "Excluded from the public runtime fixture."
            for path in ARTIFACT.EXPECTED_VISUALIZATION_EXCLUDED_PATHS
        },
        "raw_third_party_data_included": False,
        "raw_extracted_source_code_included": False,
    }


def make_pbmc_bundle_sources(root: Path, commit: str) -> tuple[Path, Path, Path]:
    run_root = root / "run"
    evidence = root / "evidence"
    CI_FIXTURE.make_pbmc(run_root)
    report = run_root / "07_reports" / "RESULTS.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("# PBMC3K computational result\n", encoding="utf-8")
    plan = {"schema_version": "1.0.0", "case": "pbmc3k", "plan_id": "fixture"}
    write_json(run_root / "01_plan" / "root-compiled-plan.json", plan)
    write_json(evidence / "compiled-plan.json", plan)
    validation = CI_VALIDATOR.validate("pbmc3k", run_root)
    write_json(run_root / "manifest" / "ci-validation-summary.json", validation)

    skill_hashes = {
        name: {"status": "installed", "sha256": hashlib.sha256(name.encode()).hexdigest()}
        for name in ARTIFACT.EXPECTED_SKILLS
    }
    verified_hashes = {
        name: {"status": "verified-existing", "sha256": record["sha256"]}
        for name, record in skill_hashes.items()
    }
    skills_lock = root / "skills.lock.json"
    write_json(
        skills_lock,
        {
            "schema_version": "1.0.0",
            "dependencies": {
                "visualization-2026718-v1": {
                    "repository": "https://github.com/anthonyvmppg3yigdt33kp-lang/visualization-2026718-v1.git",
                    "commit": "b" * 40,
                    "subdirectory": "skill/visualization-2026718-v1",
                    "distribution_profile_file": "public-install-profile.json",
                    "excluded_paths": [
                        "assets/previews-curated",
                        "assets/scheme-candidates",
                        "assets/source_archive",
                        "references/catalog.jsonl",
                    ],
                    "content_sha256": skill_hashes["visualization-2026718-v1"]["sha256"],
                    "original_code_license": "MIT",
                    "license_file": "LICENSE",
                    "third_party_notice_file": "NOTICE.md",
                    "rights_status": "mixed-original-and-third-party-not-relicensed",
                }
            },
        },
    )
    private_destination = "C:" + r"\Users\runneradmin\private-task-skills"
    write_json(
        evidence / "bootstrap-install.json",
        {
            "schema_version": "1.0.0",
            "ok": True,
            "destination": private_destination,
            "global_skills_modified": False,
            "skills": skill_hashes,
            "visualization_distribution_profile": visualization_profile(),
            "visualization_excluded_paths_absent": (
                ARTIFACT.EXPECTED_VISUALIZATION_EXCLUDED_PATHS
            ),
            "visualization_overlay_targets_verified": (
                ARTIFACT.EXPECTED_VISUALIZATION_OVERLAY_FILES
            ),
            "visualization_runtime_manifest_exclusions_absent": (
                ARTIFACT.EXPECTED_VISUALIZATION_EXCLUDED_PATHS
            ),
        },
    )
    write_json(
        evidence / "bootstrap-verify.json",
        {
            "schema_version": "1.0.0",
            "ok": True,
            "destination": private_destination,
            "global_skills_modified": False,
            "skills": verified_hashes,
            "visualization_distribution_profile": visualization_profile(),
            "visualization_excluded_paths_absent": (
                ARTIFACT.EXPECTED_VISUALIZATION_EXCLUDED_PATHS
            ),
            "visualization_overlay_targets_verified": (
                ARTIFACT.EXPECTED_VISUALIZATION_OVERLAY_FILES
            ),
            "visualization_runtime_manifest_exclusions_absent": (
                ARTIFACT.EXPECTED_VISUALIZATION_EXCLUDED_PATHS
            ),
        },
    )
    write_json(
        evidence / "authorization-negative.json",
        {
            "schema_version": "1.0.0",
            "ok": True,
            "case": "pbmc3k",
            "check": "unauthorized_run_blocked_before_writes",
            "observed_returncode": 2,
            "run_root_created": False,
        },
    )
    plan_sha = hashlib.sha256((evidence / "compiled-plan.json").read_bytes()).hexdigest()
    lock_sha = hashlib.sha256((run_root / "02_environment" / "renv.lock").read_bytes()).hexdigest()
    write_json(
        evidence / "resume-cache-evidence.json",
        {
            "schema_version": "1.0.0",
            "ok": True,
            "case": "pbmc3k",
            "check": "plan_bound_fresh_resume_shared_cache_and_immutable_artifacts",
            "fresh_returncode": 0,
            "resume_returncode": 0,
            "shared_cache_root": True,
            "explicit_environment_cache_reuse": True,
            "task_local_bootstrap": True,
            "host_package_required": False,
            "compiled_plan_sha256": plan_sha,
            "executed_plan_sha256": plan_sha,
            "fresh_renv_lock_sha256": lock_sha,
            "resume_renv_lock_sha256": lock_sha,
            "immutable_lock": True,
            "immutable_checkpoint_and_analysis_artifacts": True,
            "checkpoint_records": 1,
            "analysis_artifacts": 1,
            "checkpoint_reuse_log_records": 1,
        },
    )
    write_json(
        evidence / "checksum-negative.json",
        {
            "schema_version": "1.0.0",
            "ok": True,
            "case": "pbmc3k",
            "observed_returncode": 2,
            "failure_code": "INPUT_CHECKSUM_MISMATCH_REJECTED",
            "stderr_sha256": "a" * 64,
            "failure_closed": True,
        },
    )
    write_json(
        evidence / "nonzero-negative.json",
        {
            "schema_version": "1.0.0",
            "ok": True,
            "case": "pbmc3k",
            "check": "nonzero_environment_exit_blocked",
            "observed_returncode": 2,
            "completion_marker_created": False,
            "dedicated_fault_sentinel_observed": True,
        },
    )
    return run_root, evidence, skills_lock


def make_visium_bundle_sources(root: Path, commit: str) -> tuple[Path, Path, Path]:
    run_root, evidence, skills_lock = make_pbmc_bundle_sources(root, commit)
    shutil.rmtree(run_root)
    CI_FIXTURE.make_visium(run_root)
    report = run_root / "07_reports" / "RESULTS.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("# Visium computational result\n", encoding="utf-8")
    plan = {
        "schema_version": "1.0.0",
        "case": "visium-mouse-brain",
        "plan_id": "fixture",
    }
    write_json(run_root / "01_plan" / "root-compiled-plan.json", plan)
    write_json(evidence / "compiled-plan.json", plan)
    validation = CI_VALIDATOR.validate("visium-mouse-brain", run_root)
    write_json(run_root / "manifest" / "ci-validation-summary.json", validation)
    write_json(
        evidence / "authorization-negative.json",
        {
            "schema_version": "1.0.0",
            "ok": True,
            "case": "visium-mouse-brain",
            "check": "unauthorized_run_blocked_before_writes",
            "observed_returncode": 2,
            "run_root_created": False,
        },
    )
    plan_sha = hashlib.sha256((evidence / "compiled-plan.json").read_bytes()).hexdigest()
    lock_sha = hashlib.sha256((run_root / "02_environment" / "renv.lock").read_bytes()).hexdigest()
    write_json(
        evidence / "resume-cache-evidence.json",
        {
            "schema_version": "1.0.0",
            "ok": True,
            "case": "visium-mouse-brain",
            "check": "plan_bound_fresh_resume_shared_cache_and_immutable_artifacts",
            "fresh_returncode": 0,
            "resume_returncode": 0,
            "shared_cache_root": True,
            "explicit_environment_cache_reuse": True,
            "explicit_input_cache_reuse": True,
            "input_cache_direct_read_no_copy": True,
            "input_cache_external_to_run_root": True,
            "canonical_inputs_modified": False,
            "task_local_bootstrap": True,
            "host_package_required": False,
            "compiled_plan_sha256": plan_sha,
            "executed_plan_sha256": plan_sha,
            "fresh_renv_lock_sha256": lock_sha,
            "resume_renv_lock_sha256": lock_sha,
            "immutable_lock": True,
            "immutable_checkpoint_and_analysis_artifacts": True,
            "checkpoint_records": 1,
            "analysis_artifacts": 1,
            "checkpoint_reuse_log_records": 1,
        },
    )
    write_json(
        evidence / "checksum-negative.json",
        {
            "schema_version": "1.0.0",
            "ok": True,
            "case": "visium-mouse-brain",
            "observed_returncode": 2,
            "failure_code": "INPUT_CHECKSUM_MISMATCH_REJECTED",
            "stderr_sha256": "a" * 64,
            "failure_closed": True,
        },
    )
    write_json(
        evidence / "nonzero-negative.json",
        {
            "schema_version": "1.0.0",
            "ok": True,
            "case": "visium-mouse-brain",
            "check": "nonzero_environment_exit_blocked",
            "observed_returncode": 2,
            "completion_marker_created": False,
            "dedicated_fault_sentinel_observed": True,
        },
    )
    return run_root, evidence, skills_lock


def test_ci_is_windows_exact_python_and_r_with_static_gates():
    assert "runs-on: windows-latest" in CI
    assert 'python-version: "3.13"' in CI
    assert 'r-version: "4.5.3"' in CI
    for command in (
        "python -m pytest -q",
        "evaluate_retrieval_benchmark.py",
        "router-confusion-regressions.jsonl",
        "validate_distribution.py",
        "bootstrap_skills.py",
        "ci_r_smoke.R",
    ):
        assert command in CI


def test_every_pip_cache_uses_the_tracked_dependency_file():
    for workflow in (CI, REAL):
        assert workflow.count("cache-dependency-path: requirements-dev.txt") == workflow.count(
            "cache: pip"
        )


def test_all_third_party_actions_are_full_sha_pinned():
    for workflow in (CI, REAL):
        uses = re.findall(r"(?m)^\s*uses:\s+([^\s#]+)", workflow)
        assert uses
        for reference in uses:
            assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", reference), reference
        checkout_count = sum(reference.startswith("actions/checkout@") for reference in uses)
        assert workflow.count("persist-credentials: false") == checkout_count


def test_real_data_gate_requires_explicit_commit_and_has_no_publish_permission():
    assert "workflow_dispatch:" in REAL
    assert "expected_commit:" in REAL
    assert "RUN_REAL_DATA_V1_0_0" in REAL
    assert "contents: read" in REAL
    assert "contents: write" not in REAL
    assert "gh release create" not in REAL
    assert "matrix:" in REAL
    assert "pbmc3k" in REAL and "visium-mouse-brain" in REAL
    assert "not_asserted_by_ci" not in REAL
    assert '"05_results" / "tables"' in ARTIFACT_HELPER.read_text(encoding="utf-8")
    assert "/05_results/objects/" not in REAL
    for script in ("validate_tutorial_ci_output.py", "inject_tutorial_checksum_failure.py"):
        assert script in REAL
        assert (ROOT / "scripts" / script).is_file()
    assert "--inject-error-before-exit" in REAL
    assert "inject_environment_fault.py" in REAL
    assert "resume-cache-evidence.json" in REAL
    assert "Resume changed the frozen environment lock" in REAL


def test_real_data_gate_uses_case_specific_fault_boundaries_and_uploads_only_verified_bundle():
    assert '"nonzero-cache-$env:CASE_ID"' in REAL
    assert "inject_environment_fault.py" in REAL
    assert "native R cache validator" in REAL
    assert "canonical_run_modified" in REAL
    assert "--input-cache-root" in REAL
    assert ".github/scripts/tutorial_ci_artifact.py build" in REAL
    assert "verify_tutorial_ci_artifact.py') verify" in REAL
    upload = REAL.split("- name: Upload only the self-verifying minimal evidence bundle", 1)[1]
    upload = upload.split("\n\n  gate-summary:", 1)[0]
    assert "tutorial-evidence-${{ matrix.case }}/" in upload
    for forbidden in (
        "/inputs/",
        "/04_intermediate/",
        "/05_results/objects/",
        "/02_environment/",
        "/nonzero-",
        "/logs/",
    ):
        assert forbidden not in upload


def test_real_data_bundle_is_exact_hash_bound_minimal_and_self_verifying(tmp_path):
    commit = "a" * 40
    run_root, evidence, skills_lock = make_pbmc_bundle_sources(tmp_path, commit)
    bundle = tmp_path / "bundle"
    report = ARTIFACT.build_bundle(
        case="pbmc3k",
        commit=commit,
        run_root=run_root,
        evidence_root=evidence,
        skills_lock=skills_lock,
        output=bundle,
    )
    assert report["ok"] is True
    assert report["evidence_type"] == "tutorial-bundle-verification"
    assert report["original_final_figure_pairs"] == 5
    assert not list(bundle.rglob("*.rds"))
    assert not (bundle / "run" / "inputs").exists()
    assert not (bundle / "run" / "04_intermediate").exists()
    assert not (bundle / "run" / "05_results" / "objects").exists()
    bundled_lock = json.loads(
        (bundle / "gate-evidence" / "skills.lock.json").read_text(encoding="utf-8")
    )
    bundled_bootstrap = json.loads(
        (bundle / "gate-evidence" / "skill-bootstrap-evidence.json").read_text(
            encoding="utf-8"
        )
    )
    expected_rights = {
        "original_code_license": "MIT",
        "license_file": "LICENSE",
        "third_party_notice_file": "NOTICE.md",
        "rights_status": "mixed-original-and-third-party-not-relicensed",
        "distribution_profile_file": "public-install-profile.json",
        "excluded_paths": [
            "assets/previews-curated",
            "assets/scheme-candidates",
            "assets/source_archive",
            "references/catalog.jsonl",
        ],
    }
    bundled_dependency = bundled_lock["dependencies"]["visualization-2026718-v1"]
    for key, value in expected_rights.items():
        assert bundled_dependency[key] == value
        assert bundled_bootstrap["visualization_dependency"][key] == value
    assert "license" not in bundled_dependency
    assert "notice_file" not in bundled_dependency
    assert "license" not in bundled_bootstrap["visualization_dependency"]
    assert "notice_file" not in bundled_bootstrap["visualization_dependency"]
    assert bundled_bootstrap["visualization_distribution_profile"] == (
        visualization_profile()
    )
    assert bundled_bootstrap["visualization_excluded_paths_absent"] == (
        ARTIFACT.EXPECTED_VISUALIZATION_EXCLUDED_PATHS
    )
    assert bundled_bootstrap["visualization_overlay_targets_verified"] == (
        ARTIFACT.EXPECTED_VISUALIZATION_OVERLAY_FILES
    )
    assert bundled_bootstrap[
        "visualization_runtime_manifest_exclusions_absent"
    ] == ARTIFACT.EXPECTED_VISUALIZATION_EXCLUDED_PATHS
    serialized = "\n".join(
        path.read_text(encoding="utf-8-sig")
        for path in bundle.rglob("*")
        if path.is_file() and path.suffix.lower() in ARTIFACT.TEXT_SUFFIXES
    )
    assert ("C:" + r"\Users\runneradmin") not in serialized
    assert ARTIFACT.verify_bundle(bundle, "pbmc3k", commit)["ok"] is True

    target = bundle / "run" / "07_reports" / "RESULTS.md"
    target.write_text(target.read_text(encoding="utf-8") + "tampered\n", encoding="utf-8")
    with pytest.raises(ARTIFACT.BundleError, match="size mismatch|hash mismatch"):
        ARTIFACT.verify_bundle(bundle, "pbmc3k", commit)


def test_bundle_build_and_verify_require_visualization_rights_contract(tmp_path):
    commit = "a" * 40
    run_root, evidence, skills_lock = make_pbmc_bundle_sources(tmp_path, commit)
    lock = json.loads(skills_lock.read_text(encoding="utf-8"))
    lock["dependencies"]["visualization-2026718-v1"].pop(
        "third_party_notice_file"
    )
    write_json(skills_lock, lock)
    with pytest.raises(ARTIFACT.BundleError, match="skills.lock.json"):
        ARTIFACT.build_bundle(
            case="pbmc3k",
            commit=commit,
            run_root=run_root,
            evidence_root=evidence,
            skills_lock=skills_lock,
            output=tmp_path / "missing-notice-bundle",
        )

    run_root, evidence, skills_lock = make_pbmc_bundle_sources(tmp_path / "valid", commit)
    bundle = tmp_path / "valid-bundle"
    ARTIFACT.build_bundle(
        case="pbmc3k",
        commit=commit,
        run_root=run_root,
        evidence_root=evidence,
        skills_lock=skills_lock,
        output=bundle,
    )
    bundled_lock_path = bundle / "gate-evidence" / "skills.lock.json"
    bundled_lock = json.loads(bundled_lock_path.read_text(encoding="utf-8"))
    bundled_lock["dependencies"]["visualization-2026718-v1"].pop(
        "third_party_notice_file"
    )
    write_json(bundled_lock_path, bundled_lock)
    manifest_path = bundle / "bundle-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    record = next(
        item
        for item in manifest["files"]
        if item["path"] == "gate-evidence/skills.lock.json"
    )
    record["size_bytes"] = bundled_lock_path.stat().st_size
    record["sha256"] = hashlib.sha256(bundled_lock_path.read_bytes()).hexdigest()
    manifest["total_size_bytes"] = sum(item["size_bytes"] for item in manifest["files"])
    write_json(manifest_path, manifest)
    with pytest.raises(
        ARTIFACT.BundleError,
        match="profile evidence is invalid|bootstrap evidence is incomplete",
    ):
        ARTIFACT.verify_bundle(bundle, "pbmc3k", commit)


def test_bundle_build_rejects_visualization_profile_overlay_tampering(tmp_path):
    commit = "a" * 40
    run_root, evidence, skills_lock = make_pbmc_bundle_sources(tmp_path, commit)
    install_path = evidence / "bootstrap-install.json"
    install = json.loads(install_path.read_text(encoding="utf-8"))
    install["visualization_distribution_profile"]["overlay_files"][
        "SKILL.public-runtime.md"
    ] = "SKILL.audit.md"
    write_json(install_path, install)

    with pytest.raises(ARTIFACT.BundleError, match="profile evidence is invalid"):
        ARTIFACT.build_bundle(
            case="pbmc3k",
            commit=commit,
            run_root=run_root,
            evidence_root=evidence,
            skills_lock=skills_lock,
            output=tmp_path / "tampered-profile-bundle",
        )


def test_visium_bundle_retains_and_revalidates_minimal_warning_evidence(tmp_path):
    commit = "a" * 40
    run_root, evidence, skills_lock = make_visium_bundle_sources(tmp_path, commit)
    bundle = tmp_path / "bundle"
    report = ARTIFACT.build_bundle(
        case="visium-mouse-brain",
        commit=commit,
        run_root=run_root,
        evidence_root=evidence,
        skills_lock=skills_lock,
        output=bundle,
    )
    assert report["evidence_type"] == "tutorial-bundle-verification"
    warning = bundle / "run" / "logs" / "pipeline-warnings.json"
    pipeline = bundle / "run" / "03_scripts" / "run_pipeline.R"
    analysis = bundle / "run" / "03_scripts" / "analysis-params.json"
    assert report["runtime_warning_evidence_sha256"] == hashlib.sha256(warning.read_bytes()).hexdigest()
    assert warning.is_file() and pipeline.is_file() and analysis.is_file()
    assert not list(bundle.rglob("*.rds"))

    payload = json.loads(warning.read_text(encoding="utf-8"))
    payload["blocking_warning_occurrences"] = 1
    write_json(warning, payload)
    manifest_path = bundle / "bundle-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    record = next(item for item in manifest["files"] if item["path"] == "run/logs/pipeline-warnings.json")
    record["size_bytes"] = warning.stat().st_size
    record["sha256"] = hashlib.sha256(warning.read_bytes()).hexdigest()
    manifest["total_size_bytes"] = sum(item["size_bytes"] for item in manifest["files"])
    write_json(manifest_path, manifest)
    with pytest.raises(ARTIFACT.BundleError, match="warning evidence"):
        ARTIFACT.verify_bundle(bundle, "visium-mouse-brain", commit)


def test_workflow_powershell_blocks_parse():
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if executable is None:
        pytest.skip("PowerShell parser is unavailable")
    blocks = re.findall(r"(?m)^ {8}run: \|\r?\n((?:^ {10}.*(?:\r?\n|$))+)", REAL)
    assert blocks
    for block in blocks:
        source = "\n".join(line[10:] for line in block.splitlines())
        command = [
            executable,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "$null=[scriptblock]::Create([Console]::In.ReadToEnd())",
        ]
        parsed = subprocess.run(command, input=source, text=True, capture_output=True, check=False)
        assert parsed.returncode == 0, parsed.stderr
