from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib
import numpy as np
import pandas as pd

from common import OUTPUT_ROOT, PALETTE, add_light_y_grid, audit_and_export, configure_style


STEM = "fig1_problem1_average_speedup"
SIZE = (6.3, 3.8)
PUBLICATION_EXPORTS = (".svg", ".pdf", ".png", ".tiff")
matplotlib.rcParams.update(
    {"font.family": "sans-serif", "svg.fonttype": "none", "pdf.fonttype": 42}
)


def make_figure(data_path: Path):
    configure_style()
    frame = pd.read_csv(data_path).sort_values("cores")
    if frame["cores"].tolist() != [1, 2, 3, 4, 5]:
        raise ValueError("图 1 数据必须完整覆盖 1～5 核")
    if not (frame["case_count"] == 100).all():
        raise ValueError("图 1 每个核数必须包含 100 个用例")

    x = frame["cores"].to_numpy()
    before = frame["pre_fallback_arithmetic_mean_speedup"].to_numpy()
    final = frame["final_arithmetic_mean_speedup"].to_numpy()

    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(6.3, 3.8), layout="constrained")
    ax.plot(
        x,
        before,
        color=PALETTE["neutral"],
        linestyle=(0, (4, 2)),
        marker="s",
        markerfacecolor="white",
        markeredgewidth=0.9,
        label="继承回退前（官方实测）",
        zorder=3,
    )
    ax.plot(
        x,
        final,
        color=PALETTE["primary"],
        linestyle="-",
        marker="o",
        markerfacecolor="white",
        markeredgewidth=1.0,
        label="最终方案（官方实测）",
        zorder=4,
    )
    ax.axhline(1.0, color="#8C8C8C", linewidth=0.75, linestyle=(0, (1, 2)), zorder=1)
    add_light_y_grid(ax)

    for core, value in zip(x, final):
        ax.annotate(
            f"{value:.3f}",
            (core, value),
            xytext=(0, 7),
            textcoords="offset points",
            ha="center",
            va="bottom",
            fontsize=7.0,
            color=PALETTE["primary"],
        )
    for core, old, new in zip(x, before, final):
        if not np.isclose(old, new, rtol=0, atol=1e-12):
            ax.annotate(
                f"{old:.3f}",
                (core, old),
                xytext=(0, -8),
                textcoords="offset points",
                ha="center",
                va="top",
                fontsize=6.7,
                color=PALETTE["neutral"],
            )

    ymax = max(3.1, float(final.max()) + 0.22)
    ax.set_xlim(0.75, 5.25)
    ax.set_ylim(0.0, ymax)
    ax.set_xticks(x)
    ax.set_xlabel("核心数 $k$")
    ax.set_ylabel("算术平均加速比（无量纲）")
    ax.set_title("问题一：1～5 核平均加速比", loc="left", pad=6)
    ax.legend(loc="upper left", frameon=False, ncol=1)
    return fig


def main() -> int:
    parser = argparse.ArgumentParser(description="绘制问题一 1～5 核平均加速比")
    parser.add_argument(
        "--data",
        type=Path,
        default=OUTPUT_ROOT / "data" / "fig1_average_speedup.csv",
    )
    args = parser.parse_args()
    fig = make_figure(args.data.resolve())
    audit_and_export(fig, stem=STEM, size_inches=SIZE, dpi=600)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
