import hashlib
import importlib.util
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from test_release_evidence import COMMIT, complete_evidence


ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "scripts" / "build_release_assets.py"
VERIFY = ROOT / "scripts" / "verify_release_assets.py"
sys.path.insert(0, str(ROOT / "scripts"))
BUILD_SPEC = importlib.util.spec_from_file_location("build_release_assets", BUILD)
BUILD_MODULE = importlib.util.module_from_spec(BUILD_SPEC)
assert BUILD_SPEC.loader is not None
BUILD_SPEC.loader.exec_module(BUILD_MODULE)


def run(*command, cwd=None):
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)


def write(path: Path, text: str = "fixture\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def initialize_distribution(root: Path):
    required = {
        ".gitattributes": "* text=auto eol=lf\n",
        ".gitignore": "release-assets/\nvalidation/runtime/\n.cache/\n",
        "CHANGELOG.md": "# Changelog\n\n## [1.0.0]\n",
        "LICENSE": "MIT License\n",
        "NOTICE": (
            "The MIT grant applies only to original code and documentation. "
            "Third-party data are under CC BY 4.0 and are not part of this "
            "repository or its MIT license.\n"
        ),
        "README.md": "# fixture\n",
        "RELEASE_NOTES_v1.0.0.md": "# fixture v1.0.0\n",
        "SKILL.md": "# fixture\n",
        "THIRD_PARTY_DATA.md": (
            "No raw third-party data is committed.\n\n"
            "PBMC3K — Attribution: Copyright 10x Genomics; CC BY 4.0.\n\n"
            "Mouse Brain Sagittal-Anterior — Attribution: Copyright 10x Genomics; "
            "CC BY 4.0.\n"
        ),
        "VALIDATION.md": "# Validation procedure template\n\nNo release claim is stored in Git.\n",
        "bootstrap_skills.py": "print('fixture')\n",
        "tutorial_cli.py": "print('fixture')\n",
        ".github/workflows/ci.yml": "name: CI\n",
        ".github/workflows/anonymous-clone-release-gate.yml": "name: Anonymous clone gate\n",
        ".github/workflows/real-data-release-gate.yml": "name: Gate\n",
        ".github/scripts/run_anonymous_case.py": "print('fixture')\n",
        "scripts/build_release_assets.py": "print('fixture')\n",
        "scripts/ci_r_smoke.R": "invisible(TRUE)\n",
        "scripts/validate_release_evidence.py": "print('fixture')\n",
        "scripts/verify_release_assets.py": "print('fixture')\n",
        "scripts/verify_release_evidence_bundle.py": "print('fixture')\n",
        "validation/release-evidence.template.json": "{}\n",
    }
    for skill in (
        "bulk-rnaseq",
        "multi-omics-pipeline",
        "quantitative-proteomics-workflow",
        "scrnaseq-pipeline",
        "spatial-pipeline",
    ):
        required[f"skills/{skill}/README.md"] = f"# {skill}\n"
        required[f"skills/{skill}/SKILL.md"] = f"# {skill} skill\n"
    for case in ("pbmc3k", "visium-mouse-brain"):
        required[f"examples/{case}/README.md"] = f"# {case}\n"
        required[f"examples/{case}/PROMPT.md"] = f"# {case} prompt\n"
        required[f"examples/{case}/expected-output/README.md"] = (
            f"# {case} expected output\n"
        )
        required[
            f"examples/{case}/expected-output/manifest/verification-summary.json"
        ] = '{"schema_version":"1.0.0","status":"pass"}\n'
    for relative, content in required.items():
        write(root / relative, content)
    lock = {
        "schema_version": "1.0.0",
        "dependencies": {
            "visualization-2026718-v1": {
                "repository": "https://github.com/anthonyvmppg3yigdt33kp-lang/visualization-2026718-v1.git",
                "commit": "c" * 40,
                "subdirectory": "skill/visualization-2026718-v1",
                "distribution_profile_file": "public-install-profile.json",
                "excluded_paths": [
                    "assets/previews-curated",
                    "assets/scheme-candidates",
                    "assets/source_archive",
                    "references/catalog.jsonl",
                ],
                "content_sha256": "d" * 64,
                "original_code_license": "MIT",
                "license_file": "LICENSE",
                "third_party_notice_file": "NOTICE.md",
                "rights_status": "mixed-original-and-third-party-not-relicensed",
            }
        },
    }
    write(root / "skills.lock.json", json.dumps(lock, indent=2) + "\n")
    write(root / "scripts" / "example.py", "print('release fixture')\n")


def git_commit(root: Path) -> str:
    assert run("git", "init", "--initial-branch=main", str(root)).returncode == 0
    assert run("git", "-C", str(root), "config", "user.name", "Release Test").returncode == 0
    assert run("git", "-C", str(root), "config", "user.email", "release-test@example.invalid").returncode == 0
    assert run("git", "-C", str(root), "add", ".").returncode == 0
    committed = run("git", "-C", str(root), "commit", "-m", "fixture")
    assert committed.returncode == 0, committed.stderr
    return run("git", "-C", str(root), "rev-parse", "HEAD").stdout.strip()


def test_release_archive_is_deterministic_and_independently_verified(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    initialize_distribution(source)
    commit = git_commit(source)
    evidence = complete_evidence(source, commit=commit)
    evidence_path = tmp_path / "release-evidence.json"
    write(evidence_path, json.dumps(evidence, indent=2) + "\n")
    ignored = source / ".cache" / "must-not-ship.exe"
    ignored.parent.mkdir(parents=True)
    ignored.write_bytes(b"ignored executable")

    outputs = []
    for name in ("assets-one", "assets-two"):
        output = tmp_path / name
        completed = run(
            sys.executable,
            str(BUILD),
            "--root",
            str(source),
            "--version",
            "v1.0.0",
            "--expected-commit",
            commit,
            "--evidence",
            str(evidence_path),
            "--output-dir",
            str(output),
        )
        assert completed.returncode == 0, completed.stderr
        outputs.append(output)

    first = outputs[0] / "biomedical-analysis-agent-1.0.0.zip"
    second = outputs[1] / "biomedical-analysis-agent-1.0.0.zip"
    first_evidence = outputs[0] / "biomedical-analysis-agent-1.0.0-evidence.zip"
    second_evidence = outputs[1] / "biomedical-analysis-agent-1.0.0-evidence.zip"
    assert hashlib.sha256(first.read_bytes()).hexdigest() == hashlib.sha256(second.read_bytes()).hexdigest()
    assert hashlib.sha256(first_evidence.read_bytes()).hexdigest() == hashlib.sha256(
        second_evidence.read_bytes()
    ).hexdigest()
    with zipfile.ZipFile(first) as bundle:
        assert not any("must-not-ship.exe" in name for name in bundle.namelist())

    verified = run(
        sys.executable,
        str(VERIFY),
        "--archive",
        str(first),
        "--evidence-archive",
        str(first_evidence),
        "--checksums",
        str(outputs[0] / "SHA256SUMS.txt"),
        "--summary",
        str(outputs[0] / "release-validation-summary.json"),
        "--release-validation",
        str(outputs[0] / "RELEASE_VALIDATION.md"),
        "--version",
        "v1.0.0",
        "--commit",
        commit,
    )
    assert verified.returncode == 0, verified.stderr
    assert json.loads(verified.stdout)["ok"] is True


def test_verifier_rejects_variable_zip_timestamp_even_when_hashes_are_rebound(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    initialize_distribution(source)
    commit = git_commit(source)
    evidence_path = tmp_path / "release-evidence.json"
    write(evidence_path, json.dumps(complete_evidence(source, commit=commit), indent=2) + "\n")
    output = tmp_path / "assets"
    built = run(
        sys.executable,
        str(BUILD),
        "--root",
        str(source),
        "--version",
        "v1.0.0",
        "--expected-commit",
        commit,
        "--evidence",
        str(evidence_path),
        "--output-dir",
        str(output),
    )
    assert built.returncode == 0, built.stderr

    archive = output / "biomedical-analysis-agent-1.0.0.zip"
    evidence_archive = output / "biomedical-analysis-agent-1.0.0-evidence.zip"
    with zipfile.ZipFile(archive, "r") as bundle:
        members = [(info, bundle.read(info)) for info in bundle.infolist()]
    rewritten = archive.with_suffix(".zip.tmp")
    with zipfile.ZipFile(rewritten, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        for original, content in members:
            info = zipfile.ZipInfo(original.filename, date_time=(2026, 7, 22, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = original.create_system
            info.external_attr = original.external_attr
            bundle.writestr(info, content, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    rewritten.replace(archive)

    summary_path = output / "release-validation-summary.json"
    validation_path = output / "RELEASE_VALIDATION.md"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    old_archive_sha = summary["archive"]["sha256"]
    new_archive_sha = hashlib.sha256(archive.read_bytes()).hexdigest()
    summary["archive"]["sha256"] = new_archive_sha
    summary["archive"]["compressed_size_bytes"] = archive.stat().st_size
    validation_path.write_text(
        validation_path.read_text(encoding="utf-8").replace(old_archive_sha, new_archive_sha),
        encoding="utf-8",
        newline="\n",
    )
    summary["release_validation"]["sha256"] = hashlib.sha256(
        validation_path.read_bytes()
    ).hexdigest()
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    checksums_path = output / "SHA256SUMS.txt"
    checksums_path.write_text(
        "".join(
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
            for path in (archive, evidence_archive, summary_path, validation_path)
        ),
        encoding="utf-8",
        newline="\n",
    )

    verified = run(
        sys.executable,
        str(VERIFY),
        "--archive",
        str(archive),
        "--evidence-archive",
        str(evidence_archive),
        "--checksums",
        str(checksums_path),
        "--summary",
        str(summary_path),
        "--release-validation",
        str(validation_path),
        "--version",
        "v1.0.0",
        "--commit",
        commit,
    )
    assert verified.returncode == 2
    assert "non-deterministic ZIP timestamp" in verified.stderr


def test_packaging_refuses_pending_release_evidence(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    initialize_distribution(source)
    commit = git_commit(source)
    evidence = complete_evidence(source, commit=commit)
    evidence["gates"]["anonymous_clone"]["status"] = "pending"
    evidence_path = tmp_path / "pending.json"
    write(evidence_path, json.dumps(evidence, indent=2) + "\n")
    completed = run(
        sys.executable,
        str(BUILD),
        "--root",
        str(source),
        "--version",
        "v1.0.0",
        "--expected-commit",
        commit,
        "--evidence",
        str(evidence_path),
        "--output-dir",
        str(tmp_path / "assets"),
    )
    assert completed.returncode == 2
    assert "release evidence is incomplete" in completed.stderr


def test_packaging_refuses_draft_notes_and_pending_validation_template(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    initialize_distribution(source)
    write(source / "RELEASE_NOTES_v1.0.0.md", "# v1.0.0\n\nDraft only.\n")
    completed = run(sys.executable, str(BUILD), "--root", str(source), "--version", "v1.0.0", "--dry-run")
    assert completed.returncode == 2
    assert "draft marker" in completed.stderr

    write(source / "RELEASE_NOTES_v1.0.0.md", "# fixture v1.0.0\n")
    write(source / "VALIDATION.md", "# Validation\n\nPENDING\n")
    completed = run(sys.executable, str(BUILD), "--root", str(source), "--version", "v1.0.0", "--dry-run")
    assert completed.returncode == 2
    assert "contains PENDING" in completed.stderr


def test_packaging_refuses_mutable_release_status_and_pre_tag_date(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    initialize_distribution(source)
    write(source / "README.md", "# fixture\n\nv1.0.0 is not released.\n")
    completed = run(
        sys.executable,
        str(BUILD),
        "--root",
        str(source),
        "--version",
        "v1.0.0",
        "--dry-run",
    )
    assert completed.returncode == 2
    assert "mutable publication-status assertion" in completed.stderr

    write(source / "README.md", "# fixture\n\nRelease state is tag-bound.\n")
    write(source / "CHANGELOG.md", "# Changelog\n\n## [1.0.0] - 2026-07-22\n")
    completed = run(
        sys.executable,
        str(BUILD),
        "--root",
        str(source),
        "--version",
        "v1.0.0",
        "--dry-run",
    )
    assert completed.returncode == 2
    assert "hard-codes a publication date" in completed.stderr


def test_untracked_file_blocks_packaging_instead_of_being_archived(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    initialize_distribution(source)
    commit = git_commit(source)
    evidence = complete_evidence(source, commit=commit)
    evidence_path = tmp_path / "evidence.json"
    write(evidence_path, json.dumps(evidence) + "\n")
    write(source / "untracked-secret.txt", "do not ship\n")
    completed = run(
        sys.executable,
        str(BUILD),
        "--root",
        str(source),
        "--version",
        "v1.0.0",
        "--expected-commit",
        commit,
        "--evidence",
        str(evidence_path),
        "--output-dir",
        str(tmp_path / "assets"),
    )
    assert completed.returncode == 2
    assert "worktree is not clean" in completed.stderr


def test_git_index_symlink_and_oversize_regular_file_are_rejected(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    assert run("git", "init", "--initial-branch=main", str(source)).returncode == 0
    target = source / "target.txt"
    write(target, "target\n")
    object_id = run("git", "-C", str(source), "hash-object", "-w", str(target)).stdout.strip()
    assert object_id
    indexed = run(
        "git",
        "-C",
        str(source),
        "update-index",
        "--add",
        "--cacheinfo",
        f"120000,{object_id},linked",
    )
    assert indexed.returncode == 0, indexed.stderr
    try:
        BUILD_MODULE.release_files(source.resolve(), tmp_path / "out")
    except BUILD_MODULE.ReleaseAssetError as exc:
        assert "tracked symlink" in str(exc)
    else:
        raise AssertionError("tracked symlink was accepted")

    assert run("git", "-C", str(source), "update-index", "--force-remove", "linked").returncode == 0
    big = source / "large.bin"
    with big.open("wb") as handle:
        handle.truncate(BUILD_MODULE.MAX_RELEASE_FILE_BYTES + 1)
    assert run("git", "-C", str(source), "add", "large.bin").returncode == 0
    try:
        BUILD_MODULE.release_files(source.resolve(), tmp_path / "out")
    except BUILD_MODULE.ReleaseAssetError as exc:
        assert "exceeds" in str(exc)
    else:
        raise AssertionError("oversize tracked file was accepted")

    assert run("git", "-C", str(source), "update-index", "--force-remove", "large.bin").returncode == 0
    dangerous = source / "payload.exe"
    write(dangerous, "not an executable\n")
    assert run("git", "-C", str(source), "add", "payload.exe").returncode == 0
    try:
        BUILD_MODULE.release_files(source.resolve(), tmp_path / "out")
    except BUILD_MODULE.ReleaseAssetError as exc:
        assert "dangerous tracked extension" in str(exc)
    else:
        raise AssertionError("dangerous tracked extension was accepted")


def test_evidence_bundle_is_self_contained_and_has_no_local_locators(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    initialize_distribution(source)
    commit = git_commit(source)
    evidence_path = tmp_path / "release-evidence.json"
    write(evidence_path, json.dumps(complete_evidence(source, commit=commit), indent=2) + "\n")
    output = tmp_path / "assets"
    built = run(
        sys.executable,
        str(BUILD),
        "--root",
        str(source),
        "--version",
        "v1.0.0",
        "--expected-commit",
        commit,
        "--evidence",
        str(evidence_path),
        "--output-dir",
        str(output),
    )
    assert built.returncode == 0, built.stderr
    evidence_archive = output / "biomedical-analysis-agent-1.0.0-evidence.zip"
    prefix = "biomedical-analysis-agent-1.0.0-evidence/"
    verifier = tmp_path / "verify_release_evidence_bundle.py"
    with zipfile.ZipFile(evidence_archive) as bundle:
        verifier.write_bytes(bundle.read(prefix + "verify_release_evidence_bundle.py"))
        rewritten = json.loads(bundle.read(prefix + "release-evidence.json"))
        locators = {
            item["locator"]
            for gate in rewritten["gates"].values()
            for item in gate["evidence"]
        }
        assert locators
        assert all(locator.startswith("evidence/") for locator in locators)
        assert {
            name.removeprefix(prefix)
            for name in bundle.namelist()
            if name.startswith(prefix + "evidence/")
        } == locators
    independent = run(
        sys.executable,
        str(verifier),
        str(evidence_archive),
        "--version",
        "v1.0.0",
        "--commit",
        commit,
    )
    assert independent.returncode == 0, independent.stderr
    assert json.loads(independent.stdout)["ok"] is True

    for asset_name in (
        "RELEASE_VALIDATION.md",
        "release-validation-summary.json",
        "SHA256SUMS.txt",
    ):
        text = (output / asset_name).read_text(encoding="utf-8")
        assert "validation/runtime/release-evidence" not in text
        assert "file:" + "//" not in text
        assert str(tmp_path) not in text
    validation_text = (output / "RELEASE_VALIDATION.md").read_text(encoding="utf-8")
    assert (
        "https://github.com/anthonyvmppg3yigdt33kp-lang/"
        "biomedical-analysis-agent/actions/runs/123"
    ) in validation_text
    summary = json.loads((output / "release-validation-summary.json").read_text(encoding="utf-8"))
    assert summary["evidence"]["archive"]["sha256"] == hashlib.sha256(
        evidence_archive.read_bytes()
    ).hexdigest()
    assert summary["evidence"]["gates"]["local_native_visual_review"]["details"][
        "original_final_pair_count"
    ] == 8


def test_standalone_evidence_verifier_rejects_tampering(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    initialize_distribution(source)
    commit = git_commit(source)
    evidence_path = tmp_path / "release-evidence.json"
    write(evidence_path, json.dumps(complete_evidence(source, commit=commit), indent=2) + "\n")
    output = tmp_path / "assets"
    built = run(
        sys.executable,
        str(BUILD),
        "--root",
        str(source),
        "--version",
        "v1.0.0",
        "--expected-commit",
        commit,
        "--evidence",
        str(evidence_path),
        "--output-dir",
        str(output),
    )
    assert built.returncode == 0, built.stderr
    archive = output / "biomedical-analysis-agent-1.0.0-evidence.zip"
    with zipfile.ZipFile(archive, "r") as bundle:
        members = [(info, bundle.read(info)) for info in bundle.infolist()]
    verifier_member = next(
        content for info, content in members if info.filename.endswith("/verify_release_evidence_bundle.py")
    )
    verifier = tmp_path / "standalone.py"
    verifier.write_bytes(verifier_member)
    rewritten = archive.with_suffix(".zip.tmp")
    with zipfile.ZipFile(rewritten, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        for original, content in members:
            if original.filename.endswith("/evidence/local-static.json"):
                payload = json.loads(content)
                payload["tampered"] = True
                content = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
            info = zipfile.ZipInfo(original.filename, date_time=original.date_time)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = original.create_system
            info.external_attr = original.external_attr
            bundle.writestr(info, content, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    rewritten.replace(archive)
    rejected = run(
        sys.executable,
        str(verifier),
        str(archive),
        "--version",
        "v1.0.0",
        "--commit",
        commit,
    )
    assert rejected.returncode == 2
    assert "member mismatch" in rejected.stderr


@pytest.mark.parametrize(
    "leaked_locator",
    (
        "C:" + r"\Users\Alice\private\evidence.json",
        "file:" + "///tmp/private-evidence.json",
    ),
)
def test_evidence_bundle_refuses_sensitive_path_leaks(tmp_path, leaked_locator):
    source = tmp_path / "source"
    source.mkdir()
    initialize_distribution(source)
    commit = git_commit(source)
    evidence = complete_evidence(source, commit=commit)
    item = evidence["gates"]["local_static"]["evidence"][0]
    evidence_file = source / item["locator"]
    payload = json.loads(evidence_file.read_text(encoding="utf-8"))
    payload["diagnostic_locator"] = leaked_locator
    write(evidence_file, json.dumps(payload, sort_keys=True) + "\n")
    item["sha256"] = hashlib.sha256(evidence_file.read_bytes()).hexdigest()
    evidence_path = tmp_path / "release-evidence.json"
    write(evidence_path, json.dumps(evidence, indent=2) + "\n")
    rejected = run(
        sys.executable,
        str(BUILD),
        "--root",
        str(source),
        "--version",
        "v1.0.0",
        "--expected-commit",
        commit,
        "--evidence",
        str(evidence_path),
        "--output-dir",
        str(tmp_path / "assets"),
    )
    assert rejected.returncode == 2
    assert "sensitive absolute or file URI locator" in rejected.stderr


@pytest.mark.parametrize(
    "relative",
    (
        ".cache/forced.txt",
        ".renv/forced.txt",
        ".task-skills/forced.txt",
        "runs/forced.txt",
        "validation/runtime/forced.txt",
        "work/forced.txt",
        "release-assets/forced.txt",
        "references/corpus-sources.json",
        "assets/private-corpus-index/forced.txt",
    ),
)
def test_forced_tracked_ignored_runtime_and_private_paths_are_rejected(tmp_path, relative):
    source = tmp_path / relative.replace("/", "-").replace(".", "dot")
    source.mkdir()
    assert run("git", "init", "--initial-branch=main", str(source)).returncode == 0
    path = source / relative
    write(path, "must not ship\n")
    added = run("git", "-C", str(source), "add", "-f", relative)
    assert added.returncode == 0, added.stderr
    with pytest.raises(BUILD_MODULE.ReleaseAssetError, match="forbidden from release assets"):
        BUILD_MODULE.release_files(source.resolve(), tmp_path / "out")
