# spatial-pipeline skill

Platform-aware spatial transcriptomics workflow design for Visium, Visium HD,
Stereo-seq, Xenium, CosMx, MERFISH, and related assays. It keeps assay unit,
coordinate frame, image transform, section, and inference unit explicit.

Read `SKILL.md`, `references/platform-input-contracts.md`, and
`references/workflow-artifact-contract.md` before execution. Run:

```powershell
python -m pytest -q tests
```

Passing structural tests does not establish coordinate/image alignment or
biological validity; those require run-specific real-data and native review.
