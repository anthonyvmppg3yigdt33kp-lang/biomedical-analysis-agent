---
name: quantitative-proteomics-workflow
description: Design, execute, and audit post-search quantitative proteomics workflows from MaxQuant proteinGroups/evidence tables, peptide/PSM tables, MSstats-format feature tables, or protein-intensity matrices. Use for QFeatures PSM-to-peptide-to-protein aggregation, missingness-aware filtering/imputation, normalization, DEP/limma/MSstats differential analysis, tidyproteomics exploration, QC, visualization, and result interpretation. Excludes raw mass-spectra processing, peptide identification, and DDA/DIA search-engine configuration.
---

# Quantitative proteomics workflow

Analyze post-search quantitative tables while preserving PSM/peptide/protein provenance. Do not imply that this skill processes vendor RAW/mzML files or validates spectrum identification; route those tasks to a dedicated mass-spectrometry search workflow.

## Route by input level

| Input | Preferred route | Preserve |
|---|---|---|
| PSM/peptide table with protein mapping | QFeatures -> peptide/protein aggregation -> protein model | PSM-peptide-protein links, shared-peptide rule, aggregation function. |
| MaxQuant `proteinGroups.txt` | contaminant/decoy/site-only filtering -> intensity selection -> DEP or limma | Raw column names, LFQ/iBAQ/intensity scale, protein group IDs. |
| MaxQuant `evidence.txt` or converter output | MSstats converter/contract -> `dataProcess` -> `groupComparison` | Run, feature, condition, biological replicate, intensity, fraction/channel. |
| Protein-intensity matrix | matrix preflight -> missingness plan -> normalization -> DEP/limma | Unmodified matrix, sample metadata, scale, missingness mask. |
| tidyproteomics object | package-native summarize/subset/normalize/explore branch | Object structure, operation history, package version, source bundle. |

Read `references/input-and-artifact-contract.md` before import and `references/r-package-workflows.md` for complete package-specific routes. Treat DEP/limma, MSstats, and package-native tidyproteomics as alternative methods; do not silently switch among them.

## Apply hard scientific gates

Stop or downgrade to descriptive QC when any condition is unresolved:

1. Define biological replicate, condition, pairing/repeated structure, batch, acquisition run, channel/fraction, and contrast. Runs, fractions, peptides, and PSMs are not independent patients.
2. Verify sample identifiers and matrix columns exactly. Reject duplicate feature IDs unless a predeclared protein-group/aggregation rule resolves them.
3. Record search engine, protein sequence database/release, FDR level, contaminant/decoy/site-only flags, quantification type, and match-between-runs policy.
4. Confirm the intensity scale before log transformation. Never log2 an already logged matrix or compare raw intensity and LFQ columns as if equivalent.
5. Characterize missingness by sample, condition, intensity, feature type, and batch before filtering or imputation.
6. Require a full-rank patient-safe design. Perfect condition-batch confounding cannot be repaired statistically.
7. Keep multiple-testing control at the protein/contrast family actually interpreted. Do not relax adjusted P values until a desired pathway appears.
8. Inspect identification counts, intensity distributions, missingness heatmap, sample correlations/PCA, normalization effect, CV, and model residuals before interpreting proteins.

## Preserve the complete workflow

Execute these stages in order and checkpoint each validated artifact:

1. **Import and archive.** Hash the untouched source tables; write a column dictionary and sample map.
2. **Remove known non-biological rows.** Apply explicit contaminant, reverse/decoy, and site-only rules. Report counts before/after.
3. **Build the quantitative object.** Use QFeatures for multilevel PSM/peptide/protein data, DEP/SummarizedExperiment for protein matrices, or MSstats' long feature-level contract.
4. **Aggregate if needed.** Declare shared-peptide handling, minimum evidence, protein group rule, and robust summary method. Preserve assay links.
5. **Profile missingness.** Separate sporadic missing-at-random plausibility from left-censored/condition-specific absence; retain the original mask.
6. **Filter and normalize.** Apply condition-aware evidence filtering and evaluate normalization with diagnostics. Normalization does not cure batch confounding.
7. **Impute only when justified.** Run a no-imputation or model-native primary analysis where possible; treat left-shifted and MAR-style imputation as sensitivity variants, never as invisible preprocessing.
8. **Fit a specimen-level model.** Use limma/DEP for summarized protein intensities or MSstats for feature/run-level modeling. Encode pairing/batch explicitly.
9. **Validate and visualize.** Check residuals, sample structure, missingness sensitivity, effect stability, and influential samples. Produce QC, volcano/MA, heatmap, and protein-profile figures.
10. **Interpret conservatively.** Report protein-group ambiguity and missingness dependence. Association or differential abundance does not prove pathway activation or causal regulation.

## Select packages by data contract

- Use **QFeatures** to maintain linked assays and aggregate PSM/peptide features to proteins. It is a data infrastructure and aggregation route, not by itself a differential-testing method.
- Use **DEP** for a documented protein-intensity matrix and simple/paired limma-style contrasts. Keep its filtering, normalization, imputation, testing, and rejection stages together.
- Use **MSstats** when run/feature-level evidence and converter-compatible annotations are available; let its summarization/model handle the declared feature structure.
- Use **tidyproteomics** as an exact package-native exploration/operation-history variant when its object and locked API are available. Do not translate a screenshot tutorial into invented functions; preserve and verify the complete source bundle.
- Use **limma directly** for complex protein-level linear designs when the matrix scale and missing-data decision are explicit.

## Missingness policy

- Never globally replace missing values with zero.
- Do not infer MNAR solely because values are missing; use intensity/condition patterns and acquisition context.
- Prefer model-native/no-imputation inference when supported. If imputation is required, separate MAR-like and left-censored strategies and compare conclusions.
- Perform imputation after splitting in predictive workflows to prevent leakage.
- Flag proteins whose significance or effect direction changes materially across defensible missingness strategies.

## Output contract

Every completed run exports:

- immutable source manifest, column dictionary, sample map, feature/protein mapping, and exclusion ledger;
- original quantitative matrix, missingness mask, filtered matrix, normalized matrix, and imputed sensitivity matrices as distinct artifacts;
- model-ready metadata, formula, contrast, package/version, parameters, seeds, and serialized object;
- `protein_results.tsv` with protein/group ID, effect, uncertainty/test statistic, P value, adjusted P value, evidence count, missingness summary, and sensitivity flag;
- QC tables/figures plus final figures and `FIGURE_NOTES.md` stating what each figure can and cannot support;
- environment lock, session information, run manifest, and artifact ledger.

Use `scripts/validate_proteomics_matrix.py` for a dependency-free matrix/metadata preflight. Use `visualization-2026718-v1` for figure refinement only after the scientific gates pass.

## Execution policy

- Keep package installation outside analysis recipes. In authorized `run` mode, declare dependencies to the task EnvironmentManager and freeze the verified environment.
- Do not modify raw source tables. Write repairs/renaming maps as derived artifacts with provenance.
- Fail explicitly on missing columns, incompatible scale, unresolved sample mapping, or unsupported package API. Report alternatives and their scientific differences.
- Do not auto-fallback from MSstats to DEP/limma or from no-imputation to imputation; these change the model or estimand.

## References

- `references/input-and-artifact-contract.md`: required schemas, MaxQuant/MSstats mappings, and artifact roles.
- `references/r-package-workflows.md`: coherent QFeatures, DEP, MSstats, tidyproteomics, and limma workflows.
