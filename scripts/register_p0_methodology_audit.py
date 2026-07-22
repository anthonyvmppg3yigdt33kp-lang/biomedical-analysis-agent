#!/usr/bin/env python3
"""Atomically register one private P0 explain-mode methodology audit."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

import jsonschema


SKILL_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = SKILL_ROOT / "assets" / "private-corpus-index" / "p0-methodology-audits.json"
DEFAULT_SCHEMA = SKILL_ROOT / "references" / "schemas" / "p0-methodology-audit.schema.json"
DEFAULT_CANDIDATES = SKILL_ROOT / "references" / "p0-teaching-cases.json"


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def register_audit(
    candidate_path: Path,
    registry_path: Path = DEFAULT_REGISTRY,
    schema_path: Path = DEFAULT_SCHEMA,
    public_candidates_path: Path = DEFAULT_CANDIDATES,
) -> dict[str, Any]:
    candidate = _load(candidate_path)
    schema = _load(schema_path)
    public_candidates = _load(public_candidates_path)
    public_by_id = {item["case_id"]: item for item in public_candidates.get("cases", [])}
    public = public_by_id.get(candidate.get("case_id"))
    if public is None:
        raise ValueError(f"unknown_public_case:{candidate.get('case_id')}")
    if public.get("domain") != candidate.get("domain"):
        raise ValueError("public_case_domain_mismatch")
    if public.get("workflow_plan", {}).get("allowed_mode") != "explain":
        raise ValueError("public_case_not_explain_only")
    if public.get("execution_status") != "not-executed":
        raise ValueError("public_case_was_promoted")

    if registry_path.exists():
        registry = _load(registry_path)
    else:
        registry = {
            "schema_version": "1.0",
            "registry_id": "biomedical-analysis-agent-p0-methodology-audits",
            "distribution": "private-local-only",
            "source_mode": "read-only-evidence",
            "public_candidate_registry_ref": "references/p0-teaching-cases.json",
            "audits": [],
        }
    audits = registry.setdefault("audits", [])
    if any(item.get("audit_id") == candidate.get("audit_id") for item in audits):
        raise ValueError(f"duplicate_audit_id:{candidate.get('audit_id')}")
    if any(item.get("case_id") == candidate.get("case_id") for item in audits):
        raise ValueError(f"duplicate_case_audit:{candidate.get('case_id')}")
    audits.append(candidate)
    jsonschema.Draft202012Validator(
        schema, format_checker=jsonschema.FormatChecker()
    ).validate(registry)

    registry_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(registry, ensure_ascii=False, indent=2) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=registry_path.name + ".", suffix=".tmp", dir=registry_path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, registry_path)
    finally:
        temporary = Path(temporary_name)
        if temporary.exists():
            temporary.unlink()
    return registry


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    parser.add_argument("--public-candidates", type=Path, default=DEFAULT_CANDIDATES)
    args = parser.parse_args()
    registry = register_audit(
        args.candidate, args.registry, args.schema, args.public_candidates
    )
    print(json.dumps({"ok": True, "audit_count": len(registry["audits"])}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
