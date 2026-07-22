# P0 quantitative-proteomics teaching case: DEP UbiLength

This path-independent teaching asset contains the complete protein-level LFQ analysis runner. It does not redistribute the DEP source archive, embedded data objects, the source article, or raw mass-spectrometry files. The shared `EnvironmentManager` provisions, verifies, freezes, and executes the task environment; `run_pipeline.R` contains no installation commands.

## Frozen scientific contract

- Source identity: the `UbiLength` and `UbiLength_ExpDesign` objects are hash-bound to the DEP 1.32.0 source archive from Bioconductor 3.22.
- Verified Windows runtime: DEP 1.31.0, QFeatures 1.20.0, limma 3.66.0, and Rcpp 1.1.1 under R 4.5.3.
- The source/runtime DEP mismatch is an explicit compatibility boundary, not a version-identical reproduction. The workflow verifies that installed and extracted objects are identical and never silently substitutes a different method.
- Primary contrast: `Ubi6 - Ctrl`.
- Primary analysis: limma on observed log2 LFQ values, no imputation, at least two observations per group, robust trend empirical Bayes, BH correction.
- Sensitivity analysis: DEP MinProb (`q=0.01`, seed `20260719`) followed by the same contrast; this is an alternative preprocessing/estimand and never replaces the primary result.
- Statistical unit: documented experimental replicate/pull-down sample. The workflow cannot establish direct binding, causality, pathway activation, patient relevance, or population generalization.

## Source layout

Acquire the official inputs into a read-only source directory with this layout:

```text
<source-root>/
  DEP_1.32.0.tar.gz
  extracted/DEP/data/UbiLength.rda
  extracted/DEP/data/UbiLength_ExpDesign.rda
```

`run_case.py` checks the three frozen SHA-256 values before creating a run. The source directory is never modified. Verification serializations are written inside the run's staged input-audit checkpoint.

## Execute in an already frozen environment

The environment plan must point to an `environment.locked.json` and `renv.lock` produced by the shared manager. If the marker declares the task-local Windows exit workaround, pass its DLL explicitly.

```powershell
python run_case.py `
  --run-root <new-run-directory> `
  --source-root <source-root> `
  --environment-plan <environment_plan.json> `
  --windows-exit-helper <task-local-hard_exit.dll>

python verify_outputs.py <new-run-directory>
```

The run stops at `NATIVE_VISUAL_REVIEW`. Machine checks do not promote figures to final delivery; the agent must inspect the original-resolution PNGs, write the per-figure claim boundaries, and register a separate visual-review checkpoint.
