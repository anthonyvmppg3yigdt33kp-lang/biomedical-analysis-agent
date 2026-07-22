# Gold review validation contract

The 60-item gold queue is a manual calibration layer over the immutable raw corpus. A review record summarizes method logic and evidence; it does not copy source code and does not make a workflow executable.

Each record must include the selected bundle identity, research question, data/statistical unit, method sequence, combination logic, package usage, code completeness, code–figure consistency, assumptions, scientific risks, alternatives, required validation, claim ceiling, decision, maturity, and non-empty evidence.

Allowed record maturity is `normalized` or `parse-verified`. `parse-verified` requires all declared code files to pass a real parser; partial parser success remains `normalized`. Native viewing is recorded at figure-evidence level only and never promotes the full workflow to `native-reviewed`.

`scripts/validate_gold_reviews.py` verifies:

- exact coverage and order of `gold-set.json` without duplicate bundle IDs;
- title/domain identity and article/flow hashes against SourceFlowBundles;
- reviewed code inventory counts and, where provided, boundary code hashes;
- representative FigureCard identity, bundle relation, and image hash;
- maturity and parse-evidence consistency;
- explicit unresolved code–figure relationships rather than guessed mappings.

A passing gold review remains a candidate for Recipe reconstruction, fixture execution, data validation, and scientific review. It is not permission to publish source material or to run an analysis.

The validation metric is `representative_figures_declared_reviewed`. The former
`representative_figures_native_viewed` key remains only as a deprecated,
value-compatible alias for older consumers. It must not be used as evidence that
all figures had a named, timestamped native-view event; that stronger evidence is
tracked by `PixelArtifactReview` and `FigureContextReview`.
