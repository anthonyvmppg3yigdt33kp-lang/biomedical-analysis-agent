import importlib.util
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "tutorial_cli.py"
SPEC = importlib.util.spec_from_file_location("tutorial_cli", CLI)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def test_supported_cases_are_explicit_and_stable():
    assert set(MODULE.CASES) == {"pbmc3k", "visium-mouse-brain"}
    assert MODULE.EXPECTED_R_VERSION == "4.5.3"
    assert MODULE.EXPECTED_SEURAT_VERSION == "5.5.0"


def test_rscript_version_pattern_accepts_windows_and_legacy_formats():
    for rendered in (
        "Rscript (R) version 4.5.3 (2026-03-11)",
        "R scripting front-end version 4.5.3 (2026-03-11)",
    ):
        match = MODULE.RSCRIPT_VERSION_PATTERN.search(rendered)
        assert match is not None
        assert match.group(1) == "4.5.3"


def test_root_compiled_plan_is_hash_bound_and_resume_refuses_tampering(tmp_path):
    run_root = tmp_path / "run"
    binding = MODULE._bind_case_plan("pbmc3k", run_root, resume=False)
    bound = run_root / binding["path"]
    assert bound.is_file()
    assert binding["plan_id"] == json.loads(bound.read_text(encoding="utf-8"))["plan_id"]
    assert MODULE._bind_case_plan("pbmc3k", run_root, resume=True) == binding
    bound.write_text("{}\n", encoding="utf-8")
    try:
        MODULE._bind_case_plan("pbmc3k", run_root, resume=True)
    except MODULE.TutorialError as exc:
        assert "differs" in str(exc)
    else:
        raise AssertionError("tampered bound plan was accepted")


def test_run_without_authorization_is_rejected_before_driver_execution(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            str(CLI),
            "run",
            "--case",
            "pbmc3k",
            "--run-root",
            str(tmp_path / "run"),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "--authorize-run" in result.stderr
    assert not (tmp_path / "run").exists()


def test_plan_cli_emits_valid_frozen_json_once_examples_exist():
    request = ROOT / "examples" / "pbmc3k" / "request.json"
    if not request.is_file():
        return
    result = subprocess.run(
        [sys.executable, str(CLI), "plan", "--case", "pbmc3k"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["frozen"] is True
    assert payload["routes"][0]["capability"] == "single-cell"


def test_plan_output_bytes_exactly_match_the_bound_execution_plan(tmp_path):
    output = tmp_path / "compiled-plan.json"
    result = subprocess.run(
        [
            sys.executable,
            str(CLI),
            "plan",
            "--case",
            "pbmc3k",
            "--output",
            str(output),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    run_root = tmp_path / "run"
    MODULE._bind_case_plan("pbmc3k", run_root, resume=False)
    assert output.read_bytes() == (
        run_root / "01_plan" / "root-compiled-plan.json"
    ).read_bytes()
