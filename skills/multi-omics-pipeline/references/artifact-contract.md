# Multi-omics artifact contract

## Contents

1. View input
2. Sample map
3. Integration outputs
4. Validation and claims

## 1. View input

For each processed view record:

| Field | Requirement |
|---|---|
| `artifact_id` | Stable unique ID used by the workflow graph. |
| `view` | RNA, protein, metabolite, methylation, or another explicit modality. |
| `path` / `sha256` | Immutable file path and content hash. |
| `orientation` | Normally samples x features for the public contract. |
| `scale` | Counts, log2 intensity, z-score, CLR, etc.; never “normalized” alone. |
| `feature_namespace` | Ensembl release, UniProt release, metabolite database/version, etc. |
| `sample_id_column` | Identifier joined through `sample_map.tsv`. |
| `producer` | Upstream recipe, parameters, package/version, and source artifact IDs. |
| `missingness` | Per-view and per-sample summary plus handling rule. |

## 2. Sample map

`sample_map.tsv` contains one row per biological specimen and columns for canonical sample ID, participant ID, site, batch, time, outcome availability, each view-specific ID, and inclusion status/reason. Store a checksum of the ordered canonical IDs used by every integration matrix.

## 3. Integration outputs

Use method-neutral files where possible:

- `integration_scores.tsv`: sample ID, component/factor, score, method, model ID.
- `integration_weights.tsv`: view, feature ID, component/factor, signed weight/loading, method, model ID.
- `selected_features.tsv`: view, feature, component, selection frequency, direction, method.
- `clusters.tsv`: sample ID, frozen cluster, method, stability metrics.
- `network_edges.tsv`: source, target, view/source labels, weight, threshold rule.
- serialized model plus environment lock, seeds, parameters, and exact input artifact IDs.

Do not force a method to invent an inapplicable file. Record the method-equivalent artifact and its schema in `run_manifest.json`.

## 4. Validation and claims

Every result artifact includes:

- development, tuning, validation, and test sample roles;
- stability/convergence diagnostics and technical-covariate associations;
- performance estimate with uncertainty where supervised;
- enrichment universe and database version;
- claim role: `qc`, `exploratory`, `supportive`, or `confirmatory`;
- `can_support` and `cannot_support` statements.

Reject a handoff when hashes, ordered sample checksums, scales, or identifier namespaces do not match the compiled workflow instance.
