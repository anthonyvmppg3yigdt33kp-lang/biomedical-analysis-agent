# Environment lifecycle fixture contract

Use `scripts/run_environment_fixture.py` to exercise the real task-local Python
environment lifecycle without downloading a dependency. The caller must explicitly
choose a disposable task root and authorize `mode=run`; the harness encodes an
authorization that prohibits network installation and all package sources.

The fixture must prove:

1. `probe` and `resolve` discover the runtime and produce a stable lock hash;
2. `provision`, `verify`, and `freeze` create only the task-local `uv` environment;
3. a deterministic script executes only through the frozen interpreter;
4. a new `EnvironmentManager` restores the same lock-cached environment;
5. fresh and restored outputs have identical SHA-256 values.

This is an environment-control fixture, not a package-installation, statistical, or
biomedical validation. A P0 teaching case still needs its declared dependencies,
fixture/data QA, checkpoints and claim-boundary review.

Use `scripts/run_resume_fixture.py` after the environment fixture to exercise a real
staging-to-checkpoint promotion and read-only resume audit. The resume fixture copies
the marker and lockfile into `02_environment`, verifies their hashes, deliberately
alters the copied lockfile to prove rejection, restores it, and confirms the prior run
is resumable only through a new WorkflowInstance.

## Real R install and exact-lock cache fixture

Use `scripts/run_r_environment_fixture.py` only with explicit task-local `run`
authorization and a new disposable task root. It fixes the interpreter to
`C:\Program Files\R\R-4.5.3\bin\Rscript.exe`, installs pinned CRAN and lightweight
Bioconductor packages through `renv + pak` into the platform-specific project library,
freezes `renv.lock`, and executes a deterministic R script only after `renv::load()`.

The fixture must prove all of the following:

1. the installed package locations are children of the lock-hashed environment path;
2. `BiocVersion` and a functional Bioconductor package match the declared release;
3. the `renv.lock` SHA-256 stored in `environment.locked.json` is unchanged;
4. a new `EnvironmentManager` reuses the exact lock without another install command;
5. fresh and restored executions have identical output SHA-256 values;
6. process `PATH`, global R libraries, Conda base, and administrator settings are not
   modified.

Preserve `install.log`, `renv.lock`, `environment_manifest.json`, the deterministic
outputs, and a structured failure class. A failed Bioconductor install is evidence of a
blocked branch, not permission to substitute a CRAN package or claim Bioconductor
verification.
