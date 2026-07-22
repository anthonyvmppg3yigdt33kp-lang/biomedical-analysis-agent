# Figure notes

All figures use the R backend and the same frozen, post-QC PBMC3K analysis baseline. Original and final-size PNGs contain identical cells, values, normalization, PCA/UMAP and clusters; final variants change only declared visual parameters.

| Figure | Question | Directly visible | Supported | Not supported |
|---|---|---|---|---|
| `qc_violin` | What are the per-cell QC distributions? | Distributions of detected features, counts and mitochondrial percentage among retained cells. | Descriptive QC overview. | Donor variability or optimality of thresholds. |
| `pca_clusters` | How do cells occupy the PCA representation? | Cluster-colored cell positions in PC1/PC2. | Descriptive representation structure. | Effect magnitude, lineage or time. |
| `umap_clusters` | How do graph clusters occupy UMAP? | Nine cluster labels and their UMAP neighborhoods. | Cell-level descriptive organization. | Metric distance, abundance significance or developmental trajectory. |
| `umap_annotation` | Where are teaching labels located? | Conservative PBMC teaching labels on UMAP. | Reference-guided descriptive labeling. | Clinical classification or donor-level prevalence. |
| `marker_dotplot` | Which canonical markers characterize labels? | Average normalized expression color and percent-expressed dot size. | Marker-pattern review. | Differential expression between conditions, mechanism or causality. |

See the hash-bound JSON files in `../figures/review/` for native review evidence and decisions.
