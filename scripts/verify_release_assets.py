#!/usr/bin/env python3
"""Independently verify release ZIP paths, checksums, metadata, and text boundary."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import sys
import zipfile
from pathlib import Path, PurePosixPath

from verify_release_evidence_bundle import (
    EvidenceBundleError,
    verify_evidence_bundle,
)


SHA256 = re.compile(r"[0-9a-f]{64}")
FULL_SHA = re.compile(r"[0-9a-f]{40}")
PRIVATE_PATH = re.compile(rb"(?i)(?:[A-Z]:[\\/]Users[\\/][^\\/\s\"']+|/home/[^/\s\"']+)")
SENSITIVE_LOCATOR = re.compile(
    rb"(?i)(?:file:/[/\\]|(?<![A-Za-z0-9])[A-Z]:[\\/]"
    rb"|\\\\[^\\/\s]+[\\/]|/(?:home|Users|tmp)/)"
)
FORBIDDEN_SUFFIXES = {
    ".7z", ".bat", ".cmd", ".com", ".dll", ".dmg", ".exe", ".gz",
    ".h5", ".h5ad", ".iso", ".jar", ".joblib", ".msi", ".npy", ".npz",
    ".pickle", ".pkl", ".ps1", ".rar", ".rdata", ".rds", ".scr", ".so",
    ".tar", ".tgz", ".tif", ".tiff", ".whl", ".zip",
}
MAX_RELEASE_FILE_BYTES = 16 * 1024 * 1024
MAX_RELEASE_TOTAL_BYTES = 64 * 1024 * 1024
MAX_RELEASE_ARCHIVE_BYTES = 64 * 1024 * 1024
FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
REQUIRED_ARCHIVE_FILES = {
    "README.md",
    "LICENSE",
    "NOTICE",
    "THIRD_PARTY_DATA.md",
    "VALIDATION.md",
    "skills.lock.json",
    ".github/workflows/ci.yml",
    ".github/workflows/real-data-release-gate.yml",
    "scripts/verify_release_evidence_bundle.py",
}


class VerificationError(RuntimeError):
    """Raised when release assets are internally inconsistent."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_checksums(path: Path) -> dict[str, str]:
    records: dict[str, str] = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        match = re.fullmatch(r"([0-9a-f]{64})  ([^/\\]+)", line)
        if not match:
            raise VerificationError(f"invalid checksum record at line {line_number}")
        digest, name = match.groups()
        if name in records:
            raise VerificationError(f"duplicate checksum record: {name}")
        records[name] = digest
    return records


