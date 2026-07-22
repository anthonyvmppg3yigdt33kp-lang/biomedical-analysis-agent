---
name: multi-omics-pipeline
description: Design, execute, and audit bulk specimen-level multi-omics integration across transcriptomics, proteomics, metabolomics, and related matrices. Use for sample harmonization, MOFA2 shared-factor discovery, DIABLO supervised signatures, SNF subtype discovery, MCIA/omicade4 co-inertia analysis, factor/component interpretation, validation, and explicit cross-omic artifact handoffs. Route single-cell multimodal data elsewhere.
---

# Multi-omics integration pipeline

Integrate only after each modality has passed its own QC and preprocessing. Preserve modality-specific normalized matrices, shared sample identifiers, feature namespaces, and provenance; never concatenate heterogeneous assays and call the result integration.

## Establish the integration contract

Before choosing a method, define:

- the biological unit and whether the data are vertical (multiple omics measured on the same specimens);
- the primary objective: latent structure, supervised prediction, subtype discovery, or shared co-structure;
- outcome availability, batch/site/time structure, missing modalities, and validation cohort;
- one matrix per view with samples in rows, unique feature IDs in columns, a declared scale, and no duplicated sample IDs;
- the exact common-sample set for methods requiring complete correspondence, plus a sample-exclusion ledger.

Stop if sample identifiers are ambiguous, the outcome is confounded with site/batch, preprocessing used outcome information before resampling, or the design cannot support the requested conclusion.

## Route by scientific objective

| Objective | Method | Required interpretation |
|---|---|---|
| Unsupervised shared and view-specific factors, optionally incomplete views | MOFA2 | Per-view variance, factor scores/weights, convergence, technical-covariate associations, stability. |
| Supervised cross-omic signature | DIABLO (`block.splsda`) | Nested/externally validated balanced error, selected-feature stability, calibration where applicable. |
| Candidate patient subtypes | SNF | Cluster-number sensitivity, stability, fused-vs-single-view value, clinical/technical association. |
| Shared co-inertia among complete matched views | MCIA (`omicade4`) | Global and partial axes, view contributions, sample/feature projections, permutation/stability evidence. |

These are `alternative_method` variants, not automatic fallbacks. Read `references/method-selection-and-validation.md` for complete decision rules and executable patterns.

## Run the common workflow

1. **Profile each view.** Record feature type, scale, missingness, zero structure, normalization, batch, and identifier namespace. Use upstream domain skills for modality-specific processing.
2. **Harmonize samples.** Construct a single mapping table. Reorder every matrix explicitly; verify by checksum. Do not assume matching column or row order.
3. **Select features inside the appropriate scope.** Unsupervised variance filtering may use the analysis cohort but must ignore outcomes. Supervised filtering/tuning belongs inside each training fold.
4. **Fit one prespecified integration family.** Store parameters, seeds, model object, diagnostics, and the exact input artifact IDs.
5. **Validate.** Quantify convergence/stability, view dominance, batch association, and performance on data not used for tuning. If no external cohort exists, label biomarker/subtype claims exploratory.
6. **Interpret.** Use signed weights/loadings and the tested view-specific universe for enrichment. Distinguish an axis association from a causal regulatory mechanism.
7. **Export the artifact contract.** Produce machine-readable scores, weights, selected features/networks, diagnostics, figures, and claim boundaries.

## Resolve DIABLO design without a false default

The off-diagonal DIABLO `design` value controls the trade-off between outcome discrimination and agreement among blocks. Neither `0.1` nor `0.5` is a universal default:

- Treat a low-correlation design such as `0.1` and a moderate design such as `0.5` as prespecified sensitivity variants.
- Choose the primary design from the scientific objective and evaluate predictive error, cross-block correlation, and feature stability.
- Tune `ncomp` and `keepX` within training resamples. A `perf()` call on the same data used to tune is internal evidence, not an unbiased final estimate.
- Keep an external test cohort untouched where possible; otherwise use nested repeated cross-validation and report its limitation.
- Never choose the design value because its circos plot looks denser or its training error is lower.

## Resolve MOFA factor thresholds without contradiction

- Fit enough initial factors to represent plausible structure and inspect ELBO/convergence.
- Report variance explained per factor and per view before pruning.
- Do not declare a universal meaningful-factor threshold. A `1-2% in every view` rule and a `5% total/per-view` rule answer different questions and can discard view-specific biology.
- Predeclare a primary retention rule using variance, stability, and technical-covariate screening; show sensitivity under at least one stricter/looser rule.
- Keep technical factors available for QC even if excluded from biological interpretation.
- Label a factor shared only when its contribution is supported in multiple views; label a one-view factor view-specific, not failed.

## Apply method-specific hard gates

- **MOFA2:** verify likelihood/scale compatibility, convergence, seed stability, view dominance, and factor association with batch/depth.
- **DIABLO:** use classification-appropriate metrics such as balanced error, keep all preprocessing and feature selection inside resampling, and avoid subject/site leakage.
- **SNF:** standardize each view appropriately, distinguish neighborhood `K` from cluster count, test cluster stability, and compare fusion with the best single view.
- **MCIA:** require complete matched specimens after an explicit missingness decision; scale blocks so the largest/highest-variance view does not dominate by construction.

## Artifact handoff

Use the schema in `references/artifact-contract.md`. At minimum export:

- `sample_map.tsv` and excluded-sample ledger;
- one immutable processed matrix plus metadata per view;
- `integration_scores.tsv`, `integration_weights.tsv` or the method-equivalent network/cluster table;
- method diagnostics, resampling/stability results, seeds, package versions, and serialized model;
- view-specific enrichment inputs and feature-universe files;
- figures with notes stating what is visible, supported, and not established.

Cross-language nodes exchange explicit TSV/Parquet/HDF5 artifacts. Do not share hidden in-memory state across R and Python recipes.

## Execution policy

- Keep installation outside analysis recipes. In authorized `run` mode, declare dependencies to the task EnvironmentManager and freeze the verified environment before fitting.
- Treat package/API changes as a compatibility failure to resolve explicitly, not as permission to change methods.
- Checkpoint after harmonization, feature preparation, model fit, validation, and interpretation.
- Route CITE-seq/10x Multiome and other cell-level multimodal tasks to the single-cell multiome workflow; this skill assumes specimen-level bulk views.

## References

- `references/method-selection-and-validation.md`: complete MOFA2, DIABLO, SNF, and MCIA selection/validation patterns.
- `references/artifact-contract.md`: cross-omic artifact schemas and claim roles.
- `examples/mofa_integration.R`: a MOFA2 example; adapt only after validating package versions and the artifact contract.
