import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "validate_release_evidence.py"
SPEC = importlib.util.spec_from_file_location("validate_release_evidence", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


COMMIT = "a" * 40


def write_json(path: Path, payload: dict) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rewrite_item(root: Path, item: dict, mutate) -> None:
    path = root / item["locator"]
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutate(payload)
    item["sha256"] = write_json(path, payload)


def evidence_item(root: Path, name: str, payload: dict, *, kind: str = "file"):
    path = root / "validation" / "runtime" / "release-evidence" / name
    digest = write_json(path, payload)
    return {
        "kind": kind,
        "locator": path.relative_to(root).as_posix(),
        "sha256": digest,
    }


def static_gate(root: Path, *, commit: str = COMMIT):
    checks = {key: True for key in MODULE.STATIC_CHECK_KEYS}
    item = evidence_item(
        root,
        "local-static.json",
        {
            "schema_version": "1.0.0",
            "evidence_type": "local-static-validation",
            "gate": "local_static",
            "commit": commit,
            "ok": True,
            "checks": checks,
        },
    )
    return {"status": "passed", "commit": commit, "evidence": [item], "details": checks}


def tutorial_gate(root: Path, gate_name: str, case: str, *, commit: str = COMMIT):
    details = {
        "canonical_summary_sha256": "1" * 64,
        "r_version": "4.5.3",
        "seurat_version": "5.5.0",
        "renv_version": "1.2.2",
        "renv_lock_sha256": "2" * 64,
        "fresh_run_verified": True,
        "resume_verified": True,
        "cache_reuse_verified": True,
        "checksum_failure_rejected": True,
        "nonzero_exit_rejected": True,
    }
    if case == "visium-mouse-brain":
        details["runtime_warning_evidence_sha256"] = "3" * 64
    detail_item = evidence_item(
        root,
        f"{gate_name}-details.json",
        {
            "schema_version": "1.0.0",
            "evidence_type": "tutorial-release-details",
            "ok": True,
            "case": case,
            "commit": commit,
            **details,
        },
    )
    verification = {
        "schema_version": "1.0.0",
        "evidence_type": "tutorial-bundle-verification",
        "ok": True,
        "case": case,
        "commit": commit,
        "manifest_sha256": "4" * 64,
        "file_count": 30,
        "total_size_bytes": 12345,
        "original_final_figure_pairs": 5 if case == "pbmc3k" else 3,
        "canonical_summary_sha256": details["canonical_summary_sha256"],
        "renv_lock_sha256": details["renv_lock_sha256"],
    }
    if case == "visium-mouse-brain":
        verification["runtime_warning_evidence_sha256"] = details[
            "runtime_warning_evidence_sha256"
        ]
    verification_item = evidence_item(
        root,
        f"{gate_name}-bundle-verification.json",
        verification,
    )
    return {
        "status": "passed",
        "commit": commit,
        "evidence": [detail_item, verification_item],
        "details": details,
    }


def native_review_gate(root: Path, *, commit: str = COMMIT):
    pairs = []
    for case, count in MODULE.NATIVE_CASE_PAIR_COUNTS.items():
        for index in range(1, count + 1):
            figure_id = f"figure-{index}"
            pairs.append(
                {
                    "pair_id": f"{case}/{figure_id}",
                    "case": case,
                    "figure_id": figure_id,
                    "original_sha256": f"{index:x}" * 64,
                    "final_sha256": f"{index + count:x}" * 64,
                    "opened_original": True,
                    "opened_final": True,
                    "decision": "keep",
                    "unresolved_blocker_or_major": False,
                }
            )
    item = evidence_item(
        root,
        "native-review-registry.json",
        {
            "schema_version": "1.0.0",
            "evidence_type": "native-review-registry",
            "ok": True,
            "commit": commit,
            "all_original_final_pairs_opened": True,
            "original_final_pair_count": 8,
            "case_pair_counts": MODULE.NATIVE_CASE_PAIR_COUNTS,
            "pairs": pairs,
        },
        kind="native-review",
    )
    return {
        "status": "passed",
        "commit": commit,
        "evidence": [item],
        "details": {
            "review_registry_sha256": item["sha256"],
            "all_original_final_pairs_opened": True,
            "original_final_pair_count": 8,
            "case_pair_counts": MODULE.NATIVE_CASE_PAIR_COUNTS,
        },
    }


def upstream_gate(root: Path, *, commit: str = COMMIT):
    details = {
        "pinned_commit": "c" * 40,
        "content_sha256": "d" * 64,
        "strict_validation": True,
        "real_visium_recipe_executed": True,
        "source_object_warning_safe": True,
        "native_visual_review": True,
        "recipe_validation_evidence_sha256": "e" * 64,
        "source_warning_evidence_sha256": "3" * 64,
    }
    item = evidence_item(
        root,
        "upstream-visualization.json",
        {
            "schema_version": "1.0.0",
            "evidence_type": "upstream-visualization-validation",
            "ok": True,
            "commit": commit,
            **details,
        },
    )
    return {"status": "passed", "commit": commit, "evidence": [item], "details": details}


def clone_gate(root: Path, *, commit: str = COMMIT):
    details = {
        "remote_url": MODULE.EXPECTED_CLONE_REMOTE,
        "anonymous": True,
        "credentials_used": False,
        "head_sha": commit,
        "command_returncodes": {key: 0 for key in MODULE.CLONE_COMMAND_KEYS},
        "canonical_summary_sha256": {
            "pbmc3k": "5" * 64,
            "visium-mouse-brain": "6" * 64,
        },
        "bundle_verification_sha256": {
            "pbmc3k": "7" * 64,
            "visium-mouse-brain": "8" * 64,
        },
        "git_status_porcelain": "",
    }
    item = evidence_item(
        root,
        "anonymous-clone.json",
        {
            "schema_version": "1.0.0",
            "evidence_type": "anonymous-clone-validation",
            "ok": True,
            "commit": commit,
            **details,
        },
        kind="anonymous-clone",
    )
    return {"status": "passed", "commit": commit, "evidence": [item], "details": details}


def distribution_gate(root: Path, *, commit: str = COMMIT):
    report_item = evidence_item(
        root,
        "distribution-report.json",
        {
            "schema_version": "1.0.0",
            "root": "biomedical-analysis-agent",
            "ok": True,
            "files_scanned": 250,
            "findings": [],
        },
    )
    details = {
        "distribution_ok": True,
        "distribution_report_sha256": report_item["sha256"],
    }
    binding_item = evidence_item(
        root,
        "distribution-binding.json",
        {
            "schema_version": "1.0.0",
            "evidence_type": "license-and-leak-scan-binding",
            "ok": True,
            "commit": commit,
            **details,
        },
    )
    return {
        "status": "passed",
        "commit": commit,
        "evidence": [report_item, binding_item],
        "details": details,
    }


def action_gate(root: Path, gate_name: str, run_id: int, *, commit: str = COMMIT):
    workflow = MODULE.EXPECTED_ACTION_WORKFLOWS[gate_name]
    workflow_path = MODULE.EXPECTED_ACTION_PATHS[gate_name]
    event = "push" if gate_name == "github_actions_ci" else "workflow_dispatch"
    url = f"https://github.com/{MODULE.EXPECTED_REPOSITORY}/actions/runs/{run_id}"
    path = root / "validation" / "runtime" / "release-evidence" / f"{gate_name}-api.json"
    digest = write_json(
        path,
        {
            "id": run_id,
            "name": workflow,
            "path": workflow_path,
            "event": event,
            "head_sha": commit,
            "status": "completed",
            "conclusion": "success",
            "html_url": url,
            "repository": {"full_name": MODULE.EXPECTED_REPOSITORY},
        },
    )
    return {
        "status": "passed",
        "commit": commit,
        "evidence": [
            {
                "kind": "github-actions",
                "locator": path.relative_to(root).as_posix(),
                "sha256": digest,
            }
        ],
        "details": {
            "run_url": url,
            "repository": MODULE.EXPECTED_REPOSITORY,
            "workflow": workflow,
            "path": workflow_path,
            "event": event,
            "head_sha": commit,
            "conclusion": "success",
        },
    }


def complete_evidence(root: Path, *, commit: str = COMMIT):
    gates = {
        "local_static": static_gate(root, commit=commit),
        "local_pbmc3k": tutorial_gate(root, "local_pbmc3k", "pbmc3k", commit=commit),
        "local_visium_mouse_brain": tutorial_gate(
            root,
            "local_visium_mouse_brain",
            "visium-mouse-brain",
            commit=commit,
        ),
        "local_native_visual_review": native_review_gate(root, commit=commit),
        "upstream_visualization": upstream_gate(root, commit=commit),
        "github_actions_ci": action_gate(root, "github_actions_ci", 123, commit=commit),
        "github_actions_real_data": action_gate(root, "github_actions_real_data", 456, commit=commit),
        "anonymous_clone": clone_gate(root, commit=commit),
        "license_and_leak_scan": distribution_gate(root, commit=commit),
    }
    return {"schema_version": "1.0.0", "release": {"version": "v1.0.0", "commit": commit}, "gates": gates}


def test_complete_commit_bound_and_file_bound_evidence_passes(tmp_path):
    report = MODULE.validate_evidence(
        complete_evidence(tmp_path),
        expected_version="v1.0.0",
        expected_commit=COMMIT,
        repository_root=tmp_path,
    )
    assert report["ok"], report["findings"]
    assert report["required_gate_count"] == 9


def test_pending_gate_is_a_release_blocker(tmp_path):
    payload = complete_evidence(tmp_path)
    payload["gates"]["anonymous_clone"]["status"] = "pending"
    report = MODULE.validate_evidence(payload, require_passed=True, repository_root=tmp_path)
    assert not report["ok"]
    assert "gate-not-passed" in {item["code"] for item in report["findings"]}


def test_missing_or_hash_mismatched_local_evidence_is_rejected(tmp_path):
    payload = complete_evidence(tmp_path)
    item = payload["gates"]["local_static"]["evidence"][0]
    item["sha256"] = "0" * 64
    report = MODULE.validate_evidence(payload, repository_root=tmp_path)
    assert "evidence-hash-mismatch" in {finding["code"] for finding in report["findings"]}
    item["locator"] = "validation/missing.json"
    report = MODULE.validate_evidence(payload, repository_root=tmp_path)
    assert "evidence-local-locator" in {finding["code"] for finding in report["findings"]}


def test_actions_require_saved_api_response_for_real_repo_workflow_and_commit(tmp_path):
    payload = complete_evidence(tmp_path)
    gate = payload["gates"]["github_actions_ci"]
    snapshot_path = tmp_path / gate["evidence"][0]["locator"]
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    snapshot["head_sha"] = "d" * 40
    gate["evidence"][0]["sha256"] = write_json(snapshot_path, snapshot)
    report = MODULE.validate_evidence(payload, repository_root=tmp_path)
    assert "actions-api-head_sha" in {item["code"] for item in report["findings"]}

    gate["details"]["run_url"] = "https://github.com/example/repo/actions/runs/123"
    report = MODULE.validate_evidence(payload, repository_root=tmp_path)
    assert "actions-url" in {item["code"] for item in report["findings"]}


def test_private_home_locator_is_rejected(tmp_path):
    payload = copy.deepcopy(complete_evidence(tmp_path))
    payload["gates"]["local_static"]["evidence"][0]["locator"] = "C:" + r"\Users\Alice\private\report.json"
    report = MODULE.validate_evidence(payload, repository_root=tmp_path)
    assert "private-locator" in {item["code"] for item in report["findings"]}


def test_tutorial_details_and_bundle_verification_are_exactly_bound(tmp_path):
    payload = complete_evidence(tmp_path)
    gate = payload["gates"]["local_pbmc3k"]
    gate["details"]["canonical_summary_sha256"] = "9" * 64
    report = MODULE.validate_evidence(payload, repository_root=tmp_path)
    assert "tutorial-details-binding" in {item["code"] for item in report["findings"]}

    payload = complete_evidence(tmp_path)
    gate = payload["gates"]["local_visium_mouse_brain"]
    rewrite_item(
        tmp_path,
        gate["evidence"][1],
        lambda item: item.__setitem__("runtime_warning_evidence_sha256", "8" * 64),
    )
    report = MODULE.validate_evidence(payload, repository_root=tmp_path)
    assert "tutorial-evidence-content" in {item["code"] for item in report["findings"]}


def test_native_upstream_static_and_clone_self_reports_cannot_diverge(tmp_path):
    mutations = (
        ("local_static", "checks", "test_suite_passed", False, "static-evidence-content"),
        (
            "local_native_visual_review",
            None,
            "all_original_final_pairs_opened",
            False,
            "native-review-content",
        ),
        ("upstream_visualization", None, "strict_validation", False, "upstream-evidence-content"),
        ("upstream_visualization", None, "source_object_warning_safe", False, "upstream-evidence-content"),
        ("upstream_visualization", None, "source_warning_evidence_sha256", "9" * 64, "upstream-evidence-content"),
        ("anonymous_clone", "command_returncodes", "visium_mouse_brain", 1, "clone-evidence-content"),
    )
    for gate_name, container, key, value, expected_code in mutations:
        case_root = tmp_path / gate_name
        payload = complete_evidence(case_root)
        gate = payload["gates"][gate_name]

        def mutate(item, *, container=container, key=key, value=value):
            target = item[container] if container else item
            target[key] = value

        rewrite_item(case_root, gate["evidence"][0], mutate)
        report = MODULE.validate_evidence(payload, repository_root=case_root)
        codes = {item["code"] for item in report["findings"]}
        assert expected_code in codes


@pytest.mark.parametrize(
    "locator",
    (
        "file:///tmp/release-evidence.json",
        "D:/release-evidence.json",
        r"\\server\share\release-evidence.json",
        "validation/runtime/release-evidence/../escape.json",
        "validation/runtime/not-release-evidence/report.json",
    ),
)
def test_passed_evidence_locator_must_be_inside_release_evidence_root(tmp_path, locator):
    payload = complete_evidence(tmp_path)
    payload["gates"]["local_static"]["evidence"][0]["locator"] = locator
    report = MODULE.validate_evidence(payload, repository_root=tmp_path)
    assert "evidence-local-locator" in {item["code"] for item in report["findings"]}


@pytest.mark.parametrize(
    "mutation",
    (
        lambda item: item["pairs"].__setitem__(1, copy.deepcopy(item["pairs"][0])),
        lambda item: item["pairs"][0].__setitem__("case", "other"),
        lambda item: item["pairs"][0].__setitem__("original_sha256", "A" * 64),
        lambda item: item["pairs"][0].__setitem__("opened_final", False),
        lambda item: item["pairs"][0].__setitem__("decision", "revise"),
        lambda item: item["pairs"][0].__setitem__("unresolved_blocker_or_major", True),
        lambda item: item["pairs"].pop(),
        lambda item: item.__setitem__("case_pair_counts", {"pbmc3k": 4, "visium-mouse-brain": 4}),
        lambda item: item.__setitem__("unexpected", True),
    ),
)
def test_native_review_requires_exact_eight_pair_registry(tmp_path, mutation):
    payload = complete_evidence(tmp_path)
    gate = payload["gates"]["local_native_visual_review"]
    rewrite_item(tmp_path, gate["evidence"][0], mutation)
    report = MODULE.validate_evidence(payload, repository_root=tmp_path)
    assert "native-review-content" in {item["code"] for item in report["findings"]}


@pytest.mark.parametrize(
    ("field", "value", "expected_code"),
    (
        ("path", ".github/workflows/other.yml", "actions-api-path"),
        ("event", "pull_request", "actions-api-event"),
    ),
)
def test_actions_snapshot_is_bound_to_workflow_path_and_event(
    tmp_path, field, value, expected_code
):
    payload = complete_evidence(tmp_path)
    gate = payload["gates"]["github_actions_ci"]
    rewrite_item(
        tmp_path,
        gate["evidence"][0],
        lambda item: item.__setitem__(field, value),
    )
    report = MODULE.validate_evidence(payload, repository_root=tmp_path)
    assert expected_code in {item["code"] for item in report["findings"]}


@pytest.mark.parametrize(
    "mutation",
    (
        lambda item: item.__setitem__("remote_url", "https://example.invalid/repo.git"),
        lambda item: item.__setitem__("anonymous", False),
        lambda item: item.__setitem__("credentials_used", True),
        lambda item: item.__setitem__("head_sha", "b" * 40),
        lambda item: item["command_returncodes"].__setitem__("tests", 1),
        lambda item: item["canonical_summary_sha256"].__setitem__("pbmc3k", "A" * 64),
        lambda item: item["bundle_verification_sha256"].pop("visium-mouse-brain"),
        lambda item: item.__setitem__("git_status_porcelain", "?? generated.txt\n"),
        lambda item: item.__setitem__("unexpected", True),
    ),
)
def test_anonymous_clone_requires_exact_public_clone_schema(tmp_path, mutation):
    payload = complete_evidence(tmp_path)
    gate = payload["gates"]["anonymous_clone"]
    rewrite_item(tmp_path, gate["evidence"][0], mutation)
    report = MODULE.validate_evidence(payload, repository_root=tmp_path)
    assert "clone-evidence-content" in {item["code"] for item in report["findings"]}


def test_distribution_status_and_digest_must_match_report_and_binding(tmp_path):
    payload = complete_evidence(tmp_path)
    gate = payload["gates"]["license_and_leak_scan"]
    gate["details"]["distribution_report_sha256"] = "7" * 64
    report = MODULE.validate_evidence(payload, repository_root=tmp_path)
    assert "distribution-details-binding" in {item["code"] for item in report["findings"]}

    payload = complete_evidence(tmp_path)
    gate = payload["gates"]["license_and_leak_scan"]
    rewrite_item(
        tmp_path,
        gate["evidence"][0],
        lambda item: item.update({"ok": False, "findings": [{"code": "leak"}]}),
    )
    report = MODULE.validate_evidence(payload, repository_root=tmp_path)
    assert "distribution-evidence-content" in {item["code"] for item in report["findings"]}
