#!/usr/bin/env python3
"""Read-only syntax audit for a materialized distillation review batch.

This tool validates source hashes and parses R/Python text without evaluating it.
It does not install packages, import article code, source R files, or run analysis.
"""

from __future__ import annotations

import argparse
import ast
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Sequence


SKILL_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INDEX = SKILL_ROOT / "assets" / "private-corpus-index"
DEFAULT_AUDIT_DIR = DEFAULT_INDEX / "manual-review" / "syntax-audits"
DEFAULT_RSCRIPT = Path(r"C:\Program Files\R\R-4.5.3\bin\Rscript.exe")
DEFAULT_SKILLS_ROOT = SKILL_ROOT.parent
FENCE_RE = re.compile(r"```\s*([^\n`]*)\n(.*?)```", re.DOTALL)
R_LOCATION_RE = re.compile(r":(?P<line>\d+):(?P<column>\d+):\s*(?P<message>[^\r\n]+)")
R_TRAILING_LOCATION_RE = re.compile(r"\([^()]*:(?P<line>\d+):(?P<column>\d+)\)\s*$")
LANGUAGE_ALIASES = {
    "r": "r",
    "rscript": "r",
    "python": "python",
    "python3": "python",
    "py": "python",
    "bash": "shell",
    "sh": "shell",
    "shell": "shell",
    "zsh": "shell",
    "powershell": "shell",
    "ps1": "shell",
    "cmd": "shell",
    "batch": "shell",
}
UNICODE_SPACE_NORMALIZATION_PROFILE = "unicode-space-v1"
UNICODE_SPACE_REPLACEMENTS = {
    "\u00a0": " ",
}


R_BATCH_EXPRESSION = r'''
args <- commandArgs(trailingOnly = TRUE)
input_dir <- args[[1]]
output_file <- args[[2]]
paths <- sort(list.files(input_dir, pattern = "[.]R$", full.names = TRUE))
out <- file(output_file, open = "wt", encoding = "UTF-8")
on.exit(close(out), add = TRUE)
for (path in paths) {
  item_id <- tools::file_path_sans_ext(basename(path))
  status <- "passed"
  category <- ""
  message <- ""
  tryCatch(
    parse(file = path, keep.source = FALSE, encoding = "UTF-8"),
    error = function(e) {
      status <<- "failed"
      category <<- "r_parse_error"
      message <<- conditionMessage(e)
    }
  )
  if (nzchar(message)) {
    # On Windows a parse diagnostic can contain UTF-8 source bytes while the
    # condition string is marked with the native code page.  Calling sub()
    # directly on that string aborts the whole batch and incorrectly turns
    # every R item into an invocation failure.  Sanitize the diagnostic as
    # UTF-8 first; this only affects the error text, never the parsed source.
    message <- tryCatch(
      {
        converted <- iconv(message, from = "UTF-8", to = "UTF-8", sub = "byte")
        if (is.na(converted)) {
          "R parse error; diagnostic text unavailable due to encoding"
        } else {
          sub("[\r\n].*$", "", converted)
        }
      },
      error = function(e) "R parse error; diagnostic text unavailable due to encoding"
    )
  }
  message <- gsub("[\t]+", " ", message)
  writeLines(paste(item_id, status, category, message, sep = "\t"), out)
}
'''.strip()


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def write_jsonl_atomic(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    count = 0
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
            count += 1
    os.replace(temporary, path)
    return count


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_id(prefix: str, *parts: str) -> str:
    payload = "\x1f".join(parts).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(payload).hexdigest()[:20]}"


def decode_source(data: bytes) -> tuple[str, str]:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace"), "utf-8-replace"


