# v1.0.0 validation procedure

This tracked file defines the release gates; it records no claim that a gate has
passed. Commit-bound results live outside Git in a schema-checked evidence JSON.
After every gate succeeds, `scripts/build_release_assets.py` generates the
independent `RELEASE_VALIDATION.md`, `release-validation-summary.json`, and a
self-contained `biomedical-analysis-agent-1.0.0-evidence.zip`. This avoids the impossible cycle of editing a tracked validation record
to contain the hash of the commit that already contains it.

## Evidence baseline and fail-closed rule

Release state is not encoded in this tracked procedure. A checkout without a
separately generated, checksum-valid release-evidence set must be treated as
ineligible for tagging or publication.

Deprecated legacy Visium and PBMC outputs are excluded from release evidence.
The v1.0.0 Visium workflow freezes a complete Bioconductor 3.21 cohort and an
explicit `glmGamPoi_offset/v2` S40. The tracked expected output is derived from a
current-code real run with 2,695 reconciled spots, 30 finite PCs, zero structured
or external warnings, native exit 0, checkpoint/input/environment cache reuse,
two negative controls, and hash-bound native review. That local teaching snapshot
does not replace anonymous-clone proof, remote CI records, or commit-bound release
evidence.

## Required gates

| Gate | Required terminal evidence |
|---|---|
| Local Python/static, 180-case routing, and confusion regression | Schema-valid local report and SHA-256 |
| Local PBMC3K fresh run, resume, cache reuse, and canonical summary | Exact R/Seurat/renv lock plus positive and negative controls |
| Local Visium fresh run, resume, cache reuse, and canonical summary | Exact lock, code/config/environment-bound warning ledger with zero release blockers, six zero barcode-set differences, reports, figures, and ledger |
| Native review of every original/final figure pair | Hash-bound native-view registry with every pair opened |
| visualization strict validation and real Visium Recipe execution | Merged upstream commit, content hash, and strict report |
| Windows GitHub Actions `CI` | Saved GitHub API run response for the release commit |
| Windows GitHub Actions `Real-data release gate` | Saved GitHub API run response for the release commit |
| Anonymous clone, bootstrap, tests, both tutorials, and clean worktree | Schema-valid clone report and SHA-256 |
| License, private-locator, binary-input, and sensitive-data scan | Schema-valid distribution report and SHA-256 |

The distribution report also requires both public expected-output snapshots.
Missing or intentionally withheld snapshot manifests remain explicit release
blockers; placeholder files must not be added to make this gate pass.

Any gate not recorded as `passed`, or any missing/hash-invalid evidence file,
prohibits creating the tag or GitHub Release. A generated PNG without an actual
native pixel review does not satisfy the native-review gate.

## Evidence-based agent allocation and handoff

Release work may be partitioned into four independently bounded workstreams:
PBMC runtime, Visium runtime/root-cause analysis, release packaging, and primary
native review. Each handoff must record its assigned scope, files changed,
commands executed, observed exit codes or structured results, and unresolved
blockers. Runtime counts and timings belong in the commit-bound evidence JSON;
they must not be estimated or copied from an earlier source revision.

A subagent report is evidence to inspect, not an automatically accepted gate
result. The primary agent must independently rerun the root-level release
validators against the candidate commit, reconcile overlapping changes, and
confirm that every referenced artifact hash resolves. Native pixel review and
the final gate decision remain primary-agent responsibilities and cannot be
delegated by treating generated-image checks or a handoff summary as review.

## Commit-bound evidence contract

Every non-Actions gate uses schema-checked JSON evidence. The validator rejects
unexpected or duplicate evidence files and binds semantic fields in each file to
the release commit and to the corresponding gate details; recomputing a file
hash after changing a claim is therefore insufficient to pass validation.

