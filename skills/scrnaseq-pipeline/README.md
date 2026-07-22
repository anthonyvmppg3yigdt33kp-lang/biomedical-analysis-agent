# scrnaseq-pipeline skill

Platform-aware single-cell RNA-seq workflow design, execution, checkpointing,
and audit. The skill preserves raw counts, capture/sample/donor identity, and
separates descriptive cell-level analyses from donor-aware inference.

Read `SKILL.md` first. Validate the bundled structural fixtures with:

```powershell
python -m pytest -q tests
```

The general examples are parse/fixture guidance unless a run-specific manifest
explicitly records `data-verified` evidence.

