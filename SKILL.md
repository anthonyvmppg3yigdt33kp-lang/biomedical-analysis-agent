---
name: biomedical-analysis-agent
description: Orchestrate reproducible biomedical and clinical bioinformatics work from research question and local data through method selection, workflow compilation, isolated environment preparation, checkpointed execution, scientific and visual QA, interpretation, and auditable delivery. Use for plan, run, resume, reproduce-figure, or explain tasks spanning single-cell RNA-seq, spatial transcriptomics, bulk RNA-seq, quantitative proteomics, multi-omics, literature-methodology analysis, and publication visualization.
---

# Biomedical Analysis Agent

Use this Skill as the primary entry point. Delegate domain implementation to the selected workflow Skill, while retaining responsibility for statistical validity, environment isolation, artifact lineage, and claim boundaries.

## Route the request

1. Classify the mode as exactly one of `plan`, `run`, `resume`, `reproduce-figure`, or `explain`.
2. Record the research question, cohort or samples, endpoint, comparison, statistical unit, modality, input paths, desired outputs, and sensitivity constraints. Never upload sensitive data.
3. Run the deterministic router before browsing code modules:

   ```powershell
   python scripts/analysis_agent.py route --request request.json
   python scripts/analysis_agent.py compile --request request.json --output workflow.plan.json
   ```

   After changing routing terms or gates, run the fixed 180-case benchmark with
   `python scripts/evaluate_retrieval_benchmark.py --output benchmark-report.json`.
   Read [retrieval-benchmark-contract.md](references/retrieval-benchmark-contract.md)
   before adjudicating failures; never weaken the gold label to fit router output.

4. Read [workflow-contract.md](references/workflow-contract.md) before compiling or reviewing a run. Load only the relevant JSON Schema wrapper from `references/schemas/` when creating a knowledge object.
   Read [domain-support-matrix.md](references/domain-support-matrix.md) when the modality may be P1 or candidate-only; do not present corpus coverage as a formal executable workflow.
   Retrieve local distilled evidence before selecting a MethodCard, package flow, or figure variant:

   ```powershell
   python scripts/knowledge_retriever.py --request request.json --limit 5
   ```

   Read [knowledge-retrieval-contract.md](references/knowledge-retrieval-contract.md) before interpreting scores. Results are candidates, not approval: never execute `raw-extracted`, never emit article code into a Recipe, and never auto-substitute `compatible` or `alternative_method` variants.
5. Initialize the fixed run tree and immutable input manifest only after the request and project root are confirmed:

   ```powershell
   python scripts/run_manager.py init --request request.json
   python scripts/run_manager.py validate --run-root <compiled-run-root>
   ```

   `init` hashes declared read-only inputs, refuses to overwrite an existing run, writes JSON-compatible YAML plans, and creates an empty append-only artifact ledger. Use `transition` and `checkpoint` to enforce valid state changes and hash-verified atomic checkpoint promotion.
6. Dispatch domain work to the matching installed Skill:
   - single-cell: `bio-workflows-scrnaseq-pipeline`
   - spatial: `bio-workflows-spatial-pipeline`
   - bulk RNA: `bulk-rnaseq`
   - quantitative proteomics: `quantitative-proteomics-workflow`
   - multi-omics: `multi-omics-pipeline`
   - figures: `visualization-2026718-v1`
   - literature methods: use `MethodCard` and the methodology stage in this Skill. For a private
     explain-only teaching audit, register it separately with
     `scripts/register_p0_methodology_audit.py` and validate live evidence with
     `scripts/validate_p0_methodology_audits.py --verify-live`; never record a static audit as a
     data execution.

Treat router output as a reproducible shortlist, not as scientific approval. Resolve tied or scientifically non-equivalent choices explicitly.

## Reuse the teaching workflows

Use `examples/pbmc3k/` and `examples/visium-mouse-brain/` for the two
clone-portable v1 tutorials. Use `assets/teaching-cases/` only when a matching
legacy P0 candidate is appropriate; completeness differs by candidate and must
be checked against its README and public registry. No runner redistributes
binary inputs. Analysis scripts contain no installer commands; prepare and
freeze dependencies through the shared environment manager before invoking a
runner. A runner may be called complete only when its source hashes,
checkpoints, required artifacts, output validator and native visual-review
binding have all been verified.

Keep `references/p0-teaching-cases.json` immutable as the public design
registry. A local successful run belongs only in the private execution overlay;
it must not rewrite a public candidate from `not-executed` to a higher maturity.
In a public clone, validate only the distributable registry:

```powershell
python scripts/validate_p0_teaching_cases.py --no-local-locators
```

