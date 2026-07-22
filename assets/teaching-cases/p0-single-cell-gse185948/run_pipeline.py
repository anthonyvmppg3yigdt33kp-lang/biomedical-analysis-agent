#!/usr/bin/env python3
"""Memory-conscious, donor-preserving teaching workflow for GSE185948 snRNA-seq.

The workflow intentionally performs descriptive atlas construction only. It uses
distribution-derived per-donor QC, Scrublet per donor, an unintegrated
donor-preserving representation with diagnostics, evidence-bounded coarse
annotation, and no case-control inference.
"""

from __future__ import annotations

import argparse
import ctypes
import gc
import hashlib
import json
import math
import os
import platform
import sys
from pathlib import Path

THREAD_ENV_VARS = (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "NUMBA_NUM_THREADS",
)
for thread_variable in THREAD_ENV_VARS:
    # Process-local override only. The launcher performs the same override
    # before Python starts; assigning here prevents a high-thread host value
    # from surviving if the script is invoked directly.
    os.environ[thread_variable] = "2"

import anndata as ad
import h5py
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mtick
import numpy as np
import pandas as pd
import scanpy as sc
import scipy
import scipy.sparse as sp
import sklearn
from sklearn.mixture import GaussianMixture
from sklearn.neighbors import NearestNeighbors


SEED = 20260719
DONOR_HVG_LIMIT = 800
SELECTED_FEATURE_LIMIT = 4200
SCRUBLET_FEATURE_LIMIT = 8000
MIN_SYSTEM_AVAILABLE_GB_AT_START = 2.0
MIN_SYSTEM_AVAILABLE_GB_BEFORE_DONOR = 1.5
MIN_SYSTEM_AVAILABLE_GB_BEFORE_SCRUBLET = 1.0
FIGURE_SCOPE_SUFFIX = ""

MARKER_PANELS = {
    "Proximal tubule": ["LRP2", "CUBN", "SLC34A1", "ALDOB", "SLC5A2"],
    "Loop of Henle": ["SLC12A1", "UMOD", "CLDN10", "KCNJ1"],
    "Distal convoluted tubule": ["SLC12A3", "PVALB", "TRPM6", "CALB1"],
    "Collecting duct principal": ["AQP2", "FXYD4", "SCNN1G", "KCNJ1"],
    "Collecting duct intercalated": ["ATP6V1B1", "ATP6V0D2", "FOXI1", "SLC4A1"],
    "Podocyte": ["NPHS1", "NPHS2", "PODXL", "WT1"],
    "Endothelial": ["EMCN", "KDR", "PECAM1", "VWF", "RAMP2"],
    "Fibroblast/pericyte": ["COL1A1", "COL1A2", "DCN", "COL3A1", "RGS5", "PDGFRB"],
    "Immune": ["PTPRC", "LST1", "TYROBP", "CD3D", "NKG7"],
    "Urothelial/epithelial": ["KRT8", "KRT18", "KRT19", "CLDN1", "KRT7"],
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def stable_frame_to_tsv(frame: pd.DataFrame, path: Path, *, index: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, sep="\t", index=index, float_format="%.8g", lineterminator="\n")


def require_integer_sparse(matrix: sp.spmatrix) -> bool:
    if matrix.nnz == 0:
        return True
    data = np.asarray(matrix.data)
    return bool(np.isfinite(data).all() and np.equal(data, np.floor(data)).all())


def decode_h5_strings(values: np.ndarray) -> list[str]:
    return [
        value.decode("utf-8") if isinstance(value, (bytes, np.bytes_)) else str(value)
        for value in values
    ]


def deterministic_barcode_indices(path: Path, maximum: int, seed: int) -> np.ndarray:
    """Select barcode columns without reading the sparse count payload."""
    with h5py.File(path, "r") as handle:
        n_barcodes = int(handle["matrix"]["shape"][1])
    take = min(int(maximum), n_barcodes)
    rng = np.random.default_rng(seed)
    return np.sort(rng.choice(n_barcodes, size=take, replace=False).astype(np.int64))


def read_sampled_matrix(path: Path, barcode_indices: np.ndarray) -> ad.AnnData:
    """Read selected 10x H5 CSC columns; never materialize the full count matrix."""
    selected = np.asarray(barcode_indices, dtype=np.int64)
    if selected.ndim != 1 or selected.size == 0 or np.any(np.diff(selected) <= 0):
        raise ValueError("Sampled barcode indices must be non-empty, unique, and strictly increasing")
    with h5py.File(path, "r") as handle:
        matrix_group = handle["matrix"]
        n_features, n_barcodes = (int(value) for value in matrix_group["shape"][:])
        if int(selected[-1]) >= n_barcodes:
            raise IndexError("Sampled barcode index exceeds matrix shape")
        full_indptr = matrix_group["indptr"][:]
        data_parts: list[np.ndarray] = []
        index_parts: list[np.ndarray] = []
        sampled_indptr = np.zeros(selected.size + 1, dtype=full_indptr.dtype)
        cursor = 0
        for output_column, source_column in enumerate(selected):
            start = int(full_indptr[source_column])
            stop = int(full_indptr[source_column + 1])
            data_parts.append(matrix_group["data"][start:stop])
            index_parts.append(matrix_group["indices"][start:stop])
            cursor += stop - start
            sampled_indptr[output_column + 1] = cursor
        data = np.concatenate(data_parts) if data_parts else np.array([], dtype=matrix_group["data"].dtype)
        indices = np.concatenate(index_parts) if index_parts else np.array([], dtype=matrix_group["indices"].dtype)
        barcodes = decode_h5_strings(matrix_group["barcodes"][selected])
        feature_group = matrix_group["features"]
        feature_names = decode_h5_strings(feature_group["name"][:])
        feature_ids = decode_h5_strings(feature_group["id"][:])
        feature_types = decode_h5_strings(feature_group["feature_type"][:])
        genomes = (
            decode_h5_strings(feature_group["genome"][:])
            if "genome" in feature_group
            else [""] * n_features
        )
    feature_by_barcode = sp.csc_matrix(
        (data, indices, sampled_indptr),
        shape=(n_features, selected.size),
    )
    value = ad.AnnData(feature_by_barcode.T.tocsr())
    value.obs_names = pd.Index(barcodes, dtype="object")
    value.var_names = pd.Index(feature_names, dtype="object")
    value.var["gene_ids"] = feature_ids
    value.var["feature_types"] = feature_types
    value.var["genome"] = genomes
    gex_mask = value.var["feature_types"].astype(str).to_numpy() == "Gene Expression"
    if not bool(np.all(gex_mask)):
        value = value[:, gex_mask].copy()
    value.var_names_make_unique()
    value.obs_names_make_unique()
    return value


def read_matrix(path: Path, barcode_indices: np.ndarray | None = None) -> ad.AnnData:
    if barcode_indices is not None:
        return read_sampled_matrix(path, barcode_indices)
    value = sc.read_10x_h5(path, genome=None, gex_only=True)
    value.var_names_make_unique()
    value.obs_names_make_unique()
    if not sp.issparse(value.X):
        value.X = sp.csr_matrix(value.X)
    else:
        value.X = value.X.tocsr()
    return value


def distribution_thresholds(frame: pd.DataFrame) -> dict[str, float]:
    # Broad donor-specific 0.5%/99.5% empirical bounds avoid whole-cell canned cutoffs.
    return {
        "total_counts_low": float(frame["total_counts"].quantile(0.005)),
        "total_counts_high": float(frame["total_counts"].quantile(0.995)),
        "n_genes_low": float(frame["n_genes_by_counts"].quantile(0.005)),
        "n_genes_high": float(frame["n_genes_by_counts"].quantile(0.995)),
        "complexity_low": float(frame["complexity"].quantile(0.005)),
        "complexity_high": float(frame["complexity"].quantile(0.995)),
    }


def threshold_mask(frame: pd.DataFrame, thresholds: dict[str, float]) -> np.ndarray:
    return (
        frame["total_counts"].between(thresholds["total_counts_low"], thresholds["total_counts_high"], inclusive="both")
        & frame["n_genes_by_counts"].between(thresholds["n_genes_low"], thresholds["n_genes_high"], inclusive="both")
        & frame["complexity"].between(thresholds["complexity_low"], thresholds["complexity_high"], inclusive="both")
    ).to_numpy(dtype=bool)


def memory_snapshot(label: str) -> dict[str, object]:
    class MemoryStatusEx(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    status = MemoryStatusEx()
    status.dwLength = ctypes.sizeof(MemoryStatusEx)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MemoryStatusEx)]
    kernel32.GlobalMemoryStatusEx.restype = ctypes.c_bool
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    psapi.GetProcessMemoryInfo.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ProcessMemoryCounters),
        ctypes.c_ulong,
    ]
    psapi.GetProcessMemoryInfo.restype = ctypes.c_bool
    if not kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise OSError(ctypes.get_last_error(), "GlobalMemoryStatusEx failed")
    counters = ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(ProcessMemoryCounters)
    process = kernel32.GetCurrentProcess()
    if not psapi.GetProcessMemoryInfo(process, ctypes.byref(counters), counters.cb):
        raise OSError(ctypes.get_last_error(), "GetProcessMemoryInfo failed")
    return {
        "label": label,
        "process_rss_gb": float(counters.WorkingSetSize / (1024 ** 3)),
        "process_peak_working_set_gb": float(counters.PeakWorkingSetSize / (1024 ** 3)),
        "process_pagefile_gb": float(counters.PagefileUsage / (1024 ** 3)),
        "process_peak_pagefile_gb": float(counters.PeakPagefileUsage / (1024 ** 3)),
        "system_available_gb": float(status.ullAvailPhys / (1024 ** 3)),
        "system_percent_used": float(status.dwMemoryLoad),
    }


