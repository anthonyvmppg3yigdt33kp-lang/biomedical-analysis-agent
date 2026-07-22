#!/usr/bin/env python3
"""Complete P0 CLL MOFA-FLEX pretrained audit and two-seed stability workflow.

The source MuData and pretrained model are read-only. All generated artifacts are
written under a run tree. The reduced refit is explicitly a computational stability
probe and is not an exact reproduction of the full 15-factor reference model.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import os
import shutil
import sys
import warnings
from pathlib import Path
from typing import Any, Iterable

os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("MKL_NUM_THREADS", "2")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "2")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "2")

import h5py
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import mofaflex as mfl
import mudata as md
import numpy as np
import pandas as pd
import torch
from matplotlib.colors import Normalize
from scipy.optimize import linear_sum_assignment
from scipy.stats import spearmanr


EXPECTED_DATA_SHA = "1da99d3967f8616adcee2bcea0157c30acf34c46884902343518e9634ec2f7ee"
EXPECTED_MODEL_SHA = "8effa89420f7e4c3cc0c2f4f5423d3f6901ba9be1c0f5f04f3b4deb2437db562"
EXPECTED_VIEWS = {
    "Drugs": (184, 310),
    "Methylation": (196, 4248),
    "Mutations": (200, 69),
    "mRNA": (136, 5000),
}
FEATURE_CAPS = {"Drugs": 100, "Methylation": 200, "Mutations": 69, "mRNA": 200}
SEEDS = (42, 43)
N_FACTORS = 6
MAX_EPOCHS = 200
PATIENCE = 50
LEARNING_RATE = 0.01
TOP_LOADING_FEATURES = 20
VIEW_ORDER = ("Drugs", "Methylation", "Mutations", "mRNA")
BLUE = "#0072B2"
ORANGE = "#D55E00"
GREEN = "#009E73"
PURPLE = "#7B61A8"
GREY = "#6B7280"
LIGHT_GREY = "#E5E7EB"
VIEW_COLORS = {"Drugs": BLUE, "Methylation": ORANGE, "Mutations": GREEN, "mRNA": PURPLE}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--pretrained-model", type=Path, required=True)
    parser.add_argument("--source-spec", type=Path, required=True)
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def atomic_json(path: Path, payload: Any) -> None:
    atomic_text(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n")


def write_tsv(frame: pd.DataFrame, path: Path, *, index: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    frame.to_csv(temporary, sep="\t", index=index, lineterminator="\n")
    os.replace(temporary, path)


def write_tsv_gz(frame: pd.DataFrame, path: Path, *, index: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    frame.to_csv(
        temporary,
        sep="\t",
        index=index,
        lineterminator="\n",
        compression={"method": "gzip", "mtime": 0},
    )
    os.replace(temporary, path)


def begin_stage(run_root: Path, stage_id: str) -> Path:
    final = run_root / "04_intermediate" / stage_id
    staging = run_root / "04_intermediate" / "_staging" / stage_id
    if final.exists():
        raise RuntimeError(f"Refusing to overwrite completed stage: {final}")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True, exist_ok=False)
    return staging


def promote_stage(run_root: Path, stage_id: str, staging: Path, required: Iterable[str]) -> Path:
    missing = [name for name in required if not (staging / name).is_file()]
    if missing:
        raise RuntimeError(f"Stage {stage_id} missing required artifacts: {missing}")
    final = run_root / "04_intermediate" / stage_id
    atomic_json(
        staging / "stage.complete.json",
        {
            "stage_id": stage_id,
            "status": "validated",
            "required_files": sorted(required),
            "artifact_sha256": {
                path.relative_to(staging).as_posix(): sha256_file(path)
                for path in sorted(staging.rglob("*"))
                if path.is_file()
            },
        },
    )
    os.replace(staging, final)
    return final


def dense_float(adata) -> np.ndarray:
    matrix = adata.X
    if hasattr(matrix, "toarray"):
        matrix = matrix.toarray()
    return np.asarray(matrix, dtype=np.float64)


def model_source_version(path: Path) -> str:
    with h5py.File(path, "r") as handle:
        return str(handle["mofaflex"].attrs.get("version", "unknown"))


def profile_inputs(mdata) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, int]:
    patient_union = pd.Index(mdata.obs_names.astype(str), name="patient_id")
    availability = pd.DataFrame(index=patient_union)
    view_rows: list[dict[str, Any]] = []
    feature_rows: list[pd.DataFrame] = []
    sample_sets: list[set[str]] = []
    for view in VIEW_ORDER:
        adata = mdata.mod[view]
        samples = set(adata.obs_names.astype(str))
        sample_sets.append(samples)
        availability[view] = patient_union.isin(samples)
        values = dense_float(adata)
        finite = np.isfinite(values)
        missing_cells = int(values.size - int(finite.sum()))
        view_rows.append(
            {
                "view": view,
                "samples_with_view": int(adata.n_obs),
                "patients_missing_entire_view": int(len(patient_union) - adata.n_obs),
                "features": int(adata.n_vars),
                "matrix_cells_within_observed_samples": int(values.size),
                "missing_cells_within_observed_samples": missing_cells,
                "missing_fraction_within_observed_samples": missing_cells / values.size,
            }
        )
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=RuntimeWarning)
            variances = np.nanvar(values, axis=0)
        feature_rows.append(
            pd.DataFrame(
                {
                    "view": view,
                    "feature": adata.var_names.astype(str),
                    "finite_values": finite.sum(axis=0).astype(int),
                    "missing_values": (~finite).sum(axis=0).astype(int),
                    "missing_fraction": (~finite).mean(axis=0),
                    "variance": variances,
                }
            )
        )
    availability.insert(0, "patient_id", availability.index)
    availability["views_available"] = availability[list(VIEW_ORDER)].sum(axis=1).astype(int)
    intersection = len(set.intersection(*sample_sets))
    return pd.DataFrame(view_rows), availability.reset_index(drop=True), pd.concat(feature_rows), intersection


def select_reduced_features(mdata) -> tuple[Any, pd.DataFrame]:
    selected_views = {}
    audits: list[pd.DataFrame] = []
    for view in VIEW_ORDER:
        adata = mdata.mod[view]
        values = dense_float(adata)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=RuntimeWarning)
            variances = np.nanvar(values, axis=0)
        names = np.asarray(adata.var_names.astype(str))
        valid = np.flatnonzero(np.isfinite(variances))
        ordered = sorted(valid.tolist(), key=lambda idx: (-float(variances[idx]), str(names[idx])))
        cap = min(FEATURE_CAPS[view], len(ordered))
        keep = np.asarray(ordered[:cap], dtype=int)
        selected_views[view] = adata[:, keep].copy()
        ranks = {int(idx): rank + 1 for rank, idx in enumerate(ordered)}
        audit = pd.DataFrame(
            {
                "view": view,
                "feature": names,
                "variance": variances,
                "finite_values": np.isfinite(values).sum(axis=0).astype(int),
                "missing_fraction": (~np.isfinite(values)).mean(axis=0),
                "variance_rank": [ranks.get(idx, pd.NA) for idx in range(adata.n_vars)],
                "selected": [idx in set(keep.tolist()) for idx in range(adata.n_vars)],
            }
        )
        audits.append(audit)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=FutureWarning)
        reduced = md.MuData(selected_views, obs=mdata.obs.copy())
    return reduced, pd.concat(audits, ignore_index=True)


def export_model_tables(model, prefix: str, directory: Path) -> dict[str, Any]:
    factors = model.get_factors()
    weights = model.get_weights()
    r2 = model.get_r2("term", ordered=False)
    for group, frame in factors.items():
        exported = frame.copy()
        exported.insert(0, "patient_id", exported.index.astype(str))
        write_tsv(exported.reset_index(drop=True), directory / f"{prefix}_factor_scores_{group}.tsv")
    top_weight_frames = []
    for view, frame in weights.items():
        exported = frame.copy()
        exported.insert(0, "feature", exported.index.astype(str))
        write_tsv_gz(exported.reset_index(drop=True), directory / f"{prefix}_weights_{view}.tsv.gz")
        for factor in frame.columns:
            ranked = frame[factor].abs().nlargest(min(20, frame.shape[0])).index
            top = pd.DataFrame(
                {
                    "view": view,
                    "factor": factor,
                    "feature": ranked.astype(str),
                    "weight": frame.loc[ranked, factor].to_numpy(),
                    "absolute_weight": frame.loc[ranked, factor].abs().to_numpy(),
                    "rank_by_absolute_weight": np.arange(1, len(ranked) + 1),
                }
            )
            top_weight_frames.append(top)
    write_tsv(r2, directory / f"{prefix}_variance_explained.tsv")
    top_weights = pd.concat(top_weight_frames, ignore_index=True)
    write_tsv(top_weights, directory / f"{prefix}_top_weights.tsv")
    loss = np.asarray(model.training_loss, dtype=float)
    write_tsv(
        pd.DataFrame({"epoch": np.arange(1, len(loss) + 1), "loss": loss}),
        directory / f"{prefix}_training_loss.tsv",
    )
    return {
        "factors": factors,
        "weights": weights,
        "r2": r2,
        "top_weights": top_weights,
        "training_loss": loss,
    }


def fit_reduced_model(reduced, seed: int, model_path: Path):
    torch.manual_seed(seed)
    np.random.seed(seed)
    model = mfl.terms.MofaFlex(n_factors=N_FACTORS, weight_prior="SpikeSlab", init_factors="pca")
    model.fit(
        reduced,
        seed=seed,
        save_path=model_path,
        lr=LEARNING_RATE,
        early_stopper_patience=PATIENCE,
        max_epochs=MAX_EPOCHS,
        device="cpu",
        plot_data_overview=False,
        subset_var=None,
        use_obs="union",
        num_workers=0,
        pin_memory=False,
    )
    return model


def factor_stability(first, second) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    f1 = first.get_factors()["group_1"].sort_index()
    f2 = second.get_factors()["group_1"].sort_index()
    common = f1.index.intersection(f2.index)
    if len(common) != 200:
        raise RuntimeError(f"Factor-score patient overlap is {len(common)}, expected 200")
    corr = pd.DataFrame(index=f1.columns, columns=f2.columns, dtype=float)
    for left in f1.columns:
        for right in f2.columns:
            corr.loc[left, right] = spearmanr(f1.loc[common, left], f2.loc[common, right]).statistic
    if not np.isfinite(corr.to_numpy()).all():
        raise RuntimeError("Non-finite factor stability correlations")
    row_ind, col_ind = linear_sum_assignment(-np.abs(corr.to_numpy()))
    rows = []
    for left_idx, right_idx in zip(row_ind, col_ind, strict=True):
        rho = float(corr.iat[left_idx, right_idx])
        absolute = abs(rho)
        label = "high" if absolute >= 0.80 else "moderate" if absolute >= 0.50 else "low"
        rows.append(
            {
                "seed42_factor": str(corr.index[left_idx]),
                "seed43_factor": str(corr.columns[right_idx]),
                "spearman_rho": rho,
                "absolute_spearman_rho": absolute,
                "alignment_sign": 1 if rho >= 0 else -1,
                "operational_stability_label": label,
                "patients_compared": len(common),
            }
        )
    matching = pd.DataFrame(rows).sort_values("seed42_factor").reset_index(drop=True)

    loading_rows = []
    w1 = first.get_weights()
    w2 = second.get_weights()
    for match in matching.itertuples(index=False):
        for view in VIEW_ORDER:
            common_features = w1[view].index.intersection(w2[view].index)
            left = w1[view].loc[common_features, match.seed42_factor]
            right = w2[view].loc[common_features, match.seed43_factor] * match.alignment_sign
            rho = float(spearmanr(left, right).statistic)
            top_n = min(TOP_LOADING_FEATURES, len(common_features))
            top_left = set(left.abs().nlargest(top_n).index.astype(str))
            top_right = set(right.abs().nlargest(top_n).index.astype(str))
            union = top_left | top_right
            loading_rows.append(
                {
                    "seed42_factor": match.seed42_factor,
                    "seed43_factor": match.seed43_factor,
                    "view": view,
                    "score_alignment_sign": match.alignment_sign,
                    "aligned_loading_spearman_rho": rho,
                    "absolute_loading_spearman_rho": abs(rho),
                    "top_loading_n": top_n,
                    "top_loading_intersection": len(top_left & top_right),
                    "top_loading_jaccard": len(top_left & top_right) / len(union) if union else math.nan,
                    "features_compared": len(common_features),
                }
            )
    return corr, matching, pd.DataFrame(loading_rows)


def style_axis(ax, *, grid_axis: str | None = None) -> None:
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    if grid_axis:
        ax.grid(axis=grid_axis, color=LIGHT_GREY, linewidth=0.8, zorder=0)
    ax.tick_params(labelsize=9)


def save_figure(fig, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=300, bbox_inches="tight", facecolor="white", metadata={"Software": "matplotlib"})
    plt.close(fig)


def make_figures(
    mdata,
    view_profile: pd.DataFrame,
    pretrained: dict[str, Any],
    seed_models: dict[int, dict[str, Any]],
    corr: pd.DataFrame,
    matching: pd.DataFrame,
    output_dir: Path,
) -> pd.DataFrame:
    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "axes.titlesize": 12,
            "axes.labelsize": 10,
            "legend.fontsize": 9,
            "figure.titlesize": 14,
        }
    )
    registry: list[dict[str, Any]] = []

    fig, axes = plt.subplots(1, 3, figsize=(12, 4.5), constrained_layout=True)
    positions = np.arange(len(VIEW_ORDER))
    ordered_profile = view_profile.set_index("view").loc[list(VIEW_ORDER)]
    axes[0].bar(positions, ordered_profile["samples_with_view"], color=[VIEW_COLORS[v] for v in VIEW_ORDER])
    axes[0].axhline(200, color=GREY, linestyle="--", linewidth=1, label="patient union (n=200)")
    axes[0].set_xticks(positions, VIEW_ORDER, rotation=25, ha="right")
    axes[0].set_ylabel("Patients with observed view")
    axes[0].set_ylim(0, 215)
    axes[0].legend(frameon=False, loc="lower left")
    style_axis(axes[0], grid_axis="y")
    axes[1].bar(positions, ordered_profile["features"], color=[VIEW_COLORS[v] for v in VIEW_ORDER])
    axes[1].set_xticks(positions, VIEW_ORDER, rotation=25, ha="right")
    axes[1].set_ylabel("Features (log scale)")
    axes[1].set_yscale("log")
    style_axis(axes[1], grid_axis="y")
    axes[2].bar(
        positions,
        ordered_profile["missing_fraction_within_observed_samples"],
        color=[VIEW_COLORS[v] for v in VIEW_ORDER],
    )
    axes[2].set_xticks(positions, VIEW_ORDER, rotation=25, ha="right")
    axes[2].set_ylabel("Missing fraction within observed view")
    axes[2].set_ylim(0, max(0.36, ordered_profile["missing_fraction_within_observed_samples"].max() * 1.15))
    style_axis(axes[2], grid_axis="y")
    fig.suptitle("CLL multi-omics input coverage; patient union retained")
    path = output_dir / "fig01_view_coverage_missingness.png"
    save_figure(fig, path)
    registry.append({"figure": path.name, "semantic_role": "input coverage and missingness audit", "source": "cll.h5mu"})

    r2 = pretrained["r2"].pivot(index="component", columns="view", values="R2").loc[
        [f"Factor {idx}" for idx in range(1, 16)], list(VIEW_ORDER)
    ]
    fig, ax = plt.subplots(figsize=(7.5, 8), constrained_layout=True)
    image = ax.imshow(r2.to_numpy(), cmap="OrRd", vmin=0, vmax=max(0.15, float(r2.max().max())), aspect="auto")
    ax.set_xticks(np.arange(len(VIEW_ORDER)), VIEW_ORDER, rotation=30, ha="right")
    ax.set_yticks(np.arange(len(r2.index)), r2.index)
    for row in range(r2.shape[0]):
        for col in range(r2.shape[1]):
            value = float(r2.iat[row, col])
            ax.text(col, row, f"{value:.2f}", ha="center", va="center", fontsize=7, color="white" if value > 0.09 else "black")
    colorbar = fig.colorbar(image, ax=ax, shrink=0.82)
    colorbar.set_label("Fraction of variance explained (R²)")
    ax.set_title("Pretrained 15-factor model: view-specific variance explained")
    path = output_dir / "fig02_pretrained_r2_heatmap.png"
    save_figure(fig, path)
    registry.append({"figure": path.name, "semantic_role": "pretrained factor-by-view R2", "source": "official pretrained model"})

    factors = pretrained["factors"]["group_1"]
    metadata = mdata.obs.reindex(factors.index)
    ighv = metadata["IGHV"].astype("string").fillna("missing")
    categories = sorted(ighv.unique().tolist(), key=lambda value: (value == "missing", value))
    palette = [BLUE, ORANGE, GREY, GREEN, PURPLE]
    fig, ax = plt.subplots(figsize=(7.5, 6), constrained_layout=True)
    for idx, category in enumerate(categories):
        mask = ighv == category
        ax.scatter(
            factors.loc[mask, "Factor 1"],
            factors.loc[mask, "Factor 2"],
            s=30,
            alpha=0.75,
            color=palette[idx % len(palette)],
            edgecolor="white",
            linewidth=0.35,
            label=f"IGHV {category} (n={int(mask.sum())})",
        )
    ax.axhline(0, color=LIGHT_GREY, linewidth=0.8)
    ax.axvline(0, color=LIGHT_GREY, linewidth=0.8)
    ax.set_xlabel("Factor 1 score (arbitrary sign)")
    ax.set_ylabel("Factor 2 score (arbitrary sign)")
    ax.set_title("Pretrained factor scores with descriptive IGHV overlay")
    ax.legend(frameon=False, bbox_to_anchor=(1.02, 1), loc="upper left")
    style_axis(ax)
    path = output_dir / "fig03_pretrained_factor_scores.png"
    save_figure(fig, path)
    registry.append({"figure": path.name, "semantic_role": "descriptive patient factor scores", "source": "pretrained model plus supplied IGHV metadata"})

    aggregate = pretrained["r2"].groupby("component", sort=False)["R2"].sum().sort_values(ascending=False)
    strongest = str(aggregate.index[0])
    fig, axes = plt.subplots(2, 2, figsize=(12, 9), constrained_layout=True)
    for ax, view in zip(axes.flat, VIEW_ORDER, strict=True):
        frame = pretrained["weights"][view]
        selected = frame[strongest].abs().nlargest(10).index
        values = frame.loc[selected, strongest].sort_values()
        colors = [ORANGE if value < 0 else BLUE for value in values]
        ax.barh(np.arange(len(values)), values.to_numpy(), color=colors)
        ax.set_yticks(np.arange(len(values)), values.index.astype(str), fontsize=8)
        ax.axvline(0, color=GREY, linewidth=0.8)
        ax.set_xlabel("Weight (arbitrary factor sign)")
        ax.set_title(view)
        style_axis(ax, grid_axis="x")
    fig.suptitle(f"Top absolute loadings for {strongest}, the largest summed R² factor")
    path = output_dir / "fig04_pretrained_top_weights.png"
    save_figure(fig, path)
    registry.append({"figure": path.name, "semantic_role": "view-specific top loadings for strongest aggregate factor", "source": "official pretrained model"})

    fig, ax = plt.subplots(figsize=(7, 6.5), constrained_layout=True)
    abs_corr = corr.abs()
    image = ax.imshow(abs_corr.to_numpy(), cmap="viridis", vmin=0, vmax=1)
    ax.set_xticks(np.arange(abs_corr.shape[1]), [value.replace("Factor ", "F") for value in abs_corr.columns], rotation=45, ha="right")
    ax.set_yticks(np.arange(abs_corr.shape[0]), [value.replace("Factor ", "F") for value in abs_corr.index])
    matched_pairs = set(zip(matching.seed42_factor, matching.seed43_factor))
    for row, left in enumerate(abs_corr.index):
        for col, right in enumerate(abs_corr.columns):
            value = float(abs_corr.iat[row, col])
            color = "white" if value < 0.35 or value > 0.75 else "black"
            ax.text(col, row, f"{value:.2f}", ha="center", va="center", fontsize=8, color=color)
            if (left, right) in matched_pairs:
                ax.add_patch(plt.Rectangle((col - 0.48, row - 0.48), 0.96, 0.96, fill=False, edgecolor=ORANGE, linewidth=2))
    colorbar = fig.colorbar(image, ax=ax, shrink=0.82)
    colorbar.set_label("Absolute Spearman correlation")
    ax.set_xlabel("Seed 43 factors")
    ax.set_ylabel("Seed 42 factors")
    ax.set_title("Reduced refit factor matching (orange = Hungarian assignment)")
    path = output_dir / "fig05_seed_factor_correlation.png"
    save_figure(fig, path)
    registry.append({"figure": path.name, "semantic_role": "rotation/sign-aware two-seed factor matching", "source": "reduced real-data refits"})

    ordered = matching.sort_values("absolute_spearman_rho", ascending=True)
    labels = [f"{a} ↔ {b}" for a, b in zip(ordered.seed42_factor, ordered.seed43_factor, strict=True)]
    colors = ordered.operational_stability_label.map({"high": GREEN, "moderate": BLUE, "low": ORANGE})
    fig, ax = plt.subplots(figsize=(9, 5), constrained_layout=True)
    ax.barh(np.arange(len(ordered)), ordered.absolute_spearman_rho, color=colors)
    ax.set_yticks(np.arange(len(ordered)), labels)
    ax.axvline(0.50, color=GREY, linestyle="--", linewidth=1, label="operational moderate threshold")
    ax.axvline(0.80, color="black", linestyle=":", linewidth=1, label="operational high threshold")
    for y, value in enumerate(ordered.absolute_spearman_rho):
        ax.text(min(value + 0.02, 0.96), y, f"{value:.2f}", va="center", fontsize=9)
    ax.set_xlim(0, 1.02)
    ax.set_xlabel("Matched absolute Spearman correlation across 200 patients")
    ax.set_title("Two-seed stability of reduced MOFA-FLEX factors")
    ax.legend(frameon=False, loc="lower right")
    style_axis(ax, grid_axis="x")
    path = output_dir / "fig06_matched_factor_stability.png"
    save_figure(fig, path)
    registry.append({"figure": path.name, "semantic_role": "matched factor stability summary", "source": "reduced real-data refits"})

    fig, ax = plt.subplots(figsize=(9, 5), constrained_layout=True)
    for seed, color in zip(SEEDS, (BLUE, ORANGE), strict=True):
        loss = pd.Series(seed_models[seed]["training_loss"])
        smooth = loss.rolling(window=min(15, max(3, len(loss) // 10)), min_periods=1).median()
        ax.plot(np.arange(1, len(loss) + 1), loss, color=color, alpha=0.18, linewidth=0.8)
        ax.plot(np.arange(1, len(loss) + 1), smooth, color=color, linewidth=2, label=f"seed {seed} rolling median")
    ax.set_xlabel("Epoch")
    ax.set_ylabel("Negative ELBO training loss")
    ax.set_title("Reduced real-data refit training diagnostics")
    ax.legend(frameon=False)
    style_axis(ax, grid_axis="y")
    path = output_dir / "fig07_reduced_refit_training_loss.png"
    save_figure(fig, path)
    registry.append({"figure": path.name, "semantic_role": "optimization diagnostic, not model validity proof", "source": "reduced real-data refits"})

    return pd.DataFrame(registry)


def main() -> int:
    args = parse_args()
    run_root = args.run_root.resolve()
    data_path = args.data.resolve()
    model_path = args.pretrained_model.resolve()
    source_spec = args.source_spec.resolve()
    for path in (data_path, model_path, source_spec):
        if not path.is_file():
            raise FileNotFoundError(path)
    if sha256_file(data_path) != EXPECTED_DATA_SHA:
        raise RuntimeError("CLL MuData hash differs from the frozen source contract")
    if sha256_file(model_path) != EXPECTED_MODEL_SHA:
        raise RuntimeError("Pretrained model hash differs from the frozen source contract")

    torch.set_num_threads(2)
    torch.set_num_interop_threads(1)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=FutureWarning)
        mdata = md.read_h5mu(data_path)
    observed = {view: (int(mdata.mod[view].n_obs), int(mdata.mod[view].n_vars)) for view in VIEW_ORDER}
    if observed != EXPECTED_VIEWS or int(mdata.n_obs) != 200:
        raise RuntimeError(f"Input dimension contract failed: union={mdata.n_obs}, views={observed}")

    stage03_id = "03-methodology-review"
    stage03 = begin_stage(run_root, stage03_id)
    view_profile, availability, feature_profile, complete_all_views = profile_inputs(mdata)
    write_tsv(view_profile, stage03 / "view_profile.tsv")
    write_tsv(availability, stage03 / "patient_view_availability.tsv")
    write_tsv_gz(feature_profile, stage03 / "feature_missingness_and_variance.tsv.gz")
    write_tsv(mdata.obs.reset_index(names="patient_id"), stage03 / "supplied_patient_metadata.tsv")
    write_tsv(
        pd.DataFrame(
            [
                {"role": "dataset", "path": str(data_path), "sha256": sha256_file(data_path)},
                {"role": "pretrained_model", "path": str(model_path), "sha256": sha256_file(model_path)},
                {"role": "source_spec", "path": str(source_spec), "sha256": sha256_file(source_spec)},
            ]
        ),
        stage03 / "source_provenance.tsv",
    )
    methodology = f"""# Methodology contract