Maintainers may validate the two private layers independently only when their
local-only locator and execution overlays are actually present:

```powershell
python scripts/validate_p0_teaching_cases.py --verify-local
python scripts/validate_p0_teaching_case_executions.py --verify-live
```

The private literature-methodology teaching item is an explain-only negative
control, not a data execution. When its private overlay is present, validate it with
`python scripts/validate_p0_methodology_audits.py --verify-live`.

## Apply the five modes

- `plan`: profile inputs, compare methods, compile a workflow, and stop at `PLAN_COMPILED`. Do not install or execute.
- `run`: require explicit task-local execution authorization, resolve scientific gates, prepare frozen environments, and execute from the first stage.
- `resume`: verify the prior manifest, environment lock, input hashes, and last valid checkpoint; never infer completion from partial files.
- `reproduce-figure`: require a reference figure and its provenance; classify the result as exact, compatible, or alternative-method before running.
- `explain`: audit existing artifacts and distinguish direct observations, model-dependent interpretations, and unsupported claims. Do not rerun unless the user changes the mode.

For an incomplete `run`, `resume`, or `reproduce-figure` request, routing and gate compilation are still read-only and permitted. Compile the blocking plan and report `AWAITING_AUTHORIZATION`; do not initialize the run tree, provision an environment, or execute. Remain at `INTAKE` only when the research question itself is absent or cannot be safely represented.

## Enforce execution boundaries

Keep `AnalysisRecipe` dependency declarations separate from installation. Before any execution, read [environment-contract.md](references/environment-contract.md) and use the environment manager's `probe → resolve → provision → verify → freeze → execute → report_failure` interface.

- Install only after explicit `run`, `resume`, or `reproduce-figure` authorization.
- Use task-local, lock-hashed environments. Do not modify base environments, global libraries, system `PATH`, administrator settings, or quarantined environments.
- Freeze a verified environment. Create a new revision for new dependencies.
- Keep R and Python in sibling environments unless in-process interop is essential; exchange declared artifacts through explicit formats.
- Never silently change package, version, statistical method, or biological interpretation after compilation.

## Enforce scientific gates

Block execution when any applicable item is unresolved:

- cohort/sample identity, comparison, endpoint, or statistical unit;
- donor-aware replication for inferential single-cell or spatial analyses;
- platform and unit definition for spatial data;
- raw-count requirements for count models;
- patient/site/time-safe validation and within-resampling preprocessing for ML;
- multiple-testing control, confounding, missingness, or data leakage;
- required inputs, reference figure, prior checkpoint, or task-local authorization.

Report effect sizes with uncertainty where applicable. Keep exploratory and confirmatory claims separate; never convert association into causation.

## Execute and checkpoint

Follow this state machine exactly:

`INTAKE → DATA_PROFILED → PLAN_COMPILED → AWAITING_AUTHORIZATION → ENV_PREPARING → ENV_LOCKED → RUNNING_STAGE → STAGE_VALIDATING → CHECKPOINTED → ANALYSIS_QA → VISUALIZING → NATIVE_VISUAL_REVIEW → INTERPRETING → DELIVERED`

Write each stage to `_staging/<stage_id>`. Promote it only after its `ArtifactContract` validates. A non-zero exit or partial output is failure. Resume only from the latest validated checkpoint with matching inputs and locks.

For similar code, preserve every source implementation in a `VariantSet`: `exact` variants may be trialed automatically, `compatible` variants require recorded differences, and `alternative_method` variants require an explicit method decision.

## Deliver the run

Use the fixed run tree in [workflow-contract.md](references/workflow-contract.md). Deliver at minimum:

- `ANALYSIS_DESIGN.md` and frozen `workflow.plan.yaml`;
- executable scripts, parameters, environment locks, and sanitized logs;
- tables, serialized objects, intermediate checkpoints, and figures;
- `FIGURE_NOTES.md`, `QA_REPORT.md`, and `ARTIFACT_INDEX.md`;
- `run_manifest.json` and append-only `artifact_ledger.jsonl`.

For every figure state the question, data and statistical unit, directly visible result, supported interpretation, unsupported interpretation, assumptions, visual QA decision, reproduction class, and provenance.

## Keep private and distributable layers separate

Treat `assets/private-corpus-index/` and `references/corpus-sources.json` as local-only. They may contain article-derived code, image provenance, hashes, and private absolute paths. Never publish or upload them.

To create a reviewable core package, run `scripts/build_public_core.py` with an explicit output path and pass each local source-root literal through `--forbid`. The exporter substitutes `references/corpus-sources.example.json`, excludes the private index and caches, scans the staged text, and refuses to overwrite an existing archive. A successful export is still subject to a package-license and documentation review before publication.