def normalize_language(declared: Any, text: str = "") -> str:
    token = str(declared or "unknown").strip().split()[0].casefold() if str(declared or "").strip() else "unknown"
    token = token.strip("{}")
    if token in LANGUAGE_ALIASES:
        return LANGUAGE_ALIASES[token]
    if token not in {"", "unknown", "text", "plain", "plaintext"}:
        return token
    if re.search(r"(?m)(?:^|\s)(?:library|require)\s*\(|<-|\bggplot\s*\(", text):
        return "r"
    if re.search(r"(?m)^\s*(?:from\s+\S+\s+import|import\s+\S+|def\s+\w+\s*\(|class\s+\w+)", text):
        return "python"
    if re.search(r"(?m)^\s*(?:#!.*(?:ba)?sh|(?:pip|conda)\s+install\b|(?:wget|curl)\s+)", text):
        return "shell"
    return "unknown"


def compact_summary(value: Any, limit: int = 300) -> str | None:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 3] + "..."


def unicode_space_candidate(text: str) -> tuple[str, dict[str, int]]:
    """Return a review-only code candidate with whitelisted spaces normalized.

    The original text is never changed.  This intentionally handles only
    U+00A0, the common non-breaking-space artifact introduced while extracting
   公众号 code.  Punctuation, quotes, operators, comments, and line breaks are
    outside this profile and require a separate reviewed repair.
    """

    normalized = text
    changes: dict[str, int] = {}
    for source, replacement in UNICODE_SPACE_REPLACEMENTS.items():
        count = normalized.count(source)
        if count:
            label = f"U+{ord(source):04X}->U+{ord(replacement):04X}"
            changes[label] = count
            normalized = normalized.replace(source, replacement)
    return normalized, changes


def source_line_for_item(item: dict[str, Any], error_line: int | None) -> int | None:
    if error_line is None:
        return None
    span = item.get("source_span")
    if item.get("item_type") in {"article_fenced_block", "external_markdown_fenced_block"} and isinstance(span, list) and span:
        return int(span[0]) + error_line
    return error_line


def base_evidence(
    queue_row: dict[str, Any],
    bundle_id: str | None,
    item_type: str,
    ordinal: int,
    declared_language: str,
    normalized_language: str,
    declared_sha256: str | None,
    observed_sha256: str | None,
    source_reference: str,
    source_span: list[int] | None = None,
) -> dict[str, Any]:
    item_key = f"{item_type}:{ordinal}:{declared_sha256 or observed_sha256 or source_reference}"
    return {
        "schema_version": "1.0",
        "audit_item_id": stable_id(
            "syntax-audit",
            str(queue_row["preprocess_record_id"]),
            str(bundle_id or "external"),
            item_key,
        ),
        "batch_id": queue_row.get("batch_id"),
        "queue_item_id": queue_row.get("queue_item_id"),
        "preprocess_record_id": queue_row["preprocess_record_id"],
        "record_sha256": queue_row.get("record_sha256"),
        "bundle_id": bundle_id,
        "item_type": item_type,
        "item_ordinal": ordinal,
        "source_reference": source_reference,
        "source_span": source_span,
        "declared_language": declared_language or "unknown",
        "normalized_language": normalized_language,
        "declared_sha256": declared_sha256,
        "observed_sha256": observed_sha256,
        "hash_verified": bool(declared_sha256 and observed_sha256 and declared_sha256 == observed_sha256),
        "parser": None,
        "parse_status": "pending",
        "error_category": None,
        "error_line": None,
        "error_column": None,
        "source_error_line": None,
        "error_summary": None,
        "scientific_boundary": "Syntax parsing only; no code was evaluated and no executable or scientific validity is implied.",
    }


def mark_failure(row: dict[str, Any], category: str, summary: Any) -> None:
    row.update({
        "parse_status": "failed",
        "error_category": category,
        "error_summary": compact_summary(summary),
    })


