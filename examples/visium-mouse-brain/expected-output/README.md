# Verified Visium Mouse Brain teaching output

This directory is a deterministic snapshot from the real Seurat 5.5.0 Mouse Brain Sagittal-Anterior run. It contains only compliant derived tables, reports, original/final PNG pairs, terminal native-review evidence, and sanitized validation/provenance records.

Observed in this single-section descriptive run: 2,695 matrix/in-tissue/loaded/retained spots, zero across all six directed assay/image/coordinate barcode differences, and 11 expression-derived spot clusters. These clusters are not cell types or population-level effects.

The 10x input files, Seurat R object, task-local renv library, binaries, caches and checkpoints are deliberately not distributed. Data attribution and frozen downloader hashes are recorded under `provenance/`; the original 10x data remain CC BY 4.0 and are not relicensed by the repository MIT license.

The snapshot can only be exported from a fresh run of the exact current candidate code. Its structured warning ledger must be bound to the executed code/config/environment and contain zero release blockers; unknown, API, numerical and spatial-integrity warnings fail closed.

From the repository root, verify the exact inventory, hashes, PNG containers/dimensions, barcode reconciliation, native-review bindings, failure injections and path sanitization with:

```powershell
python examples/visium-mouse-brain/verify_expected_output.py
```
