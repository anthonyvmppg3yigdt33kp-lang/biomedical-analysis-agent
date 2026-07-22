# bulk-rnaseq skill

Checkpointed bulk RNA-seq workflow guidance from immutable inputs through QC,
count-aware modeling, multiplicity control, figures, and interpretation.

Read `SKILL.md` before use. This compact skill does not carry a standalone
`tests/` directory; validate it as part of the repository suite from the
repository root:

```powershell
python -m pytest -q
```

Count models require untransformed integer-like counts and a valid biological
replicate design; normalized expression is not silently substituted.