def require_available_memory(label: str, minimum_gb: float, snapshots: list[dict[str, object]]) -> None:
    snapshot = memory_snapshot(label)
    snapshots.append(snapshot)
    if float(snapshot["system_available_gb"]) < minimum_gb:
        raise MemoryError(
            f"Memory guard failed at {label}: available={snapshot['system_available_gb']:.3f} GB, "
            f"required>={minimum_gb:.3f} GB. Fail-closed before uncontrolled paging."
        )


def scrublet_feature_subset(matrix: sp.spmatrix, limit: int = SCRUBLET_FEATURE_LIMIT) -> tuple[sp.csr_matrix, int]:
    matrix = matrix.tocsr()
    detected = np.asarray((matrix > 0).sum(axis=0)).ravel()
    eligible = np.flatnonzero(detected >= 3)
    if eligible.size <= limit:
        return matrix[:, eligible].tocsr(), int(eligible.size)
    eligible_matrix = matrix[:, eligible]
    means = np.asarray(eligible_matrix.mean(axis=0)).ravel()
    squared_means = np.asarray(eligible_matrix.power(2).mean(axis=0)).ravel()
    variances = np.maximum(squared_means - means ** 2, 0)
    dispersion = variances / np.maximum(means, 1e-8)
    selected_local = np.argsort(-dispersion, kind="stable")[:limit]
    selected = eligible[selected_local]
    return matrix[:, selected].tocsr(), int(selected.size)


def run_scrublet(matrix: sp.spmatrix, seed: int) -> tuple[np.ndarray, np.ndarray, dict[str, object]]:
    expected_rate = 0.06
    controlled_matrix, input_features = scrublet_feature_subset(matrix)
    temporary = ad.AnnData(controlled_matrix)
    sc.pp.scrublet(
        temporary,
        expected_doublet_rate=expected_rate,
        sim_doublet_ratio=1.0,
        n_prin_comps=20,
        use_approx_neighbors=False,
        threshold=float("inf"),
        random_state=seed,
        verbose=False,
    )
    scores = temporary.obs["doublet_score"].to_numpy(dtype=float)
    if not np.isfinite(scores).all():
        raise RuntimeError("Scanpy Scrublet produced non-finite scores")
    clipped = np.clip(scores, 1e-6, 1 - 1e-6)
    transformed = np.log(clipped / (1 - clipped)).reshape(-1, 1)
    mixture = GaussianMixture(n_components=2, covariance_type="full", random_state=seed, n_init=5)
    mixture.fit(transformed)
    component_means = mixture.means_.ravel()
    high_component = int(np.argmax(component_means))
    probabilities = mixture.predict_proba(transformed)[:, high_component]
    predicted = probabilities >= 0.5
    predicted_loose = probabilities >= 0.25
    predicted_strict = probabilities >= 0.75
    component_sd = np.sqrt(mixture.covariances_.reshape(-1))
    separation = float(abs(component_means[1] - component_means[0]) / max(np.sqrt(np.mean(component_sd ** 2)), 1e-8))
    predicted_fraction = float(np.mean(predicted))
    reliability_reasons = []
    if separation < 0.5:
        reliability_reasons.append(f"mixture separation {separation:.3f} < 0.5")
    if not 0.005 <= predicted_fraction <= 0.20:
        reliability_reasons.append(f"candidate predicted fraction {predicted_fraction:.4f} outside [0.005, 0.20]")
    filter_eligible = not reliability_reasons
    minimum_positive_score = float(np.min(scores[predicted])) if bool(np.any(predicted)) else None
    report = {
        "status": "scored-filter-eligible" if filter_eligible else "scored-filter-inconclusive",
        "scoring_completed": True,
        "scores_finite": True,
        "filter_eligible": filter_eligible,
        "filter_reliability_reasons": reliability_reasons,
        "method": "Scanpy bundled Scrublet scoring per donor plus two-component Gaussian-mixture posterior threshold",
        "expected_doublet_rate": expected_rate,
        "expected_doublet_rate_status": "fixed operational prior; donor-specific 10x chemistry/loading target was unavailable, so this is not a calibrated prevalence estimate",
        "expected_rate_sensitivity": "not rerun at alternative rate priors; posterior cutoffs 0.25/0.50/0.75 are reported, and all labels remain model-based screening labels",
        "sim_doublet_ratio": 1.0,
        "n_prin_comps": 20,
        "memory_control": "donor-serial; deterministic overdispersion prefilter capped at 8,000 genes before Scrublet internal filtering",
        "input_features_after_memory_prefilter": input_features,
        "score_cutoff_available": False,
        "minimum_score_among_posterior_positive": minimum_positive_score,
        "minimum_score_semantics": "descriptive minimum only, not a reusable decision threshold; with heteroscedastic Gaussian components, posterior>=0.5 need not be equivalent to one monotone score cutoff",
        "decision_basis": "posterior probability >=0.5 for the higher-mean component of a seeded heteroscedastic two-component Gaussian mixture on logit Scrublet scores",
        "seed": seed,
        "posterior_cutoff": 0.5,
        "mixture_standardized_separation": separation,
        "candidate_predicted_count": int(np.sum(predicted)),
        "candidate_predicted_fraction": predicted_fraction,
        "score_median": float(np.median(scores)),
        "score_q95": float(np.quantile(scores, 0.95)),
        "threshold_sensitivity": {
            "posterior_cutoff_0.25_count": int(np.sum(predicted_loose)),
            "posterior_cutoff_0.25_fraction": float(np.mean(predicted_loose)),
            "posterior_cutoff_0.50_count": int(np.sum(predicted)),
            "posterior_cutoff_0.50_fraction": predicted_fraction,
            "posterior_cutoff_0.75_count": int(np.sum(predicted_strict)),
            "posterior_cutoff_0.75_fraction": float(np.mean(predicted_strict)),
            "use_in_primary_filter": "candidate posterior cutoff 0.50 is eligible only if every donor passes reliability gates; otherwise all donors receive no doublet filtering",
        },
        "truth_status": "predicted doublet labels are model-based screening labels, not ground truth",
    }
    return np.asarray(scores, dtype=float), np.asarray(predicted, dtype=bool), report


def decide_doublet_filter_policy(reports: list[dict[str, object]]) -> dict[str, object]:
    scoring_all_completed = bool(reports) and all(bool(item.get("scoring_completed")) for item in reports)
    all_donors_filter_eligible = scoring_all_completed and all(
        bool(item.get("filter_eligible")) for item in reports
    )
    filtering_applied = bool(all_donors_filter_eligible)
    reason = (
        "Every donor completed finite Scrublet scoring and passed the predeclared GMM reliability gates; candidate labels were applied consistently to all donors."
        if filtering_applied
        else "At least one donor lacked an eligible GMM filtering decision; candidate labels were applied to no donors to prevent donor-specific preprocessing bias."
    )
    return {
        "scoring_all_completed": scoring_all_completed,
        "all_donors_filter_eligible": all_donors_filter_eligible,
        "filtering_applied": filtering_applied,
        "doublet_cleared": filtering_applied,
        "filter_policy": "all-donors-or-none",
        "reason": reason,
    }


def normalized_entropy(values: np.ndarray, global_category_count: int) -> float:
    if global_category_count <= 1:
        return 0.0
    counts = np.bincount(values, minlength=global_category_count)
    probabilities = counts[counts > 0] / counts.sum()
    if probabilities.size <= 1:
        return 0.0
    return float(
        -(probabilities * np.log(probabilities)).sum()
        / np.log(global_category_count)
    )


def neighbor_mixing(embedding: np.ndarray, donor_codes: np.ndarray, n_neighbors: int = 15) -> dict[str, float]:
    donor_counts = np.bincount(donor_codes)
    global_category_count = int(np.count_nonzero(donor_counts))
    if global_category_count <= 1:
        raise ValueError("Donor-mixing diagnostics require at least two donors")
    donor_abundance = donor_counts[donor_counts > 0] / donor_counts.sum()
    abundance_same_donor_baseline = float(np.sum(donor_abundance ** 2))
    abundance_entropy_baseline = float(
        -(donor_abundance * np.log(donor_abundance)).sum()
        / np.log(global_category_count)
    )
    model = NearestNeighbors(n_neighbors=n_neighbors + 1, metric="euclidean", n_jobs=2)
    indices = model.fit(embedding).kneighbors(return_distance=False)[:, 1:]
    local = donor_codes[indices]
    same = float(np.mean(local == donor_codes[:, None]))
    entropies = np.fromiter(
        (normalized_entropy(row, global_category_count) for row in local),
        dtype=float,
        count=local.shape[0],
    )
    expected_cross = max(1.0 - abundance_same_donor_baseline, 1e-12)
    return {
        "same_donor_neighbor_fraction": same,
        "abundance_expected_same_donor_fraction": abundance_same_donor_baseline,
        "same_donor_excess_vs_abundance": same - abundance_same_donor_baseline,
        "cross_donor_mixing_ratio_vs_abundance": (1.0 - same) / expected_cross,
        "normalized_donor_entropy_mean": float(entropies.mean()),
        "normalized_donor_entropy_median": float(np.median(entropies)),
        "abundance_expected_normalized_donor_entropy": abundance_entropy_baseline,
        "entropy_normalization_global_donor_count": global_category_count,
        "n_neighbors": int(n_neighbors),
    }


