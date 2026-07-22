# Environment execution contract

## Purpose

Use `scripts/environment_manager.py` to keep dependency declaration, installation,
verification and analysis execution separate. `probe()` and `resolve()` are read-only.
Only an explicit approved `ExecutionAuthorization(mode="run")` permits task-local
provisioning, freezing or execution.

## Lifecycle

`discovered → planned → authorized → provisioning → provisioned → verified → frozen → runnable`

Failure states are `failed` and `quarantined`. A Device Guard, WDAC, AppLocker or group
policy signature immediately quarantines the candidate; never repair or reuse it silently.

## API

- `probe(candidate_executables=())`: report absolute Rscript, Python, uv/Conda/Git,
  PowerShell `R` alias collision and read-only health checks.
- `resolve(intent, recipe, probe_result=None)`: validate sources and compile stable
  R/Python environment specs. R and Python receive sibling environments.
- `provision(plan, authorization)`: install only in the plan's task-local cache.
- `verify(handle)`: test interpreter startup and declared package imports without installs.
- `freeze(handle, authorization)`: write backend lock evidence and
  `environment.locked.json` only after verification.
- `execute(handle, script, authorization, ...)`: run only through the frozen interpreter.
- `report_failure(...)`: return failure class, a bounded locator/credential-redacted error
  summary, a locator-free command outline plus its redacted fingerprint, retry safety,
  alternatives and the rule that
  scientific method changes require user choice.

## Recipe dependency declaration

```json
{
  "runtimes": ["r", "python"],
  "dependencies": [
    {"name": "DESeq2", "source": "bioconductor", "version": "1.50.0"},
    {"name": "scanpy", "source": "pypi", "version": "1.11.5"},
    {
      "name": "examplepkg",
      "source": "github",
      "repository": "owner/examplepkg",
      "ref": "0123456789abcdef0123456789abcdef01234567",
      "runtime": "python"
    }
  ]
}
```

For a reviewed R binary/source ABI compatibility constraint, the recipe may add
`"r_install_strategy": "pins-first"` and an exact-version package list such as
`"r_preinstall": ["Rcpp"]`. The ordering policy and package names are part of the
environment lock hash. pak installs those declared pins first with `upgrade=FALSE`,
then resolves the remaining declared packages against the same task-local library.
The manager rejects undeclared or unpinned preinstalls and does not expose this strategy
for Python or Conda environments. It changes installation order only; it cannot change
the scientific method, package identity, package version, or source.

For a Conda fallback from a non-Conda source, provide a reviewed `conda_name` mapping from
the package card. Pinned GitHub packages stay in a separate pak/uv environment; do not mix
them into a partially failed Conda environment.

GitHub repositories must be present in the manager allowlist and use a full 40-character
commit SHA. Branches, tags, arbitrary URLs and short SHAs are rejected. Package and method
substitution is never automatic.

## Isolation and locks

- Keep `cache_root` strictly below `task_root`; reject environment paths that escape it.
- Invoke R through the configured absolute `Rscript.exe`, never PowerShell `R`.
- Use process-local Rtools PATH only while executing R; do not modify system or user PATH.
- Default to `renv + pak` for R and `uv` for Python. Use a fresh Conda environment only
  when a recipe explicitly requires it.
- Cache by canonical dependency/runtime/platform lock hash. Reuse only a marker whose
  lock hash and platform match exactly and whose interpreter/package preflight succeeds.
- On Windows, `cache_key_chars` may shorten only the directory key (minimum 16 hexadecimal
  characters) to keep compiler include paths below the host limit. The plan and frozen
  marker always retain and compare the full 64-character lock hash, so a prefix collision
  cannot be accepted as a cache hit.
- Freeze verified environments. Adding dependencies requires a new hash/environment.
- Retry an identical command at most twice. Do not change versions, sources or methods
  during retry.

## Authorization boundary

Explicit `run` authorization permits allowlisted CRAN, Bioconductor, PyPI, conda-forge,
bioconda and pinned GitHub dependencies inside the task root. It never permits modifying
base/global environments, system libraries, administrator policy, global PATH, Conda init,
private credentials, arbitrary URLs, remote uploads or deletion/repair of existing
environments. Obtain separate user authority for any such action.

## Failure reporting

Classify failures as `DEVICE_GUARD`, `VERSION_CONFLICT`, `NETWORK`,
`OFFLINE_CACHE_MISS`, `COMPILER`, `PERMISSION`, `NATIVE_PROCESS_CRASH` or `UNKNOWN`.
Record Windows `0xC0000005` specifically as `NATIVE_PROCESS_CRASH` with diagnostic code
`ACCESS_VIOLATION_0xC0000005`; partially written package files do not convert that attempt
to success. An offline restore that lacks
an exact artifact is `OFFLINE_CACHE_MISS`, is not retry-safe for the same package/version/
source, and must move to a new reviewed environment plan rather than silently relaxing the
lock. Preserve task-local logs, a sanitized actionable error summary, stderr fingerprints,
exact attempts, whether retry is safe, and reviewed alternatives. State the scientific
impact of an alternative before asking the user to choose it.
