# Changelog

All notable changes to this project are documented here.

Publication state and date are authoritative only in the immutable Git tag and
release metadata; this tracked changelog contains no mutable publication claim.

## [1.0.0]

### Added

- Clone-portable biomedical orchestration skill with five bundled P0 workflow
  skills and a commit-pinned visualization dependency.
- Unified `tutorial_cli.py` plan/run/resume/verify/report interface.
- Seurat 5.5.0 PBMC3K and Visium Mouse Brain teaching-workflow candidates.
- Task-local bootstrap, environment locks, checkpoint/resume, artifact ledger,
  native visual-review trail, license/data notices, Windows CI, and release gates.

### Fixed

- Spatial requests containing shared terms such as `10x`, `Seurat`, or
  `CellChat` no longer create incidental single-cell workflow nodes.
- Teaching documentation now distinguishes public `not-executed` candidates
  from real data-verified executions and places hash-bound native review before
  terminal report/verification.
- Distribution validation rejects private-home locators (including escaped
  JSON/Python forms), high-confidence credentials, private keys, symlinks,
  oversized payloads, absolute paths in committed expected outputs, and a
  missing public expected-output snapshot for either tutorial.
- Release evidence now binds static, tutorial, native-review, upstream,
  anonymous-clone, and distribution claims to exact hash-verified evidence;
  Visium warning evidence is independently code/config/environment/ledger
  bound and fails closed on numerical, API, spatial-integrity, or unknown
  warnings.
- The visualization lock and bootstrap now require the exact reviewed HTTPS
  repository, full commit, skill subdirectory, content hash, original-code MIT
  license, third-party notice, and mixed-rights status. The upstream tree is not
  bundled or relicensed. Its locked public-runtime profile excludes upstream
  audit-only article/source archives, curated reference previews,
  source-block-derived candidates, and the raw extracted code/search catalog
  while retaining the profile and formal Recipe runtime assets; upstream release
  evidence must use the warning-safe Visium object rather than an older
  contract-only preview source.
