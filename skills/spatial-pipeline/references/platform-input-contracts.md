# Platform and input contracts

## Contents

1. Shared manifest
2. Platform-specific requirements
3. Metadata and coordinate tables
4. Platform-aware QC
5. Stop conditions

## 1. Shared manifest

Use a JSON object with `schema_version`, `platform`, `assay_class`, `assay_unit`, `species`, `coordinate_unit`, `samples`, and `requested_modules`. Each sample must include `sample_id`, `subject_id`, and `section_id`. Represent files with these optional keys:

- `input_root`: immutable vendor output directory;
- `matrix_path`: count/expression matrix;
- `coordinates_path`: unit coordinates;
- `metadata_path`: unit/sample metadata;
- `image_path`: histology or morphology image;
- `transform_path`: scale factors, affine transform, or vendor transform bundle;
- `transcripts_path`: molecule-level transcript coordinates;
- `segmentation_path`: cell/nucleus geometry or mask;
- `controls_path`: negative/control probes or quality metrics;
- `reference_path`: sc/snRNA reference used for mapping or deconvolution.

Paths may be relative to the manifest directory or absolute. Preserve source files read-only and record file hashes at `S00_INTAKE`. A vendor `input_root` can satisfy file-level structural requirements at intake, but `S10_INGEST` must resolve and record the actual files consumed.

Recommended example:

```json
{
  "schema_version": "1.0",
  "platform": "visium",
  "assay_class": "capture",
  "assay_unit": "spot",
  "species": "human",
  "coordinate_unit": "pixel",
  "requested_modules": ["domains", "svg", "deconvolution", "image_overlay"],
  "samples": [
    {
      "sample_id": "S01_secA",
      "subject_id": "P01",
      "section_id": "secA",
      "group": "case",
      "input_root": "inputs/P01_secA/outs"
    }
  ]
}
```

## 2. Platform-specific requirements

### Visium

- Declare `platform=visium`, `assay_class=capture`, and `assay_unit=spot`.
- Accept a Space Ranger output root or an explicit matrix plus coordinate table.
- For histology overlay, resolve tissue image and scale factors/transform; do not infer orientation by eye.
- Validate barcode identity across matrix, tissue positions, and metadata.
- Treat each spot as a mixture. Use deconvolution for cell-type composition claims.
- Use lattice-aware neighbors when vendor coordinates preserve the hex grid; document any distance graph instead.

### Visium HD

- Declare `platform=visium_hd`, `assay_class=capture`, and `assay_unit=bin` or `cell` only when a documented segmentation/aggregation created cells.
- Record original bin size, analysis bin size, aggregation/segmentation method, coordinate scale, and whether counts were reassigned.
- Repeat key findings across at least one scientifically reasonable bin/resolution or explain why this is impossible.
- Do not call binned units cells unless a segmentation-backed mapping exists.

### Stereo-seq

- Declare `platform=stereo_seq`, `assay_class=capture`, and `assay_unit=bin` or documented derived `cell`.
- Record bin size (for example bin20/bin50), coordinate unit, resolution conversion, mask/segmentation source, and chip/tissue boundaries.
- Validate x/y uniqueness rules and coordinate extent; inspect edge/background bins separately.
- Treat bin-size choice as an analysis parameter with sensitivity implications.

### Xenium

- Declare `platform=xenium`, `assay_class=imaging`, and `assay_unit=cell` for the cell matrix.
- Preserve molecule-level transcripts, cell/nucleus boundaries, morphology images, panel definition, negative/control probes, and transforms where available.
- Audit segmentation: cell/nucleus area, transcript allocation, boundary leakage, empty/oversized cells, and regional/FOV effects.
- Use panel-aware QC. Mitochondrial fraction may be unavailable and a Visium count floor is invalid.
- Skip deconvolution unless cells were deliberately aggregated into mixed units.

### CosMx

