#!/usr/bin/env python3
"""Evaluate the deterministic biomedical request router on a fixed JSONL gold set.

The evaluator imports and calls ``analysis_agent.route_request`` and
``analysis_agent.scientific_gates`` directly.  It does not duplicate routing
logic and does not generate benchmark prompts at runtime.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from analysis_agent import route_request, scientific_gates


DEFAULT_BENCHMARK = Path(__file__).resolve().parents[1] / "references" / "retrieval-benchmark.jsonl"
REQUIRED_FIELDS = {
    "id",
    "stratum",
    "request",
    "expected_top1",
    "allowed_top3",
    "forbidden_non_equivalent",
    "scientific_gate_tags",
}


class BenchmarkError(ValueError):
    """Raised when the fixed gold set is malformed."""


def load_cases(path: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            if not raw_line.strip():
                continue
            try:
                case = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise BenchmarkError(f"line {line_number}: invalid JSON: {exc}") from exc
            if not isinstance(case, dict):
                raise BenchmarkError(f"line {line_number}: case must be an object")
            missing = REQUIRED_FIELDS - set(case)
            if missing:
                raise BenchmarkError(f"line {line_number}: missing fields {sorted(missing)}")
            case_id = str(case["id"])
            if case_id in seen:
                raise BenchmarkError(f"line {line_number}: duplicate id {case_id}")
            seen.add(case_id)
            if not isinstance(case["request"], dict):
                raise BenchmarkError(f"line {line_number}: request must be an object")
            for field in ("allowed_top3", "forbidden_non_equivalent", "scientific_gate_tags"):
                if not isinstance(case[field], list):
                    raise BenchmarkError(f"line {line_number}: {field} must be a list")
            if case["expected_top1"] not in case["allowed_top3"]:
                raise BenchmarkError(f"line {line_number}: expected_top1 must be in allowed_top3")
            if set(case["allowed_top3"]) & set(case["forbidden_non_equivalent"]):
                raise BenchmarkError(f"line {line_number}: allowed and forbidden capabilities overlap")
            cases.append(case)
    if not cases:
        raise BenchmarkError("benchmark is empty")
    return cases


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def evaluate_cases(cases: Iterable[dict[str, Any]]) -> dict[str, Any]:
    case_list = list(cases)
    failures: list[dict[str, Any]] = []
    by_stratum: dict[str, Counter[str]] = {}
    totals: Counter[str] = Counter()

    for case in case_list:
        request = case["request"]
        routes = route_request(request)
        gates = scientific_gates(request, routes)
        capabilities = [route["capability"] for route in routes]
        actual_top1 = capabilities[0]
        actual_top3 = capabilities[:3]
        expected = case["expected_top1"]
        forbidden = set(case["forbidden_non_equivalent"])
        expected_gates = set(case["scientific_gate_tags"])
        actual_gate_ids = {gate["id"] for gate in gates}

        top1_ok = actual_top1 == expected
        # Conventional Top-3 accuracy: the unique primary gold label must occur
        # in the first three routes. allowed_top3 records scientifically
        # acceptable complementary/fallback capabilities for review, but does
        # not dilute this metric.
        top3_ok = expected in actual_top3
        forbidden_substitution = actual_top1 in forbidden
        gates_ok = expected_gates <= actual_gate_ids
        unexpected_top3 = [item for item in actual_top3 if item not in set(case["allowed_top3"])]

        stat = by_stratum.setdefault(case["stratum"], Counter())
        for counter in (totals, stat):
            counter["cases"] += 1
            counter["top1_correct"] += int(top1_ok)
            counter["top3_correct"] += int(top3_ok)
            counter["forbidden_substitutions"] += int(forbidden_substitution)
            counter["gate_coverage_correct"] += int(gates_ok)
            counter["top3_allowlist_correct"] += int(not unexpected_top3)
            counter["unexpected_top3_candidates"] += len(unexpected_top3)

        if not (top1_ok and top3_ok and not forbidden_substitution and gates_ok and not unexpected_top3):
            failures.append(
                {
                    "id": case["id"],
                    "stratum": case["stratum"],
                    "expected_top1": expected,
                    "actual_top1": actual_top1,
                    "actual_top3": actual_top3,
                    "allowed_top3": case["allowed_top3"],
                    "forbidden_non_equivalent": sorted(forbidden),
                    "forbidden_substitution": forbidden_substitution,
                    "missing_scientific_gates": sorted(expected_gates - actual_gate_ids),
                    "unexpected_top3_candidates": unexpected_top3,
                    "routes": routes[:3],
                }
            )

    def summarize(counter: Counter[str]) -> dict[str, Any]:
        count = counter["cases"]
        return {
            "cases": count,
            "top1_correct": counter["top1_correct"],
            "top1_accuracy": _ratio(counter["top1_correct"], count),
            "top3_correct": counter["top3_correct"],
            "top3_accuracy": _ratio(counter["top3_correct"], count),
            "forbidden_substitutions": counter["forbidden_substitutions"],
            "gate_coverage_correct": counter["gate_coverage_correct"],
            "gate_coverage_accuracy": _ratio(counter["gate_coverage_correct"], count),
            "top3_allowlist_correct": counter["top3_allowlist_correct"],
            "top3_allowlist_accuracy": _ratio(counter["top3_allowlist_correct"], count),
            "unexpected_top3_candidates": counter["unexpected_top3_candidates"],
        }

    return {
        "schema_version": "1.0.0",
        "benchmark_cases": len(case_list),
        "overall": summarize(totals),
        "by_stratum": {name: summarize(counter) for name, counter in sorted(by_stratum.items())},
        "failures": failures,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark", type=Path, default=DEFAULT_BENCHMARK)
    parser.add_argument("--output", type=Path, help="Write the full JSON report here")
    parser.add_argument("--min-top1", type=float, default=0.95)
    parser.add_argument("--min-top3", type=float, default=0.99)
    parser.add_argument("--max-forbidden-substitutions", type=int, default=0)
    parser.add_argument(
        "--require-gate-coverage",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cases = load_cases(args.benchmark)
        report = evaluate_cases(cases)
    except (OSError, BenchmarkError, ValueError) as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        return 2

    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    else:
        sys.stdout.write(payload)

    overall = report["overall"]
    passed = (
        overall["top1_accuracy"] >= args.min_top1
        and overall["top3_accuracy"] >= args.min_top3
        and overall["forbidden_substitutions"] <= args.max_forbidden_substitutions
        and overall["top3_allowlist_accuracy"] == 1.0
        and (not args.require_gate_coverage or overall["gate_coverage_accuracy"] == 1.0)
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
