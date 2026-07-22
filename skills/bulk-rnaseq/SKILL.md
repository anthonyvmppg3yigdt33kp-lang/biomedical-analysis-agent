---
name: bulk-rnaseq
description: Design, execute, and audit reproducible bulk RNA-seq analyses from FASTQ, STAR/featureCounts, Salmon quantification, gene-count matrices, or clearly documented expression matrices. Use for read QC and quantification, R tximport plus DESeq2, edgeR quasi-likelihood, limma-voom, time-course or RUV-aware designs, enrichment handoff, and publication figures. Do not use for single-cell RNA-seq or for treating TPM/FPKM as counts.
---

# Bulk RNA-seq

Build a specimen-level, checkpointed workflow. Preserve the complete route from input to figures; do not copy isolated code blocks without their object, design, reference, and output dependencies.

## Route the input before choosing a model

| Input | Route | Non-negotiable rule |
|---|---|---|
| FASTQ | FastQC/MultiQC -> trimming if justified -> STAR/Salmon/nf-core -> gene abundance | Pin genome, annotation, pipeline, strandedness, and quantifier across all samples. |
| Salmon `quant.sf` | R `tximport` -> DESeq2, edgeR, or limma-voom | Prefer the native R offset/length route; never round TPM and call it raw counts. |
| STAR/featureCounts counts | integer gene-by-sample matrix -> selected DE engine | Select and document the correct stranded column. |
| Gene count matrix | matrix preflight -> DESeq2/edgeR/limma-voom | Require gene rows, unique sample columns, non-negative count-scale values, and exact metadata alignment. |
| Normalized/log expression | limma or descriptive analysis after scale verification | Do not use DESeq2/edgeR count likelihoods and do not reconstruct counts. |

For upstream commands read `references/upstream-nfcore.md` or `references/upstream-manual.md`. For complete R-native matrix and Salmon workflows read `references/r-matrix-workflows.md`. The Python bridge in `scripts/build_counts_matrix.py` remains a compatible path for PyDESeq2, not a substitute for the R tximport model.

## Apply hard scientific gates

Stop before modeling when any gate fails:

1. Define the biological unit, outcome/contrast, paired or repeated structure, batch variables, and exclusions. Technical replicates are not biological replicates.
2. Align metadata rows to matrix columns by identifier and reject duplicates or silent reordering.
3. Confirm the design matrix is full rank. Do not claim an adjusted condition effect when condition is perfectly confounded with batch/site/sex.
4. Require biological replication for inferential DE. With fewer than three independent units per group, deliver QC/descriptive results and label inference underpowered unless a defensible paired or continuous design applies.
5. Verify the assay scale. Raw/estimated counts, TPM/FPKM, log-expression, and variance-stabilized values are not interchangeable.
6. Filter low information genes before multiple testing using a predeclared, design-aware rule; never relax FDR until desired pathways appear.
7. Use donor/patient as the inferential unit. Model pairing or repeated observations; do not count aliquots, lanes, or regions as independent patients.
8. Inspect library size, mapping/assignment, complexity, sample distances, PCA, mean-variance trend, and p-value distribution before interpreting genes.

Read `references/design-and-qc.md` for the full preflight and QC checklist.

## Select the differential engine

- Use **DESeq2** for count-scale gene matrices or `tximport` objects when median-ratio normalization and negative-binomial shrinkage suit the design.
- Use **edgeR quasi-likelihood** when flexible GLM contrasts, small-sample robustness, or TMM normalization are useful. Prefer `glmQLFit/glmQLFTest` over an unqualified likelihood-ratio workflow.
- Use **limma-voom** for count data with a stable mean-variance trend, especially complex linear models; inspect the voom trend and use quality weights only when justified.
- Use **limma without voom** only for verified approximately log2-normal expression, not raw counts.
- Treat engines as `alternative_method`, not silent fallbacks. Record the chosen normalization, design, contrast, independent filtering, shrinkage, and FDR method.

Complete, coherent R recipes are in `references/r-matrix-workflows.md`; keep package installation outside all recipes and delegate dependency provisioning to the execution environment manager.

## Add conditional modules only when identifiable

- **Unwanted variation:** use RUVSeq/EDASeq only with valid negative controls, replicate structure, or empirically justified controls. Estimate `k` by sensitivity analysis and retain the biological variable in the design. Do not blind-correct a PCA until groups look separated.
- **Time course:** choose a likelihood-ratio/spline interaction model, limma spline model, or maSigPro according to repeated structure and the scientific contrast. Model subject when repeated; a time point is not an independent replicate.
- **WGCNA:** hand a variance-stabilized/log-expression matrix and specimen metadata to `wgcna_analysis`; never supply raw counts or force a soft threshold/module-trait association.
- **GSVA/singscore:** calculate specimen-level pathway scores from a documented expression scale and versioned gene sets. Test scores with the same patient-safe design.
- **Prediction:** route to `ml_biomarker`; perform feature selection and preprocessing inside patient/site/time-safe resampling. A DE list selected on the full cohort cannot be evaluated on that same cohort as a biomarker.

Read `references/advanced-modules.md` before enabling any of these branches.

## Preserve downstream contracts

Every completed run must export:

- `counts_or_expression.tsv` plus a scale declaration and feature identifier namespace.
- `sample_metadata.tsv`, design formula, tested contrast, exclusion ledger, and sample-order checksum.
- `de_results.tsv` with base expression, effect estimate, standard error/test statistic, raw P value, adjusted P value, direction, and method.
- normalized/transformed expression for visualization, never mislabeled as the testing input.
- QC tables/figures, session information, package versions, reference genome/annotation, seeds, and parameter file.
- an enrichment rank vector derived from the full tested universe and a separate thresholded hit list; use the tested feature universe as ORA background.
- `FIGURE_NOTES.md` stating what each plot directly shows and what it cannot establish.

Use `scientific-visualization` or `visualization-2026718-v1` for PCA, sample-distance, MA, volcano, heatmap, time-course, and enrichment figures. A figure never overrides a failed design or model gate.

## Execution and failure policy

- In `plan` mode, inspect dependencies but do not install or execute.
- In authorized `run` mode, declare R/Python/system dependencies to the environment manager; do not embed `install.packages`, `BiocManager::install`, `pak`, `conda`, or `uv pip install` in analysis code.
- Write each stage to staging, validate its artifact contract, then checkpoint. Resume only from a valid checkpoint.
- If a package or reference cannot be provisioned, report the exact failed dependency and scientifically equivalent or non-equivalent alternatives. Never silently change the DE engine.

## Resources

- `scripts/validate_samplesheet.py`: validate FASTQ samplesheets and obvious design confounding.
- `scripts/build_counts_matrix.py`: build a PyDESeq2-compatible matrix from Salmon, STAR, or featureCounts outputs.
- `references/r-matrix-workflows.md`: complete R workflows for matrix input, tximport+DESeq2, edgeR QL, and limma-voom.
- `references/advanced-modules.md`: RUV, time-course, pathway-score, WGCNA, and prediction routing contracts.
- `references/counts-and-handoff.md`: count semantics, identifier mapping, and downstream handoff.
- `references/design-and-qc.md`: design and QC rules.
