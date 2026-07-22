#!/usr/bin/env python3
"""Validate and compile a single-cell RNA-seq design without external packages.

This command is read-only with respect to input data. It can write a JSON report and
compiled stage plan only to explicitly supplied output paths. It never installs or
imports analysis packages.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


PLATFORMS = {
    "10x-3prime",
    "10x-5prime",
    "10x-feature-barcode",
    "10x-multiome-rna",
    "parse-evercode",
    "drop-seq",
    "seq-well",
    "smart-seq2",
    "generic-count-matrix",
    "processed-object",
}
INPUT_TYPES = {"10x_mtx_dir", "10x_h5", "h5ad", "rds", "mtx", "count_matrix"}
DROPLET_PLATFORMS = PLATFORMS - {"smart-seq2", "generic-count-matrix", "processed-object"}
INFERENTIAL_BRANCHES = {"condition_de", "composition"}
MEX_COMPONENTS = (
    ("matrix.mtx", "matrix.mtx.gz"),
    ("barcodes.tsv", "barcodes.tsv.gz"),
    ("features.tsv", "features.tsv.gz", "genes.tsv", "genes.tsv.gz"),
)

METHOD_DEPENDENCIES: dict[str, list[dict[str, str]]] = {
    "seurat-core": [
        {"name": "Seurat", "runtime": "r", "source": "cran"},
        {"name": "SingleCellExperiment", "runtime": "r", "source": "bioconductor"},
    ],
    "scanpy-core": [
        {"name": "scanpy", "runtime": "python", "source": "pypi"},
        {"name": "anndata", "runtime": "python", "source": "pypi"},
    ],
    "soupx": [{"name": "SoupX", "runtime": "r", "source": "cran"}],
    "cellbender": [{"name": "cellbender", "runtime": "python", "source": "pypi"}],
    "decontx": [{"name": "celda", "runtime": "r", "source": "bioconductor"}],
    "scdblfinder": [{"name": "scDblFinder", "runtime": "r", "source": "bioconductor"}],
    "doubletfinder": [{"name": "DoubletFinder", "runtime": "r", "source": "github-reviewed-pin-required"}],
    "scrublet": [{"name": "scrublet", "runtime": "python", "source": "pypi"}],
    "solo": [{"name": "scvi-tools", "runtime": "python", "source": "pypi"}],
    "harmony": [{"name": "harmony", "runtime": "r", "source": "cran"}],
    "seurat-rpca": [{"name": "Seurat", "runtime": "r", "source": "cran"}],
    "seurat-cca": [{"name": "Seurat", "runtime": "r", "source": "cran"}],
    "scvi": [{"name": "scvi-tools", "runtime": "python", "source": "pypi"}],
    "scanorama": [{"name": "scanorama", "runtime": "python", "source": "pypi"}],
    "pseudobulk": [
        {"name": "edgeR", "runtime": "r", "source": "bioconductor"},
        {"name": "DESeq2", "runtime": "r", "source": "bioconductor"},
    ],
    "composition": [{"name": "speckle", "runtime": "r", "source": "bioconductor"}],
    "trajectory": [{"name": "slingshot", "runtime": "r", "source": "bioconductor"}],
    "cnv": [{"name": "infercnv", "runtime": "r", "source": "bioconductor"}],
    "communication": [{"name": "CellChat", "runtime": "r", "source": "github-reviewed-pin-required"}],
}


def issue(code: str, severity: str, message: str, **context: Any) -> dict[str, Any]:
    item: dict[str, Any] = {"code": code, "severity": severity, "message": message}
    if context:
        item["context"] = context
    return item


def require_string(obj: dict[str, Any], key: str, issues: list[dict[str, Any]], scope: str) -> None:
    if not isinstance(obj.get(key), str) or not obj[key].strip():
        issues.append(issue("MISSING_FIELD", "error", f"{scope}.{key} must be a non-empty string"))


def has_mex_components(path: Path) -> tuple[bool, list[str]]:
    missing: list[str] = []
    for alternatives in MEX_COMPONENTS:
        if not any((path / name).is_file() for name in alternatives):
            missing.append("|".join(alternatives))
    return not missing, missing


def validate_structure(config: Any) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    if not isinstance(config, dict):
        return [issue("CONFIG_TYPE", "error", "Top-level configuration must be a JSON object")]
    for key in ("project_id", "organism"):
        require_string(config, key, issues, "config")
    if config.get("platform") not in PLATFORMS:
        issues.append(issue("PLATFORM", "error", "Unsupported or missing platform", value=config.get("platform")))
    samples = config.get("samples")
    if not isinstance(samples, list) or not samples:
        issues.append(issue("SAMPLES", "error", "samples must be a non-empty array"))
        samples = []
    for index, sample in enumerate(samples):
        if not isinstance(sample, dict):
            issues.append(issue("SAMPLE_TYPE", "error", "Each sample must be an object", index=index))
            continue
        for key in ("sample_id", "capture_id", "donor_id", "condition", "batch", "path"):
            require_string(sample, key, issues, f"samples[{index}]")
        if sample.get("input_type") not in INPUT_TYPES:
            issues.append(issue("INPUT_TYPE", "error", "Unsupported or missing input_type", index=index, value=sample.get("input_type")))
    analysis = config.get("analysis")
    if not isinstance(analysis, dict):
        issues.append(issue("ANALYSIS", "error", "analysis must be an object"))
    else:
        if analysis.get("mode") not in {"descriptive", "inferential"}:
            issues.append(issue("ANALYSIS_MODE", "error", "analysis.mode must be descriptive or inferential"))
        for key in ("condition_de", "composition", "trajectory", "cnv", "communication", "repeated_measures"):
            if key in analysis and not isinstance(analysis[key], bool):
                issues.append(issue("ANALYSIS_BOOLEAN", "error", f"analysis.{key} must be boolean"))
        if analysis.get("de_unit", "donor_pseudobulk") not in {"donor_pseudobulk", "sample_pseudobulk", "cell_level"}:
            issues.append(issue("DE_UNIT", "error", "Unsupported analysis.de_unit"))
    return issues


def validate_design(config: dict[str, Any], check_paths: bool = False) -> list[dict[str, Any]]:
    issues = validate_structure(config)
    if any(item["severity"] == "error" for item in issues):
        return issues

    samples: list[dict[str, Any]] = config["samples"]
    analysis: dict[str, Any] = config["analysis"]
    platform = config["platform"]
    multiplexing = config.get("multiplexing") or {"enabled": False, "method": "none", "demultiplexed": True}

    sample_counts = Counter(s["sample_id"] for s in samples)
    for sample_id, count in sample_counts.items():
        if count > 1:
            issues.append(issue("DUPLICATE_SAMPLE", "error", "sample_id must be unique", sample_id=sample_id, count=count))

    capture_to_samples: dict[str, list[str]] = defaultdict(list)
    for sample in samples:
        capture_to_samples[sample["capture_id"]].append(sample["sample_id"])
    if not multiplexing.get("enabled", False):
        for capture_id, member_samples in capture_to_samples.items():
            if len(member_samples) > 1:
                issues.append(issue("CAPTURE_REUSED", "error", "Multiple samples share a capture but multiplexing.enabled is false", capture_id=capture_id, samples=member_samples))
    elif not multiplexing.get("demultiplexed", False):
        issues.append(issue("DEMULTIPLEX_REQUIRED", "error", "Multiplexed captures must be demultiplexed before per-sample QC and doublet calling", method=multiplexing.get("method")))

    if platform == "smart-seq2" and any(s["input_type"] in {"10x_mtx_dir", "10x_h5"} for s in samples):
        issues.append(issue("PLATFORM_INPUT_MISMATCH", "error", "Smart-seq2 cannot be declared with a 10x-specific input type"))
    if platform in {"processed-object"} and any(s["input_type"] not in {"h5ad", "rds"} for s in samples):
        issues.append(issue("PLATFORM_INPUT_MISMATCH", "error", "processed-object platform requires h5ad or rds input"))

    if check_paths:
        for sample in samples:
            data_path = Path(os.path.expandvars(sample["path"])).expanduser()
            if not data_path.exists():
                issues.append(issue("INPUT_NOT_FOUND", "error", "Declared input path does not exist", sample_id=sample["sample_id"], path=str(data_path)))
            elif sample["input_type"] == "10x_mtx_dir":
                if not data_path.is_dir():
                    issues.append(issue("MEX_NOT_DIRECTORY", "error", "10x_mtx_dir must point to a directory", sample_id=sample["sample_id"], path=str(data_path)))
                else:
                    complete, missing = has_mex_components(data_path)
                    if not complete:
                        issues.append(issue("MEX_INCOMPLETE", "error", "10x MEX directory is missing required components", sample_id=sample["sample_id"], missing=missing))
            elif data_path.is_dir():
                issues.append(issue("FILE_EXPECTED", "error", "This input_type requires a file, not a directory", sample_id=sample["sample_id"], input_type=sample["input_type"]))
            raw_path = sample.get("raw_droplet_path")
            if raw_path and not Path(os.path.expandvars(str(raw_path))).expanduser().exists():
                issues.append(issue("RAW_DROPLET_NOT_FOUND", "error", "raw_droplet_path does not exist", sample_id=sample["sample_id"], path=raw_path))

    ambient = analysis.get("ambient_rna", "none")
    if platform not in DROPLET_PLATFORMS and ambient in {"soupx", "cellbender"}:
        issues.append(issue("AMBIENT_PLATFORM", "error", f"{ambient} requires a droplet-based assay"))
    if ambient in {"soupx", "cellbender"}:
        missing_raw = [s["sample_id"] for s in samples if not s.get("raw_droplet_path")]
        if missing_raw:
            issues.append(issue("RAW_DROPLETS_REQUIRED", "error", f"{ambient} requires an unfiltered droplet matrix for every capture", samples=missing_raw))
    elif ambient == "decontx" and not any(s.get("raw_droplet_path") for s in samples):
        issues.append(issue("BACKGROUND_LIMITED", "warning", "DecontX can run without an empty-droplet background, but contamination estimation is less informed"))

    if platform in DROPLET_PLATFORMS and analysis.get("doublets", "none") == "none":
        issues.append(issue("DOUBLETS_DISABLED", "warning", "Droplet workflow has no doublet caller; justify this choice and review doublet-sensitive clusters"))

    donors_by_condition: dict[str, set[str]] = defaultdict(set)
    conditions_by_donor: dict[str, set[str]] = defaultdict(set)
    batches_by_condition: dict[str, set[str]] = defaultdict(set)
    conditions_by_batch: dict[str, set[str]] = defaultdict(set)
    for sample in samples:
        donors_by_condition[sample["condition"]].add(sample["donor_id"])
        conditions_by_donor[sample["donor_id"]].add(sample["condition"])
        batches_by_condition[sample["condition"]].add(sample["batch"])
        conditions_by_batch[sample["batch"]].add(sample["condition"])

    inferential = analysis.get("mode") == "inferential" or any(analysis.get(key, False) for key in INFERENTIAL_BRANCHES)
    if inferential:
        for condition, donors in sorted(donors_by_condition.items()):
            if len(donors) < 2:
                issues.append(issue("INSUFFICIENT_REPLICATES", "error", "Inferential groups require at least two independent donors", condition=condition, donors=sorted(donors)))
            elif len(donors) < 3:
                issues.append(issue("LOW_REPLICATES", "warning", "Only two donors support this group; effect estimates and dispersion will be fragile", condition=condition))
        if len(donors_by_condition) < 2:
            issues.append(issue("NO_CONTRAST", "error", "Inferential analysis requires at least two conditions"))

    crossover_donors = {donor: sorted(conditions) for donor, conditions in conditions_by_donor.items() if len(conditions) > 1}
    if crossover_donors and not analysis.get("repeated_measures", False):
        issues.append(issue("REPEATED_MEASURES_UNDECLARED", "error", "Donors occur in multiple conditions; declare repeated_measures and use a paired/block model", donors=crossover_donors))

    completely_confounded = (
        len(batches_by_condition) > 1
        and all(len(batches) == 1 for batches in batches_by_condition.values())
        and all(len(conditions) == 1 for conditions in conditions_by_batch.values())
    )
    if completely_confounded:
        issues.append(issue("BATCH_CONDITION_CONFOUNDING", "error", "Condition is completely confounded with batch; integration cannot identify the condition effect", mapping={k: sorted(v) for k, v in batches_by_condition.items()}))

    if analysis.get("condition_de", False):
        de_unit = analysis.get("de_unit", "donor_pseudobulk")
        if de_unit == "cell_level":
            issues.append(issue("PSEUDOREPLICATION", "error", "Condition DE cannot treat cells as independent replicates; use donor-aware pseudobulk"))
        elif de_unit == "sample_pseudobulk" and any(len(v) > 1 for v in conditions_by_donor.values()):
            issues.append(issue("DONOR_BLOCK_REQUIRED", "error", "Repeated measures require donor-aware blocking even when aggregating by sample"))
        processed = [s["sample_id"] for s in samples if s["input_type"] in {"h5ad", "rds"}]
        if processed:
            issues.append(issue("RAW_COUNTS_AUDIT", "warning", "Processed objects require proof of an unmodified raw-count layer before pseudobulk DE", samples=processed))

    if len({s["batch"] for s in samples}) > 1 and analysis.get("integration", "none") == "none":
        issues.append(issue("NO_INTEGRATION", "warning", "Multiple batches are present but integration is disabled; justify or quantify batch structure"))

    if analysis.get("trajectory", False) and not analysis.get("trajectory_root"):
        issues.append(issue("TRAJECTORY_ROOT", "error", "Trajectory analysis requires a biologically justified root or root-selection rule"))
    if analysis.get("cnv", False) and not analysis.get("cnv_reference"):
        issues.append(issue("CNV_REFERENCE", "error", "CNV inference requires a credible diploid reference-cell definition"))
    if analysis.get("communication", False) and inferential and min((len(v) for v in donors_by_condition.values()), default=0) < 3:
        issues.append(issue("COMMUNICATION_REPLICATES", "warning", "Replicate-aware differential communication is fragile with fewer than three donors per condition"))

    if not config.get("reference_build"):
        issues.append(issue("REFERENCE_BUILD", "warning", "reference_build is absent; record genome/transcriptome provenance before execution"))
    if not config.get("feature_namespace"):
        issues.append(issue("FEATURE_NAMESPACE", "warning", "feature_namespace is absent; record Ensembl/symbol namespace and version before annotation"))
    if not config.get("tissue"):
        issues.append(issue("TISSUE", "warning", "tissue is absent; QC and annotation cannot be tissue-aware"))

    return issues


def stages_for(config: dict[str, Any]) -> list[dict[str, Any]]:
    analysis = config["analysis"]
    multiplexing = config.get("multiplexing") or {}
    stages: list[tuple[str, str, bool, str]] = [
        ("SC00_INTAKE", "Freeze question, estimand, design and inputs", True, "request_and_design"),
        ("SC01_IMPORT_AND_IDENTITY", "Import counts and lock cell-to-capture-to-donor identity", True, "identity_locked"),
        ("SC02_DEMULTIPLEX", "Demultiplex pooled captures", bool(multiplexing.get("enabled")), "identity_locked"),
        ("SC03_AMBIENT_RNA", "Estimate and remove ambient RNA per capture", analysis.get("ambient_rna", "none") != "none", "raw_counts_preserved"),
        ("SC04_QC_PER_CAPTURE", "Calculate platform-aware QC and filter per capture", True, "qc_accepted"),
        ("SC05_DOUBLETS_PER_CAPTURE", "Call and review doublets per capture", analysis.get("doublets", "none") != "none", "doublets_accepted"),
        ("SC06_NORMALIZE_AND_HVG", "Normalize and select features without overwriting raw counts", True, "raw_counts_preserved"),
        ("SC07_MERGE_AND_INTEGRATE", "Merge and correct the analysis embedding", analysis.get("integration", "none") != "none", "integration_accepted"),
        ("SC08_GRAPH_CLUSTER_AND_EMBED", "Build graph, resolution sweep, clusters and embeddings", True, "clusters_accepted"),
        ("SC09_ANNOTATE_AND_REVIEW", "Annotate with evidence, confidence and unknown labels", True, "annotation_accepted"),
        ("SC10_PSEUDOBULK_DE", "Aggregate raw counts and fit donor-aware expression models", bool(analysis.get("condition_de")), "inference_ready"),
        ("SC11_DIFFERENTIAL_ABUNDANCE", "Test donor-aware composition changes", bool(analysis.get("composition")), "inference_ready"),
        ("SC12_TRAJECTORY", "Infer and sensitivity-check trajectories", bool(analysis.get("trajectory")), "advanced_reviewed"),
        ("SC12_CNV", "Infer relative CNV against declared diploid references", bool(analysis.get("cnv")), "advanced_reviewed"),
        ("SC12_COMMUNICATION", "Estimate and replicate-review ligand-receptor patterns", bool(analysis.get("communication")), "advanced_reviewed"),
        ("SC13_FIGURES_AND_INTERPRETATION", "Render figures, scientific QA and claim boundaries", True, "delivery_ready"),
    ]
    active: list[dict[str, Any]] = []
    previous: str | None = None
    for stage_id, purpose, enabled, checkpoint in stages:
        if not enabled:
            continue
        item = {
            "stage_id": stage_id,
            "purpose": purpose,
            "depends_on": [previous] if previous else [],
            "write_target": f"_staging/{stage_id}",
            "checkpoint": checkpoint,
            "promotion_rule": "Promote only after declared ArtifactContracts pass; non-zero exit or partial files fail the stage.",
        }
        active.append(item)
        previous = stage_id
    return active


def dependency_recipe(config: dict[str, Any]) -> dict[str, Any]:
    analysis = config["analysis"]
    methods = ["seurat-core"]
    for key in ("ambient_rna", "doublets", "integration"):
        method = analysis.get(key, "none")
        if method != "none":
            methods.append(method)
    if analysis.get("condition_de"):
        methods.append("pseudobulk")
    for branch in ("composition", "trajectory", "cnv", "communication"):
        if analysis.get(branch):
            methods.append(branch)
    dependencies: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for method in methods:
        for dep in METHOD_DEPENDENCIES.get(method, []):
            key = (dep["name"], dep["runtime"], dep["source"])
            if key not in seen:
                dependencies.append(dep)
                seen.add(key)
    return {
        "recipe_type": "dependency_declaration_only",
        "methods": methods,
        "runtimes": sorted({dep["runtime"] for dep in dependencies}),
        "dependencies": dependencies,
        "environment_manager": "biomedical-analysis-agent/scripts/environment_manager.py",
        "installation_commands_allowed": False,
        "method_substitution_allowed": False,
    }


def compile_report(config: dict[str, Any], check_paths: bool = False) -> dict[str, Any]:
    issues = validate_design(config, check_paths=check_paths)
    errors = [item for item in issues if item["severity"] == "error"]
    warnings = [item for item in issues if item["severity"] == "warning"]
    report: dict[str, Any] = {
        "schema_version": "1.0.0",
        "project_id": config.get("project_id"),
        "status": "invalid" if errors else "valid_with_warnings" if warnings else "valid",
        "summary": {"errors": len(errors), "warnings": len(warnings), "samples": len(config.get("samples", []))},
        "issues": issues,
        "execution_authorized": False,
        "maturity": "parse-verified",
    }
    if not errors:
        report["workflow_instance"] = {
            "frozen": True,
            "platform": config["platform"],
            "biological_unit": "donor_id",
            "stages": stages_for(config),
            "analysis_recipe": dependency_recipe(config),
            "artifact_contract_schema": "../references/artifact-contract.schema.json",
        }
    return report


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("top-level JSON must be an object")
    return value


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(temporary, path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate and compile an scRNA-seq design without installing packages")
    parser.add_argument("--config", required=True, type=Path, help="Input JSON design")
    parser.add_argument("--output", type=Path, help="Optional JSON report path")
    parser.add_argument("--check-paths", action="store_true", help="Read-only existence and 10x MEX structure checks")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_json(args.config)
        report = compile_report(config, check_paths=args.check_paths)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "invalid", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    if args.output:
        write_json(args.output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 2 if report["status"] == "invalid" else 0


if __name__ == "__main__":
    raise SystemExit(main())
