# Retrieval benchmark contract

Use `retrieval-benchmark.jsonl` as the fixed, human-reviewable routing and gate gold set. It contains exactly 180 cases across six 30-case strata: visualization, single-cell, spatial transcriptomics, bulk RNA plus quantitative proteomics, multi-omics, and literature methodology.

Each JSONL object contains:

- `id`: stable case identifier;
- `stratum`: one of the six benchmark strata;
- `request`: a complete request accepted by `analysis_agent.py`;
- `expected_top1`: the unique primary capability;
- `allowed_top3`: primary and scientifically compatible complementary capabilities;
- `forbidden_non_equivalent`: capabilities that must not silently replace the primary method;
- `scientific_gate_tags`: gate identifiers that the real router/gate engine must emit.

Top-1 is exact equality with `expected_top1`. Top-3 uses the conventional definition: `expected_top1` must appear among the first three routed capabilities. Every emitted Top-3 capability must also belong to `allowed_top3`; this complementary-route allowlist does not weaken Top-3 accuracy. A forbidden substitution is counted when the actual Top-1 belongs to `forbidden_non_equivalent`. Scientific-gate coverage requires every tagged gate to be emitted, regardless of whether its status is `pass`, `deferred`, or `block` for the requested mode.

Run:

```powershell
python scripts/evaluate_retrieval_benchmark.py --output benchmark-report.json
```

The command fails unless Top-1 is at least 95%, Top-3 is at least 99%, forbidden substitutions and unlisted Top-3 candidates are zero, and gate-label coverage is complete.
