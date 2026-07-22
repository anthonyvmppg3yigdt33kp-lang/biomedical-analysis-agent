# Source Syntax Audit Contract

## Purpose and boundary

`scripts/audit_source_syntax.py` performs a read-only syntax audit over one
materialized high-value review batch. It covers every `ordered_code_files` item
and every indexed article fenced block in each linked `SourceFlowBundle`. It
also resolves `external_targets` into the installed visualization Skill,
validates the Markdown file hash, extracts all fenced blocks, and audits them.

The audit never evaluates article code. R text is passed only to base R
`parse()`; Python text is passed only to `ast.parse()`. R files are never
`source()`d, Python modules are never imported, Shell-family text is never run,
and package installation is forbidden. A syntax pass therefore supports only
parse evidence. It does not establish dependency availability, runtime success,
data compatibility, statistical validity, figure fidelity, or scientific
correctness.

Raw syntax status and repairability are separate evidence. When a failed R or
Python item contains the extraction artifact U+00A0, the auditor may create an
in-memory `unicode-space-v1` candidate that replaces only U+00A0 with ASCII
space and parses that candidate a second time. The original bytes, hash, source,
and raw `parse_status` remain unchanged. A recovered candidate is not executable,
does not receive maturity promotion, and must be reviewed and stored separately
before any downstream use. Full-width punctuation, quotes, operators, comments,
line breaks, and other code changes are outside this automatic profile.

If a queued record has no ordered code files, no fenced code blocks, and no
external code target, the auditor emits one `no_code_inventory` evidence item
with `parse_status=not_applicable`. This closes record-level inventory coverage
without fabricating code or granting `parse-verified` maturity; the record can
continue only through method and figure-context review.

The fixed R parser is:

```text
C:\Program Files\R\R-4.5.3\bin\Rscript.exe
```

No fallback R executable is selected automatically.

## Inputs

- a materialized `high-value-*.jsonl` queue batch;
- `assets/private-corpus-index/source-flow-bundles.jsonl`;
- source locators and hashes already captured in each `SourceFlowBundle`;
- external target skill, root-relative Markdown locator, and hash captured in
  the preprocessing crosswalk.

All source directories are read-only. The tool writes only temporary UTF-8
parser copies and private audit evidence. It never rewrites source articles,
source scripts, bundles, queue rows, or external visualization snapshots.

## Hash gate

An ordered code file is parsed only when its observed byte SHA-256 equals the
hash declared by its bundle. Article fenced blocks are parsed only when both
the original article byte hash and the indexed block-text hash match. External
Markdown blocks are parsed only when the file hash equals the crosswalk hash.
Missing, unreadable, path-escaping, or hash-mismatched sources receive a failed
disposition and are not parsed.

## Evidence row

Each JSONL row represents one record/bundle/code item and includes:

- stable `audit_item_id`, `batch_id`, `queue_item_id`,
  `preprocess_record_id`, `record_sha256`, and optional `bundle_id`;
- `item_type`: `ordered_code_file`, `article_fenced_block`,
  `external_markdown_fenced_block`, `no_code_inventory`, or a synthetic
  inventory failure;
- item ordinal, source reference, optional source span, declared and normalized
  language;
- declared and observed item SHA-256 plus `hash_verified`;
- parser, parse status, error category, item-relative line and column, original
  source line when derivable, and a compact error summary;
- for eligible raw failures only, a `normalization_candidate` containing the
  profile, exact change counts, decoded-text and candidate hashes, candidate
  parse disposition, immutable-source flag, and explicit no-auto-promotion flag;
- an explicit scientific boundary statement.

Absolute source paths may appear only inside this private evidence layer. Do
not include these audit outputs in a public-core export.

## Parse dispositions

- `passed`: R or Python syntax parser accepted the complete item.
- `failed`: syntax error, missing/stale source, parser unavailability, parser
  timeout, parser invocation error, or evidence-integrity failure.
- `unsupported`: Shell-family or another language without an approved parser.
- `not_applicable`: the record contains no auditable code item; this is an
  inventory disposition, not a syntax pass.

Shell-family content must remain `unsupported`; it must not be changed to
`passed` by invoking a shell linter or interpreter.

Common error categories are `r_parse_error`, `python_syntax_error`,
`unsupported_shell`, `unsupported_language`, `source_missing`,
`source_hash_mismatch`, `article_source_hash_mismatch`,
`external_source_hash_mismatch`, `r_runtime_unavailable`, and
`r_parser_timeout`.

## Outputs and immutability

Default private outputs are:

```text
assets/private-corpus-index/manual-review/syntax-audits/
  <batch>-syntax-audit.jsonl
  <batch>-syntax-audit-summary.json
```

The command refuses to replace either output unless `--overwrite` is explicit.
JSONL writing is atomic. The summary records the batch and bundle-index hashes,
item/status/language/error counts, status breakdowns by language and item type,
aggregated syntax-error summaries, internal queue inventory coverage, external
target/block counts, fixed parser path, and maturity boundary.
It also records the number and parse dispositions of Unicode-space candidates;
these counts never replace the raw status counts.

Example:

```powershell
python scripts/audit_source_syntax.py `
  --batch assets/private-corpus-index/manual-review/queue-batches/high-value-001.jsonl
```

Changing the source bundle index or batch requires a new audit. A prior result
must not be treated as current when either recorded input hash differs.
