# Single-cell RNA-seq workflow contract

## Contents

- [Required design facts](#required-design-facts)
- [Platform and input routing](#platform-and-input-routing)
- [Stage DAG and checkpoints](#stage-dag-and-checkpoints)
- [Statistical gates](#statistical-gates)
- [Advanced branches](#advanced-branches)
- [Environment handoff](#environment-handoff)

## Required design facts

Freeze these facts before compiling an executable workflow:

- organism, tissue, assay chemistry, gene identifier namespace and reference build;
- `sample_id`, `capture_id`, `donor_id`, condition, batch and optional subject/time fields;
- biological replicate and repeated-measures structure;
- raw-droplet availability, filtered-matrix availability, multiplexing method and demultiplexing evidence;
- primary estimand, contrasts, covariates, exclusion rules and multiplicity family;
- whether each optional branch is exploratory or confirmatory.

Do not infer `donor_id` from barcodes. Do not treat captures, libraries or cells as independent biological replicates. Pause when condition is completely confounded with batch, an inferential contrast has fewer than two biological replicates in a group, or donor identity is absent.

## Platform and input routing

| Platform family | Accepted entry | Required handling |
|---|---|---|
| 10x 3' / 5' gene expression | raw/filtered MEX, Cell Ranger H5, H5AD, RDS | Preserve raw counts; process each GEM capture separately through ambient RNA, QC and doublets. |
| 10x Feature Barcode / CITE-seq | gene-expression plus feature matrices | Demultiplex HTO before per-sample QC; keep feature modalities distinct. |
| 10x Multiome RNA assay | RNA counts plus paired-cell identity | Preserve pairing; route joint ATAC analysis to a multiome component. |
| Parse/Evercode, Drop-seq, Seq-Well | vendor matrix plus capture metadata | Use platform-specific barcode/capture structure and documented doublet expectations. |
| Smart-seq2/full-length | gene-by-cell counts and plate metadata | Do not apply droplet ambient-RNA assumptions; model plate/well effects explicitly. |
| Generic count matrix | integer gene-by-cell matrix plus cell metadata | Require explicit orientation, identifier namespace, sample/capture/donor mapping and raw-count confirmation. |
| Processed H5AD/RDS | serialized object plus provenance | Audit raw-count layer, transformations and metadata; do not claim upstream stages were run locally. |

Platform detection proposes a route; it never silently overrides declared metadata. A filtered matrix alone cannot support SoupX-style ambient estimation. CellBender requires an unfiltered droplet matrix and is an alternative method, not an automatic fallback.

## Stage DAG and checkpoints

Compile only applicable branches. Every stage writes to `_staging/<stage_id>`, validates its artifact contract, then promotes atomically.

```text
SC00_INTAKE
  -> SC01_IMPORT_AND_IDENTITY
  -> SC02_DEMULTIPLEX?                  # pooled captures only
  -> SC03_AMBIENT_RNA?                  # droplet data with raw droplets
  -> SC04_QC_PER_CAPTURE
  -> SC05_DOUBLETS_PER_CAPTURE
  -> SC06_NORMALIZE_AND_HVG_PER_SAMPLE
  -> SC07_MERGE_AND_INTEGRATE?          # multi-sample; embedding only
  -> SC08_GRAPH_CLUSTER_AND_EMBED
  -> SC09_ANNOTATE_AND_REVIEW
  -> SC10_PSEUDOBULK_DE?                # condition expression estimand
  -> SC11_DIFFERENTIAL_ABUNDANCE?       # composition estimand
  -> SC12_ADVANCED_BRANCHES?
  -> SC13_FIGURES_AND_INTERPRETATION
```

Required checkpoints:

| Checkpoint | Must establish before promotion |
|---|---|
| `identity_locked` | Cell barcode is mapped to capture, sample and donor; duplicates and unmatched metadata are resolved. |
| `raw_counts_preserved` | Integer-like uncorrected counts remain available for aggregation and count models. |
| `qc_accepted` | Thresholds are data- and tissue-aware; retained/excluded counts are reported per capture and donor. |
| `doublets_accepted` | Calls are per capture; expected-rate assumptions and enrichment by sample/cluster are reviewed. |
| `integration_accepted` | Batch mixing and biological conservation are reviewed; rare populations and condition structure were not erased. |
| `annotation_accepted` | Labels have positive and contradictory marker evidence, confidence and provenance; unknowns remain unknown. |
| `inference_ready` | Replicate unit, design matrix, contrast, covariates, rank and sample counts are validated. |
| `delivery_ready` | Tables, objects, figures, logs, hashes and claim boundaries are indexed. |

Persist a raw-count object before normalization, a post-QC object before integration, an annotated object, pseudobulk matrices and model-ready sample metadata. Never overwrite an earlier checkpoint.

## Statistical gates

- Use cluster markers for descriptive annotation, not condition inference.
- Aggregate uncorrected counts by `donor_id x cell_type` (or the declared biological unit) for condition-level expression testing. Require enough donors after cell-count eligibility filtering and keep donor-level covariates in the model.
- Test composition separately with a donor-aware method such as propeller, scCODA/sccomp or Milo. These are alternatives with different sampling assumptions; do not substitute silently.
- Reject a design matrix that is not full rank. Integration cannot repair condition-batch confounding.
- Treat repeated samples from one donor with paired/block/random-effect structure as appropriate; do not relabel them independent donors.
- Control FDR within a declared hypothesis family. Report effect size and uncertainty alongside adjusted P values.
- Use raw or properly normalized non-integrated expression for DE. Never use batch-corrected embeddings or integrated assay values as count-model input.
- Keep preprocessing and feature selection inside donor/site/time-safe resampling for predictive models.

## Advanced branches

| Branch | Entry gate | Minimum output and conclusion boundary |
|---|---|---|
| Trajectory | A continuous biological process, defensible root/terminal states, sufficient coverage and no dominant batch path | Lineage graph, pseudotime uncertainty/sensitivity and branch-associated programs. Pseudotime is not chronological time or proof of lineage. |
| CNV inference | Tumour-like cells plus credible diploid reference cells from compatible tissue/context | Relative CNV score, reference definition and method sensitivity. It does not replace DNA-level CNV or diagnose malignancy alone. |
| Cell communication | Curated ligand-receptor database/version, eligible cell counts, replicate-aware comparison | Per-sample or replicate-aware interaction summaries and database provenance. Co-expression is not physical signaling or causal direction. |
| RNA velocity | Spliced/unspliced counts and compatible chemistry/processing | Velocity diagnostics, latent-time sensitivity and excluded genes/cells. Arrows are model-dependent predictions, not direct lineage observations. |

Run each branch from the annotated, non-integrated expression checkpoint unless its documented input contract says otherwise. Record every alternative method as an explicit choice.

## Environment handoff

An analysis recipe declares runtimes and pinned dependencies only. It must not contain `install.packages`, `BiocManager::install`, `pak::pkg_install`, `pip install`, `uv add`, `conda install` or GitHub installer calls.

Hand the frozen recipe to `biomedical-analysis-agent/scripts/environment_manager.py` through:

`probe -> resolve -> provision -> verify -> freeze -> execute -> report_failure`

Use read-only `probe` and `resolve` during planning. Provision or execute only after explicit `run` authorization. Keep R and Python environments separate and exchange declared files (`h5ad`, `rds`, Matrix Market, Parquet/TSV plus metadata) through ArtifactContracts. A changed dependency or method creates a new environment revision.
