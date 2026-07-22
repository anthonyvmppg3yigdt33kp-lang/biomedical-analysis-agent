# P0 multi-omics teaching case: CLL MOFA-FLEX

This asset contains a complete, path-independent Python workflow for auditing a supplied 15-factor CLL MOFA-family model and probing the operational stability of a reduced six-factor MOFA-FLEX specification across two seeds. It includes no raw CLL data or pretrained model.

## Scientific contract

- Statistical unit: patient.
- Patient policy: retain the 200-patient union and preserve view-specific absence; do not replace it with the 121-patient complete-four-view intersection.
- Supplied views: Drugs 184 x 310, Methylation 196 x 4,248, Mutations 200 x 69, and mRNA 136 x 5,000.
- Reference audit: read factors, weights, variance explained, and training loss from the supplied 15-factor model.
- Reduced stability probe: six factors, seeds 42 and 43, outcome-free within-view variance selection, fixed feature caps, CPU, and one-to-one Hungarian matching that maximizes absolute Spearman score correlation.
- Factor signs, order, and labels are non-identifiable. Operational stability labels are workflow conventions, not inferential thresholds.
- The workflow is exploratory/descriptive and cannot establish causal programs, clinical subtypes, prognosis, treatment response, diagnostic performance, or patient-specific recommendations.

The supplied pretrained model was serialized with a different MOFA-FLEX development version than the frozen runtime. Core arrays were successfully audited, but this remains an explicit compatibility boundary; version-identical serialization or a full 15-factor refit is not claimed.

## Environment and inputs

The analysis script declares no installation commands. Provision the pinned Python environment through the shared `EnvironmentManager`, freeze it, and supply its `environment_plan.json`. The two binary inputs must match the SHA-256 values in `case-spec.yaml`. `source-spec.example.json` is distributable provenance metadata; review the underlying data license before redistributing the binary inputs.

```powershell
python run_case.py `
  --run-root <new-run-directory> `
  --data <cll.h5mu> `
  --pretrained-model <cll_model.h5> `
  --source-spec source-spec.example.json `
  --environment-plan <environment_plan.json>

python verify_outputs.py <new-run-directory>
```

The runner produces seven original figures and seven optimized candidates, registers hash-bound checkpoints, and stops at `NATIVE_VISUAL_REVIEW`. A human/agent original-pixel review must still approve the visual semantics and write “can explain / cannot explain” notes before delivery.