def ordered_file_items(queue_row: dict[str, Any], bundle: dict[str, Any]) -> list[tuple[dict[str, Any], str | None]]:
    items: list[tuple[dict[str, Any], str | None]] = []
    for position, code_file in enumerate(bundle.get("ordered_code_files", []), start=1):
        ordinal = int(code_file.get("ordinal") or position)
        source = Path(str(code_file.get("source_locator") or ""))
        declared_hash = str(code_file.get("sha256") or "") or None
        declared_language = str(code_file.get("declared_language") or "unknown")
        normalized_language = normalize_language(code_file.get("normalized_language") or declared_language)
        reference = str(code_file.get("source_relative_path") or code_file.get("relative_to_bundle") or source)
        if not source.is_file():
            row = base_evidence(queue_row, bundle["bundle_id"], "ordered_code_file", ordinal, declared_language, normalized_language, declared_hash, None, reference)
            mark_failure(row, "source_missing", f"Source file is missing: {reference}")
            items.append((row, None))
            continue
        try:
            data = source.read_bytes()
        except OSError as exc:
            row = base_evidence(queue_row, bundle["bundle_id"], "ordered_code_file", ordinal, declared_language, normalized_language, declared_hash, None, reference)
            mark_failure(row, "source_read_error", type(exc).__name__)
            items.append((row, None))
            continue
        observed_hash = sha256_bytes(data)
        row = base_evidence(queue_row, bundle["bundle_id"], "ordered_code_file", ordinal, declared_language, normalized_language, declared_hash, observed_hash, reference)
        if declared_hash != observed_hash:
            mark_failure(row, "source_hash_mismatch", "Observed source hash differs from SourceFlowBundle evidence.")
            items.append((row, None))
            continue
        text, encoding = decode_source(data)
        row["source_encoding"] = encoding
        items.append((row, text))
    return items


def article_block_items(queue_row: dict[str, Any], bundle: dict[str, Any]) -> list[tuple[dict[str, Any], str | None]]:
    article = bundle.get("article", {})
    article_path = Path(str(article.get("source_locator") or ""))
    article_declared_hash = str(article.get("sha256") or "") or None
    article_observed_hash: str | None = None
    article_error: tuple[str, str] | None = None
    if not article_path.is_file():
        article_error = ("article_source_missing", "Article source file is missing.")
    else:
        try:
            article_observed_hash = sha256_file(article_path)
        except OSError as exc:
            article_error = ("article_source_read_error", type(exc).__name__)
        else:
            if article_declared_hash != article_observed_hash:
                article_error = ("article_source_hash_mismatch", "Observed article hash differs from SourceFlowBundle evidence.")
    items: list[tuple[dict[str, Any], str | None]] = []
    for position, block in enumerate(article.get("fenced_code_blocks", []), start=1):
        ordinal = int(block.get("ordinal") or position)
        text = str(block.get("text") or "")
        declared_hash = str(block.get("sha256") or "") or None
        observed_hash = sha256_bytes(text.encode("utf-8"))
        declared_language = str(block.get("declared_language") or "unknown")
        normalized_language = normalize_language(block.get("normalized_language") or declared_language, text)
        span = block.get("source_span") if isinstance(block.get("source_span"), list) else None
        row = base_evidence(
            queue_row,
            bundle["bundle_id"],
            "article_fenced_block",
            ordinal,
            declared_language,
            normalized_language,
            declared_hash,
            observed_hash,
            str(article.get("source_locator") or bundle.get("source_relative_directory") or bundle["bundle_id"]),
            span,
        )
        row["article_declared_sha256"] = article_declared_hash
        row["article_observed_sha256"] = article_observed_hash
        row["article_hash_verified"] = bool(article_declared_hash and article_declared_hash == article_observed_hash)
        if article_error:
            mark_failure(row, article_error[0], article_error[1])
            items.append((row, None))
        elif declared_hash != observed_hash:
            mark_failure(row, "block_hash_mismatch", "Observed block text hash differs from SourceFlowBundle evidence.")
            items.append((row, None))
        else:
            items.append((row, text))
    return items


def markdown_blocks(text: str) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for ordinal, match in enumerate(FENCE_RE.finditer(text), start=1):
        info = match.group(1).strip()
        declared = info.split()[0].casefold() if info else "unknown"
        code = match.group(2).replace("\r\n", "\n")
        blocks.append({
            "ordinal": ordinal,
            "declared_language": declared,
            "normalized_language": normalize_language(declared, code),
            "sha256": sha256_bytes(code.encode("utf-8")),
            "text": code,
            "source_span": [text.count("\n", 0, match.start()) + 1, text.count("\n", 0, match.end()) + 1],
        })
    return blocks


