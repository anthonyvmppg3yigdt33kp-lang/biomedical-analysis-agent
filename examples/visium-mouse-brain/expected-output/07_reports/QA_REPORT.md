# QA REPORT

| Gate | Status | Evidence |
|---|---|---|
| Exact R 4.5.3 | pass | environment.locked.json |
| Exact task-local renv 1.2.2 for bootstrap and project; no host renv dependency | pass | pre-install binary hash, environment lock, renv.lock, and environment probe |
| Exact Seurat 5.5.0 and hdf5r 1.3.12 from Windows binary snapshot | pass | environment lock and spatial/API/H5 smoke probe |
| Complete Bioconductor 3.21 cohort | pass | archive pins, SHA-256/DESCRIPTION gates, renv.lock; glmGamPoi 1.20.0 and SparseArray 1.8.1 |
| Explicit SCT backend | pass | preprocess_summary.json records vst.flavor=v2, method=glmGamPoi_offset, glmGamPoi_check=true, exact package paths and finite layers/PCA |
| Native Windows R shutdown | pass | child-only AMD64 restoration, clean stdout/stderr scan, native exit 0, hash-bound completion marker |
| Input size/SHA freeze | pass | resolved-inputs.json |
| Assay/image/coordinate two-way barcode reconciliation | pass | barcode_reconciliation.csv, barcode_set_reconciliation.json, barcode_set_differences.csv |
| Coordinate/image/scale-factor structural QC | pass | coordinate_image_qc.json |
| No undocumented post-load filtering | pass | attrition.csv and analysis config |
| Checkpoint hash binding | pass | per-stage _checkpoint.json |
| Runtime warning classification | pass | pipeline-warnings.json; 0 occurrence(s), zero release blockers |
| Descriptive claim boundary | pass | claim_boundary_qa.json |
| Original/final PNG export | pass | figure_index.csv |
| Migrated environment cache provenance | pass | historical build basename `visium-mouse-brain-r453-seurat550-5324b3ca515435690ada4e70` retained only as origin; current hash-bound cache key `166efc610ed4b0d8d5004f1b`, current basename `visium-mouse-brain-r453-seurat550-166efc610ed4b0d8d5004f1b`, native R revalidation rc=0 with no forbidden output |
| Native coordinate alignment and visual review | pass | `review-round-1.json` SHA-256 `73493da1caf9a5c0ff5f52926b9d814bffbb4328c8c5e129351bd035fc3752b5` |

Native review opened every registered original/final pair and reached a delivery-ready keep decision.
