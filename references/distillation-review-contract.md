# Distillation review contract

Read this file before creating, resuming or adjudicating a high-value corpus review batch.

## Queue unit and state

Use the stable `preprocess_record_id` as the queue unit. A source record may link to one bundle, a project-root file set, a collection of bundles or a hash-identical external visualization snapshot. Do not replace record coverage with bundle occurrence counts.

Advance only through:

`QUEUED → SOURCE_VALIDATED → METHOD_REVIEWED → CODE_REVIEWED → FIGURE_CONTEXT_REVIEWED → SCIENTIFIC_QA → COMPLETE`

Write review events append-only. Before resuming, compare the queue manifest, preprocessing record, crosswalk relation, bundle, article, code and image hashes. Mark changed evidence `stale`; never silently reuse the prior decision.

## Required review evidence

For each record preserve:

- research question, input modality, cohort/sample structure and statistical unit;
- ordered method sequence and why the methods are combined;
- package/function usage, object production/consumption and full-flow gaps;
- code completeness, installers, paths, undefined objects and data requirements;
- each result figure's role, pixel hash, code binding evidence and claim boundary;
- assumptions, alternatives, negative controls and required validation;
- `visible`, `interpretable`, `confirmed` and `cannot_assert` statements;
- decision, maturity and an explicit reason for every blocked promotion.

Source identity or successful parsing does not authorize execution. A review may reach `normalized` or `parse-verified`; `parse-verified` requires every raw declared code item to pass the hash-gated syntax audit. Partial success or a passing normalization candidate remains `normalized`. `fixture-verified`, `data-verified` and `native-reviewed` require their separate execution and visual evidence contracts.

Store one review object per stable preprocessing record and validate it against
`references/schemas/distillation-review-record.schema.json`. A record-level review must
cover every linked bundle or hash-identical external target. For a bundle, retain the
article hash, flow fingerprint, the ordered hashes of every standalone code file and
article code block, and the ordered image hashes. Counts without the ordered hashes are
not sufficient evidence of full-inventory review.

`FIGURE_CONTEXT_REVIEWED` is not synonymous with `native-reviewed` maturity:

- `native_pixels` requires opening the exact local image with `view_image`, recording
  its SHA-256, and separating pixel-visible observations from contextual inference.
- `article_context_only` and `reference_only` may establish figure intent, but not
  pixel quality or exact code-to-figure reproduction.
- `confirmed` statements require direct code/data/result evidence. Put plausible but
  untested readings under `interpretable`; put prohibited extrapolations under
  `cannot_assert`.
- A markdown image reference is not generating-code evidence. Use
  `confirmed_generating_code` only when the object/export chain or an exact source
  binding supports it.

Completing all six review stages closes the human review task only. The decision field
must keep `automatic_execution_allowed=false`; execution eligibility is decided later
by WorkflowInstance compilation, environment locking and fixture/data validation.

## Deterministic batching

Generate the queue with:

```powershell
python scripts/build_distillation_review_queue.py build --batch-size 30
python scripts/build_distillation_review_queue.py validate
```

Pending items are interleaved across domains and ranked by mapping risk, code completeness, code inventory and figure inventory. Existing complete gold reviews remain in the registry but are never reassigned to a pending batch. Materialized batch files are immutable and receive a receipt containing the batch and queue-manifest hashes. Preparation and later revalidation bind to that receipt's historical queue-manifest hash, not to a subsequently rebuilt live manifest.

## Completed deep-review overlay

Queue rebuilds accept record-level completion only through
`manual-review/high-value-review-batch-*-validation.json`. A trusted receipt must
explicitly contain `ok=true`, `errors=[]`, and SHA-256 values that match every listed
review JSONL file. Each loaded `COMPLETE` record is rechecked against the current
preprocessing-record and source-relation hashes, must prohibit automatic execution,
and retains its materialized `batch_id` and `batch_position` as immutable history.

Trusted records become `COMPLETE_DEEP_REVIEW` with `action_required=false` and the
reviewed maturity. Their historical batch slots remain occupied; later rebuilds never
reuse or reorder those slots. Complete gold coverage remains unbatched. The queue
manifest records receipt hashes plus separate pending and completed batch counts.
