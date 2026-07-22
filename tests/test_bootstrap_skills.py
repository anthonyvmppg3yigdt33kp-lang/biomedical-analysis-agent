import importlib.util
import json
import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bootstrap_skills.py"
SPEC = importlib.util.spec_from_file_location("bootstrap_skills", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def visualization_profile():
    return {
        "schema_version": "1.0.0",
        "profile_id": MODULE.EXPECTED_VISUALIZATION_PROFILE_ID,
        "purpose": "Public task-local runtime fixture.",
        "capability_scope": list(MODULE.EXPECTED_VISUALIZATION_CAPABILITY_SCOPE),
        "overlay_files": MODULE.EXPECTED_VISUALIZATION_OVERLAY_FILES,
        "included_rights_boundary": {
            "original_code_license": "MIT",
            "third_party_rights_status": MODULE.EXPECTED_VISUALIZATION_RIGHTS_STATUS,
            "notice_file": "NOTICE.md",
        },
        "excluded_paths": list(MODULE.EXPECTED_VISUALIZATION_EXCLUDED_PATHS),
        "exclusion_reasons": {
            path: "Excluded from the public runtime fixture."
            for path in MODULE.EXPECTED_VISUALIZATION_EXCLUDED_PATHS
        },
        "raw_third_party_data_included": False,
        "raw_extracted_source_code_included": False,
    }


def write_visualization_profile(root):
    (root / MODULE.EXPECTED_VISUALIZATION_DISTRIBUTION_PROFILE).write_text(
        json.dumps(visualization_profile()), encoding="utf-8"
    )
    overlay_contents = {
        "SKILL.public-runtime.md": "# Public runtime skill\n",
        "manifest.public-runtime.yaml": "profile: biomedical-public-runtime-v1\n",
    }
    for overlay_source, installed_target in (
        MODULE.EXPECTED_VISUALIZATION_OVERLAY_FILES.items()
    ):
        content = overlay_contents[overlay_source]
        (root / overlay_source).write_text(content, encoding="utf-8")
        (root / installed_target).write_text(content, encoding="utf-8")


def test_tree_hash_is_path_and_content_stable(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "a.txt").write_text("alpha\n", encoding="utf-8")
    (second / "a.txt").write_text("alpha\n", encoding="utf-8")
    assert MODULE.tree_sha256(first) == MODULE.tree_sha256(second)
    (second / "a.txt").write_text("beta\n", encoding="utf-8")
    assert MODULE.tree_sha256(first) != MODULE.tree_sha256(second)


def test_global_skill_destinations_are_rejected():
    destination = Path.home() / ".codex" / "skills" / "test"
    with pytest.raises(MODULE.BootstrapError, match="global skill"):
        MODULE.validate_destination(destination)


def test_source_repository_cannot_be_used_as_destination():
    with pytest.raises(MODULE.BootstrapError, match="source repository"):
        MODULE.validate_destination(ROOT)


def test_visualization_subdirectory_must_remain_inside_checkout():
    assert MODULE._validated_subdirectory(
        r"skill\visualization-2026718-v1"
    ) == "skill/visualization-2026718-v1"
    for value in ("../private", "/absolute", r"..\private"):
        with pytest.raises(MODULE.BootstrapError, match="subdirectory"):
            MODULE._validated_subdirectory(value)


def test_main_skill_staging_excludes_private_corpus_but_keeps_example(
    tmp_path, monkeypatch
):
    source = tmp_path / "repository"
    source.mkdir()
    for entry in MODULE.MAIN_ENTRIES:
        path = source / entry
        if Path(entry).suffix:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"fixture for {entry}\n", encoding="utf-8")
        else:
            path.mkdir(parents=True, exist_ok=True)
    private_index = source / "assets" / "private-corpus-index"
    private_index.mkdir(parents=True)
    (private_index / "private.json").write_text("private\n", encoding="utf-8")
    references = source / "references"
    (references / "corpus-sources.json").write_text("private\n", encoding="utf-8")
    (references / "corpus-sources.example.json").write_text(
        "public template\n", encoding="utf-8"
    )

    monkeypatch.setattr(MODULE, "ROOT", source)
    staged = MODULE._stage_main_skill(tmp_path / "stage")

    assert not (staged / "assets" / "private-corpus-index").exists()
    assert not (staged / "references" / "corpus-sources.json").exists()
    assert (
        staged / "references" / "corpus-sources.example.json"
    ).read_text(encoding="utf-8") == "public template\n"


