---
name: bio-workflows-spatial-pipeline
description: Design, execute, resume, and audit platform-aware spatial transcriptomics workflows for Visium, Visium HD, Stereo-seq, Xenium, CosMx, MERFISH, and related assays. Use for spatial input and coordinate validation, image/segmentation QC, spot/bin/cell preprocessing, spatial domains and variable genes, deconvolution, neighborhoods, gradients, communication, single-cell mapping, image overlays, donor-aware inference, checkpointed delivery, or review of what a spatial result can and cannot support.
---

# Spatial Transcriptomics Pipeline

Treat platform, assay unit, coordinate frame, image transform, and biological replicate as explicit inputs. Do not compile a generic “spatial” script before those facts are known.

## Non-negotiable gates

1. Classify the assay as `capture` or `imaging` and identify the exact platform.
2. Identify the observational unit (`spot`, `bin`, or segmented `cell`) separately from the independent inference unit (normally donor/specimen).
3. Validate matrix/metadata identities, coordinates, image/segmentation assets, and transforms before analysis.
4. Preserve raw counts/molecules and vendor coordinates; write corrected or transformed values as new artifacts.
5. Stop group-level inference when subject identity, group assignment, or nesting of sections within subjects is missing.
6. Do not treat spots, bins, cells, fields of view, or serial sections as independent patient replicates.
7. Do not silently replace statistically different domain, SVG, deconvolution, mapping, or communication methods.

Read [platform-input-contracts.md](references/platform-input-contracts.md) for platform-specific required inputs and [workflow-artifact-contract.md](references/workflow-artifact-contract.md) for the stage DAG, artifacts, checkpoints, inference rules, and method-selection boundaries.

## Intake and deterministic validation

Create a task-local `spatial-input-manifest.json` following the platform contract. Validate it before loading a large object:

```powershell
python scripts/validate_spatial_inputs.py manifest spatial-input-manifest.json
python scripts/validate_spatial_inputs.py manifest spatial-input-manifest.json --check-paths
python scripts/validate_spatial_inputs.py table coordinates.csv --kind coordinates
python scripts/validate_spatial_inputs.py table metadata.csv --kind metadata
```

The validator uses only the Python standard library. A successful structural check does not prove image registration, segmentation quality, count integrity, or biological adequacy; those remain explicit stage gates.

## Platform fork

| Assay class | Platforms | Primary unit | Essential branch |
|---|---|---|---|
| Capture, spot | Visium | spot mixture | tissue/image alignment, spot QC, deconvolution when cell composition is needed |
| Capture, high resolution | Visium HD, Stereo-seq | bin or derived cell | bin-size declaration, coordinate scale, aggregation provenance, sensitivity to resolution |
| Imaging, molecule based | Xenium, CosMx, MERFISH | segmented cell | transcript/control-probe QC, segmentation QC, cell geometry; normally no deconvolution |

Never apply Visium count floors to imaging assays. Never describe a Visium spot cluster as a cell type. Never treat an imaging cell matrix as complete-transcriptome data when it was measured with a targeted panel.

## Compile the workflow

Use these ordered stages and retain optional branches in the compiled plan:

1. `S00_INTAKE`: freeze request, manifest, source hashes, platform, assay unit, subject/section nesting, coordinate units, image frame, and requested conclusions.
2. `S10_INGEST`: load vendor-native data without mutating raw values; reconcile identifiers and feature annotations.
3. `S20_COORD_IMAGE_QC`: verify coordinate bounds, orientation, scale factors/transforms, tissue masks, registration, segmentation, field-of-view stitching, and control probes as applicable.
4. `S30_UNIT_QC`: calculate platform-aware QC; document adaptive thresholds and attrition by subject/section/region.
5. `S40_PREPROCESS`: preserve counts, normalize with an assay-appropriate method, create reductions, and retain full object plus analysis view.
6. `S50_SPATIAL_GRAPH`: build a graph that matches lattice/geometry, record distance units and graph parameters, and inspect boundary effects.
7. `S60_CORE_DISCOVERY`: run transcriptomic clustering, spatial domains, SVG analysis, and within-domain/within-cell-type contrasts with FDR control.
8. `S70_COMPOSITION_MAPPING`: branch to deconvolution for mixtures or scRNA mapping/label transfer with reference compatibility checks.
9. `S80_ADVANCED`: run neighborhoods, gradients, spatial communication, morphology/image overlays, or other declared modules with appropriate null models.
10. `S90_INFERENCE_QA`: aggregate or model at the independent replicate level, test sensitivity to thresholds/resolution/graph/reference, and audit multiplicity and confounding.
11. `S95_VISUALIZE_INTERPRET`: create coordinate-faithful figures, native-review them, and state direct observations, supported conclusions, and prohibited interpretations.