def external_markdown_items(
    queue_row: dict[str, Any],
    target: dict[str, Any],
    skills_root: Path,
) -> list[tuple[dict[str, Any], str | None]]:
    target_skill = str(target.get("target_skill") or "")
    target_relative = str(target.get("target_relative_path") or "")
    declared_article_hash = str(target.get("sha256") or "") or None
    target_root = (skills_root / target_skill).resolve()
    path = (target_root / target_relative).resolve()
    try:
        path.relative_to(target_root)
    except ValueError:
        synthetic = base_evidence(queue_row, None, "external_markdown_fenced_block", 0, "unknown", "unknown", None, None, f"{target_skill}/{target_relative}")
        mark_failure(synthetic, "external_path_escape", "External target resolves outside the declared skill root.")
        return [(synthetic, None)]
    if not path.is_file():
        synthetic = base_evidence(queue_row, None, "external_markdown_fenced_block", 0, "unknown", "unknown", None, None, f"{target_skill}/{target_relative}")
        mark_failure(synthetic, "external_source_missing", "External Markdown target is missing.")
        return [(synthetic, None)]
    try:
        data = path.read_bytes()
    except OSError as exc:
        synthetic = base_evidence(queue_row, None, "external_markdown_fenced_block", 0, "unknown", "unknown", None, None, f"{target_skill}/{target_relative}")
        mark_failure(synthetic, "external_source_read_error", type(exc).__name__)
        return [(synthetic, None)]
    article_hash = sha256_bytes(data)
    text, encoding = decode_source(data)
    blocks = markdown_blocks(text)
    if declared_article_hash != article_hash:
        if not blocks:
            blocks = [{"ordinal": 0, "declared_language": "unknown", "normalized_language": "unknown", "sha256": None, "text": "", "source_span": None}]
        items: list[tuple[dict[str, Any], str | None]] = []
        for block in blocks:
            row = base_evidence(queue_row, None, "external_markdown_fenced_block", int(block["ordinal"]), str(block["declared_language"]), str(block["normalized_language"]), block.get("sha256"), block.get("sha256"), f"{target_skill}/{target_relative}", block.get("source_span"))
            row.update({"external_article_declared_sha256": declared_article_hash, "external_article_observed_sha256": article_hash, "source_encoding": encoding})
            mark_failure(row, "external_source_hash_mismatch", "Observed external Markdown hash differs from crosswalk evidence.")
            items.append((row, None))
        return items
    items = []
    for block in blocks:
        row = base_evidence(queue_row, None, "external_markdown_fenced_block", int(block["ordinal"]), str(block["declared_language"]), str(block["normalized_language"]), str(block["sha256"]), str(block["sha256"]), f"{target_skill}/{target_relative}", block.get("source_span"))
        row.update({"external_article_declared_sha256": declared_article_hash, "external_article_observed_sha256": article_hash, "external_article_hash_verified": True, "source_encoding": encoding})
        items.append((row, str(block["text"])))
    return items


def parse_python(row: dict[str, Any], text: str) -> None:
    row["parser"] = "python.ast.parse"
    try:
        ast.parse(text, filename=f"<{row['audit_item_id']}>", mode="exec")
    except SyntaxError as exc:
        row.update({
            "parse_status": "failed",
            "error_category": "python_syntax_error",
            "error_line": exc.lineno,
            "error_column": exc.offset,
            "source_error_line": source_line_for_item(row, exc.lineno),
            "error_summary": compact_summary(exc.msg),
        })
    except (ValueError, TypeError, MemoryError, RecursionError) as exc:
        mark_failure(row, "python_parser_error", type(exc).__name__)
    else:
        row["parse_status"] = "passed"


