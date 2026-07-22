# Corpus distillation contract

Read this file when inventorying or retrieving the local WeChat-derived corpus.

## Non-negotiable rules

- Treat every configured source root as read-only. Write only to the selected index or run directory.
- Preserve every original code file and fenced code block in source order. Stage modules are derived views; they never replace the parent `SourceFlowBundle`.
- Hash bytes, relative path and size. Do not use modification time in content identity.
- Keep raw articles, code and images private. Do not copy them into a distributable package without a separate license review.
- Mark static extraction as `raw-extracted` or `normalized`. It is not executable evidence.
- Record missing files, undefined context, hard-coded paths, installers and downloads. Never repair raw code in place.
- Link images to code only when the article contains an explicit reference. Order proximity is a hypothesis and must be labeled `heuristic`.
- Use `materialize` only to copy a selected bundle into a task run directory after verifying every source hash.

## Generated private index

`corpus_distiller.py build` writes:

- `file-inventory.jsonl`: all files and byte hashes.
- `duplicate-groups.json`: exact byte-level duplicate groups.
- `preprocessing-records.jsonl`: every preprocessing row with a deterministic ID derived from source folder, inventory file, record title and normalized source path.
- `preprocessing-crosswalk.jsonl`: one row for every preprocessing record, including exact bundle, collection, project-root, external snapshot or unresolved relations.
- `preprocessing-crosswalk-report.json`: whole-corpus and high-value coverage counts; bundle occurrences are never substituted for unique record coverage.
- `source-flow-bundles.jsonl`: reconstructable article/project flows.
- `method-cards.jsonl`: metadata-assisted method candidates.
- `package-cards.jsonl`: package usage and package-centric source flows.
- `figure-cards.jsonl`: image/code/provenance candidates awaiting native review.
- `capability-modules.json`: same-purpose variants without deletion.
- `gold-set.json`: deterministic cross-domain review queue.
- `corpus-manifest.json` and `ingestion-report.md`: counts, gaps and maturity.

Every inventory row also records `source_mode=read_only`, private distribution, privacy class, license-review status, and `publish_allowed=false`.

## Stable preprocessing crosswalk

Treat the preprocessing record as the primary crosswalk unit. Its v2 ID uses `source_root_id + canonical root-relative locator`, with Unicode NFC, slash normalization and case folding. It excludes the drive letter and configured absolute root, preserves article numbers and aggregation descriptors, and rejects paths outside the source root. Never infer unique coverage from the truncated method fields stored on a bundle.

Prefer evidence in this order:

1. exact bundle directory or exact source-file membership;
2. an explicit collection-to-member or project-root descriptor relation;
3. an external reuse relation verified by identical source and snapshot SHA-256;
4. a labeled heuristic candidate;
5. explicit unmatched status.

Every preprocessing record must have exactly one crosswalk row even when it has zero or multiple relations. A verified crosswalk proves identity and provenance only. It does not verify code execution, scientific validity or the code-to-figure relationship.

After a raw build, run:

```powershell
python scripts/normalize_knowledge_registry.py --validate
```

This writes `normalized-registry/` without changing the raw records. The normalized layer must validate against the shared `SourceFlowBundle`, `MethodCard`, `PackageCard`, `FigureCard`, and `VariantSet` schemas. Articles with no code remain method candidates and are intentionally excluded from normalized SourceFlowBundles. Structural normalization does not raise maturity or authorize execution.

## Promotion boundary

Static extraction may identify packages, functions and likely object flow. Promote a candidate only after code review, a valid input contract, environment preflight, fixture execution, native image review and scientific interpretation. A missing source dataset remains blocked; do not substitute invented data.