def verify(
    archive: Path,
    evidence_archive: Path,
    checksums: Path,
    summary_path: Path,
    version: str,
    commit: str,
    release_validation: Path,
) -> dict:
    archive = archive.resolve(strict=True)
    evidence_archive = evidence_archive.resolve(strict=True)
    checksums = checksums.resolve(strict=True)
    summary_path = summary_path.resolve(strict=True)
    release_validation = release_validation.resolve(strict=True)
    if not FULL_SHA.fullmatch(commit):
        raise VerificationError("commit must be a full lowercase Git SHA")
    records = parse_checksums(checksums)
    expected_records = {
        archive.name,
        evidence_archive.name,
        summary_path.name,
        release_validation.name,
    }
    if set(records) != expected_records:
        raise VerificationError(
            "checksum manifest must name exactly both archives and both validation assets"
        )
    for path in (archive, evidence_archive, summary_path, release_validation):
        expected = records.get(path.name)
        if expected is None or sha256_file(path) != expected:
            raise VerificationError(f"checksum mismatch or missing record: {path.name}")

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    for public_asset in (checksums, summary_path, release_validation):
        if SENSITIVE_LOCATOR.search(public_asset.read_bytes()):
            raise VerificationError(f"sensitive absolute or file URI locator in {public_asset.name}")
    if summary.get("release") != {"version": version, "commit": commit}:
        raise VerificationError("release summary version/commit mismatch")
    if summary.get("archive", {}).get("filename") != archive.name:
        raise VerificationError("release summary archive name mismatch")
    if summary.get("archive", {}).get("sha256") != sha256_file(archive):
        raise VerificationError("release summary archive hash mismatch")
    evidence_summary = summary.get("evidence", {})
    if evidence_summary.get("all_required_gates_passed") is not True:
        raise VerificationError("release summary does not assert all evidence gates")
    if evidence_summary.get("required_gate_count") != 9:
        raise VerificationError("release summary required gate count mismatch")
    summary_gates = evidence_summary.get("gates")
    if (
        not isinstance(summary_gates, dict)
        or set(summary_gates) != {
            "local_static",
            "local_pbmc3k",
            "local_visium_mouse_brain",
            "local_native_visual_review",
            "upstream_visualization",
            "github_actions_ci",
            "github_actions_real_data",
            "anonymous_clone",
            "license_and_leak_scan",
        }
        or any(
            not isinstance(gate, dict) or gate.get("status") != "passed"
            for gate in summary_gates.values()
        )
    ):
        raise VerificationError("release summary gate inventory/status mismatch")
    try:
        evidence_verification = verify_evidence_bundle(evidence_archive, version, commit)
    except EvidenceBundleError as exc:
        raise VerificationError(f"evidence archive verification failed: {exc}") from exc
    evidence_record = summary.get("evidence", {}).get("archive", {})
    if evidence_record.get("filename") != evidence_archive.name:
        raise VerificationError("release summary evidence archive name mismatch")
    if evidence_record.get("sha256") != sha256_file(evidence_archive):
        raise VerificationError("release summary evidence archive hash mismatch")
    if evidence_record.get("compressed_size_bytes") != evidence_archive.stat().st_size:
        raise VerificationError("release summary evidence archive size mismatch")
    if evidence_record.get("verification") != evidence_verification:
        raise VerificationError("release summary evidence verification mismatch")
    validation_record = summary.get("release_validation", {})
    if validation_record.get("filename") != release_validation.name:
        raise VerificationError("release validation asset name mismatch")
    if validation_record.get("sha256") != sha256_file(release_validation):
        raise VerificationError("release validation asset hash mismatch")
    release_validation_text = release_validation.read_text(encoding="utf-8")
    if re.search(r"(?i)\bPENDING\b|\(\s*draft\s*\)", release_validation_text):
        raise VerificationError("release validation asset contains pending/draft language")
    for required_text in (
        version,
        commit,
        archive.name,
        sha256_file(archive),
        evidence_archive.name,
        sha256_file(evidence_archive),
    ):
        if required_text not in release_validation_text:
            raise VerificationError("release validation asset is not bound to release identity")
    if archive.stat().st_size > MAX_RELEASE_ARCHIVE_BYTES:
        raise VerificationError("archive exceeds compressed size limit")

    manifest = summary.get("archive", {}).get("tracked_files")
    if not isinstance(manifest, list) or not manifest:
        raise VerificationError("release summary lacks a tracked-file allowlist")
    allowed: dict[str, dict] = {}
    for index, record in enumerate(manifest):
        if not isinstance(record, dict):
            raise VerificationError(f"invalid allowlist record at index {index}")
        name = str(record.get("path", ""))
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or not path.parts or path.as_posix() != name:
            raise VerificationError(f"unsafe allowlist path: {name}")
        if name in allowed:
            raise VerificationError(f"duplicate allowlist path: {name}")
        size = record.get("size_bytes")
        digest = str(record.get("sha256", ""))
        if not isinstance(size, int) or size < 0 or size > MAX_RELEASE_FILE_BYTES:
            raise VerificationError(f"invalid allowlist size: {name}")
        if not SHA256.fullmatch(digest):
            raise VerificationError(f"invalid allowlist hash: {name}")
        if path.suffix.casefold() in FORBIDDEN_SUFFIXES:
            raise VerificationError(f"dangerous extension in allowlist: {name}")
        allowed[name] = record
    expected_uncompressed = sum(item["size_bytes"] for item in allowed.values())
    if expected_uncompressed > MAX_RELEASE_TOTAL_BYTES:
        raise VerificationError("allowlisted payload exceeds uncompressed size limit")
    if summary.get("archive", {}).get("uncompressed_size_bytes") != expected_uncompressed:
        raise VerificationError("release summary uncompressed byte count mismatch")
    if summary.get("archive", {}).get("compressed_size_bytes") != archive.stat().st_size:
        raise VerificationError("release summary compressed byte count mismatch")
    if summary.get("archive", {}).get("deterministic_zip_timestamp") != "1980-01-01T00:00:00Z":
        raise VerificationError("release summary lacks the deterministic ZIP timestamp")

    expected_prefix = f"biomedical-analysis-agent-{version.removeprefix('v')}"
    with zipfile.ZipFile(archive, "r") as bundle:
        if bundle.comment:
            raise VerificationError("ZIP archive comment is forbidden")
        infos = bundle.infolist()
        names = [info.filename for info in infos]
        if len(names) != len(set(names)):
            raise VerificationError("archive contains duplicate paths")
        if names != sorted(names):
            raise VerificationError("archive members are not in deterministic path order")
        relative_names: set[str] = set()
        total_uncompressed = 0
        for info in infos:
            path = PurePosixPath(info.filename)
            if (
                path.is_absolute()
                or ".." in path.parts
                or not path.parts
                or path.as_posix() != info.filename
            ):
                raise VerificationError(f"unsafe archive path: {info.filename}")
            if path.parts[0] != expected_prefix:
                raise VerificationError(f"unexpected archive prefix: {info.filename}")
            relative = PurePosixPath(*path.parts[1:])
            relative_text = relative.as_posix()
            relative_names.add(relative_text)
            if info.is_dir():
                raise VerificationError(f"directory entry is forbidden: {relative}")
            mode = (info.external_attr >> 16) & 0xFFFF
            if info.create_system != 3 or mode != 0o100644 or stat.S_IFMT(mode) != stat.S_IFREG:
                raise VerificationError(f"non-regular ZIP member is forbidden: {relative}")
            if info.date_time != FIXED_ZIP_TIME:
                raise VerificationError(f"non-deterministic ZIP timestamp: {relative}")
            if info.compress_type != zipfile.ZIP_DEFLATED:
                raise VerificationError(f"unexpected ZIP compression method: {relative}")
            if info.extra or info.comment:
                raise VerificationError(f"variable ZIP metadata is forbidden: {relative}")
            if info.flag_bits & 0x1:
                raise VerificationError(f"encrypted ZIP member is forbidden: {relative}")
            record = allowed.get(relative_text)
            if record is None:
                raise VerificationError(f"archive path is absent from tracked allowlist: {relative}")
            if info.file_size != record["size_bytes"] or info.file_size > MAX_RELEASE_FILE_BYTES:
                raise VerificationError(f"archive member size mismatch: {relative}")
            total_uncompressed += info.file_size
            if total_uncompressed > MAX_RELEASE_TOTAL_BYTES:
                raise VerificationError("archive exceeds uncompressed size limit")
            if relative.suffix.casefold() in FORBIDDEN_SUFFIXES:
                raise VerificationError(f"dangerous extension in archive: {relative}")
            text_file = relative.suffix.casefold() in {
                ".md", ".py", ".json", ".jsonl", ".yaml", ".yml", ".txt", ".r"
            } or relative.name in {"LICENSE", "NOTICE", ".gitignore", ".gitattributes"}
            if info.file_size <= 2 * 1024 * 1024 and text_file:
                content = bundle.read(info)
                if hashlib.sha256(content).hexdigest() != record["sha256"]:
                    raise VerificationError(f"archive member hash mismatch: {relative}")
                if PRIVATE_PATH.search(content):
                    raise VerificationError(f"private home locator in archive: {relative}")
            else:
                with bundle.open(info) as handle:
                    digest = hashlib.sha256()
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                if digest.hexdigest() != record["sha256"]:
                    raise VerificationError(f"archive member hash mismatch: {relative}")
        if relative_names != set(allowed):
            missing_allowed = sorted(set(allowed) - relative_names)
            raise VerificationError(
                "archive does not exactly match tracked allowlist: " + ", ".join(missing_allowed)
            )
        missing = sorted(REQUIRED_ARCHIVE_FILES - relative_names)
        if missing:
            raise VerificationError(f"archive is missing required files: {', '.join(missing)}")
        if summary.get("archive", {}).get("file_count") != len(infos) or len(infos) != len(allowed):
            raise VerificationError("archive file count differs from release summary")
    return {
        "schema_version": "1.0.0",
        "ok": True,
        "version": version,
        "commit": commit,
        "archive_sha256": sha256_file(archive),
        "evidence_archive_sha256": sha256_file(evidence_archive),
        "evidence_files": evidence_verification["evidence_files"],
        "archive_files": len(infos),
        "checksum_records": len(records),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--evidence-archive", type=Path, required=True)
    parser.add_argument("--checksums", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--release-validation", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--commit", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = verify(
            args.archive,
            args.evidence_archive,
            args.checksums,
            args.summary,
            args.version,
            args.commit,
            args.release_validation,
        )
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        zipfile.BadZipFile,
        VerificationError,
    ) as exc:
        sys.stderr.write(f"RELEASE_ASSET_VERIFICATION_ERROR: {exc}\n")
        return 2
    sys.stdout.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
