import importlib.util
import json
import sys
import unittest
from pathlib import Path

import jsonschema


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "normalize_knowledge_registry.py"
SPEC = importlib.util.spec_from_file_location("normalize_knowledge_registry", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)
SCHEMA_ROOT = Path(__file__).parents[1] / "references" / "schemas"


def validate(kind, value):
    bundle = json.loads((SCHEMA_ROOT / "knowledge-objects.schema.json").read_text(encoding="utf-8"))
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$ref": f"#/$defs/{MODULE.KIND_TO_DEFINITION[kind]}",
        "$defs": bundle["$defs"],
    }
    jsonschema.Draft202012Validator(schema).validate(value)


class KnowledgeNormalizationTests(unittest.TestCase):
    def setUp(self):
        self.bundle = {
            "bundle_id": "flow-test",
            "title": "Test workflow",
            "private_source_directory": "D:/private/article",
            "article": {"source_locator": "D:/private/article/article.md", "sha256": "a" * 64, "fenced_code_blocks": []},
            "ordered_code_files": [
                {
                    "ordinal": 1,
                    "normalized_language": "r",
                    "source_locator": "D:/private/article/code/s01.R",
                    "sha256": "b" * 64,
                    "static_facts": {"assignments": ["x"], "input_calls": ["read.csv"], "output_calls": [], "installer_calls": [], "absolute_paths": []},
                }
            ],
            "images": [
                {"source_locator": "D:/private/article/images/a.png", "sha256": "c" * 64, "ordinal": 1, "width": 10, "height": 20}
            ],
            "package_index": {"packages": ["Seurat"]},
            "issues": [],
            "flow_integrity": {"flow_fingerprint_sha256": "d" * 64},
        }
        self.bundles = {"flow-test": self.bundle}

    def test_source_bundle_conforms_to_shared_schema(self):
        value = MODULE.normalize_source_bundle(self.bundle)
        validate("source-flow-bundle", value)
        self.assertEqual(value["ordered_code"][0]["produces"], ["x"])

    def test_article_without_code_is_not_fabricated_as_flow(self):
        value = dict(self.bundle, ordered_code_files=[], article=dict(self.bundle["article"], fenced_code_blocks=[]))
        self.assertIsNone(MODULE.normalize_source_bundle(value))

    def test_method_card_conforms(self):
        value = MODULE.normalize_method_card(
            {
                "method_card_id": "method-test",
                "title": "Test",
                "source_bundle_ids": ["flow-test"],
                "research_question_hints": ["What changes?"],
                "data_types": ["counts"],
                "analysis_unit": "patient",
                "method_sequence": ["QC", "model"],
                "limitations": ["observational"],
                "required_validation": ["FDR"],
                "claim_ceiling": "association_only",
            },
            self.bundles,
        )
        validate("method-card", value)

    def test_unknown_package_source_is_explicit_and_non_executable(self):
        value = MODULE.normalize_package_card(
            {"package_card_id": "package-test", "package": "X", "language": "r", "canonical_source": "unknown_requires_review", "source_bundle_ids": ["flow-test"]},
            self.bundles,
        )
        validate("package-card", value)
        self.assertEqual(value["source"], "unknown")
        self.assertTrue(value["known_failures"])

    def test_unreviewed_figure_is_blocked(self):
        value = MODULE.normalize_figure_card(
            {"figure_card_id": "figure-test", "source_bundle_id": "flow-test", "private_source_locator": "D:/private/article/images/a.png", "sha256": "c" * 64, "dimensions": {"width": 10, "height": 20}, "does_not_support": []},
            self.bundles,
            {},
        )
        validate("figure-card", value)
        self.assertEqual(value["visual_qa"], "block")
        self.assertEqual(value["maturity"], "raw-extracted")

    def test_alternative_variant_never_auto_falls_back(self):
        value = MODULE.normalize_variant_set(
            {"capability_module_id": "cap-test", "capability": "test", "variants": [{"variant_id": "v1", "source_bundle_id": "flow-test", "equivalence": "alternative_method"}]},
            self.bundles,
        )
        validate("variant-set", value)
        self.assertFalse(value["variants"][0]["auto_fallback"])


if __name__ == "__main__":
    unittest.main()