- Statistical unit: patient.
- Patient universe: union of all supplied views (`n=200`); complete coverage in all four views is `n={complete_all_views}`.
- Missingness: unavailable views and within-view `NaN` values remain missing. No complete-case replacement or outcome-informed imputation is used.
- Pretrained audit: read-only extraction from the supplied 15-factor model; its factor order and sign are arbitrary.
- Reduced stability probe: within-view variance selection (Drugs 100, Methylation 200, Mutations 69, mRNA 200), 6 factors, SpikeSlab weight prior, PCA initialization, seeds 42/43, CPU, maximum 200 epochs.
- Factor comparison: one-to-one Hungarian assignment maximizing absolute Spearman correlation across the same 200 patient scores.
- Scope: exploratory/descriptive. No causal, prognostic, diagnostic, treatment-response, or clinical-subtype claim is tested.
"""
    atomic_text(stage03 / "methodology_contract.md", methodology)
    atomic_json(
        stage03 / "methodology_summary.json",
        {
            "patient_union": 200,
            "complete_all_views": complete_all_views,
            "view_dimensions": observed,
            "pretrained_model_source_version": model_source_version(model_path),
            "runtime_mofaflex_version": importlib.metadata.version("mofaflex"),
            "source_runtime_version_match": model_source_version(model_path) == importlib.metadata.version("mofaflex"),
            "version_mismatch_boundary": "The bundled pretrained model loads with a compatibility warning; factors, weights, R2 and loss are audited, but unsupported API behavior is not assumed.",
        },
    )
    stage03 = promote_stage(
        run_root,
        stage03_id,
        stage03,
        (
            "view_profile.tsv",
            "patient_view_availability.tsv",
            "feature_missingness_and_variance.tsv.gz",
            "supplied_patient_metadata.tsv",
            "source_provenance.tsv",
            "methodology_contract.md",
            "methodology_summary.json",
        ),
    )

    stage04_id = "04-multi-omics"
    stage04 = begin_stage(run_root, stage04_id)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        pretrained_model = mfl.MOFAFLEX.load(model_path, map_location="cpu")
    pretrained = export_model_tables(pretrained_model, "pretrained", stage04)
    atomic_text(stage04 / "pretrained_load_warnings.txt", "\n".join(str(item.message) for item in caught) + "\n")

    reduced, selection_audit = select_reduced_features(mdata)
    write_tsv_gz(selection_audit, stage04 / "reduced_feature_selection_audit.tsv.gz")
    selection_summary = selection_audit.loc[selection_audit.selected].groupby("view").size().to_dict()
    if selection_summary != FEATURE_CAPS:
        raise RuntimeError(f"Reduced feature selection contract failed: {selection_summary}")

    fitted = {}
    exported = {}
    for seed in SEEDS:
        fitted[seed] = fit_reduced_model(reduced, seed, stage04 / f"reduced_model_seed{seed}.h5")
        exported[seed] = export_model_tables(fitted[seed], f"seed{seed}", stage04)
        reloaded = mfl.MOFAFLEX.load(stage04 / f"reduced_model_seed{seed}.h5", map_location="cpu")
        if reloaded.get_factors()["group_1"].shape != (200, N_FACTORS):
            raise RuntimeError(f"Reloaded model contract failed for seed {seed}")

    score_corr, matching, loading_stability = factor_stability(fitted[42], fitted[43])
    score_corr_export = score_corr.copy()
    score_corr_export.insert(0, "seed42_factor", score_corr_export.index)
    write_tsv(score_corr_export.reset_index(drop=True), stage04 / "seed_factor_spearman_matrix.tsv")
    write_tsv(matching, stage04 / "seed_factor_hungarian_matching.tsv")
    write_tsv(loading_stability, stage04 / "seed_loading_stability.tsv")
    stability_summary = {
        "matched_factors": int(len(matching)),
        "median_absolute_score_rho": float(matching.absolute_spearman_rho.median()),
        "minimum_absolute_score_rho": float(matching.absolute_spearman_rho.min()),
        "high_count": int((matching.operational_stability_label == "high").sum()),
        "moderate_count": int((matching.operational_stability_label == "moderate").sum()),
        "low_count": int((matching.operational_stability_label == "low").sum()),
        "median_absolute_loading_rho": float(loading_stability.absolute_loading_spearman_rho.median()),
        "median_top20_loading_jaccard": float(loading_stability.top_loading_jaccard.median()),
    }
    atomic_json(stage04 / "stability_summary.json", stability_summary)
    stage04 = promote_stage(
        run_root,
        stage04_id,
        stage04,
        (
            "pretrained_factor_scores_group_1.tsv",
            "pretrained_variance_explained.tsv",
            "pretrained_top_weights.tsv",
            "pretrained_training_loss.tsv",
            "pretrained_load_warnings.txt",
            "reduced_feature_selection_audit.tsv.gz",
            "reduced_model_seed42.h5",
            "reduced_model_seed43.h5",
            "seed42_factor_scores_group_1.tsv",
            "seed43_factor_scores_group_1.tsv",
            "seed_factor_spearman_matrix.tsv",
            "seed_factor_hungarian_matching.tsv",
            "seed_loading_stability.tsv",
            "stability_summary.json",
        ),
    )

    stage05_id = "05-analysis-qa"
    stage05 = begin_stage(run_root, stage05_id)
    figure_registry = make_figures(
        mdata,
        view_profile,
        pretrained,
        exported,
        score_corr,
        matching,
        stage05 / "figures" / "original",
    )
    write_tsv(figure_registry, stage05 / "figure_registry.tsv")
    qa_rows = [
        {"check": "input_hashes", "status": "pass", "detail": "dataset and pretrained model match frozen SHA-256"},
        {"check": "patient_union", "status": "pass" if int(mdata.n_obs) == 200 else "fail", "detail": str(mdata.n_obs)},
        {"check": "view_dimensions", "status": "pass" if observed == EXPECTED_VIEWS else "fail", "detail": json.dumps(observed)},
        {"check": "complete_case_not_substituted", "status": "pass", "detail": f"union=200; four-view intersection={complete_all_views}"},
        {"check": "pretrained_factor_count", "status": "pass" if pretrained["factors"]["group_1"].shape[1] == 15 else "fail", "detail": str(pretrained["factors"]["group_1"].shape)},
        {"check": "pretrained_r2_finite", "status": "pass" if np.isfinite(pretrained["r2"].R2).all() else "fail", "detail": "60 factor-view entries"},
        {"check": "reduced_models_reload", "status": "pass", "detail": "both HDF5 models reloaded with 200 x 6 factor scores"},
        {"check": "seed_matching_one_to_one", "status": "pass" if len(set(matching.seed42_factor)) == len(set(matching.seed43_factor)) == 6 else "fail", "detail": "6 Hungarian assignments"},
        {"check": "seed_correlations_finite", "status": "pass" if np.isfinite(score_corr.to_numpy()).all() else "fail", "detail": "6 x 6 score-correlation matrix"},
        {"check": "figures_created", "status": "pass" if len(figure_registry) == 7 else "fail", "detail": f"{len(figure_registry)} PNG files"},
        {"check": "model_version_boundary", "status": "pass_with_boundary", "detail": f"source={model_source_version(model_path)}; runtime={importlib.metadata.version('mofaflex')}"},
    ]
    qa = pd.DataFrame(qa_rows)
    if (qa.status == "fail").any():
        raise RuntimeError("One or more scientific QA checks failed")
    write_tsv(qa, stage05 / "qa_checks.tsv")
    atomic_json(
        stage05 / "runtime.json",
        {
            "python": sys.version,
            "mofaflex": importlib.metadata.version("mofaflex"),
            "mudata": importlib.metadata.version("mudata"),
            "torch": importlib.metadata.version("torch"),
            "numpy": importlib.metadata.version("numpy"),
            "threads": {"torch": torch.get_num_threads(), "interop": torch.get_num_interop_threads()},
        },
    )
    figure_names = figure_registry.figure.tolist()
    stage05 = promote_stage(
        run_root,
        stage05_id,
        stage05,
        ("figure_registry.tsv", "qa_checks.tsv", "runtime.json", *[f"figures/original/{name}" for name in figure_names]),
    )

    figure_original = run_root / "06_figures" / "original"
    figure_original.mkdir(parents=True, exist_ok=True)
    for name in figure_names:
        shutil.copy2(stage05 / "figures" / "original" / name, figure_original / name)

    stage06_id = "06-interpretation"
    stage06 = begin_stage(run_root, stage06_id)
    strongest = (
        pretrained["r2"].groupby("component", sort=False)["R2"].sum().sort_values(ascending=False).reset_index()
    )
    strongest.columns = ["factor", "summed_view_r2"]
    write_tsv(strongest, stage06 / "pretrained_factor_ranking_by_summed_r2.tsv")
    summary = f"""# Exploratory analysis summary