def cluster_summaries(adata: ad.AnnData, cluster_key: str) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    cluster_values = adata.obs[cluster_key].astype(str).to_numpy()
    clusters = sorted(pd.unique(cluster_values), key=lambda value: (len(value), value))
    codes = pd.Categorical(cluster_values, categories=clusters, ordered=True).codes
    membership = sp.csr_matrix(
        (np.ones(adata.n_obs, dtype=np.float32), (codes, np.arange(adata.n_obs))),
        shape=(len(clusters), adata.n_obs),
    )
    matrix = adata.X.tocsr() if sp.issparse(adata.X) else sp.csr_matrix(adata.X)
    sizes = np.bincount(codes, minlength=len(clusters)).astype(float)
    # Only the cluster-by-feature aggregate is materialized (never the full
    # nucleus-by-feature matrix); this bounded dense result is required for
    # marker summaries and remains small by construction.
    means = (membership @ matrix).toarray() / sizes[:, None]
    detected = matrix.copy()
    detected.data = np.ones_like(detected.data, dtype=np.float32)
    fractions = (membership @ detected).toarray() / sizes[:, None]
    mean_frame = pd.DataFrame(means, index=clusters, columns=adata.var_names)
    frac_frame = pd.DataFrame(fractions, index=clusters, columns=adata.var_names)
    overall = np.asarray(matrix.mean(axis=0)).ravel()
    marker_rows: list[dict[str, object]] = []
    for cluster_index, cluster in enumerate(clusters):
        score = means[cluster_index] - overall
        order = np.argsort(-score, kind="stable")[:20]
        for rank, gene_index in enumerate(order, start=1):
            marker_rows.append(
                {
                    "cluster": cluster,
                    "rank": rank,
                    "gene": str(adata.var_names[gene_index]),
                    "mean_log1p": float(means[cluster_index, gene_index]),
                    "fraction_detected": float(fractions[cluster_index, gene_index]),
                    "mean_difference_vs_all": float(score[gene_index]),
                    "evidence_scope": "descriptive nucleus-level cluster marker; not donor-level inference",
                }
            )
    return mean_frame, frac_frame, pd.DataFrame(marker_rows)