def test_lock_loader_requires_the_exact_reviewed_dependency(tmp_path, monkeypatch):
    lock = {
        "schema_version": "1.0.0",
        "dependencies": {
            "visualization-2026718-v1": {
                "repository": MODULE.EXPECTED_VISUALIZATION_REPOSITORY,
                "commit": "a" * 40,
                "subdirectory": MODULE.EXPECTED_VISUALIZATION_SUBDIRECTORY,
                "distribution_profile_file": (
                    MODULE.EXPECTED_VISUALIZATION_DISTRIBUTION_PROFILE
                ),
                "excluded_paths": list(MODULE.EXPECTED_VISUALIZATION_EXCLUDED_PATHS),
                "content_sha256": "b" * 64,
                "original_code_license": "MIT",
                "license_file": "LICENSE",
                "third_party_notice_file": "NOTICE.md",
                "rights_status": "mixed-original-and-third-party-not-relicensed",
            }
        },
    }
    path = tmp_path / "skills.lock.json"
    monkeypatch.setattr(MODULE, "LOCK_PATH", path)

    path.write_text(json.dumps(lock), encoding="utf-8")
    assert MODULE._load_lock() == lock

    for key, value, message in (
        ("repository", "https://example.org/unreviewed.git", "reviewed HTTPS remote"),
        ("commit", "short", "full lowercase Git SHA"),
        ("subdirectory", "skill/another-skill", "reviewed skill path"),
        ("distribution_profile_file", "profile.json", "public-install-profile.json"),
        ("excluded_paths", list(reversed(MODULE.EXPECTED_VISUALIZATION_EXCLUDED_PATHS)), "exact public exclusions"),
        ("content_sha256", "short", "lowercase SHA-256"),
        ("original_code_license", "BSD-3-Clause", "original code"),
        ("license_file", "COPYING", "declare LICENSE"),
        ("third_party_notice_file", "NOTICE", "NOTICE.md"),
        ("rights_status", "MIT-only", "mixed rights status"),
    ):
        invalid = json.loads(json.dumps(lock))
        invalid["dependencies"]["visualization-2026718-v1"][key] = value
        path.write_text(json.dumps(invalid), encoding="utf-8")
        with pytest.raises(MODULE.BootstrapError, match=message):
            MODULE._load_lock()

    invalid = json.loads(json.dumps(lock))
    invalid["dependencies"]["visualization-2026718-v1"]["license"] = "MIT"
    path.write_text(json.dumps(invalid), encoding="utf-8")
    with pytest.raises(MODULE.BootstrapError, match="ambiguous legacy rights fields"):
        MODULE._load_lock()


def test_companion_skill_names_match_routes_and_agent_prompts():
    expected = {
        "scrnaseq-pipeline": "bio-workflows-scrnaseq-pipeline",
        "spatial-pipeline": "bio-workflows-spatial-pipeline",
        "bulk-rnaseq": "bulk-rnaseq",
        "quantitative-proteomics-workflow": "quantitative-proteomics-workflow",
        "multi-omics-pipeline": "multi-omics-pipeline",
    }
    assert MODULE.BUNDLED_SKILLS == tuple(expected)
    root_skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    for directory, public_name in expected.items():
        skill_root = ROOT / "skills" / directory
        skill_text = (skill_root / "SKILL.md").read_text(encoding="utf-8")
        readme_text = (skill_root / "README.md").read_text(encoding="utf-8")
        agent_text = (skill_root / "agents" / "openai.yaml").read_text(encoding="utf-8")
        declared = re.search(r"(?m)^name:\s*(\S+)\s*$", skill_text)
        assert declared is not None and declared.group(1) == public_name
        assert readme_text.startswith(f"# {directory} skill\n")
        assert f"${public_name}" in agent_text
        assert f"`{public_name}`" in root_skill


def test_bootstrap_dispatches_main_five_companions_and_locked_visualization(
    tmp_path, monkeypatch
):
    def fake_clone(dependency, destination):
        assert dependency == MODULE._load_lock()["dependencies"]["visualization-2026718-v1"]
        target = destination / "visualization-2026718-v1"
        target.mkdir()
        (target / "SKILL.md").write_text(
            "---\nname: visualization-2026718-v1\n---\n", encoding="utf-8"
        )
        (target / "LICENSE").write_text("MIT License\n", encoding="utf-8")
        (target / "NOTICE.md").write_text("# Notices\n", encoding="utf-8")
        write_visualization_profile(target)
        return {"status": "installed", "sha256": "fixture"}

    monkeypatch.setattr(MODULE, "_clone_visualization", fake_clone)
    destination = tmp_path / "task-skills"
    report = MODULE.bootstrap(destination)
    expected = {
        "biomedical-analysis-agent",
        *MODULE.BUNDLED_SKILLS,
        "visualization-2026718-v1",
    }
    assert report["ok"] is True
    assert report["global_skills_modified"] is False
    assert report["visualization_distribution_profile"] == visualization_profile()
    assert report["visualization_excluded_paths_absent"] == list(
        MODULE.EXPECTED_VISUALIZATION_EXCLUDED_PATHS
    )
    assert report["visualization_overlay_targets_verified"] == (
        MODULE.EXPECTED_VISUALIZATION_OVERLAY_FILES
    )
    assert set(report["skills"]) == expected
    assert {path.name for path in destination.iterdir()} == expected
    assert (destination / "biomedical-analysis-agent" / "SKILL.md").is_file()
    for name in MODULE.BUNDLED_SKILLS:
        assert (destination / name / "SKILL.md").is_file()
        for legal_name in MODULE.COMPANION_LEGAL_ENTRIES:
            assert (destination / name / legal_name).read_bytes() == (
                ROOT / legal_name
            ).read_bytes()

    visualization = destination / "visualization-2026718-v1"
    verified_lock = MODULE._load_lock()
    verified_lock["dependencies"]["visualization-2026718-v1"][
        "content_sha256"
    ] = MODULE.tree_sha256(visualization)
    monkeypatch.setattr(MODULE, "_load_lock", lambda: verified_lock)
    assert MODULE.bootstrap(destination, verify_only=True)["ok"] is True

    companion_notice = destination / MODULE.BUNDLED_SKILLS[0] / "NOTICE"
    companion_notice.unlink()
    with pytest.raises(MODULE.BootstrapError, match="Installed bundled skill differs"):
        MODULE.bootstrap(destination, verify_only=True)
    companion_notice.write_bytes((ROOT / "NOTICE").read_bytes())

    (visualization / "NOTICE.md").unlink()
    verified_lock["dependencies"]["visualization-2026718-v1"][
        "content_sha256"
    ] = MODULE.tree_sha256(visualization)
    with pytest.raises(MODULE.BootstrapError, match="third-party notice file is missing"):
        MODULE.bootstrap(destination, verify_only=True)