The public CLL object contains 200 patients in the union; {complete_all_views} have all four views. The workflow retained the union and preserved both absent views and within-view missing values.

The supplied pretrained model contains 15 latent factors. `{strongest.iloc[0].factor}` has the largest sum of view-specific R² ({strongest.iloc[0].summed_view_r2:.3f}), but this ranking is descriptive and does not identify a causal program or clinical subtype.

In the reduced two-seed stability probe, {stability_summary['high_count']} of 6 matched factors met the operational high-stability label (absolute Spearman at least 0.80), {stability_summary['moderate_count']} were moderate, and {stability_summary['low_count']} were low. The median matched absolute score correlation was {stability_summary['median_absolute_score_rho']:.3f}. These values characterize this reduced specification only; they do not prove that the full 15-factor reference solution is unique or stable.

The pretrained model was serialized with MOFA-FLEX `{model_source_version(model_path)}` and audited under `{importlib.metadata.version('mofaflex')}`. Core factors, weights, R² and training loss loaded, but the compatibility warning remains a formal interpretation boundary.
"""
    atomic_text(stage06 / "analysis_summary.md", summary)
    atomic_text(
        stage06 / "interpretation_guardrails.md",
        """# Interpretation guardrails

- Factor number, order and sign are not biologically identifiable; compare solutions only after matching and sign handling.
- A high loading is an association with a latent axis, not evidence that a feature drives disease.
- The IGHV overlay is unadjusted and descriptive; no hypothesis test or clinical classifier was fitted.
- Variance explained is in-sample descriptive fit and does not measure external predictive performance.
- Training-loss reduction is an optimizer diagnostic, not proof of biological validity or a global optimum.
- Operational stability labels are workflow conventions, not universal statistical thresholds.
- This teaching dataset cannot support patient-specific diagnosis, prognosis, treatment choice, or causal claims.
""",
    )
    stage06 = promote_stage(
        run_root,
        stage06_id,
        stage06,
        ("analysis_summary.md", "interpretation_guardrails.md", "pretrained_factor_ranking_by_summed_r2.tsv"),
    )

    results_tables = run_root / "05_results" / "tables"
    results_objects = run_root / "05_results" / "objects"
    results_tables.mkdir(parents=True, exist_ok=True)
    results_objects.mkdir(parents=True, exist_ok=True)
    table_copies = {
        stage03 / "view_profile.tsv": "view_profile.tsv",
        stage03 / "patient_view_availability.tsv": "patient_view_availability.tsv",
        stage04 / "pretrained_variance_explained.tsv": "pretrained_variance_explained.tsv",
        stage04 / "pretrained_top_weights.tsv": "pretrained_top_weights.tsv",
        stage04 / "seed_factor_spearman_matrix.tsv": "seed_factor_spearman_matrix.tsv",
        stage04 / "seed_factor_hungarian_matching.tsv": "seed_factor_hungarian_matching.tsv",
        stage04 / "seed_loading_stability.tsv": "seed_loading_stability.tsv",
        stage06 / "pretrained_factor_ranking_by_summed_r2.tsv": "pretrained_factor_ranking_by_summed_r2.tsv",
    }
    for source, name in table_copies.items():
        shutil.copy2(source, results_tables / name)
    for seed in SEEDS:
        shutil.copy2(stage04 / f"reduced_model_seed{seed}.h5", results_objects / f"reduced_model_seed{seed}.h5")
    shutil.copy2(stage06 / "analysis_summary.md", run_root / "05_results" / "analysis_summary.md")
    shutil.copy2(stage06 / "interpretation_guardrails.md", run_root / "05_results" / "interpretation_guardrails.md")

    report_dir = run_root / "07_reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    analysis_design = f"""# ANALYSIS DESIGN — CLL MOFA-FLEX P0 teaching case

