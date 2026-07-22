import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "audit_source_syntax.py"
SPEC = importlib.util.spec_from_file_location("audit_source_syntax", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def write_jsonl(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def code_file(path: Path, language: str, ordinal: int):
    return {
        "ordinal": ordinal,
        "source_locator": str(path),
        "source_relative_path": path.name,
        "relative_to_bundle": f"code/{path.name}",
        "declared_language": language,
        "normalized_language": MODULE.normalize_language(language),
        "sha256": MODULE.sha256_file(path),
    }


class SourceSyntaxAuditTests(unittest.TestCase):
    def build_fixture(self, base: Path, ordered, article_text: str, external_text: str | None = None):
        index = base / "index"
        source = base / "source"
        source.mkdir(parents=True, exist_ok=True)
        article = source / "article.md"
        article.write_bytes(article_text.encode("utf-8"))
        bundle = {
            "bundle_id": "flow-fixture",
            "source_relative_directory": "fixture",
            "ordered_code_files": ordered,
            "article": {
                "source_locator": str(article),
                "sha256": MODULE.sha256_file(article),
                "fenced_code_blocks": MODULE.markdown_blocks(article_text),
            },
        }
        write_jsonl(index / "source-flow-bundles.jsonl", [bundle])
        queue_row = {
            "batch_id": "high-value-fixture",
            "queue_item_id": "distill-review-fixture",
            "preprocess_record_id": "prep-fixture",
            "record_sha256": "a" * 64,
            "linked_bundle_ids": ["flow-fixture"],
            "external_targets": [],
            "code_inventory_count": len(ordered) + len(bundle["article"]["fenced_code_blocks"]),
        }
        skills_root = base / "skills"
        if external_text is not None:
            external = skills_root / "visualization-2026718-v1" / "assets" / "source_archive" / "external.md"
            external.parent.mkdir(parents=True, exist_ok=True)
            external.write_bytes(external_text.encode("utf-8"))
            queue_row["external_targets"] = [{
                "target_skill": "visualization-2026718-v1",
                "target_relative_path": "assets/source_archive/external.md",
                "sha256": MODULE.sha256_file(external),
            }]
        batch = base / "high-value-fixture.jsonl"
        write_jsonl(batch, [queue_row])
        return index, batch, skills_root

    def test_python_shell_and_external_markdown_are_dispositioned_without_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "source"
            source.mkdir()
            sentinel = base / "must-not-exist.txt"
            valid_python = source / "valid.py"
            valid_python.write_text(
                f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('executed')\n",
                encoding="utf-8",
            )
            shell = source / "commands.sh"
            shell.write_text("echo must-not-run\n", encoding="utf-8")
            article_text = "# Fixture\n\n```python\ndef broken(:\n    pass\n```\n"
            external_text = "# External\n\n```python\nvalue = 1\n```\n"
            ordered = [code_file(valid_python, "python", 1), code_file(shell, "bash", 2)]
            index, batch, skills_root = self.build_fixture(base, ordered, article_text, external_text)
            rows, summary = MODULE.audit_batch(
                batch,
                index=index,
                skills_root=skills_root,
                rscript=base / "missing-Rscript.exe",
            )
            self.assertFalse(sentinel.exists())
            self.assertEqual(summary["code_items"], 4)
            self.assertEqual(summary["status_counts"], {"failed": 1, "passed": 2, "unsupported": 1})
            self.assertTrue(summary["all_items_dispositioned"])
            self.assertEqual(len({row["audit_item_id"] for row in rows}), 4)
            errors = {row["error_category"] for row in rows if row["error_category"]}
            self.assertIn("python_syntax_error", errors)
            self.assertIn("unsupported_shell", errors)
            external = next(row for row in rows if row["item_type"] == "external_markdown_fenced_block")
            self.assertTrue(external["external_article_hash_verified"])
            self.assertEqual(external["parse_status"], "passed")

    @unittest.skipUnless(MODULE.DEFAULT_RSCRIPT.is_file(), "fixed R 4.5.3 runtime is unavailable")
    def test_r_parse_uses_fixed_parser_and_never_evaluates_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "source"
            source.mkdir()
            sentinel = (base / "must-not-exist-r.txt").as_posix()
            valid_r = source / "valid.R"
            valid_r.write_text(f'writeLines("executed", "{sentinel}")\n', encoding="utf-8")
            article_text = "# Fixture\n\n```r\nx <-\n```\n"
            index, batch, skills_root = self.build_fixture(base, [code_file(valid_r, "r", 1)], article_text)
            rows, summary = MODULE.audit_batch(batch, index=index, skills_root=skills_root)
            self.assertFalse(Path(sentinel).exists())
            self.assertEqual(summary["status_counts"], {"failed": 1, "passed": 1})
            passed = next(row for row in rows if row["parse_status"] == "passed")
            failed = next(row for row in rows if row["parse_status"] == "failed")
            self.assertEqual(passed["parser"], "R::parse")
            self.assertEqual(failed["error_category"], "r_parse_error")
            self.assertIsInstance(failed["error_line"], int)
            self.assertGreater(failed["source_error_line"], failed["error_line"])
            self.assertNotIn("x <-", failed["error_summary"])

    @unittest.skipUnless(MODULE.DEFAULT_RSCRIPT.is_file(), "fixed R 4.5.3 runtime is unavailable")
    def test_r_parse_non_ascii_diagnostic_does_not_abort_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "source"
            source.mkdir()
            valid_r = source / "valid.R"
            valid_r.write_text("value <- 1\n", encoding="utf-8")
            article_text = "# Fixture\n\n```r\n调整轴标签 <-\n```\n"
            index, batch, skills_root = self.build_fixture(base, [code_file(valid_r, "r", 1)], article_text)
            rows, summary = MODULE.audit_batch(batch, index=index, skills_root=skills_root)
            self.assertEqual(summary["status_counts"], {"failed": 1, "passed": 1})
            failed = next(row for row in rows if row["parse_status"] == "failed")
            self.assertEqual(failed["error_category"], "r_parse_error")
            self.assertNotEqual(failed["error_category"], "r_parser_invocation_error")

    def test_hash_mismatch_is_reported_and_not_parsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "source"
            source.mkdir()
            changed = source / "changed.py"
            changed.write_text("value = 1\n", encoding="utf-8")
            item = code_file(changed, "python", 1)
            item["sha256"] = "0" * 64
            index, batch, skills_root = self.build_fixture(base, [item], "# no fenced code\n")
            rows, summary = MODULE.audit_batch(batch, index=index, skills_root=skills_root)
            self.assertEqual(summary["status_counts"], {"failed": 1})
            self.assertEqual(rows[0]["error_category"], "source_hash_mismatch")
            self.assertIsNone(rows[0]["parser"])

    def test_unicode_nbsp_repair_candidate_is_separate_from_raw_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "source"
            source.mkdir()
            broken = source / "nbsp.py"
            broken.write_text("value\u00a0=\u00a01\n", encoding="utf-8")
            index, batch, skills_root = self.build_fixture(
                base,
                [code_file(broken, "python", 1)],
                "# no fenced code\n",
            )
            rows, summary = MODULE.audit_batch(batch, index=index, skills_root=skills_root)
            self.assertEqual(summary["status_counts"], {"failed": 1})
            self.assertEqual(summary["normalization_candidates"], 1)
            self.assertEqual(summary["normalization_recovered_items"], 1)
            candidate = rows[0]["normalization_candidate"]
            self.assertEqual(candidate["profile"], "unicode-space-v1")
            self.assertEqual(candidate["change_counts"], {"U+00A0->U+0020": 2})
            self.assertEqual(candidate["parse_status"], "passed")
            self.assertFalse(candidate["automatic_promotion_allowed"])
            self.assertTrue(candidate["source_code_immutable"])
            self.assertEqual(rows[0]["parse_status"], "failed")

    def test_unicode_space_inside_valid_string_is_not_rewritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "source"
            source.mkdir()
            valid = source / "valid.py"
            valid.write_text('value = "\u00a0"\n', encoding="utf-8")
            index, batch, skills_root = self.build_fixture(
                base,
                [code_file(valid, "python", 1)],
                "# no fenced code\n",
            )
            rows, summary = MODULE.audit_batch(batch, index=index, skills_root=skills_root)
            self.assertEqual(rows[0]["parse_status"], "passed")
            self.assertNotIn("normalization_candidate", rows[0])
            self.assertEqual(summary["normalization_candidates"], 0)

    def test_record_without_code_is_explicitly_not_applicable(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            index, batch, skills_root = self.build_fixture(base, [], "# Method-only article\n")
            rows, summary = MODULE.audit_batch(
                batch,
                index=index,
                skills_root=skills_root,
                rscript=base / "missing-Rscript.exe",
            )
            self.assertTrue(summary["inventory_coverage_ok"])
            self.assertTrue(summary["all_items_dispositioned"])
            self.assertEqual(summary["records_with_evidence"], 1)
            self.assertEqual(summary["audit_items"], 1)
            self.assertEqual(summary["code_items"], 0)
            self.assertEqual(summary["no_code_records"], 1)
            self.assertEqual(summary["status_counts"], {"not_applicable": 1})
            self.assertEqual(rows[0]["item_type"], "no_code_inventory")
            self.assertEqual(rows[0]["parse_status"], "not_applicable")
            self.assertIn("cannot receive parse-verified", rows[0]["scientific_boundary"])


if __name__ == "__main__":
    unittest.main()
