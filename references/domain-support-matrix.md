# Domain support and claim boundary

## P0 formal workflows

| Domain | Delegated Skill | Formal boundary |
|---|---|---|
| Single-cell RNA | `bio-workflows-scrnaseq-pipeline` | import through donor-aware inference and optional trajectory/CNV/communication |
| Spatial transcriptomics | `bio-workflows-spatial-pipeline` | platform-aware spot/bin/cell processing through spatial inference and image overlays |
| Bulk RNA | `bulk-rnaseq` | FASTQ or matrix entry through differential expression and governed downstream modules |
| Quantitative proteomics | `quantitative-proteomics-workflow` | post-search peptide/protein quantification; excludes RAW/mzML search |
| Multi-omics | `multi-omics-pipeline` | contracted aligned inputs for MOFA2/DIABLO/SNF/mixOmics-style integration |
| Visualization | `visualization-2026718-v1` | semantic figure choice, variants, reproduction, visual QA, and explanation |
| Literature methodology | this Skill | question-to-method logic, validation chain, alternatives, and claim ceiling |

Formal support means the workflow contract, routing, artifacts, scientific gates, and environment handoff exist. It does not imply every package workflow is `data-verified`; inspect the selected Recipe or PackageCard maturity before execution.

## P1 and candidate-only domains

- `ribo-seq`: P1. Corpus MethodCards and PackageCards may be retrieved, but there is no formal end-to-end workflow in this release.
- metabolomics, ATAC-seq, methylation, GWAS, somatic mutation, and CNV pipelines: candidate method modules only unless an installed dedicated Skill is explicitly selected and its own contract passes review.
- Raw mass-spectrometry search, clinical diagnosis/treatment, and regulatory decisions are outside the workflow boundary.

For candidate-only domains, route to literature methodology, retrieve evidence, describe missing contracts, and stop at `PLAN_COMPILED`. Never relabel a nearby P0 workflow as scientifically equivalent.
