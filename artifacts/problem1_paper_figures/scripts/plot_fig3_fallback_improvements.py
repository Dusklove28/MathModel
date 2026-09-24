from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib
import numpy as np
import pandas as pd
from matplotlib.lines import Line2D

from common import (
    OUTPUT_ROOT,
    PALETTE,
    add_light_x_grid,
    add_panel_labels,
    audit_and_export,
    configure_style,
)


STEM = "fig3_problem1_inherited_fallback_improvements"
SIZE = (6.5, 8.6)
PUBLICATION_EXPORTS = (".svg", ".pdf", ".png", ".tiff")
matplotlib.rcParams.update(
    {"font.family": "sans-serif", "svg.fonttype": "none", "pdf.fonttype": 42}
)
CORE_STYLE = {
    3: (PALETTE["primary"], "o"),
    4: (PALETTE["secondary"], "s"),
    5: (PALETTE["positive"], "^"),
}


def make_figure(data_path: Path):
    configure_style()
    frame = pd.read_csv(data_path).sort_values(
        ["makespan_reduction_pct", "case", "cores"], ascending=[False, True, True]
    )
    if len(frame) != 36:
        raise ValueError(f"图 3 必须包含 36 组严格改善，实际 {len(frame)}")
    if frame.groupby("cores", observed=True).size().to_dict() != {3: 3, 4: 9, 5: 24}:
        raise ValueError("图 3 的改善组核数分布应为 k3=3、k4=9、k5=24")
    if not bool(frame["officially_measured"].all()):
        raise ValueError("图 3 存在非官方实测行")
    if not bool((frame["added_copy_reduction_bytes"] >= 0).all()):
        raise ValueError("图 3 存在额外搬运量增加的改善组")

    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(
        1,
        2,
        figsize=(6.5, 8.6),
        sharey=True,
        layout="constrained",
        gridspec_kw={"width_ratios": [1.03, 1.0]},
    )
    y = np.arange(len(frame))
    labels = frame["case_core_label"].tolist()

    for ax, column in zip(
        axes, ["makespan_reduction_pct", "added_copy_reduction_mib"]
    ):
        values = frame[column].to_numpy()
        ax.hlines(y, 0.0, values, color="#C9C9C9", linewidth=0.75, zorder=1)
        for core, (color, marker) in CORE_STYLE.items():
            mask = frame["cores"].to_numpy() == core
            ax.scatter(
                values[mask],
                y[mask],
                s=31,
                marker=marker,
                facecolor=color,
                edgecolor="#202020",
                linewidth=0.45,
                zorder=3,
                label=f"{core} 核",
            )
        ax.axvline(0.0, color="#555555", linewidth=0.7, zorder=0)
        add_light_x_grid(ax)
        ax.margins(x=0.05)

    axes[0].set_yticks(y)
    axes[0].set_yticklabels(labels, fontsize=6.5)
    axes[0].invert_yaxis()
    axes[0].set_xlabel("Makespan 相对降幅（%）")
    axes[1].set_xlabel("额外搬运减少量（MiB）")
    axes[0].set_title("Makespan 改善", loc="left", pad=6)
    axes[1].set_title("通信开销变化", loc="left", pad=6)
    makespan_upper = max(46.0, float(frame["makespan_reduction_pct"].max()) * 1.08)
    copy_upper = max(110.0, float(frame["added_copy_reduction_mib"].max()) * 1.08)
    # Leave a small negative-side margin so points at zero (or very near zero)
    # remain fully visible instead of being clipped by the plotting boundary.
    axes[0].set_xlim(-0.02 * makespan_upper, makespan_upper)
    axes[1].set_xlim(-0.02 * copy_upper, copy_upper)

    handles = [
        Line2D(
            [],
            [],
            marker=marker,
            linestyle="none",
            markerfacecolor=color,
            markeredgecolor="#202020",
            markersize=5.5,
            label=f"目标 {core} 核",
        )
        for core, (color, marker) in CORE_STYLE.items()
    ]
    axes[1].legend(
        handles=handles,
        loc="lower right",
        ncol=1,
        frameon=False,
    )
    fig.suptitle(
        "问题一：继承回退带来的逐例改善（36 组）",
        x=0.02,
        ha="left",
        fontsize=10.0,
    )
    add_panel_labels(
        fig,
        axes=axes,
        labels=["a", "b"],
        style="nature",
        x_offset_pt=-8.0,
        y_offset_pt=1.0,
    )
    return fig


def main() -> int:
    parser = argparse.ArgumentParser(description="绘制问题一继承回退改善图")
    parser.add_argument(
        "--data",
        type=Path,
        default=OUTPUT_ROOT / "data" / "fig3_fallback_improvements.csv",
    )
    args = parser.parse_args()
    fig = make_figure(args.data.resolve())
    audit_and_export(fig, stem=STEM, size_inches=SIZE, dpi=600)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
