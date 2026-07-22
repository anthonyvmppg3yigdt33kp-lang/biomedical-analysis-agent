#!/usr/bin/env python3
"""Render final figures after the first native-pixel review."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


BLUE = "#0072B2"
ORANGE = "#D55E00"
GREEN = "#009E73"
PURPLE = "#7B61A8"
GREY = "#6B7280"
LIGHT_GREY = "#E5E7EB"
VIEW_ORDER = ("Drugs", "Methylation", "Mutations", "mRNA")
VIEW_COLORS = {"Drugs": BLUE, "Methylation": ORANGE, "Mutations": GREEN, "mRNA": PURPLE}


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    args = parser.parse_args()
    run_root = args.run_root.resolve()
    final = run_root / "06_figures" / "final-revision-2"
    final.mkdir(parents=True, exist_ok=True)
    original = run_root / "06_figures" / "original"
    stage03 = run_root / "04_intermediate" / "03-methodology-review"
    stage04 = run_root / "04_intermediate" / "04-multi-omics"

    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "axes.titlesize": 12,
            "axes.labelsize": 10,
            "legend.fontsize": 8,
            "figure.titlesize": 14,
        }
    )

    profile = pd.read_csv(stage03 / "view_profile.tsv", sep="\t").set_index("view").loc[list(VIEW_ORDER)]
    fig, axes = plt.subplots(1, 3, figsize=(11, 4.2), constrained_layout=True)
    positions = np.arange(4)
    colors = [VIEW_COLORS[view] for view in VIEW_ORDER]
    bars = axes[0].bar(positions, profile.samples_with_view, color=colors, zorder=2)
    axes[0].axhline(200, color=GREY, linestyle="--", linewidth=1)
    for bar, value in zip(bars, profile.samples_with_view, strict=True):
        axes[0].text(bar.get_x() + bar.get_width() / 2, value + 2, str(int(value)), ha="center", va="bottom", fontsize=8)
    axes[0].text(0.02, 0.98, "patient union = 200", transform=axes[0].transAxes, va="top", color=GREY, fontsize=8)
    axes[0].set_xticks(positions, VIEW_ORDER, rotation=25, ha="right")
    axes[0].set_ylabel("Patients with observed view")
    axes[0].set_ylim(0, 218)
    style_axis(axes[0], grid_axis="y")

    bars = axes[1].bar(positions, profile.features, color=colors, zorder=2)
    for bar, value in zip(bars, profile.features, strict=True):
        axes[1].text(bar.get_x() + bar.get_width() / 2, value * 1.09, f"{int(value):,}", ha="center", fontsize=8)
    axes[1].set_xticks(positions, VIEW_ORDER, rotation=25, ha="right")
    axes[1].set_ylabel("Features (log scale)")
    axes[1].set_yscale("log")
    axes[1].set_ylim(35, 7200)
    style_axis(axes[1], grid_axis="y")

    missing = profile.missing_fraction_within_observed_samples.to_numpy()
    bars = axes[2].bar(positions, missing, color=colors, zorder=2)
    for bar, value in zip(bars, missing, strict=True):
        label = "0" if value == 0 else f"{value:.3f}"
        y = value + 0.008 if value > 0 else 0.008
        axes[2].text(bar.get_x() + bar.get_width() / 2, y, label, ha="center", va="bottom", fontsize=8)
    axes[2].set_xticks(positions, VIEW_ORDER, rotation=25, ha="right")
    axes[2].set_ylabel("Missing fraction within observed view")
    axes[2].set_ylim(0, 0.38)
    style_axis(axes[2], grid_axis="y")
    fig.suptitle("CLL multi-omics input coverage; patient union retained")
    save_figure(fig, final / "fig01_view_coverage_missingness.png")

    factors = pd.read_csv(stage04 / "pretrained_factor_scores_group_1.tsv", sep="\t").set_index("patient_id")
    metadata = pd.read_csv(stage03 / "supplied_patient_metadata.tsv", sep="\t").set_index("patient_id").reindex(factors.index)
    ighv = metadata["IGHV"].astype("string").fillna("missing")
    categories = sorted(ighv.unique().tolist(), key=lambda value: (value == "missing", value))
    palette = [BLUE, ORANGE, GREY, GREEN, PURPLE]
    fig, ax = plt.subplots(figsize=(8.2, 6.2), constrained_layout=True)
    for idx, category in enumerate(categories):
        mask = ighv == category
        ax.scatter(
            factors.loc[mask, "Factor 1"],
            factors.loc[mask, "Factor 2"],
            s=28,
            alpha=0.78,
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
    ax.legend(frameon=True, framealpha=0.9, facecolor="white", edgecolor=LIGHT_GREY, loc="upper right")
    style_axis(ax)
    save_figure(fig, final / "fig03_pretrained_factor_scores.png")

    matching = pd.read_csv(stage04 / "seed_factor_hungarian_matching.tsv", sep="\t")
    ordered = matching.sort_values("absolute_spearman_rho", ascending=True)
    labels = [f"{a} ↔ {b}" for a, b in zip(ordered.seed42_factor, ordered.seed43_factor, strict=True)]
    bar_colors = ordered.operational_stability_label.map({"high": GREEN, "moderate": BLUE, "low": ORANGE})
    fig, ax = plt.subplots(figsize=(9, 5.6))
    ax.barh(np.arange(len(ordered)), ordered.absolute_spearman_rho, color=bar_colors, zorder=2)
    ax.set_yticks(np.arange(len(ordered)), labels)
    ax.axvline(0.50, color=GREY, linestyle="--", linewidth=1.2, zorder=3)
    ax.axvline(0.80, color="black", linestyle=":", linewidth=1.2, zorder=3)
    for y, value in enumerate(ordered.absolute_spearman_rho):
        ax.text(max(value - 0.045, 0.02), y, f"{value:.2f}", va="center", ha="center", fontsize=9, color="white", fontweight="bold")
    ax.text(0.50, 1.01, "moderate cutoff", transform=ax.get_xaxis_transform(), ha="center", va="bottom", color=GREY, fontsize=8)
    ax.text(0.80, 1.01, "high cutoff", transform=ax.get_xaxis_transform(), ha="center", va="bottom", color="black", fontsize=8)
    ax.set_xlim(0, 1.02)
    ax.set_xlabel("Matched absolute Spearman correlation across 200 patients")
    ax.set_title("Two-seed stability of reduced MOFA-FLEX factors", pad=18)
    style_axis(ax, grid_axis="x")
    fig.tight_layout(rect=(0, 0.10, 1, 1))
    fig.text(
        0.99,
        0.018,
        "Operational labels; not inferential thresholds",
        ha="right",
        va="bottom",
        fontsize=8,
        color=GREY,
    )
    save_figure(fig, final / "fig06_matched_factor_stability.png")

    for name in (
        "fig02_pretrained_r2_heatmap.png",
        "fig04_pretrained_top_weights.png",
        "fig05_seed_factor_correlation.png",
        "fig07_reduced_refit_training_loss.png",
    ):
        shutil.copy2(original / name, final / name)
    print(f"Rendered {len(list(final.glob('*.png')))} final figures to {final}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
