#!/usr/bin/env python3
"""Verify a biomedical-analysis-agent release-evidence ZIP using stdlib only."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import sys
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any


SHA256 = re.compile(r"[0-9a-f]{64}")
FULL_SHA = re.compile(r"[0-9a-f]{40}")
SEMVER_TAG = re.compile(r"v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")
SENSITIVE_LOCATOR = re.compile(
    rb"(?i)(?:file:/[/\\]|(?<![A-Za-z0-9])[A-Z]:[\\/]"
    rb"|\\\\[^\\/\s]+[\\/]|/(?:home|Users|tmp)/)"
)
FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
MAX_BUNDLE_BYTES = 64 * 1024 * 1024
MAX_MEMBER_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MANIFEST_NAME = "evidence-bundle-manifest.json"
RELEASE_EVIDENCE_NAME = "release-evidence.json"
VERIFIER_NAME = "verify_release_evidence_bundle.py"
INSTRUCTIONS_NAME = "VERIFY.md"
REQUIRED_GATES = {
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


class EvidenceBundleError(RuntimeError):
    """Raised when an evidence ZIP is unsafe or internally inconsistent."""


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in value
        or path.as_posix() != value
    ):
        raise EvidenceBundleError(f"unsafe evidence-bundle path: {value}")
    return path


def _reject_sensitive(content: bytes, label: str) -> None:
    if SENSITIVE_LOCATOR.search(content):
        raise EvidenceBundleError(f"sensitive absolute or file URI locator in {label}")


def _validate_rewritten_evidence(
    payload: Any,
    *,
    version: str,
    commit: str,
    member_content: dict[str, bytes],
) -> int:
    if not isinstance(payload, dict):
        raise EvidenceBundleError("release-evidence.json root must be an object")
    if payload.get("schema_version") != "1.0.0":
        raise EvidenceBundleError("release evidence schema mismatch")
    if payload.get("release") != {"version": version, "commit": commit}:
        raise EvidenceBundleError("release evidence identity mismatch")
    gates = payload.get("gates")
    if not isinstance(gates, dict) or set(gates) != REQUIRED_GATES:
        raise EvidenceBundleError("release evidence does not contain the exact required gates")
    referenced: dict[str, str] = {}
    for gate_name, gate in gates.items():
        if not isinstance(gate, dict) or gate.get("status") != "passed":
            raise EvidenceBundleError(f"non-passed gate in evidence bundle: {gate_name}")
        if gate.get("commit") != commit:
            raise EvidenceBundleError(f"gate commit mismatch in evidence bundle: {gate_name}")
        evidence = gate.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            raise EvidenceBundleError(f"gate lacks bundled evidence: {gate_name}")
        for item in evidence:
            if not isinstance(item, dict):
                raise EvidenceBundleError(f"invalid evidence item: {gate_name}")
            locator = str(item.get("locator", ""))
            relative = _safe_relative(locator)
            if relative.parts[0] != "evidence" or len(relative.parts) < 2:
                raise EvidenceBundleError(f"non-bundled evidence locator: {locator}")
            content = member_content.get(locator)
            digest = str(item.get("sha256", ""))
            if content is None or not SHA256.fullmatch(digest) or sha256_bytes(content) != digest:
                raise EvidenceBundleError(f"missing or hash-mismatched evidence: {locator}")
            prior_digest = referenced.get(locator)
            if prior_digest is not None and prior_digest != digest:
                raise EvidenceBundleError(f"conflicting repeated evidence locator: {locator}")
            referenced[locator] = digest
    bundled = {name for name in member_content if name.startswith("evidence/")}
    if bundled != set(referenced):
        raise EvidenceBundleError("bundled evidence files do not exactly match rewritten locators")
    return len(referenced)


def verify_evidence_bundle(archive: Path, version: str, commit: str) -> dict[str, Any]:
    archive = archive.resolve(strict=True)
    if not SEMVER_TAG.fullmatch(version):
        raise EvidenceBundleError("version must be a lowercase v-prefixed SemVer tag")
    if not FULL_SHA.fullmatch(commit):
        raise EvidenceBundleError("commit must be a full lowercase Git SHA")
    if archive.stat().st_size > MAX_BUNDLE_BYTES:
        raise EvidenceBundleError("evidence bundle exceeds compressed size limit")
    prefix = f"biomedical-analysis-agent-{version.removeprefix('v')}-evidence"
    expected_archive_name = f"{prefix}.zip"
    if archive.name != expected_archive_name:
        raise EvidenceBundleError("evidence bundle filename mismatch")

    with zipfile.ZipFile(archive, "r") as bundle:
        if bundle.comment:
            raise EvidenceBundleError("evidence ZIP comment is forbidden")
        infos = bundle.infolist()
        names = [info.filename for info in infos]
        if names != sorted(names) or len(names) != len(set(names)):
            raise EvidenceBundleError("evidence ZIP paths are duplicate or non-deterministic")
        content: dict[str, bytes] = {}
        total = 0
        for info in infos:
            path = _safe_relative(info.filename)
            if path.parts[0] != prefix or len(path.parts) < 2:
                raise EvidenceBundleError(f"unexpected evidence ZIP prefix: {info.filename}")
            relative = PurePosixPath(*path.parts[1:]).as_posix()
            if info.is_dir() or info.file_size > MAX_MEMBER_BYTES:
                raise EvidenceBundleError(f"invalid evidence ZIP member: {relative}")
            mode = (info.external_attr >> 16) & 0xFFFF
            if info.create_system != 3 or mode != 0o100644 or stat.S_IFMT(mode) != stat.S_IFREG:
                raise EvidenceBundleError(f"non-regular evidence ZIP member: {relative}")
            if info.date_time != FIXED_ZIP_TIME:
                raise EvidenceBundleError(f"non-deterministic evidence ZIP timestamp: {relative}")
            if info.compress_type != zipfile.ZIP_DEFLATED or info.extra or info.comment:
                raise EvidenceBundleError(f"variable evidence ZIP metadata: {relative}")
            if info.flag_bits & 0x1:
                raise EvidenceBundleError(f"encrypted evidence ZIP member: {relative}")
            data = bundle.read(info)
            total += len(data)
            if total > MAX_TOTAL_BYTES:
                raise EvidenceBundleError("evidence ZIP exceeds uncompressed size limit")
            content[relative] = data

    required = {MANIFEST_NAME, RELEASE_EVIDENCE_NAME, VERIFIER_NAME, INSTRUCTIONS_NAME}
    missing = sorted(required - set(content))
    if missing:
        raise EvidenceBundleError("evidence ZIP is missing: " + ", ".join(missing))
    manifest = json.loads(content[MANIFEST_NAME].decode("utf-8"))
    if not isinstance(manifest, dict) or set(manifest) != {
        "schema_version",
        "bundle_type",
        "release",
        "root_prefix",
        "release_evidence_sha256",
        "evidence_file_count",
        "deterministic_zip_timestamp",
        "files",
    }:
        raise EvidenceBundleError("evidence-bundle manifest schema mismatch")
    if manifest.get("schema_version") != "1.0.0" or manifest.get("bundle_type") != "release-evidence":
        raise EvidenceBundleError("evidence-bundle manifest identity mismatch")
    if manifest.get("release") != {"version": version, "commit": commit}:
        raise EvidenceBundleError("evidence-bundle manifest release mismatch")
    if manifest.get("root_prefix") != prefix:
        raise EvidenceBundleError("evidence-bundle manifest prefix mismatch")
    if manifest.get("deterministic_zip_timestamp") != "1980-01-01T00:00:00Z":
        raise EvidenceBundleError("evidence-bundle manifest timestamp contract mismatch")

    records = manifest.get("files")
    if not isinstance(records, list) or not records:
        raise EvidenceBundleError("evidence-bundle manifest lacks files")
    allowlist: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict) or set(record) != {"path", "size_bytes", "sha256"}:
            raise EvidenceBundleError("invalid evidence-bundle file record")
        name = str(record.get("path", ""))
        _safe_relative(name)
        if name == MANIFEST_NAME or name in allowlist:
            raise EvidenceBundleError(f"duplicate or self-referential manifest record: {name}")
        size = record.get("size_bytes")
        digest = str(record.get("sha256", ""))
        if type(size) is not int or size < 0 or size > MAX_MEMBER_BYTES or not SHA256.fullmatch(digest):
            raise EvidenceBundleError(f"invalid evidence-bundle record: {name}")
        allowlist[name] = record
    if set(content) != set(allowlist) | {MANIFEST_NAME}:
        raise EvidenceBundleError("evidence ZIP inventory differs from manifest")
    for name, record in allowlist.items():
        data = content[name]
        if len(data) != record["size_bytes"] or sha256_bytes(data) != record["sha256"]:
            raise EvidenceBundleError(f"evidence ZIP member mismatch: {name}")

    release_evidence_bytes = content[RELEASE_EVIDENCE_NAME]
    if manifest.get("release_evidence_sha256") != sha256_bytes(release_evidence_bytes):
        raise EvidenceBundleError("rewritten release evidence hash mismatch")
    rewritten = json.loads(release_evidence_bytes.decode("utf-8"))
    evidence_count = _validate_rewritten_evidence(
        rewritten,
        version=version,
        commit=commit,
        member_content=content,
    )
    if manifest.get("evidence_file_count") != evidence_count:
        raise EvidenceBundleError("evidence file count mismatch")
    for name, data in content.items():
        if name != VERIFIER_NAME:
            _reject_sensitive(data, name)
    instructions = content[INSTRUCTIONS_NAME].decode("utf-8")
    if VERIFIER_NAME not in instructions or expected_archive_name not in instructions:
        raise EvidenceBundleError("VERIFY.md lacks an explicit standalone verification command")
    return {
        "schema_version": "1.0.0",
        "ok": True,
        "version": version,
        "commit": commit,
        "evidence_bundle_sha256": sha256_file(archive),
        "evidence_files": evidence_count,
        "bundle_members": len(content),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--commit", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = verify_evidence_bundle(args.archive, args.version, args.commit)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, zipfile.BadZipFile, EvidenceBundleError) as exc:
        sys.stderr.write(f"EVIDENCE_BUNDLE_VERIFICATION_ERROR: {exc}\n")
        return 2
    sys.stdout.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
