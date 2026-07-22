# P0 teaching-case contract

`p0-teaching-cases.json` is a public-core metadata registry for the seven first-line teaching candidates: visualization, single-cell, spatial transcriptomics, bulk RNA, quantitative proteomics, multi-omics and literature methodology.

## Safety boundary

- A registry entry is a candidate, not an execution record. Every entry is `not-executed` and may be only `raw-extracted` or `normalized`.
- `fixture-verified` and `data-verified` require a separate authorized run, frozen environment, hash-valid checkpoints and scientific QA.
- The public registry contains only stable `local:*` references, hashes, sizes and non-sensitive provenance. Absolute paths live only in `assets/private-corpus-index/p0-teaching-case-local-availability.json`, which the public-core exporter excludes.
- Public metadata needed to interpret a candidate may be frozen in a public sidecar. The registry binds each sidecar by a relative filename, metadata ID and SHA-256; the validator rejects missing, relocated, modified or case-mismatched metadata.
- `not-cached` candidates remain plans. The registry does not authorize downloading or installing their data or dependencies.
- Package probes describe the audited base environment; they are not a lock file and cannot be reused as proof of compatibility.

## GSE185948 single-nucleus correction

`p0-single-cell-gse185948` is the five-donor healthy-control kidney-cortex candidate. GSM5627690-GSM5627694 belong to GSE185948, not GSE185809. The frozen public metadata in `p0-single-cell-gse185948-metadata.json` records nuclear RNA, 10x Genomics Chromium 3' v3, Cell Ranger 6.0.0 with `--include-introns`, GRCh38, the Healthy1-Healthy5 donor sheet, PMID 36310237 and DOI 10.1038/s41467-022-34255-z.

This correction freezes provenance but does not promote execution status. The case remains `allowed_mode: plan`, `not-executed`, `install_authorized: false`, with license and redistribution status pending. QC must be nucleus-aware and distribution-derived per donor; canned whole-cell mitochondrial thresholds are not acceptable evidence for retaining or excluding nuclei.

## Validation

Run a portable metadata validation:

```powershell
python scripts/validate_p0_teaching_cases.py --no-local-locators
```

On the audited workstation, also verify every private locator against its live file:

```powershell
python scripts/validate_p0_teaching_cases.py --verify-local
```

The validator checks the standalone JSON Schema, exact domain coverage, no public absolute paths, non-promoted maturity, disabled installation, ordered workflow DAGs, checkpoint correspondence, hash-bound public metadata, public/private integrity agreement and, when requested, live file size and SHA-256.

## Promotion gate

A future case run must create a new `WorkflowInstance`; it must not edit this registry in place to imply execution. Promotion requires, at minimum:

1. resolved license, citation and input metadata blockers for the intended use and redistribution scope;
2. explicit `run` authorization;
3. a new task-scoped locked environment;
4. checkpoint artifacts that pass their declared validation rules;
5. statistical and native-figure QA;
6. a separate evidence record linking run manifest, artifacts and hashes.
