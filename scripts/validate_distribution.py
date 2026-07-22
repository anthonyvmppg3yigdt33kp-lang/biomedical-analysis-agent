#!/usr/bin/env python3
"""Validate the public repository boundary before commit, clone, or release."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_VISUALIZATION_REPOSITORY = (
    "https://github.com/anthonyvmppg3yigdt33kp-lang/visualization-2026718-v1.git"
)
EXPECTED_VISUALIZATION_SUBDIRECTORY = "skill/visualization-2026718-v1"
EXCLUDED_PARTS = {
    ".git",
    ".cache",
    ".pytest_cache",
    ".renv",
    ".task-skills",
    "__pycache__",
    "runs",
}
TEXT_SUFFIXES = {
    ".cfg",
    ".csv",
    ".ini",
    ".json",
    ".jsonl",
    ".md",
    ".ps1",
    ".py",
    ".r",
    ".rmd",
    ".sh",
    ".toml",
    ".tsv",
    ".txt",
    ".yaml",
    ".yml",
}
FORBIDDEN_BINARY_SUFFIXES = {
    ".dll",
    ".dylib",
    ".exe",
    ".gz",
    ".h5",
    ".h5ad",
    ".rdata",
    ".rds",
    ".so",
    ".tif",
    ".tiff",
    ".zip",
}
MAX_PUBLIC_FILE_BYTES = 16 * 1024 * 1024
# Match concrete user-profile locators, not the regex literals used by the
# repository's own leakage guards.  A real profile segment starts with an
# alphanumeric character; constructs such as ``/home/[^...]`` therefore do not
# make the scanner flag itself.
PRIVATE_HOME = re.compile(
    r"(?i)(?:"
    r"[A-Z]:[\\/]+Users[\\/]+[A-Za-z0-9][A-Za-z0-9._ -]{0,127}(?=[\\/]|[\s\"']|$)"
    r"|/home/[A-Za-z0-9][A-Za-z0-9._-]{0,127}(?=/|[\s\"']|$)"
    r")"
)
# High-confidence credential formats only. Generic words such as ``password``
# are intentionally not matched because source and tests legitimately discuss
# secret handling without containing a credential.
SECRET_TOKEN = re.compile(
    r"(?i)(?:"
    r"github_pat_[A-Za-z0-9_]{20,}"
    r"|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,}"
    r"|sk_live_[A-Za-z0-9]{16,}"
    r")"
)
AUTHORIZATION_CREDENTIAL = re.compile(
    r"(?i)\bauthorization\s*[:=]\s*(?:bearer|token|basic)\s+"
    r"[A-Za-z0-9._~+/-]{12,}={0,2}"
)
PRIVATE_KEY_MATERIAL = re.compile(r"-----BEGIN (?:(?:RSA|EC|OPENSSH) )?PRIVATE KEY-----")
# Expected-output payloads are clone-portable evidence, so even a non-private
# absolute locator is forbidden there. The negative lookbehind prevents a URL
# such as ``https://`` from being mistaken for a Windows drive locator.
EXPECTED_OUTPUT_ABSOLUTE = re.compile(
    r"(?i)(?:"
    r"(?<![a-z0-9])[a-z]:[\\/]"
    r"|(?<![\\])\\\\[^\\/\s]+[\\/]"
    r"|/(?:home|Users|tmp)/"
    r"|file://"
    r")"
)
REQUIRED_ROOT_FILES = {
    ".gitattributes",
    ".github/workflows/ci.yml",
    ".github/workflows/real-data-release-gate.yml",
    ".gitignore",
    "CHANGELOG.md",
    "LICENSE",
    "NOTICE",
    "README.md",
    "RELEASE_NOTES_v1.0.0.md",
    "SKILL.md",
    "THIRD_PARTY_DATA.md",
    "VALIDATION.md",
    "bootstrap_skills.py",
    "scripts/build_release_assets.py",
    "scripts/ci_r_smoke.R",
    "scripts/validate_release_evidence.py",
    "scripts/verify_release_assets.py",
    "scripts/verify_release_evidence_bundle.py",
    "skills.lock.json",
    "tutorial_cli.py",
    "validation/release-evidence.template.json",
}
COMPANION_SKILLS = (
    "bulk-rnaseq",
    "multi-omics-pipeline",
    "quantitative-proteomics-workflow",
    "scrnaseq-pipeline",
    "spatial-pipeline",
)
for _skill_name in COMPANION_SKILLS:
    REQUIRED_ROOT_FILES.update(
        {
            f"skills/{_skill_name}/README.md",
            f"skills/{_skill_name}/SKILL.md",
        }
    )
REQUIRED_ROOT_FILES.update(
    {
        "examples/pbmc3k/README.md",
        "examples/pbmc3k/PROMPT.md",
        "examples/pbmc3k/expected-output/README.md",
        "examples/pbmc3k/expected-output/manifest/verification-summary.json",
        "examples/visium-mouse-brain/README.md",
        "examples/visium-mouse-brain/PROMPT.md",
        "examples/visium-mouse-brain/expected-output/README.md",
        "examples/visium-mouse-brain/expected-output/manifest/verification-summary.json",
    }
)


def iter_public_entries(root: Path) -> Iterable[Path]:
    """Yield public-boundary entries without descending into excluded trees."""

    for current_raw, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current = Path(current_raw)
        kept_directories: list[str] = []
        for dirname in sorted(dirnames, key=str.casefold):
            path = current / dirname
            relative = path.relative_to(root)
            if dirname in EXCLUDED_PARTS:
                continue
            relative_posix = relative.as_posix()
            if relative_posix == "validation/runtime" or relative_posix.startswith(
                "validation/runtime/"
            ):
                continue
            if path.is_symlink():
                yield path
                continue
            kept_directories.append(dirname)
        dirnames[:] = kept_directories

        for filename in sorted(filenames, key=str.casefold):
            path = current / filename
            relative = path.relative_to(root)
            if relative.as_posix().startswith("validation/runtime/"):
                continue
            yield path


def iter_public_files(root: Path) -> Iterable[Path]:
    for path in iter_public_entries(root):
        # Never follow a public symlink while scanning contents. Symlinks are
        # reported separately as blockers, including dangling ones.
        if not path.is_symlink() and path.is_file():
            yield path


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _normalized_text(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").casefold().split())


def validate(root: Path) -> dict[str, Any]:
    root = root.resolve()
    findings: list[dict[str, str]] = []
    entries = list(iter_public_entries(root))
    for path in entries:
        relative_path = path.relative_to(root)
        # Check entries before ``is_file`` filtering so a dangling symlink is
        # not silently omitted from the public-boundary report.
        if path.is_symlink():
            findings.append(
                {
                    "severity": "blocker",
                    "code": "symlink",
                    "path": relative_path.as_posix(),
                }
            )
    files = [path for path in entries if not path.is_symlink() and path.is_file()]
    relative_files = {path.relative_to(root).as_posix() for path in files}

    for required in sorted(REQUIRED_ROOT_FILES):
        if required not in relative_files:
            findings.append({"severity": "blocker", "code": "missing-root-file", "path": required})

    for path in files:
        relative_path = path.relative_to(root)
        relative = relative_path.as_posix()
        if "private-corpus-index" in path.parts:
            findings.append({"severity": "blocker", "code": "private-corpus", "path": relative})
        if path.stat().st_size > MAX_PUBLIC_FILE_BYTES:
            findings.append({"severity": "blocker", "code": "oversized-public-file", "path": relative})
        if path.suffix.casefold() in FORBIDDEN_BINARY_SUFFIXES:
            findings.append({"severity": "blocker", "code": "binary-data", "path": relative})
        if path.suffix.casefold() in TEXT_SUFFIXES or path.name in {"LICENSE", "NOTICE"}:
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                findings.append({"severity": "blocker", "code": "non-utf8-text", "path": relative})
                continue
            match = PRIVATE_HOME.search(text)
            if match:
                findings.append(
                    {
                        "severity": "blocker",
                        "code": "private-home-locator",
                        "path": relative,
                    }
                )
            if SECRET_TOKEN.search(text):
                findings.append(
                    {"severity": "blocker", "code": "credential-token", "path": relative}
                )
            if AUTHORIZATION_CREDENTIAL.search(text):
                findings.append(
                    {"severity": "blocker", "code": "authorization-credential", "path": relative}
                )
            if PRIVATE_KEY_MATERIAL.search(text):
                findings.append(
                    {"severity": "blocker", "code": "private-key-material", "path": relative}
                )
            if "expected-output" in relative_path.parts and EXPECTED_OUTPUT_ABSOLUTE.search(text):
                findings.append(
                    {"severity": "blocker", "code": "expected-output-absolute-path", "path": relative}
                )

    license_path = root / "LICENSE"
    if license_path.is_file() and not _normalized_text(license_path).startswith("mit license"):
        findings.append(
            {"severity": "blocker", "code": "invalid-original-code-license", "path": "LICENSE"}
        )

    notice_path = root / "NOTICE"
    if notice_path.is_file():
        notice = _normalized_text(notice_path)
        notice_requirements = (
            "mit grant",
            "applies only",
            "original code",
            "third-party",
            "cc by 4.0",
            "not part of this repository or its mit license",
        )
        if any(requirement not in notice for requirement in notice_requirements):
            findings.append(
                {"severity": "blocker", "code": "ambiguous-license-boundary", "path": "NOTICE"}
            )

    third_party_data = root / "THIRD_PARTY_DATA.md"
    if third_party_data.is_file():
        data_notice = _normalized_text(third_party_data)
        attribution_ok = (
            "pbmc3k" in data_notice
            and "mouse brain sagittal-anterior" in data_notice
            and "cc by 4.0" in data_notice
            and "10x genomics" in data_notice
            and data_notice.count("attribution:") >= 2
            and "no raw third-party data is committed" in data_notice
        )
        if not attribution_ok:
            findings.append(
                {
                    "severity": "blocker",
                    "code": "missing-third-party-data-attribution",
                    "path": "THIRD_PARTY_DATA.md",
                }
            )

    source_config = root / "references" / "corpus-sources.json"
    example_config = root / "references" / "corpus-sources.example.json"
    if source_config.is_file() and example_config.is_file():
        if source_config.read_bytes() != example_config.read_bytes():
            findings.append(
                {
                    "severity": "blocker",
                    "code": "unsanitized-corpus-source-config",
                    "path": "references/corpus-sources.json",
                }
            )

    pbmc_manifest = root / "examples" / "pbmc3k" / "input_manifest.json"
    visium_manifest = root / "examples" / "visium-mouse-brain" / "input-manifest.json"
    if third_party_data.is_file() and pbmc_manifest.is_file() and visium_manifest.is_file():
        documented = third_party_data.read_text(encoding="utf-8")
        pbmc = _load_json(pbmc_manifest)["inputs"][0]
        visium = _load_json(visium_manifest)["files"]
        fingerprints = [
            (pbmc["content_length_bytes"], pbmc["sha256"]),
            *[(item["expected_size_bytes"], item["expected_sha256"]) for item in visium],
        ]
        for size, digest in fingerprints:
            if f"{int(size):,}" not in documented or str(digest) not in documented:
                findings.append(
                    {
                        "severity": "blocker",
                        "code": "third-party-data-manifest-mismatch",
                        "path": "THIRD_PARTY_DATA.md",
                    }
                )
                break

    registry = root / "references" / "p0-teaching-cases.json"
    if registry.is_file():
        payload = _load_json(registry)
        for case in payload.get("cases", []):
            execution = str(case.get("execution_status", "")).casefold()
            maturity = str(case.get("maturity", "")).casefold()
            if execution != "not-executed" or maturity in {"data-verified", "native-reviewed"}:
                findings.append(
                    {
                        "severity": "blocker",
                        "code": "public-registry-maturity-inflation",
                        "path": f"references/p0-teaching-cases.json:{case.get('case_id', 'unknown')}",
                    }
                )

    lock_path = root / "skills.lock.json"
    if lock_path.is_file():
        try:
            lock = _load_json(lock_path)
            dependency = lock["dependencies"]["visualization-2026718-v1"]
            commit = dependency["commit"]
            digest = dependency["content_sha256"]
            if lock.get("schema_version") != "1.0.0":
                raise ValueError("schema_version must be 1.0.0")
            if set(lock.get("dependencies", {})) != {"visualization-2026718-v1"}:
                raise ValueError("lock must contain exactly one reviewed dependency")
            if dependency.get("repository") != EXPECTED_VISUALIZATION_REPOSITORY:
                raise ValueError("visualization repository must match the reviewed HTTPS remote")
            if not re.fullmatch(r"[0-9a-f]{40}", commit):
                raise ValueError("commit must be a full lowercase Git SHA")
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError("content_sha256 must be lowercase SHA-256")
            if dependency.get("subdirectory") != EXPECTED_VISUALIZATION_SUBDIRECTORY:
                raise ValueError("visualization subdirectory must match the reviewed skill path")
            if dependency.get("distribution_profile_file") != "public-install-profile.json":
                raise ValueError(
                    "visualization distribution profile must be public-install-profile.json"
                )
            if dependency.get("excluded_paths") != [
                "assets/previews-curated",
                "assets/scheme-candidates",
                "assets/source_archive",
                "references/catalog.jsonl",
            ]:
                raise ValueError("visualization public exclusions must match exactly")
            if "license" in dependency or "notice_file" in dependency:
                raise ValueError("visualization lock uses ambiguous legacy rights fields")
            if dependency.get("original_code_license") != "MIT":
                raise ValueError("visualization original code license must be MIT")
            if dependency.get("license_file") != "LICENSE":
                raise ValueError("visualization original code license file must be LICENSE")
            if dependency.get("third_party_notice_file") != "NOTICE.md":
                raise ValueError("visualization third-party notice must be NOTICE.md")
            if (
                dependency.get("rights_status")
                != "mixed-original-and-third-party-not-relicensed"
            ):
                raise ValueError("visualization rights status must preserve mixed rights")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            findings.append(
                {
                    "severity": "blocker",
                    "code": "invalid-skills-lock",
                    "path": f"skills.lock.json:{exc}",
                }
            )

    return {
        "schema_version": "1.0.0",
        "ok": not findings,
        "root": root.name,
        "files_scanned": len(files),
        "findings": findings,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = validate(args.root)
    except (OSError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        return 2
    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    else:
        sys.stdout.write(payload)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
