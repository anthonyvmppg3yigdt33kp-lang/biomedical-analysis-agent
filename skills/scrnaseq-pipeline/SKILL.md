---
name: bio-workflows-scrnaseq-pipeline
description: Design, validate, execute, resume, and audit reproducible single-cell RNA-seq workflows across droplet, plate-based, generic count-matrix, H5AD, and RDS inputs. Use for platform-aware QC, ambient RNA and doublet handling, normalization, multi-sample integration, clustering and annotation, donor-aware pseudobulk differential expression, composition testing, trajectory, CNV inference, cell communication, single-cell figures, or review of statistical and biological claim boundaries.
---

# Single-cell RNA-seq pipeline

Build a deterministic workflow around the research question, platform, sample hierarchy and inferential unit. Preserve raw counts and provenance. Do not reduce a multi-donor study to a single-cell cookbook.

## Start here

1. Identify the requested mode: `plan`, `run`, `resume`, `reproduce-figure`, or `explain`.
2. Record organism, tissue, assay/platform, reference build, gene namespace and input format.
3. Lock the hierarchy `cell -> capture -> sample -> donor` and condition, batch, subject/time and multiplexing fields.
4. Define the estimand, biological replicate, contrasts, covariates, multiplicity family and exploratory/confirmatory status.
5. Read [references/workflow-contract.md](references/workflow-contract.md) before compiling stages or choosing advanced branches.
6. Validate a JSON design with `python scripts/validate_scrna_design.py --config <design.json> --check-paths --output <report.json>`.
7. Stop on every validation error. Preserve warnings and their disposition in the analysis design.
8. Compile a frozen WorkflowInstance, declare dependencies, and hand environment work to the shared EnvironmentManager.

Use [references/design.schema.json](references/design.schema.json) when creating the design input. Use [references/artifact-contract.schema.json](references/artifact-contract.schema.json) for every promoted artifact.

## Non-negotiable scientific rules

- Demultiplex pooled captures before per-sample QC. Run ambient-RNA correction, QC and doublet detection per capture before merging.
- Derive QC thresholds from capture- and tissue-specific distributions. Treat fixed cutoffs only as reviewable starting values.
- Preserve unmodified raw counts. Keep ambient-corrected counts, normalized expression, scaled values and integrated embeddings as distinct artifacts.
- Use integration for a shared representation, clustering and visualization. Do not use integrated expression or embeddings as count-model input.
- Pair batch-mixing diagnostics with biological-conservation diagnostics. Reject correction that erases condition structure or rare populations.
- Annotate with positive and contradictory markers, reference provenance and confidence. Keep unresolved cells/clusters explicitly unknown.
- Use donor-aware pseudobulk raw counts for condition-level expression inference. Cell-level tests are descriptive and cannot substitute for biological replicates.
- Test composition separately from expression. Do not interpret abundance change as within-cell expression change or vice versa.
- Reject a rank-deficient model or complete condition-batch confounding. No integration method recovers an unidentified effect.
- Report effect sizes, uncertainty, FDR family, sensitivity analyses and conclusion limits.

## Platform and method routing

Use platform evidence rather than filename alone:

- For 10x 3'/5', Feature Barcode, Parse, Drop-seq or Seq-Well, require capture-aware droplet handling. SoupX and CellBender require unfiltered droplets; they are not interchangeable.
- For Smart-seq2/full-length assays, model plate/well effects and skip droplet-only assumptions.
- For H5AD/RDS, audit layers/assays, transformations, identifiers and provenance. Mark upstream stages as imported, not locally verified.
- For generic matrices, require orientation, raw-count status, feature namespace and cell-to-donor metadata before analysis.
- For CITE-seq or multiome inputs, preserve modality pairing and route non-RNA inference to the corresponding multimodal component.

Choose normalization and integration only after assessing depth, chemistry, sample count and expected biology. Treat SCTransform vs log/scran and Harmony vs RPCA/CCA vs scVI as explicit method choices with distinct assumptions, not fallbacks.

## Required stage order

Use the applicable subset of this DAG:

`intake -> import/identity -> demultiplex? -> ambient RNA? -> per-capture QC -> per-capture doublets -> per-sample normalization/HVG -> merge/integrate? -> graph/cluster/embed -> annotate/review -> pseudobulk DE? -> differential abundance? -> advanced branches? -> figures/interpretation`

Write each stage to `_staging/<stage_id>`. Validate its ArtifactContracts before atomic promotion. A non-zero exit, missing contract, partial file or failed scientific gate is a failed stage. Resume only from the last promoted checkpoint with matching input, plan and environment hashes.

## Core and advanced outputs

Always produce:

- input and sample manifest with donor/capture identity;
- per-capture QC and exclusion audit;
- raw-count and post-QC checkpoints;
- integration/cluster sensitivity review when applicable;
- annotated object with label evidence and confidence;
- tables and figures with direct-observation versus inference boundaries;
- artifact ledger, hashes, environment evidence and QA report.

For condition inference, also produce donor-by-cell-type raw pseudobulk matrices, model-ready sample metadata, design/contrast diagnostics and adjusted results. For composition, produce donor-level counts/proportions and the declared sampling model.

For trajectory, CNV or communication, apply the entry gates and conclusion limits in [references/workflow-contract.md](references/workflow-contract.md). These branches remain optional and must never be added because they are fashionable or expected by a paper template.

## Environment and execution boundary

Analysis recipes declare runtimes, package sources and versions only. Do not place installation commands inside a recipe or analysis script.

During `plan`, use read-only environment probing only. During an explicitly authorized `run`, invoke the shared EnvironmentManager lifecycle:

`probe -> resolve -> provision -> verify -> freeze -> execute -> report_failure`

Use the configured absolute `Rscript.exe`; do not rely on PowerShell `R`. Keep R and Python in separate task-local locked environments unless interop is essential and declared. Never change global libraries, the base Conda environment, system PATH or administrator policy. Do not silently replace a failed method with a scientifically different method.

## Figure and interpretation review

For every figure, state:

- research question, cells shown and biological/statistical unit;
- expression layer or embedding used and transformations applied;
- what is directly visible;
- what the result supports and cannot support;
- statistical assumptions, uncertainty and multiplicity;
- visual legibility, color accessibility, label overlap and export specifications;
- whether the figure is descriptive, inferential, exact reproduction or optimized variant.

UMAP distance does not establish developmental time, cell-cell interaction or effect magnitude. Marker heatmaps do not establish condition effects. Ligand-receptor co-expression does not prove physical signaling or causality.

## Bundled resources

- [references/workflow-contract.md](references/workflow-contract.md): full platform routing, stage DAG, checkpoints, statistical gates, advanced branches and environment handoff.
- [references/design.schema.json](references/design.schema.json): machine-readable input design contract.
- [references/artifact-contract.schema.json](references/artifact-contract.schema.json): promoted artifact provenance and semantic contract.
- `scripts/validate_scrna_design.py`: dependency-free, read-only input/design validator and deterministic stage-plan compiler.
- `examples/seurat_workflow.R` and `examples/scanpy_workflow.py`: minimal single-sample descriptive teaching paths. Do not present them as donor-aware multi-sample inference workflows.

## Maturity boundary

Treat the workflow contract, schemas and validator as `fixture-verified` only after their bundled tests pass. Treat bundled R/Python teaching recipes as `parse-verified` unless they are executed in a locked environment. Promote a specific workflow to `data-verified` only after it succeeds on declared data with artifact validation; promote figures to `native-reviewed` only after visual inspection. Never inherit maturity from another dataset, package version or method variant.