| Gate | Required evidence payload |
|---|---|
| Local static | One `local-static-validation` payload with the exact five required checks set to `true` |
| Each tutorial | One `tutorial-release-details` payload plus one independent `tutorial-bundle-verification` payload for the same case and commit |
| Native review | One exact `native-review-registry` payload with five PBMC3K and three Visium original/final pairs; every pair has unique IDs, lowercase hashes, both files opened, `keep`, and no unresolved blocker/major finding |
| Upstream visualization | One `upstream-visualization-validation` payload binding the release commit, pinned upstream commit, content hash, strict real-Visium Recipe execution, warning-safe source object, local Visium warning ledger, and native review |
| Anonymous clone | One exact `anonymous-clone-validation` payload binding the public HTTPS remote, anonymous/no-credential clone, HEAD, six zero-return commands, two canonical and bundle-verification hashes, and raw empty porcelain |
| License/leak scan | One clean distribution report plus one `license-and-leak-scan-binding` payload binding its SHA-256 to the release commit |

The Visium tutorial bundle additionally retains the structured
`logs/pipeline-warnings.json`, the exact executed `run_pipeline.R`, and the
analysis configuration. Their hashes must agree with the CI summary, ledger,
environment lock, and release details. Raw inputs, RDS objects, caches,
checkpoints, task-local libraries, and unrestricted logs are excluded from the
public evidence bundle. These rules validate provenance and packaging only;
they do not convert a blocked or unexecuted tutorial into a passed release gate.

An upstream preview rendered from an object with unresolved preprocessing
warnings may test the plotting API and barcode/coordinate contract, but it
cannot satisfy the upstream release gate. After the warning-safe current-source
Visium run completes, the Recipe must be rerun against that object and its
original/final previews must receive native review. If the tracked upstream
evidence, previews, catalog, or review registry changes, merge a new upstream
commit, update both fields in `skills.lock.json`, and repeat bootstrap and
commit-bound release validation.

## Reproducible command set

```powershell
python -m pytest -q
python tutorial_cli.py plan --case pbmc3k
python tutorial_cli.py plan --case visium-mouse-brain
python scripts/evaluate_retrieval_benchmark.py --output validation/runtime/benchmark-180.json
python scripts/evaluate_retrieval_benchmark.py --benchmark references/router-confusion-regressions.jsonl --output validation/runtime/router-confusion.json
python scripts/validate_p0_teaching_cases.py --no-local-locators
python examples/pbmc3k/verify_expected_output.py
python examples/visium-mouse-brain/verify_expected_output.py
python scripts/validate_distribution.py --output validation/runtime/distribution.json
```

After `skills.lock.json` contains the merged visualization commit and content
hash, verify the task-local bootstrap:

```powershell
$skills = Join-Path $env:TEMP 'biomedical-analysis-agent-task-skills'
python bootstrap_skills.py --destination $skills
python bootstrap_skills.py --destination $skills --verify-only
```

Validate a completed evidence document before packaging:

```powershell
python scripts/validate_release_evidence.py `
  --evidence validation/runtime/release-evidence.json `
  --expected-version v1.0.0 `
  --expected-commit <FULL_COMMIT_SHA> `
  --repository-root .
```

Build and independently verify release assets only from a clean worktree at
that same commit:

```powershell
python scripts/build_release_assets.py `
  --version v1.0.0 `
  --expected-commit <FULL_COMMIT_SHA> `
  --evidence validation/runtime/release-evidence.json `
  --output-dir release-assets

python scripts/verify_release_assets.py `
  --archive release-assets/biomedical-analysis-agent-1.0.0.zip `
  --evidence-archive release-assets/biomedical-analysis-agent-1.0.0-evidence.zip `
  --checksums release-assets/SHA256SUMS.txt `
  --summary release-assets/release-validation-summary.json `
  --release-validation release-assets/RELEASE_VALIDATION.md `
  --version v1.0.0 `
  --commit <FULL_COMMIT_SHA>
```

## Interpretation boundary

The two tutorials are single-dataset or single-section descriptive examples.
Their successful execution cannot support donor-level, population-level,
mechanistic, or causal claims. The public P0 candidate registry remains
`not-executed`; release evidence is recorded separately and must not be copied
into that registry as a private overlay.
