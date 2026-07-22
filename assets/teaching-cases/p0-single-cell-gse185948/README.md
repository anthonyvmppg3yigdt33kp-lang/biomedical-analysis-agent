# GSE185948 single-nucleus RNA-seq teaching runner

This asset preserves the complete reduced-real-data workflow that was validated
on the five healthy kidney donors GSM5627690-GSM5627694. Binary 10x HDF5 inputs
are intentionally excluded. The public metadata sidecar remains hash-bound at
references/p0-single-cell-gse185948-metadata.json.

The workflow is deliberately conservative:

- it reads each 10x HDF5 file without changing it;
- it deterministically samples at most 1,000 nuclei per donor before loading
  the sparse count payload;
- it derives nucleus-aware QC thresholds separately for each donor;
- it runs Scrublet scoring per donor, but applies no doublet filter unless every
  donor passes the same reliability gate;
- it retains an unintegrated PCA because donor, library and batch are
  confounded one-to-one;
- it performs coarse marker-panel annotation with an explicit
  unknown/ambiguous state;
- it performs no case-control test and no patient-level clinical inference.

## Files

- case-spec.yaml: immutable scientific and validation boundary.
- input-config.example.json: path-free template with the five expected source
  hashes, sizes and matrix dimensions.
- run_pipeline.py: complete analysis code copied byte-for-byte from the
  data-verified formal execution.
- run_case.py: task runner that verifies inputs and a frozen python-uv
  environment, stages output, validates it, then registers checkpoints.
- verify_outputs.py: standard-library-only output contract validator.

## Execution

First copy input-config.example.json to a task-local configuration and replace
the five placeholder paths. Prepare and freeze a task-local Python environment
through the shared EnvironmentManager. The environment must contain the
dependencies declared by the workflow, including scanpy, anndata, h5py,
numpy, pandas, scipy, scikit-learn, matplotlib, igraph and leidenalg.

Then run:

    python run_case.py ^
      --run-root <managed-task-root>\runs\p0-single-cell-gse185948\<run-id> ^
      --input-config <task-local-input-config.json> ^
      --environment-plan <frozen-environment-plan.json>

Calling run_case.py is an explicit run request. It does not install packages and
does not modify a base environment, the system PATH, source files or global
package libraries. Failed analysis remains under _staging with a failure record;
only a contract-valid analysis is atomically promoted.

## Verified reference execution

The original formal run used a frozen, self-contained Python snapshot with lock
hash e6a35fca578e65e9ccd32ce9644a94b7d34f4591ec6273a3357081826ee5df24.
It read 56,728 filtered nuclei across five donors, analyzed a deterministic
5,000-nucleus fixture, retained 4,919 nuclei after distribution-derived QC,
selected 2,773 genes, produced 15 Leiden clusters and six figures, and passed
machine QA with warnings. Those warnings are scientifically material: ambient
RNA could not be estimated from filtered matrices, doublet filtering was not
applied, donor and batch could not be separated, and the coarse labels were not
reference validated.

This asset can validate mechanics and descriptive single-nucleus analysis. It
cannot support disease effects, population prevalence, causality, prognosis,
treatment response or clinical decision-making.

