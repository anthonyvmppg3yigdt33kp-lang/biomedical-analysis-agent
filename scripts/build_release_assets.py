#!/usr/bin/env python3
"""Build deterministic release assets only after every release gate is passed."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from validate_distribution import validate as validate_distribution
from validate_release_evidence import EVIDENCE_ROOT, validate_evidence
from verify_release_evidence_bundle import (
    EvidenceBundleError,
    verify_evidence_bundle,
)
from verify_release_assets import (
    FORBIDDEN_SUFFIXES,
    VerificationError,
    verify as verify_release_assets,
)


ROOT = Path(__file__).resolve().parents[1]
FULL_SHA = re.compile(r"[0-9a-f]{40}")
SEMVER_TAG = re.compile(r"v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")
FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
MAX_RELEASE_FILE_BYTES = 16 * 1024 * 1024
MAX_RELEASE_TOTAL_BYTES = 64 * 1024 * 1024
MAX_RELEASE_ARCHIVE_BYTES = 64 * 1024 * 1024
MUTABLE_RELEASE_ASSERTIONS = (
    re.compile(r"(?i)\b(?:is|has)\s+not\s+(?:been\s+)?released\b"),
    re.compile(r"(?i)\bcurrent\s+(?:candidate\s+status|blocker)\b"),
    re.compile(r"(?i)\bno\s+tag\s+or\s+github\s+release\b"),
)
FORBIDDEN_RELEASE_PARTS = {
    ".cache",
    ".renv",
    ".task-skills",
    "runs",
    "work",
    "release-assets",
    "private-corpus-index",
}
FORBIDDEN_RELEASE_PATHS = {"references/corpus-sources.json"}
SENSITIVE_LOCATOR = re.compile(
    rb"(?i)(?:file:/[/\\]|(?<![A-Za-z0-9])[A-Z]:[\\/]"
    rb"|\\\\[^\\/\s]+[\\/]|/(?:home|Users|tmp)/)"
)


class ReleaseAssetError(RuntimeError):
    """Raised when packaging would bypass a release requirement."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_sha256(payload: Any) -> str:
    rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def git_output(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise ReleaseAssetError(completed.stderr.strip() or "git command failed")
    return completed.stdout.strip()


def validate_version_documents(root: Path, version: str) -> None:
    if not SEMVER_TAG.fullmatch(version):
        raise ReleaseAssetError("version must be a lowercase v-prefixed SemVer tag")
    plain = version.removeprefix("v")
    notes = root / f"RELEASE_NOTES_{version}.md"
    if not notes.is_file():
        raise ReleaseAssetError(f"release notes are missing: {notes.name}")
    notes_text = notes.read_text(encoding="utf-8")
    if version not in notes_text:
        raise ReleaseAssetError("release notes do not name the requested version")
    if re.search(r"(?i)\bdraft\b", notes_text):
        raise ReleaseAssetError("release notes still carry a draft marker")
    changelog = root / "CHANGELOG.md"
    if not changelog.is_file() or f"[{plain}]" not in changelog.read_text(encoding="utf-8"):
        raise ReleaseAssetError("CHANGELOG.md does not contain the requested version")
    validation = root / "VALIDATION.md"
    if not validation.is_file():
        raise ReleaseAssetError("VALIDATION.md template is missing")
    if re.search(r"(?i)\bPENDING\b", validation.read_text(encoding="utf-8")):
        raise ReleaseAssetError(
            "VALIDATION.md contains PENDING; keep a claim-free template and use the "
            "generated RELEASE_VALIDATION.md asset for commit-bound evidence"
        )
    lifecycle_documents = {
        "README.md": root / "README.md",
        "CHANGELOG.md": changelog,
        "VALIDATION.md": validation,
        notes.name: notes,
    }
    for name, path in lifecycle_documents.items():
        if not path.is_file():
            raise ReleaseAssetError(f"release lifecycle document is missing: {name}")
        text = path.read_text(encoding="utf-8")
        if any(pattern.search(text) for pattern in MUTABLE_RELEASE_ASSERTIONS):
            raise ReleaseAssetError(
                f"{name} contains a mutable publication-status assertion; "
                "tracked release documents must remain valid before and after tagging"
            )
    dated_heading = re.compile(
        rf"(?m)^##\s+\[{re.escape(plain)}\]\s+-\s+\d{{4}}-\d{{2}}-\d{{2}}\s*$"
    )
    if dated_heading.search(changelog.read_text(encoding="utf-8")):
        raise ReleaseAssetError(
            "CHANGELOG.md hard-codes a publication date before tag metadata is authoritative"
        )


def release_files(root: Path, output_dir: Path) -> list[Path]:
    """Return only regular files recorded in the Git index.

    Ignored and untracked paths are deliberately invisible to this function.  Index
    modes are checked before reading the worktree so a symlink or submodule cannot be
    dereferenced and smuggled into the archive.
    """

    del output_dir  # The allowlist comes exclusively from the Git index.
    completed = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--stage", "-z"],
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise ReleaseAssetError(
            completed.stderr.decode("utf-8", errors="replace").strip()
            or "git ls-files failed"
        )
    files: list[Path] = []
    total_bytes = 0
    seen: set[str] = set()
    for raw_entry in completed.stdout.split(b"\0"):
        if not raw_entry:
            continue
        try:
            metadata, raw_name = raw_entry.split(b"\t", 1)
            mode, _object_id, stage = metadata.decode("ascii").split(" ")
            relative_text = raw_name.decode("utf-8")
        except (UnicodeDecodeError, ValueError) as exc:
            raise ReleaseAssetError("cannot parse a git ls-files index record") from exc
        if stage != "0":
            raise ReleaseAssetError(f"unmerged index entry is forbidden: {relative_text}")
        if mode == "120000":
            raise ReleaseAssetError(f"tracked symlink is forbidden: {relative_text}")
        if mode not in {"100644", "100755"}:
            raise ReleaseAssetError(
                f"only tracked regular files may be released: {relative_text} (mode {mode})"
            )
        relative = Path(relative_text)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise ReleaseAssetError(f"unsafe tracked path: {relative_text}")
        normalized = relative.as_posix()
        folded_parts = {part.casefold() for part in PurePosixPath(normalized).parts}
        if (
            folded_parts & FORBIDDEN_RELEASE_PARTS
            or normalized.casefold() in FORBIDDEN_RELEASE_PATHS
            or normalized.casefold().startswith("validation/runtime/")
        ):
            raise ReleaseAssetError(f"tracked path is forbidden from release assets: {normalized}")
        if normalized in seen:
            raise ReleaseAssetError(f"duplicate tracked path: {normalized}")
        seen.add(normalized)
        path = root / relative
        if path.is_symlink() or not path.is_file():
            raise ReleaseAssetError(f"tracked path is not a regular worktree file: {normalized}")
        if path.suffix.casefold() in FORBIDDEN_SUFFIXES:
            raise ReleaseAssetError(f"dangerous tracked extension is forbidden: {normalized}")
        try:
            path.resolve(strict=True).relative_to(root)
        except ValueError as exc:
            raise ReleaseAssetError(f"tracked path escapes repository root: {normalized}") from exc
        size = path.stat().st_size
        if size > MAX_RELEASE_FILE_BYTES:
            raise ReleaseAssetError(
                f"tracked file exceeds {MAX_RELEASE_FILE_BYTES} bytes: {normalized}"
            )
        total_bytes += size
        if total_bytes > MAX_RELEASE_TOTAL_BYTES:
            raise ReleaseAssetError(
                f"tracked payload exceeds {MAX_RELEASE_TOTAL_BYTES} uncompressed bytes"
            )
        files.append(path)
    if not files:
        raise ReleaseAssetError("git index contains no tracked regular files")
    return sorted(files, key=lambda item: item.relative_to(root).as_posix())


def build_deterministic_zip(root: Path, files: Iterable[Path], archive: Path, prefix: str) -> int:
    archive.parent.mkdir(parents=True, exist_ok=True)
    temporary = archive.with_suffix(archive.suffix + ".tmp")
    count = 0
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
            for path in files:
                relative = path.relative_to(root).as_posix()
                info = zipfile.ZipInfo(f"{prefix}/{relative}", date_time=FIXED_ZIP_TIME)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = (0o100644 & 0xFFFF) << 16
                bundle.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
                count += 1
        os.replace(temporary, archive)
    finally:
        if temporary.exists():
            temporary.unlink()
    return count


def _json_bytes(payload: Any) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _reject_sensitive_public_content(content: bytes, label: str) -> None:
    if SENSITIVE_LOCATOR.search(content):
        raise ReleaseAssetError(f"sensitive absolute or file URI locator in public asset: {label}")


def _write_deterministic_members(
    archive: Path,
    *,
    prefix: str,
    members: dict[str, bytes],
) -> None:
    archive.parent.mkdir(parents=True, exist_ok=True)
    temporary = archive.with_suffix(archive.suffix + ".tmp")
    try:
        with zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as bundle:
            for relative in sorted(members):
                path = PurePosixPath(relative)
                if (
                    not path.parts
                    or path.is_absolute()
                    or ".." in path.parts
                    or "\\" in relative
                    or path.as_posix() != relative
                ):
                    raise ReleaseAssetError(f"unsafe generated ZIP member: {relative}")
                info = zipfile.ZipInfo(f"{prefix}/{relative}", date_time=FIXED_ZIP_TIME)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = (0o100644 & 0xFFFF) << 16
                bundle.writestr(
                    info,
                    members[relative],
                    compress_type=zipfile.ZIP_DEFLATED,
                    compresslevel=9,
                )
        os.replace(temporary, archive)
    finally:
        if temporary.exists():
            temporary.unlink()


def build_evidence_bundle(
    *,
    root: Path,
    evidence: dict[str, Any],
    archive: Path,
    version: str,
    commit: str,
) -> dict[str, Any]:
    """Create a sanitized, self-contained, deterministic evidence bundle."""

    rewritten = copy.deepcopy(evidence)
    members: dict[str, bytes] = {}
    source_to_member: dict[str, str] = {}
    for gate_name, gate in rewritten["gates"].items():
        for index, item in enumerate(gate["evidence"]):
            locator = PurePosixPath(str(item["locator"]))
            if locator.parts[: len(EVIDENCE_ROOT.parts)] != EVIDENCE_ROOT.parts:
                raise ReleaseAssetError(
                    f"validated evidence escaped the release-evidence root: {gate_name}[{index}]"
                )
            tail = PurePosixPath(*locator.parts[len(EVIDENCE_ROOT.parts) :])
            bundled_locator = (PurePosixPath("evidence") / tail).as_posix()
            source_path = (root / Path(*locator.parts)).resolve(strict=True)
            try:
                source_path.relative_to(root)
            except ValueError as exc:
                raise ReleaseAssetError("validated evidence path escaped repository root") from exc
            content = source_path.read_bytes()
            _reject_sensitive_public_content(content, bundled_locator)
            previous_member = source_to_member.get(locator.as_posix())
            if previous_member is not None and previous_member != bundled_locator:
                raise ReleaseAssetError("one evidence source maps to multiple bundle members")
            existing = members.get(bundled_locator)
            if existing is not None and existing != content:
                raise ReleaseAssetError(f"evidence bundle path collision: {bundled_locator}")
            source_to_member[locator.as_posix()] = bundled_locator
            members[bundled_locator] = content
            item["locator"] = bundled_locator

    release_evidence_bytes = _json_bytes(rewritten)
    _reject_sensitive_public_content(release_evidence_bytes, "release-evidence.json")
    members["release-evidence.json"] = release_evidence_bytes
    verifier_path = ROOT / "scripts" / "verify_release_evidence_bundle.py"
    verifier_bytes = verifier_path.read_bytes()
    members["verify_release_evidence_bundle.py"] = verifier_bytes
    prefix = f"biomedical-analysis-agent-{version.removeprefix('v')}-evidence"
    instructions = "\n".join(
        [
            f"# {version} evidence bundle verification",
            "",
            "Extract the verifier next to this ZIP, then run:",
            "",
            "```text",
            f"python verify_release_evidence_bundle.py {archive.name} --version {version} --commit {commit}",
            "```",
            "",
            "The verifier uses only the Python standard library and checks the complete",
            "manifest, member hashes, release identity, deterministic ZIP metadata,",
            "rewritten relative locators, and sensitive-path boundary.",
            "",
        ]
    ).encode("utf-8")
    _reject_sensitive_public_content(instructions, "VERIFY.md")
    members["VERIFY.md"] = instructions
    manifest_records = [
        {
            "path": name,
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
        for name, content in sorted(members.items())
    ]
    manifest = {
        "schema_version": "1.0.0",
        "bundle_type": "release-evidence",
        "release": {"version": version, "commit": commit},
        "root_prefix": prefix,
        "release_evidence_sha256": hashlib.sha256(release_evidence_bytes).hexdigest(),
        "evidence_file_count": len(
            [name for name in members if name.startswith("evidence/")]
        ),
        "deterministic_zip_timestamp": "1980-01-01T00:00:00Z",
        "files": manifest_records,
    }
    manifest_bytes = _json_bytes(manifest)
    _reject_sensitive_public_content(manifest_bytes, "evidence-bundle-manifest.json")
    members["evidence-bundle-manifest.json"] = manifest_bytes
    _write_deterministic_members(archive, prefix=prefix, members=members)
    if archive.stat().st_size > MAX_RELEASE_ARCHIVE_BYTES:
        archive.unlink(missing_ok=True)
        raise ReleaseAssetError("release evidence archive exceeds compressed size limit")
    try:
        verification = verify_evidence_bundle(archive, version, commit)
    except EvidenceBundleError as exc:
        raise ReleaseAssetError(f"generated evidence bundle failed verification: {exc}") from exc
    return {
        "filename": archive.name,
        "sha256": sha256_file(archive),
        "compressed_size_bytes": archive.stat().st_size,
        "rewritten_release_evidence_sha256": hashlib.sha256(
            release_evidence_bytes
        ).hexdigest(),
        "evidence_file_count": manifest["evidence_file_count"],
        "verification": verification,
    }


def tracked_manifest(root: Path, files: Iterable[Path]) -> list[dict[str, Any]]:
    return [
        {
            "path": path.relative_to(root).as_posix(),
            "size_bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        for path in files
    ]


def render_release_validation(
    evidence: dict[str, Any],
    *,
    version: str,
    commit: str,
    archive_name: str,
    archive_sha256: str,
    evidence_sha256: str,
    evidence_archive_name: str,
    evidence_archive_sha256: str,
) -> str:
    """Render an external, commit-bound validation asset without editing Git."""

    gate_sections = []
    for gate_name in sorted(evidence["gates"]):
        gate = evidence["gates"][gate_name]
        evidence_hashes = ", ".join(
            f"`{item['sha256']}`" for item in gate["evidence"]
        )
        details = json.dumps(
            gate.get("details", {}), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        gate_sections.extend(
            [
                f"### `{gate_name}`",
                "",
                f"- Status: `{gate['status']}`",
                f"- Evidence SHA-256: {evidence_hashes}",
                f"- Details: `{details.replace('`', '&#96;')}`",
            ]
        )
        if gate_name in {"github_actions_ci", "github_actions_real_data"}:
            gate_sections.append(f"- Actions run: {gate['details']['run_url']}")
        gate_sections.append("")
    return "\n".join(
        [
            f"# {version} release validation",
            "",
            "This independent release asset was generated only after the machine-readable",
            "evidence document passed every required gate. The tracked `VALIDATION.md` is a",
            "claim-free procedure template and is not used as post-commit evidence.",
            "",
            f"- Release commit: `{commit}`",
            f"- Source archive: `{archive_name}`",
            f"- Source archive SHA-256: `{archive_sha256}`",
            f"- Evidence JSON SHA-256: `{evidence_sha256}`",
            f"- Evidence archive: `{evidence_archive_name}`",
            f"- Evidence archive SHA-256: `{evidence_archive_sha256}`",
            "",
            "## Gate details",
            "",
            *gate_sections,
            "",
            "No native visual-review claim is inferred from generated PNGs; it is accepted",
            "only through the separately hash-bound native-review gate above.",
            "",
        ]
    )


def preflight(root: Path, version: str) -> dict[str, Any]:
    root = root.resolve(strict=True)
    validate_version_documents(root, version)
    distribution = validate_distribution(root)
    if not distribution["ok"]:
        blockers = ", ".join(
            sorted({f"{item['code']}:{item['path']}" for item in distribution["findings"]})
        )
        raise ReleaseAssetError(f"distribution validation failed: {blockers}")
    return distribution


def build_assets(
    root: Path,
    output_dir: Path,
    version: str,
    expected_commit: str,
    evidence_path: Path,
) -> dict[str, Any]:
    root = root.resolve(strict=True)
    output_dir = output_dir.resolve()
    if output_dir == root or root.is_relative_to(output_dir):
        raise ReleaseAssetError("output directory cannot equal or contain the source repository")
    if output_dir == Path(output_dir.anchor):
        raise ReleaseAssetError("output directory cannot be a filesystem root")
    if not FULL_SHA.fullmatch(expected_commit):
        raise ReleaseAssetError("expected_commit must be a full lowercase Git SHA")
    distribution = preflight(root, version)
    head = git_output(root, "rev-parse", "HEAD")
    if head != expected_commit:
        raise ReleaseAssetError(f"HEAD {head} differs from expected commit {expected_commit}")
    status = git_output(root, "status", "--porcelain", "--untracked-files=all")
    if status:
        raise ReleaseAssetError("source worktree is not clean")

    evidence_path = evidence_path.resolve(strict=True)
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    if not isinstance(evidence, dict):
        raise ReleaseAssetError("release evidence root must be an object")
    evidence_report = validate_evidence(
        evidence,
        expected_version=version,
        expected_commit=expected_commit,
        require_passed=True,
        repository_root=root,
    )
    if not evidence_report["ok"]:
        codes = ", ".join(sorted({item["code"] for item in evidence_report["findings"]}))
        raise ReleaseAssetError(f"release evidence is incomplete: {codes}")
    lock = json.loads((root / "skills.lock.json").read_text(encoding="utf-8"))
    pinned = lock["dependencies"]["visualization-2026718-v1"]
    upstream_details = evidence["gates"]["upstream_visualization"]["details"]
    if upstream_details.get("pinned_commit") != pinned.get("commit"):
        raise ReleaseAssetError("upstream evidence commit differs from skills.lock.json")
    if upstream_details.get("content_sha256") != pinned.get("content_sha256"):
        raise ReleaseAssetError("upstream evidence content hash differs from skills.lock.json")

    plain = version.removeprefix("v")
    prefix = f"biomedical-analysis-agent-{plain}"
    archive = output_dir / f"{prefix}.zip"
    files = release_files(root, output_dir)
    manifest = tracked_manifest(root, files)
    file_count = build_deterministic_zip(root, files, archive, prefix)
    if archive.stat().st_size > MAX_RELEASE_ARCHIVE_BYTES:
        archive.unlink(missing_ok=True)
        raise ReleaseAssetError(
            f"release archive exceeds {MAX_RELEASE_ARCHIVE_BYTES} compressed bytes"
        )
    archive_sha = sha256_file(archive)
    evidence_sha = sha256_file(evidence_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    evidence_archive = output_dir / f"{prefix}-evidence.zip"
    evidence_bundle = build_evidence_bundle(
        root=root,
        evidence=evidence,
        archive=evidence_archive,
        version=version,
        commit=expected_commit,
    )
    validation_asset = output_dir / "RELEASE_VALIDATION.md"
    validation_asset.write_text(
        render_release_validation(
            evidence,
            version=version,
            commit=expected_commit,
            archive_name=archive.name,
            archive_sha256=archive_sha,
            evidence_sha256=evidence_sha,
            evidence_archive_name=evidence_archive.name,
            evidence_archive_sha256=evidence_bundle["sha256"],
        ),
        encoding="utf-8",
        newline="\n",
    )
    public_gates = {
        gate_name: {
            "status": gate["status"],
            "details": gate.get("details", {}),
            "evidence_sha256": [item["sha256"] for item in gate["evidence"]],
        }
        for gate_name, gate in sorted(evidence["gates"].items())
    }
    actions_run_urls = {
        gate_name: evidence["gates"][gate_name]["details"]["run_url"]
        for gate_name in ("github_actions_ci", "github_actions_real_data")
    }
    summary = {
        "schema_version": "1.0.0",
        "release": {"version": version, "commit": expected_commit},
        "archive": {
            "filename": archive.name,
            "sha256": archive_sha,
            "file_count": file_count,
            "uncompressed_size_bytes": sum(item["size_bytes"] for item in manifest),
            "compressed_size_bytes": archive.stat().st_size,
            "tracked_files": manifest,
            "deterministic_zip_timestamp": "1980-01-01T00:00:00Z",
        },
        "evidence": {
            "sha256": evidence_sha,
            "required_gate_count": evidence_report["required_gate_count"],
            "all_required_gates_passed": True,
            "archive": evidence_bundle,
            "gates": public_gates,
            "actions_run_urls": actions_run_urls,
        },
        "release_validation": {
            "filename": validation_asset.name,
            "sha256": sha256_file(validation_asset),
            "source": "validated external evidence; not the tracked procedure template",
        },
        "distribution": {
            "ok": True,
            "files_scanned": distribution["files_scanned"],
            "report_sha256": canonical_sha256(distribution),
        },
    }
    summary_path = output_dir / "release-validation-summary.json"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    _reject_sensitive_public_content(validation_asset.read_bytes(), validation_asset.name)
    _reject_sensitive_public_content(summary_path.read_bytes(), summary_path.name)
    checksums = output_dir / "SHA256SUMS.txt"
    checksums.write_text(
        (
            f"{archive_sha}  {archive.name}\n"
            f"{evidence_bundle['sha256']}  {evidence_archive.name}\n"
            f"{sha256_file(summary_path)}  {summary_path.name}\n"
            f"{sha256_file(validation_asset)}  {validation_asset.name}\n"
        ),
        encoding="utf-8",
        newline="\n",
    )
    try:
        asset_verification = verify_release_assets(
            archive,
            evidence_archive,
            checksums,
            summary_path,
            version,
            expected_commit,
            validation_asset,
        )
    except VerificationError as exc:
        raise ReleaseAssetError(f"post-build asset verification failed: {exc}") from exc
    return {
        "ok": True,
        "archive": str(archive),
        "evidence_archive": str(evidence_archive),
        "checksums": str(checksums),
        "validation_summary": str(summary_path),
        "release_validation": str(validation_asset),
        "asset_verification": asset_verification,
        **summary,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--version", required=True)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "release-assets")
    parser.add_argument("--expected-commit")
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.dry_run:
            distribution = preflight(args.root, args.version)
            report = {
                "ok": True,
                "dry_run": True,
                "version": args.version,
                "files_scanned": distribution["files_scanned"],
                "assets_created": False,
                "release_evidence_checked": False,
            }
        else:
            if not args.expected_commit or args.evidence is None:
                raise ReleaseAssetError("final packaging requires --expected-commit and --evidence")
            report = build_assets(
                args.root,
                args.output_dir,
                args.version,
                args.expected_commit,
                args.evidence,
            )
    except (OSError, json.JSONDecodeError, subprocess.SubprocessError, ReleaseAssetError) as exc:
        sys.stderr.write(f"RELEASE_ASSET_ERROR: {exc}\n")
        return 2
    sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
