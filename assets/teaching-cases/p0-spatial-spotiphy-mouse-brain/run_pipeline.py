#!/usr/bin/env python3
"""Run a bounded, descriptive Visium teaching workflow on declared local inputs.

This runner deliberately does not execute Spotiphy deconvolution.  It profiles the
real spatial and reference objects, validates the coordinate/image transform,
performs platform-aware spot QC, builds a declared Visium lattice graph, computes
an expression-only clustering and descriptive Moran-I ranking, and renders figures
for a separate native-pixel review step.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import shutil
import sys
from pathlib import Path
from typing import Any, Callable

import anndata as ad
import h5py
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import scipy
import scipy.sparse as sp
from PIL import Image
from scipy.sparse.csgraph import connected_components
from scipy.stats import spearmanr
from sklearn.cluster import KMeans
from sklearn.decomposition import PCA
from sklearn.metrics import adjusted_rand_score, silhouette_score
from sklearn.neighbors import NearestNeighbors


RUNNER_VERSION = "1.0.1"
THREAD_ENV = (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
)


class PipelineError(RuntimeError):
    """Raised for a scientific or artifact-contract failure."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(stable_json(value), encoding="utf-8")


def write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value.rstrip() + "\n", encoding="utf-8")


def tree_contract(root: Path, *, exclude: set[str] | None = None) -> dict[str, Any]:
    excluded = exclude or set()
    records: list[dict[str, Any]] = []
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        if relative in excluded:
            continue
        record = {
            "relative_path": relative,
            "size_bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        records.append(record)
        digest.update((json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"))
    return {"tree_sha256": digest.hexdigest(), "file_count": len(records), "records": records}


def check_input(path: Path, evidence: dict[str, Any], role: str) -> dict[str, Any]:
    if not path.is_file():
        raise PipelineError(f"missing_input:{role}:{path}")
    observed = {"size_bytes": path.stat().st_size, "sha256": sha256_file(path)}
    if observed["size_bytes"] != evidence.get("size_bytes"):
        raise PipelineError(f"input_size_mismatch:{role}:{observed['size_bytes']}")
    if observed["sha256"] != evidence.get("sha256"):
        raise PipelineError(f"input_hash_mismatch:{role}:{observed['sha256']}")
    return {
        "role": role,
        "locator_ref": evidence.get("locator_ref"),
        "size_bytes": observed["size_bytes"],
        "sha256": observed["sha256"],
        "access_mode": "read-only",
    }


def promote_stage(
    run_root: Path,
    stage_id: str,
    builder: Callable[[Path], dict[str, Any]],
    upstream: dict[str, str],
) -> tuple[Path, dict[str, Any]]:
    staging = run_root / "_staging" / f"{stage_id}-attempt1"
    final = run_root / "04_intermediate" / stage_id
    if staging.exists() or final.exists():
        raise PipelineError(f"stage_target_exists:{stage_id}")
    staging.mkdir(parents=True, exist_ok=False)
    validation = builder(staging)
    if validation.get("ok") is not True:
        raise PipelineError(f"stage_validation_failed:{stage_id}:{validation}")
    write_json(staging / "stage-validation.json", validation)
    payload = tree_contract(staging)
    checkpoint = {
        "stage_id": stage_id,
        "status": "checkpointed",
        "attempt": 1,
        "upstream_hashes": upstream,
        "payload_tree_sha256": payload["tree_sha256"],
        "payload_file_count": payload["file_count"],
        "validation": validation,
    }
    write_json(staging / "checkpoint.json", checkpoint)
    final.parent.mkdir(parents=True, exist_ok=True)
    os.replace(staging, final)
    return final, checkpoint


def matrix_stats(matrix: Any) -> dict[str, Any]:
    values = matrix.data if sp.issparse(matrix) else np.asarray(matrix).ravel()
    return {
        "shape": [int(matrix.shape[0]), int(matrix.shape[1])],
        "dtype": str(matrix.dtype),
        "sparse": bool(sp.issparse(matrix)),
        "nnz": int(matrix.nnz if sp.issparse(matrix) else np.count_nonzero(matrix)),
        "min_nonzero": float(values.min()) if values.size else 0.0,
        "max": float(values.max()) if values.size else 0.0,
        "sum": float(matrix.sum()),
        "noninteger_nonzero_count": int(np.count_nonzero(np.abs(values - np.rint(values)) > 1e-8)),
        "negative_nonzero_count": int(np.count_nonzero(values < 0)),
    }


def robust_thresholds(values: np.ndarray, *, direction: str, multiplier: float) -> dict[str, float]:
    values = np.asarray(values, dtype=float)
    median = float(np.median(values))
    mad = float(np.median(np.abs(values - median)))
    robust_sigma = 1.4826 * mad
    threshold = median - multiplier * robust_sigma if direction == "low" else median + multiplier * robust_sigma
    return {"median": median, "mad": mad, "robust_sigma": robust_sigma, "threshold": float(threshold)}


def visium_lattice_graph(rows: np.ndarray, cols: np.ndarray) -> sp.csr_matrix:
    lookup = {(int(row), int(col)): index for index, (row, col) in enumerate(zip(rows, cols))}
    deltas = ((0, -2), (0, 2), (-1, -1), (-1, 1), (1, -1), (1, 1))
    source: list[int] = []
    target: list[int] = []
    for index, (row, col) in enumerate(zip(rows, cols)):
        for delta_row, delta_col in deltas:
            neighbor = lookup.get((int(row + delta_row), int(col + delta_col)))
            if neighbor is not None:
                source.append(index)
                target.append(neighbor)
    data = np.ones(len(source), dtype=np.float64)
    graph = sp.csr_matrix((data, (source, target)), shape=(len(rows), len(rows)))
    graph = graph.maximum(graph.T)
    graph.setdiag(0)
    graph.eliminate_zeros()
    return graph


def row_normalize(graph: sp.csr_matrix) -> sp.csr_matrix:
    degrees = np.asarray(graph.sum(axis=1)).ravel()
    inverse = np.divide(1.0, degrees, out=np.zeros_like(degrees, dtype=float), where=degrees > 0)
    return sp.diags(inverse) @ graph


def moran_vector(values: np.ndarray, weights: sp.csr_matrix) -> np.ndarray:
    centered = values - np.mean(values, axis=0, keepdims=True)
    denominator = np.sum(centered * centered, axis=0)
    spatial = weights @ centered
    numerator = np.sum(centered * spatial, axis=0)
    return np.divide(numerator, denominator, out=np.full_like(numerator, np.nan), where=denominator > 0)


def figure_pair(
    figure_id: str,
    staging_root: Path,
    draw: Callable[[tuple[float, float], int], plt.Figure],
    original_size: tuple[float, float],
    final_size: tuple[float, float],
) -> dict[str, Any]:
    records: dict[str, Any] = {"figure_id": figure_id, "backend": "python"}
    for kind, size, dpi in (("original", original_size, 300), ("final", final_size, 220)):
        output = staging_root / "figures" / kind / f"{figure_id}.png"
        output.parent.mkdir(parents=True, exist_ok=True)
        figure = draw(size, dpi)
        figure.savefig(
            output,
            dpi=dpi,
            bbox_inches="tight",
            facecolor="white",
            metadata={"Software": "biomedical-analysis-agent"},
        )
        plt.close(figure)
        with Image.open(output) as image:
            records[kind] = {
                "relative_path": f"figures/{kind}/{figure_id}.png",
                "sha256": sha256_file(output),
                "size_bytes": output.stat().st_size,
                "pixel_dimensions": [int(image.width), int(image.height)],
                "physical_inches": [float(size[0]), float(size[1])],
                "dpi": dpi,
            }
    return records


def _spatial_panel(
    axis: plt.Axes,
    image: np.ndarray,
    coords: np.ndarray,
    values: np.ndarray,
    *,
    title: str,
    cmap: str = "viridis",
    categorical: bool = False,
    alpha_image: float = 0.55,
):
    axis.imshow(image, origin="upper", alpha=alpha_image)
    if categorical:
        unique = np.unique(values)
        colors = plt.get_cmap("tab10")(np.linspace(0, 1, max(len(unique), 2)))
        for color, value in zip(colors, unique):
            mask = values == value
            axis.scatter(coords[mask, 0], coords[mask, 1], s=14, color=color, label=str(value), linewidths=0, alpha=0.88)
        axis.legend(title="Expression cluster", frameon=False, fontsize=7, title_fontsize=8, loc="upper left", bbox_to_anchor=(1.01, 1.0))
        artist = None
    else:
        artist = axis.scatter(coords[:, 0], coords[:, 1], s=12, c=values, cmap=cmap, linewidths=0, alpha=0.9)
    axis.set_xlim(0, image.shape[1])
    axis.set_ylim(image.shape[0], 0)
    axis.set_aspect("equal")
    axis.set_title(title, fontsize=10)
    axis.set_xlabel("full-resolution x (pixel)")
    axis.set_ylabel("full-resolution y (pixel)")
    axis.tick_params(labelsize=7)
    return artist


def run_fresh(config_path: Path) -> dict[str, Any]:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    run_root = Path(config["run_root"]).resolve()
    if run_root.exists():
        raise PipelineError(f"run_root_exists:{run_root}")
    run_root.mkdir(parents=True, exist_ok=False)
    for relative in (
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
        "_staging",
    ):
        (run_root / relative).mkdir(parents=True, exist_ok=True)

    authorization = config.get("authorization", {})
    if authorization != {
        "mode": "run",
        "scope": "task-local",
        "source_access": "read-only",
        "global_changes": False,
        "remote_publication": False,
    }:
        raise PipelineError("authorization_contract_mismatch")

    sources: dict[str, tuple[Path, dict[str, Any]]] = {}
    input_evidence: list[dict[str, Any]] = []
    for key in ("st_h5ad", "scrna_h5ad", "histology_png", "visiumhd_zip"):
        evidence = config["inputs"][key]
        source = Path(evidence["path"]).resolve()
        sources[key] = (source, evidence)
        input_evidence.append(check_input(source, evidence, key))

    environment_source = Path(config["environment"]["manifest_path"]).resolve()
    environment_manifest = json.loads(environment_source.read_text(encoding="utf-8"))
    if environment_manifest.get("lock_hash") != config["environment"].get("lock_hash"):
        raise PipelineError("environment_lock_hash_mismatch")
    if environment_manifest.get("state") != "frozen" or environment_manifest.get("global_changes") is not False:
        raise PipelineError("environment_not_frozen_or_global")

    shutil.copy2(config_path, run_root / "00_request" / "input-config.private.json")
    shutil.copy2(Path(__file__).resolve(), run_root / "03_scripts" / "run_pipeline.py")
    shutil.copy2(environment_source, run_root / "02_environment" / "environment_manifest.json")
    for name in ("uv_lock_path", "pyproject_path", "install_log_path", "requirements_lock_path", "marker_path"):
        source_value = config["environment"].get(name)
        if source_value:
            source = Path(source_value).resolve()
            if not source.is_file():
                raise PipelineError(f"environment_evidence_missing:{name}")
            target_name = {
                "uv_lock_path": "uv.lock",
                "pyproject_path": "pyproject.toml",
                "install_log_path": "install.log",
                "requirements_lock_path": "requirements-lock.txt",
                "marker_path": "environment.locked.json",
            }[name]
            shutil.copy2(source, run_root / "02_environment" / target_name)

    intent = {
        "mode": "run",
        "case_id": config["case_id"],
        "research_question": "Can this single Visium section support a reproducible descriptive spatial profile and coordinate-faithful figure set?",
        "platform": "visium",
        "assay_class": "capture",
        "assay_unit": "spot",
        "spatial_unit": "spot",
        "sampling_unit": "section",
        "inference_unit": "animal/specimen (one available; no population inference)",
        "requested_modules": config["requested_modules"],
        "authorization": authorization,
    }
    write_json(run_root / "00_request" / "intent.yaml", intent)
    write_json(
        run_root / "00_request" / "input_manifest.json",
        {
            "schema_version": "1.0",
            "platform": "visium",
            "assay_class": "capture",
            "assay_unit": "spot",
            "species": "mouse",
            "coordinate_unit": "full-resolution pixel",
            "samples": [
                {
                    "sample_id": config["sample"]["sample_id"],
                    "subject_id": config["sample"]["subject_id"],
                    "section_id": config["sample"]["section_id"],
                }
            ],
            "inputs": input_evidence,
            "source_mode": "read-only",
        },
    )
    workflow = {
        "schema_version": "1.0",
        "mode": "run",
        "frozen": True,
        "run": {"run_id": config["run_id"], "root": str(run_root)},
        "stages": [
            "S00_INTAKE",
            "S10_INGEST",
            "S20_COORD_IMAGE_QC",
            "S30_UNIT_QC",
            "S40_PREPROCESS",
            "S50_SPATIAL_GRAPH",
            "S60_CORE_DISCOVERY",
            "S70_COMPOSITION_MAPPING",
            "S90_INFERENCE_QA",
            "S95_VISUALIZE_INTERPRET",
        ],
        "scientific_gates": [
            {"gate": "platform-and-assay-unit", "status": "pass"},
            {"gate": "single-section-descriptive-only", "status": "pass"},
            {"gate": "Spotiphy-model-environment", "status": "blocked-optional-branch"},
            {"gate": "image-transform", "status": "pending-S20"},
        ],
    }
    write_json(run_root / "01_plan" / "workflow.plan.yaml", workflow)
    write_text(
        run_root / "01_plan" / "ANALYSIS_DESIGN.md",
        """# Analysis design

This is a reduced-but-real, single-section 10x Visium teaching execution. The measured and spatial unit is the capture spot. The available section is the only sampling unit, so all outputs are descriptive and within-section exploratory.

The executable core validates raw integer counts, vendor array coordinates, full-resolution pixel coordinates, histology identity/orientation, spot QC, a Visium hex-lattice graph, log-normalized expression, expression-only clusters and descriptive Moran-I rankings. The Spotiphy deconvolution/decomposition branch is recorded as blocked and is not replaced by another method.

No spot-level p-value, cohort contrast, population effect, cell-type abundance, direct cell contact, signaling, lineage, clinical or causal claim is permitted. Moran-I values are rankings without permutation p-values or FDR and therefore are not confirmatory SVG calls.
""",
    )
    write_json(run_root / "03_scripts" / "params.yaml", config["parameters"])

    checkpoints: dict[str, dict[str, Any]] = {}
    context: dict[str, Any] = {}

    def stage_intake(stage: Path) -> dict[str, Any]:
        write_json(stage / "source-hashes.json", {item["role"]: item for item in input_evidence})
        write_json(
            stage / "rights-citation-boundary.json",
            {
                "code_repository_commit": config["governance"].get("repository_commit"),
                "code_license": "Apache-2.0",
                "dataset_record": "Zenodo 10.5281/zenodo.10520022",
                "dataset_record_license": "CC BY 4.0",
                "primary_method": "Yang et al., Nature Methods 22, 724-736 (2025)",
                "tutorial_byte_level_redistribution": "not-authorized-by-this-run",
                "run_distribution": "private-local-only",
            },
        )
        return {"ok": True, "source_count": len(input_evidence), "source_mode": "read-only"}

    stage_path, checkpoints["S00_INTAKE"] = promote_stage(run_root, "S00_INTAKE", stage_intake, {})

    st = ad.read_h5ad(sources["st_h5ad"][0])
    scrna = ad.read_h5ad(sources["scrna_h5ad"][0], backed="r")
    image = np.asarray(Image.open(sources["histology_png"][0]).convert("RGB"))

    def stage_ingest(stage: Path) -> dict[str, Any]:
        st_stats = matrix_stats(st.X)
        reference_stats = {
            "shape": [int(scrna.n_obs), int(scrna.n_vars)],
            "obs_columns": list(scrna.obs.columns),
            "var_columns": list(scrna.var.columns),
            "celltype_nonmissing": int(scrna.obs["celltype"].notna().sum()) if "celltype" in scrna.obs else 0,
            "reference_sample_nonmissing": int(scrna.obs["orig.ident"].notna().sum()) if "orig.ident" in scrna.obs else 0,
            "matrix_loaded": False,
        }
        overlap = pd.DataFrame({"gene": sorted(set(st.var_names) & set(scrna.var_names))})
        overlap.to_csv(stage / "gene-overlap.tsv", sep="\t", index=False)
        reconciliation = {
            "st_shape": [int(st.n_obs), int(st.n_vars)],
            "reference_shape": [int(scrna.n_obs), int(scrna.n_vars)],
            "shared_gene_count": int(len(overlap)),
            "st_only_gene_count": int(st.n_vars - len(overlap)),
            "reference_only_gene_count": int(scrna.n_vars - len(overlap)),
            "st_obs_names_unique": bool(st.obs_names.is_unique),
            "st_var_names_unique": bool(st.var_names.is_unique),
            "reference_obs_names_unique": bool(scrna.obs_names.is_unique),
            "reference_var_names_unique": bool(scrna.var_names.is_unique),
        }
        write_json(stage / "st-matrix-profile.json", st_stats)
        write_json(stage / "reference-profile.json", reference_stats)
        write_json(stage / "identifier-reconciliation.json", reconciliation)
        context.update({"st_stats": st_stats, "reference_stats": reference_stats, "reconciliation": reconciliation})
        ok = (
            st_stats["negative_nonzero_count"] == 0
            and st_stats["noninteger_nonzero_count"] == 0
            and st.n_obs == 685
            and len(overlap) > 0
            and reconciliation["st_obs_names_unique"]
        )
        return {"ok": bool(ok), **reconciliation, "raw_integer_counts_confirmed": st_stats["noninteger_nonzero_count"] == 0}

    stage_path, checkpoints["S10_INGEST"] = promote_stage(
        run_root,
        "S10_INGEST",
        stage_ingest,
        {"S00_INTAKE": checkpoints["S00_INTAKE"]["payload_tree_sha256"]},
    )

    coords = np.asarray(st.obsm["spatial"], dtype=float)
    spatial_uns = st.uns.get("spatial", {})
    library_ids = list(spatial_uns)
    if len(library_ids) != 1:
        raise PipelineError(f"expected_one_spatial_library:{library_ids}")
    library_id = library_ids[0]
    library = spatial_uns[library_id]
    hires = np.asarray(library["images"]["hires"])[..., :3]
    scale = float(library["scalefactors"]["tissue_hires_scalef"])
    spot_diameter = float(library["scalefactors"]["spot_diameter_fullres"])

    def stage_coord(stage: Path) -> dict[str, Any]:
        resized = np.asarray(
            Image.fromarray(image).resize((hires.shape[1], hires.shape[0]), Image.Resampling.BILINEAR),
            dtype=np.float32,
        ) / 255.0
        identity_corr = float(np.corrcoef(resized.ravel(), hires.ravel())[0, 1])
        flip_y_corr = float(np.corrcoef(resized[::-1].ravel(), hires.ravel())[0, 1])
        flip_x_corr = float(np.corrcoef(resized[:, ::-1].ravel(), hires.ravel())[0, 1])
        bounds_ok = bool(
            np.all(np.isfinite(coords))
            and coords[:, 0].min() >= 0
            and coords[:, 1].min() >= 0
            and coords[:, 0].max() < image.shape[1]
            and coords[:, 1].max() < image.shape[0]
        )
        expected_full = [int(round(hires.shape[1] / scale)), int(round(hires.shape[0] / scale))]
        dimensions_ok = expected_full == [int(image.shape[1]), int(image.shape[0])]
        identity_supported = bool(
            bounds_ok
            and dimensions_ok
            and identity_corr >= float(config["parameters"]["overlay_identity_corr_min"])
            and identity_corr > max(flip_y_corr, flip_x_corr) + 0.1
        )
        audit = {
            "platform": "visium",
            "library_id": library_id,
            "coordinate_system": "Space Ranger full-resolution image pixels",
            "coordinate_order": ["x", "y"],
            "image_array_order": ["y", "x", "channel"],
            "origin": "upper-left",
            "y_direction": "down",
            "fullres_image_dimensions_xy": [int(image.shape[1]), int(image.shape[0])],
            "embedded_hires_dimensions_xy": [int(hires.shape[1]), int(hires.shape[0])],
            "tissue_hires_scalef": scale,
            "expected_fullres_dimensions_xy": expected_full,
            "coordinate_min_xy": coords.min(axis=0).tolist(),
            "coordinate_max_xy": coords.max(axis=0).tolist(),
            "coordinate_unique_count": int(np.unique(coords, axis=0).shape[0]),
            "spot_diameter_fullres_pixels": spot_diameter,
            "identity_bilinear_correlation": identity_corr,
            "flip_y_correlation": flip_y_corr,
            "flip_x_correlation": flip_x_corr,
            "coordinate_bounds_ok": bounds_ok,
            "image_dimensions_scale_ok": dimensions_ok,
            "identity_transform_quantitatively_supported": identity_supported,
            "native_overlay_review": "pending",
        }
        pd.DataFrame(
            {
                "sample_id": config["sample"]["sample_id"],
                "unit_id": st.obs_names.astype(str),
                "x": coords[:, 0],
                "y": coords[:, 1],
                "coordinate_system": "fullres_pixel_upper_left_y_down",
                "array_row": st.obs["array_row"].to_numpy(),
                "array_col": st.obs["array_col"].to_numpy(),
                "in_tissue": st.obs["in_tissue"].to_numpy(),
            }
        ).to_csv(stage / "coordinates.tsv", sep="\t", index=False)
        write_json(stage / "transform-audit.json", audit)
        context["transform"] = audit
        return {"ok": identity_supported, "identity_transform_quantitatively_supported": identity_supported, "native_review": "pending"}

    stage_path, checkpoints["S20_COORD_IMAGE_QC"] = promote_stage(
        run_root,
        "S20_COORD_IMAGE_QC",
        stage_coord,
        {"S10_INGEST": checkpoints["S10_INGEST"]["payload_tree_sha256"]},
    )

    matrix = st.X.tocsr() if sp.issparse(st.X) else sp.csr_matrix(st.X)
    total_counts = np.asarray(matrix.sum(axis=1)).ravel()
    detected = np.asarray((matrix > 0).sum(axis=1)).ravel()
    mt_mask = np.asarray([str(name).lower().startswith("mt-") for name in st.var_names])
    mt_counts = np.asarray(matrix[:, mt_mask].sum(axis=1)).ravel() if mt_mask.any() else np.zeros(st.n_obs)
    pct_mt = np.divide(mt_counts, total_counts, out=np.zeros_like(total_counts, dtype=float), where=total_counts > 0) * 100.0
    multiplier = float(config["parameters"]["qc_mad_multiplier"])
    count_threshold = robust_thresholds(np.log1p(total_counts), direction="low", multiplier=multiplier)
    gene_threshold = robust_thresholds(detected, direction="low", multiplier=multiplier)
    mt_threshold = robust_thresholds(pct_mt, direction="high", multiplier=multiplier)
    flag_low_counts = np.log1p(total_counts) < count_threshold["threshold"]
    flag_low_genes = detected < gene_threshold["threshold"]
    flag_high_mt = pct_mt > mt_threshold["threshold"]
    qc_flag = flag_low_counts | flag_low_genes | flag_high_mt | (total_counts <= 0)
    qc = pd.DataFrame(
        {
            "sample_id": config["sample"]["sample_id"],
            "unit_id": st.obs_names.astype(str),
            "total_counts": total_counts,
            "n_genes_by_counts": detected,
            "pct_counts_mt": pct_mt,
            "flag_low_counts": flag_low_counts,
            "flag_low_genes": flag_low_genes,
            "flag_high_mt": flag_high_mt,
            "qc_flag_any": qc_flag,
            "retained_primary_view": total_counts > 0,
        }
    )

    def stage_qc(stage: Path) -> dict[str, Any]:
        qc.to_csv(stage / "spot-qc.tsv", sep="\t", index=False)
        thresholds = {
            "policy": "flag-only robust MAD; no adaptive exclusion in the primary single-section teaching view",
            "qc_mad_multiplier": multiplier,
            "log1p_total_counts_low": count_threshold,
            "n_genes_low": gene_threshold,
            "pct_mt_high": mt_threshold,
        }
        write_json(stage / "qc-thresholds.json", thresholds)
        pd.DataFrame(
            [
                {"step": "vendor in-tissue matrix", "units": int(st.n_obs)},
                {"step": "nonzero-count analysis view", "units": int(np.count_nonzero(total_counts > 0))},
                {"step": "robust-QC flagged (not automatically removed)", "units": int(qc_flag.sum())},
            ]
        ).to_csv(stage / "attrition.tsv", sep="\t", index=False)
        context["qc"] = {"thresholds": thresholds, "flagged": int(qc_flag.sum())}
        return {
            "ok": bool(np.all(total_counts > 0) and np.all(np.isfinite(pct_mt))),
            "spots": int(st.n_obs),
            "primary_retained": int(np.count_nonzero(total_counts > 0)),
            "flagged_not_removed": int(qc_flag.sum()),
            "mt_gene_count": int(mt_mask.sum()),
        }

    stage_path, checkpoints["S30_UNIT_QC"] = promote_stage(
        run_root,
        "S30_UNIT_QC",
        stage_qc,
        {"S20_COORD_IMAGE_QC": checkpoints["S20_COORD_IMAGE_QC"]["payload_tree_sha256"]},
    )

    scale_factor = np.divide(10000.0, total_counts, out=np.zeros_like(total_counts, dtype=float), where=total_counts > 0)
    log_matrix = matrix.multiply(scale_factor[:, None]).tocsr()
    log_matrix.data = np.log1p(log_matrix.data)
    mean = np.asarray(log_matrix.mean(axis=0)).ravel()
    mean_sq = np.asarray(log_matrix.power(2).mean(axis=0)).ravel()
    variance = np.maximum(mean_sq - mean * mean, 0)
    detected_spots = np.asarray((matrix > 0).sum(axis=0)).ravel()
    eligible = (detected_spots >= int(config["parameters"]["minimum_detected_spots"])) & (~mt_mask)
    eligible_indices = np.flatnonzero(eligible)
    selected_count = min(int(config["parameters"]["selected_feature_count"]), len(eligible_indices))
    selected_indices = eligible_indices[np.argsort(variance[eligible_indices], kind="stable")[-selected_count:][::-1]]
    selected_dense = log_matrix[:, selected_indices].toarray().astype(np.float32)
    component_count = min(int(config["parameters"]["pca_components"]), selected_dense.shape[0] - 1, selected_dense.shape[1])
    pca = PCA(n_components=component_count, svd_solver="full")
    pcs = pca.fit_transform(selected_dense)

    def stage_preprocess(stage: Path) -> dict[str, Any]:
        pd.DataFrame(
            {
                "gene": st.var_names[selected_indices].astype(str),
                "variance_log1p_cpm": variance[selected_indices],
                "detected_spots": detected_spots[selected_indices],
                "selection_rank": np.arange(1, len(selected_indices) + 1),
            }
        ).to_csv(stage / "selected-features.tsv", sep="\t", index=False)
        embedding = pd.DataFrame({"unit_id": st.obs_names.astype(str), **{f"PC{index + 1}": pcs[:, index] for index in range(min(10, pcs.shape[1]))}})
        embedding.to_csv(stage / "pca-embedding.tsv", sep="\t", index=False)
        sp.save_npz(stage / "log1p-cpm-selected-features.npz", sp.csr_matrix(selected_dense))
        write_json(
            stage / "preprocess-profile.json",
            {
                "normalization": "library-size 10,000 then log1p",
                "raw_counts_mutated": False,
                "selected_feature_count": int(selected_count),
                "minimum_detected_spots": int(config["parameters"]["minimum_detected_spots"]),
                "pca_components": int(component_count),
                "explained_variance_ratio": pca.explained_variance_ratio_.tolist(),
            },
        )
        return {"ok": bool(np.all(np.isfinite(pcs))), "selected_features": int(selected_count), "pca_components": int(component_count)}

    stage_path, checkpoints["S40_PREPROCESS"] = promote_stage(
        run_root,
        "S40_PREPROCESS",
        stage_preprocess,
        {"S30_UNIT_QC": checkpoints["S30_UNIT_QC"]["payload_tree_sha256"]},
    )

    rows = st.obs["array_row"].to_numpy(dtype=int)
    cols = st.obs["array_col"].to_numpy(dtype=int)
    lattice = visium_lattice_graph(rows, cols)
    lattice_degrees = np.asarray(lattice.sum(axis=1)).ravel()
    component_number, component_labels = connected_components(lattice, directed=False)
    knn = NearestNeighbors(n_neighbors=min(7, st.n_obs), metric="euclidean")
    knn.fit(coords)
    knn_indices = knn.kneighbors(return_distance=False)[:, 1:]
    knn_rows = np.repeat(np.arange(st.n_obs), knn_indices.shape[1])
    knn_graph = sp.csr_matrix((np.ones(knn_rows.size), (knn_rows, knn_indices.ravel())), shape=(st.n_obs, st.n_obs))
    knn_graph = knn_graph.maximum(knn_graph.T)
    knn_graph.setdiag(0)
    knn_graph.eliminate_zeros()

    def stage_graph(stage: Path) -> dict[str, Any]:
        sp.save_npz(stage / "visium-lattice-adjacency.npz", lattice)
        sp.save_npz(stage / "pixel-knn6-sensitivity-adjacency.npz", knn_graph)
        pd.DataFrame(
            {
                "unit_id": st.obs_names.astype(str),
                "lattice_degree": lattice_degrees.astype(int),
                "connected_component": component_labels.astype(int),
            }
        ).to_csv(stage / "graph-unit-summary.tsv", sep="\t", index=False)
        summary = {
            "primary_graph": "Visium vendor array hex-lattice",
            "primary_neighbor_deltas": [[0, -2], [0, 2], [-1, -1], [-1, 1], [1, -1], [1, 1]],
            "coordinate_unit": "array row/column",
            "nodes": int(lattice.shape[0]),
            "undirected_edges": int(lattice.nnz // 2),
            "connected_components": int(component_number),
            "degree_min": int(lattice_degrees.min()),
            "degree_median": float(np.median(lattice_degrees)),
            "degree_max": int(lattice_degrees.max()),
            "sensitivity_graph": "symmetric fullres-pixel 6-nearest-neighbor",
        }
        write_json(stage / "graph-summary.json", summary)
        context["graph"] = summary
        return {"ok": bool(component_number == 1 and lattice.nnz > 0), **summary}

    stage_path, checkpoints["S50_SPATIAL_GRAPH"] = promote_stage(
        run_root,
        "S50_SPATIAL_GRAPH",
        stage_graph,
        {"S40_PREPROCESS": checkpoints["S40_PREPROCESS"]["payload_tree_sha256"]},
    )

    seed = int(config["parameters"]["seed"])
    cluster_space = pcs[:, : min(10, pcs.shape[1])]
    cluster_scores: list[dict[str, Any]] = []
    candidates: dict[int, np.ndarray] = {}
    for cluster_count in range(int(config["parameters"]["cluster_k_min"]), int(config["parameters"]["cluster_k_max"]) + 1):
        model = KMeans(n_clusters=cluster_count, random_state=seed, n_init=20)
        labels = model.fit_predict(cluster_space)
        score = float(silhouette_score(cluster_space, labels))
        candidates[cluster_count] = labels
        cluster_scores.append({"k": cluster_count, "silhouette": score})
    selected_k = max(cluster_scores, key=lambda item: (item["silhouette"], -item["k"]))["k"]
    clusters = candidates[selected_k]
    lattice_weights = row_normalize(lattice)
    knn_weights = row_normalize(knn_graph)
    moran_lattice = moran_vector(selected_dense.astype(float), lattice_weights)
    moran_knn = moran_vector(selected_dense.astype(float), knn_weights)
    ranking = pd.DataFrame(
        {
            "gene": st.var_names[selected_indices].astype(str),
            "moran_i_lattice": moran_lattice,
            "moran_i_knn6_sensitivity": moran_knn,
            "variance_log1p_cpm": variance[selected_indices],
            "detected_spots": detected_spots[selected_indices],
        }
    ).sort_values(["moran_i_lattice", "gene"], ascending=[False, True], kind="stable")
    top_genes = ranking.head(int(config["parameters"]["top_spatial_gene_count"]))["gene"].tolist()
    gene_to_selected = {str(st.var_names[index]): position for position, index in enumerate(selected_indices)}

    def stage_discovery(stage: Path) -> dict[str, Any]:
        pd.DataFrame(cluster_scores).to_csv(stage / "cluster-k-sensitivity.tsv", sep="\t", index=False)
        pd.DataFrame({"unit_id": st.obs_names.astype(str), "expression_cluster": clusters.astype(str)}).to_csv(
            stage / "expression-clusters.tsv", sep="\t", index=False
        )
        ranking.to_csv(stage / "exploratory-moran-ranking.tsv", sep="\t", index=False)
        pd.DataFrame({"rank": np.arange(1, len(top_genes) + 1), "gene": top_genes}).to_csv(
            stage / "selected-spatial-genes.tsv", sep="\t", index=False
        )
        sensitivity = float(spearmanr(moran_lattice, moran_knn, nan_policy="omit").statistic)
        summary = {
            "cluster_method": "expression-only KMeans on centered PCA",
            "selected_k": int(selected_k),
            "selection_rule": "maximum silhouette across frozen k range",
            "spatial_domain_claim": False,
            "moran_role": "descriptive within-section ranking only",
            "permutation_p_values": False,
            "multiple_testing_fdr": False,
            "top_spatial_genes": top_genes,
            "lattice_knn_moran_spearman": sensitivity,
        }
        write_json(stage / "core-discovery-summary.json", summary)
        context["discovery"] = summary
        return {"ok": bool(len(np.unique(clusters)) == selected_k and len(top_genes) > 0), **summary}

    stage_path, checkpoints["S60_CORE_DISCOVERY"] = promote_stage(
        run_root,
        "S60_CORE_DISCOVERY",
        stage_discovery,
        {"S50_SPATIAL_GRAPH": checkpoints["S50_SPATIAL_GRAPH"]["payload_tree_sha256"]},
    )

    def stage_mapping(stage: Path) -> dict[str, Any]:
        probe_path_value = config["model_branch"].get("probe_path")
        probe = None
        if probe_path_value:
            probe_path = Path(probe_path_value).resolve()
            if not probe_path.is_file():
                raise PipelineError("model_environment_probe_missing")
            probe = json.loads(probe_path.read_text(encoding="utf-8"))
            shutil.copy2(probe_path, stage / "model-environment-probe.json")
        status = {
            "requested_method": "Spotiphy deconvolution/decomposition",
            "status": "blocked",
            "reason": config["model_branch"]["blocked_reason"],
            "attempted": False,
            "substitution_performed": False,
            "deconvolution_completed": False,
            "cell_type_abundance_artifact": None,
            "scientifically_non_equivalent_fallback_used": False,
            "required_future_environment": config["model_branch"]["required_environment"],
            "source_commit": config["governance"].get("repository_commit"),
            "environment_probe": probe,
        }
        write_json(stage / "spotiphy-model-status.json", status)
        return {"ok": True, "branch_status": "blocked", "main_descriptive_core_may_continue": True}

    stage_path, checkpoints["S70_COMPOSITION_MAPPING"] = promote_stage(
        run_root,
        "S70_COMPOSITION_MAPPING",
        stage_mapping,
        {"S60_CORE_DISCOVERY": checkpoints["S60_CORE_DISCOVERY"]["payload_tree_sha256"]},
    )

    unflagged = ~qc_flag
    if np.count_nonzero(unflagged) > selected_k:
        subset_model = KMeans(n_clusters=selected_k, random_state=seed, n_init=20).fit(cluster_space[unflagged])
        subset_labels_all = subset_model.predict(cluster_space)
        qc_cluster_ari = float(adjusted_rand_score(clusters, subset_labels_all))
    else:
        qc_cluster_ari = float("nan")

    def stage_inference(stage: Path) -> dict[str, Any]:
        qa = {
            "estimand": "descriptive spatial expression structure within one mouse-brain Visium section",
            "assay_unit": "spot",
            "spatial_unit": "spot",
            "sampling_unit": "section",
            "inference_unit": "animal/specimen",
            "independent_inference_units_available": 1,
            "inferential_tests_performed": False,
            "population_inference_allowed": False,
            "pseudoreplication_guard": "spots are not independent animals",
            "qc_flag_sensitivity_cluster_ari": qc_cluster_ari,
            "graph_sensitivity_moran_spearman": context["discovery"]["lattice_knn_moran_spearman"],
            "deconvolution_completed": False,
            "claim_ceiling": "coordinate-faithful descriptive spot-level patterns for one section",
        }
        write_json(stage / "estimand-and-claim-boundary.json", qa)
        return {"ok": True, "inferential_tests_performed": False, "independent_inference_units": 1}

    stage_path, checkpoints["S90_INFERENCE_QA"] = promote_stage(
        run_root,
        "S90_INFERENCE_QA",
        stage_inference,
        {"S70_COMPOSITION_MAPPING": checkpoints["S70_COMPOSITION_MAPPING"]["payload_tree_sha256"]},
    )

    # Materialize non-figure result artifacts before the native-review gate.
    result_copies = {
        "S10_INGEST/gene-overlap.tsv": "05_results/tables/gene-overlap.tsv",
        "S20_COORD_IMAGE_QC/coordinates.tsv": "05_results/tables/coordinates.tsv",
        "S30_UNIT_QC/spot-qc.tsv": "05_results/tables/spot-qc.tsv",
        "S30_UNIT_QC/attrition.tsv": "05_results/tables/attrition.tsv",
        "S40_PREPROCESS/selected-features.tsv": "05_results/tables/selected-features.tsv",
        "S40_PREPROCESS/pca-embedding.tsv": "05_results/tables/pca-embedding.tsv",
        "S40_PREPROCESS/log1p-cpm-selected-features.npz": "05_results/objects/log1p-cpm-selected-features.npz",
        "S50_SPATIAL_GRAPH/visium-lattice-adjacency.npz": "05_results/objects/visium-lattice-adjacency.npz",
        "S60_CORE_DISCOVERY/expression-clusters.tsv": "05_results/tables/expression-clusters.tsv",
        "S60_CORE_DISCOVERY/exploratory-moran-ranking.tsv": "05_results/tables/exploratory-moran-ranking.tsv",
        "S60_CORE_DISCOVERY/selected-spatial-genes.tsv": "05_results/tables/selected-spatial-genes.tsv",
        "S70_COMPOSITION_MAPPING/spotiphy-model-status.json": "05_results/tables/spotiphy-model-status.json",
        "S90_INFERENCE_QA/estimand-and-claim-boundary.json": "05_results/tables/estimand-and-claim-boundary.json",
    }
    for source_relative, target_relative in result_copies.items():
        shutil.copy2(run_root / "04_intermediate" / source_relative, run_root / target_relative)

    visual_staging = run_root / "_staging" / "S95_VISUALIZE_INTERPRET-attempt1"
    visual_staging.mkdir(parents=True, exist_ok=False)
    values_by_gene = {gene: selected_dense[:, gene_to_selected[gene]] for gene in top_genes}

    def draw_qc_overlay(size: tuple[float, float], dpi: int) -> plt.Figure:
        figure, axes = plt.subplots(2, 2, figsize=size, constrained_layout=True)
        panels = [
            (np.log10(total_counts + 1), "log10(total counts + 1)", "viridis"),
            (detected, "Detected genes", "magma"),
            (pct_mt, "Mitochondrial fraction (%)", "cividis"),
            (qc_flag.astype(int), "Robust QC flags (flag-only)", "coolwarm"),
        ]
        for axis, (values, title, cmap) in zip(axes.ravel(), panels):
            artist = _spatial_panel(axis, image, coords, values, title=title, cmap=cmap)
            figure.colorbar(artist, ax=axis, fraction=0.035, pad=0.02)
        figure.suptitle("Visium spot QC on the registered full-resolution H&E frame", fontsize=13, weight="bold")
        return figure

    def draw_clusters(size: tuple[float, float], dpi: int) -> plt.Figure:
        figure, axes = plt.subplots(1, 2, figsize=size, constrained_layout=True)
        for value in np.unique(clusters):
            mask = clusters == value
            axes[0].scatter(pcs[mask, 0], pcs[mask, 1], s=12, label=str(value), alpha=0.8, linewidths=0)
        axes[0].set_xlabel("PC1")
        axes[0].set_ylabel("PC2")
        axes[0].set_title("KMeans clusters\n(expression only)")
        axes[0].legend(title="Cluster", frameon=False, fontsize=7)
        _spatial_panel(
            axes[1],
            image,
            coords,
            clusters,
            title="Tissue-coordinate overlay\n(same expression clusters)",
            categorical=True,
        )
        figure.suptitle("Expression structure (not a spatial-domain or cell-type call)", fontsize=13, weight="bold")
        return figure

    def draw_genes(size: tuple[float, float], dpi: int) -> plt.Figure:
        rows_number = int(math.ceil(len(top_genes) / 2))
        figure, axes = plt.subplots(rows_number, 2, figsize=size, constrained_layout=True)
        axes_array = np.atleast_1d(axes).ravel()
        for axis, gene in zip(axes_array, top_genes):
            artist = _spatial_panel(
                axis,
                image,
                coords,
                values_by_gene[gene],
                title=f"{gene}: log1p library-normalized expression",
                cmap="viridis",
            )
            figure.colorbar(artist, ax=axis, fraction=0.035, pad=0.02)
        for axis in axes_array[len(top_genes) :]:
            axis.set_visible(False)
        figure.suptitle("Top descriptive Moran-I rankings (no permutation p-value or FDR)", fontsize=13, weight="bold")
        return figure

    def draw_qc_distributions(size: tuple[float, float], dpi: int) -> plt.Figure:
        figure, axes = plt.subplots(2, 2, figsize=size, constrained_layout=True)
        axes[0, 0].hist(np.log10(total_counts + 1), bins=30, color="#4477AA", edgecolor="white")
        axes[0, 0].set_xlabel("log10(total counts + 1)")
        axes[0, 0].set_ylabel("Spots")
        axes[0, 1].hist(detected, bins=30, color="#66CCEE", edgecolor="white")
        axes[0, 1].set_xlabel("Detected genes")
        axes[0, 1].set_ylabel("Spots")
        axes[1, 0].hist(pct_mt, bins=30, color="#AA3377", edgecolor="white")
        axes[1, 0].set_xlabel("Mitochondrial fraction (%)")
        axes[1, 0].set_ylabel("Spots")
        artist = axes[1, 1].scatter(total_counts, detected, c=pct_mt, cmap="cividis", s=16, alpha=0.8, linewidths=0)
        axes[1, 1].set_xscale("log")
        axes[1, 1].set_xlabel("Total counts")
        axes[1, 1].set_ylabel("Detected genes")
        figure.colorbar(artist, ax=axes[1, 1], label="Mitochondrial fraction (%)")
        figure.suptitle("Platform-aware Visium spot QC distributions", fontsize=13, weight="bold")
        return figure

    figure_records = [
        figure_pair("spatial-qc-overlay", visual_staging, draw_qc_overlay, (12.0, 10.0), (7.2, 6.2)),
        figure_pair("expression-cluster-map", visual_staging, draw_clusters, (12.0, 5.8), (7.2, 3.8)),
        figure_pair("exploratory-spatial-genes", visual_staging, draw_genes, (12.0, 10.0), (7.2, 6.2)),
        figure_pair("qc-distributions", visual_staging, draw_qc_distributions, (11.0, 8.5), (7.2, 5.6)),
    ]
    write_json(visual_staging / "figures-manifest.json", {"figures": figure_records})
    write_json(
        visual_staging / "review-template.json",
        {
            "review_state": "awaiting-native-review",
            "required_tool": "native_local_image_view",
            "required_pairs": [
                {
                    "figure_id": item["figure_id"],
                    "original_sha256": item["original"]["sha256"],
                    "final_sha256": item["final"]["sha256"],
                    "original_path": str(visual_staging / item["original"]["relative_path"]),
                    "final_path": str(visual_staging / item["final"]["relative_path"]),
                }
                for item in figure_records
            ],
            "allowed_decisions": ["PASS_WITH_MINOR_FINDINGS", "BLOCKED"],
        },
    )
    write_text(
        visual_staging / "reports" / "FIGURE_NOTES.md",
        """# Figure notes

Native visual review: pending.

## `spatial-qc-overlay.png`

- Question: Are major spot-QC metrics spatially patterned on the registered full-resolution H&E frame?
- Data/statistical unit: 685 measured Visium spots in one section; independent inferential units available = 1.
- Directly visible after review: spatial distribution of total counts, detected genes, mitochondrial fraction and flag-only robust QC labels.
- Supports: descriptive within-section localization of QC metrics after the identity transform passes quantitative and native review.
- Does not support: excluding tissue regions as artifacts, cohort effects, cell-type identity, histologic diagnosis or causality.
- Reproduction class: scientific reimplementation from the real tutorial data, not an exact Spotiphy-paper panel.

## `expression-cluster-map.png`

- Question: How does expression-only structure map to tissue coordinates?
- Data/statistical unit: Visium spots; one section; no population inference.
- Supports: descriptive co-localization of deterministic expression clusters and tissue coordinates.
- Does not support: cell types, spatial domains, anatomical labels, replicated effects or biological mechanism.
- Reproduction class: new descriptive teaching figure.

## `exploratory-spatial-genes.png`

- Question: Which selected genes have high descriptive spatial autocorrelation on the declared Visium lattice?
- Data/statistical unit: genes ranked within one section; measured unit = spot.
- Supports: visible within-section expression patterns for the top descriptive Moran-I rankings.
- Does not support: confirmatory SVG significance, FDR-controlled discoveries, cell-intrinsic regulation, replicated anatomy or causality; no permutation p-values were computed.
- Reproduction class: new descriptive teaching figure.

## `qc-distributions.png`

- Question: What are the marginal and joint distributions of spot QC metrics?
- Data/statistical unit: 685 spots in one section.
- Supports: descriptive distribution and covariance of counts, detected genes and mitochondrial fraction.
- Does not support: universal QC cutoffs or treating spots as independent biological replicates.
- Reproduction class: new descriptive teaching figure.
""",
    )
    write_text(
        visual_staging / "reports" / "QA_REPORT.md",
        f"""# QA report

Status: AWAITING_NATIVE_VISUAL_REVIEW

- Input integrity: PASS ({len(input_evidence)} read-only sources hash-bound).
- Matrix integrity: PASS ({st.n_obs} x {st.n_vars}; integer, non-negative sparse counts).
- Platform/unit contract: PASS (10x Visium capture spots; one section).
- Coordinate/image quantitative contract: PASS (identity correlation {context['transform']['identity_bilinear_correlation']:.6f}; full-resolution bounds valid).
- Spot QC: PASS_WITH_BOUNDARY (robust flags are descriptive and not automatic exclusion).
- Spatial graph: PASS (vendor array hex-lattice; connected components = {context['graph']['connected_components']}).
- Core discovery: PASS_WITH_BOUNDARY (expression-only clusters and descriptive Moran-I ranking; no inferential SVG test).
- Spotiphy deconvolution/decomposition: BLOCKED, not attempted, no substitution.
- Population inference: NOT_APPLICABLE/BLOCKED by one independent section.
- Native visual review: PENDING. Delivery remains incomplete until both original and final-size figures are opened and hash-bound.
""",
    )
    write_json(
        run_root / "manifest" / "run_manifest.json",
        {
            "schema_version": "1.0",
            "run_id": config["run_id"],
            "case_id": config["case_id"],
            "mode": "run",
            "state": "NATIVE_VISUAL_REVIEW",
            "environment_lock_hash": config["environment"]["lock_hash"],
            "stages": [
                {"stage_id": stage_id, "status": "checkpointed", "payload_tree_sha256": checkpoint["payload_tree_sha256"]}
                for stage_id, checkpoint in checkpoints.items()
            ]
            + [{"stage_id": "S95_VISUALIZE_INTERPRET", "status": "awaiting-native-review"}],
            "latest_valid_checkpoint": "S90_INFERENCE_QA",
            "source_hashes": {item["locator_ref"]: item["sha256"] for item in input_evidence},
            "runner_version": RUNNER_VERSION,
        },
    )
    result = {
        "ok": True,
        "state": "NATIVE_VISUAL_REVIEW",
        "run_root": str(run_root),
        "run_id": config["run_id"],
        "figure_count": len(figure_records),
        "review_template": str(visual_staging / "review-template.json"),
        "review_pairs": json.loads((visual_staging / "review-template.json").read_text(encoding="utf-8"))["required_pairs"],
        "completed_stages": list(checkpoints),
        "blocked_branches": ["Spotiphy deconvolution/decomposition"],
    }
    print(stable_json(result), end="")
    return result


def verify_resume(config_path: Path) -> dict[str, Any]:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    run_root = Path(config["run_root"]).resolve()
    manifest_path = run_root / "manifest" / "run_manifest.json"
    ledger_path = run_root / "manifest" / "artifact_ledger.jsonl"
    if not manifest_path.is_file() or not ledger_path.is_file():
        raise PipelineError("delivered_manifest_or_ledger_missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("state") != "DELIVERED" or manifest.get("environment_lock_hash") != config["environment"]["lock_hash"]:
        raise PipelineError("resume_state_or_environment_mismatch")
    source_hashes = manifest.get("source_hashes", {})
    source_checks = 0
    for evidence in config["inputs"].values():
        source = Path(evidence["path"]).resolve()
        if source_hashes.get(evidence["locator_ref"]) != sha256_file(source):
            raise PipelineError(f"resume_input_hash_mismatch:{evidence['locator_ref']}")
        source_checks += 1
    entries = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    artifact_checks = 0
    for entry in entries:
        path = run_root / entry["relative_path"]
        if not path.is_file() or path.stat().st_size != entry["size_bytes"] or sha256_file(path) != entry["sha256"]:
            raise PipelineError(f"resume_artifact_mismatch:{entry['relative_path']}")
        artifact_checks += 1
    result = {
        "ok": True,
        "state": "DELIVERED",
        "run_root": str(run_root),
        "restored_from_cache": True,
        "read_only": True,
        "source_hashes_revalidated": source_checks,
        "artifacts_revalidated": artifact_checks,
        "checkpoints_revalidated": [item["stage_id"] for item in manifest.get("stages", [])],
        "latest_valid_checkpoint": manifest.get("latest_valid_checkpoint"),
    }
    print(stable_json(result), end="")
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.resume:
            verify_resume(args.config.resolve())
        else:
            run_fresh(args.config.resolve())
    except Exception as exc:
        print(stable_json({"ok": False, "error": f"{type(exc).__name__}:{exc}"}), end="", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
