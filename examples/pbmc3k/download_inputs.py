#!/usr/bin/env python3
"""Download and safely extract the pinned PBMC3K teaching input.

This helper uses only the Python standard library.  It never accepts an arbitrary
URL from the command line: the URL, byte length, checksum and member root all
come from the reviewed input manifest shipped with this case.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


CHUNK_SIZE = 1024 * 1024
REQUIRED_MEX_FILES = ("matrix.mtx", "genes.tsv", "barcodes.tsv")


class InputError(RuntimeError):
    """Raised when a pinned input cannot be verified safely."""


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise InputError(f"expected a JSON object: {path}")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _pinned_input(manifest_path: Path) -> dict[str, Any]:
    manifest = _read_json(manifest_path)
    inputs = manifest.get("inputs")
    if not isinstance(inputs, list) or len(inputs) != 1 or not isinstance(inputs[0], dict):
        raise InputError("PBMC3K manifest must declare exactly one input")
    item = inputs[0]
    required = {
        "url",
        "archive_name",
        "content_length_bytes",
        "sha256",
        "expected_member_root",
        "license",
    }
    missing = sorted(required - set(item))
    if missing:
        raise InputError(f"input manifest is missing: {', '.join(missing)}")
    if item["license"] != "CC BY 4.0":
        raise InputError("unexpected data license")
    return item


def verify_archive(path: Path, item: dict[str, Any]) -> dict[str, Any]:
    if not path.is_file():
        raise InputError(f"archive not found: {path}")
    actual_size = path.stat().st_size
    expected_size = int(item["content_length_bytes"])
    if actual_size != expected_size:
        raise InputError(f"size mismatch: expected {expected_size}, observed {actual_size}")
    actual_hash = _sha256(path)
    expected_hash = str(item["sha256"]).lower()
    if actual_hash != expected_hash:
        raise InputError(f"SHA-256 mismatch: expected {expected_hash}, observed {actual_hash}")
    return {"path": str(path.resolve()), "size_bytes": actual_size, "sha256": actual_hash}


def download_archive(manifest_path: Path, cache_root: Path) -> tuple[Path, dict[str, Any]]:
    item = _pinned_input(manifest_path)
    cache_root.mkdir(parents=True, exist_ok=True)
    archive_path = cache_root / str(item["archive_name"])
    if archive_path.exists():
        return archive_path, verify_archive(archive_path, item)

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{archive_path.name}.", suffix=".partial", dir=cache_root
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        request = urllib.request.Request(
            str(item["url"]), headers={"User-Agent": "biomedical-analysis-agent-pbmc3k/1.0"}
        )
        with urllib.request.urlopen(request, timeout=120) as response, temporary_path.open("wb") as output:
            shutil.copyfileobj(response, output, length=CHUNK_SIZE)
        evidence = verify_archive(temporary_path, item)
        os.replace(temporary_path, archive_path)
        evidence["path"] = str(archive_path.resolve())
        return archive_path, evidence
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def _validate_members(archive: tarfile.TarFile, destination: Path) -> None:
    root = destination.resolve()
    for member in archive.getmembers():
        if member.issym() or member.islnk() or member.isdev():
            raise InputError(f"unsafe archive member type: {member.name}")
        target = (destination / member.name).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise InputError(f"unsafe archive member path: {member.name}") from exc


def _complete_extract(extract_root: Path, item: dict[str, Any]) -> Path | None:
    marker = extract_root / ".input.complete.json"
    data_root = extract_root / Path(str(item["expected_member_root"]))
    if not marker.is_file() or not data_root.is_dir():
        return None
    try:
        evidence = _read_json(marker)
    except (OSError, json.JSONDecodeError, InputError):
        return None
    if evidence.get("archive_sha256") != str(item["sha256"]).lower():
        return None
    if not all((data_root / name).is_file() for name in REQUIRED_MEX_FILES):
        return None
    return data_root


def extract_archive(archive_path: Path, manifest_path: Path, cache_root: Path) -> Path:
    item = _pinned_input(manifest_path)
    extract_root = cache_root / "pbmc3k_filtered_gene_bc_matrices"
    complete = _complete_extract(extract_root, item)
    if complete is not None:
        return complete
    if extract_root.exists():
        raise InputError(
            f"existing extraction is incomplete or has the wrong signature: {extract_root}; "
            "quarantine it or choose a new cache root"
        )

    staging = Path(tempfile.mkdtemp(prefix=".pbmc3k-extract-", dir=cache_root))
    try:
        with tarfile.open(archive_path, mode="r:gz") as archive:
            _validate_members(archive, staging)
            archive.extractall(staging)
        data_root = staging / Path(str(item["expected_member_root"]))
        missing = [name for name in REQUIRED_MEX_FILES if not (data_root / name).is_file()]
        if missing:
            raise InputError(f"archive is missing required MEX files: {', '.join(missing)}")
        marker = {
            "schema_version": "1.0.0",
            "archive_sha256": str(item["sha256"]).lower(),
            "archive_size_bytes": int(item["content_length_bytes"]),
            "member_root": str(item["expected_member_root"]),
            "required_files": list(REQUIRED_MEX_FILES),
        }
        with (staging / ".input.complete.json").open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(marker, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(staging, extract_root)
        return extract_root / Path(str(item["expected_member_root"]))
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def prepare_input(manifest_path: Path, cache_root: Path) -> dict[str, Any]:
    manifest_path = manifest_path.resolve()
    cache_root = cache_root.resolve()
    archive_path, archive_evidence = download_archive(manifest_path, cache_root)
    data_root = extract_archive(archive_path, manifest_path, cache_root)
    return {
        "schema_version": "1.0.0",
        "archive": archive_evidence,
        "data_root": str(data_root.resolve()),
        "cache_root": str(cache_root),
        "license": "CC BY 4.0",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path(__file__).with_name("input_manifest.json"))
    parser.add_argument("--cache-root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        evidence = prepare_input(args.manifest, args.cache_root)
        rendered = json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            temporary = args.output.with_suffix(args.output.suffix + ".tmp")
            temporary.write_text(rendered, encoding="utf-8", newline="\n")
            os.replace(temporary, args.output)
        print(rendered, end="")
        return 0
    except (InputError, OSError, tarfile.TarError, urllib.error.URLError) as exc:
        print(f"INPUT_PREPARATION_FAILED: {exc}", file=__import__("sys").stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
