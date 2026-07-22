# Conditional bulk RNA-seq modules

## Contents

1. Unwanted variation
2. Time-course designs
3. WGCNA and pathway scores
4. Predictive modeling
5. Module artifact contract

## 1. Unwanted variation

Enable RUVSeq/EDASeq only when controls are defensible:

- `RUVg`: require external or empirically stable negative-control genes that are not expected to respond to the biological contrast.
- `RUVs`: require genuine replicate/control samples where biology is held constant.
- `RUVr`: use residuals from an initial model cautiously; inspect whether the unwanted factors absorb the target effect.

Evaluate multiple plausible `k` values. Record associations of each unwanted factor with both technical and biological covariates. If the factor is inseparable from the outcome, report non-identifiability rather than “correcting” it away.

## 2. Time-course designs

Choose the estimand before the package:

- **Any trajectory difference:** use a full-vs-reduced likelihood-ratio test, such as `~ subject + time + condition + time:condition` versus the model without the interaction, when the design is supported.
- **Specific time contrast:** fit a full interaction and test a predeclared contrast with DESeq2, edgeR, or limma.
- **Smooth trajectory:** use spline bases with sufficient unique times and report degrees of freedom.
- **maSigPro:** treat as an alternative longitudinal regression workflow; preserve its complete design matrix, regression/selection stages, and cluster outputs rather than extracting only a plotting call.

Repeated measurements require a subject-aware method. Do not claim `n = subjects x timepoints` independent replicates.

## 3. WGCNA and pathway scores

- Pass variance-stabilized or defensibly normalized log-expression to `wgcna_analysis`.
- Retain sample identifiers and patient-level covariates; remove genes by low information, not by outcome P value.
- Treat module-trait correlations as associations. Correct across tested module-trait pairs and preserve module membership uncertainty.
- For GSVA/singscore, version gene sets and identifier mapping. Test scores with the same batch, pairing, and repeated-measure design used for gene-level outcomes.

## 4. Predictive modeling

Route predictive tasks to `ml_biomarker` only after defining outcome, prediction time, and deployment population. Split by patient and, where relevant, site/time. Put normalization, batch handling, feature selection, and tuning inside resampling. Require nested cross-validation or an untouched external set; report calibration and decision utility in addition to discrimination.

## 5. Module artifact contract

Every optional module returns:

- its exact input artifact ID and scale;
- preconditions and why they were satisfied;
- package/version, parameters, design/contrast, seed, and tested universe;
- primary table and diagnostic figures;
- sensitivity results and failure flags;
- a statement of what the result supports and cannot support.

If a module changes the statistical estimand, classify it as `alternative_method` and require explicit selection. Do not auto-fallback from one inferential method to another.
