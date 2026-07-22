# Workflow, inference, and artifact contract

## Contents

1. Stage DAG
2. Module selection
3. Statistical units and pseudoreplication
4. ArtifactContract and checkpoints
5. Visual and interpretation QA
6. Environment handoff

## 1. Stage DAG

```text
S00_INTAKE
  -> S10_INGEST
  -> S20_COORD_IMAGE_QC
  -> S30_UNIT_QC
  -> S40_PREPROCESS
  -> S50_SPATIAL_GRAPH
  -> S60_CORE_DISCOVERY
      -> S70_COMPOSITION_MAPPING (optional)
      -> S80_ADVANCED (optional; may depend on S70)
  -> S90_INFERENCE_QA
  -> S95_VISUALIZE_INTERPRET
```

Each stage declares inputs, parameters, dependencies, expected artifacts, validation rules, and stop conditions. Optional modules must remain explicit branches; do not hide them inside a monolithic script.

### Stage outputs

| Stage | Minimum registered artifacts | Required validation |
|---|---|---|
| S00 | request, input manifest, source hashes, design table | platform/unit/subject/section/coordinate contract |
| S10 | vendor-native inventory, loaded object, identifier reconciliation | dimensions, IDs, count/molecule integrity, feature annotation |
| S20 | transform chain, coordinate QC, image/segmentation review | bounds/orientation/scale, tissue mask, FOV stitching, controls |
| S30 | QC table, thresholds, attrition table, QC figures | platform-aware metrics and group/sample attrition |
| S40 | preserved counts, normalized object/view, reductions | no raw mutation, method assumptions, sample/batch checks |
| S50 | neighbor graph and parameters | geometry, distance unit, connectivity, boundary sensitivity |
| S60 | clusters/domains, SVG/contrast tables | FDR, composition sensitivity, domain parameter sensitivity |
| S70 | proportions/mapping scores/uncertainty | reference compatibility, coverage, unknown handling, sum/calibration checks |
| S80 | neighborhoods/gradients/communication/image features | appropriate null, radius/graph sensitivity, subject reproducibility |
| S90 | estimand table, subject-level effects, sensitivity results | pseudoreplication, confounding, multiplicity, effect/uncertainty |
| S95 | source/final figures, figure notes, QA report, ledger | coordinate fidelity, scientific semantics, native visual review |

## 2. Module selection

### Spatial graph

- Use grid/lattice adjacency when the acquisition design is a regular array and vendor grid coordinates are preserved.
- Use distance/radius/kNN/Delaunay graphs for cell/bin point clouds only after declaring coordinate units and edge behavior.
- Do not compare statistics across samples when graph definitions or coordinate units differ without harmonization.

### Domains

Classify candidates as expression-only clustering, spatially regularized clustering, graph/deep-learning models, or Bayesian spatial models. These are not equivalent. Record resolution/spatial weight/seed and assess stability. Validate against morphology/known anatomy without circularly forcing annotations.

### SVG and differential analysis

Separate:

1. within-section spatial autocorrelation;
2. recurrent spatial pattern across sections/subjects;
3. between-group differential spatial pattern;
4. within-cell-type spatial regulation.

Each has a different null and unit of inference. Correct for all tested genes/modules and avoid describing composition-driven markers as cell-intrinsic regulation.

### Deconvolution

Use on mixed spots/bins. Compare reference labels/features with the spatial platform, record preprocessing compatibility, donor leakage risk, missing cell types, uncertainty, and residuals. Do not train and assess on an overlapping donor without disclosure. Keep proportions/abundances distinct.

### Mapping and label transfer

Use on cell or mixed-unit data only with an explicit target. Preserve continuous scores and uncertainty rather than forcing every unit into a label. Detect out-of-reference cells/regions. Mapping similarity is not lineage evidence.

### Neighborhoods and gradients

Condition null models on sample and, where required, abundance, compartment, geometry, and boundaries. Summarize subject-level effects for cohort comparisons. Test radius/bin/graph sensitivity and distinguish local enrichment from direct contact.

### Spatial communication

Treat ligand-receptor scoring as hypothesis generation. Require expression, spatial opportunity, sample-level recurrence, and sensitivity to neighborhood definition. A score does not establish receptor activation, directionality, or causal signaling.

## 3. Statistical units and pseudoreplication

Record four units separately:

- `assay_unit`: measured spot/bin/cell;
- `spatial_unit`: coordinate-bearing unit used in graph/statistic;
- `sampling_unit`: section/FOV/library collected from a subject;
- `inference_unit`: independent subject/specimen for the target estimand.

For cohort claims, use subject-level aggregation, subject-stratified permutation/meta-analysis, or mixed/hierarchical models with the actual nesting. Serial sections and FOVs are repeated observations, not new subjects. Include batch/site/section covariates only when identifiable; do not fit a model that cannot separate group from batch.

Report the number of subjects, sections, FOVs, assay units, and retained units at each stage. Do not report only the largest number.

## 4. ArtifactContract and checkpoints

Every registered artifact conforms to [spatial-artifact-contract.schema.json](spatial-artifact-contract.schema.json) and contains:

- stable `artifact_id`, `stage_id`, `role`, `type`, `format`, and absolute/manifest-relative `path`;
- `producer` recipe/code version and `consumers`;
- content SHA-256, size, creation time, and environment lock hash;
- assay/spatial/sampling/inference unit where relevant;
- coordinate system/units/transform identifier for spatial objects and figures;
- validation rules/results, maturity, and conclusion role.

### Atomic checkpoint rule

1. Write all stage output to `<stage>/_staging/<attempt_id>`.
2. Require zero exit status and all declared artifacts.
3. Validate file readability, schema, identifiers, dimensions, units, hashes, and stage-specific scientific checks.
4. Write a checkpoint manifest and artifact-ledger entries.
5. Atomically move/register the successful stage directory.
6. Never register partial files as completed.

Resume only when upstream hashes, workflow parameters, code version, and environment lock match. Otherwise recompile from the earliest invalidated stage and retain the prior run as immutable evidence.

## 5. Visual and interpretation QA

For every spatial figure verify:

- coordinates, origin, axis direction, scale, aspect ratio, crop, transform chain, and tissue/image alignment;
- whether glyphs represent spots, bins, cells, centroids, boundaries, or interpolated surfaces;
- whether color represents raw counts, normalized values, probabilities, proportions, labels, or test statistics;
- legends, units, missing values, saturation/clipping, color-vision accessibility, and text overlap;
- sample count and independent statistical unit;
- whether a summary hides section/subject heterogeneity;
- native-resolution alignment in representative and worst regions.

Each figure note must distinguish direct visual evidence, statistical support, interpretation assumptions, and prohibited conclusions. Mark exact reproduction separately from scientific reimplementation and visual optimization.

## 6. Environment handoff

Recipes declare dependencies only. A dependency record should include ecosystem, package, source, version constraint or pinned version, optional Git SHA, system dependency, platform, and reason.

Hand the record to the shared `EnvironmentManager`:

```text
probe -> resolve -> provision -> verify -> freeze -> execute -> report_failure
```

`plan` stops before provisioning. `run` requires explicit authorization and a task-isolated environment. Verify import/API signatures and a minimal platform reader before freezing. Do not modify global libraries, base Conda, system PATH, or administrator settings. A dependency change creates a new locked environment version. Installation failure must retain exact command/exit/error classification and present scientifically equivalent or non-equivalent alternatives explicitly.
