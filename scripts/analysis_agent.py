#!/usr/bin/env python3
"""Deterministic request router and workflow-plan compiler.

This module never installs packages or executes analyses. It produces a frozen,
auditable plan for the environment manager and domain workflow skills.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path, PureWindowsPath
from typing import Any, Iterable


MODES = ("plan", "run", "resume", "reproduce-figure", "explain")
ANALYSIS_SCOPES = ("auto", "descriptive-only", "inferential")
STATE_MACHINE = (
    "INTAKE",
    "DATA_PROFILED",
    "PLAN_COMPILED",
    "AWAITING_AUTHORIZATION",
    "ENV_PREPARING",
    "ENV_LOCKED",
    "RUNNING_STAGE",
    "STAGE_VALIDATING",
    "CHECKPOINTED",
    "ANALYSIS_QA",
    "VISUALIZING",
    "NATIVE_VISUAL_REVIEW",
    "INTERPRETING",
    "DELIVERED",
)

ROUTES: tuple[dict[str, Any], ...] = (
    {
        "id": "literature-methodology",
        "skill": "biomedical-analysis-agent",
        "keywords": (
            "literature", "paper", "methodology", "method selection", "文献",
            "方法学", "方法选择", "研究设计", "套路", "原理", "methods section",
            "study design", "method card", "package card", "方法组合", "结论边界",
            "对象依赖", "统计语义", "输入对象", "结果图映射", "可复现原因",
        ),
    },
    {
        "id": "single-cell",
        "skill": "bio-workflows-scrnaseq-pipeline",
        "keywords": (
            "single-cell", "single cell", "scrna", "scrna-seq", "seurat", "scanpy", "pbmc",
            "10x", "cell ranger", "cellranger", "doubletfinder", "scrublet", "soupx",
            "harmony", "pseudobulk", "donor-aware", "infercnv", "scvi", "scvi-tools",
            "cell-cycle", "sct anchors", "cluster marker", "monocle", "slingshot",
            "cellchat", "单细胞", "双细胞", "环境rna", "细胞组成", "细胞亚群",
            "稀有细胞", "细胞通讯", "拟时序",
        ),
    },
    {
        "id": "spatial-transcriptomics",
        "skill": "bio-workflows-spatial-pipeline",
        "keywords": (
            "spatial", "visium", "stereo-seq", "stereoseq", "giotto", "spata2",
            "xenium", "cosmx", "merfish", "slide-seq", "bayesspace", "cell2location",
            "tangram", "stereoscope", "spacexr", "rctd", "squidpy", "moran's i",
            "空间转录组", "空转", "空间域", "解卷积", "空间邻域", "空间表达",
            "空间细胞通讯", "空间细胞类型", "空间自相关", "空间梯度", "组织图像",
            "spot", "bead-level", "field-of-view", "患者切片", "共定位",
        ),
    },
    {
        "id": "bulk-rna",
        "skill": "bulk-rnaseq",
        "keywords": (
            "bulk rna", "bulk-rna", "rnaseq", "rna-seq", "deseq2", "edger",
            "limma", "voom", "salmon", "tximport", "featurecounts", "fastq", "star",
            "gsva", "wgcna", "bulk转录组", "常规转录组", "转录组测序",
        ),
    },
    {
        "id": "quantitative-proteomics",
        "skill": "quantitative-proteomics-workflow",
        "keywords": (
            "proteomics", "protein", "maxquant", "lfq", "qfeatures", "dep",
            "msstats", "dia-nn", "tmt", "psm", "peptide", "proteingroups",
            "蛋白组", "蛋白质组", "定量蛋白", "质谱", "肽段", "前体离子",
        ),
    },
    {
        "id": "multi-omics",
        "skill": "multi-omics-pipeline",
        "keywords": (
            "multi-omics", "multiomics", "mofa", "mofa2", "diablo", "mixomics", "snf",
            "mcia", "transcriptome and proteome", "rna and protein", "joint integration",
            "integrative omics", "omics integration", "multi-omics factor", "多组学",
            "跨组学", "组学整合", "联合整合", "联合组学", "不同组学层",
            "多个组学", "多个组学块", "多组学样本", "组学样本量", "rna、蛋白",
            "rna、甲基化", "rna, methylation", "transcriptome and methylation",
            "转录组基因、蛋白", "pathway-level", "multiomics 分析",
        ),
    },
    {
        "id": "visualization",
        "skill": "visualization-2026718-v1",
        "keywords": (
            "visualization", "figure", "figures", "plot", "plots", "heatmap", "heatmaps",
            "volcano", "volcano plots", "umap", "umaps",
            "ggplot", "matplotlib", "forest plot", "kaplan-meier", "survival curve",
            "forest plots", "boxplot", "boxplots", "violin", "violin plots",
            "dot plot", "dot plots", "ridge plot", "ridge plots", "sankey", "alluvial",
            "chord diagram", "circos", "pca scatter", "ma plot", "upset", "venn",
            "raincloud", "beeswarm", "bar chart", "line chart", "line plots",
            "scatter plots", "manhattan", "roc curve",
            "calibration curve", "decision curve", "panel layout", "visual qa",
            "可视化", "绘图", "出图", "结果图", "热图", "火山图", "森林图",
            "生存曲线", "箱线图", "小提琴图", "桑基图", "柱状图", "折线图",
            "配色", "排版", "美化", "复刻", "色觉可辨", "文字重叠", "分辨率",
        ),
    },
)

# ``10x``, Seurat, Scanpy and CellChat are used across single-cell and spatial
# workflows.  They remain useful evidence, but they must not create a
# single-cell stage by themselves when the request already identifies a
# spatial assay.  The sets below deliberately separate platform evidence from
# decisive single-cell evidence so this policy stays auditable.
SPATIAL_MODALITY_EVIDENCE = {
    "spatial",
    "visium",
    "stereo-seq",
    "stereoseq",
    "xenium",
    "cosmx",
    "merfish",
    "slide-seq",
    "spot",
    "bead-level",
    "field-of-view",
}

DECISIVE_SINGLE_CELL_EVIDENCE = {
    "single-cell",
    "single cell",
    "scrna",
    "scrna-seq",
    "pbmc",
    "cell ranger",
    "cellranger",
    "doubletfinder",
    "scrublet",
    "soupx",
    "infercnv",
    "scvi",
    "scvi-tools",
    "sct anchors",
}

SINGLE_CELL_REFERENCE_PHRASES = (
    "single-cell reference",
    "single cell reference",
    "scrna reference",
    "scrna-seq reference",
    "single-cell mapping",
    "single cell mapping",
    "joint mapping",
    "reference mapping",
)

MODALITY_ALIASES = {
    "scrna": "single-cell",
    "single-cell": "single-cell",
    "single_cell": "single-cell",
    "单细胞": "single-cell",
    "spatial": "spatial-transcriptomics",
    "spatial-transcriptomics": "spatial-transcriptomics",
    "空间转录组": "spatial-transcriptomics",
    "空转": "spatial-transcriptomics",
    "bulk": "bulk-rna",
    "bulk-rna": "bulk-rna",
    "bulk_rna": "bulk-rna",
    "bulk-rnaseq": "bulk-rna",
    "蛋白组": "quantitative-proteomics",
    "proteomics": "quantitative-proteomics",
    "quantitative-proteomics": "quantitative-proteomics",
    "multi-omics": "multi-omics",
    "multiomics": "multi-omics",
    "visualization": "visualization",
    "多组学": "multi-omics",
}

RUN_TREE = (
    "00_request",
    "01_plan",
    "02_environment",
    "03_scripts/modules",
    "04_intermediate",
    "05_results/tables",
    "05_results/objects",
    "06_figures/original",
    "06_figures/final",
    "06_figures/review",
    "07_reports",
    "logs",
    "manifest",
)


class RequestError(ValueError):
    """Raised when a request cannot be deterministically compiled."""


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _text(request: dict[str, Any]) -> str:
    fields = (
        request.get("question"), request.get("analysis_type"), request.get("modality"),
        request.get("platform"), request.get("spatial_unit"), request.get("requested_outputs"),
        request.get("method"), request.get("notes"),
    )
    return " ".join(str(item) for field in fields for item in _as_list(field)).casefold()


def _keyword_in_text(keyword: str, haystack: str) -> bool:
    """Match ASCII terms on token boundaries and CJK/mixed terms literally.

    Boundary matching prevents a broad route such as ``rna-seq`` from firing
    inside the more specific token ``scRNA-seq`` while preserving literal CJK
    phrase matching and punctuation-rich package names.
    """
    folded = keyword.casefold()
    if re.fullmatch(r"[a-z0-9][a-z0-9 ._+/'-]*[a-z0-9]", folded):
        return re.search(rf"(?<![a-z0-9]){re.escape(folded)}(?![a-z0-9])", haystack) is not None
    return folded in haystack


def _slug(value: str) -> str:
    value = value.strip().casefold()
    value = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", value).strip("-")
    return (value[:64] or "biomedical-analysis")


def _join_run_root(project_root: str, task_slug: str, run_id: str) -> str:
    if re.match(r"^[A-Za-z]:[\\/]", project_root):
        return str(PureWindowsPath(project_root) / "runs" / task_slug / run_id)
    return str(Path(project_root) / "runs" / task_slug / run_id)


def validate_request(request: dict[str, Any]) -> None:
    if not isinstance(request, dict):
        raise RequestError("request must be a JSON object")
    mode = request.get("mode", "plan")
    if mode not in MODES:
        raise RequestError(f"mode must be one of: {', '.join(MODES)}")
    if not str(request.get("question", "")).strip():
        raise RequestError("question is required")
    if request.get("execution_authorized") and mode not in {"run", "resume", "reproduce-figure"}:
        raise RequestError("execution_authorized is invalid for plan or explain mode")
    if request.get("execution_authorized") and request.get("authorization_scope") != "task-local":
        raise RequestError("execution authorization must use authorization_scope='task-local'")
    if "inferential" in request and not isinstance(request["inferential"], bool):
        raise RequestError("inferential must be a JSON boolean")
    scope = str(request.get("analysis_scope", "")).strip().casefold().replace("_", "-")
    if scope and scope not in ANALYSIS_SCOPES:
        raise RequestError(f"analysis_scope must be one of: {', '.join(ANALYSIS_SCOPES)}")
    if request.get("inferential") is True and scope == "descriptive-only":
        raise RequestError("inferential=true conflicts with analysis_scope='descriptive-only'")
    if request.get("inferential") is False and scope == "inferential":
        raise RequestError("inferential=false conflicts with analysis_scope='inferential'")


def route_request(request: dict[str, Any]) -> list[dict[str, Any]]:
    """Return a stable, evidence-bearing route shortlist."""
    validate_request(request)
    haystack = _text(request)
    explicit_modalities = {
        MODALITY_ALIASES.get(str(item).casefold(), str(item).casefold())
        for item in _as_list(request.get("modality"))
    }
    scored: list[dict[str, Any]] = []
    for route in ROUTES:
        matched = sorted({keyword for keyword in route["keywords"] if _keyword_in_text(keyword, haystack)})
        explicit = route["id"] in explicit_modalities
        score = (100 if explicit else 0) + 10 * len(matched)
        if score:
            scored.append(
                {
                    "capability": route["id"],
                    "skill": route["skill"],
                    "score": score,
                    "evidence": (["explicit-modality"] if explicit else []) + matched,
                }
            )

    by_capability = {item["capability"]: item for item in scored}
    spatial_route = by_capability.get("spatial-transcriptomics")
    single_route = by_capability.get("single-cell")
    if spatial_route is not None and single_route is not None:
        spatial_is_explicit = "spatial-transcriptomics" in explicit_modalities
        single_is_explicit = "single-cell" in explicit_modalities
        spatial_terms = set(spatial_route["evidence"]) & SPATIAL_MODALITY_EVIDENCE
        single_terms = set(single_route["evidence"]) & DECISIVE_SINGLE_CELL_EVIDENCE
        reference_mapping = any(phrase in haystack for phrase in SINGLE_CELL_REFERENCE_PHRASES)
        if (spatial_is_explicit or spatial_terms) and not (
            single_is_explicit or single_terms or reference_mapping
        ):
            scored = [item for item in scored if item["capability"] != "single-cell"]
    if not scored:
        scored.append(
            {
                "capability": "literature-methodology",
                "skill": "biomedical-analysis-agent",
                "score": 1,
                "evidence": ["safe-default-method-review"],
            }
        )
    return sorted(scored, key=lambda item: (-item["score"], item["capability"]))


def _declared_inferential_scope(request: dict[str, Any]) -> bool | None:
    """Return the explicit inference contract, or None for auto-detection."""
    scope = str(request.get("analysis_scope", "")).strip().casefold().replace("_", "-")
    if request.get("inferential") is True or scope == "inferential":
        return True
    if request.get("inferential") is False or scope == "descriptive-only":
        return False
    return None


def _intent_set(request: dict[str, Any]) -> set[str]:
    text = _text(request)
    intents = set()
    mapping = {
        "differential": ("differential", "差异", "contrast", "deseq2", "edger", "limma"),
        "inference": (
            "infer", "inference", "inferential", "association", "associations",
            "effect", "effects", "推断", "关联", "效应", "差异",
        ),
        "machine-learning": ("machine learning", "prediction", "classifier", "机器学习", "预测", "分类器"),
        "count-model": ("deseq2", "edger", "negative binomial", "count model", "负二项"),
    }
    for intent, terms in mapping.items():
        if any(_keyword_in_text(term, text) for term in terms):
            intents.add(intent)
    return intents


def _gate(gate_id: str, passed: bool, message: str, mode: str, *, always_block: bool = False) -> dict[str, str]:
    if passed:
        status = "pass"
    elif always_block or mode in {"run", "resume", "reproduce-figure"}:
        status = "block"
    else:
        status = "deferred"
    return {"id": gate_id, "status": status, "message": message}


def scientific_gates(request: dict[str, Any], routes: Iterable[dict[str, Any]]) -> list[dict[str, str]]:
    """Evaluate pre-execution scientific and authorization gates."""
    mode = request.get("mode", "plan")
    capabilities = {route["capability"] for route in routes}
    intents = _intent_set(request)
    gates: list[dict[str, str]] = []

    if request.get("remote_upload"):
        gates.append(_gate("sensitive-data-boundary", False, "Remote upload is outside this agent's authority.", mode, always_block=True))
    else:
        gates.append(_gate("sensitive-data-boundary", True, "No remote upload requested.", mode))

    if mode in {"run", "resume", "reproduce-figure"}:
        gates.append(_gate("input-manifest", bool(_as_list(request.get("inputs"))), "Declare local input paths before execution.", mode))
        authorized = request.get("execution_authorized") is True and request.get("authorization_scope") == "task-local"
        gates.append(_gate("execution-authorization", authorized, "Explicit task-local execution authorization is required.", mode))

    declared_inferential = _declared_inferential_scope(request)
    mandatory_inferential = bool(intents & {"differential", "machine-learning"})
    inferred_from_text = "inference" in intents and declared_inferential is not False
    inferential = declared_inferential is True or mandatory_inferential or inferred_from_text
    if inferential:
        gates.append(_gate("statistical-unit", bool(request.get("statistical_unit")), "Define the independent statistical unit.", mode))
        gates.append(_gate("multiple-testing", bool(request.get("multiplicity_method")), "Declare the multiplicity-control method.", mode))

    if "differential" in intents:
        has_contrast = bool(request.get("contrast") or request.get("group_column"))
        gates.append(_gate("contrast", has_contrast, "Define the comparison or group column.", mode))

    if inferential and capabilities & {"single-cell", "spatial-transcriptomics"}:
        gates.append(_gate("biological-replicate", bool(request.get("sample_id_column") or request.get("donor_id_column")), "Use donor/sample-aware biological replication for inference.", mode))

    if "spatial-transcriptomics" in capabilities:
        gates.append(_gate("spatial-platform", bool(request.get("platform")), "Declare the spatial platform.", mode))
        gates.append(_gate("spatial-unit", bool(request.get("spatial_unit")), "Declare spot, bin, or cell resolution.", mode))

    if "count-model" in intents:
        raw_counts = str(request.get("data_scale", "")).casefold() in {"raw-counts", "raw_counts", "counts", "integer-counts"}
        gates.append(_gate("count-scale", raw_counts, "DESeq2/edgeR count models require untransformed integer-like counts.", mode))

    if "machine-learning" in intents:
        gates.append(_gate("ml-outcome", bool(request.get("outcome")), "Declare the prediction outcome.", mode))
        split = str(request.get("split_strategy", "")).casefold()
        safe = any(term in split for term in ("patient", "site", "time", "external", "nested"))
        gates.append(_gate("ml-validation", safe, "Use patient/site/time-safe or external/nested validation.", mode))

    if mode == "resume":
        gates.append(_gate("resume-checkpoint", bool(request.get("resume_from")), "Declare the prior manifest or validated checkpoint.", mode))
    if mode == "reproduce-figure":
        gates.append(_gate("reference-figure", bool(request.get("reference_figure")), "Declare the reference figure and provenance.", mode))
    if mode == "explain":
        has_artifact = bool(_as_list(request.get("artifacts")) or request.get("figure_path"))
        gates.append(_gate("explain-artifact", has_artifact, "Declare the figure, table, or result object to audit.", mode))

    return sorted(gates, key=lambda gate: gate["id"])


def _stages(routes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered: list[tuple[str, str]] = [
        ("intake", "biomedical-analysis-agent"),
        ("data-profile", "biomedical-analysis-agent"),
        ("methodology-review", "biomedical-analysis-agent"),
    ]
    domain_order = (
        "single-cell", "spatial-transcriptomics", "bulk-rna",
        "quantitative-proteomics", "multi-omics",
    )
    by_capability = {item["capability"]: item for item in routes}
    for capability in domain_order:
        if capability in by_capability:
            ordered.append((capability, by_capability[capability]["skill"]))
    ordered.append(("analysis-qa", "biomedical-analysis-agent"))
    if "visualization" in by_capability:
        ordered.extend(
            [
                ("visualization", by_capability["visualization"]["skill"]),
                ("native-visual-review", "biomedical-analysis-agent"),
            ]
        )
    ordered.append(("interpretation", "biomedical-analysis-agent"))
    stages: list[dict[str, Any]] = []
    for index, (stage_id, skill) in enumerate(ordered, start=1):
        stages.append(
            {
                "node_id": f"{index:02d}-{stage_id}",
                "recipe_id": f"compiled:{stage_id}",
                "skill": skill,
                "depends_on": [] if index == 1 else [stages[-1]["node_id"]],
                "checkpoint_required": stage_id not in {"intake", "data-profile"},
                "environment_id": None,
            }
        )
    return stages


def compile_plan(request: dict[str, Any]) -> dict[str, Any]:
    """Compile a frozen deterministic WorkflowInstance-like plan."""
    validate_request(request)
    normalized = json.loads(_stable_json(request))
    request_hash = _fingerprint(normalized)
    plan_id = f"plan-{request_hash[:12]}"
    routes = route_request(normalized)
    gates = scientific_gates(normalized, routes)
    mode = normalized.get("mode", "plan")
    blocking = [gate for gate in gates if gate["status"] == "block"]
    if mode in {"run", "resume", "reproduce-figure"}:
        state = "ENV_PREPARING" if not blocking else "AWAITING_AUTHORIZATION"
    else:
        state = "PLAN_COMPILED"

    task_slug = _slug(str(normalized.get("task_slug") or normalized["question"]))
    run_id = str(normalized.get("run_id") or f"run-{request_hash[:12]}")
    project_root = str(normalized.get("project_root") or ".")
    run_root = _join_run_root(project_root, task_slug, run_id)
    output_contract = {item: str(Path(run_root) / Path(item)) for item in RUN_TREE}

    return {
        "schema_version": "1.0.0",
        "plan_id": plan_id,
        "request_sha256": request_hash,
        "mode": mode,
        "frozen": True,
        "state": state,
        "state_machine": list(STATE_MACHINE),
        "routes": routes,
        "scientific_gates": gates,
        "blocking_issues": blocking,
        "workflow": {
            "instance_id": f"workflow-{request_hash[:12]}",
            "template_id": "compiled-composite-v1",
            "plan_id": plan_id,
            "request_sha256": request_hash,
            "frozen": True,
            "mode": mode,
            "nodes": _stages(routes),
            "routes": [route["capability"] for route in routes],
            "scientific_gates": gates,
            "output_root": run_root,
            "method_substitution": "forbidden-without-new-plan",
            "cross_backend_exchange": "artifact-contract-only",
        },
        "environment_policy": {
            "task_local_only": True,
            "reuse": "exact-lock-hash-and-preflight",
            "immutable_after_freeze": True,
            "max_identical_install_retries": 2,
        },
        "run": {"task_slug": task_slug, "run_id": run_id, "root": run_root},
        "output_contract": {
            "directories": output_contract,
            "required_files": [
                "00_request/intent.yaml",
                "00_request/input_manifest.json",
                "01_plan/ANALYSIS_DESIGN.md",
                "01_plan/workflow.plan.yaml",
                "02_environment/environment_manifest.json",
                "03_scripts/params.yaml",
                "07_reports/FIGURE_NOTES.md",
                "07_reports/QA_REPORT.md",
                "07_reports/ARTIFACT_INDEX.md",
                "manifest/run_manifest.json",
                "manifest/artifact_ledger.jsonl",
            ],
        },
    }


def _load_json(path: str) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8-sig") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise RequestError("request JSON root must be an object")
    return data


def _write_json(value: Any, path: str | None) -> None:
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if path:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(payload, encoding="utf-8")
    else:
        sys.stdout.write(payload)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("route", "compile"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--request", required=True, help="UTF-8 JSON request")
        subparser.add_argument("--output", help="Write UTF-8 JSON here; default stdout")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        request = _load_json(args.request)
        result = route_request(request) if args.command == "route" else compile_plan(request)
        _write_json(result, args.output)
        return 0
    except (OSError, json.JSONDecodeError, RequestError) as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
