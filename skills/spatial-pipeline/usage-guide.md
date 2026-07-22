# Spatial Transcriptomics Pipeline - Compatibility Note

## Overview

This legacy quick guide is retained for compatibility. Use `SKILL.md` plus `references/platform-input-contracts.md` and `references/workflow-artifact-contract.md` as the authoritative workflow. The workflow now covers Visium, Visium HD, Stereo-seq, Xenium, CosMx, MERFISH, and documented compatible assays.

## Dependencies

Recipes declare dependencies but never install them. The shared `EnvironmentManager` probes, resolves, provisions, verifies, and freezes a task-isolated environment only after explicit `run` authorization.

## Quick Start

Tell your AI agent what you want to do:
- "Analyze my Visium spatial transcriptomics data"
- "Find spatially variable genes in my tissue"
- "Identify spatial domains in my sample"

## Example Prompts

### Loading and QC
> "Load my Space Ranger output"

> "Show QC metrics on the tissue image"

### Analysis
> "Find spatially variable genes"

> "Run neighborhood enrichment analysis"

> "Detect spatial domains"

### Visualization
> "Plot gene expression on the tissue"

> "Show clusters overlaid on the image"

## Input Requirements

| Input | Format | Description |
|-------|--------|-------------|
| Vendor output | Directory | Space Ranger, Xenium, CosMx, MERFISH, or Stereo-seq processed output |
| Matrix + coordinates | Files | Explicit count/expression and coordinate representation |
| Images/transforms | Files | Required for coordinate-faithful image overlays |
| Subject metadata | CSV/TSV | `sample_id`, `subject_id`, `section_id`, and `group` for contrasts |

## What the Workflow Does

1. **Validate inputs** - Freeze platform, assay unit, coordinate system, image transform, and subject/section nesting.
2. **Load without mutation** - Preserve raw counts/molecules, coordinates, images, segmentation, and provenance.
3. **Platform-aware QC** - Branch spot/bin/cell, capture/imaging, and image/segmentation checks.
4. **Core spatial analysis** - Graph, domains, SVGs, and subject-aware contrasts.
5. **Optional modules** - Deconvolution, single-cell mapping, neighborhoods, gradients, communication, and image features.
6. **QA and delivery** - Atomic checkpoints, artifact ledger, native image review, and bounded interpretation.

## Tips

- **Platform fork**: decide imaging vs sequencing first; it sets QC floors and whether you deconvolve or segment
- **QC floors**: Visium UMI/gene floors are tissue-dependent starting points; never apply a 500-count floor to imaging data
- **SVGs**: gate top Moran's I on FDR, and separate composition-driven SVGs (cell-type markers) from within-type regulation
- **nhood enrichment**: a positive z is not a specific interaction; the global null is confounded by abundance and compartments
- **Deconvolution and communication**: run them through spatial-deconvolution and spatial-communication, which carry the reference and spillover caveats