def test_visualization_license_and_notice_files_are_both_required(tmp_path):
    dependency = {
        "license_file": "LICENSE",
        "third_party_notice_file": "NOTICE.md",
    }
    (tmp_path / "LICENSE").write_text("MIT License\n", encoding="utf-8")
    (tmp_path / "NOTICE.md").write_text("# Notices\n", encoding="utf-8")
    MODULE._validate_visualization_legal_files(tmp_path, dependency)
    (tmp_path / "NOTICE.md").unlink()
    with pytest.raises(MODULE.BootstrapError, match="third-party notice file is missing"):
        MODULE._validate_visualization_legal_files(tmp_path, dependency)


def test_visualization_public_profile_staging_excludes_audit_only_assets(tmp_path):
    dependency = MODULE._load_lock()["dependencies"]["visualization-2026718-v1"]
    source = tmp_path / "upstream-skill"
    (source / "assets" / "previews-rendered").mkdir(parents=True)
    (source / "references").mkdir()
    (source / "fixtures").mkdir()
    (source / "recipes").mkdir()
    (source / "LICENSE").write_text("MIT License\n", encoding="utf-8")
    (source / "NOTICE.md").write_text("# Notices\n", encoding="utf-8")
    write_visualization_profile(source)
    (source / "SKILL.md").write_text("# Audit checkout skill\n", encoding="utf-8")
    (source / "manifest.yaml").write_text(
        "audit_catalog: references/catalog.jsonl\n", encoding="utf-8"
    )
    for relative in MODULE.EXPECTED_VISUALIZATION_EXCLUDED_PATHS:
        excluded = source.joinpath(*Path(relative).parts)
        if excluded.suffix:
            excluded.write_text("audit-only\n", encoding="utf-8")
        else:
            excluded.mkdir(parents=True)
            (excluded / "excluded.txt").write_text("audit-only\n", encoding="utf-8")
    for relative in (
        "assets/previews-rendered/retained.txt",
        "references/retained.txt",
        "fixtures/retained.txt",
        "recipes/retained.txt",
    ):
        source.joinpath(*Path(relative).parts).write_text("retained\n", encoding="utf-8")

    staged = MODULE._stage_visualization_install(source, tmp_path / "stage", dependency)

    assert (staged / MODULE.EXPECTED_VISUALIZATION_DISTRIBUTION_PROFILE).is_file()
    for overlay_source, installed_target in (
        MODULE.EXPECTED_VISUALIZATION_OVERLAY_FILES.items()
    ):
        assert (staged / overlay_source).is_file()
        assert (staged / installed_target).read_bytes() == (
            staged / overlay_source
        ).read_bytes()
    for relative in MODULE.EXPECTED_VISUALIZATION_EXCLUDED_PATHS:
        assert not staged.joinpath(*Path(relative).parts).exists()
    for relative in (
        "assets/previews-rendered/retained.txt",
        "references/retained.txt",
        "fixtures/retained.txt",
        "recipes/retained.txt",
    ):
        assert staged.joinpath(*Path(relative).parts).is_file()
    MODULE._validate_visualization_install_tree(staged, dependency)
    for manifest_name in ("manifest.public-runtime.yaml", "manifest.yaml"):
        (staged / manifest_name).write_text(
            "audit_catalog: references/catalog.jsonl\n", encoding="utf-8"
        )
    with pytest.raises(MODULE.BootstrapError, match="manifest references exclusion"):
        MODULE._validate_visualization_install_tree(staged, dependency)
    assert MODULE._visualization_sparse_patterns(
        MODULE.EXPECTED_VISUALIZATION_SUBDIRECTORY,
        list(MODULE.EXPECTED_VISUALIZATION_EXCLUDED_PATHS),
    ) == (
        "/skill/visualization-2026718-v1/\n"
        "!/skill/visualization-2026718-v1/assets/previews-curated/\n"
        "!/skill/visualization-2026718-v1/assets/scheme-candidates/\n"
        "!/skill/visualization-2026718-v1/assets/source_archive/\n"
        "!/skill/visualization-2026718-v1/references/catalog.jsonl\n"
    )
