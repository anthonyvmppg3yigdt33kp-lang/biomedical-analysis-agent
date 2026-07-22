# Single-cell RNA-seq usage notes

Use `SKILL.md` as the entry point and `references/workflow-contract.md` for the formal stage, statistical and artifact rules.

Create a JSON design conforming to `references/design.schema.json`, then run:

```powershell
python scripts/validate_scrna_design.py --config design.json --check-paths --output design.validation.json
```

The validator performs read-only path checks and compiles a stage plan only when blocking scientific and structural gates pass. It does not install packages or execute an analysis.

Dependency installation and execution belong to the task-local EnvironmentManager after explicit `run` authorization. Keep R and Python package declarations in the compiled recipe; keep installer commands out of analysis recipes and workflow scripts.

The bundled Seurat and Scanpy examples are minimal single-sample descriptive teaching paths. For multi-sample inference, follow donor-aware pseudobulk, differential-abundance and checkpoint requirements from the formal workflow contract.
