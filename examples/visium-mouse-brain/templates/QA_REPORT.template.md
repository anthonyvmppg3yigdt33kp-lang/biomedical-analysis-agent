# QA REPORT

| Gate | Status | Evidence |
|---|---|---|
| Exact R 4.5.3 | `{{status}}` | environment lock |
| Exact Seurat 5.5.0 | `{{status}}` | environment lock and API smoke test |
| Input hash freeze | `{{status}}` | resolved-inputs.json |
| Barcode reconciliation | `{{status}}` | barcode_reconciliation.csv |
| Coordinate/image/scale-factor QC | `{{status}}` | coordinate_image_qc.json |
| Checkpoints | `{{status}}` | run_manifest.json |
| Scientific claim boundary | `{{status}}` | RESULTS.md and automated phrase audit |
| Original/final export | `{{status}}` | figure hashes and dimensions |
| Native visual review | `{{pending_or_terminal}}` | review JSON |

Missing evidence remains `blocked` or `pending`; it is never converted to `pass`.

