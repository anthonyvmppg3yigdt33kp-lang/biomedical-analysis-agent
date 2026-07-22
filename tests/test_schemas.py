import json
import unittest
from pathlib import Path


SCHEMA_DIR = Path(__file__).resolve().parents[1] / "references" / "schemas"
EXPECTED = {
    "SourceFlowBundle": "source-flow-bundle.schema.json",
    "MethodCard": "method-card.schema.json",
    "PackageCard": "package-card.schema.json",
    "AnalysisRecipe": "analysis-recipe.schema.json",
    "WorkflowTemplate": "workflow-template.schema.json",
    "WorkflowInstance": "workflow-instance.schema.json",
    "FigureCard": "figure-card.schema.json",
    "ArtifactContract": "artifact-contract.schema.json",
    "RunManifest": "run-manifest.schema.json",
    "VariantSet": "variant-set.schema.json",
    "PreprocessingRecord": "preprocessing-record.schema.json",
    "PreprocessingCrosswalk": "preprocessing-crosswalk.schema.json",
}
STANDALONE = {
    "DistillationReviewRecord": "distillation-review-record.schema.json",
    "P0TeachingCaseRegistry": "p0-teaching-case.schema.json",
    "P0TeachingCaseExecutionRegistry": "p0-teaching-case-execution.schema.json",
    "P0MethodologyAuditRegistry": "p0-methodology-audit.schema.json",
}


class SchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = json.loads((SCHEMA_DIR / "knowledge-objects.schema.json").read_text(encoding="utf-8"))

    def test_all_json_files_parse(self):
        files = sorted(SCHEMA_DIR.glob("*.json"))
        self.assertEqual(len(files), len(EXPECTED) + len(STANDALONE) + 1)
        for path in files:
            with self.subTest(path=path.name):
                value = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(value["$schema"], "https://json-schema.org/draft/2020-12/schema")

    def test_standalone_schemas_are_explicitly_registered(self):
        for title, filename in STANDALONE.items():
            with self.subTest(title=title):
                value = json.loads((SCHEMA_DIR / filename).read_text(encoding="utf-8"))
                self.assertEqual(value["title"], title)
                self.assertNotIn("$ref", value)

    def test_required_definitions_and_wrappers_exist(self):
        definitions = self.bundle["$defs"]
        for title, filename in EXPECTED.items():
            with self.subTest(title=title):
                self.assertIn(title, definitions)
                wrapper = json.loads((SCHEMA_DIR / filename).read_text(encoding="utf-8"))
                self.assertEqual(wrapper["title"], title)
                self.assertEqual(wrapper["$ref"], f"knowledge-objects.schema.json#/$defs/{title}")

    def test_maturity_order_is_canonical(self):
        self.assertEqual(
            self.bundle["$defs"]["Maturity"]["enum"],
            ["raw-extracted", "normalized", "parse-verified", "fixture-verified", "data-verified", "native-reviewed"],
        )

    def test_analysis_recipe_cannot_contain_installer_commands(self):
        recipe = self.bundle["$defs"]["AnalysisRecipe"]
        self.assertFalse(recipe["additionalProperties"])
        self.assertNotIn("installation_commands", recipe["properties"])
        self.assertIn("install\\.packages", recipe["properties"]["steps"]["items"]["not"]["pattern"])

    def test_alternative_method_cannot_auto_fallback(self):
        variant = self.bundle["$defs"]["VariantSet"]["properties"]["variants"]["items"]
        rule = variant["allOf"][0]
        self.assertEqual(rule["if"]["not"]["properties"]["classification"]["const"], "exact")
        self.assertFalse(rule["then"]["properties"]["auto_fallback"]["const"])


if __name__ == "__main__":
    unittest.main()
