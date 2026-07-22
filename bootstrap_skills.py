#!/usr/bin/env python3
"""Install this repository's skills into an explicit task-local directory."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parent
LOCK_PATH = ROOT / "skills.lock.json"
EXPECTED_VISUALIZATION_REPOSITORY = (
    "https://github.com/anthonyvmppg3yigdt33kp-lang/visualization-2026718-v1.git"
)
EXPECTED_VISUALIZATION_SUBDIRECTORY = "skill/visualization-2026718-v1"
EXPECTED_VISUALIZATION_DISTRIBUTION_PROFILE = "public-install-profile.json"
EXPECTED_VISUALIZATION_PROFILE_ID = "biomedical-public-runtime-v1"
EXPECTED_VISUALIZATION_EXCLUDED_PATHS = (
    "assets/previews-curated",
    "assets/scheme-candidates",
    "assets/source_archive",
    "references/catalog.jsonl",
)
EXPECTED_VISUALIZATION_CAPABILITY_SCOPE = (
    "formal_recipe_adaptation",
    "formal_recipe_composition",
    "formal_recipe_preflight",
    "formal_recipe_rendering",
    "native_visual_review",
)
EXPECTED_VISUALIZATION_OVERLAY_FILES = {
    "SKILL.public-runtime.md": "SKILL.md",
    "manifest.public-runtime.yaml": "manifest.yaml",
}
EXPECTED_VISUALIZATION_RIGHTS_STATUS = (
    "mixed-original-and-third-party-not-relicensed"
)
BUNDLED_SKILLS = (
    "scrnaseq-pipeline",
    "spatial-pipeline",
    "bulk-rnaseq",
    "quantitative-proteomics-workflow",
    "multi-omics-pipeline",
)
COMPANION_LEGAL_ENTRIES = ("LICENSE", "NOTICE")
MAIN_ENTRIES = (
    "SKILL.md",
    "README.md",
    "LICENSE",
    "NOTICE",
    "THIRD_PARTY_DATA.md",
    "agents",
    "assets",
    "examples",
    "references",
    "scripts",
    "tutorial_cli.py",
)
IGNORED_PARTS = {
    ".git",
    ".cache",
    ".pytest_cache",
    ".renv",
    ".task-skills",
    "__pycache__",
    "runs",
}
MAIN_SKILL_EXCLUDED_PATHS = {
    PurePosixPath("assets/private-corpus-index"),
    PurePosixPath("references/corpus-sources.json"),
}


class BootstrapError(RuntimeError):
    """Raised when an installation would be unsafe or non-reproducible."""


def iter_tree_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().casefold()):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part in IGNORED_PARTS for part in relative.parts):
            continue
        if path.suffix.casefold() in {".pyc", ".pyo"}:
            continue
        yield path


def tree_sha256(root: Path) -> str:
    root = root.resolve()
    if not root.is_dir():
        raise BootstrapError(f"Tree does not exist: {root}")
    digest = hashlib.sha256()
    for path in iter_tree_files(root):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        file_digest = hashlib.sha256(path.read_bytes()).digest()
        digest.update(file_digest)
    return digest.hexdigest()


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def validate_destination(destination: Path) -> Path:
    destination = destination.expanduser().resolve()
    home = Path.home().resolve()
    forbidden = (
        home / ".codex" / "skills",
        home / ".agents" / "skills",
    )
    if any(_is_relative_to(destination, path.resolve()) for path in forbidden):
        raise BootstrapError(
            "Refusing a global skill directory; choose an explicit task-local destination"
        )
    if destination == ROOT or _is_relative_to(ROOT, destination):
        raise BootstrapError("Destination cannot contain the source repository")
    return destination


def _copy_entry(
    source: Path,
    destination: Path,
    *,
    excluded_paths: set[PurePosixPath] | None = None,
    exclusion_root: Path | None = None,
) -> None:
    if source.is_dir():
        excluded_paths = excluded_paths or set()

        def ignore(directory: str, names: list[str]) -> set[str]:
            ignored = {
                name
                for name in names
                if name in IGNORED_PARTS
                or Path(name).suffix.casefold() in {".pyc", ".pyo"}
            }
            directory_path = Path(directory).resolve()
            exclusion_root_resolved = (exclusion_root or ROOT).resolve()
            for name in names:
                candidate = directory_path / name
                try:
                    relative = PurePosixPath(
                        candidate.relative_to(exclusion_root_resolved).as_posix()
                    )
                except ValueError:
                    continue
                if relative in excluded_paths:
                    ignored.add(name)
            return ignored

        shutil.copytree(
            source,
            destination,
            ignore=ignore,
        )
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def _stage_main_skill(stage: Path) -> Path:
    target = stage / "biomedical-analysis-agent"
    target.mkdir(parents=True)
    for entry in MAIN_ENTRIES:
        source = ROOT / entry
        if not source.exists():
            raise BootstrapError(f"Main skill entry is missing: {entry}")
        _copy_entry(
            source,
            target / entry,
            excluded_paths=MAIN_SKILL_EXCLUDED_PATHS,
        )
    return target


def _stage_bundled_skill(stage: Path, name: str) -> Path:
    if name not in BUNDLED_SKILLS:
        raise BootstrapError(f"Unknown bundled skill: {name}")
    source = ROOT / "skills" / name
    if not source.is_dir():
        raise BootstrapError(f"Bundled skill is missing: {name}")
    target = stage / name
    _copy_entry(source, target)
    for entry in COMPANION_LEGAL_ENTRIES:
        legal_source = ROOT / entry
        if not legal_source.is_file():
            raise BootstrapError(f"Companion legal file is missing: {entry}")
        _copy_entry(legal_source, target / entry)
    return target


def _install_tree(source: Path, target: Path, expected_sha256: str | None = None) -> dict[str, str]:
    source_hash = tree_sha256(source)
    if expected_sha256 is not None and source_hash != expected_sha256:
        raise BootstrapError(
            f"Source content hash mismatch for {source.name}: {source_hash} != {expected_sha256}"
        )
    if target.exists():
        installed_hash = tree_sha256(target)
        if installed_hash != source_hash:
            raise BootstrapError(
                f"Existing destination differs for {target.name}; use a fresh task-local destination"
            )
        return {"status": "verified-existing", "sha256": installed_hash}
    _copy_entry(source, target)
    installed_hash = tree_sha256(target)
    if installed_hash != source_hash:
        raise BootstrapError(f"Post-copy hash mismatch for {target.name}")
    return {"status": "installed", "sha256": installed_hash}


def _load_lock() -> dict[str, Any]:
    try:
        lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
        dependency = lock["dependencies"]["visualization-2026718-v1"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise BootstrapError(f"Invalid skills.lock.json: {exc}") from exc
    if lock.get("schema_version") != "1.0.0":
        raise BootstrapError("Pinned visualization lock must use schema_version 1.0.0")
    if set(lock.get("dependencies", {})) != {"visualization-2026718-v1"}:
        raise BootstrapError("skills.lock.json must contain exactly the visualization dependency")
    if dependency.get("repository") != EXPECTED_VISUALIZATION_REPOSITORY:
        raise BootstrapError("Pinned visualization repository is not the reviewed HTTPS remote")
    if re.fullmatch(r"[0-9a-f]{40}", str(dependency.get("commit", ""))) is None:
        raise BootstrapError("Pinned visualization commit must be a full lowercase Git SHA")
    if re.fullmatch(r"[0-9a-f]{64}", str(dependency.get("content_sha256", ""))) is None:
        raise BootstrapError("Pinned visualization content hash must be lowercase SHA-256")
    if (
        _validated_subdirectory(dependency.get("subdirectory"))
        != EXPECTED_VISUALIZATION_SUBDIRECTORY
    ):
        raise BootstrapError("Pinned visualization subdirectory is not the reviewed skill path")
    if (
        dependency.get("distribution_profile_file")
        != EXPECTED_VISUALIZATION_DISTRIBUTION_PROFILE
    ):
        raise BootstrapError(
            "Pinned visualization dependency must declare public-install-profile.json"
        )
    if dependency.get("excluded_paths") != list(EXPECTED_VISUALIZATION_EXCLUDED_PATHS):
        raise BootstrapError(
            "Pinned visualization dependency must declare the exact public exclusions"
        )
    if "license" in dependency or "notice_file" in dependency:
        raise BootstrapError(
            "Pinned visualization dependency must not use ambiguous legacy rights fields"
        )
    if dependency.get("original_code_license") != "MIT":
        raise BootstrapError(
            "Pinned visualization dependency must declare MIT for original code only"
        )
    if dependency.get("license_file") != "LICENSE":
        raise BootstrapError("Pinned visualization dependency must declare LICENSE")
    if dependency.get("third_party_notice_file") != "NOTICE.md":
        raise BootstrapError(
            "Pinned visualization dependency must declare third-party NOTICE.md"
        )
    if (
        dependency.get("rights_status") != EXPECTED_VISUALIZATION_RIGHTS_STATUS
    ):
        raise BootstrapError(
            "Pinned visualization dependency must declare the reviewed mixed rights status"
        )
    return lock


def _validated_subdirectory(value: object) -> str:
    subdirectory = str(value or ".").replace("\\", "/")
    parsed = PurePosixPath(subdirectory)
    if parsed.is_absolute() or ".." in parsed.parts or not parsed.parts:
        raise BootstrapError("Pinned visualization subdirectory is invalid")
    return parsed.as_posix()


def _validate_visualization_legal_files(
    target: Path, dependency: dict[str, Any]
) -> None:
    for key, expected, label in (
        ("license_file", "LICENSE", "license"),
        ("third_party_notice_file", "NOTICE.md", "third-party notice"),
    ):
        if dependency.get(key) != expected:
            raise BootstrapError(f"Pinned visualization {label} declaration is invalid")
        path = target / expected
        if path.is_symlink() or not path.is_file():
            raise BootstrapError(f"Pinned visualization {label} file is missing: {expected}")


def _load_visualization_distribution_profile(
    target: Path, dependency: dict[str, Any]
) -> dict[str, Any]:
    profile_name = dependency.get("distribution_profile_file")
    if profile_name != EXPECTED_VISUALIZATION_DISTRIBUTION_PROFILE:
        raise BootstrapError("Pinned visualization distribution profile declaration is invalid")
    profile_path = target / EXPECTED_VISUALIZATION_DISTRIBUTION_PROFILE
    if profile_path.is_symlink() or not profile_path.is_file():
        raise BootstrapError(
            "Pinned visualization distribution profile is missing: "
            f"{EXPECTED_VISUALIZATION_DISTRIBUTION_PROFILE}"
        )
    try:
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BootstrapError(f"Invalid visualization distribution profile: {exc}") from exc
    expected_keys = {
        "schema_version",
        "profile_id",
        "purpose",
        "capability_scope",
        "overlay_files",
        "included_rights_boundary",
        "excluded_paths",
        "exclusion_reasons",
        "raw_third_party_data_included",
        "raw_extracted_source_code_included",
    }
    if not isinstance(profile, dict) or set(profile) != expected_keys:
        raise BootstrapError("Visualization distribution profile schema is invalid")
    if profile.get("schema_version") != "1.0.0":
        raise BootstrapError("Visualization distribution profile schema_version is invalid")
    if profile.get("profile_id") != EXPECTED_VISUALIZATION_PROFILE_ID:
        raise BootstrapError("Visualization distribution profile_id is invalid")
    if not isinstance(profile.get("purpose"), str) or not profile["purpose"].strip():
        raise BootstrapError("Visualization distribution profile purpose is invalid")
    if profile.get("capability_scope") != list(
        EXPECTED_VISUALIZATION_CAPABILITY_SCOPE
    ):
        raise BootstrapError("Visualization distribution profile capability scope is invalid")
    if profile.get("overlay_files") != EXPECTED_VISUALIZATION_OVERLAY_FILES:
        raise BootstrapError("Visualization distribution profile overlays are invalid")
    for relative in EXPECTED_VISUALIZATION_OVERLAY_FILES:
        overlay = target / relative
        if overlay.is_symlink() or not overlay.is_file():
            raise BootstrapError(
                f"Visualization distribution profile overlay is missing: {relative}"
            )
    if profile.get("included_rights_boundary") != {
        "original_code_license": dependency.get("original_code_license"),
        "third_party_rights_status": dependency.get("rights_status"),
        "notice_file": dependency.get("third_party_notice_file"),
    }:
        raise BootstrapError("Visualization distribution profile rights boundary is invalid")
    if profile.get("excluded_paths") != list(EXPECTED_VISUALIZATION_EXCLUDED_PATHS):
        raise BootstrapError("Visualization distribution profile exclusions are invalid")
    reasons = profile.get("exclusion_reasons")
    if (
        not isinstance(reasons, dict)
        or set(reasons) != set(EXPECTED_VISUALIZATION_EXCLUDED_PATHS)
        or any(not isinstance(value, str) or not value.strip() for value in reasons.values())
    ):
        raise BootstrapError("Visualization distribution profile exclusion reasons are invalid")
    if profile.get("raw_third_party_data_included") is not False:
        raise BootstrapError(
            "Visualization distribution profile must exclude raw third-party data"
        )
    if profile.get("raw_extracted_source_code_included") is not False:
        raise BootstrapError(
            "Visualization distribution profile must exclude raw extracted source code"
        )
    return profile


def _validate_visualization_install_tree(
    target: Path, dependency: dict[str, Any]
) -> dict[str, Any]:
    _validate_visualization_legal_files(target, dependency)
    profile = _load_visualization_distribution_profile(target, dependency)
    for relative in profile["excluded_paths"]:
        path = target.joinpath(*PurePosixPath(relative).parts)
        if path.exists() or path.is_symlink():
            raise BootstrapError(
                f"Pinned visualization public exclusion is present: {relative}"
            )
    for overlay_source, installed_target in profile["overlay_files"].items():
        source_path = target / overlay_source
        target_path = target / installed_target
        if target_path.is_symlink() or not target_path.is_file():
            raise BootstrapError(
                f"Pinned visualization overlay target is missing: {installed_target}"
            )
        if source_path.read_bytes() != target_path.read_bytes():
            raise BootstrapError(
                f"Pinned visualization overlay target differs: {installed_target}"
            )
    manifest = (target / "manifest.yaml").read_text(encoding="utf-8").replace("\\", "/")
    for relative in profile["excluded_paths"]:
        if relative in manifest:
            raise BootstrapError(
                f"Pinned visualization runtime manifest references exclusion: {relative}"
            )
    return profile


def _stage_visualization_install(
    source: Path, stage: Path, dependency: dict[str, Any]
) -> Path:
    target = stage / "visualization-2026718-v1"
    profile = _load_visualization_distribution_profile(source, dependency)
    excluded = {PurePosixPath(path) for path in profile["excluded_paths"]}
    _copy_entry(
        source,
        target,
        excluded_paths=excluded,
        exclusion_root=source,
    )
    for overlay_source, installed_target in profile["overlay_files"].items():
        _copy_entry(target / overlay_source, target / installed_target)
    _validate_visualization_install_tree(target, dependency)
    return target


def _visualization_sparse_patterns(
    subdirectory: str, excluded_paths: list[str]
) -> str:
    patterns = [f"/{subdirectory}/"]
    patterns.extend(
        f"!/{subdirectory}/{relative}"
        + ("" if PurePosixPath(relative).suffix else "/")
        for relative in excluded_paths
    )
    return "\n".join(patterns) + "\n"


def _clone_visualization(dependency: dict[str, Any], destination: Path) -> dict[str, str]:
    target = destination / "visualization-2026718-v1"
    expected = str(dependency["content_sha256"])
    if target.exists():
        observed = tree_sha256(target)
        if observed != expected:
            raise BootstrapError(
                f"Existing visualization content hash mismatch: {observed} != {expected}"
            )
        _validate_visualization_install_tree(target, dependency)
        return {"status": "verified-existing", "sha256": observed}

    repository = str(dependency["repository"])
    commit = str(dependency["commit"])
    subdirectory = _validated_subdirectory(dependency.get("subdirectory", "."))
    with tempfile.TemporaryDirectory(prefix="skill-bootstrap-", dir=destination) as temporary:
        checkout = Path(temporary) / "checkout"
        commands = (
            ["git", "clone", "--filter=blob:none", "--no-checkout", repository, str(checkout)],
            ["git", "-C", str(checkout), "config", "core.longpaths", "true"],
            ["git", "-C", str(checkout), "sparse-checkout", "init", "--no-cone"],
        )
        for command in commands:
            result = subprocess.run(command, capture_output=True, text=True, check=False)
            if result.returncode != 0:
                raise BootstrapError(
                    f"Git bootstrap failed ({result.returncode}): {result.stderr.strip()}"
                )
        sparse = subprocess.run(
            [
                "git",
                "-C",
                str(checkout),
                "sparse-checkout",
                "set",
                "--no-cone",
                "--stdin",
            ],
            input=_visualization_sparse_patterns(
                subdirectory, dependency["excluded_paths"]
            ),
            capture_output=True,
            text=True,
            check=False,
        )
        if sparse.returncode != 0:
            raise BootstrapError(
                f"Git sparse checkout failed ({sparse.returncode}): {sparse.stderr.strip()}"
            )
        checkout_result = subprocess.run(
            ["git", "-C", str(checkout), "checkout", "--detach", commit],
            capture_output=True,
            text=True,
            check=False,
        )
        if checkout_result.returncode != 0:
            raise BootstrapError(
                "Git bootstrap failed "
                f"({checkout_result.returncode}): {checkout_result.stderr.strip()}"
            )
        observed_commit = subprocess.run(
            ["git", "-C", str(checkout), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        if observed_commit.returncode != 0 or observed_commit.stdout.strip() != commit:
            raise BootstrapError("Pinned visualization commit was not checked out exactly")
        source = (checkout / subdirectory).resolve()
        if not _is_relative_to(source, checkout.resolve()) or not source.is_dir():
            raise BootstrapError("Pinned visualization subdirectory is invalid")
        _validate_visualization_legal_files(source, dependency)
        profile = _load_visualization_distribution_profile(source, dependency)
        for relative in profile["excluded_paths"]:
            materialized = source.joinpath(*PurePosixPath(relative).parts)
            if materialized.exists() or materialized.is_symlink():
                raise BootstrapError(
                    f"Sparse checkout materialized public exclusion: {relative}"
                )
        staged = _stage_visualization_install(
            source, Path(temporary) / "stage", dependency
        )
        result = _install_tree(staged, target, expected)
    _validate_visualization_install_tree(target, dependency)
    return result


def bootstrap(destination: Path, verify_only: bool = False) -> dict[str, Any]:
    destination = validate_destination(destination)
    lock = _load_lock()
    if verify_only and not destination.is_dir():
        raise BootstrapError(f"Destination does not exist: {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "schema_version": "1.0.0",
        "destination": str(destination),
        "global_skills_modified": False,
        "skills": {},
    }

    if verify_only:
        with tempfile.TemporaryDirectory(prefix="main-skill-verify-") as temporary:
            source = _stage_main_skill(Path(temporary))
            expected = tree_sha256(source)
            target = destination / "biomedical-analysis-agent"
            if not target.is_dir() or tree_sha256(target) != expected:
                raise BootstrapError("Installed biomedical-analysis-agent differs from the repository")
            report["skills"]["biomedical-analysis-agent"] = {
                "status": "verified-existing",
                "sha256": expected,
            }
        with tempfile.TemporaryDirectory(prefix="bundled-skills-verify-") as temporary:
            stage = Path(temporary)
            for name in BUNDLED_SKILLS:
                source = _stage_bundled_skill(stage, name)
                expected = tree_sha256(source)
                target = destination / name
                if not target.is_dir() or tree_sha256(target) != expected:
                    raise BootstrapError(f"Installed bundled skill differs: {name}")
                report["skills"][name] = {
                    "status": "verified-existing",
                    "sha256": expected,
                }
        dependency = lock["dependencies"]["visualization-2026718-v1"]
        target = destination / "visualization-2026718-v1"
        observed = tree_sha256(target)
        if observed != dependency["content_sha256"]:
            raise BootstrapError("Installed visualization skill differs from skills.lock.json")
        _validate_visualization_install_tree(target, dependency)
        report["skills"]["visualization-2026718-v1"] = {
            "status": "verified-existing",
            "sha256": observed,
        }
    else:
        with tempfile.TemporaryDirectory(prefix="main-skill-stage-") as temporary:
            source = _stage_main_skill(Path(temporary))
            report["skills"]["biomedical-analysis-agent"] = _install_tree(
                source, destination / "biomedical-analysis-agent"
            )
        with tempfile.TemporaryDirectory(prefix="bundled-skills-stage-") as temporary:
            stage = Path(temporary)
            for name in BUNDLED_SKILLS:
                report["skills"][name] = _install_tree(
                    _stage_bundled_skill(stage, name), destination / name
                )
        report["skills"]["visualization-2026718-v1"] = _clone_visualization(
            lock["dependencies"]["visualization-2026718-v1"], destination
        )
    dependency = lock["dependencies"]["visualization-2026718-v1"]
    profile = _validate_visualization_install_tree(
        destination / "visualization-2026718-v1", dependency
    )
    report["visualization_distribution_profile"] = profile
    report["visualization_excluded_paths_absent"] = list(profile["excluded_paths"])
    report["visualization_overlay_targets_verified"] = dict(profile["overlay_files"])
    report["visualization_runtime_manifest_exclusions_absent"] = list(
        profile["excluded_paths"]
    )
    report["ok"] = True
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, default=ROOT / ".task-skills")
    parser.add_argument("--verify-only", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = bootstrap(args.destination, verify_only=args.verify_only)
    except (BootstrapError, OSError, subprocess.SubprocessError) as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        return 2
    sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
