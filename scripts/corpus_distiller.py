#!/usr/bin/env python3
"""Build a private, provenance-complete index over local bioinformatics sources.

The source trees are never modified. The index preserves source order and hashes,
and deliberately separates static extraction from executable verification.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import difflib
import hashlib
import json
import mimetypes
import os
import re
import shutil
import struct
import sys
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence


SKILL_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = SKILL_ROOT / "references" / "corpus-sources.json"
DEFAULT_OUTPUT = SKILL_ROOT / "assets" / "private-corpus-index"
CODE_SUFFIXES = {
    ".r": "r",
    ".rmd": "r",
    ".qmd": "mixed",
    ".py": "python",
    ".ipynb": "python",
    ".sh": "shell",
    ".bash": "shell",
    ".ps1": "powershell",
    ".jl": "julia",
    ".sql": "sql",
}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".tif", ".tiff", ".svg"}
TEXT_SUFFIXES = CODE_SUFFIXES.keys() | {".md", ".txt", ".json", ".yaml", ".yml", ".csv", ".tsv"}
IGNORE_DIRS = {".git", "__pycache__", ".pytest_cache", ".mypy_cache"}
MATURITY = "raw-extracted"
PREPROCESS_IDENTITY_FIELDS = ("source_root_id", "canonical_locator")
LEGACY_PREPROCESS_IDENTITY_FIELDS = ("来源文件夹", "清单文件", "记录标题", "路径")
PREPROCESS_DISTILLED_FIELDS = (
    "一级分类",
    "能力标签",
    "数据类型",
    "代码资产完整度",
    "蒸馏价值",
    "内容要点",
    "_理由",
    "_tags",
    "_r",
    "_py",
    "_plots",
)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def json_dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    count = 0
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            count += 1
    os.replace(tmp, path)
    return count


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def read_text(path: Path) -> tuple[str, str]:
    data = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace"), "utf-8-replace"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_id(prefix: str, *parts: str) -> str:
    normalized = "\x1f".join(parts).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(normalized).hexdigest()[:20]}"


def natural_key(value: str) -> list[Any]:
    return [int(token) if token.isdigit() else token.casefold() for token in re.split(r"(\d+)", value)]


def posix_relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def image_dimensions(path: Path) -> tuple[int | None, int | None]:
    try:
        with path.open("rb") as handle:
            head = handle.read(32)
            if head.startswith(b"\x89PNG\r\n\x1a\n") and len(head) >= 24:
                return struct.unpack(">II", head[16:24])
            if head[:2] == b"\xff\xd8":
                handle.seek(2)
                while True:
                    marker = handle.read(1)
                    if not marker:
                        break
                    if marker != b"\xff":
                        continue
                    code = handle.read(1)
                    while code == b"\xff":
                        code = handle.read(1)
                    if code in {bytes([x]) for x in range(0xC0, 0xC4)} | {bytes([x]) for x in range(0xC5, 0xC8)} | {bytes([x]) for x in range(0xC9, 0xCC)} | {bytes([x]) for x in range(0xCD, 0xD0)}:
                        length = int.from_bytes(handle.read(2), "big")
                        payload = handle.read(length - 2)
                        if len(payload) >= 5:
                            return int.from_bytes(payload[3:5], "big"), int.from_bytes(payload[1:3], "big")
                        break
                    length_bytes = handle.read(2)
                    if len(length_bytes) != 2:
                        break
                    handle.seek(max(0, int.from_bytes(length_bytes, "big") - 2), 1)
    except OSError:
        pass
    return None, None


def language_for(path: Path, text: str = "") -> str:
    declared = CODE_SUFFIXES.get(path.suffix.casefold())
    if declared and declared != "mixed":
        return declared
    sample = text[:4000]
    if re.search(r"(?m)^\s*(library|require)\s*\(", sample) or "<-" in sample:
        return "r"
    if re.search(r"(?m)^\s*(from\s+\S+\s+import|import\s+\S+)", sample):
        return "python"
    if re.search(r"(?m)^\s*(#!/.*\b(?:bash|sh)\b|set\s+-[a-z]*e)", sample):
        return "shell"
    return declared or "unknown"


def extract_packages(text: str, language: str) -> tuple[list[str], list[dict[str, str]]]:
    packages: set[str] = set()
    functions: list[dict[str, str]] = []
    if language in {"r", "mixed", "unknown"}:
        packages.update(re.findall(r"\b(?:library|require)\s*\(\s*[\"']?([A-Za-z][A-Za-z0-9._]+)", text))
        for package, function in re.findall(r"\b([A-Za-z][A-Za-z0-9._]+):::{0,1}([A-Za-z][A-Za-z0-9._]+)", text):
            packages.add(package)
            functions.append({"package": package, "function": function})
    if language in {"python", "mixed", "unknown"}:
        for match in re.finditer(r"(?m)^\s*import\s+([A-Za-z_][\w.]*)|^\s*from\s+([A-Za-z_][\w.]*)\s+import", text):
            package = (match.group(1) or match.group(2)).split(".")[0]
            packages.add(package)
    return sorted(packages, key=str.casefold), sorted(functions, key=lambda item: (item["package"].casefold(), item["function"].casefold()))


def static_code_facts(text: str, language: str) -> dict[str, Any]:
    assignments: list[str] = []
    if language == "r":
        assignments = re.findall(r"(?m)^\s*([A-Za-z.][\w.]*)\s*(?:<-|=(?!=))", text)
    elif language == "python":
        assignments = re.findall(r"(?m)^\s*([A-Za-z_][\w]*)\s*=(?!=)", text)
    absolute_paths = sorted(set(re.findall(r"(?:[A-Za-z]:[\\/][^\"'\s,)]+|/(?:home|Users|data|mnt)/[^\"'\s,)]+)", text)))
    installers = sorted(set(re.findall(r"(?i)(?:install\.packages|BiocManager::install|pak::pkg_install|remotes::install_github|pip\s+install|conda\s+install)", text)))
    downloads = sorted(set(re.findall(r"(?i)(?:download\.file|requests\.get|urlretrieve|wget\s|curl\s|GEOquery|getGEO)", text)))
    inputs = sorted(set(re.findall(r"(?i)\b(?:read\.[A-Za-z0-9_]+|read_[A-Za-z0-9_]+|read[A-Z][A-Za-z0-9_]*|Load10X_[A-Za-z0-9_]+)\s*\(", text)))
    outputs = sorted(set(re.findall(r"(?i)\b(?:write\.[A-Za-z0-9_]+|write_[A-Za-z0-9_]+|saveRDS|save|ggsave|savefig|write_h5ad)\s*\(", text)))
    return {
        "assignments": sorted(set(assignments), key=str.casefold)[:500],
        "absolute_paths": absolute_paths[:100],
        "installer_calls": installers,
        "download_calls": downloads,
        "input_calls": inputs,
        "output_calls": outputs,
        "analysis_level": "static_approximation",
    }


FENCE_RE = re.compile(r"```\s*([^\n`]*)\n(.*?)```", re.DOTALL)
IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")


def markdown_blocks(text: str) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for ordinal, match in enumerate(FENCE_RE.finditer(text), start=1):
        declared = match.group(1).strip().split()[0].casefold() if match.group(1).strip() else "unknown"
        code = match.group(2).replace("\r\n", "\n")
        language = {"rscript": "r", "python3": "python", "bash": "shell", "sh": "shell"}.get(declared, declared)
        if language not in {"r", "python", "shell", "powershell", "julia", "sql"}:
            language = language_for(Path("snippet.txt"), code)
        packages, functions = extract_packages(code, language)
        blocks.append({
            "ordinal": ordinal,
            "declared_language": declared,
            "normalized_language": language,
            "sha256": hashlib.sha256(code.encode("utf-8")).hexdigest(),
            "text": code,
            "packages": packages,
            "package_functions": functions,
            "static_facts": static_code_facts(code, language),
            "source_span": [text.count("\n", 0, match.start()) + 1, text.count("\n", 0, match.end()) + 1],
        })
    return blocks


def markdown_images(text: str) -> list[dict[str, Any]]:
    return [
        {
            "ordinal": ordinal,
            "alt": match.group(1),
            "target": match.group(2),
            "source_line": text.count("\n", 0, match.start()) + 1,
        }
        for ordinal, match in enumerate(IMAGE_RE.finditer(text), start=1)
    ]


def clean_title(value: str) -> str:
    value = re.sub(r"^\s*\d+[_.、-]*", "", value)
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", value).casefold()


def normalize_identity_text(value: Any) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", str(value or "")).strip())


def normalize_windows_path(value: Any) -> str:
    text = unicodedata.normalize("NFC", str(value or "")).strip().replace("/", "\\")
    text = re.sub(r"\\+", r"\\", text)
    return text.rstrip("\\").casefold()


def canonical_relative_locator(record: dict[str, Any], root: SourceRoot) -> str:
    raw_path = unicodedata.normalize("NFC", str(record.get("路径") or "")).strip().replace("/", "\\")
    raw_path = re.sub(r"\\+", r"\\", raw_path).rstrip("\\")
    root_path = unicodedata.normalize("NFC", str(root.path)).strip().replace("/", "\\")
    root_path = re.sub(r"\\+", r"\\", root_path).rstrip("\\")
    if raw_path.casefold() == root_path.casefold():
        relative = "."
    elif raw_path.casefold().startswith(root_path.casefold() + "\\"):
        relative = raw_path[len(root_path):].lstrip("\\")
    else:
        raise ValueError(f"Preprocessing path is outside configured source root {root.id}: {raw_path}")
    components = [part for part in relative.replace("\\", "/").split("/") if part not in {"", "."}]
    if any(part == ".." for part in components):
        raise ValueError(f"Preprocessing locator escapes source root {root.id}: {relative}")
    return "/".join(components).casefold() if components else "."


def preprocessing_record_identity(record: dict[str, Any], root: SourceRoot) -> dict[str, str]:
    return {
        "source_root_id": root.id,
        "canonical_locator": canonical_relative_locator(record, root),
    }


def legacy_preprocessing_identity(record: dict[str, Any]) -> dict[str, str]:
    identity: dict[str, str] = {}
    for field in LEGACY_PREPROCESS_IDENTITY_FIELDS:
        value = record.get(field, "")
        identity[field] = normalize_windows_path(value) if field == "路径" else normalize_identity_text(value)
    return identity


def preprocessing_record_id(record: dict[str, Any], root: SourceRoot) -> str:
    identity = preprocessing_record_identity(record, root)
    payload = "preprocessing-record-v2\0" + identity["source_root_id"] + "\0" + identity["canonical_locator"]
    return "prep-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def preprocessing_record_tiebreak_id(record: dict[str, Any]) -> str:
    identity = legacy_preprocessing_identity(record)
    return stable_id("preprocess-tie", *(identity[field] for field in LEGACY_PREPROCESS_IDENTITY_FIELDS))


def preprocessing_record_sha256(record: dict[str, Any]) -> str:
    payload = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_preprocessing_registry(records: Sequence[dict[str, Any]], roots: Sequence[SourceRoot]) -> list[dict[str, Any]]:
    registry: list[dict[str, Any]] = []
    seen: dict[str, dict[str, str]] = {}
    root_by_name = {root.name: root for root in roots}
    for ordinal, record in enumerate(records, start=1):
        source_name = str(record.get("来源文件夹") or "")
        root = root_by_name.get(source_name)
        if root is None:
            raise ValueError(f"Unknown preprocessing source root: {source_name}")
        record_id = preprocessing_record_id(record, root)
        identity = preprocessing_record_identity(record, root)
        if record_id in seen:
            raise ValueError(f"Duplicate preprocessing identity for {record_id}: {identity}")
        seen[record_id] = identity
        registry.append(
            {
                "schema_version": "1.0",
                "preprocess_record_id": record_id,
                "ordinal": ordinal,
                "identity": identity,
                "legacy_identity": legacy_preprocessing_identity(record),
                "record_sha256": preprocessing_record_sha256(record),
                "distillation_value": str(record.get("蒸馏价值") or "unknown"),
                "record": record,
                "source_mode": "read_only",
                "distribution": "private_local_only",
                "maturity": MATURITY,
            }
        )
    return registry


def record_match_details(
    source_name: str,
    bundle_title: str,
    bundle_path: Path,
    records: Sequence[dict[str, Any]],
) -> tuple[dict[str, Any] | None, float, str]:
    candidates = [row for row in records if str(row.get("来源文件夹", "")) == source_name]
    if not candidates:
        return None, 0.0, "no_source_candidates"
    title_key = clean_title(bundle_title)
    bundle_path_key = normalize_windows_path(bundle_path)
    ranked: list[tuple[float, int, str, str, dict[str, Any]]] = []
    for row in candidates:
        path_value = str(row.get("路径", ""))
        record_title = str(row.get("记录标题", ""))
        record_title_key = clean_title(record_title)
        path_title_key = clean_title(Path(path_value).name)
        title_ratio = difflib.SequenceMatcher(None, title_key, record_title_key).ratio()
        path_ratio = difflib.SequenceMatcher(None, title_key, path_title_key).ratio()
        comparison = max(title_ratio, path_ratio)
        method = "fuzzy_title"
        priority = 1
        if normalize_windows_path(path_value) and bundle_path_key == normalize_windows_path(path_value):
            comparison, method, priority = 1.0, "exact_path", 4
        elif title_key and title_key == record_title_key:
            comparison, method, priority = 1.0, "exact_record_title", 3
        elif title_key and title_key == path_title_key:
            comparison, method, priority = 1.0, "exact_path_title", 2
        ranked.append((comparison, priority, preprocessing_record_tiebreak_id(row), method, row))
    ranked.sort(key=lambda item: (-item[0], -item[1], item[2]))
    comparison, _, _, method, row = ranked[0]
    return (row, comparison, method) if comparison >= 0.55 else (None, comparison, "below_threshold")


def record_match(source_name: str, bundle_title: str, bundle_path: Path, records: Sequence[dict[str, Any]]) -> tuple[dict[str, Any] | None, float]:
    row, score, _ = record_match_details(source_name, bundle_title, bundle_path, records)
    return row, score


@dataclass(frozen=True)
class SourceRoot:
    id: str
    name: str
    path: Path
    extra: dict[str, Any]


def load_config(path: Path) -> tuple[dict[str, Any], list[SourceRoot], list[dict[str, Any]]]:
    config = read_json(path)
    roots: list[SourceRoot] = []
    for item in config["source_roots"]:
        root_path = Path(item["path"])
        roots.append(SourceRoot(item["id"], item["name"], root_path, {k: v for k, v in item.items() if k not in {"id", "name", "path"}}))
    records_path = Path(config["preprocessing_records"])
    records = read_json(records_path) if records_path.exists() else []
    return config, roots, records


def iter_files(root: Path) -> Iterator[Path]:
    for current, dirs, files in os.walk(root):
        dirs[:] = sorted([name for name in dirs if name not in IGNORE_DIRS], key=natural_key)
        for name in sorted(files, key=natural_key):
            yield Path(current) / name


def scan_files(
    roots: Sequence[SourceRoot],
    cached_lookup: dict[tuple[str, str], dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]], int]:
    rows: list[dict[str, Any]] = []
    by_sha: dict[str, list[dict[str, Any]]] = defaultdict(list)
    reused = 0
    cached_lookup = cached_lookup or {}
    for root in roots:
        if not root.path.is_dir():
            continue
        for path in iter_files(root.path):
            try:
                stat = path.stat()
                relative_path = posix_relative(path, root.path)
                cached = cached_lookup.get((root.id, relative_path))
                if cached and cached.get("size") == stat.st_size and cached.get("sha256"):
                    digest = str(cached["sha256"])
                    reused += 1
                else:
                    digest = sha256_file(path)
            except OSError as exc:
                rows.append({"source_root_id": root.id, "relative_path": posix_relative(path, root.path), "error": str(exc)})
                continue
            row = {
                "file_id": stable_id("file", root.id, relative_path),
                "source_root_id": root.id,
                "relative_path": relative_path,
                "private_absolute_path": str(path),
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "sha256": digest,
                "suffix": path.suffix.casefold(),
                "kind": "code" if path.suffix.casefold() in CODE_SUFFIXES else "image" if path.suffix.casefold() in IMAGE_SUFFIXES else "text" if path.suffix.casefold() in TEXT_SUFFIXES else "other",
                "source_mode": "read_only",
                "distribution_class": "private_local_only",
                "privacy_class": "private_source_locator",
                "license_status": "unverified_requires_review",
                "publish_allowed": False,
            }
            rows.append(row)
            by_sha[digest].append(row)
    return rows, by_sha, reused


def discover_bundle_dirs(root: SourceRoot) -> list[Path]:
    article_dirs: set[Path] = set()
    project_dirs: set[Path] = set()
    loose_code = False
    for current, dirs, files in os.walk(root.path):
        dirs[:] = [name for name in dirs if name not in IGNORE_DIRS]
        current_path = Path(current)
        lowered = {name.casefold() for name in files}
        if "article.md" in lowered or "metadata.json" in lowered:
            article_dirs.add(current_path)
            dirs[:] = []
            continue
        if current_path == root.path and any(Path(name).suffix.casefold() in CODE_SUFFIXES for name in files):
            loose_code = True
    if root.id == "single-cell-projects":
        for child in root.path.iterdir():
            contains_article_bundle = any(
                article_dir == child or child in article_dir.parents for article_dir in article_dirs
            )
            if child.is_dir() and child.name not in IGNORE_DIRS and not contains_article_bundle:
                if any(path.suffix.casefold() in CODE_SUFFIXES for path in iter_files(child)):
                    project_dirs.add(child)
    if loose_code:
        project_dirs.add(root.path)
    return sorted(article_dirs | project_dirs, key=lambda path: natural_key(posix_relative(path, root.path) if path != root.path else "."))


def file_row_lookup(file_rows: Sequence[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    return {(row.get("source_root_id", ""), row.get("relative_path", "")): row for row in file_rows if "sha256" in row}


def build_bundle(root: SourceRoot, bundle_dir: Path, file_lookup: dict[tuple[str, str], dict[str, Any]], records: Sequence[dict[str, Any]]) -> dict[str, Any]:
    rel_bundle = "." if bundle_dir == root.path else posix_relative(bundle_dir, root.path)
    metadata_path = bundle_dir / "metadata.json"
    article_path = bundle_dir / "article.md"
    metadata: dict[str, Any] = {}
    issues: list[dict[str, Any]] = []
    if metadata_path.exists():
        try:
            metadata = read_json(metadata_path)
        except (OSError, json.JSONDecodeError) as exc:
            issues.append({"severity": "major", "code": "metadata_parse_failed", "detail": str(exc)})
    article_text = ""
    article_encoding = None
    if article_path.exists():
        article_text, article_encoding = read_text(article_path)
    title = str(metadata.get("title") or (article_text.splitlines()[0].lstrip("# ") if article_text else bundle_dir.name))
    bundle_id = stable_id("flow", root.id, rel_bundle)
    # A root-level project bundle represents only loose top-level scripts.  Its
    # descendant article packs are indexed independently and must not be folded
    # into the parent a second time.
    candidate_files = list(bundle_dir.iterdir()) if bundle_dir == root.path else list(iter_files(bundle_dir))
    code_paths = sorted(
        [path for path in candidate_files if path.is_file() and path.suffix.casefold() in CODE_SUFFIXES and not any(parent.name in IGNORE_DIRS for parent in path.parents)],
        key=lambda path: natural_key(posix_relative(path, bundle_dir)),
    )
    code_files: list[dict[str, Any]] = []
    package_names: set[str] = set()
    package_functions: list[dict[str, str]] = []
    for ordinal, path in enumerate(code_paths, start=1):
        text, encoding = read_text(path)
        language = language_for(path, text)
        packages, functions = extract_packages(text, language)
        package_names.update(packages)
        package_functions.extend(functions)
        source_rel = posix_relative(path, root.path)
        inv = file_lookup.get((root.id, source_rel), {})
        code_files.append({
            "ordinal": ordinal,
            "relative_to_bundle": posix_relative(path, bundle_dir),
            "source_locator": str(path),
            "source_relative_path": source_rel,
            "sha256": inv.get("sha256") or sha256_file(path),
            "size": inv.get("size", path.stat().st_size),
            "encoding": encoding,
            "declared_language": CODE_SUFFIXES.get(path.suffix.casefold(), "unknown"),
            "normalized_language": language,
            "packages": packages,
            "package_functions": functions,
            "static_facts": static_code_facts(text, language),
        })
    fenced = markdown_blocks(article_text) if article_text else []
    for block in fenced:
        package_names.update(block["packages"])
        package_functions.extend(block["package_functions"])
    article_images = markdown_images(article_text) if article_text else []
    image_paths = sorted([path for path in candidate_files if path.is_file() and path.suffix.casefold() in IMAGE_SUFFIXES], key=lambda path: natural_key(posix_relative(path, bundle_dir)))
    image_by_rel = {posix_relative(path, bundle_dir): path for path in image_paths}
    images: list[dict[str, Any]] = []
    referenced_targets = {entry["target"].replace("\\", "/"): entry for entry in article_images}
    plot_hints: list[str] = []
    matched_record, match_score, match_method = record_match_details(root.name, title, bundle_dir, records)
    if matched_record:
        plot_hints = list(matched_record.get("_plots", []))
    for ordinal, path in enumerate(image_paths, start=1):
        rel = posix_relative(path, bundle_dir)
        source_rel = posix_relative(path, root.path)
        inv = file_lookup.get((root.id, source_rel), {})
        width, height = image_dimensions(path)
        explicit = referenced_targets.get(rel)
        images.append({
            "image_id": stable_id("image", bundle_id, rel),
            "ordinal": ordinal,
            "relative_to_bundle": rel,
            "source_locator": str(path),
            "sha256": inv.get("sha256") or sha256_file(path),
            "size": inv.get("size", path.stat().st_size),
            "mime_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            "width": width,
            "height": height,
            "article_reference": explicit,
            "link_confidence": "explicit_article_reference" if explicit else "unlinked",
            "plot_hints": plot_hints,
            "native_review_status": "not_reviewed",
        })
    expected_blocks = metadata.get("code_blocks")
    expected_images = metadata.get("images")
    if isinstance(expected_blocks, int) and expected_blocks != len(fenced):
        issues.append({"severity": "major", "code": "fenced_code_count_mismatch", "expected": expected_blocks, "observed": len(fenced)})
    if isinstance(expected_images, int) and expected_images != len(image_paths):
        issues.append({"severity": "major", "code": "image_count_mismatch", "expected": expected_images, "observed": len(image_paths)})
    if not code_files and not fenced:
        issues.append({"severity": "info", "code": "no_code"})
    if any(item["static_facts"]["absolute_paths"] for item in code_files) or any(item["static_facts"]["absolute_paths"] for item in fenced):
        issues.append({"severity": "major", "code": "hard_coded_paths"})
    if any(item["static_facts"]["installer_calls"] for item in code_files) or any(item["static_facts"]["installer_calls"] for item in fenced):
        issues.append({"severity": "info", "code": "installer_calls_present", "action": "remove_from_analysis_recipe"})
    flow_fingerprint_parts = [item["sha256"] for item in code_files] + [item["sha256"] for item in fenced] + [item["sha256"] for item in images]
    flow_fingerprint = hashlib.sha256("\n".join(flow_fingerprint_parts).encode("ascii")).hexdigest()
    preprocessed = None
    if matched_record:
        preprocessed = {key: matched_record.get(key) for key in PREPROCESS_DISTILLED_FIELDS}
        package_names.update(matched_record.get("_r", []))
        package_names.update(matched_record.get("_py", []))
    return {
        "schema_version": "1.0",
        "bundle_id": bundle_id,
        "title": title,
        "source_root_id": root.id,
        "source_root_name": root.name,
        "source_relative_directory": rel_bundle,
        "private_source_directory": str(bundle_dir),
        "source_mode": "read_only",
        "source_type": "wechat_article" if article_path.exists() else "project_code_bundle",
        "metadata": metadata,
        "article": {
            "source_locator": str(article_path) if article_path.exists() else None,
            "encoding": article_encoding,
            "sha256": sha256_file(article_path) if article_path.exists() else None,
            "fenced_code_blocks": fenced,
            "image_references": article_images,
        },
        "ordered_code_files": code_files,
        "images": images,
        "package_index": {
            "packages": sorted(package_names, key=str.casefold),
            "qualified_functions": sorted({(item["package"], item["function"]) for item in package_functions}, key=lambda item: (item[0].casefold(), item[1].casefold())),
        },
        "preprocessing_match": {
            "score": round(match_score, 4),
            "method": match_method,
            "record_id": preprocessing_record_id(matched_record, root) if matched_record else None,
            "record": preprocessed,
            "status": "matched" if preprocessed else "unmatched",
        },
        "flow_integrity": {
            "ordered": True,
            "reconstructable_from_sources": all(Path(item["source_locator"]).exists() for item in code_files),
            "raw_sources_immutable": True,
            "flow_fingerprint_sha256": flow_fingerprint,
        },
        "issues": issues,
        "maturity": MATURITY,
        "distribution": "private_local_only",
    }


def claim_ceiling(category: str) -> str:
    if category in {"分析思路与文献解读", "流程与规范"}:
        return "method_candidate_only_until_primary_literature_and_data_are_verified"
    if category in {"统计与机器学习", "bulk组学分析", "单细胞分析", "空间转录组"}:
        return "association_or_descriptive_result_only_until_design_and_replication_are_verified"
    return "descriptive_only_until_execution_and_scientific_review"


def build_method_cards(bundles: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    for bundle in bundles:
        record = bundle.get("preprocessing_match", {}).get("record")
        if not record:
            continue
        tags = list(record.get("_tags") or [])
        category = str(record.get("一级分类") or "unknown")
        cards.append({
            "schema_version": "1.0",
            "method_card_id": stable_id("method", bundle["bundle_id"]),
            "title": bundle["title"],
            "source_bundle_ids": [bundle["bundle_id"]],
            "research_question_hints": [str(record.get("内容要点") or "")],
            "data_types": [str(record.get("数据类型") or "unknown")],
            "category": category,
            "method_sequence": tags,
            "combination_logic": "metadata_order_candidate_requires_manual_methodology_review",
            "analysis_unit": "unknown",
            "assumptions": [],
            "required_validation": ["primary_method_review", "input_contract", "statistical_unit_review", "fixture_execution"],
            "alternatives": [],
            "limitations": ["Derived from local article inventory; primary literature and source data were not yet verified."],
            "claim_ceiling": claim_ceiling(category),
            "maturity": MATURITY,
            "review_status": "not_manually_reviewed",
        })
    return cards


def build_package_cards(bundles: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    sources: dict[tuple[str, str], set[str]] = defaultdict(set)
    functions: dict[tuple[str, str], set[str]] = defaultdict(set)
    tags: dict[tuple[str, str], set[str]] = defaultdict(set)
    for bundle in bundles:
        record = bundle.get("preprocessing_match", {}).get("record") or {}
        r_packages = set(record.get("_r") or [])
        py_packages = set(record.get("_py") or [])
        detected = set(bundle.get("package_index", {}).get("packages") or [])
        for package in sorted(r_packages | detected):
            if not package:
                continue
            language = "r" if package in r_packages else "unknown"
            sources[(language, package)].add(bundle["bundle_id"])
            tags[(language, package)].update(record.get("_tags") or [])
        for package in py_packages:
            sources[("python", package)].add(bundle["bundle_id"])
            tags[("python", package)].update(record.get("_tags") or [])
        for package, function in bundle.get("package_index", {}).get("qualified_functions") or []:
            functions[("r", package)].add(function)
    cards: list[dict[str, Any]] = []
    for (language, package), bundle_ids in sorted(sources.items(), key=lambda item: (item[0][0], item[0][1].casefold())):
        cards.append({
            "schema_version": "1.0",
            "package_card_id": stable_id("package", language, package),
            "package": package,
            "language": language,
            "version_constraint": "unknown",
            "canonical_source": "unknown_requires_review",
            "github_commit": None,
            "conda_mapping": None,
            "system_dependencies": [],
            "functions": sorted(functions.get((language, package), set()), key=str.casefold),
            "capability_hints": sorted(tags.get((language, package), set()), key=str.casefold),
            "source_bundle_ids": sorted(bundle_ids),
            "installation_policy": "environment_manager_only",
            "maturity": MATURITY,
        })
    return cards


def build_figure_cards(bundles: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    for bundle in bundles:
        for image in bundle.get("images", []):
            cards.append({
                "schema_version": "1.0",
                "figure_card_id": stable_id("figure", image["image_id"]),
                "source_bundle_id": bundle["bundle_id"],
                "source_image_id": image["image_id"],
                "private_source_locator": image["source_locator"],
                "sha256": image["sha256"],
                "dimensions": {"width": image["width"], "height": image["height"]},
                "plot_hints": image.get("plot_hints", []),
                "code_link": image.get("article_reference"),
                "evidence_level": "image_metadata" if image.get("article_reference") else "pixels_only_pending_review",
                "visible": [],
                "supports": [],
                "does_not_support": ["No scientific conclusion before native image, code and data review."],
                "visual_qa": "not_reviewed",
                "reproduction_class": "unknown",
                "maturity": MATURITY,
            })
    return cards


def normalize_capability(value: str) -> str:
    cleaned = re.sub(r"\([^)]*\)|（[^）]*）", "", value)
    cleaned = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "_", cleaned).strip("_").casefold()
    return cleaned or "unknown"


def build_capability_modules(bundles: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    labels: dict[str, Counter[str]] = defaultdict(Counter)
    bundle_signatures = {
        bundle["bundle_id"]: tuple(item.get("sha256") for item in bundle.get("ordered_code_files", []))
        or tuple(item.get("sha256") for item in bundle.get("article", {}).get("fenced_code_blocks", []))
        for bundle in bundles
    }
    for bundle in bundles:
        record = bundle.get("preprocessing_match", {}).get("record") or {}
        for plot in record.get("_plots") or []:
            key = normalize_capability(plot)
            labels[key][plot] += 1
            grouped[key].append({
                "variant_id": stable_id("variant", bundle["bundle_id"], plot),
                "source_bundle_id": bundle["bundle_id"],
                "source_plot_label": plot,
                "equivalence": "pending",
                "selection_status": "candidate_pending_semantic_review",
                "maturity": MATURITY,
            })
    modules: list[dict[str, Any]] = []
    for key, variants in sorted(grouped.items()):
        label = labels[key].most_common(1)[0][0]
        canonical = max(
            variants,
            key=lambda variant: (
                len(bundle_signatures.get(variant["source_bundle_id"], ())),
                bundle_signatures.get(variant["source_bundle_id"], ()),
                variant["variant_id"],
            ),
        )
        canonical_signature = bundle_signatures.get(canonical["source_bundle_id"], ())
        for variant in variants:
            signature = bundle_signatures.get(variant["source_bundle_id"], ())
            exact = variant["variant_id"] == canonical["variant_id"] or (bool(signature) and signature == canonical_signature)
            variant["equivalence"] = "exact" if exact else "compatible"
            variant["selection_status"] = "exact_code_signature_maturity_blocked" if exact else "candidate_pending_semantic_review"
        modules.append({
            "schema_version": "1.0",
            "capability_module_id": f"plot-{stable_id('capability', key)}",
            "capability": label,
            "semantic_key": key,
            "canonical_variant_id": canonical["variant_id"],
            "variants": variants,
            "variant_policy": {
                "preserve_all": True,
                "auto_trial_allowed_for": ["exact"],
                "requires_explanation_for": ["compatible"],
                "requires_user_choice_for": ["alternative_method"],
            },
        })
    return modules


def _bundle_source_locators(bundle: dict[str, Any]) -> set[str]:
    locators = {
        normalize_windows_path(item.get("source_locator"))
        for item in bundle.get("ordered_code_files", []) + bundle.get("images", [])
        if item.get("source_locator")
    }
    article = bundle.get("article", {}).get("source_locator")
    if article:
        locators.add(normalize_windows_path(article))
    return locators


def _external_snapshot_relations(
    root: SourceRoot,
    record: dict[str, Any],
    file_rows: Sequence[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str, list[str]]:
    warnings: list[str] = []
    target_skill = str(root.extra.get("reuse_skill") or "visualization-2026718-v1")
    snapshot_relative = str(root.extra.get("reuse_snapshot") or "assets/source_archive")
    snapshot_root = SKILL_ROOT.parent / target_skill / snapshot_relative
    checksum_path = snapshot_root / "SHA256SUMS.csv"
    if not checksum_path.exists():
        return [], "external_reuse_pending_item_link", ["external_snapshot_checksum_manifest_missing"]
    with checksum_path.open("r", encoding="utf-8-sig", newline="") as handle:
        checksum_rows = list(csv.DictReader(handle))
    checksums = {str(row.get("path") or "").replace("\\", "/"): str(row.get("sha256") or "") for row in checksum_rows}
    source_hashes = {
        str(row.get("relative_path") or "").replace("\\", "/"): str(row.get("sha256") or "")
        for row in file_rows
        if row.get("source_root_id") == root.id and row.get("sha256")
    }
    record_path = Path(str(record.get("路径") or ""))
    targets: list[str] = []
    if record_path.exists() and record_path.is_file():
        try:
            targets = [record_path.resolve().relative_to(root.path.resolve()).as_posix()]
        except ValueError:
            warnings.append("record_file_outside_declared_source_root")
    else:
        path_text = str(record.get("路径") or "")
        root_text = str(root.path)
        relative_hint = path_text[len(root_text):].lstrip("\\/") if normalize_windows_path(path_text).startswith(normalize_windows_path(root_text)) else path_text
        album = re.split(r"[（(]", relative_hint, maxsplit=1)[0].strip("\\/")
        article_match = re.search(r"文章\s*([^）)]+)", relative_hint)
        sequences = re.findall(r"(?<!\d)(\d{3})(?!\d)", article_match.group(1)) if article_match else []
        for sequence in sequences:
            prefix = f"{album}/{sequence}_"
            matches = sorted(path for path in checksums if path.startswith(prefix) and path.casefold().endswith(".md"))
            if len(matches) == 1:
                targets.append(matches[0])
            else:
                warnings.append(f"external_sequence_{sequence}_matched_{len(matches)}_files")
    relations: list[dict[str, Any]] = []
    for target in sorted(set(targets), key=natural_key):
        snapshot_hash = checksums.get(target)
        source_hash = source_hashes.get(target)
        verified = bool(snapshot_hash and source_hash and snapshot_hash == source_hash)
        relations.append(
            {
                "relation_type": "external_snapshot_exact_hash" if verified else "external_snapshot_candidate",
                "target_skill": target_skill,
                "target_relative_path": f"{snapshot_relative.rstrip('/')}/{target}",
                "sha256": snapshot_hash,
                "source_sha256": source_hash,
                "verified": verified,
            }
        )
    if relations and all(item["verified"] for item in relations) and not warnings:
        return relations, "external_reuse_hash_verified", []
    if relations:
        return relations, "external_reuse_partial", warnings
    return [], "external_reuse_pending_item_link", warnings or ["external_reuse_target_not_resolved"]


def build_preprocessing_crosswalk(
    records: Sequence[dict[str, Any]],
    bundles: Sequence[dict[str, Any]],
    roots: Sequence[SourceRoot],
    file_rows: Sequence[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    registry = build_preprocessing_registry(records, roots)
    root_by_name = {root.name: root for root in roots}
    bundles_by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    bundle_dirs: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    source_file_bundles: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    heuristic_bundles: dict[str, set[str]] = defaultdict(set)
    for bundle in bundles:
        source_name = str(bundle.get("source_root_name") or "")
        bundles_by_source[source_name].append(bundle)
        bundle_dirs[source_name][normalize_windows_path(bundle.get("private_source_directory"))].append(bundle)
        for locator in _bundle_source_locators(bundle):
            source_file_bundles[source_name][locator].add(bundle["bundle_id"])
        record_id = bundle.get("preprocessing_match", {}).get("record_id")
        if record_id:
            heuristic_bundles[str(record_id)].add(bundle["bundle_id"])
    crosswalk: list[dict[str, Any]] = []
    for registry_row in registry:
        record = registry_row["record"]
        record_id = registry_row["preprocess_record_id"]
        source_name = str(record.get("来源文件夹") or "")
        root = root_by_name.get(source_name)
        record_path_text = str(record.get("路径") or "")
        record_path_key = normalize_windows_path(record_path_text)
        relations: list[dict[str, Any]] = []
        warnings: list[str] = []
        status = "unmatched"
        relation_verified = False
        exact = bundle_dirs[source_name].get(record_path_key, [])
        if exact:
            relations = [
                {"relation_type": "exact_bundle_path", "bundle_id": bundle["bundle_id"], "verified": True}
                for bundle in sorted(exact, key=lambda item: item["bundle_id"])
            ]
            status, relation_verified = "exact_bundle_path", True
        elif source_file_bundles[source_name].get(record_path_key):
            relations = [
                {"relation_type": "exact_source_file", "bundle_id": bundle_id, "verified": True}
                for bundle_id in sorted(source_file_bundles[source_name][record_path_key])
            ]
            status, relation_verified = "exact_source_file", True
        elif root and Path(record_path_text).exists() and Path(record_path_text).is_dir():
            descendants = [
                bundle for bundle in bundles_by_source[source_name]
                if normalize_windows_path(bundle.get("private_source_directory")).startswith(record_path_key + "\\")
            ]
            if descendants:
                relations = [
                    {"relation_type": "collection_member", "bundle_id": bundle["bundle_id"], "verified": True}
                    for bundle in sorted(descendants, key=lambda item: item["bundle_id"])
                ]
                status, relation_verified = "collection_bundle_set", True
        if not relations and root:
            root_key = normalize_windows_path(root.path)
            pseudo_descriptor = record_path_key.startswith(root_key + "\\（") or record_path_key.startswith(root_key + "\\(")
            root_bundles = bundle_dirs[source_name].get(root_key, [])
            if pseudo_descriptor and root_bundles:
                requested_files = sorted(
                    set(re.findall(r"(?i)([A-Za-z0-9_.-]+\.(?:r|rmd|qmd|py|sh|ps1|jl|sql))", record_path_text)),
                    key=natural_key,
                )
                file_relations: list[dict[str, Any]] = []
                for bundle in sorted(root_bundles, key=lambda item: item["bundle_id"]):
                    indexed_files = {Path(item["relative_to_bundle"]).name.casefold(): item for item in bundle.get("ordered_code_files", [])}
                    for requested in requested_files:
                        item = indexed_files.get(requested.casefold())
                        if item:
                            file_relations.append(
                                {
                                    "relation_type": "project_root_source_file",
                                    "bundle_id": bundle["bundle_id"],
                                    "source_relative_path": item["source_relative_path"],
                                    "sha256": item["sha256"],
                                    "verified": True,
                                }
                            )
                if requested_files and len(file_relations) == len(requested_files):
                    relations = file_relations
                else:
                    relations = [
                        {"relation_type": "project_root_descriptor", "bundle_id": bundle["bundle_id"], "verified": True}
                        for bundle in sorted(root_bundles, key=lambda item: item["bundle_id"])
                    ]
                    if requested_files:
                        warnings.append("project_descriptor_files_not_fully_resolved")
                status, relation_verified = "project_root_descriptor", True
        if not relations and root and root.extra.get("overlap_policy"):
            relations, status, warnings = _external_snapshot_relations(root, record, file_rows)
            relation_verified = bool(relations) and all(bool(item.get("verified")) for item in relations)
        if not relations and root and root.extra.get("empty_asset_policy"):
            status = "declared_missing_source_asset"
            warnings.append(str(root.extra.get("empty_asset_policy")))
        if not relations and heuristic_bundles.get(record_id):
            relations = [
                {"relation_type": "heuristic_bundle_candidate", "bundle_id": bundle_id, "verified": False}
                for bundle_id in sorted(heuristic_bundles[record_id])
            ]
            status = "heuristic_bundle_candidate"
        if root is None:
            status = "source_root_not_configured"
            warnings.append("preprocessing_source_folder_not_in_corpus_config")
        crosswalk.append(
            {
                "schema_version": "1.0",
                "preprocess_record_id": record_id,
                "record_sha256": registry_row["record_sha256"],
                "source_root_id": root.id if root else None,
                "source_folder": source_name,
                "record_title": str(record.get("记录标题") or ""),
                "distillation_value": registry_row["distillation_value"],
                "category": str(record.get("一级分类") or "unknown"),
                "code_asset_completeness": str(record.get("代码资产完整度") or "unknown"),
                "status": status,
                "relation_verified": relation_verified,
                "relations": relations,
                "warnings": sorted(set(warnings)),
                "maturity": MATURITY,
            }
        )
    status_counts = Counter(row["status"] for row in crosswalk)
    high_rows = [row for row in crosswalk if row["distillation_value"] == "高"]
    high_status = Counter(row["status"] for row in high_rows)
    report = {
        "schema_version": "1.0",
        "record_identity_fields": list(PREPROCESS_IDENTITY_FIELDS),
        "records": len(registry),
        "unique_record_ids": len({row["preprocess_record_id"] for row in registry}),
        "crosswalk_entries": len(crosswalk),
        "status": dict(sorted(status_counts.items())),
        "relation_verified_records": sum(bool(row["relation_verified"]) for row in crosswalk),
        "records_without_relations": sum(not row["relations"] for row in crosswalk),
        "high_value": {
            "records": len(high_rows),
            "status": dict(sorted(high_status.items())),
            "relation_verified_records": sum(bool(row["relation_verified"]) for row in high_rows),
            "records_without_relations": sum(not row["relations"] for row in high_rows),
            "record_ids_complete": len({row["preprocess_record_id"] for row in high_rows}) == len(high_rows),
        },
        "scientific_boundary": "A verified crosswalk proves source identity and relation only; it does not promote code, methods, figures, or claims beyond raw-extracted maturity.",
    }
    return registry, crosswalk, report


DOMAIN_QUOTAS = {
    "可视化图形": 12,
    "单细胞分析": 12,
    "空间转录组": 8,
    "bulk组学分析": 8,
    "统计与机器学习": 6,
    "R包或Python包工具": 6,
    "分析思路与文献解读": 8,
}


def gold_score(bundle: dict[str, Any]) -> tuple[int, int, int, str]:
    record = bundle.get("preprocessing_match", {}).get("record") or {}
    completeness = str(record.get("代码资产完整度") or "")
    complete_score = 4 if "完整" in completeness else 3 if "多脚本" in completeness else 2 if "单脚本" in completeness else 0
    code_count = len(bundle.get("ordered_code_files", [])) + len(bundle.get("article", {}).get("fenced_code_blocks", []))
    image_count = len(bundle.get("images", []))
    return complete_score, min(code_count, 50), min(image_count, 20), bundle["bundle_id"]


def select_gold(bundles: Sequence[dict[str, Any]]) -> dict[str, Any]:
    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    selected_titles: set[str] = set()

    def add_unique(bundle: dict[str, Any], domain: str, reason: str) -> bool:
        title_key = clean_title(str(bundle.get("title", "")))
        if not title_key or title_key in selected_titles or bundle["bundle_id"] in selected_ids:
            return False
        selected_ids.add(bundle["bundle_id"])
        selected_titles.add(title_key)
        selected.append(
            {
                "bundle_id": bundle["bundle_id"],
                "domain": domain,
                "title": bundle["title"],
                "reason": reason,
            }
        )
        return True

    for domain, quota in DOMAIN_QUOTAS.items():
        candidates = [bundle for bundle in bundles if (bundle.get("preprocessing_match", {}).get("record") or {}).get("一级分类") == domain and (bundle.get("preprocessing_match", {}).get("record") or {}).get("蒸馏价值") == "高"]
        candidates.sort(key=gold_score, reverse=True)
        accepted = 0
        for bundle in candidates:
            if add_unique(bundle, domain, "high_value_stratified_unique_article_complete_flow_priority"):
                accepted += 1
            if accepted == quota:
                break
    if len(selected) < 60:
        remaining = [bundle for bundle in bundles if bundle["bundle_id"] not in selected_ids and (bundle.get("preprocessing_match", {}).get("record") or {}).get("蒸馏价值") == "高"]
        remaining.sort(key=gold_score, reverse=True)
        for bundle in remaining:
            record = bundle.get("preprocessing_match", {}).get("record") or {}
            add_unique(bundle, record.get("一级分类", "other"), "high_value_unique_article_global_fill")
            if len(selected) == 60:
                break
    return {
        "schema_version": "1.1",
        "selection_policy": "fixed_domain_quotas_unique_normalized_titles_then_global_high_value_fill",
        "target": 60,
        "unique_article_key": "normalized_title",
        "selected": selected[:60],
    }


def resolve_gold_set(
    output_dir: Path,
    candidate: dict[str, Any],
    bundles: Sequence[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    review_dir = output_dir / "manual-review"
    review_files = sorted(review_dir.glob("gold-review-batch-*.jsonl")) if review_dir.exists() else []
    if not review_files:
        return candidate, None
    reviews: list[dict[str, Any]] = []
    for path in review_files:
        reviews.extend(read_jsonl(path))
    if not reviews:
        return candidate, None
    bundle_ids = {bundle["bundle_id"] for bundle in bundles}
    review_ids = [str(review.get("bundle_id") or "") for review in reviews]
    if len(review_ids) != len(set(review_ids)):
        raise ValueError("Manual gold reviews contain duplicate bundle IDs")
    missing = sorted(set(review_ids) - bundle_ids)
    if missing:
        raise ValueError(f"Manual gold reviews reference missing bundles: {missing}")
    frozen = {
        "schema_version": "1.2",
        "selection_policy": "frozen_existing_manual_review_set_after_crosswalk_migration",
        "target": len(reviews),
        "unique_article_key": "bundle_id",
        "frozen": True,
        "review_batch_files": [path.name for path in review_files],
        "selected": [
            {
                "bundle_id": review["bundle_id"],
                "domain": review["domain"],
                "title": review["title"],
                "reason": "existing_manual_review_preserved",
            }
            for review in reviews
        ],
    }
    return frozen, candidate


def reconcile_gold_set(output_dir: Path) -> dict[str, Any]:
    bundles = read_jsonl(output_dir / "source-flow-bundles.jsonl")
    frozen, candidate = resolve_gold_set(output_dir, select_gold(bundles), bundles)
    json_dump(output_dir / "gold-set.json", frozen)
    candidate_path = output_dir / "gold-set-candidate-v2.json"
    if candidate is not None:
        json_dump(candidate_path, candidate)
    return {
        "ok": True,
        "frozen": bool(frozen.get("frozen")),
        "selected": len(frozen.get("selected", [])),
        "candidate_saved": candidate is not None,
    }


def validate_bundle(bundle: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    required = {"bundle_id", "title", "source_root_id", "flow_integrity", "ordered_code_files", "images", "maturity"}
    missing = sorted(required - set(bundle))
    if missing:
        errors.append(f"missing fields: {missing}")
    for item in bundle.get("ordered_code_files", []):
        path = Path(item["source_locator"])
        if not path.exists():
            errors.append(f"missing source code: {path}")
        elif sha256_file(path) != item["sha256"]:
            errors.append(f"source hash changed: {path}")
    return errors


def build_index(config_path: Path, output_dir: Path, reuse_inventory: bool = False) -> dict[str, Any]:
    config, roots, records = load_config(config_path)
    started = utc_now()
    cached_lookup: dict[tuple[str, str], dict[str, Any]] = {}
    if reuse_inventory and (output_dir / "file-inventory.jsonl").exists():
        cached_lookup = file_row_lookup(read_jsonl(output_dir / "file-inventory.jsonl"))
    file_rows, by_sha, reused_hashes = scan_files(roots, cached_lookup)
    lookup = file_row_lookup(file_rows)
    bundles: list[dict[str, Any]] = []
    source_status: list[dict[str, Any]] = []
    for root in roots:
        if not root.path.is_dir():
            source_status.append({"id": root.id, "name": root.name, "path": str(root.path), "status": "missing"})
            continue
        dirs = discover_bundle_dirs(root)
        source_status.append({"id": root.id, "name": root.name, "path": str(root.path), "status": "indexed", "bundle_directories": len(dirs), **root.extra})
        bundles.extend(build_bundle(root, path, lookup, records) for path in dirs)
    duplicate_groups = [
        {"sha256": digest, "size": rows[0].get("size"), "files": [{"source_root_id": row["source_root_id"], "relative_path": row["relative_path"]} for row in rows]}
        for digest, rows in by_sha.items() if len(rows) > 1
    ]
    duplicate_groups.sort(key=lambda item: (-len(item["files"]), item["sha256"]))
    preprocessing_registry, preprocessing_crosswalk, crosswalk_report = build_preprocessing_crosswalk(
        records, bundles, roots, file_rows
    )
    methods = build_method_cards(bundles)
    packages = build_package_cards(bundles)
    figures = build_figure_cards(bundles)
    modules = build_capability_modules(bundles)
    gold_candidate = select_gold(bundles)
    gold, next_gold = resolve_gold_set(output_dir, gold_candidate, bundles)
    output_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(output_dir / "file-inventory.jsonl", file_rows)
    json_dump(output_dir / "duplicate-groups.json", {"schema_version": "1.0", "groups": duplicate_groups})
    write_jsonl(output_dir / "preprocessing-records.jsonl", preprocessing_registry)
    write_jsonl(output_dir / "preprocessing-crosswalk.jsonl", preprocessing_crosswalk)
    json_dump(output_dir / "preprocessing-crosswalk-report.json", crosswalk_report)
    write_jsonl(output_dir / "source-flow-bundles.jsonl", bundles)
    write_jsonl(output_dir / "method-cards.jsonl", methods)
    write_jsonl(output_dir / "package-cards.jsonl", packages)
    write_jsonl(output_dir / "figure-cards.jsonl", figures)
    json_dump(output_dir / "capability-modules.json", {"schema_version": "1.0", "modules": modules})
    json_dump(output_dir / "gold-set.json", gold)
    if next_gold is not None:
        json_dump(output_dir / "gold-set-candidate-v2.json", next_gold)
    issue_counts = Counter(issue["code"] for bundle in bundles for issue in bundle.get("issues", []))
    maturity_counts = Counter(bundle["maturity"] for bundle in bundles)
    category_counts = Counter((bundle.get("preprocessing_match", {}).get("record") or {}).get("一级分类", "unmatched") for bundle in bundles)
    manifest = {
        "schema_version": "1.0",
        "generated_at": utc_now(),
        "started_at": started,
        "config_sha256": sha256_file(config_path),
        "source_policy": "read_only",
        "distribution": config.get("distribution", {}),
        "source_roots": source_status,
        "counts": {
            "files": len(file_rows),
            "hashes_reused_from_previous_inventory": reused_hashes,
            "file_errors": sum(1 for row in file_rows if "error" in row),
            "duplicate_groups": len(duplicate_groups),
            "source_flow_bundles": len(bundles),
            "matched_preprocessing_records": sum(1 for bundle in bundles if bundle["preprocessing_match"]["status"] == "matched"),
            "preprocessing_records": len(preprocessing_registry),
            "preprocessing_crosswalk_entries": len(preprocessing_crosswalk),
            "high_value_preprocessing_records": crosswalk_report["high_value"]["records"],
            "high_value_relation_verified_records": crosswalk_report["high_value"]["relation_verified_records"],
            "high_value_records_without_relations": crosswalk_report["high_value"]["records_without_relations"],
            "method_cards": len(methods),
            "package_cards": len(packages),
            "figure_cards": len(figures),
            "capability_modules": len(modules),
            "gold_set": len(gold["selected"]),
        },
        "maturity": dict(maturity_counts),
        "categories": dict(category_counts),
        "issues": dict(issue_counts),
        "privacy": "Contains private absolute source locators and extracted code. Do not distribute.",
    }
    json_dump(output_dir / "corpus-manifest.json", manifest)
    report = [
        "# Corpus ingestion report",
        "",
        f"Generated: {manifest['generated_at']}",
        "",
        "## Outcome",
        "",
        f"- Files hashed: {manifest['counts']['files']}",
        f"- SourceFlowBundles: {manifest['counts']['source_flow_bundles']}",
        f"- Preprocessing matches: {manifest['counts']['matched_preprocessing_records']}",
        f"- Stable preprocessing records: {manifest['counts']['preprocessing_records']}",
        f"- High-value records with verified source relations: {manifest['counts']['high_value_relation_verified_records']}/{manifest['counts']['high_value_preprocessing_records']}",
        f"- High-value records without source relations: {manifest['counts']['high_value_records_without_relations']}",
        f"- MethodCards: {manifest['counts']['method_cards']}",
        f"- PackageCards: {manifest['counts']['package_cards']}",
        f"- FigureCards awaiting native review: {manifest['counts']['figure_cards']}",
        f"- CapabilityModules preserving variants: {manifest['counts']['capability_modules']}",
        "",
        "## Scientific boundary",
        "",
        "All generated records are static extraction at `raw-extracted` maturity. No code was promoted to executable or data-verified status by this operation.",
        "",
        "## Issues",
        "",
    ]
    report.extend(f"- {key}: {value}" for key, value in sorted(issue_counts.items()))
    (output_dir / "ingestion-report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    return manifest


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def validate_index(output_dir: Path, verify_source_hashes: bool = False) -> dict[str, Any]:
    manifest = read_json(output_dir / "corpus-manifest.json")
    bundles = read_jsonl(output_dir / "source-flow-bundles.jsonl")
    errors: list[dict[str, Any]] = []
    seen: set[str] = set()
    for bundle in bundles:
        if bundle["bundle_id"] in seen:
            errors.append({"bundle_id": bundle["bundle_id"], "errors": ["duplicate bundle_id"]})
        seen.add(bundle["bundle_id"])
        if verify_source_hashes:
            bundle_errors = validate_bundle(bundle)
            if bundle_errors:
                errors.append({"bundle_id": bundle["bundle_id"], "errors": bundle_errors})
    expected = manifest.get("counts", {}).get("source_flow_bundles")
    if expected != len(bundles):
        errors.append({"scope": "manifest", "errors": [f"bundle count {len(bundles)} != {expected}"]})
    registry_path = output_dir / "preprocessing-records.jsonl"
    crosswalk_path = output_dir / "preprocessing-crosswalk.jsonl"
    registry: list[dict[str, Any]] = []
    crosswalk: list[dict[str, Any]] = []
    if not registry_path.exists() or not crosswalk_path.exists():
        errors.append({"scope": "preprocessing-crosswalk", "errors": ["stable preprocessing registry or crosswalk is missing"]})
    else:
        registry = read_jsonl(registry_path)
        crosswalk = read_jsonl(crosswalk_path)
        registry_ids = [str(row.get("preprocess_record_id") or "") for row in registry]
        crosswalk_ids = [str(row.get("preprocess_record_id") or "") for row in crosswalk]
        if len(registry_ids) != len(set(registry_ids)):
            errors.append({"scope": "preprocessing-registry", "errors": ["duplicate preprocess_record_id"]})
        if len(crosswalk_ids) != len(set(crosswalk_ids)):
            errors.append({"scope": "preprocessing-crosswalk", "errors": ["duplicate preprocess_record_id"]})
        if set(registry_ids) != set(crosswalk_ids):
            errors.append({"scope": "preprocessing-crosswalk", "errors": ["registry and crosswalk record IDs differ"]})
        expected_records = manifest.get("counts", {}).get("preprocessing_records")
        if expected_records != len(registry):
            errors.append({"scope": "manifest", "errors": [f"preprocessing record count {len(registry)} != {expected_records}"]})
        bundle_ids = {bundle["bundle_id"] for bundle in bundles}
        bad_relations = [
            (row.get("preprocess_record_id"), relation.get("bundle_id"))
            for row in crosswalk
            for relation in row.get("relations", [])
            if relation.get("bundle_id") and relation.get("bundle_id") not in bundle_ids
        ]
        if bad_relations:
            errors.append({"scope": "preprocessing-crosswalk", "errors": [f"unknown bundle relations: {bad_relations[:10]}"]})
        high_rows = [row for row in crosswalk if row.get("distillation_value") == "高"]
        expected_high = manifest.get("counts", {}).get("high_value_preprocessing_records")
        if expected_high != len(high_rows):
            errors.append({"scope": "manifest", "errors": [f"high-value crosswalk count {len(high_rows)} != {expected_high}"]})
    return {
        "ok": not errors,
        "bundles_checked": len(bundles),
        "preprocessing_records_checked": len(registry),
        "crosswalk_entries_checked": len(crosswalk),
        "verify_source_hashes": verify_source_hashes,
        "errors": errors,
    }


def materialize_bundle(output_dir: Path, bundle_id: str, destination: Path) -> dict[str, Any]:
    bundles = read_jsonl(output_dir / "source-flow-bundles.jsonl")
    matches = [bundle for bundle in bundles if bundle["bundle_id"] == bundle_id]
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one bundle for {bundle_id}, found {len(matches)}")
    bundle = matches[0]
    errors = validate_bundle(bundle)
    if errors:
        raise RuntimeError("Source validation failed: " + "; ".join(errors))
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    copied: list[dict[str, Any]] = []
    for item in bundle["ordered_code_files"]:
        source = Path(item["source_locator"])
        target = destination / item["relative_to_bundle"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied.append({"source": str(source), "destination": str(target), "sha256": sha256_file(target)})
    fenced_dir = destination / "article_fenced_blocks"
    for block in bundle.get("article", {}).get("fenced_code_blocks", []):
        suffix = {"r": ".R", "python": ".py", "shell": ".sh", "powershell": ".ps1"}.get(block["normalized_language"], ".txt")
        target = fenced_dir / f"block-{block['ordinal']:04d}{suffix}"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(block["text"], encoding="utf-8")
        copied.append({"source": f"article:block:{block['ordinal']}", "destination": str(target), "sha256": sha256_file(target)})
    receipt = {"bundle_id": bundle_id, "source_fingerprint": bundle["flow_integrity"]["flow_fingerprint_sha256"], "materialized_at": utc_now(), "files": copied}
    json_dump(destination / "materialization-receipt.json", receipt)
    return receipt


def rebuild_crosswalk(config_path: Path, output_dir: Path) -> dict[str, Any]:
    _, roots, records = load_config(config_path)
    bundles = read_jsonl(output_dir / "source-flow-bundles.jsonl")
    file_rows = read_jsonl(output_dir / "file-inventory.jsonl")
    registry, crosswalk, report = build_preprocessing_crosswalk(records, bundles, roots, file_rows)
    write_jsonl(output_dir / "preprocessing-records.jsonl", registry)
    write_jsonl(output_dir / "preprocessing-crosswalk.jsonl", crosswalk)
    json_dump(output_dir / "preprocessing-crosswalk-report.json", report)
    manifest_path = output_dir / "corpus-manifest.json"
    manifest = read_json(manifest_path)
    manifest["crosswalk_generated_at"] = utc_now()
    manifest.setdefault("counts", {}).update(
        {
            "preprocessing_records": len(registry),
            "preprocessing_crosswalk_entries": len(crosswalk),
            "high_value_preprocessing_records": report["high_value"]["records"],
            "high_value_relation_verified_records": report["high_value"]["relation_verified_records"],
            "high_value_records_without_relations": report["high_value"]["records_without_relations"],
        }
    )
    json_dump(manifest_path, manifest)
    return {"ok": True, **report}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="Hash sources and build the private index")
    build.add_argument("--reuse-inventory", action="store_true", help="Reuse immediately prior hashes for same-path, same-size files")
    validate = sub.add_parser("validate", help="Validate index structure and optional source hashes")
    validate.add_argument("--verify-source-hashes", action="store_true")
    sub.add_parser("crosswalk", help="Rebuild stable preprocessing IDs and relations without rescanning source files")
    sub.add_parser("gold", help="Preserve reviewed gold records and write the next candidate set separately")
    materialize = sub.add_parser("materialize", help="Copy one hash-verified code flow to a run directory")
    materialize.add_argument("--bundle-id", required=True)
    materialize.add_argument("--destination", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "build":
        result = build_index(args.config.resolve(), args.output_dir.resolve(), args.reuse_inventory)
    elif args.command == "validate":
        result = validate_index(args.output_dir.resolve(), args.verify_source_hashes)
    elif args.command == "crosswalk":
        result = rebuild_crosswalk(args.config.resolve(), args.output_dir.resolve())
    elif args.command == "gold":
        result = reconcile_gold_set(args.output_dir.resolve())
    elif args.command == "materialize":
        result = materialize_bundle(args.output_dir.resolve(), args.bundle_id, args.destination)
    else:
        raise AssertionError(args.command)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main())
