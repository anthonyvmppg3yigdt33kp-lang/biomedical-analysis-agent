#!/usr/bin/env python3
"""Build scientifically bounded sample-level composition figures from cell annotations."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mtick
import numpy as np
import pandas as pd
import seaborn as sns


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def plot_stacked(proportions: pd.DataFrame, output: Path) -> None:
    sample_ids = proportions.index.tolist()
    cell_types = proportions.columns.tolist()
    colors = plt.get_cmap("tab20")(np.linspace(0, 1, max(20, len(cell_types))))[: len(cell_types)]
    figure, axis = plt.subplots(figsize=(10.6, 6.3))
    figure.subplots_adjust(left=0.10, right=0.78, bottom=0.12, top=0.84)
    bottom = np.zeros(len(sample_ids), dtype=float)
    for cell_type, color in zip(cell_types, colors):
        values = proportions[cell_type].to_numpy(dtype=float) * 100.0
        axis.bar(sample_ids, values, bottom=bottom, width=0.72, label=cell_type, color=color, edgecolor="white", linewidth=0.25)
        bottom += values
    axis.set_ylim(0, 100)
    axis.yaxis.set_major_formatter(mtick.PercentFormatter(xmax=100, decimals=0))
    axis.set_xlabel("Sample")
    axis.set_ylabel("Within-sample cell composition")
    figure.text(0.10, 0.955, "Cell-type composition by sample (descriptive)", ha="left", va="top", fontsize=14, weight="bold")
    figure.text(0.10, 0.915, "Each bar sums to 100%; cells are not independent biological replicates.", ha="left", va="top", fontsize=9)
    axis.spines[["top", "right"]].set_visible(False)
    axis.grid(axis="y", color="#d9d9d9", linewidth=0.6, alpha=0.7)
    axis.set_axisbelow(True)
    axis.legend(title="Cell type", bbox_to_anchor=(1.015, 1), loc="upper left", frameon=False, ncol=1, fontsize=8, title_fontsize=9)
    figure.savefig(output, dpi=240, bbox_inches="tight", facecolor="white", metadata={"Software": "biomedical-analysis-agent"})
    plt.close(figure)


def plot_dot(proportions: pd.DataFrame, output: Path) -> None:
    long = proportions.rename_axis("sample_id").reset_index().melt(id_vars="sample_id", var_name="cell_type", value_name="proportion")
    long["percent"] = long["proportion"] * 100.0
    sample_order = proportions.index.tolist()
    cell_order = proportions.columns.tolist()[::-1]
    xmap = {value: index for index, value in enumerate(sample_order)}
    ymap = {value: index for index, value in enumerate(cell_order)}
    figure, axis = plt.subplots(figsize=(8.7, 7.3))
    figure.subplots_adjust(left=0.19, right=0.82, bottom=0.11, top=0.86)
    points = axis.scatter(
        long["sample_id"].map(xmap),
        long["cell_type"].map(ymap),
        s=12 + 7 * long["percent"],
        c=long["percent"],
        cmap="viridis",
        vmin=0,
        vmax=float(long["percent"].max()),
        edgecolors="#222222",
        linewidths=0.25,
        alpha=0.9,
    )
    axis.set_xticks(range(len(sample_order)), sample_order)
    axis.set_yticks(range(len(cell_order)), cell_order)
    axis.set_xlabel("Sample")
    axis.set_ylabel("Cell type")
    figure.text(0.19, 0.955, "Sample-level composition matrix", ha="left", va="top", fontsize=14, weight="bold")
    figure.text(0.19, 0.918, "Point area and color both encode within-sample percentage.", ha="left", va="top", fontsize=9)
    axis.grid(color="#e8e8e8", linewidth=0.6)
    axis.set_axisbelow(True)
    axis.spines[["top", "right"]].set_visible(False)
    colorbar = figure.colorbar(points, ax=axis, pad=0.02, shrink=0.78)
    colorbar.set_label("Within-sample percentage")
    figure.savefig(output, dpi=240, bbox_inches="tight", facecolor="white", metadata={"Software": "biomedical-analysis-agent"})
    plt.close(figure)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--sample-column", default="sample_id")
    parser.add_argument("--cell-type-column", default="cell_type")
    args = parser.parse_args()
    input_path = args.input.resolve()
    output = args.output_dir.resolve()
    tables = output / "tables"
    figures = output / "figures"
    reports = output / "reports"
    for directory in (tables, figures, reports):
        directory.mkdir(parents=True, exist_ok=True)
    observed_sha = sha256_file(input_path)
    if observed_sha != args.expected_sha256:
        raise RuntimeError(f"Input SHA-256 mismatch: {observed_sha}")
    frame = pd.read_csv(input_path)
    if frame.columns[0].startswith("Unnamed"):
        frame = frame.rename(columns={frame.columns[0]: "cell_id"})
    required = [args.sample_column, args.cell_type_column]
    missing_columns = [column for column in required if column not in frame]
    if missing_columns:
        raise RuntimeError(f"Missing required columns: {missing_columns}")
    missing_required = frame[required].isna().sum().to_dict()
    if any(int(value) for value in missing_required.values()):
        raise RuntimeError(f"Required metadata contain missing values: {missing_required}")
    counts = pd.crosstab(frame[args.sample_column], frame[args.cell_type_column], dropna=False)
    overall_order = frame[args.cell_type_column].value_counts().index.tolist()
    counts = counts.reindex(columns=overall_order, fill_value=0).sort_index()
    proportions = counts.div(counts.sum(axis=1), axis=0)
    sums = proportions.sum(axis=1)
    max_error = float((sums - 1.0).abs().max())
    if max_error > 1e-12:
        raise RuntimeError(f"Composition denominator check failed: {max_error}")
    counts.to_csv(tables / "composition-counts.tsv", sep="\t")
    proportions.to_csv(tables / "composition-proportions.tsv", sep="\t", float_format="%.12g")
    category_ledger = (
        frame.groupby([args.sample_column, args.cell_type_column], observed=True)
        .size()
        .rename("cell_count")
        .reset_index()
    )
    category_ledger.to_csv(tables / "category-ledger.tsv", sep="\t", index=False)
    profile = {
        "schema_version": "1.0",
        "input_sha256": observed_sha,
        "rows": int(frame.shape[0]),
        "columns": int(frame.shape[1]),
        "sample_column": args.sample_column,
        "cell_type_column": args.cell_type_column,
        "sample_count": int(counts.shape[0]),
        "cell_type_count": int(counts.shape[1]),
        "sample_cell_counts": {str(key): int(value) for key, value in frame[args.sample_column].value_counts().sort_index().items()},
        "cell_type_counts": {str(key): int(value) for key, value in frame[args.cell_type_column].value_counts().items()},
        "missing_required": {str(key): int(value) for key, value in missing_required.items()},
        "max_composition_sum_error": max_error,
        "statistical_unit": "sample is the descriptive grouping unit; donor inference requires a verified donor-sample mapping and independence; cell is a nested measurement",
    }
    write_json(tables / "input-profile.json", profile)
    plot_stacked(proportions, figures / "composition-stacked-bars.png")
    plot_dot(proportions, figures / "composition-dot-matrix.png")
    versions = {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "pandas": pd.__version__,
        "matplotlib": matplotlib.__version__,
        "seaborn": sns.__version__,
        "numpy": np.__version__,
    }
    write_json(reports / "environment-versions.json", versions)
    qa = {
        "ok": True,
        "input_hash_verified": True,
        "required_metadata_complete": True,
        "sample_count": int(counts.shape[0]),
        "cell_type_count": int(counts.shape[1]),
        "all_sample_proportions_sum_to_one": True,
        "max_sum_error": max_error,
        "inferential_test_performed": False,
        "figure_notes_derived_from_input_profile": True,
        "known_negative_controls": [
            "Reject any stacked bar whose segments sum above 100%.",
            "Reject axes that label cell counts as a percentage or vice versa.",
            "Reject pie labels that overlap or hide rare categories.",
        ],
        "remaining_visual_review": "Open both native PNGs and document legibility, color discrimination and claim boundaries.",
    }
    write_json(reports / "qa-machine.json", qa)
    sample_count = int(profile["sample_count"])
    measured_cell_count = int(profile["rows"])
    (reports / "FIGURE_NOTES.md").write_text(
        "# Figure notes\n\n"
        f"Research question: What is the descriptive within-sample cell-type composition across the {sample_count:,} observed `{args.sample_column}` groups?\n\n"
        f"Data/unit: {measured_cell_count:,} measured cells across {sample_count:,} descriptive sample groups; proportions are computed within each sample. Sample is only a descriptive grouping unit here. Donor-level inference would require a verified donor-sample mapping, donor independence and a registered contrast; none is established, so no inferential test is performed.\n\n"
        "Directly supported: sample-specific descriptive composition and the presence of rare categories.\n\n"
        "Not supported: disease effects, donor-general population estimates, cell-level p-values, causality, or clinical conclusions.\n\n"
        "Native visual review: pending.\n",
        encoding="utf-8",
    )
    artifacts = []
    for path in sorted(item for item in output.rglob("*") if item.is_file()):
        artifacts.append({"relative_path": path.relative_to(output).as_posix(), "size": path.stat().st_size, "sha256": sha256_file(path)})
    write_json(reports / "artifact-index.json", {"schema_version": "1.0", "artifacts": artifacts})
    print(json.dumps({"ok": True, "output": str(output), "profile": profile, "versions": versions}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
