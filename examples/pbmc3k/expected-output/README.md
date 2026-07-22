# PBMC3K verified expected output

This directory is a compact, public, derived teaching reference exported from
`pbmc3k-native-v1-20260722-v5` after fresh execution, checkpoint resume, task-local
environment cache reuse, report generation, and native review of all five
original/final figure pairs.

Canonical result: **2,700 input cells -> 2,638 QC-retained cells -> 9 descriptive
clusters** under R 4.5.3 and Seurat 5.5.0.
The feature-name mapping records the explicit underscore-to-dash normalization
performed before Seurat object creation and proves dimensions/count values unchanged.

The directory contains derived tables, reports, figures and hash-bound
provenance only. It does **not** contain the 10x archive, extracted matrices,
cell-level exports, RDS objects, checkpoints, package libraries, caches, process
logs, or absolute workstation paths. Native-exit process records retain only
return codes, architecture, forbidden-scan results and cryptographic hashes.
The input data remain separately
attributed to 10x Genomics under CC BY 4.0; repository MIT terms cover only
original code and documentation.

Verify from the repository root:

```powershell
python examples/pbmc3k/verify_expected_output.py
```

`ARTIFACT_INDEX.md` is the human-readable payload index. The append-only-style
`manifest/artifact_ledger.jsonl` binds every other exported file, including the
index, to its byte size and SHA-256.