def mark_unsupported(row: dict[str, Any]) -> None:
    row["parser"] = "unsupported"
    row["parse_status"] = "unsupported"
    if row["normalized_language"] == "shell":
        row["error_category"] = "unsupported_shell"
        row["error_summary"] = "Shell-family code is inventoried but not parsed by this audit."
    else:
        row["error_category"] = "unsupported_language"
        row["error_summary"] = f"No syntax parser is configured for language: {row['normalized_language']}"


def parse_r_batch(
    pending: Sequence[tuple[dict[str, Any], str]],
    rscript: Path,
    timeout_seconds: int,
) -> None:
    if not pending:
        return
    if not rscript.is_file():
        for row, _ in pending:
            row["parser"] = "R::parse"
            mark_failure(row, "r_runtime_unavailable", f"Fixed Rscript is unavailable: {rscript}")
        return
    with tempfile.TemporaryDirectory(prefix="biomedical-source-parse-") as tmp:
        work = Path(tmp)
        input_dir = work / "r-input"
        input_dir.mkdir()
        result_path = work / "r-results.tsv"
        driver_path = work / "syntax-parser-driver.R"
        driver_path.write_text(R_BATCH_EXPRESSION + "\n", encoding="utf-8", newline="\n")
        id_to_row: dict[str, dict[str, Any]] = {}
        for index, (row, text) in enumerate(pending, start=1):
            parser_id = f"{index:06d}"
            (input_dir / f"{parser_id}.R").write_text(text, encoding="utf-8", newline="\n")
            row["parser"] = "R::parse"
            id_to_row[parser_id] = row
        command = [str(rscript), "--vanilla", str(driver_path), str(input_dir), str(result_path)]
        try:
            completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout_seconds, check=False)
        except subprocess.TimeoutExpired:
            for row, _ in pending:
                mark_failure(row, "r_parser_timeout", f"R syntax parser exceeded {timeout_seconds} seconds.")
            return
        except OSError as exc:
            for row, _ in pending:
                mark_failure(row, "r_parser_invocation_error", type(exc).__name__)
            return
        if completed.returncode != 0 or not result_path.is_file():
            summary = compact_summary(completed.stderr or completed.stdout or f"Rscript exit code {completed.returncode}")
            for row, _ in pending:
                mark_failure(row, "r_parser_invocation_error", summary)
            return
        seen: set[str] = set()
        for line in result_path.read_text(encoding="utf-8-sig").splitlines():
            parts = line.split("\t", 3)
            if len(parts) < 3 or parts[0] not in id_to_row:
                continue
            parser_id, status, category = parts[:3]
            message = parts[3] if len(parts) == 4 else ""
            seen.add(parser_id)
            row = id_to_row[parser_id]
            if status == "passed":
                row["parse_status"] = "passed"
                continue
            row["parse_status"] = "failed"
            row["error_category"] = category or "r_parse_error"
            location = R_LOCATION_RE.search(message)
            if location:
                row["error_line"] = int(location.group("line"))
                row["error_column"] = int(location.group("column"))
                row["source_error_line"] = source_line_for_item(row, row["error_line"])
                row["error_summary"] = compact_summary(location.group("message"))
            else:
                trailing_location = R_TRAILING_LOCATION_RE.search(message)
                if trailing_location:
                    row["error_line"] = int(trailing_location.group("line"))
                    row["error_column"] = int(trailing_location.group("column"))
                    row["source_error_line"] = source_line_for_item(row, row["error_line"])
                row["error_summary"] = compact_summary(R_TRAILING_LOCATION_RE.sub("(<source>)", message))
        for parser_id, row in id_to_row.items():
            if parser_id not in seen:
                mark_failure(row, "r_parser_result_missing", "R parser returned no result for this item.")


