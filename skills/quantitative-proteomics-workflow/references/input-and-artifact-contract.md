# Proteomics input and artifact contract

## Contents

1. Sample metadata
2. Matrix and long-table schemas
3. MaxQuant rules
4. Artifact roles

## 1. Sample metadata

Require one row per biological sample/run combination with `sample_id`, `participant_id`, `condition`, and `run`; add `batch`, `pair`, `time`, `fraction`, `channel`, and exclusion fields when applicable.

Do not use fraction, peptide, PSM, technical injection, or TMT channel as an independent biological replicate. For multiplexed experiments, represent plex/channel structure in the model or package-specific converter.

## 2. Matrix and long-table schemas

Protein matrix contract:

- first column is a stable `protein_id` or protein-group ID;
- remaining quantitative columns map one-to-one to `sample_id`;
- values are numeric intensity or log-intensity with a declared scale;
- missing cells remain missing, not zero-filled;
- annotations remain separate or are named explicitly so they cannot be mistaken for samples.

MSstats-style long data minimally preserve `ProteinName`, peptide/feature identity, `Condition`, `BioReplicate`, `Run`, and `Intensity`, plus labeling/fraction fields required by the acquisition design and the locked MSstats/converter version.

## 3. MaxQuant rules

- Archive `proteinGroups.txt`, `evidence.txt`, `summary.txt`, `parameters.txt`, and the MaxQuant version when available.
- Filter rows marked `Reverse`, `Potential contaminant`, or `Only identified by site` using the actual symbols present in the source file. Report every removed category.
- Choose one quantification family—raw intensity, LFQ intensity, or iBAQ—based on the question. Do not merge families into one model.
- Convert zero placeholders to missing only when the export convention documents zero as “not quantified”; preserve a mask of the conversion.
- Keep protein-group membership and leading/majority protein IDs. Avoid pretending an ambiguous protein group is a uniquely identified protein.
- Record match-between-runs because it affects missingness and evidence interpretation.

## 4. Artifact roles

| Artifact | Required fields/metadata | Claim role |
|---|---|---|
| `source_manifest.tsv` | path, hash, format, search/quant software version | provenance |
| `sample_map.tsv` | sample, participant, run, condition, batch/pair/channel | design |
| `feature_map.tsv` | PSM/peptide/protein group links and aggregation rule | provenance |
| `missingness_mask.tsv` | feature x sample Boolean mask before imputation | QC |
| `normalized_matrix.tsv` | declared transform/normalization and source artifact | analysis/visualization |
| `protein_results.tsv` | effect, uncertainty/statistic, P, FDR, evidence, sensitivity | inference |
| `qc_metrics.tsv` | IDs, coverage, missingness, CV, correlations, model flags | QC |

Each artifact carries `artifact_id`, producer recipe/version, parent artifact IDs, ordered sample checksum, file hash, and maturity (`parse-verified`, `fixture-verified`, or `data-verified`).
