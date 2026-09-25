"""Render publication-sized Problem 3 figures from validated source tables.

Run from project root: python plot_q3_figures.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from PIL import Image
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch, Polygon

from validate_q3_figures import ROOT, REPORT, main as validate
from reproduce_q3_figures import locate_skill_root

SKILL_FIG = locate_skill_root() / "tools/figure/scripts"
sys.path.insert(0, str(SKILL_FIG))
from setup_style import setup_style
from export_figure import export_figure
from visual_qa import audit_layout, render_preview

FIG = ROOT / "figures"
QA = FIG / "qa"
RES = ROOT / "results"
BLUE = "#0072B2"
ORANGE = "#E69F00"
GREEN = "#009E73"
PURPLE = "#CC79A7"
GREY = "#666666"


def finish(fig, name: str, size: tuple[float, float]):
    fig.set_size_inches(*size, forward=True)
    QA.mkdir(exist_ok=True)
    render_preview(fig, str(QA / f"{name}_preview.png"), dpi=150)
    issues = audit_layout(fig)
    if any(level == "FAIL" for level, _ in issues):
        raise RuntimeError(f"Visual QA failure {name}: {issues}")
    if issues:
        print("LAYOUT", name, issues)
    export_figure(fig, basename=str(FIG / name), formats=["svg", "png"], size_inches=size, dpi=300, grayscale_preview=False, tight=False)
    with Image.open(FIG / f"{name}.png") as source:
        source.convert("L").save(QA / f"{name}_grayscale.png", dpi=(300, 300))
    plt.close(fig)


def raw_graph_scale(raw):
    fig, ax = plt.subplots(figsize=(7.2, 3.5), layout="constrained")
    ax.hist(raw["ops"], bins=18, color=BLUE, edgecolor="white", linewidth=.5)
    median = raw["ops"].median()
    ax.axvline(median, color=ORANGE, linestyle="--", linewidth=1.3, label=f"中位数 {median:,.0f}")
    ax.set(xlabel="原始操作数（个/例）", ylabel="算例数（例）")
    ax.set_xlim(left=0)
    ax.legend(frameon=False)
    finish(fig, "raw_q3_graph_scale", (7.2, 3.5))


def raw_tensor_size(ts):
    fig, ax = plt.subplots(figsize=(7.2, 3.8), layout="constrained")
    for pos, color, style in (("DDR", BLUE, "-"), ("L1", ORANGE, "--"), ("UB", GREEN, ":")):
        counts = ts.loc[ts["pos"] == pos].groupby("size_bytes").size().sort_index()
        ax.step(counts.index.to_numpy(), counts.cumsum().to_numpy()/counts.sum(), where="post", label=f"{pos} (n={counts.sum():,})", color=color, linestyle=style, linewidth=1.6)
    ax.set_xscale("log")
    ax.set(xlabel="原始 Tensor 大小（B，对数刻度）", ylabel="累计比例")
    ax.set_ylim(0, 1.02)
    ax.legend(frameon=False, loc="lower right")
    finish(fig, "raw_q3_tensor_size", (7.2, 3.8))


def raw_graph_bytes(raw):
    fig, ax = plt.subplots(figsize=(7.2, 4.0), layout="constrained")
    for phase, color, marker, label in (("development", BLUE, "o", "开发 n=12"), ("validation", ORANGE, "s", "验证 n=18"), ("holdout", GREEN, "^", "留出 n=70")):
        d = raw.loc[raw["split"] == phase]
        ax.scatter(d["ops"], d["ddr_tensor_bytes_sum"], s=23, alpha=.72, color=color, marker=marker, label=label, edgecolors="none")
    ax.set(xscale="log", yscale="log", xlabel="原始操作数（个/例，对数刻度）", ylabel="DDR 标记 Tensor 名义大小总和（B，对数刻度）")
    ax.legend(frameon=False)
    finish(fig, "raw_q3_graph_bytes", (7.2, 4.0))


def process_coverage(proc):
    order = ["开发", "验证", "留出"]
    vals = [48, 72, 280]
    fig, ax = plt.subplots(figsize=(7.2, 3.2), layout="constrained")
    y = np.arange(3)
    ax.barh(y, vals, height=.54, color=[BLUE, ORANGE, GREEN], alpha=.78)
    ax.barh(0, 12, height=.54, color="#222222", alpha=.9, label="smoke：开发子集 12 组")
    for yi, value, cases in zip(y, vals, (12, 18, 70)):
        ax.text(value+4, yi, f"{value} 组 / {cases} 例", va="center", fontsize=8)
    ax.set_yticks(y, labels=order)
    ax.invert_yaxis()
    ax.set_xlim(0, 330)
    ax.set_xlabel("预注册 case-core 组数（2–5 核）")
    ax.legend(loc="upper right", frameon=False)
    finish(fig, "process_q3_coverage", (7.2, 3.2))


def process_improvements(proc):
    phases = ["smoke", "development", "validation", "holdout"]
    labels = ["smoke*", "开发", "验证", "留出"]
    x = np.arange(4)
    fig, ax = plt.subplots(figsize=(7.2, 3.7), layout="constrained")
    for branch, offset, color, marker, label in (("ordering", -.12, BLUE, "o", "固定映射排序"), ("mapping", .12, ORANGE, "s", "共享输入映射")):
        d = proc.set_index(["branch", "phase"])
        rates = [d.loc[(branch, p), "strict_improved_groups"]/d.loc[(branch, p), "groups"] for p in phases]
        counts = [int(d.loc[(branch, p), "strict_improved_groups"]) for p in phases]
        ax.scatter(x+offset, rates, s=45, color=color, marker=marker, label=label, zorder=3)
        for xi, rate, count in zip(x+offset, rates, counts):
            ax.annotate(str(count), (xi, rate), xytext=(0, 6 if branch == "ordering" else -12), textcoords="offset points", ha="center", fontsize=7)
    ax.axvspan(-.45, .45, color="#eeeeee", zorder=0)
    ax.set_xticks(x, labels=labels)
    ax.set_ylim(0, .45)
    ax.set_ylabel("严格改善组 / 阶段组数")
    ax.legend(frameon=False, loc="upper right")
    ax.text(.02, .02, "* smoke 与开发集重叠", transform=ax.transAxes, color=GREY, fontsize=7)
    finish(fig, "process_q3_improvements", (7.2, 3.7))


def process_calls(proc):
    phases = ["smoke", "development", "validation", "holdout"]
    labels = ["smoke*", "开发", "验证", "留出"]
    d = proc.set_index(["branch", "phase"])
    x = np.arange(4)
    fig, ax = plt.subplots(figsize=(7.2, 3.7), layout="constrained")
    for branch, offset, color, hatch, label in (("ordering", -.17, BLUE, "", "固定映射排序"), ("mapping", .17, ORANGE, "//", "共享输入映射")):
        values = [int(d.loc[(branch, p), "official_calls_this_invocation"]) for p in phases]
        bars = ax.bar(x+offset, values, width=.32, color=color, edgecolor="#333333", linewidth=.4, hatch=hatch, label=label)
        ax.bar_label(bars, padding=2, fontsize=7)
    ax.set_xticks(x, labels=labels)
    ax.set_ylabel("本阶段官方候选评估调用（次）")
    ax.set_ylim(0, 440)
    ax.legend(frameon=False, loc="upper left")
    ax.text(.02, .75, "* smoke 组属于开发集；调用数按 invocation 计", transform=ax.transAxes, color=GREY, fontsize=7)
    finish(fig, "process_q3_calls", (7.2, 3.7))


def scaling(curve):
    spec = [
        ("mean_of_case_parallel_speedups_baseline_no_l2_vs_k1", "冻结方案 / 无 L2", GREY, "--", "o"),
        ("mean_of_case_parallel_speedups_baseline_l2_vs_k1", "冻结方案 / 有 L2", BLUE, "-", "s"),
        ("mean_of_case_parallel_speedups_final_no_l2_vs_k1", "最终方案 / 无 L2", ORANGE, "-.", "^"),
        ("mean_of_case_parallel_speedups_final_l2_vs_k1", "最终方案 / 有 L2", GREEN, "-", "D"),
    ]
    fig, ax = plt.subplots(figsize=(7.2, 4.2), layout="constrained")
    for field, label, color, line, marker in spec:
        ax.plot(curve["cores"], curve[field], label=label, color=color, linestyle=line, marker=marker, markersize=4, linewidth=1.5)
    ax.set_xticks([1,2,3,4,5])
    ax.set(xlabel="请求核数 k", ylabel="逐例并行加速比的均值（×）")
    ax.set_ylim(0.9, 3.6)
    ax.legend(frameon=False, loc="upper left", ncol=2)
    finish(fig, "result_q3_scaling", (7.2, 4.2))


def effects(summary):
    curves = summary["curves"]
    x = np.arange(1,6)
    fig, axes = plt.subplots(1, 2, figsize=(7.2, 4.0), layout="constrained")
    panels = [
        (axes[0], [("hardware_speedup_baseline", "冻结方案", BLUE, "o", -.08), ("hardware_speedup_final", "最终方案", ORANGE, "s", .08)], "硬件收益：无 L2 / 有 L2"),
        (axes[1], [("no_l2_plan_tuning_speedup", "无 L2", GREY, "^", -.08), ("l2_plan_tuning_speedup", "有 L2", GREEN, "D", .08)], "方案收益：原方案 / 最终方案"),
    ]
    for ax, specs, title in panels:
        for suffix, label, color, marker, offset in specs:
            mean = np.array([c[f"mean_of_case_ratios_{suffix}"] for c in curves])
            ci = np.array([c[f"bootstrap_95ci_mean_{suffix}"] for c in curves])
            ax.errorbar(x+offset, mean, yerr=np.vstack((mean-ci[:,0], ci[:,1]-mean)), fmt=marker, linestyle="none", color=color, capsize=2, markersize=4, elinewidth=1, label=label)
        ax.axhline(1, color="#888888", linewidth=.8, linestyle=":")
        ax.set_xticks(x)
        ax.set_xlabel("请求核数 k")
        ax.set_title(title, fontsize=8)
        ax.legend(frameon=False, loc="upper left")
    axes[0].set_ylabel("逐例配对比值的均值（×）")
    axes[0].set_ylim(.99, 1.055)
    axes[1].set_ylim(.99, 1.035)
    axes[0].text(.02, .02, "a", transform=axes[0].transAxes, fontweight="bold")
    axes[1].text(.02, .02, "b", transform=axes[1].transAxes, fontweight="bold")
    finish(fig, "result_q3_effects", (7.2, 4.0))


def holdout(effects_df):
    data = effects_df.loc[(effects_df["split"] == "holdout") & (effects_df["cores"] > 1)]
    rng = np.random.default_rng(20260925)
    fig, ax = plt.subplots(figsize=(7.2, 4.0), layout="constrained")
    box_data = [data.loc[data["cores"] == k, "schedule_l2_ratio"].to_numpy() for k in (2,3,4,5)]
    ax.boxplot(box_data, positions=(2,3,4,5), widths=.42, showfliers=False, patch_artist=True, medianprops={"color":"#222222"}, boxprops={"facecolor":"#d9e9f0","edgecolor":BLUE}, whiskerprops={"color":BLUE}, capprops={"color":BLUE})
    for k, values in zip((2,3,4,5), box_data):
        jitter = rng.uniform(-.17,.17,len(values))
        gain = values > 1
        ax.scatter(k+jitter[gain], values[gain], s=12, facecolor=GREEN, edgecolor="none", alpha=.6, zorder=3)
        ax.scatter(k+jitter[~gain], values[~gain], s=17, facecolor="white", edgecolor="#222222", linewidth=.65, alpha=.8, zorder=4)
        ax.text(k, 1.195, f"{gain.sum()}/70", ha="center", fontsize=7)
    ax.axhline(1, color="#666666", linestyle=":", linewidth=.8)
    ax.set(xlabel="请求核数 k", ylabel="留出集逐例 L2 下方案收益（×）")
    ax.set_xticks([2,3,4,5])
    ax.set_ylim(.996, 1.21)
    finish(fig, "result_q3_holdout", (7.2, 4.0))


def flow():
    fig, ax = plt.subplots(figsize=(7.2, 6.0), layout="constrained")
    ax.set(xlim=(0,10), ylim=(0,10))
    ax.axis("off")
    nodes = [
        (9.25, "输入图与官方语义", "input"),
        (8.20, "冻结问题二方案", "start"),
        (7.15, "预注册划分冻结（收益检查前）", "process"),
        (6.10, "G1 同方案无/有 L2 配对评估", "process"),
        (5.05, "固定映射：合法排序候选", "process"),
        (4.00, "共享输入：合法映射候选", "process"),
        (2.95, "官方 makespan 择优与回退", "choice"),
        (1.90, "分阶段验证与留出评估", "process"),
        (.85, "2×2 消融与 1–5 核曲线", "output"),
    ]
    for y, label, kind in nodes:
        w, h = (6.8,.68) if kind != "choice" else (6.8,.72)
        x0 = (10-w)/2
        if kind == "input":
            patch = Polygon([(x0+.25,y-h/2),(x0+w,y-h/2),(x0+w-.25,y+h/2),(x0,y+h/2)], closed=True, edgecolor=BLUE, facecolor="#e6f2f8", linewidth=1)
        elif kind == "choice":
            patch = Polygon([(5,y+h/2),(x0+w,y),(5,y-h/2),(x0,y)], closed=True, edgecolor=ORANGE, facecolor="#fff2dd", linewidth=1)
        else:
            patch = FancyBboxPatch((x0,y-h/2),w,h, boxstyle="round,pad=0.02,rounding_size=0.16" if kind in ("start","output") else "square,pad=0.02", edgecolor=GREEN if kind=="output" else BLUE, facecolor="#e4f3eb" if kind=="output" else "#f5f8fa", linewidth=1)
        ax.add_patch(patch)
        ax.text(5,y,label,ha="center",va="center",fontsize=8)
    for (y1,_,_), (y2,_,_) in zip(nodes[:-1],nodes[1:]):
        ax.add_patch(FancyArrowPatch((5,y1-.38),(5,y2+.38),arrowstyle="-|>",mutation_scale=9,linewidth=.9,color="#444444"))
    finish(fig, "flow_q3_model", (7.2, 6.0))


def main():
    validate()
    FIG.mkdir(exist_ok=True)
    setup_style(journal="general", lang="zh", use_sciplots=False)
    raw = pd.read_csv(RES / "q3_raw_case_graph.csv")
    ts = pd.read_csv(RES / "q3_raw_tensors.csv", usecols=["pos","size_bytes"])
    proc = pd.read_csv(RES / "q3_process_invocations.csv")
    effect = pd.read_csv(RES / "q3_effects_case_core.csv")
    curve = pd.read_csv(RES / "q3_scaling_curve_source.csv")
    import json
    with (REPORT / "summary.json").open(encoding="utf-8") as f:
        summary = json.load(f)
    raw_graph_scale(raw)
    raw_tensor_size(ts)
    raw_graph_bytes(raw)
    process_coverage(proc)
    process_improvements(proc)
    process_calls(proc)
    scaling(curve)
    effects(summary)
    holdout(effect)
    flow()
    print("RENDERED 10 Q3 figures")


if __name__ == "__main__":
    main()