def attach_normalization_candidates(
    parseable: Sequence[tuple[dict[str, Any], str]],
    rscript: Path,
    timeout_seconds: int,
) -> None:
    """Attach parse evidence for review-only whitespace repair candidates.

    Raw parse dispositions remain authoritative.  Candidates are generated only
    for failed R/Python syntax items whose decoded text actually changes under
    the narrow Unicode-space profile.  A recovered parse is not a maturity or
    execution promotion.
    """

    pending_r: list[tuple[dict[str, Any], str]] = []
    candidates: list[tuple[dict[str, Any], dict[str, Any], str, str, dict[str, int]]] = []
    for row, text in parseable:
        if row.get("parse_status") != "failed" or row.get("normalized_language") not in {"r", "python"}:
            continue
        candidate_text, changes = unicode_space_candidate(text)
        if not changes or candidate_text == text:
            continue
        candidate_row = dict(row)
        candidate_row.update({
            "audit_item_id": f"{row['audit_item_id']}-{UNICODE_SPACE_NORMALIZATION_PROFILE}",
            "parser": None,
            "parse_status": "pending",
            "error_category": None,
            "error_line": None,
            "error_column": None,
            "source_error_line": None,
            "error_summary": None,
        })
        if row["normalized_language"] == "python":
            parse_python(candidate_row, candidate_text)
        else:
            pending_r.append((candidate_row, candidate_text))
        candidates.append((row, candidate_row, text, candidate_text, changes))
    parse_r_batch(pending_r, rscript, timeout_seconds)
    for row, candidate_row, original_text, candidate_text, changes in candidates:
        if candidate_row["parse_status"] == "pending":
            mark_failure(candidate_row, "internal_audit_error", "No candidate parser disposition was recorded.")
        row["normalization_candidate"] = {
            "profile": UNICODE_SPACE_NORMALIZATION_PROFILE,
            "change_counts": changes,
            "decoded_text_sha256": sha256_bytes(original_text.encode("utf-8")),
            "candidate_sha256": sha256_bytes(candidate_text.encode("utf-8")),
            "parser": candidate_row.get("parser"),
            "parse_status": candidate_row.get("parse_status"),
            "error_category": candidate_row.get("error_category"),
            "error_line": candidate_row.get("error_line"),
            "error_column": candidate_row.get("error_column"),
            "source_error_line": candidate_row.get("source_error_line"),
            "error_summary": candidate_row.get("error_summary"),
            "source_code_immutable": True,
            "automatic_promotion_allowed": False,
            "scientific_boundary": "Whitespace-normalized text is a repair candidate only; it was parsed but never evaluated, and must be reviewed and stored separately before use.",
        }


def collect_audit_items(
    batch_path: Path,
    index: Path,
    skills_root: Path,
) -> tuple[list[dict[str, Any]], list[tuple[dict[str, Any], str]]]:
    batch = read_jsonl(batch_path)
    bundle_ids = {str(bundle_id) for row in batch for bundle_id in row.get("linked_bundle_ids", [])}
    bundles: dict[str, dict[str, Any]] = {}
    with (index / "source-flow-bundles.jsonl").open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            bundle = json.loads(line)
            if bundle.get("bundle_id") in bundle_ids:
                bundles[str(bundle["bundle_id"])] = bundle
    evidence: list[dict[str, Any]] = []
    parseable: list[tuple[dict[str, Any], str]] = []
    for queue_row in batch:
        record_evidence_start = len(evidence)
        for bundle_id in queue_row.get("linked_bundle_ids", []):
            bundle = bundles.get(str(bundle_id))
            if bundle is None:
                row = base_evidence(queue_row, str(bundle_id), "bundle_inventory", 0, "unknown", "unknown", None, None, str(bundle_id))
                mark_failure(row, "bundle_missing", "Linked SourceFlowBundle is absent from the index.")
                evidence.append(row)
                continue
            for row, text in ordered_file_items(queue_row, bundle) + article_block_items(queue_row, bundle):
                evidence.append(row)
                if text is not None and row["parse_status"] == "pending":
                    parseable.append((row, text))
        for target in queue_row.get("external_targets", []):
            for row, text in external_markdown_items(queue_row, target, skills_root):
                evidence.append(row)
                if text is not None and row["parse_status"] == "pending":
                    parseable.append((row, text))
        if len(evidence) == record_evidence_start:
            row = base_evidence(
                queue_row,
                None,
                "no_code_inventory",
                0,
                "not_applicable",
                "not_applicable",
                None,
                None,
                "no-code-items-declared",
            )
            row.update({
                "parser": "not_applicable",
                "parse_status": "not_applicable",
                "disposition": "method-or-figure-context-only",
                "scientific_boundary": "No code item is available to parse; this record can support method/figure review only and cannot receive parse-verified maturity from this audit.",
            })
            evidence.append(row)
    evidence.sort(key=lambda row: (
        str(row.get("batch_id") or ""),
        str(row.get("preprocess_record_id") or ""),
        str(row.get("bundle_id") or "~external"),
        str(row.get("item_type") or ""),
        int(row.get("item_ordinal") or 0),
    ))
    return evidence, parseable


