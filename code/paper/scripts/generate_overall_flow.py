"""Render the evidence-accurate Q1–Q3 solution flow (no simulation rerun)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=Path("figures/flow_overall_model"))
    parser.add_argument(
        "--figure-skill",
        type=Path,
        default=Path.home() / ".codex/skills/math-modeling/tools/figure/scripts",
    )
    args = parser.parse_args()
    sys.path.insert(0, str(args.figure_skill))
    from export_figure import export_figure
    from setup_style import setup_style

    setup_style(journal="general", lang="zh", use_sciplots=False, constrained_layout=False)
    fig, ax = plt.subplots(figsize=(7.2, 6.0))
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    ax.set_position([.035, .035, .93, .93])

    def box(x: float, y: float, w: float, h: float, label: str, face: str = "#f3f3f3") -> None:
        ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.012,rounding_size=0.012",
                                     ec="#424242", fc=face, lw=0.9))
        ax.text(x + w / 2, y + h / 2, label, ha="center", va="center", fontsize=7.1,
                linespacing=1.3, color="#202020")

    def arrow(x1: float, y1: float, x2: float, y2: float, label: str | None = None) -> None:
        ax.add_patch(FancyArrowPatch((x1, y1), (x2, y2), arrowstyle="-|>", mutation_scale=9,
                                     color="#555555", lw=1.0, connectionstyle="arc3"))
        if label:
            ax.text((x1 + x2) / 2, (y1 + y2) / 2 + 0.012, label, fontsize=7.0,
                    ha="center", va="bottom", color="#444444")

    box(.32, .91, .36, .065, "输入：100 张算子图、配置与 1—5 核", "#e9e9e9")
    box(.32, .81, .36, .065, "统一图检查与合法子图构造")
    box(.32, .70, .36, .075, "问题一候选：单子图、B0/B1、\n依赖层分窗 B2A")
    box(.055, .53, .32, .09, "场景 A：EFT 排程、官方评估\n及低核优胜方案继承")
    box(.585, .53, .36, .09, "场景 B：六方案交叉评估\n冻结 Stage 1 实际赢家", "#e9e9e9")
    box(.055, .37, .32, .075, "问题一：100 例 × 1—5 核\n官方 Makespan 与搬运")
    box(.41, .35, .25, .105, "问题二：局部核映射\n继承＋Stage 1 回退")
    box(.69, .35, .25, .105, "问题三 G1：冻结 Stage 1\n同方案有/无 L2 配对")
    box(.41, .18, .25, .09, "问题二：B 官方择优\n500 组最终结果")
    box(.69, .18, .25, .09, "问题三 G2：L2 专用排序\n与共享输入映射")
    box(.69, .025, .25, .09, "问题三：官方 2×2 评估\n500 组最终结果")

    arrow(.5, .91, .5, .875)
    arrow(.5, .81, .5, .775)
    arrow(.43, .70, .215, .63)
    arrow(.57, .70, .765, .63)
    arrow(.215, .53, .215, .445)
    arrow(.70, .53, .535, .455)
    arrow(.82, .53, .815, .455)
    arrow(.535, .35, .535, .27)
    arrow(.815, .35, .815, .27)
    arrow(.815, .18, .815, .115)

    ax.text(.215, .327, "A 口径", fontsize=7.3, ha="center", color="#5b5b5b")
    ax.text(.535, .133, "B 口径", fontsize=7.3, ha="center", color="#5b5b5b")
    paths = export_figure(fig, str(args.out), formats=["svg", "png"], dpi=600,
                          size_inches=(7.2, 6.0), grayscale_preview=True, tight=False)
    plt.close(fig)
    print("\n".join(paths))


if __name__ == "__main__":
    main()
