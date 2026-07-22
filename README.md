# biomedical-analysis-agent

Reproducible biomedical workflow orchestration for Codex. The repository bundles
the main `biomedical-analysis-agent` skill and five companion workflow skills for
single-cell RNA-seq, spatial transcriptomics, bulk RNA-seq, quantitative
proteomics, and multi-omics. Publication visualization is installed task-locally
from the exact commit recorded in `skills.lock.json`.

The candidate teaching cases are deliberately narrow and descriptive:

- `pbmc3k`: Seurat's official PBMC3K baseline workflow using the 10x public data;
- `visium-mouse-brain`: one 10x Mouse Brain Sagittal-Anterior Visium section,
  following the scientific scope of the Seurat spatial vignette.

Neither case supports population effects, donor-level inference, mechanisms, or
causal claims.

## Release lifecycle and evidence boundary

Release state is not encoded in this tracked file. Before a `v1.0.0` tag, treat
the commit as a candidate; after publication, verify the tag and the attached,
commit-bound `RELEASE_VALIDATION.md`, `release-validation-summary.json`,
`biomedical-analysis-agent-1.0.0-evidence.zip`, and `SHA256SUMS.txt`. The evidence
ZIP carries rewritten relative locators, all hash-bound evidence files, and a
stdlib-only verifier; the source archive alone never proves that release gates passed.

The legacy Visium S40 emitted 192 numerical warnings and is permanently excluded
from release evidence. The v1.0.0 workflow freezes one Bioconductor 3.21 closure
and explicitly uses `SCTransform(vst.flavor="v2", method="glmGamPoi_offset")`,
with exact backend/path, finite-value, structured-warning, external stdout/stderr,
and native-exit gates. Valid release evidence must cover a fresh full run,
resume/cache/negative controls, native review, expected output, anonymous clone,
and remote CI. Deprecated legacy snapshots cannot authorize publication; the
tracked expected-output directories are independently verifiable local teaching
artifacts, not substitutes for commit-bound remote or anonymous-clone evidence.

The pinned visualization commit validates the Seurat plotting and
barcode/coordinate contract and carries hash-bound original/final previews from
an independent warning-free Recipe execution on a trusted 2,695-spot object.
That evidence validates the Recipe only; the biomedical tutorial still requires
its own fresh-run, warning, report, native-review, and clone gates. The lock pins
the merged upstream commit and the complete sanitized installed-tree hash.

## Clone and bootstrap

```powershell
git clone https://github.com/anthonyvmppg3yigdt33kp-lang/biomedical-analysis-agent.git
Set-Location biomedical-analysis-agent
python bootstrap_skills.py --destination .\.task-skills
python bootstrap_skills.py --destination .\.task-skills --verify-only
```

The bootstrap command never writes to a global Codex skill directory. It stages
the main skill and each of the five companion skills with this repository's root
`LICENSE` and applicable `NOTICE`, then installs them below the explicit
destination. Installation and verify-only mode hash and validate those complete
staged trees.

The biomedical repository and its release archives do not bundle or redistribute
the optional upstream visualization tree. Bootstrap obtains that tree only into
the explicit task-local destination. Its lock records
`original_code_license: MIT`, `license_file: LICENSE`,
`third_party_notice_file: NOTICE.md`, and
`rights_status: mixed-original-and-third-party-not-relicensed`. Installation and
verify-only mode require both upstream files. Third-party or data-derived
materials identified by upstream `NOTICE.md` are not relicensed by this
repository's MIT license.

The locked `public-install-profile.json` (`biomedical-public-runtime-v1`) is
retained in the installed visualization skill. Its non-cone sparse checkout and
staging rules exclude exactly `assets/previews-curated`,
`assets/scheme-candidates`, `assets/source_archive`, and
`references/catalog.jsonl`. Consequently, unresolved-license article full text,
curated reference images, source-block-derived candidates, and the raw extracted
code/search catalog remain only in the upstream audit repository and are neither
downloaded nor installed by the biomedical public runtime profile. The retained
capability scope is the formal Recipe adaptation/composition/preflight/rendering
runtime plus native visual review; original recipes, ordinary references,
fixtures, and `assets/previews-rendered` remain available. Their third-party
attributions, including applicable 10x CC BY terms, remain governed by upstream
`NOTICE.md`.

During staging, the same profile applies the exact overlays
`SKILL.public-runtime.md -> SKILL.md` and
`manifest.public-runtime.yaml -> manifest.yaml`; the overlay source files remain
in the installation for auditability. Verify-only mode checks the byte-identical
targets and rejects any installed runtime manifest that refers to an excluded
path. Thus the public task-local Skill cannot advertise source-audit, source-
update, or reference-only candidate execution that its sanitized tree does not
contain.