Each stage writes to `_staging`, validates its artifact contracts, and is registered atomically only after success. Resume only from a checkpoint whose upstream artifact hashes, parameters, code version, environment lock hash, and validation status match.

## Select methods by question, not popularity

- **Spatial domains:** distinguish expression-only clusters from explicitly spatial models. Record spatial weight/resolution and compare at least one sensitivity setting. A domain is a region, not automatically a cell type or interaction niche.
- **Spatially variable genes:** apply multiple-testing correction and separate composition-driven spatial patterns from within-cell-type regulation. Use subject-aware meta-analysis or a model that preserves subject structure for cohort claims.
- **Deconvolution:** use only for mixed units. Require a compatible single-cell/snRNA reference or a clearly labeled reference-free objective; assess reference coverage and composition-sum behavior.
- **Single-cell mapping:** document feature overlap, reference tissue/state/platform, mapping uncertainty, out-of-reference handling, and whether labels or continuous programs were transferred.
- **Neighborhoods and gradients:** use nulls/permutations that preserve sample, abundance, tissue compartment, and spatial structure as required. A positive adjacency score is not evidence of molecular interaction.
- **Spatial communication:** require expressed ligand/receptor, spatial opportunity, sample-level reproducibility, and sensitivity to neighborhood radius. Report as a hypothesis, not causal signaling proof.
- **Image overlays:** preserve aspect ratio, origin, axis orientation, crop, scale, transform chain, and the distinction between measured coordinates and a display-only offset.

## Statistical and interpretation boundaries

- Use subject/specimen as the default independent unit for cohort contrasts. Model sections/fields of view as nested repeated observations.
- For two-group claims, prefer subject-level summaries/pseudobulk or mixed/hierarchical models. A test across thousands of spots or cells without subject blocking is pseudoreplication.
- Report effect sizes and uncertainty alongside FDR. Do not relax thresholds to manufacture expected biology.
- Separate `cell type`, `cell state/program`, `spatial domain`, `neighborhood`, and `morphologic region` in names and claims.
- A spatial overlay shows co-location at the assay resolution; it does not by itself prove direct contact, interaction, lineage, directionality, or causality.
- A targeted imaging panel cannot support unbiased transcriptome-wide absence claims.

## Environment handoff

Analysis recipes declare package names, versions/ranges, system dependencies, source, and command entry points; they contain no installation commands. Hand dependency declarations to the shared `EnvironmentManager` using `probe → resolve → provision → verify → freeze → execute → report_failure`.

Only provision after explicit `run` authorization. Use a task-isolated lock environment; never modify a base environment, system `PATH`, global R/Python library, or administrator setting. Freeze the verified environment before analysis, and create a new environment version if dependencies change. If installation fails, retain the failure evidence and offer alternatives with their scientific differences.

## Required delivery

Return the compiled design, immutable input manifest, dependency declaration/lock, full scripts, parameters, checkpoints, tables/objects, original and final figures, artifact ledger, QA report, and figure notes. For every figure state:

- research question, data, assay unit, and independent statistical unit;
- what is directly visible;
- what the result supports and does not support;
- statistical/method assumptions and relevant sensitivity checks;
- coordinate/image review and visual-quality conclusion;
- reproduction level and source/provenance.

Use the schema in [spatial-artifact-contract.schema.json](references/spatial-artifact-contract.schema.json) to validate registered artifacts.

## Bundled resources

- [platform-input-contracts.md](references/platform-input-contracts.md): vendor/platform inputs, coordinate and metadata contracts, QC branches.
- [workflow-artifact-contract.md](references/workflow-artifact-contract.md): stage DAG, module choices, checkpoint rules, inference and visual QA.
- [validate_spatial_inputs.py](scripts/validate_spatial_inputs.py): standard-library validator for manifests and CSV/TSV tables.
- [visium_workflow.py](examples/visium_workflow.py): parse-level Visium example only; adapt it through the contracts above and do not use it as a universal platform recipe.

Maturity boundary: bundled guidance and validator can be `parse-verified`/fixture-verified after tests. A platform workflow becomes `data-verified` only after successful execution on suitable data with checkpoint and artifact validation; it becomes `native-reviewed` only after the resulting figures and image alignment are inspected at native resolution.