def audit_batch(
    batch_path: Path,
    index: Path = DEFAULT_INDEX,
    skills_root: Path = DEFAULT_SKILLS_ROOT,
    rscript: Path = DEFAULT_RSCRIPT,
    timeout_seconds: int = 180,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if timeout_seconds < 1:
        raise ValueError("timeout_seconds must be positive")
    evidence, parseable = collect_audit_items(batch_path, index, skills_root)
    r_pending: list[tuple[dict[str, Any], str]] = []
    for row, text in parseable:
        language = row["normalized_language"]
        if language == "python":
            parse_python(row, text)
        elif language == "r":
            r_pending.append((row, text))
        else:
            mark_unsupported(row)
    parse_r_batch(r_pending, rscript, timeout_seconds)
    attach_normalization_candidates(parseable, rscript, timeout_seconds)
    for row in evidence:
        if row["parse_status"] == "pending":
            mark_failure(row, "internal_audit_error", "No parser disposition was recorded.")
    status_counts = Counter(str(row["parse_status"]) for row in evidence)
    language_counts = Counter(str(row["normalized_language"]) for row in evidence)
    error_counts = Counter(str(row["error_category"]) for row in evidence if row.get("error_category"))
    status_by_language = Counter(f"{row['normalized_language']}::{row['parse_status']}" for row in evidence)
    status_by_item_type = Counter(f"{row['item_type']}::{row['parse_status']}" for row in evidence)
    parse_error_summaries = Counter(str(row["error_summary"]) for row in evidence if row.get("error_category") in {"r_parse_error", "python_syntax_error"})
    normalization_candidates = [row["normalization_candidate"] for row in evidence if row.get("normalization_candidate")]
    normalization_status_counts = Counter(str(candidate["parse_status"]) for candidate in normalization_candidates)
    record_ids = {str(row["preprocess_record_id"]) for row in evidence}
    bundle_ids = {str(row["bundle_id"]) for row in evidence if row.get("bundle_id")}
    batch_rows = read_jsonl(batch_path)
    expected_internal_items = sum(int(row.get("code_inventory_count") or 0) for row in batch_rows)
    observed_internal_items = sum(
        1 for row in evidence if row.get("item_type") in {"ordered_code_file", "article_fenced_block"}
    )
    external_target_count = sum(len(row.get("external_targets", [])) for row in batch_rows)
    observed_external_blocks = sum(1 for row in evidence if row.get("item_type") == "external_markdown_fenced_block")
    inventory_coverage_ok = (
        len(record_ids) == len(batch_rows)
        and observed_internal_items == expected_internal_items
    )
    batch_id = next((str(row.get("batch_id")) for row in batch_rows if row.get("batch_id")), batch_path.stem)
    no_code_records = sum(1 for row in evidence if row.get("item_type") == "no_code_inventory")
    code_item_count = len(evidence) - no_code_records
    summary = {
        "schema_version": "1.0",
        "generated_at": utc_now(),
        "batch_id": batch_id,
        "batch_file": batch_path.name,
        "batch_sha256": sha256_file(batch_path),
        "source_flow_bundles_sha256": sha256_file(index / "source-flow-bundles.jsonl"),
        "fixed_rscript": str(rscript),
        "rscript_available": rscript.is_file(),
        "records_declared": len(batch_rows),
        "records_with_evidence": len(record_ids),
        "bundles_with_evidence": len(bundle_ids),
        "expected_internal_code_items": expected_internal_items,
        "observed_internal_code_items": observed_internal_items,
        "external_targets": external_target_count,
        "observed_external_fenced_blocks": observed_external_blocks,
        "inventory_coverage_ok": inventory_coverage_ok,
        "audit_items": len(evidence),
        "code_items": code_item_count,
        "no_code_records": no_code_records,
        "status_counts": dict(sorted(status_counts.items())),
        "language_counts": dict(sorted(language_counts.items())),
        "error_category_counts": dict(sorted(error_counts.items())),
        "status_by_language": dict(sorted(status_by_language.items())),
        "status_by_item_type": dict(sorted(status_by_item_type.items())),
        "parse_error_summary_counts": dict(sorted(parse_error_summaries.items())),
        "normalization_profile": UNICODE_SPACE_NORMALIZATION_PROFILE,
        "normalization_candidates": len(normalization_candidates),
        "normalization_candidate_status_counts": dict(sorted(normalization_status_counts.items())),
        "normalization_recovered_items": sum(candidate["parse_status"] == "passed" for candidate in normalization_candidates),
        "all_items_dispositioned": inventory_coverage_ok and len(evidence) > 0 and sum(status_counts.values()) == len(evidence) and "pending" not in status_counts,
        "execution_boundary": "Only R::parse and Python ast.parse were used; article code was never evaluated, sourced, imported, or installed.",
        "maturity_boundary": "A passed syntax audit supports parse-verified evidence only and does not establish package availability, runtime success, statistical validity, or scientific correctness.",
        "normalization_boundary": "A normalization candidate never changes the raw parse disposition and cannot authorize execution or maturity promotion.",
    }
    return evidence, summary


def default_output_paths(batch_path: Path, audit_dir: Path) -> tuple[Path, Path]:
    stem = batch_path.stem
    return audit_dir / f"{stem}-syntax-audit.jsonl", audit_dir / f"{stem}-syntax-audit-summary.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", type=Path, required=True, help="Materialized high-value batch JSONL.")
    parser.add_argument("--index", type=Path, default=DEFAULT_INDEX, help="Private corpus index root.")
    parser.add_argument("--skills-root", type=Path, default=DEFAULT_SKILLS_ROOT, help="Installed skills root for external targets.")
    parser.add_argument("--timeout-seconds", type=int, default=180, help="Timeout for the single batched R parser process.")
    parser.add_argument("--output", type=Path, help="Evidence JSONL output path.")
    parser.add_argument("--summary", type=Path, help="Summary JSON output path.")
    parser.add_argument("--overwrite", action="store_true", help="Explicitly replace existing audit outputs.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output, summary_path = default_output_paths(args.batch, DEFAULT_AUDIT_DIR)
    output = args.output or output
    summary_path = args.summary or summary_path
    existing = [path for path in (output, summary_path) if path.exists()]
    if existing and not args.overwrite:
        raise FileExistsError("Refusing to overwrite existing audit output: " + ", ".join(str(path) for path in existing))
    evidence, summary = audit_batch(args.batch, args.index, args.skills_root, DEFAULT_RSCRIPT, args.timeout_seconds)
    summary["evidence_file"] = output.name
    summary["evidence_sha256"] = hashlib.sha256(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for row in evidence).encode("utf-8")
    ).hexdigest()
    write_jsonl_atomic(output, evidence)
    write_json_atomic(summary_path, summary)
    print(json.dumps({"ok": summary["all_items_dispositioned"], "output": str(output), "summary": str(summary_path), **summary}, ensure_ascii=False, indent=2))
    return 0 if summary["all_items_dispositioned"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
