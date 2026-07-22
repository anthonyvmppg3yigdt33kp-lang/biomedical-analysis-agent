#!/usr/bin/env python3
"""Atomically append or evidence-correct one private P0 execution record."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import jsonschema


SKILL_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = SKILL_ROOT / "assets" / "private-corpus-index" / "p0-teaching-case-executions.json"
DEFAULT_SCHEMA = SKILL_ROOT / "references" / "schemas" / "p0-teaching-case-execution.schema.json"
DEFAULT_CANDIDATES = SKILL_ROOT / "references" / "p0-teaching-cases.json"


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def register_execution(
    candidate_path: Path,
    registry_path: Path = DEFAULT_REGISTRY,
    schema_path: Path = DEFAULT_SCHEMA,
    candidates_path: Path = DEFAULT_CANDIDATES,
    *,
    replace_existing: bool = False,
) -> dict[str, Any]:
    candidate = load_json(candidate_path)
    registry = load_json(registry_path)
    schema = load_json(schema_path)
    candidates = load_json(candidates_path)
    executions = registry.get("executions", [])
    execution_id = candidate.get("execution_id")
    case_id = candidate.get("case_id")
    id_matches = [index for index, item in enumerate(executions) if item.get("execution_id") == execution_id]
    case_matches = [index for index, item in enumerate(executions) if item.get("case_id") == case_id]
    if replace_existing:
        if len(id_matches) != 1 or id_matches != case_matches:
            raise ValueError(f"replace_requires_exact_existing_execution:{execution_id}:{case_id}")
    else:
        if id_matches:
            raise ValueError(f"duplicate_execution_id:{execution_id}")
        if case_matches:
            raise ValueError(f"duplicate_case_execution:{case_id}")
    public_case = next(
        (item for item in candidates.get("cases", []) if item.get("case_id") == case_id),
        None,
    )
    if public_case is None:
        raise ValueError(f"unknown_public_case:{case_id}")
    if public_case.get("domain") != candidate.get("domain"):
        raise ValueError(f"domain_mismatch:{case_id}")
    if public_case.get("execution_status") != "not-executed":
        raise ValueError(f"public_execution_overlay_not_immutable:{case_id}")

    updated = dict(registry)
    if replace_existing:
        revised = list(executions)
        revised[id_matches[0]] = candidate
        updated["executions"] = revised
    else:
        updated["executions"] = [*executions, candidate]
    jsonschema.Draft202012Validator(
        schema,
        format_checker=jsonschema.FormatChecker(),
    ).validate(updated)
    payload = json.dumps(updated, ensure_ascii=False, indent=2) + "\n"
    temporary = registry_path.with_name(f".{registry_path.name}.{os.getpid()}.tmp")
    if temporary.exists():
        raise FileExistsError(f"temporary_path_exists:{temporary}")
    try:
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, registry_path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return {
        "ok": True,
        "execution_id": execution_id,
        "case_id": case_id,
        "execution_count": len(updated["executions"]),
        "action": "replaced" if replace_existing else "appended",
        "registry": str(registry_path),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    parser.add_argument("--candidates", type=Path, default=DEFAULT_CANDIDATES)
    parser.add_argument(
        "--replace-existing",
        action="store_true",
        help="Replace exactly one record only when execution_id and case_id both match.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = register_execution(
            args.candidate.resolve(),
            args.registry.resolve(),
            args.schema.resolve(),
            args.candidates.resolve(),
            replace_existing=args.replace_existing,
        )
    except (OSError, ValueError, json.JSONDecodeError, jsonschema.ValidationError) as exc:
        result = {"ok": False, "error": str(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