def annotate_clusters(
    mean_frame: pd.DataFrame,
    fraction_frame: pd.DataFrame,
    marker_frame: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    available_panels = {
        label: [gene for gene in genes if gene in mean_frame.columns]
        for label, genes in MARKER_PANELS.items()
    }
    gene_union = sorted({gene for genes in available_panels.values() for gene in genes})
    marker_means = mean_frame.loc[:, gene_union]
    gene_sd = marker_means.std(axis=0, ddof=0).replace(0, np.nan)
    marker_z = marker_means.sub(marker_means.mean(axis=0), axis=1).div(gene_sd, axis=1).fillna(0.0)
    panel_scores = pd.DataFrame(index=mean_frame.index)
    for label, genes in available_panels.items():
        panel_scores[label] = marker_z.loc[:, genes].mean(axis=1) if genes else np.nan

    evidence_rows: list[dict[str, object]] = []
    top_gene_lookup = marker_frame.groupby("cluster")["gene"].apply(list).to_dict()
    for cluster in mean_frame.index:
        ranked = panel_scores.loc[cluster].dropna().sort_values(ascending=False)
        top_label = str(ranked.index[0])
        top_score = float(ranked.iloc[0])
        second_score = float(ranked.iloc[1]) if len(ranked) > 1 else float("nan")
        margin = top_score - second_score if math.isfinite(second_score) else float("inf")
        genes = available_panels[top_label]
        top_markers = set(top_gene_lookup.get(cluster, []))
        supporting = [gene for gene in genes if gene in top_markers or float(fraction_frame.loc[cluster, gene]) >= 0.20]
        conflict_label = str(ranked.index[1]) if len(ranked) > 1 else None
        if top_score >= 0.75 and margin >= 0.35 and len(supporting) >= 2:
            assigned = top_label
            confidence = "moderate"
        elif top_score >= 0.45 and margin >= 0.20 and len(supporting) >= 2:
            assigned = f"Ambiguous: {top_label}"
            confidence = "low"
        else:
            assigned = "Unknown/ambiguous"
            confidence = "unresolved"
        evidence_rows.append(
            {
                "cluster": cluster,
                "assigned_label": assigned,
                "confidence": confidence,
                "top_panel": top_label,
                "top_panel_score": top_score,
                "second_panel": conflict_label,
                "second_panel_score": second_score,
                "score_margin": margin,
                "supporting_markers": ";".join(supporting),
                "available_panel_markers": ";".join(genes),
                "decision_rule": "moderate requires score>=0.75, margin>=0.35, >=2 supporting markers; otherwise ambiguous/unknown",
            }
        )
    return pd.DataFrame(evidence_rows), panel_scores


def set_style() -> None:
    matplotlib.rcParams.update(
        {
            "font.size": 9,
            "axes.titlesize": 11,
            "axes.labelsize": 9,
            "legend.fontsize": 8,
            "figure.dpi": 120,
            "savefig.dpi": 220,
            "axes.spines.top": False,
            "axes.spines.right": False,
        }
    )


def save_figure(figure: plt.Figure, output: Path) -> None:
    figure.savefig(
        output,
        dpi=220,
        bbox_inches="tight",
        facecolor="white",
        metadata={"Software": "biomedical-analysis-agent"},
    )
    plt.close(figure)


def plot_qc(qc: pd.DataFrame, thresholds: pd.DataFrame, output: Path) -> None:
    set_style()
    donors = thresholds["donor_id"].tolist()
    palette = plt.get_cmap("tab10")(np.linspace(0, 1, len(donors)))
    figure, axes = plt.subplots(1, 3, figsize=(13.0, 4.4))
    figure.subplots_adjust(top=0.80, wspace=0.30)
    metrics = [
        ("n_genes_by_counts", "Detected genes per nucleus", "n_genes_low", "n_genes_high"),
        ("total_counts", "UMIs per nucleus", "total_counts_low", "total_counts_high"),
        ("pct_counts_mt", "Mitochondrial fraction (%)", None, None),
    ]
    for axis, (metric, label, lower, upper) in zip(axes, metrics):
        values = [qc.loc[qc["donor_id"] == donor, metric].to_numpy() for donor in donors]
        parts = axis.violinplot(values, positions=np.arange(len(donors)), showmedians=True, widths=0.8)
        for body, color in zip(parts["bodies"], palette):
            body.set_facecolor(color)
            body.set_alpha(0.65)
            body.set_edgecolor("#333333")
        if lower:
            low = thresholds.set_index("donor_id").loc[donors, lower].to_numpy()
            high = thresholds.set_index("donor_id").loc[donors, upper].to_numpy()
            axis.scatter(np.arange(len(donors)), low, marker="_", s=120, color="#b2182b", label="empirical 0.5/99.5% bounds")
            axis.scatter(np.arange(len(donors)), high, marker="_", s=120, color="#b2182b")
        axis.set_xticks(np.arange(len(donors)), donors, rotation=35, ha="right")
        axis.set_ylabel(label)
        axis.grid(axis="y", alpha=0.25)
    axes[0].legend(frameon=False, loc="upper left", fontsize=7)
    figure.suptitle(f"Nucleus-aware QC distributions by donor{FIGURE_SCOPE_SUFFIX}", x=0.07, ha="left", fontsize=14, weight="bold")
    figure.text(0.07, 0.88, "Filtering uses donor-specific empirical count/gene/complexity bounds; mitochondrial fraction is diagnostic only.", fontsize=9)
    save_figure(figure, output)


def plot_embedding(adata: ad.AnnData, output: Path) -> None:
    set_style()
    coords = adata.obsm["X_umap"]
    figure, axes = plt.subplots(1, 2, figsize=(12.5, 5.4))
    donors = list(adata.obs["donor_id"].cat.categories)
    donor_colors = plt.get_cmap("tab10")(np.linspace(0, 1, len(donors)))
    for donor, color in zip(donors, donor_colors):
        mask = adata.obs["donor_id"].to_numpy() == donor
        axes[0].scatter(coords[mask, 0], coords[mask, 1], s=1.2, alpha=0.45, color=color, rasterized=True, label=donor)
    axes[0].set_title("Unintegrated PCA/UMAP colored by donor")
    axes[0].legend(markerscale=5, frameon=False, ncol=1)
    clusters = list(adata.obs["cluster"].cat.categories)
    colors = plt.get_cmap("tab20")(np.linspace(0, 1, max(20, len(clusters))))
    for cluster, color in zip(clusters, colors):
        mask = adata.obs["cluster"].astype(str).to_numpy() == cluster
        axes[1].scatter(coords[mask, 0], coords[mask, 1], s=1.2, alpha=0.55, color=color, rasterized=True, label=cluster)
    axes[1].set_title("Same embedding colored by Leiden cluster")
    axes[1].legend(markerscale=5, frameon=False, ncol=2, bbox_to_anchor=(1.01, 1.0), loc="upper left")
    for axis in axes:
        axis.set_xlabel("UMAP1")
        axis.set_ylabel("UMAP2")
        axis.set_xticks([])
        axis.set_yticks([])
    figure.suptitle(f"Donor-preserving unintegrated embedding{FIGURE_SCOPE_SUFFIX}", x=0.07, ha="left", fontsize=14, weight="bold")
    figure.text(0.07, 0.92, "Donor is retained because donor and library/batch are inseparable; mixing remains a descriptive diagnostic.", fontsize=9)
    save_figure(figure, output)


def plot_mixing(diagnostics: pd.DataFrame, output: Path) -> None:
    set_style()
    figure, axes = plt.subplots(1, 2, figsize=(9.2, 4.2))
    # Reserve a dedicated title/subtitle band.  Without this explicit margin,
    # the 1.0 y tick and the legends collide with the subtitle when the figure
    # is exported with a tight bounding box.
    figure.subplots_adjust(top=0.72, wspace=0.34)
    x = np.arange(diagnostics.shape[0])
    width = 0.36
    axes[0].bar(x - width / 2, diagnostics["same_donor_neighbor_fraction"], width=width, color="#4c78a8", label="observed local")
    axes[0].bar(x + width / 2, diagnostics["abundance_expected_same_donor_fraction"], width=width, color="#f58518", label="donor-abundance baseline")
    axes[0].set_ylabel("Same-donor neighbor fraction")
    axes[0].set_ylim(0, 1.08)
    axes[1].bar(x - width / 2, diagnostics["normalized_donor_entropy_mean"], width=width, color="#4c78a8", label="observed local")
    axes[1].bar(x + width / 2, diagnostics["abundance_expected_normalized_donor_entropy"], width=width, color="#f58518", label="donor-abundance baseline")
    axes[1].set_ylabel("Normalized donor entropy (global log 5)")
    axes[1].set_ylim(0, 1.08)
    for axis in axes:
        axis.set_xticks(x, diagnostics["representation"])
        axis.grid(axis="y", alpha=0.25)
        axis.legend(frameon=False, fontsize=7, loc="upper center", bbox_to_anchor=(0.5, 1.0))
    figure.suptitle(f"Local donor structure in the retained PCA{FIGURE_SCOPE_SUFFIX}", x=0.08, ha="left", fontsize=13, weight="bold")
    figure.text(0.08, 0.855, "No donor correction was applied because technical batch and biological donor are not identifiable separately.", fontsize=9)
    save_figure(figure, output)


def plot_annotation(adata: ad.AnnData, output: Path) -> None:
    set_style()
    coords = adata.obsm["X_umap"]
    labels = list(adata.obs["coarse_annotation"].cat.categories)
    cmap = plt.get_cmap("tab20")
    colors = cmap(np.linspace(0, 1, max(20, len(labels))))
    figure, axis = plt.subplots(figsize=(9.3, 6.5))
    for label, color in zip(labels, colors):
        mask = adata.obs["coarse_annotation"].astype(str).to_numpy() == label
        axis.scatter(coords[mask, 0], coords[mask, 1], s=1.4, alpha=0.55, color=color, rasterized=True, label=label)
    axis.set_xticks([])
    axis.set_yticks([])
    axis.set_xlabel("UMAP1")
    axis.set_ylabel("UMAP2")
    axis.legend(markerscale=5, frameon=False, bbox_to_anchor=(1.01, 1.0), loc="upper left")
    figure.suptitle(f"Evidence-bounded coarse kidney annotation{FIGURE_SCOPE_SUFFIX}", x=0.10, ha="left", fontsize=14, weight="bold")
    figure.text(0.10, 0.92, "Ambiguous and unknown labels are retained; labels are teaching hypotheses, not reference-validated identities.", fontsize=9)
    save_figure(figure, output)


def plot_composition(proportions: pd.DataFrame, output: Path) -> None:
    set_style()
    labels = proportions.columns.tolist()
    colors = plt.get_cmap("tab20")(np.linspace(0, 1, max(20, len(labels))))[: len(labels)]
    figure, axis = plt.subplots(figsize=(10.5, 5.7))
    bottom = np.zeros(proportions.shape[0])
    for label, color in zip(labels, colors):
        values = proportions[label].to_numpy() * 100
        axis.bar(proportions.index, values, bottom=bottom, label=label, color=color, edgecolor="white", linewidth=0.3)
        bottom += values
    axis.set_ylim(0, 100)
    axis.yaxis.set_major_formatter(mtick.PercentFormatter(100))
    axis.set_ylabel("Within-donor retained-nucleus composition")
    axis.set_xlabel("Donor")
    axis.legend(frameon=False, bbox_to_anchor=(1.01, 1), loc="upper left")
    axis.grid(axis="y", alpha=0.25)
    figure.suptitle(f"Coarse annotation composition across five healthy donors{FIGURE_SCOPE_SUFFIX}", x=0.10, ha="left", fontsize=14, weight="bold")
    figure.text(0.10, 0.90, "Descriptive only: there is no disease group or inferential contrast.", fontsize=9)
    save_figure(figure, output)


def plot_panel_heatmap(panel_scores: pd.DataFrame, evidence: pd.DataFrame, output: Path) -> None:
    set_style()
    figure, axis = plt.subplots(figsize=(10.5, max(4.5, 0.36 * panel_scores.shape[0] + 2.0)))
    values = panel_scores.to_numpy(dtype=float)
    image = axis.imshow(values, aspect="auto", cmap="RdBu_r", vmin=-2, vmax=2)
    axis.set_xticks(np.arange(panel_scores.shape[1]), panel_scores.columns, rotation=45, ha="right")
    axis.set_yticks(np.arange(panel_scores.shape[0]), [f"Cluster {x}" for x in panel_scores.index])
    axis.set_xlabel("Canonical marker panel")
    axis.set_ylabel("Leiden cluster")
    colorbar = figure.colorbar(image, ax=axis, pad=0.02, shrink=0.85)
    colorbar.set_label("Mean marker z-score across clusters")
    figure.suptitle(f"Marker-panel evidence used for coarse annotation{FIGURE_SCOPE_SUFFIX}", x=0.11, ha="left", fontsize=14, weight="bold")
    figure.text(0.11, 0.92, "Scores summarize selected canonical genes; they do not independently validate cell identity.", fontsize=9)
    save_figure(figure, output)


def main() -> int:
    global FIGURE_SCOPE_SUFFIX
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-config", type=Path, required=True)
    parser.add_argument("--metadata-sidecar", type=Path, required=True)
    parser.add_argument("--expected-metadata-sha256", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args()

    np.random.seed(args.seed)
    sc.settings.n_jobs = 2
    sc.settings.verbosity = 2
    memory_snapshots: list[dict[str, object]] = []
    # The first fail-closed memory gate precedes output-directory creation so
    # an under-resourced attempt does not leave a directory that resembles a
    # resumable or partially completed analysis.
    require_available_memory("pipeline-start", MIN_SYSTEM_AVAILABLE_GB_AT_START, memory_snapshots)
    output = args.output_dir.resolve()
    if output.exists():
        raise RuntimeError(f"Refusing to overwrite output directory: {output}")
    tables = output / "tables"
    objects = output / "objects"
    figures = output / "figures"
    reports = output / "reports"
    for directory in (tables, objects, figures, reports):
        directory.mkdir(parents=True, exist_ok=False)

    config = json.loads(args.input_config.read_text(encoding="utf-8-sig"))
    sampling_config = config.get("sampling")
    reduced_fixture = sampling_config is not None
    if reduced_fixture:
        if sampling_config.get("method") != "deterministic-without-replacement-h5-csc-column-slice":
            raise ValueError("Unsupported sampling method")
        if int(sampling_config.get("max_nuclei_per_donor", 0)) <= 0:
            raise ValueError("sampling.max_nuclei_per_donor must be positive")
        FIGURE_SCOPE_SUFFIX = " [reduced real-data fixture]"
    metadata_hash = sha256_file(args.metadata_sidecar.resolve())
    if metadata_hash != args.expected_metadata_sha256:
        raise RuntimeError(f"Metadata sidecar hash mismatch: {metadata_hash}")
    metadata = json.loads(args.metadata_sidecar.read_text(encoding="utf-8-sig"))
    metadata_samples = {sample["donor_label"]: sample for sample in metadata["samples"]}

    integrity_rows: list[dict[str, object]] = []
    qc_frames: list[pd.DataFrame] = []
    threshold_rows: list[dict[str, object]] = []
    doublet_reports: list[dict[str, object]] = []
    retained_by_donor: dict[str, list[str]] = {}
    donor_hvgs: dict[str, list[str]] = {}
    sampled_indices_by_donor: dict[str, np.ndarray] = {}
    sampling_rows: list[dict[str, object]] = []
    sampling_summaries: list[dict[str, object]] = []
    reference_features: list[str] | None = None
    total_barcodes = 0
    total_analyzed_barcodes = 0
    for donor_index, item in enumerate(config["inputs"]):
        require_available_memory(
            f"before-read-{item['donor_id']}",
            MIN_SYSTEM_AVAILABLE_GB_BEFORE_DONOR,
            memory_snapshots,
        )
        path = Path(item["path"]).resolve()
        observed_hash = sha256_file(path)
        observed_size = path.stat().st_size
        if observed_hash != item["sha256"] or observed_size != int(item["size_bytes"]):
            raise RuntimeError(f"Input integrity mismatch: {path.name}")
        with h5py.File(path, "r") as handle:
            shape = [int(value) for value in handle["matrix"]["shape"][:]]
            nnz = int(handle["matrix"]["data"].shape[0])
            dtype = str(handle["matrix"]["data"].dtype)
        donor = str(item["donor_id"])
        if shape != [int(item["expected_features"]), int(item["expected_barcodes"])]:
            raise RuntimeError(f"Raw H5 matrix shape mismatch for {donor}: {shape}")
        selected_indices: np.ndarray | None = None
        effective_sampling_seed: int | None = None
        if reduced_fixture:
            effective_sampling_seed = int(sampling_config["seed"]) + donor_index
            selected_indices = deterministic_barcode_indices(
                path,
                int(sampling_config["max_nuclei_per_donor"]),
                effective_sampling_seed,
            )
            sampled_indices_by_donor[donor] = selected_indices
        adata = read_matrix(path, selected_indices)
        if donor not in metadata_samples:
            raise RuntimeError(f"Donor missing from metadata sidecar: {donor}")
        expected_loaded_barcodes = (
            int(selected_indices.size) if selected_indices is not None else int(item["expected_barcodes"])
        )
        if adata.n_obs != expected_loaded_barcodes or adata.n_vars != int(item["expected_features"]):
            raise RuntimeError(f"Matrix shape mismatch for {donor}: {(adata.n_obs, adata.n_vars)}")
        if not require_integer_sparse(adata.X):
            raise RuntimeError(f"Non-integer counts detected for {donor}")
        if reference_features is None:
            reference_features = adata.var_names.tolist()
        elif adata.var_names.tolist() != reference_features:
            raise RuntimeError(f"Feature order mismatch for {donor}")
        total_barcodes += int(item["expected_barcodes"])
        total_analyzed_barcodes += adata.n_obs
        if selected_indices is not None:
            sampling_payload = "".join(
                f"{int(index)}\t{barcode}\n"
                for index, barcode in zip(selected_indices, adata.obs_names.astype(str))
            )
            membership_sha256 = hashlib.sha256(sampling_payload.encode("utf-8")).hexdigest()
            sampling_summaries.append(
                {
                    "donor_id": donor,
                    "sample_accession": item["sample_accession"],
                    "seed": effective_sampling_seed,
                    "full_barcodes": int(item["expected_barcodes"]),
                    "sampled_barcodes": int(adata.n_obs),
                    "sampling_fraction": float(adata.n_obs / int(item["expected_barcodes"])),
                    "membership_sha256": membership_sha256,
                    "streaming_contract": "only H5 indptr plus selected CSC data/index slices were read; full sparse matrix was not loaded before sampling",
                }
            )
            sampling_rows.extend(
                {
                    "donor_id": donor,
                    "sample_accession": item["sample_accession"],
                    "source_barcode_index": int(index),
                    "barcode": str(barcode),
                    "effective_seed": effective_sampling_seed,
                    "membership_sha256": membership_sha256,
                }
                for index, barcode in zip(selected_indices, adata.obs_names.astype(str))
            )
        integrity_rows.append(
            {
                "donor_id": donor,
                "sample_accession": item["sample_accession"],
                "file_name": path.name,
                "size_bytes": observed_size,
                "sha256": observed_hash,
                "features": adata.n_vars,
                "barcodes": int(item["expected_barcodes"]),
                "analyzed_barcodes": adata.n_obs,
                "sampling_fraction": float(adata.n_obs / int(item["expected_barcodes"])),
                "reduced_fixture": reduced_fixture,
                "nnz": nnz,
                "count_dtype": dtype,
                "integer_counts": True,
                "source_mode": "read_only",
            }
        )

        adata.var["mt"] = adata.var_names.str.upper().str.startswith("MT-")
        adata.var["ribo"] = adata.var_names.str.upper().str.startswith(("RPS", "RPL"))
        sc.pp.calculate_qc_metrics(adata, qc_vars=["mt", "ribo"], percent_top=(50,), log1p=False, inplace=True)
        qc = adata.obs[["total_counts", "n_genes_by_counts", "pct_counts_mt", "pct_counts_ribo", "pct_counts_in_top_50_genes"]].copy()
        qc["complexity"] = np.log10(qc["n_genes_by_counts"] + 1) / np.log10(qc["total_counts"] + 1)
        thresholds = distribution_thresholds(qc)
        initial_mask = threshold_mask(qc, thresholds)
        qc["donor_id"] = donor
        qc["sample_accession"] = item["sample_accession"]
        qc["original_barcode"] = qc.index.astype(str)
        qc["qc_pass_distribution"] = initial_mask
        qc["doublet_score"] = np.nan
        qc["predicted_doublet"] = pd.array([pd.NA] * qc.shape[0], dtype="boolean")
        qc["retained_final"] = False
        threshold_rows.append(
            {
                "donor_id": donor,
                **thresholds,
                "raw_nuclei": int(qc.shape[0]),
                "distribution_qc_pass": int(initial_mask.sum()),
                "mt_filter_applied": False,
                "ribo_filter_applied": False,
                "intronic_filter_applied": False,
                "threshold_basis": "donor-specific empirical 0.5th and 99.5th percentiles of UMIs, detected genes, and complexity",
            }
        )

        qc_adata = adata[initial_mask].copy()
        del adata
        gc.collect()
        require_available_memory(
            f"before-scrublet-{donor}",
            MIN_SYSTEM_AVAILABLE_GB_BEFORE_SCRUBLET,
            memory_snapshots,
        )
        try:
            scores, predicted, doublet_report = run_scrublet(qc_adata.X, args.seed + donor_index)
        except Exception as exc:
            scores = np.full(qc_adata.n_obs, np.nan, dtype=float)
            predicted = np.zeros(qc_adata.n_obs, dtype=bool)
            doublet_report = {
                "status": "scoring-failed",
                "scoring_completed": False,
                "scores_finite": False,
                "filter_eligible": False,
                "filter_reliability_reasons": ["Scrublet scoring did not complete with finite scores"],
                "method": "Scanpy bundled Scrublet scoring plus Gaussian-mixture threshold per donor",
                "failure_type": type(exc).__name__,
                "failure_message": str(exc),
                "scientific_action": "The all-donors-or-none policy forbids doublet filtering for every donor; this run cannot pass machine QA because scoring itself failed.",
            }
        doublet_report["donor_id"] = donor
        doublet_report["input_nuclei"] = int(qc_adata.n_obs)
        doublet_reports.append(doublet_report)
        passed_indices = np.flatnonzero(initial_mask)
        qc.iloc[passed_indices, qc.columns.get_loc("doublet_score")] = scores
        if bool(doublet_report["scoring_completed"]):
            qc.loc[qc.index[passed_indices], "predicted_doublet"] = predicted
        del qc_adata
        gc.collect()
        qc_frames.append(qc.reset_index(drop=True))

    assert reference_features is not None
    doublet_policy = decide_doublet_filter_policy(doublet_reports)
    doublet_scoring_all_completed = bool(doublet_policy["scoring_all_completed"])
    all_donors_filter_eligible = bool(doublet_policy["all_donors_filter_eligible"])
    doublet_filtering_applied = bool(doublet_policy["filtering_applied"])
    filter_policy_reason = str(doublet_policy["reason"])
    if not doublet_filtering_applied:
        FIGURE_SCOPE_SUFFIX = (
            " [reduced; not doublet-cleared]"
            if reduced_fixture
            else " [not doublet-cleared]"
        )
    for report in doublet_reports:
        report["filter_policy"] = "all-donors-or-none"
        report["filter_applied"] = doublet_filtering_applied
        report["doublet_cleared"] = doublet_filtering_applied
        report["global_policy_reason"] = filter_policy_reason
    for qc in qc_frames:
        donor = str(qc["donor_id"].iloc[0])
        distribution_pass = qc["qc_pass_distribution"].to_numpy(dtype=bool)
        candidate_doublet = qc["predicted_doublet"].fillna(False).to_numpy(dtype=bool)
        retained_mask = distribution_pass & (~candidate_doublet if doublet_filtering_applied else True)
        qc["doublet_filter_applied"] = doublet_filtering_applied
        qc["retained_final"] = retained_mask
        retained_by_donor[donor] = qc.loc[retained_mask, "original_barcode"].astype(str).tolist()
    for row in threshold_rows:
        row["doublet_filter_applied"] = doublet_filtering_applied
        row["filter_policy"] = "all-donors-or-none"

    # Recompute donor HVGs only after the cross-donor filter policy is frozen.
    # This prevents a single eligible donor from receiving different feature
    # selection when another donor's filtering decision is inconclusive.
    for item in config["inputs"]:
        donor = str(item["donor_id"])
        require_available_memory(
            f"before-final-hvg-{donor}",
            MIN_SYSTEM_AVAILABLE_GB_BEFORE_DONOR,
            memory_snapshots,
        )
        hvg_adata = read_matrix(Path(item["path"]).resolve(), sampled_indices_by_donor.get(donor))
        hvg_adata = hvg_adata[retained_by_donor[donor]].copy()
        sc.pp.normalize_total(hvg_adata, target_sum=1e4)
        sc.pp.log1p(hvg_adata)
        sc.pp.highly_variable_genes(
            hvg_adata,
            n_top_genes=min(DONOR_HVG_LIMIT, hvg_adata.n_vars),
            flavor="seurat",
            inplace=True,
        )
        donor_hvgs[donor] = hvg_adata.var_names[hvg_adata.var["highly_variable"]].astype(str).tolist()
        del hvg_adata
        gc.collect()

    qc_all = pd.concat(qc_frames, ignore_index=True)
    thresholds_frame = pd.DataFrame(threshold_rows)
    integrity_frame = pd.DataFrame(integrity_rows)
    hvg_union = set().union(*[set(value) for value in donor_hvgs.values()])
    canonical_markers = {gene for genes in MARKER_PANELS.values() for gene in genes}
    selected_set = hvg_union | canonical_markers
    selected_features = [gene for gene in reference_features if gene in selected_set]
    if len(selected_features) > SELECTED_FEATURE_LIMIT:
        raise MemoryError(
            f"Selected-feature cap exceeded: {len(selected_features)} > {SELECTED_FEATURE_LIMIT}; "
            "fail-closed instead of densifying or paging an unbounded merged matrix."
        )
    selected_feature_frame = pd.DataFrame(
        {
            "gene": selected_features,
            "selected_as_hvg": [gene in hvg_union for gene in selected_features],
            "selected_as_marker": [gene in canonical_markers for gene in selected_features],
            "hvg_donor_count": [sum(gene in donor_hvgs[donor] for donor in donor_hvgs) for gene in selected_features],
        }
    )

    retained_frames: list[ad.AnnData] = []
    for item in config["inputs"]:
        path = Path(item["path"]).resolve()
        donor = str(item["donor_id"])
        value = read_matrix(path, sampled_indices_by_donor.get(donor))
        value = value[retained_by_donor[donor], selected_features].copy()
        value.obs["original_barcode"] = value.obs_names.astype(str)
        value.obs_names = [f"{donor}:{barcode}" for barcode in value.obs_names.astype(str)]
        sample = metadata_samples[donor]
        value.obs["donor_id"] = donor
        value.obs["sample_accession"] = str(item["sample_accession"])
        value.obs["condition"] = "healthy control"
        value.obs["age_years"] = int(sample["age_years"])
        value.obs["sex"] = str(sample["sex"])
        value.obs["egfr_ml_min_1_73m2"] = int(sample["egfr_ml_min_1_73m2"])
        retained_frames.append(value)
    adata = ad.concat(retained_frames, axis=0, join="inner", merge="same", index_unique=None)
    del retained_frames
    gc.collect()
    require_available_memory("after-selected-feature-concat", 0.75, memory_snapshots)
    adata.obs["donor_id"] = pd.Categorical(adata.obs["donor_id"], categories=[item["donor_id"] for item in config["inputs"]], ordered=True)
    adata.var["highly_variable"] = adata.var_names.isin(hvg_union)
    adata.var["canonical_marker"] = adata.var_names.isin(canonical_markers)
    sc.pp.normalize_total(adata, target_sum=1e4)
    sc.pp.log1p(adata)
    sc.pp.pca(
        adata,
        n_comps=30,
        mask_var="highly_variable",
        zero_center=True,
        svd_solver="arpack",
        random_state=args.seed,
    )
    donor_codes = adata.obs["donor_id"].cat.codes.to_numpy()
    pre = neighbor_mixing(adata.obsm["X_pca"], donor_codes, n_neighbors=15)
    integration_frame = pd.DataFrame([
        {"representation": "PCA; no donor integration", **pre},
    ])
    sc.pp.neighbors(adata, n_neighbors=15, n_pcs=30, use_rep="X_pca", random_state=args.seed)
    sc.tl.umap(adata, random_state=args.seed, min_dist=0.35)
    sc.tl.leiden(adata, resolution=0.55, random_state=args.seed, key_added="cluster", flavor="igraph", n_iterations=2, directed=False)
    adata.obs["cluster"] = pd.Categorical(adata.obs["cluster"])

    mean_frame, fraction_frame, marker_frame = cluster_summaries(adata, "cluster")
    annotation_evidence, panel_scores = annotate_clusters(mean_frame, fraction_frame, marker_frame)
    label_map = annotation_evidence.set_index("cluster")["assigned_label"].to_dict()
    adata.obs["coarse_annotation"] = pd.Categorical(adata.obs["cluster"].astype(str).map(label_map))

    counts = pd.crosstab(adata.obs["donor_id"], adata.obs["coarse_annotation"], dropna=False)
    proportions = counts.div(counts.sum(axis=1), axis=0)
    composition_error = float((proportions.sum(axis=1) - 1.0).abs().max())
    if composition_error > 1e-12:
        raise RuntimeError(f"Composition reconciliation failed: {composition_error}")
    cluster_donor = pd.crosstab(adata.obs["cluster"], adata.obs["donor_id"], normalize="index")
    annotation_evidence = annotation_evidence.merge(
        adata.obs["cluster"].astype(str).value_counts().rename_axis("cluster").rename("nuclei").reset_index(),
        on="cluster",
        how="left",
    )

    embedding = adata.obs[["donor_id", "sample_accession", "condition", "cluster", "coarse_annotation", "original_barcode"]].copy()
    embedding["UMAP1"] = adata.obsm["X_umap"][:, 0]
    embedding["UMAP2"] = adata.obsm["X_umap"][:, 1]

    stable_frame_to_tsv(integrity_frame, tables / "input-integrity.tsv")
    if reduced_fixture:
        stable_frame_to_tsv(pd.DataFrame(sampling_rows), tables / "sampled-barcodes.tsv")
    stable_frame_to_tsv(qc_all, tables / "nucleus-qc.tsv")
    stable_frame_to_tsv(thresholds_frame, tables / "donor-qc-thresholds.tsv")
    stable_frame_to_tsv(selected_feature_frame, tables / "selected-features.tsv")
    stable_frame_to_tsv(integration_frame, tables / "integration-diagnostics.tsv")
    stable_frame_to_tsv(marker_frame, tables / "cluster-marker-evidence.tsv")
    stable_frame_to_tsv(annotation_evidence, tables / "annotation-evidence.tsv")
    stable_frame_to_tsv(counts.reset_index(), tables / "donor-annotation-counts.tsv")
    stable_frame_to_tsv(proportions.reset_index(), tables / "donor-annotation-proportions.tsv")
    stable_frame_to_tsv(cluster_donor.reset_index(), tables / "cluster-donor-composition.tsv")
    stable_frame_to_tsv(panel_scores.reset_index(names="cluster"), tables / "marker-panel-scores.tsv")
    stable_frame_to_tsv(embedding.reset_index(names="nucleus_id"), tables / "embedding.tsv")

    plot_qc(qc_all, thresholds_frame, figures / "qc-distributions-by-donor.png")
    plot_embedding(adata, figures / "embedding-donor-cluster.png")
    plot_mixing(integration_frame, figures / "donor-mixing-diagnostics.png")
    plot_annotation(adata, figures / "coarse-annotation-umap.png")
    plot_composition(proportions, figures / "donor-composition-descriptive.png")
    plot_panel_heatmap(panel_scores, annotation_evidence, figures / "marker-panel-heatmap.png")

    adata.uns["analysis_scope"] = (
        "reduced real-data healthy-kidney teaching fixture; deterministic maximum 1,000 nuclei per donor; no full-data prevalence or case-control estimand"
        if reduced_fixture
        else "descriptive full-input healthy-kidney teaching atlas; no case-control estimand"
    )
    adata.uns["input_accessions"] = [item["sample_accession"] for item in config["inputs"]]
    adata.uns["integration_method"] = "none; unintegrated 30-PC PCA retained because donor_id equals library/batch and cannot be separated from biological donor variation"
    adata.uns["expression_matrix_scope"] = "log1p normalized counts for union of per-donor HVGs plus canonical marker genes; not full-gene differential-analysis object"
    adata.uns["doublet_filter_policy"] = "all-donors-or-none"
    adata.uns["doublet_filtering_applied"] = doublet_filtering_applied
    adata.uns["doublet_cleared"] = doublet_filtering_applied
    adata.write_h5ad(objects / "processed-selected-feature-log1p.h5ad", compression="gzip")
    memory_snapshots.append(memory_snapshot("after-object-write"))

    input_profile = {
        "schema_version": "1.0",
        "case_id": "p0-single-cell-gse185948",
        "metadata_sidecar_sha256": metadata_hash,
        "input_files": integrity_rows,
        "donors": [item["donor_id"] for item in config["inputs"]],
        "total_filtered_matrix_barcodes": total_barcodes,
        "analyzed_input_barcodes": total_analyzed_barcodes,
        "retained_nuclei": int(adata.n_obs),
        "original_features": len(reference_features),
        "selected_features": int(adata.n_vars),
        "measurement_unit": "nucleus",
        "biological_replicate": "donor/library (n=5 healthy controls)",
        "inference_unit": "donor for any future inferential model",
        "case_control_estimand_available": False,
        "analysis_mode": "reduced-real-data-teaching-fixture" if reduced_fixture else "full-input-teaching-case",
        "full_data_analyzed": not reduced_fixture,
        "doublet_filtering_applied": doublet_filtering_applied,
        "doublet_cleared": doublet_filtering_applied,
        "sampling": sampling_summaries if reduced_fixture else None,
    }
    qc_decisions = {
        "schema_version": "1.0",
        "nucleus_aware": True,
        "thresholds": threshold_rows,
        "mitochondrial_fraction": "calculated and plotted diagnostically; no mitochondrial cutoff was applied",
        "ribosomal_fraction": "calculated diagnostically; no ribosomal cutoff was applied",
        "intronic_fraction": "not separately estimable from filtered gene-count matrices because exonic and intronic reads are already combined by Cell Ranger --include-introns and no spliced/unspliced/read-level layer is present",
        "ambient_rna": "not estimated or corrected: filtered matrices lack empty droplets/unfiltered barcode counts required for contamination estimation",
        "doublet_policy": {
            "scoring_all_completed": doublet_scoring_all_completed,
            "all_donors_filter_eligible": all_donors_filter_eligible,
            "filter_policy": "all-donors-or-none",
            "filtering_applied": doublet_filtering_applied,
            "doublet_cleared": doublet_filtering_applied,
            "reason": filter_policy_reason,
        },
        "retained_reconciliation": {donor: len(values) for donor, values in retained_by_donor.items()},
        "memory_safety": {
            "thread_environment_observed": {name: os.environ.get(name) for name in THREAD_ENV_VARS},
            "scanpy_n_jobs_observed": int(sc.settings.n_jobs),
            "launcher_and_script_process_local_override": True,
            "donor_processing": "serial",
            "donor_hvg_limit": DONOR_HVG_LIMIT,
            "selected_feature_limit": SELECTED_FEATURE_LIMIT,
            "scrublet_feature_limit": SCRUBLET_FEATURE_LIMIT,
            "full_gene_merged_matrix_created": False,
            "dense_full_count_matrix_created": False,
            "reduced_fixture_streaming_column_slice": reduced_fixture,
            "snapshots": memory_snapshots,
        },
    }
    integration_report = {
        "schema_version": "1.0",
        "method_executed": "no integration",
        "covariate_reviewed": "donor_id equals donor/library/batch",
        "input_representation": "30-PC PCA on union of per-donor HVGs",
        "output_representation": "X_pca unchanged",
        "diagnostics": integration_frame.to_dict(orient="records"),
        "mixing_normalization": "local donor entropy uses the fixed global donor count log(5); observed same-donor fraction and entropy are compared with donor-abundance expectations",
        "decision": "Do not regress donor: each donor is also one library/batch, so technical and biological donor variation are not identifiable; Harmony on donor would risk erasing the biological replicate axis.",
        "interpretation": "Abundance-adjusted local donor mixing and cluster-by-donor composition diagnose donor structure in the retained PCA; they are descriptive, cannot separate technical from biological donor effects, and do not prove that technical effects are absent.",
        "overcorrection_boundary": "No correction was applied. Canonical marker coherence and donor representation were reviewed descriptively; no external reference benchmark was used.",
    }
    annotation_report = {
        "schema_version": "1.0",
        "clusters": int(adata.obs["cluster"].nunique()),
        "coarse_labels": int(adata.obs["coarse_annotation"].nunique()),
        "unknown_or_ambiguous_nuclei": int(adata.obs["coarse_annotation"].astype(str).str.contains("Unknown|Ambiguous", regex=True).sum()),
        "method": "cluster mean canonical-marker z scores plus descriptive top-marker support",
        "reference_mapping_performed": False,
        "claim_boundary": "Assignments are coarse teaching hypotheses and are not reference-validated cell identities.",
    }
    environment_versions = {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "scanpy": sc.__version__,
        "anndata": ad.__version__,
        "scrublet_implementation": "scanpy.preprocessing._scrublet bundled implementation",
        "numpy": np.__version__,
        "pandas": pd.__version__,
        "scipy": scipy.__version__,
        "scikit_learn": sklearn.__version__,
        "h5py": h5py.__version__,
        "matplotlib": matplotlib.__version__,
    }
    qa_status = (
        "PASS"
        if doublet_scoring_all_completed and doublet_filtering_applied
        else "PASS_WITH_WARNINGS"
        if doublet_scoring_all_completed
        else "FAIL"
    )
    qa = {
        "ok": bool(doublet_scoring_all_completed),
        "qa_status": qa_status,
        "input_hashes_verified": True,
        "metadata_sidecar_hash_verified": True,
        "integer_counts_verified": True,
        "donors_preserved": True,
        "donor_count": 5,
        "distribution_derived_qc": True,
        "whole_cell_mt_cutoff_applied": False,
        "doublet_checks_all_executed": doublet_scoring_all_completed,
        "doublet_scoring_all_completed": doublet_scoring_all_completed,
        "doublet_filter_candidates_all_eligible": all_donors_filter_eligible,
        "doublet_filter_policy": "all-donors-or-none",
        "doublet_filtering_applied": doublet_filtering_applied,
        "doublet_cleared": doublet_filtering_applied,
        "doublet_warning": None if doublet_filtering_applied else filter_policy_reason,
        "ambient_estimation_executed": False,
        "ambient_nonexecution_reason": "filtered matrices lack empty droplets/unfiltered barcode counts",
        "intronic_fraction_available": False,
        "integration_method_matches_code": True,
        "integration_method": "no integration; unintegrated PCA retained after confounding review",
        "unknown_annotations_allowed": True,
        "inferential_tests_performed": False,
        "case_control_estimand_available": False,
        "analysis_mode": "reduced-real-data-teaching-fixture" if reduced_fixture else "full-input-teaching-case",
        "full_data_analyzed": not reduced_fixture,
        "sampling_membership_hashed": reduced_fixture,
        "composition_max_sum_error": composition_error,
        "native_visual_review": "pending",
        "claim_ceiling": (
            "Reduced real-data healthy-kidney workflow fixture only; sampled composition cannot estimate full-dataset or donor-population prevalence, and no disease, causal, prognostic, or clinical claim is supported."
            if reduced_fixture
            else "Descriptive healthy-kidney nuclear atlas teaching evidence only; no disease, causal, prognostic, or clinical claim."
        ),
    }
    write_json(reports / "input-profile.json", input_profile)
    write_json(reports / "qc-decisions.json", qc_decisions)
    write_json(reports / "memory-safety.json", qc_decisions["memory_safety"])
    write_json(
        reports / "doublet-decisions.json",
        {
            "schema_version": "1.0",
            "filter_policy": "all-donors-or-none",
            "scoring_all_completed": doublet_scoring_all_completed,
            "all_donors_filter_eligible": all_donors_filter_eligible,
            "filtering_applied": doublet_filtering_applied,
            "doublet_cleared": doublet_filtering_applied,
            "policy_reason": filter_policy_reason,
            "donors": doublet_reports,
        },
    )
    write_json(reports / "integration-diagnostics.json", integration_report)
    write_json(reports / "annotation-summary.json", annotation_report)
    write_json(reports / "environment-versions.json", environment_versions)
    if reduced_fixture:
        write_json(
            reports / "sampling-manifest.json",
            {
                "schema_version": "1.0",
                "mode": "reduced-real-data-teaching-fixture",
                "full_matrix_loaded_before_sampling": False,
                "method": sampling_config["method"],
                "base_seed": int(sampling_config["seed"]),
                "max_nuclei_per_donor": int(sampling_config["max_nuclei_per_donor"]),
                "donors": sampling_summaries,
                "claim_boundary": "The fixture validates execution mechanics on real sparse counts; it is not a substitute for a full-data biological analysis.",
            },
        )
    write_json(reports / "qa-machine.json", qa)
    execution_mode_line = (
        f"- Execution mode: reduced real-data teaching fixture using deterministic streaming column slices ({total_analyzed_barcodes:,} sampled barcodes; maximum 1,000 per donor). No full H5 sparse matrix was loaded before sampling.\n"
        if reduced_fixture
        else f"- Execution mode: full-input teaching case using all {total_analyzed_barcodes:,} source barcodes before QC.\n"
    )
    (reports / "QA_REPORT.md").write_text(
        "# QA report\n\n"
        + f"Machine QA: {qa_status}\n\n"
        + f"- Inputs: five SHA-256-verified filtered 10x snRNA H5 files; {total_barcodes:,} source barcodes.\n"
        + execution_mode_line
        + f"- Retained: {adata.n_obs:,} nuclei across five donors after empirical per-donor QC and the predeclared all-donors-or-none doublet policy.\n"
        + f"- Doublet handling: scoring completed for all donors; filtering applied={doublet_filtering_applied}; doublet-cleared={doublet_filtering_applied}. {filter_policy_reason}\n"
        + "- Mitochondrial and ribosomal fractions were diagnostic only; no canned whole-cell mitochondrial threshold was used.\n"
        + "- Intronic fraction cannot be separated from these --include-introns filtered count matrices.\n"
        + "- Ambient RNA was not estimated because empty droplets/unfiltered counts are unavailable.\n"
        + "- No donor integration was executed because donor and library/batch are identical; local donor structure was quantified on the retained PCA.\n"
        + "- Annotation is coarse and evidence-bounded; ambiguous/unknown labels remain.\n"
        + "- No inferential test or case-control comparison was performed.\n\n"
        + "Native visual review: pending.\n",
        encoding="utf-8",
    )
    figure_scope_line = (
        "All figures are explicitly marked as a reduced real-data fixture based on at most 1,000 deterministically sampled nuclei per donor. Sampled composition does not estimate full-data or donor-population prevalence. "
        if reduced_fixture
        else "All figures use the full filtered inputs before QC. "
    )
    doublet_figure_boundary = (
        "No doublet filter was applied because at least one donor failed the predeclared reliability gate; candidate scores and labels are diagnostic only."
        if not doublet_filtering_applied
        else "Doublet filtering passed the predeclared reliability gates and was applied consistently across all donors."
    )
    common_source = (
        "Generated by `03_scripts/run_pipeline.py` from the five SHA-256-bound GSE185948 filtered 10x snRNA H5 inputs; "
        "sample membership and source hashes are recorded in `sampling-manifest.json` and `input-profile.json`."
    )
    figure_notes = f"""# Figure notes

## Scope shared by all figures

{figure_scope_line}{doublet_figure_boundary} Native visual review is pending and is registered as a separate immutable checkpoint after every PNG is opened at native resolution.

## `qc-distributions-by-donor.png`

- Research question: Do sampled nuclei from each donor satisfy donor-specific empirical library-complexity bounds without imposing a whole-cell mitochondrial cutoff?
- Data and statistical unit: Per-nucleus detected-gene, UMI and mitochondrial fractions for five donors; the plotted unit is one sampled nucleus and red bars are donor-specific empirical 0.5/99.5% bounds.
- Directly visible: Donor-specific distributions and the large upper tails in UMI, gene and mitochondrial diagnostics.
- Supports: Auditing the operational distribution-derived QC rules and between-donor heterogeneity in the reduced fixture.
- Does not support: Cell viability, ambient-RNA removal, intronic fraction, calibrated doublet prevalence, full-dataset retention rates or donor-population prevalence.
- Assumptions and method prerequisites: Filtered matrices contain valid integer UMI counts; mitochondrial fraction is diagnostic only; sampling is deterministic but reduced.
- Visual quality draft: Three aligned panels, units and empirical bounds are explicit; native-resolution clipping, overlap and color legibility remain to be reviewed.
- Reproduction level and source: Data-verified for this reduced teaching fixture; not an exact reproduction of a publication figure. {common_source}

## `embedding-donor-cluster.png`

- Research question: What structure is present in the retained, unintegrated PCA/UMAP, and how is it distributed across donor and Leiden cluster labels?
- Data and statistical unit: One retained sampled nucleus per point; both panels use the identical UMAP coordinates derived from selected-feature log-normalized expression.
- Directly visible: Donor localization and overlap on the left, and the descriptive Leiden partition on the right.
- Supports: Describing the reduced embedding, donor structure and cluster geometry used by downstream teaching summaries.
- Does not support: Biological distances, trajectories, batch-correction success, disease separation, donor-independent populations or cluster truth.
- Assumptions and method prerequisites: UMAP preserves local neighborhoods imperfectly; donor and library/batch are inseparable, so no donor integration was applied.
- Visual quality draft: The paired panels share coordinates and expose donor confounding; native-resolution label, palette and density legibility remain to be reviewed.
- Reproduction level and source: Data-verified for this reduced teaching fixture; not an exact reproduction of a publication figure. {common_source}

## `donor-mixing-diagnostics.png`

- Research question: How donor-local is the retained PCA neighborhood structure relative to expectations from global donor abundance?
- Data and statistical unit: Per-nucleus 15-nearest-neighbor donor labels summarized across retained nuclei; bars compare the observed local statistic with its donor-abundance baseline.
- Directly visible: Observed same-donor neighbor fraction and normalized donor entropy versus their abundance-derived expectations.
- Supports: Diagnosing strong donor-associated structure in the retained PCA and documenting why unintegrated results require cautious interpretation.
- Does not support: Separating technical batch from donor biology, proving a batch effect, proving absence of valid donor biology or selecting a correction method by itself.
- Assumptions and method prerequisites: All five donors define the entropy denominator `log(5)`; abundance baselines use retained reduced-fixture frequencies; neighborhoods are PCA-derived.
- Visual quality draft: Paired observed-versus-baseline bars use a shared 0-1.08 range and a reserved title band; native-resolution overlap and legend placement remain to be reviewed.
- Reproduction level and source: Data-verified for this reduced teaching fixture; not an exact reproduction of a publication figure. {common_source}

## `coarse-annotation-umap.png`

- Research question: Which coarse kidney-lineage hypotheses are consistent with the canonical-marker evidence for each Leiden cluster?
- Data and statistical unit: One retained sampled nucleus per point, colored by its cluster-level coarse hypothesis; ambiguous/unknown assignments are retained.
- Directly visible: The location and relative separation of eight coarse annotation categories in the reduced UMAP.
- Supports: A teaching-level, evidence-bounded map of marker-consistent coarse compartments.
- Does not support: Reference-validated cell identity, fine subtypes, lineage, state, function, disease association or clinical interpretation.
- Assumptions and method prerequisites: Cluster means and selected canonical panels are adequate only for coarse hypotheses; no external reference mapping or orthogonal validation was performed.
- Visual quality draft: Legend and uncertainty category are explicit; native-resolution palette discrimination and point-density legibility remain to be reviewed.
- Reproduction level and source: Data-verified for this reduced teaching fixture; not an exact reproduction of a publication figure. {common_source}

## `donor-composition-descriptive.png`

- Research question: How do coarse annotation proportions differ descriptively among the five healthy-donor reduced samples?
- Data and statistical unit: Retained sampled nuclei nested within donor; each bar is a within-donor descriptive composition summing to 100%, not an independent inferential replicate.
- Directly visible: Relative fractions of the coarse annotation categories within each sampled donor.
- Supports: Descriptive comparison of this deterministic reduced fixture and detection of gross donor-specific composition differences for follow-up.
- Does not support: Population prevalence, differential abundance, uncertainty, disease effects or significance; nucleus counts must not be treated as independent donor replicates.
- Assumptions and method prerequisites: Coarse labels are provisional and the deterministic 1,000-nucleus source sampling can alter proportions relative to the full matrices.
- Visual quality draft: Percent scale and descriptive-only subtitle are explicit; native-resolution thin-segment and palette discrimination remain to be reviewed.
- Reproduction level and source: Data-verified for this reduced teaching fixture; not an exact reproduction of a publication figure. {common_source}

## `marker-panel-heatmap.png`

- Research question: Which canonical marker panels provide relative support for each coarse cluster hypothesis?
- Data and statistical unit: Cluster-level mean expression summaries standardized as marker-panel z-scores across the 15 Leiden clusters; the heatmap cell is one cluster-by-panel summary.
- Directly visible: Relative positive and negative marker-panel scores and conflicts among candidate kidney compartments.
- Supports: Auditing the marker evidence that informed coarse labels and identifying ambiguous/conflicting clusters.
- Does not support: Per-cell expression prevalence, differential expression significance, marker specificity, reference-validated identity or independent biological validation.
- Assumptions and method prerequisites: The selected canonical genes are present and interpretable in nuclear data; z-scores are relative across these clusters and are clipped to the displayed -2 to 2 range.
- Visual quality draft: Diverging scale, cluster rows and panel labels are explicit; native-resolution label clipping and color-scale legibility remain to be reviewed.
- Reproduction level and source: Data-verified for this reduced teaching fixture; not an exact reproduction of a publication figure. {common_source}
"""
    (reports / "FIGURE_NOTES.md").write_text(figure_notes, encoding="utf-8")

    artifact_rows = []
    for path in sorted(item for item in output.rglob("*") if item.is_file() and item.name != "artifact-index.json"):
        artifact_rows.append(
            {
                "relative_path": path.relative_to(output).as_posix(),
                "size_bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    write_json(reports / "artifact-index.json", {"schema_version": "1.0", "artifacts": artifact_rows})
    print(
        json.dumps(
            {
                "ok": qa["ok"],
                "output": str(output),
                "retained_nuclei": int(adata.n_obs),
                "selected_features": int(adata.n_vars),
                "clusters": int(adata.obs["cluster"].nunique()),
                "unknown_or_ambiguous_nuclei": annotation_report["unknown_or_ambiguous_nuclei"],
            },
            ensure_ascii=False,
        )
    )
    return 0 if qa["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
