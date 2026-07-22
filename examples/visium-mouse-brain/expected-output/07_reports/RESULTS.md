# RESULTS

## Data and reconciliation

- Matrix barcodes: 2695
- Vendor in-tissue barcodes: 2695
- Loaded and retained spots: 2695
- Spatial assay cells / image cells / coordinate barcodes: 2695 / 2695 / 2695
- All six directed assay/image/coordinate set differences are zero; H5 ∩ vendor in-tissue and the loaded object also reconcile exactly.
- Vendor image assets and positive finite scale factors passed structural QC; the hash-bound native alignment review passed.

## Descriptive analysis

- Expression-derived spot clusters: 11
- Normalization and reduction: SCTransform(vst.flavor = v2, method = glmGamPoi_offset) on Spatial assay followed by PCA.
- Backend evidence: glmGamPoi 1.20.0, BiocVersion 3.21.1, SparseArray 1.8.1; 3000 SCT variable features; 30 finite PCs; no spot attrition.
- Feature overlays: Hpca and Ttr from the SCT data slot.
- Pixel-grounded biological descriptions will be added only after native review of both original and final-size images.

## Claim boundary

This is a single-section, spot-level descriptive tutorial. Spots are mixtures, not cells; clusters are not cell types or spatially regularized domains. No donor/group effect, population generalization, mechanism, interaction, or causal claim is supported.

## Native visual review

Overall decision: `keep` (round 1).

### Pixel-grounded observations

- `spatial_qc`: Both nCount_Spatial and nFeature_Spatial spot layers follow the same tissue image geometry without a visible systematic offset.
- `spatial_qc`: The title, panel labels, color bars, ticks, tissue image, and spot overlays are visible in both the original and final-size exports.
- `spatial_qc`: The final-size export is more compact while retaining the spatial gradients visible in the original export.
- `spatial_clusters`: Eleven spot clusters labeled 0 through 10 are shown with distinct colors over the tissue image.
- `spatial_clusters`: Cluster labels, leader lines, legend entries, tissue background, and spot positions remain readable in both exports.
- `spatial_clusters`: The final-size export enlarges the text and legend relative to the plotting area without clipping labels or tissue.
- `spatial_features_hpca_ttr`: Hpca shows a broader central-to-lower spatial signal, whereas the strongest Ttr signal is localized toward the right upper portion of the section.
- `spatial_features_hpca_ttr`: Both feature overlays follow the tissue and spot geometry consistently in the original and final-size exports.
- `spatial_features_hpca_ttr`: Feature names, color bars, tick labels, tissue background, and spot signals are visible without clipping.

### Cannot assert

- `spatial_qc`: donor or group effect
- `spatial_qc`: cell identity
- `spatial_qc`: biological mechanism or causality
- `spatial_clusters`: cluster equals cell type
- `spatial_clusters`: spatially regularized anatomical domain
- `spatial_clusters`: donor or population effect
- `spatial_clusters`: mechanism or causality
- `spatial_features_hpca_ttr`: cell identity or purity
- `spatial_features_hpca_ttr`: marker enrichment tested by an inferential model
- `spatial_features_hpca_ttr`: direct cell-cell interaction
- `spatial_features_hpca_ttr`: mechanism or causality
- `spatial_features_hpca_ttr`: generalization beyond this section

## Environment cache provenance

The cached environment was originally built under basename `visium-mouse-brain-r453-seurat550-5324b3ca515435690ada4e70`; that value is retained only as historical build provenance.
This run used current hash-bound cache key `166efc610ed4b0d8d5004f1b` and current basename `visium-mouse-brain-r453-seurat550-166efc610ed4b0d8d5004f1b` only after a fresh native R validation of exact package versions, the reviewed renv.lock, zero status differences, an empty restore plan, and the frozen H5 smoke input.
