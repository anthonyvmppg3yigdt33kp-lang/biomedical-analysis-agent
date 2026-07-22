import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from evaluate_retrieval_benchmark import evaluate_cases, load_cases  # noqa: E402
from analysis_agent import route_request  # noqa: E402


def test_cross_domain_confusion_regressions_are_exact():
    cases = load_cases(ROOT / "references" / "router-confusion-regressions.jsonl")
    assert len(cases) == 7
    report = evaluate_cases(cases)
    assert report["failures"] == []
    assert report["overall"]["top1_accuracy"] == 1.0
    assert report["overall"]["top3_accuracy"] == 1.0
    assert report["overall"]["top3_allowlist_accuracy"] == 1.0
    assert report["overall"]["forbidden_substitutions"] == 0


def test_spatial_suppression_and_explicit_reference_dual_route():
    cases = {
        case["id"]: case["request"]
        for case in load_cases(ROOT / "references" / "router-confusion-regressions.jsonl")
    }
    expected = {
        "confusion-01": {"spatial-transcriptomics"},
        "confusion-02": {"single-cell"},
        "confusion-03": {"spatial-transcriptomics", "single-cell"},
        "confusion-04": {"spatial-transcriptomics"},
        "confusion-05": {"spatial-transcriptomics"},
        "confusion-06": {"single-cell"},
        "confusion-07": {"spatial-transcriptomics"},
    }
    for case_id, capabilities in expected.items():
        observed = {route["capability"] for route in route_request(cases[case_id])}
        assert observed == capabilities, (case_id, observed)
