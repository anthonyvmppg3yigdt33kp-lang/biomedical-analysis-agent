# Native figure review contract

Native figure review uses two linked registries because one pixel payload can appear in multiple articles or analytic contexts.

- `PixelArtifactReview` is keyed by the image SHA-256. It records whether the actual pixel payload was opened with a native image viewer, what was visibly inspected, and any file-level limitations. Duplicate FigureCards with the same SHA-256 share one pixel review.
- `FigureContextReview` is keyed by FigureCard identity. It links the pixel review to the source bundle, article position, candidate generating code, statistical semantics, claim boundary, and reproduction applicability.

The generated queues are private local evidence. They must not be published because context records are derived from private source paths and article material.

## Status rules

`pending` means the required review has not occurred. `not_applicable` means the review question is inapplicable and requires a reason. These states are never interchangeable.

A Markdown image link is a source-position reference, not generating-code evidence. It is stored as `markdown_image_reference`; code binding remains `pending` until an explicit code relationship is reviewed. If a SourceFlowBundle has no extracted code candidate, code binding and code-based reproduction are `not_applicable`, with the absence recorded as the reason.

Legacy Gold60 declarations are copied only into `legacy_declaration` with `trust_level: declaration_only`. They do not promote `native_review_status`, `code_binding_status`, semantic review, or maturity in the new registries. A new `native_reviewed` state requires a named reviewer, review time, at least one native-view evidence item, visible findings, and explicit unsupported claims.

## Deterministic build and validation

Run:

```powershell
python scripts/build_native_figure_review_registry.py build
python scripts/build_native_figure_review_registry.py validate
```

The builder reads existing FigureCards and SourceFlowBundles without changing them. It emits one pixel queue row per unique SHA-256, one context queue row per FigureCard, a manifest, and a fixed first batch. The first batch remains `pending`; selecting an item for review is not evidence that it was viewed.

Validation reconstructs both registries from FigureCards, checks exact hash groups, deterministic IDs and links, verifies the fixed batch order and roles, and refuses any first-batch row marked as newly reviewed without a separate review workflow.

## Claim boundary

Pixel review can establish what is visible and whether the raster is legible. Context review can establish code proximity, statistical mapping, and defensible interpretation. Neither proves that the source code executes, that underlying numerical results are correct, or that the source study supports causal or clinical claims.
