#!/usr/bin/env python3
"""Build a sanitized, clone-portable core without private corpus payloads.

The exporter deliberately omits local source configuration and every derived record
that contains article text, code, images, hashes, or private absolute locators.  It
copies the executable engine, schemas, documentation, and tests, substitutes an
example source configuration, scans the staged tree, and then creates a ZIP archive.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Iterable


SKILL_ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_PARTS = {
    "private-corpus-index",
    "__pycache__",
    ".pytest_cache",
    ".git",
    ".cache",
    ".task-skills",
    ".renv",
    "renv",
    "runs",
    "work",
}
EXCLUDED_RELATIVE = {Path("references/corpus-sources.json")}
EXAMPLE_CONFIG = Path("references/corpus-sources.example.json")
TEXT_SUFFIXES = {".md", ".py", ".R", ".json", ".jsonl", ".yaml", ".yml", ".txt"}
WINDOWS_ABSOLUTE = re.compile(r"(?i)(?:[A-Z]:\\(?:Users|Documents and Settings)\\[^\s\"']+)")


class DistributionError(RuntimeError):
    """Raised when a public-core package would disclose private material."""


def _iter_source_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().casefold()):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if relative in EXCLUDED_RELATIVE or any(part in EXCLUDED_PARTS for part in relative.parts):
            continue
        yield path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _scan_text(path: Path, forbidden_literals: Iterable[str]) -> list[str]:
    if path.suffix.casefold() not in TEXT_SUFFIXES:
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        return [f"non_utf8_text:{path}:{exc.start}"]
    findings = [f"forbidden_literal:{path}:{literal}" for literal in forbidden_literals if literal and literal in text]
    if WINDOWS_ABSOLUTE.search(text):
        findings.append(f"private_home_path:{path}")
    return findings


def build_public_core(skill_root: Path, output_zip: Path, forbidden_literals: Iterable[str]) -> dict[str, object]:
    skill_root = skill_root.resolve()
    output_zip = output_zip.resolve()
    if not (skill_root / "SKILL.md").is_file():
        raise DistributionError(f"Not a Skill root: {skill_root}")
    if output_zip.exists():
        raise DistributionError(f"Refusing to overwrite existing output: {output_zip}")
    output_zip.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="biomedical-public-core-") as temporary:
        stage = Path(temporary) / "biomedical-analysis-agent"
        copied: list[Path] = []
        for source in _iter_source_files(skill_root):
            relative = source.relative_to(skill_root)
            target = stage / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copied.append(target)

        example = stage / EXAMPLE_CONFIG
        if not example.is_file():
            raise DistributionError("The sanitized corpus source example is missing.")
        shutil.copy2(example, stage / "references" / "corpus-sources.json")
        copied.append(stage / "references" / "corpus-sources.json")

        findings: list[str] = []
        for path in copied:
            findings.extend(_scan_text(path, forbidden_literals))
        if findings:
            raise DistributionError("Public-core leakage scan failed:\n" + "\n".join(findings))

        manifest_entries = []
        for path in sorted(set(copied), key=lambda item: item.relative_to(stage).as_posix()):
            manifest_entries.append(
                {
                    "path": path.relative_to(stage).as_posix(),
                    "size": path.stat().st_size,
                    "sha256": _sha256(path),
                }
            )
        distribution_manifest = {
            "schema_version": "1.0",
            "package": "biomedical-analysis-agent-public-core",
            "private_corpus_included": False,
            "source_configuration": "sanitized-example-only",
            "file_count": len(manifest_entries),
            "files": manifest_entries,
        }
        manifest_path = stage / "DISTRIBUTION_MANIFEST.json"
        manifest_path.write_text(
            json.dumps(distribution_manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        with zipfile.ZipFile(output_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for path in sorted(stage.rglob("*"), key=lambda item: item.as_posix().casefold()):
                if path.is_file():
                    archive.write(path, Path(stage.name) / path.relative_to(stage))

    return {
        "ok": True,
        "output": str(output_zip),
        "file_count": distribution_manifest["file_count"] + 1,
        "sha256": _sha256(output_zip),
        "private_corpus_included": False,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skill-root", type=Path, default=SKILL_ROOT)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--forbid",
        action="append",
        default=[],
        help="Additional literal that must not occur in the staged public package.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = build_public_core(args.skill_root, args.output, args.forbid)
    except (OSError, DistributionError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
