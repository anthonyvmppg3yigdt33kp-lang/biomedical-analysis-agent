# biomedical-analysis-agent v1.0.0

First public-release payload for the reproducible biomedical analysis
orchestrator. This tracked document is lifecycle-neutral: publication status is
established by the immutable `v1.0.0` tag plus the attached, commit-bound release
validation assets, not by wording changed after the candidate commit.

- Bundles the core skill and five domain workflow skills.
- Adds pinned task-local visualization bootstrap and deterministic routing.
- Adds Seurat 5.5.0 PBMC3K and Visium Mouse Brain tutorial interfaces from
  prompt and plan through checkpoints, figures, QA, and report.
- Separates descriptive teaching results from inferential claims and private
  execution overlays.

Publication requires Windows R 4.5.3 execution, remote CI, native figure review,
and a clean anonymous clone. The generated `RELEASE_VALIDATION.md`,
`release-validation-summary.json`, self-verifying sanitized evidence ZIP, and
`SHA256SUMS.txt` assets bind those checks to the release commit without rewriting
a tracked file after that commit.

Legacy Visium and PBMC execution snapshots are not valid for the corrected
native-exit runtime. Release evidence for this version must instead bind the
complete Bioconductor 3.21 closure and explicit Visium `glmGamPoi_offset/v2` to
fresh/resume/cache/negative-control/native-review/expected-output runs, anonymous
clone validation, and remote CI.
The pinned upstream spatial Recipe is a plotting-contract validation only until
its previews are rerendered and reviewed from the warning-safe final object.
