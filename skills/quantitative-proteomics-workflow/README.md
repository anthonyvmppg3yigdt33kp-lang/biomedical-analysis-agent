# quantitative-proteomics-workflow skill

Reproducible quantitative proteomics workflow guidance with explicit peptide to
protein provenance, missingness assumptions, normalization, contrasts,
multiplicity, checkpoints, and QA.

Read `SKILL.md` before use. Bundled examples and validators are public guidance;
their maturity is stated by their own manifests and must not be inflated from a
static test.

Run the dependency-light validator smoke cases from this skill directory:

```powershell
python scripts/test_validate_proteomics_matrix.py
```
