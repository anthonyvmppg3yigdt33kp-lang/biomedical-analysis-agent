# Third-party data and attribution

No raw third-party data is committed to or redistributed with this repository.
The tutorial downloaders store inputs in an ignored task-local cache and verify
them before analysis.

## PBMC3K

- Provider: 10x Genomics
- Dataset: 3k PBMCs from a Healthy Donor, filtered gene-barcode matrices
- Method reference: <https://satijalab.org/seurat/articles/pbmc3k_tutorial.html>
- Data URL: <https://cf.10xgenomics.com/samples/cell/pbmc3k/pbmc3k_filtered_gene_bc_matrices.tar.gz>
- Download size: 7,621,991 bytes
- SHA-256: `847d6ebd9a1ec9a768f2be7e40ca42cbfe75ebeb6d76a4c24167041699dc28b5`
- Data license: Creative Commons Attribution 4.0 International (CC BY 4.0)
- Attribution: Copyright 10x Genomics; used for a reproducible teaching workflow.

## Mouse Brain Sagittal-Anterior

- Provider: 10x Genomics
- Dataset: Mouse Brain Serial Section 1, Sagittal-Anterior, Visium 1.1.0
- Method reference: <https://satijalab.org/seurat/articles/spatial_vignette.html>
- Filtered H5: <https://cf.10xgenomics.com/samples/spatial-exp/1.1.0/V1_Mouse_Brain_Sagittal_Anterior/V1_Mouse_Brain_Sagittal_Anterior_filtered_feature_bc_matrix.h5>
- Spatial archive: <https://cf.10xgenomics.com/samples/spatial-exp/1.1.0/V1_Mouse_Brain_Sagittal_Anterior/V1_Mouse_Brain_Sagittal_Anterior_spatial.tar.gz>
- Default profile excludes the approximately 405 MB raw microscopy TIFF.
- Filtered H5 size: 20,554,697 bytes
- Filtered H5 SHA-256: `56078d8d6fe6c13de248fdb1c518b691cdef78fb00021b659786b4a47c6656d5`
- Spatial archive size: 9,233,573 bytes
- Spatial archive SHA-256: `5f41a803e2bd69fa4dfca6abc8fa2d4e0d76aeb6c72d7038a5fdcf9cc50a36f8`
- These values were frozen after the first verified real-data download; the
  downloader now rejects any mismatch and has no first-learning mode.
- Data license: Creative Commons Attribution 4.0 International (CC BY 4.0)
- Attribution: Copyright 10x Genomics; used for a reproducible teaching workflow.

CC BY 4.0 license text: <https://creativecommons.org/licenses/by/4.0/>

Derived tables, reports, and figures may be distributed only with the above
attribution and without suggesting endorsement by 10x Genomics. Generated
Seurat objects and cached source archives are excluded from release assets.

## Additional P0 teaching-case inputs

The runners under `assets/teaching-cases/` contain no upstream package archives,
package-embedded data objects, pretrained models, raw mass-spectrometry files,
or Spotiphy tutorial binaries. Their case specifications and source manifests
record identifiers and integrity values so that a separately acquired,
read-only input can be checked before execution. In particular:

- the CLL MOFA-FLEX case excludes `cll.h5mu` and `cll_model.h5`; its manifest
  requires review of the underlying CLL data reuse terms before redistribution;
- the Spotiphy case excludes its AnnData, histology, and Visium HD inputs and
  explicitly marks tutorial-byte redistribution as unauthorized by the run;
- the DEP UbiLength case excludes the DEP source archive, embedded R data
  objects, source article, and raw mass-spectrometry files.

These exclusions are deliberate. The licenses of the referenced software
repositories or packages do not, by themselves, establish redistribution
rights for the associated scientific datasets.
