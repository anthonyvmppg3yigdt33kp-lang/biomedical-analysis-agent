from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

import jsonschema


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from register_p0_methodology_audit import register_audit  # noqa: E402
from validate_p0_methodology_audits import validate_methodology_audits  # noqa: E402


REGISTRY = SKILL_ROOT / "assets" / "private-corpus-index" / "p0-methodology-audits.json"
SCHEMA = SKILL_ROOT / "references" / "schemas" / "p0-methodology-audit.schema.json"
CANDIDATES = SKILL_ROOT / "references" / "p0-teaching-cases.json"
LOCATORS = SKILL_ROOT / "assets" / "private-corpus-index" / "p0-teaching-case-local-availability.json"
PRIVATE_AUDIT_AVAILABLE = REGISTRY.is_file() and LOCATORS.is_file()


@unittest.skipUnless(PRIVATE_AUDIT_AVAILABLE, "Private methodology-audit evidence is absent from a public-core clone")
class MethodologyAuditContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
        cls.schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        cls.validator = jsonschema.Draft202012Validator(
            cls.schema, format_checker=jsonschema.FormatChecker()
        )

    def test_private_registry_satisfies_strict_union_contract(self) -> None:
        self.validator.validate(self.registry)
        self.assertEqual(len(self.registry["audits"]), 1)
        audit = self.registry["audits"][0]
        self.assertEqual(audit["domain"], "literature-methodology")
        self.assertEqual(audit["authorization"]["mode"], "explain")
        self.assertFalse(audit["authorization"]["source_code_execution"])
        self.assertFalse(audit["authorization"]["package_installation"])

    def test_schema_rejects_execution_or_scientific_result_promotion(self) -> None:
        promoted = copy.deepcopy(self.registry)
        promoted["audits"][0]["authorization"]["source_code_execution"] = True
        promoted["audits"][0]["native_review"]["decision"] = "PASS"
        promoted["audits"][0]["maturity"] = "data-verified"
        self.assertTrue(list(self.validator.iter_errors(promoted)))

    def test_atomic_registrar_refuses_duplicate_audit_and_case(self) -> None:
        audit = self.registry["audits"][0]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate_path = root / "candidate.json"
            registry_path = root / "registry.json"
            candidate_path.write_text(
                json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            first = register_audit(candidate_path, registry_path, SCHEMA, CANDIDATES)
            self.assertEqual(len(first["audits"]), 1)
            with self.assertRaisesRegex(ValueError, "duplicate_audit_id"):
                register_audit(candidate_path, registry_path, SCHEMA, CANDIDATES)

    def test_live_validator_rehashes_sources_checkpoints_cards_and_delivery(self) -> None:
        report = validate_methodology_audits(
            REGISTRY, SCHEMA, CANDIDATES, LOCATORS, verify_live=True
        )
        self.assertTrue(report["ok"], report["errors"])
        self.assertEqual(report["audit_count"], 1)
        self.assertEqual(report["verified_source_files"], 3)
        self.assertEqual(report["verified_checkpoints"], 4)
        self.assertEqual(report["verified_formal_artifacts"], 19)
        self.assertEqual(report["verified_delivery_files"], 15)
        self.assertEqual(report["verified_native_figures"], 1)


if __name__ == "__main__":
    unittest.main()
