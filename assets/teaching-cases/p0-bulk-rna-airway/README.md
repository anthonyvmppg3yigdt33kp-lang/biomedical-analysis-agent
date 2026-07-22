# P0 Bulk RNA teaching case: airway

This asset is a public, path-independent runner for the official Bioconductor `airway` experiment. It demonstrates a paired-block bulk RNA-seq analysis with DESeq2.

## Scientific contract

- Official data package: `airway` 1.30.0; accession GSE52778; 63,677 features x 8 samples. This dimension is checked against the materialized Bioconductor 3.22 object and supersedes the earlier unverified candidate estimate.
- Experimental unit: airway smooth-muscle cell line; four cell lines each contribute one `trt` and one `untrt` sample.
- Model: `~ cell + dex`.
- Contrast: `trt - untrt`; positive log2 fold change means higher expression in `trt`.
- Pre-filter: raw count >=10 in at least 4 samples.
- Inference: DESeq2 Wald test with Benjamini-Hochberg adjustment; FDR threshold 0.05.
- Boundary: teaching evidence in four cell lines only; no patient, clinical, causal, or population-generalization claim.

## Runtime

Use R 4.5.3 with Bioconductor 3.22 and these direct package versions:

- `airway` 1.30.0
- `DESeq2` 1.50.2
- `BiocVersion` 3.22.0
- `BiocManager` 1.30.27
- `matrixStats` 1.5.0

Dependency installation is deliberately outside the analysis runner. The runner verifies direct versions and stops on a mismatch; it never calls `install.packages()`, `BiocManager::install()`, `pak`, or `renv`.

## Run

```powershell
Rscript.exe --vanilla run_pipeline.R --output-dir airway-teaching-output
python verify_outputs.py airway-teaching-output
```

The output directory must not already exist. The runner creates `tables/`, `objects/`, `figures/original/`, `figures/final/`, `reports/`, and `provenance/`, then writes a SHA-256 artifact manifest. `verify_outputs.py` checks scientific invariants, exact direct versions, figure dimensions, inventory, and every declared hash. Native pixel review remains a separate required delivery step.

Official references: [airway package manual](https://www.bioconductor.org/packages/release/data/experiment/manuals/airway/man/airway.pdf) and [DESeq2 3.22 package page](https://bioconductor.org/packages/3.22/bioc/html/DESeq2.html).
