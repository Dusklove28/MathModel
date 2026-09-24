from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib
import numpy as np
import pandas as pd
from matplotlib.lines import Line2D

from common import OUTPUT_ROOT, PALETTE, add_light_y_grid, audit_and_export, configure_style


STEM = "fig2_problem1_case_speedup_distribution"
SIZE = (6.3, 4.1)
PUBLICATION_EXPORTS = (".svg", ".pdf", ".png", ".tiff")
matplotlib.rcParams.update(
    {"font.family": "sans-serif", "svg.fonttype": "none", "pdf.fonttype": 42}
)


def make_figure(data_path: Path):
    configure_style()
    frame = pd.read_csv(data_path)
    counts = frame.groupby("cores", observed=True).size().to_dict()
    if counts != {2: 100, 3: 100, 4: 100, 5: 100}:
        raise ValueError(f"图 2 各核数样本量异常：{counts}")
    if not bool(frame["officially_measured"].all()):
        raise ValueError("图 2 存在非官方实测行")

    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(6.3, 4.1), layout="constrained")
    cores = [2, 3, 4, 5]
    values = [frame.loc[frame["cores"] == core, "final_speedup"].to_numpy() for core in cores]
    box = ax.boxplot(
        values,
        positions=cores,
        widths=0.44,
        patch_artist=True,
        showfliers=False,
        whis=1.5,
        medianprops={"color": "#111111", "linewidth": 1.25},
        whiskerprops={"color": "#4D4D4D", "linewidth": 0.9},
        capprops={"color": "#4D4D4D", "linewidth": 0.9},
        boxprops={"edgecolor": PALETTE["primary"], "linewidth": 1.0},
    )
    for patch in box["boxes"]:
        patch.set_facecolor("#CFE5F3")
        patch.set_alpha(0.72)

    for core, group in zip(cores, values):
        n = len(group)
        order = (np.arange(n) * 37) % n
        jitter = -0.17 + 0.34 * order / max(n - 1, 1)
        ax.scatter(
            np.full(len(group), core) + jitter,
            group,
            s=11,
            color=PALETTE["primary"],
            alpha=0.43,
            edgecolors="none",
            rasterized=False,
            zorder=2,
        )
        ax.scatter(
            core,
            float(np.mean(group)),
            s=36,
            marker="D",
            facecolor="#111111",
            edgecolor="white",
            linewidth=0.6,
            zorder=5,
        )

    ax.axhline(1.0, color="#777777", linewidth=0.8, linestyle=(0, (2, 2)), zorder=1)
    add_light_y_grid(ax)
    ax.set_xlim(1.55, 5.45)
    ax.set_ylim(0.0, max(5.7, float(frame["final_speedup"].max()) + 0.25))
    ax.set_xticks(cores)
    ax.set_xlabel("核心数 $k$", fontsize=8.5)
    ax.set_ylabel("逐例加速比 $S_{i,k}$（无量纲）", fontsize=8.5)
    ax.set_title("问题一：2～5 核逐例加速比分布", loc="left", pad=6, fontsize=9.5)
    handles = [
        Line2D([], [], marker="o", linestyle="none", color=PALETTE["primary"], alpha=0.55, markersize=4, label="单个测试用例"),
        Line2D([], [], marker="D", linestyle="none", markerfacecolor="#111111", markeredgecolor="white", markersize=5, label="算术平均"),
        Line2D([], [], color="#777777", linestyle=(0, (2, 2)), linewidth=0.8, label="$S=1$ 基线"),
    ]
    ax.legend(handles=handles, loc="upper left", frameon=False, ncol=1)
    return fig


def main() -> int:
    parser = argparse.ArgumentParser(description="绘制问题一逐例加速比分布")
    parser.add_argument(
        "--data",
        type=Path,
        default=OUTPUT_ROOT / "data" / "fig2_case_speedup_distribution.csv",
    )
    args = parser.parse_args()
    fig = make_figure(args.data.resolve())
    audit_and_export(fig, stem=STEM, size_inches=SIZE, dpi=600)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
