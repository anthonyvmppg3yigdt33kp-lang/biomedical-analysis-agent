# Knowledge retrieval contract

Read this file before using distilled WeChat-derived knowledge to choose a method, package, workflow, or figure variant.

## Interface

Use either a UTF-8 request object or explicit CLI fields:

```powershell
python scripts/knowledge_retriever.py --request request.json --limit 5
python scripts/knowledge_retriever.py --query "donor-aware 单细胞差异分析" --domain single-cell --package Seurat --limit 5
python scripts/knowledge_retriever.py --query "空间域与邻域分析" --domain spatial --figure "组织切片空间叠加图"
```

The request object accepts `query`, `domain`, `package`, `figure_intent`, and `mode`. It also understands the orchestrator aliases `question`, `research_question`, `analysis_type`, `modality`, `preferred_package`, and `requested_outputs`, including fields nested under `intent`. At least one retrieval field is required. CLI fields override matching request-file fields. Output is deterministic JSON and contains no raw article code or private source path.

## Retrieval unit

The retriever first uses `assets/private-corpus-index/normalized-registry` when its manifest confirms Schema validation. Only when that layer is absent or invalid does it report a `raw-fallback`; it never silently mixes layers. It prefers a `SourceFlowBundle` anchor and joins linked `MethodCard`, `PackageCard`, `FigureCard`, and `CapabilityModule`/`VariantSet` evidence. A method, package, figure, or variant whose normalized source flow is intentionally absent remains retrievable as `knowledge_object_without_source_flow`; that candidate has no code, cannot be materialized from the normalized layer, and cannot execute. It ranks:

1. bilingual query, domain, package, and figure-intent agreement;
2. reconstructable full-flow candidates over fragments;
3. code and explicitly linked figure evidence;
4. verified maturity over static extraction;
5. fewer source defects.

Every score component is returned with an English machine key and Chinese label. A score is retrieval relevance, not scientific approval.

## Maturity and execution boundary

- `raw-extracted`, `normalized`, and `parse-verified` records are evidence candidates only.
- Only `fixture-verified`, `data-verified`, or `native-reviewed` evidence can pass the retriever's preliminary execution gate.
- A positive execution flag still requires request-specific scientific gates, explicit authorization, environment verification, and a frozen lock.
- `materialization.eligible` means the original bundle can be reconstructed into a private task directory after `corpus_distiller.py materialize` verifies every source hash. It does not mean the code is executable or distributable.
- Installation calls are never emitted. When detected, the candidate is blocked as a Recipe until installation code is removed and dependencies are declared for `EnvironmentManager`.

## Manual review overlay

When `assets/private-corpus-index/manual-review/gold-review-validation.json` is valid and every declared batch hash matches, the retriever overlays the reviewed bundle records without changing the normalized registry. Reviewed candidates receive a deterministic ranking boost. Gold-bundle reviews expose only the review decision, review maturity, claim ceiling, code-figure status, and unresolved flag. It never emits review evidence locators, raw article text, raw code, or source paths.

Completed high-value record reviews are also eligible for this overlay, but only when a successful `high-value-review-batch-*-validation.json` receipt authenticates every review-file hash and the review's current preprocessing-record and crosswalk-relation hashes still match. Multiple record reviews linked to one bundle are aggregated conservatively: the lowest review maturity and every claim ceiling/blocker remain visible. A deep review takes precedence over a coarser gold-bundle annotation for ranking explanation, while recording that the gold review also exists.

An authenticated deep review additionally contributes a bounded `derived_semantics` summary. The summary is whitelist-derived only from `research_context`, `combination_logic`, `method_sequence`, `package_usage`, `scientific_review`, and `figure_context_review`; every scalar is normalized, locator-redacted, and length-limited, and every list/object collection has a fixed count limit. Arbitrary review keys, evidence/provenance, article text, raw code, hashes, and absolute paths are excluded. These semantics may improve lexical/domain retrieval, and reviewed `package_names` participate in the existing package score (`exact=20`, `partial=10`). They add no separate score component, evidence count, maturity, execution eligibility, or verification claim. The public candidate may expose this safe summary so its match is auditable. If its receipt or source hashes are invalid, the entire overlay fails closed and none of these semantics are used.

`normalized` and `parse-verified` review maturity mean structural/manual or syntax evidence only. Neither implies fixture or data verification, so an overlaid candidate is always `execution.eligible=false`. A missing or invalid overlay is reported under `index.manual_review_overlay` and in warnings, then safely ignored.

## Exact-duplicate folding

Before applying the requested Top-K limit, the retriever folds copied implementations when either a valid flow fingerprint agrees or a multi-file ordered-code SHA-256 sequence and normalized title agree. Within an exact group, an authenticated review classified as `canonical` is preferred for display; otherwise a member not classified as `duplicate_reuse` is preferred. If neither distinction is available, the existing score-descending and candidate-ID order is the stable fallback. The selected member keeps its own score, maturity, and execution gate: the engine does not transfer those properties from a discarded duplicate or add a canonicality score. `duplicate_provenance` lists every source candidate ID and the number of reviewed sources. Duplicate sources add zero ranking points and never count as independent scientific, validation, or replication evidence. Single-file candidates without a shared flow fingerprint remain separate because an identical installer or helper line is not enough to prove an equivalent workflow.

## Variant boundary

- `exact`: the policy may permit automatic trial only after all execution gates pass.
- `compatible`: explain semantic or presentation differences before selection.
- `alternative_method`: require an explicit method decision; never auto-substitute.

Unknown or unreviewed equivalence remains blocked for semantic review.

## Safe degradation

If the private index or any component is absent, return valid JSON with `index.status` set to `unavailable` or `partial`, identify `index.layer`, list missing components, and make no claim about absent corpus content. Fall back to installed domain Skills and manually verified sources; do not fabricate a candidate.
