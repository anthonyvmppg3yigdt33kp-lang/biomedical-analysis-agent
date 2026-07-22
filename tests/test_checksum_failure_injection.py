import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "inject_tutorial_checksum_failure.py"
SPEC = importlib.util.spec_from_file_location("inject_tutorial_checksum_failure", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def write_json(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_pbmc_checksum_negative_control_reaches_dedicated_hash_gate(tmp_path):
    run_root = tmp_path / "run"
    cache_root = tmp_path / "cache"
    run_root.mkdir()
    cache_root.mkdir()
    archive = cache_root / "pbmc3k_filtered_gene_bc_matrices.tar.gz"
    # Match the official byte length so this cannot fail at the earlier size gate.
    with archive.open("wb") as handle:
        handle.truncate(7_621_991)
    report = MODULE.inject("pbmc3k", run_root, cache_root)
    assert report["ok"] is True
    assert report["observed_returncode"] == 2
    assert report["failure_code"] == "INPUT_CHECKSUM_MISMATCH_REJECTED"


def test_visium_checksum_negative_control_reaches_byte_hash_gate(tmp_path):
    run_root = tmp_path / "run"
    cache_root = tmp_path / "cache"
    input_root = cache_root / "inputs" / "visium-mouse-brain"
    input_root.mkdir(parents=True)
    source = input_root / "fixture.h5"
    source.write_bytes(b"real-input-bytes")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    source_record = {
        "file_id": "filtered_h5",
        "filename": source.name,
        "url": "https://example.invalid/fixture.h5",
        "expected_size_bytes": source.stat().st_size,
        "expected_sha256": digest,
        "freeze_policy": "exact_required",
    }
    write_json(
        run_root / "00_request" / "input-manifest.json",
        {"dataset_id": "fixture", "files": [source_record], "required_extracted_assets": []},
    )
    write_json(
        run_root / "00_request" / "resolved-inputs.json",
        {
            "dataset_id": "fixture",
            "freeze_policy": "exact_required_manifest",
            "files": [
                {
                    "file_id": "filtered_h5",
                    "filename": source.name,
                    "url": source_record["url"],
                    "size_bytes": source.stat().st_size,
                    "sha256": digest,
                }
            ],
            "extracted_files": [],
        },
    )
    report = MODULE.inject("visium-mouse-brain", run_root, cache_root)
    assert report["observed_returncode"] == 2
    assert report["failure_code"] == "INPUT_CHECKSUM_MISMATCH_REJECTED"


def test_arbitrary_nonzero_exit_is_not_accepted_as_checksum_evidence(tmp_path, monkeypatch):
    run_root = tmp_path / "run"
    cache_root = tmp_path / "cache"
    run_root.mkdir()
    cache_root.mkdir()
    archive = cache_root / "pbmc3k_filtered_gene_bc_matrices.tar.gz"
    with archive.open("wb") as handle:
        handle.truncate(7_621_991)
    monkeypatch.setattr(
        MODULE.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=2, stderr="INPUT_PREPARATION_FAILED: unrelated", stdout=""),
    )
    with pytest.raises(MODULE.InjectionError, match="other than the dedicated"):
        MODULE.inject("pbmc3k", run_root, cache_root)
