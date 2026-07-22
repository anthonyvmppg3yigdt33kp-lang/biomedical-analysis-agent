# PBMC3K results

## Outcome

The pinned PBMC3K archive produced **2,700 input cells**, **2,638 QC-retained cells**, and **9 descriptive clusters** under R 4.5.3 and Seurat 5.5.0. Canonical numerical checks: **pass**.

Both R phases completed by native process exit with return code 0. Their structured evidence binds the AMD64 child architecture, sanitized stdout/stderr and an empty forbidden-pattern scan; no external process-termination helper is used.

Before Seurat object creation, `21` of 32,738 input feature names were explicitly normalized from `_` to `-`. The mapping artifact proves that this created no duplicates and changed neither matrix dimensions nor count values.

UMAP used the explicitly frozen `uwot` / `cosine` / seed 42 contract. Seurat's official `Seurat.warn.umap.uwot=FALSE` transition option disabled only its one-time migration notice in the smallest scope and was restored afterward. The pipeline fixed `options(warn=1)` so every other R warning was emitted immediately to stderr for fail-closed scanning; it used no `suppressWarnings()`, handler muffling, or warning allowlist, and did not change the algorithm.

Cluster markers and the nine teaching labels are descriptive aids for this one public library. They do not estimate donor-level abundance, condition effects, population prevalence, mechanism, or causality. No CellChat, GSEA, pseudobulk, differential abundance, or advanced branch was run.

## Visual status

Native visual review: **pass**. A rendered PNG is not called native-reviewed until both original and final-size pixels have been opened and their hashes bound to a terminal `keep` record.

## Canonical metrics

| Metric | Observed | Expected | Status |
|---|---:|---:|---|
| Input cells | 2700 | 2700 | pass |
| QC-retained cells | 2638 | 2638 | pass |
| Clusters | 9 | 9 | pass |