## Unified tutorial interface

Planning is read-only and does not install packages or create a run tree:

```powershell
python tutorial_cli.py plan --case pbmc3k
python tutorial_cli.py plan --case visium-mouse-brain
```

Execution requires the explicit authorization flag and task-local caches. The
Visium input cache is external to the run tree and is directly read without
copying on fresh/resume; the environment cache is independently hash- and
lock-bound. The validated baseline is R 4.5.3 with Seurat 5.5.0; the CLI does
not silently fall back to another Seurat version.

```powershell
$rscript = 'C:\Program Files\R\R-4.5.3\bin\Rscript.exe'
python tutorial_cli.py run --case pbmc3k --authorize-run --rscript $rscript
python tutorial_cli.py run --case visium-mouse-brain --authorize-run --rscript $rscript
```

By default, the root CLI places the Visium inputs at
`.cache/tutorials/inputs/visium-mouse-brain`. Use `--cache-root`,
`--input-cache-root`, and `--run-root` to make all task-local locations explicit;
`--input-cache-root` must remain outside the fresh run root.

Use `resume` only after a run has at least one hash-valid checkpoint:

```powershell
python tutorial_cli.py resume --case pbmc3k --authorize-run --rscript $rscript
python tutorial_cli.py resume --case visium-mouse-brain --authorize-run --rscript $rscript
```

A successful computational run deliberately leaves each review JSON at
`awaiting_native_review`. Open every `06_figures/original` and paired
`06_figures/final` PNG in a native image viewer, record the opened hashes and
terminal decision in `06_figures/review`, and do not copy a `keep` decision from
any prior output without opening the current pixels. Then rebuild
the reports and run the terminal verifier:

```powershell
python tutorial_cli.py report --case pbmc3k
python tutorial_cli.py verify --case pbmc3k
python tutorial_cli.py report --case visium-mouse-brain
python tutorial_cli.py verify --case visium-mouse-brain
```

Every run uses the fixed tree documented in
`references/workflow-contract.md`, freezes its task-local environment, preserves
checkpoints, writes an append-only artifact ledger, and separates original from
visually optimized figures. Native visual review is a human/agent pixel review;
deterministic image checks alone are never reported as native review.

## Validation

```powershell
python -m pytest -q
python scripts/evaluate_retrieval_benchmark.py --output validation/runtime/benchmark-180.json
python scripts/evaluate_retrieval_benchmark.py `
  --benchmark references/router-confusion-regressions.jsonl `
  --output validation/runtime/router-confusion.json
python scripts/validate_p0_teaching_cases.py --no-local-locators
python examples/pbmc3k/verify_expected_output.py
python examples/visium-mouse-brain/verify_expected_output.py
```

Release gates additionally require both real-data tutorials, checkpoint resume,
cache reuse, checksum/non-zero-exit failure injection, native review of every
original/final image pair, Windows GitHub Actions, and a clean anonymous clone.
See `VALIDATION.md` for the fail-closed gate contract. A valid Release attaches
the commit-bound machine validation summary and checksums generated only after
those gates pass.

`CI` runs the Windows Python 3.13 static/unit/routing/distribution/bootstrap
checks and an R 4.5.3 syntax and binary-snapshot smoke test. The separate
`Real-data release gate` workflow requires an explicit commit SHA and execution
confirmation, downloads the two official datasets, and publishes computational
artifacts for review. It does not label generated figures as native-reviewed.

## Repository layout

```text
assets/                    legacy P0 teaching-case runner implementations
examples/                  PBMC3K and Visium end-to-end tutorials
references/                workflow, schemas, route gold sets, and public P0 registry
scripts/                   router, environment, checkpoint, and QA tooling
skills/                    five bundled domain workflow skills
tests/                     static and orchestration regression tests
bootstrap_skills.py        task-local skill bootstrap and lock verifier
tutorial_cli.py            plan/run/resume/verify/report interface
skills.lock.json           pinned external visualization dependency
```

## Licensing and data

This repository's original code and documentation are MIT licensed. The main
and five companion task-local installations carry the root `LICENSE` and
`NOTICE`. The optional upstream visualization dependency is not bundled; its
original code license and mixed-rights boundary are recorded separately in
`skills.lock.json`, and bootstrap installation does not relicense its
third-party or data-derived materials. Third-party datasets are not included or
covered by this repository's MIT license. The tutorial downloaders retrieve 10x
public data under CC BY 4.0 and verify the recorded input manifest. See `NOTICE`
and `THIRD_PARTY_DATA.md`.