## Research question

Audit the supplied official 15-factor CLL model, then assess whether a reduced six-factor real-data specification yields matched latent axes across seeds 42 and 43.

## Units and inputs

- Statistical unit: patient.
- Patient policy: retain the union (`n=200`), not the four-view complete-case subset (`n={complete_all_views}`).
- Views: normalized mRNA, methylation M-values, drug viability and mutation status as supplied.
- No clinical endpoint, supervised prediction, differential test or causal estimand is defined.

## Model and stability design

- Reference audit: read-only pretrained 15-factor MOFA-FLEX model.
- Reduced probe: 6 factors; SpikeSlab weights; PCA initialization; within-view outcome-free variance selection; explicit CPU execution; maximum 200 epochs; early stopping patience 50.
- Stability: all 36 cross-seed score correlations, one-to-one Hungarian assignment on absolute Spearman rho, sign alignment, and view-specific loading/top-feature agreement.

## Conclusion ceiling

Results can describe factor/view structure and computational stability under this specification. They cannot establish mechanisms, clinical subtypes, prognosis, treatment response, external validity or patient-specific recommendations.
"""
    atomic_text(run_root / "01_plan" / "ANALYSIS_DESIGN.md", analysis_design)
    qa_report = "# QA REPORT\n\n" + "\n".join(
        f"- **{row.check}**: {row.status} — {row.detail}" for row in qa.itertuples(index=False)
    ) + "\n\nNative pixel review is pending; no figure is final until `06_figures/review/native-visual-review.json` is added.\n"
    atomic_text(report_dir / "QA_REPORT.md", qa_report)
    atomic_text(
        report_dir / "FIGURE_NOTES.md",
        "# FIGURE NOTES\n\nSeven original figures were generated. Scientific and native pixel review is pending; final conclusions and reproduction levels will be added after visual review.\n",
    )
    atomic_text(
        report_dir / "ARTIFACT_INDEX.md",
        "# ARTIFACT INDEX\n\nThis index is finalized after checkpoint registration and native visual review. See `manifest/artifact_ledger.jsonl` for registered hashes.\n",
    )
    print(
        json.dumps(
            {
                "ok": True,
                "patient_union": 200,
                "complete_all_views": complete_all_views,
                "pretrained_factors": 15,
                "reduced_factors": 6,
                "seed_stability": stability_summary,
                "figures": figure_names,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
