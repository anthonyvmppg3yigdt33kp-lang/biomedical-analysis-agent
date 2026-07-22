#!/usr/bin/env python3
"""Download, freeze, and verify the two public 10x Visium tutorial inputs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request


CHUNK_SIZE = 4 * 1024 * 1024
USER_AGENT = "biomedical-analysis-agent-visium-tutorial/1.0"
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
EXACT_FREEZE_POLICY = "exact_required"
RESOLVED_FREEZE_POLICY = "exact_required_manifest"


class InputError(RuntimeError):
    """Raised when an input cannot be safely frozen or verified."""


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InputError(f"cannot read JSON {path}: {exc}") from exc


def write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_record(path: Path) -> dict:
    if not path.is_file():
        raise InputError(f"required input is missing or not a file: {path}")
    return {"size_bytes": path.stat().st_size, "sha256": sha256_file(path)}


def validate_expected(record: dict, actual: dict, context: str) -> None:
    expected_size = record.get("expected_size_bytes", record.get("size_bytes"))
    expected_hash = record.get("expected_sha256", record.get("sha256"))
    if expected_size is not None and int(expected_size) != actual["size_bytes"]:
        raise InputError(
            f"size mismatch for {context}: expected {expected_size}, got {actual['size_bytes']}"
        )
    if expected_hash is not None and str(expected_hash).lower() != actual["sha256"]:
        raise InputError(
            f"SHA-256 mismatch for {context}: expected {expected_hash}, got {actual['sha256']}"
        )


def download_file(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(f".{destination.name}.partial-{os.getpid()}")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=120) as response, partial.open("wb") as out:
            while True:
                chunk = response.read(CHUNK_SIZE)
                if not chunk:
                    break
                out.write(chunk)
        if partial.stat().st_size == 0:
            raise InputError(f"downloaded empty response from {url}")
        os.replace(partial, destination)
    except Exception:
        if partial.exists():
            partial.unlink()
        raise


def safe_extract_spatial(archive: Path, input_root: Path, required_assets: list[str]) -> None:
    spatial_dir = input_root / "spatial"
    if spatial_dir.is_dir():
        missing = [item for item in required_assets if not (input_root / item).is_file()]
        if missing:
            raise InputError(
                "existing spatial directory is incomplete; refusing to merge or overwrite: "
                + ", ".join(missing)
            )
        return

    staging = Path(tempfile.mkdtemp(prefix=".spatial-extract-", dir=input_root))
    promoted = False
    try:
        with tarfile.open(archive, mode="r:gz") as bundle:
            members = bundle.getmembers()
            if not members:
                raise InputError(f"spatial archive is empty: {archive}")
            for member in members:
                pure = PurePosixPath(member.name)
                if pure.is_absolute() or ".." in pure.parts or not pure.parts:
                    raise InputError(f"unsafe archive member path: {member.name}")
                if member.issym() or member.islnk() or member.isdev():
                    raise InputError(f"unsupported archive member type: {member.name}")
                target = staging.joinpath(*pure.parts)
                target.resolve().relative_to(staging.resolve())
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                source = bundle.extractfile(member)
                if source is None:
                    raise InputError(f"cannot read archive member: {member.name}")
                with source, target.open("wb") as out:
                    shutil.copyfileobj(source, out, length=CHUNK_SIZE)

        candidates = sorted(
            {path.parent for path in staging.rglob("scalefactors_json.json")},
            key=lambda path: str(path).lower(),
        )
        valid = []
        for candidate in candidates:
            names = {item.name for item in candidate.iterdir() if item.is_file()}
            has_positions = bool({"tissue_positions_list.csv", "tissue_positions.csv"} & names)
            if {
                "scalefactors_json.json",
                "tissue_hires_image.png",
                "tissue_lowres_image.png",
            }.issubset(names) and has_positions:
                valid.append(candidate)
        if len(valid) != 1:
            raise InputError(
                f"expected exactly one complete spatial asset directory, found {len(valid)}"
            )
        os.replace(valid[0], spatial_dir)
        promoted = True
        missing = [item for item in required_assets if not (input_root / item).is_file()]
        if missing:
            raise InputError("promoted spatial directory is incomplete: " + ", ".join(missing))
    finally:
        if staging.exists():
            shutil.rmtree(staging)
        if not promoted and spatial_dir.exists():
            # Promotion is atomic; this branch only protects against a post-promotion
            # validation failure and never touches pre-existing user data.
            shutil.rmtree(spatial_dir)


def source_files_by_id(manifest: dict) -> dict[str, dict]:
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise InputError("input manifest must contain a non-empty files array")
    by_id: dict[str, dict] = {}
    for record in files:
        file_id = record.get("file_id")
        filename = record.get("filename")
        url = record.get("url")
        if not all(isinstance(value, str) and value for value in (file_id, filename, url)):
            raise InputError("each file requires non-empty file_id, filename, and url")
        if Path(filename).name != filename:
            raise InputError(f"filename must be a basename: {filename}")
        expected_size = record.get("expected_size_bytes")
        expected_hash = record.get("expected_sha256")
        if not isinstance(expected_size, int) or isinstance(expected_size, bool) or expected_size <= 0:
            raise InputError(
                f"{file_id} requires a positive integer expected_size_bytes; "
                "first-learning/null input freezes are prohibited"
            )
        if not isinstance(expected_hash, str) or not SHA256_PATTERN.fullmatch(expected_hash):
            raise InputError(
                f"{file_id} requires an exact lowercase SHA-256; "
                "first-learning/null input freezes are prohibited"
            )
        if record.get("freeze_policy") != EXACT_FREEZE_POLICY:
            raise InputError(
                f"{file_id} must use freeze_policy={EXACT_FREEZE_POLICY}"
            )
        if file_id in by_id:
            raise InputError(f"duplicate file_id: {file_id}")
        by_id[file_id] = record
    return by_id


def extracted_file_records(input_root: Path) -> list[dict]:
    spatial_dir = input_root / "spatial"
    if not spatial_dir.is_dir():
        raise InputError("extracted spatial directory is missing")
    records = []
    for path in sorted((item for item in spatial_dir.rglob("*") if item.is_file()), key=lambda item: item.as_posix().lower()):
        relative = path.relative_to(input_root).as_posix()
        records.append({"path": relative, **file_record(path)})
    if not records:
        raise InputError("extracted spatial directory contains no files")
    return records


def verify_extracted_lock(lock: dict, input_root: Path) -> None:
    expected = lock.get("extracted_files")
    if not isinstance(expected, list) or not expected:
        raise InputError("resolved manifest does not freeze extracted spatial assets")
    actual = extracted_file_records(input_root)
    expected_by_path = {record.get("path"): record for record in expected}
    actual_by_path = {record["path"]: record for record in actual}
    if None in expected_by_path or set(expected_by_path) != set(actual_by_path):
        raise InputError("extracted spatial asset inventory differs from the frozen manifest")
    for path, record in expected_by_path.items():
        validate_expected(record, actual_by_path[path], f"extracted asset {path}")


def verify_locked(
    manifest: dict, lock: dict, input_root: Path, *, require_extracted: bool = True
) -> list[dict]:
    source_by_id = source_files_by_id(manifest)
    if lock.get("dataset_id") != manifest.get("dataset_id"):
        raise InputError("resolved manifest dataset_id does not match source manifest")
    if lock.get("freeze_policy") != RESOLVED_FREEZE_POLICY:
        raise InputError(
            f"resolved manifest must use freeze_policy={RESOLVED_FREEZE_POLICY}"
        )
    locked_files = lock.get("files")
    if not isinstance(locked_files, list) or not locked_files:
        raise InputError("resolved manifest has no frozen files")
    locked_ids = [record.get("file_id") for record in locked_files]
    if len(locked_ids) != len(set(locked_ids)) or set(locked_ids) != set(source_by_id):
        raise InputError("resolved manifest file inventory differs from the exact source manifest")
    verified = []
    for record in locked_files:
        file_id = record.get("file_id")
        if file_id not in source_by_id:
            raise InputError(f"resolved manifest contains unknown file_id: {file_id}")
        source = source_by_id[file_id]
        if record.get("url") != source.get("url") or record.get("filename") != source.get("filename"):
            raise InputError(f"source identity changed for frozen file: {file_id}")
        if (
            record.get("size_bytes") != source.get("expected_size_bytes")
            or record.get("sha256") != source.get("expected_sha256")
        ):
            raise InputError(
                f"resolved size/hash differs from the exact source manifest: {file_id}"
            )
        path = input_root / record["filename"]
        actual = file_record(path)
        validate_expected(record, actual, file_id)
        verified.append({**record, **actual, "verified": True})
    if require_extracted:
        missing = [
            item
            for item in manifest.get("required_extracted_assets", [])
            if not (input_root / item).is_file()
        ]
        if missing:
            raise InputError("required extracted assets are missing: " + ", ".join(missing))
        verify_extracted_lock(lock, input_root)
    return verified


def fetch(manifest_path: Path, input_root: Path, resolved_path: Path) -> dict:
    manifest = read_json(manifest_path)
    source_by_id = source_files_by_id(manifest)
    input_root.mkdir(parents=True, exist_ok=True)
    if resolved_path.exists():
        lock = read_json(resolved_path)
        verified = verify_locked(manifest, lock, input_root, require_extracted=False)
    else:
        verified = []
        for file_id in sorted(source_by_id):
            record = source_by_id[file_id]
            destination = input_root / record["filename"]
            if not destination.exists():
                download_file(record["url"], destination)
            actual = file_record(destination)
            validate_expected(record, actual, file_id)
            verified.append(
                {
                    "file_id": file_id,
                    "role": record.get("role"),
                    "url": record["url"],
                    "filename": record["filename"],
                    **actual,
                    "license_spdx": manifest.get("license", {}).get("spdx"),
                    "verified": True,
                }
            )
        lock = {
            "schema_version": "1.0",
            "dataset_id": manifest.get("dataset_id"),
            "sample_id": manifest.get("sample_id"),
            "freeze_policy": RESOLVED_FREEZE_POLICY,
            "files": sorted(verified, key=lambda item: item["file_id"]),
        }
        write_json_atomic(resolved_path, lock)

    archive_record = next(
        (record for record in verified if record.get("file_id") == "spatial_archive"), None
    )
    if archive_record is None:
        raise InputError("resolved inputs do not contain spatial_archive")
    safe_extract_spatial(
        input_root / archive_record["filename"],
        input_root,
        list(manifest.get("required_extracted_assets", [])),
    )
    final_lock = read_json(resolved_path)
    if final_lock.get("extracted_files"):
        verify_extracted_lock(final_lock, input_root)
    else:
        final_lock["extracted_files"] = extracted_file_records(input_root)
        write_json_atomic(resolved_path, final_lock)
    final_verified = verify_locked(manifest, final_lock, input_root, require_extracted=True)
    return {
        "status": "verified",
        "dataset_id": manifest.get("dataset_id"),
        "files": final_verified,
        "resolved_manifest": str(resolved_path),
    }


def verify(manifest_path: Path, input_root: Path, resolved_path: Path) -> dict:
    if not resolved_path.is_file():
        raise InputError(f"resolved input manifest does not exist: {resolved_path}")
    manifest = read_json(manifest_path)
    lock = read_json(resolved_path)
    files = verify_locked(manifest, lock, input_root, require_extracted=True)
    return {"status": "verified", "dataset_id": manifest.get("dataset_id"), "files": files}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("fetch", "verify"):
        sub = subparsers.add_parser(command)
        sub.add_argument("--manifest", type=Path, required=True)
        sub.add_argument("--input-root", type=Path, required=True)
        sub.add_argument("--resolved-manifest", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest = args.manifest.resolve(strict=True)
        input_root = args.input_root.resolve()
        resolved = args.resolved_manifest.resolve()
        if args.command == "fetch":
            result = fetch(manifest, input_root, resolved)
        else:
            result = verify(manifest, input_root, resolved)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (InputError, OSError, tarfile.TarError, urllib.error.URLError) as exc:
        print(f"INPUT_ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
