# Workflow and delivery contract

## Contents

- [Knowledge objects](#knowledge-objects)
- [Compilation rules](#compilation-rules)
- [Run directory](#run-directory)
- [Scientific review](#scientific-review)

## Knowledge objects

Create knowledge objects against the wrapper schemas in `references/schemas/`. The shared definitions live in `knowledge-objects.schema.json`.

| Object | Purpose |
|---|---|
| `SourceFlowBundle` | Immutable article/code/image lineage and ordered full workflow |
| `MethodCard` | Question-to-method rationale, assumptions, alternatives, validation, limits |
| `PackageCard` | Versioned package/function workflow and installation provenance |
| `AnalysisRecipe` | One executable analysis unit; declarations only, no installer commands |
| `WorkflowTemplate` | Reusable DAG with declared optional or conditional branches |
| `WorkflowInstance` | Deterministic, frozen DAG compiled for one request and dataset |
| `FigureCard` | Figure semantics, code/image lineage, claims, and visual review |
| `ArtifactContract` | Producer/consumer, schema, unit, hash, validation, and claim role |
| `RunManifest` | Request, plan, environment, stages, artifacts, QA, and terminal status |
| `VariantSet` | Preserved implementations classified as exact, compatible, or alternative method |

Use maturity in order only: `raw-extracted`, `normalized`, `parse-verified`, `fixture-verified`, `data-verified`, `native-reviewed`. Never infer a higher level from code appearance.

## Compilation rules

- Normalize JSON keys and sort candidates and stages deterministically.
- Declare inference intent with the optional request fields "inferential" (JSON
  boolean) or "analysis_scope" ("auto", "descriptive-only", or "inferential").
  "inferential: true" or "analysis_scope: inferential" always enables the
  statistical-unit and multiple-testing gates. "inferential: false" or
  "analysis_scope: descriptive-only" suppresses gates caused only by generic
  words such as "infer", "association", or "effect"; it never suppresses
  explicit differential analysis ("differential", "DESeq2", "edgeR", "limma")
  or machine-learning gates. Conflicting declarations are invalid.
- Use "modality: visualization" to request the visualization component
  explicitly. Common singular and plural English forms such as "figure",
  "figures", "plot", and "plots" also route to that component.
- Freeze the compiled `WorkflowInstance`. A changed method, dependency, input, or contrast produces a new plan and environment revision.
- Keep `alternative_method` outside automatic fallback.
- Connect R and Python stages only through an `ArtifactContract` unless true in-process interop is declared.
- Stop execution on any blocking scientific gate. Preserve warnings and decisions in the manifest.
- Missing inputs or authorization do not prevent read-only gate compilation. Such a request stops at `AWAITING_AUTHORIZATION`; it does not create a run directory or environment.

## Run directory

```text
<project_root>/runs/<task_slug>/<run_id>/
  00_request/{intent.yaml,input_manifest.json}
  01_plan/{ANALYSIS_DESIGN.md,workflow.plan.yaml}
  02_environment/{environment_manifest.json,renv.lock,environment.yml,explicit.txt,install.log}
  03_scripts/{run_pipeline.R|run_pipeline.py,modules/,params.yaml}
  04_intermediate/<stage_id>/
  05_results/{tables/,objects/}
  06_figures/{original/,final/,review/}
  07_reports/{FIGURE_NOTES.md,QA_REPORT.md,ARTIFACT_INDEX.md}
  logs/
  manifest/{run_manifest.json,artifact_ledger.jsonl}
```

Write stage output to `_staging/<stage_id>` first. Promote only after validation and append the artifact ledger entry after promotion.

Use `scripts/run_manager.py init` to create the tree without overwriting an existing run. Use `transition` for state changes and `checkpoint` with an ArtifactContract JSON to promote a validated `_staging/<stage_id>` directory into `04_intermediate/<stage_id>`. The checkpoint operation verifies declared SHA-256 values before moving anything; a mismatch leaves staging intact.

Before compiling a `resume` WorkflowInstance, run `scripts/run_manager.py resume-audit --run-root <prior-run>`. It is read-only and requires unchanged input hashes, a verified frozen environment lock, and at least one hash-valid checkpoint. The environment manifest is not trusted by declaration: every environment must reference a copied `environment.locked.json` marker and at least one lockfile under `02_environment`, with recorded SHA-256 values; marker backend, platform and lock hash must agree with the manifest. Resume creates a new WorkflowInstance that references the reported checkpoint; it never mutates history or infers success from partial stage files.

## Scientific review

For inference, state the estimand, biological replicate, model, covariates, contrast, multiplicity procedure, effect size, uncertainty, and sensitivity analysis. For ML, place preprocessing and feature selection inside patient/site/time-safe resampling. For figures, review statistical semantics before aesthetics and record what the figure cannot establish.
