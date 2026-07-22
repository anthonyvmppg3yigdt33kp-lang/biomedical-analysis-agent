# QA report

| Gate | Status | Evidence |
|---|---|---|
| Pinned archive hash and size | **pass** | Recorded in ../manifest/input-evidence.json |
| Feature-name mapping | **pass** | 21 underscore-to-dash names; dimensions/counts unchanged |
| UMAP runtime contract | **pass** | uwot/cosine/seed 42; warn=1; official scoped transition option restored; no suppressWarnings/handler muffling/allowlist |
| R 4.5.3 / Seurat 5.5.0 runtime | **pass** | Pipeline exact-version gate passed |
| Native R exit and clean stdout/stderr | **pass** | AMD64 child evidence; return code 0; no forbidden matches |
| 2700 / 2638 / 9 canonical values | **pass** | {'input_cells': 2700, 'qc_retained_cells': 2638, 'clusters': 9} |
| Single-library descriptive claim boundary | **pass** | No inferential or advanced branch |
| Original/final figure pairs | **pass** | 5 paired figures |
| Native visual review | **pass** | hash-bound terminal keep |

A blocked native-review gate is reported honestly and prevents release qualification; it does not erase a successful data execution.