- Declare `platform=cosmx`, `assay_class=imaging`, and `assay_unit=cell`.
- Preserve FOV identity, stitching/global coordinates, morphology channels, segmentation, transcripts, panel, negative probes, and run QC.
- Model/stratify FOV and sample; do not treat FOVs as patient replicates.
- Check FOV seams and transform consistency before global-neighborhood analyses.

### MERFISH

- Declare `platform=merfish`, `assay_class=imaging`, and `assay_unit=cell` when segmentation exists.
- Preserve decoded molecule coordinates, codebook/panel, registration transforms, segmentation, image channels, and imaging round/FOV identifiers.
- Audit decoding/control metrics, registration, molecule density, segmentation, and FOV/batch effects.
- State whether the panel is targeted and limit absence/generalization claims accordingly.

### Other platforms

Use `other_capture` or `other_imaging`, document the closest contract, and add explicit deviations. Do not force an unsupported vendor reader; export to a declared common representation while retaining vendor-native assets and provenance.

## 3. Metadata and coordinate tables

Use UTF-8 CSV or TSV with unique column names.

### Sample/subject metadata

Minimum columns for cohort inference:

- `sample_id`: unique assay section/library identifier;
- `subject_id`: independent donor/specimen identifier;
- `section_id`: section identifier nested in subject;
- `group`: comparison group when a group contrast is requested.

Also record batch, site, tissue region, acquisition/run, FOV, treatment, time point, paired structure, and clinically relevant confounders. Reject a design in which group is perfectly confounded with batch/site when no valid estimand remains.

### Unit metadata

Minimum columns: `sample_id`, `unit_id`. Keep unit IDs unique within sample; the composite `sample_id + unit_id` must be globally unique. Include QC/annotation columns as new derived fields and preserve originals.

### Coordinates

Minimum columns: `sample_id`, `unit_id`, `x`, `y`, `coordinate_system`. Optional: `z`, `in_tissue`, `fov`, `row`, `col`.

- Require finite numeric `x`/`y`; reject duplicate composite unit IDs and conflicting duplicate coordinates.
- Declare unit (`pixel`, `micron`, `array`, or `other`) and origin/orientation in the manifest or transform record.
- Verify coordinate membership against the matrix and report unmatched units in both directions.
- Compare bounds with the image or tissue mask after the declared transform; never silently flip the y-axis.
- For multi-FOV data, retain both local and global coordinates with an explicit transform chain.

## 4. Platform-aware QC

### Capture assays

Inspect total counts, detected genes, mitochondrial/ribosomal fractions when meaningful, tissue/background labels, spatial gradients, edge effects, library/section differences, and image alignment. Derive thresholds per tissue/platform with an attrition table; fixed example values are not universal defaults.

### Imaging assays

Inspect decoded transcripts, genes detected, negative/control probes, cell/nucleus area and shape, transcript density, fraction assigned to cells, segmentation leakage, empty/oversized cells, FOV/run effects, panel coverage, and image registration. Avoid whole-transcriptome QC assumptions for targeted panels.

### Image and segmentation QC

At native resolution inspect multiple representative and worst-case regions. Confirm transform, orientation, scale, tissue mask, boundary alignment, and FOV stitching. Quantify segmentation/registration metrics where available; a visually attractive downsampled overlay is not sufficient evidence.

## 5. Stop conditions

Pause before execution or inference when any of the following holds:

- platform or assay unit is unknown;
- coordinate frame/unit/orientation cannot be resolved;
- matrix-unit and coordinate identifiers cannot be reconciled;
- requested image overlay lacks a resolvable image/transform chain;
- subject identity/group/nesting is missing for cohort claims;
- an imaging assay lacks essential panel/control or segmentation provenance for the requested conclusion;
- a mixed-unit composition claim lacks a valid deconvolution/mapping strategy;
- group is completely confounded with batch/site and no defensible estimand exists;
- the requested method requires inputs not present or would be replaced by a scientifically non-equivalent method.
