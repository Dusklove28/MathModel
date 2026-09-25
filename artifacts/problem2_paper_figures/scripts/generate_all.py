#!/usr/bin/env python3
"""Rebuild the frozen Problem 2 paper figures from official Scene B results.

The raw experiment directory is read only.  A compact, committed data snapshot
lets the figures be regenerated without copying its multi-gigabyte plan cache.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib import patches
from matplotlib.ticker import FuncFormatter
from PIL import Image


HERE = Path(__file__).resolve().parent.parent
PROJECT = HERE.parents[1]
DATA = HERE / "data"
FIGURES = HERE / "figures"
QA = HERE / "qa"
SOURCE = PROJECT / "artifacts" / "problem2_final_v1"
SPLIT = PROJECT / "problem2_preregistered_split.json"
FIGURE_SIZE = (6.3, 3.9)
FLOW_SIZE = (6.3, 5.7)
DPI = 600
SEED = 20260925

sys.path.insert(0, str(PROJECT))
from utils.plot_style import PALETTE  # noqa: E402


def skill_tools():
    root = Path(os.environ.get(
        "MATH_MODELING_SKILL_ROOT",
        Path.home() / ".codex" / "skills" / "math-modeling",
    ))
    scripts = root / "tools" / "figure" / "scripts"
    if not scripts.is_dir():
        raise FileNotFoundError(
            f"科研可视化 SKILL 未找到：{scripts}。请设置 MATH_MODELING_SKILL_ROOT。"
        )
    sys.path.insert(0, str(scripts))
    from export_figure import export_figure
    from setup_style import setup_style
    from visual_qa import audit_layout, print_report, render_preview
    return export_figure, setup_style, audit_layout, print_report, render_preview


EXPORT, SETUP_STYLE, AUDIT_LAYOUT, PRINT_REPORT, RENDER_PREVIEW = skill_tools()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def percentile(values, p: float) -> float:
    return float(np.percentile(np.asarray(values, dtype=float), p))


def load_snapshot() -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    return (
        read_csv(DATA / "selected_compact.csv"),
        read_csv(DATA / "graph_features.csv"),
        read_csv(DATA / "tensor_position.csv"),
        read_csv(DATA / "candidate_effects.csv"),
    )


def prepare() -> dict:
    selected = read_csv(SOURCE / "selected_results.csv")
    candidates = read_csv(SOURCE / "candidate_results.csv")
    split = json.loads(SPLIT.read_text(encoding="utf-8"))
    split_of = {
        case: name
        for name, key in (
            ("development", "development_cases"),
            ("validation", "validation_cases"),
            ("holdout", "holdout_cases"),
        )
        for case in split[key]
    }
    keys = [(r["case"], int(r["cores"])) for r in selected]
    assert len(selected) == len(set(keys)) == 500
    assert len(set(case for case, _ in keys)) == 100
    assert len(candidates) == 5300
    assert all(r["status"] == "success" and r["candidate_failures"] == "0" for r in selected)
    assert all(r["legal"] == "True" and not r["error_type"] for r in candidates)
    assert set(split_of) == {case for case, _ in keys}

    compact = []
    baseline = {}
    for r in selected:
        case, core = r["case"], int(r["cores"])
        final = int(r["makespan_cycles"])
        old = int(r["stage1_makespan_cycles"])
        copy = int(r["added_copy_bytes"])
        old_copy = int(r["stage1_added_copy_bytes"])
        assert (final, copy) <= (old, old_copy)
        row = {
            "case": case, "cores": core, "split": split_of[case],
            "winner": r["winner"], "winner_family": r["winner_family"],
            "makespan_cycles": final, "stage1_makespan_cycles": old,
            "added_copy_bytes": copy, "stage1_added_copy_bytes": old_copy,
            "spill_added_copy_bytes": int(r["spill_added_copy_bytes"]),
            "stage1_spill_added_copy_bytes": int(r["spill_added_copy_bytes"]) - int(r["spill_delta_vs_stage1"]),
            "cross_task_traffic_bytes": int(r["cross_task_traffic_bytes"]),
            "cross_core_transfer_count": int(r["cross_core_transfer_count"]),
            "used_core_count": int(r["used_core_count"]),
            "group_wall_seconds": float(r["group_wall_seconds"]),
            "gain_pct": 100.0 * (old - final) / old,
            "copy_delta_mib": (copy - old_copy) / 2**20,
            "spill_delta_mib": int(r["spill_delta_vs_stage1"]) / 2**20,
        }
        compact.append(row)
        baseline[(case, core)] = (old, old_copy)
    compact.sort(key=lambda r: (r["case"], r["cores"]))
    single = {r["case"]: r["makespan_cycles"] for r in compact if r["cores"] == 1}
    assert len(single) == 100
    for r in compact:
        r["speedup"] = single[r["case"]] / r["makespan_cycles"]
    write_csv(DATA / "selected_compact.csv", compact, list(compact[0]))

    graph_rows = []
    tensor_rows = []
    graph_hashes = {}
    for case in sorted(split_of):
        path = PROJECT / "data" / f"{case}.json"
        graph = json.loads(path.read_text(encoding="utf-8"))
        assert set(graph) == {"ops", "tensors", "edges"}
        tensors = graph["tensors"]
        ops = graph["ops"]
        graph_rows.append({
            "case": case, "split": split_of[case],
            "noncopy_ops": sum(not str(op.get("op", "")).startswith("COPY") for op in ops),
            "copy_ops": sum(str(op.get("op", "")).startswith("COPY") for op in ops),
            "tensor_count": len(tensors), "edge_count": len(graph["edges"]),
            "ddr_tensor_bytes": sum(int(t["size"]) for t in tensors if t["pos"] == "DDR"),
            "total_tensor_bytes": sum(int(t["size"]) for t in tensors),
        })
        for pos in ("DDR", "L1", "UB"):
            sizes = [int(t["size"]) for t in tensors if t["pos"] == pos]
            if sizes:
                tensor_rows.append({
                    "case": case, "position": pos, "n_tensors": len(sizes),
                    "median_tensor_kib": statistics.median(sizes) / 1024,
                    "total_tensor_mib": sum(sizes) / 2**20,
                })
        graph_hashes[f"data/{case}.json"] = sha256(path)
    write_csv(DATA / "graph_features.csv", graph_rows, list(graph_rows[0]))
    write_csv(DATA / "tensor_position.csv", tensor_rows, list(tensor_rows[0]))

    effects = []
    coverage = []
    for r in candidates:
        case, core = r["case"], int(r["cores"])
        family = r["family"]
        if r["candidate"].startswith("map_locality@"):
            makespan = int(r["makespan_cycles"])
            old, _ = baseline[(case, core)]
            effects.append({
                "case": case, "cores": core, "candidate": r["candidate"],
                "gain_pct": 100 * (old - makespan) / old,
                "relation": "better" if makespan < old else ("tie" if makespan == old else "worse"),
            })
        if family == "verified_stage1_global_fallback":
            cat = "回退"
        elif family == "stage1_literal_candidate":
            cat = "single"
        elif family == "stage1_fixed_partition_original_mapping":
            cat = "原映射"
        elif family == "scene_b_fixed_partition_mapping":
            cat = "新映射"
        elif family == "recursive_final_winner_inheritance":
            cat = "递归继承"
        else:
            raise ValueError(f"unknown family {family}")
        coverage.append({"case": case, "cores": core, "category": cat})
    assert len(effects) == 2000
    write_csv(DATA / "candidate_effects.csv", effects, list(effects[0]))
    counts = Counter((r["cores"], r["category"]) for r in coverage)
    count_rows = [
        {"cores": k, "category": cat, "logical_candidates": counts[(k, cat)]}
        for k in range(1, 6)
        for cat in ("回退", "single", "原映射", "新映射", "递归继承")
    ]
    write_csv(DATA / "candidate_coverage.csv", count_rows, list(count_rows[0]))

    sources = {
        "run_identity_sha256": json.loads((SOURCE / "run_identity.json").read_text(encoding="utf-8"))["run_identity_sha256"],
        "source_files": {
            f"artifacts/problem2_final_v1/{name}": sha256(SOURCE / name)
            for name in ("selected_results.csv", "candidate_results.csv", "aggregate_by_core.csv", "summary.json", "run_identity.json")
        },
        "split_sha256": sha256(SPLIT),
        "config_sha256": sha256(PROJECT / "data" / "config.txt"),
        "graph_sha256": graph_hashes,
    }
    (DATA / "source_hashes.json").write_text(json.dumps(sources, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return sources


def style() -> None:
    SETUP_STYLE(journal="general", lang="zh", use_sciplots=False,
                serif_for_zh=False, constrained_layout=True)
    plt.rcParams.update({
        "font.size": 8.0, "axes.labelsize": 8.3, "axes.titlesize": 9,
        "xtick.labelsize": 7.3, "ytick.labelsize": 7.3,
        "legend.fontsize": 7.3, "axes.grid": False,
        "axes.unicode_minus": False, "svg.fonttype": "none", "pdf.fonttype": 42,
        "svg.hashsalt": "problem2-paper-figures-2026",
    })


def light_grid(ax, axis="y"):
    ax.grid(axis=axis, color="#D8DEE5", linewidth=0.55, linestyle=(0, (2, 2)))
    ax.set_axisbelow(True)


def export(fig, stem: str, size=FIGURE_SIZE) -> dict:
    FIGURES.mkdir(parents=True, exist_ok=True)
    (QA / "previews").mkdir(parents=True, exist_ok=True)
    (QA / "programmatic").mkdir(parents=True, exist_ok=True)
    fig.set_size_inches(*size)
    fig.canvas.draw()
    preview = QA / "previews" / f"{stem}_preview.png"
    RENDER_PREVIEW(fig, str(preview), dpi=180)
    issues = AUDIT_LAYOUT(fig)
    verdict = PRINT_REPORT(issues)
    qa = {
        "figure": stem, "size_inches": list(size), "dpi": DPI,
        "layout_verdict": verdict,
        "issues": [{"severity": a, "message": b} for a, b in issues],
    }
    if verdict != "PASS":
        raise RuntimeError(f"{stem}: layout {verdict}; {issues}")
    base = FIGURES / stem
    outputs = EXPORT(fig, str(base), formats=["svg", "png"],
                     size_inches=size, dpi=DPI,
                     grayscale_preview=False, tight=False)
    svg = base.with_suffix(".svg")
    svg.write_text("\n".join(line.rstrip() for line in svg.read_text(encoding="utf-8").splitlines())
                   + "\n", encoding="utf-8")
    gray = QA / "grayscale" / f"{stem}_grayscale.png"
    gray.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(base.with_suffix(".png")) as image:
        image.convert("L").save(gray, dpi=(DPI, DPI))
    outputs.append(str(gray))
    qa["files"] = {Path(p).name: sha256(Path(p)) for p in outputs}
    (QA / "programmatic" / f"{stem}.json").write_text(
        json.dumps(qa, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    plt.close(fig)
    return qa


def jitter(case: str, width: float = 0.15) -> float:
    number = int(case[-3:])
    return width * (2 * ((number * 73) % 101) / 100 - 1)


def raw_graph_scale(graphs):
    values = np.asarray([int(r["noncopy_ops"]) for r in graphs])
    fig, ax = plt.subplots(figsize=FIGURE_SIZE, layout="constrained")
    bins = np.geomspace(values.min() * .9, values.max() * 1.1, 14)
    ax.hist(values, bins=bins, color=PALETTE["primary"], edgecolor="white", linewidth=.5)
    ax.axvline(np.median(values), color=PALETTE["contrast"], ls="--", lw=1.2,
               label=f"中位数 {np.median(values):,.1f}")
    ax.set_xscale("log")
    ax.set_xlabel("原图非 COPY 算子数（对数刻度）")
    ax.set_ylabel("用例数")
    ax.legend(frameon=False)
    light_grid(ax)
    return export(fig, "raw_q2_graph_scale")


def raw_tensor_footprint(tensors):
    fig, ax = plt.subplots(figsize=FIGURE_SIZE, layout="constrained")
    positions = ["DDR", "L1", "UB"]
    colors = [PALETTE["primary"], PALETTE["secondary"], PALETTE["positive"]]
    values = [[float(r["median_tensor_kib"]) for r in tensors if r["position"] == pos]
              for pos in positions]
    box = ax.boxplot(values, positions=[1, 2, 3], widths=.43, patch_artist=True,
                     showfliers=False, medianprops={"color": "#222222", "linewidth": 1.2})
    for patch, color in zip(box["boxes"], colors):
        patch.set_facecolor(color)
        patch.set_alpha(.35)
        patch.set_edgecolor(color)
    for x, pos, color in zip([1, 2, 3], positions, colors):
        points = [r for r in tensors if r["position"] == pos]
        ax.scatter([x + jitter(r["case"], .18) for r in points],
                   [float(r["median_tensor_kib"]) for r in points],
                   s=9, color=color, edgecolor="none", alpha=.55, rasterized=False)
    ax.set_xticks([1, 2, 3], [f"{p}\n(n={len(v)})" for p, v in zip(positions, values)])
    ax.set_yscale("log")
    ax.set_yticks([.01, .1, 1, 10])
    ax.yaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value:g}"))
    ax.set_ylabel("每例张量大小中位数（KiB，对数刻度）")
    ax.set_xlabel("原图张量存储位置")
    light_grid(ax)
    return export(fig, "raw_q2_tensor_footprint")


def raw_graph_io(graphs):
    fig, ax = plt.subplots(figsize=FIGURE_SIZE, layout="constrained")
    x = np.asarray([int(r["noncopy_ops"]) for r in graphs])
    y = np.asarray([int(r["ddr_tensor_bytes"]) / 2**20 for r in graphs])
    ax.scatter(x, y, color=PALETTE["primary"], s=27, alpha=.73,
               linewidths=.25, edgecolors="#1A1A1A")
    ax.set_xscale("log"); ax.set_yscale("log")
    ax.set_yticks([.1, 1, 10])
    ax.yaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value:g}"))
    ax.set_xlabel("原图非 COPY 算子数（对数刻度）")
    ax.set_ylabel("原图 DDR 张量字节量（MiB，对数刻度）")
    light_grid(ax)
    return export(fig, "raw_q2_graph_io")


def process_candidate_coverage():
    rows = read_csv(DATA / "candidate_coverage.csv")
    cats = ["回退", "single", "原映射", "新映射", "递归继承"]
    colors = [PALETTE["neutral"], PALETTE["dark"], PALETTE["sky"],
              PALETTE["primary"], PALETTE["secondary"]]
    counts = {(int(r["cores"]), r["category"]): int(r["logical_candidates"]) for r in rows}
    fig, ax = plt.subplots(figsize=FIGURE_SIZE, layout="constrained")
    bottoms = np.zeros(5)
    for cat, color in zip(cats, colors):
        values = np.asarray([counts[(core, cat)] for core in range(1, 6)])
        ax.bar(range(1, 6), values, bottom=bottoms, color=color, label=cat,
               width=.62, edgecolor="white", linewidth=.4)
        bottoms += values
    for core, total in enumerate(bottoms, start=1):
        ax.text(core, total + 24, str(int(total)), ha="center", va="bottom", fontsize=7.5)
    ax.set_xticks(range(1, 6))
    ax.set_xlabel("请求核数")
    ax.set_ylabel("逻辑候选数（100 例合计）")
    ax.set_ylim(0, max(bottoms) * 1.16)
    ax.legend(frameon=False, ncol=5, loc="upper center", bbox_to_anchor=(.5, 1.0))
    light_grid(ax)
    return export(fig, "process_q2_candidate_coverage")


def process_mapping_effect(effects):
    families = ["b0", "b1", "b2a_w4", "b2a_w8", "b2a_w16"]
    labels = ["B0", "B1", "B2A-w4", "B2A-w8", "B2A-w16"]
    matrix = np.asarray([
        [sum(r["candidate"] == f"map_locality@{family}" and int(r["cores"]) == core
             and r["relation"] == "better" for r in effects)
         for core in range(2, 6)]
        for family in families
    ])
    assert matrix.shape == (5, 4)
    fig, (ax, cax) = plt.subplots(1, 2, figsize=FIGURE_SIZE,
                                  gridspec_kw={"width_ratios": [20, 1], "wspace": .15},
                                  layout="constrained")
    ax.pcolormesh(np.arange(5) - .5, np.arange(6) - .5, matrix,
                  cmap="Blues", vmin=0, vmax=matrix.max() + 1,
                  shading="flat", edgecolors="white", linewidth=.7)
    ax.set_xlim(-.5, 3.5)
    ax.set_ylim(4.5, -.5)
    ax.set_xticks(range(4), ["2 核", "3 核", "4 核", "5 核"])
    ax.set_yticks(range(5), labels)
    ax.set_xlabel("请求核数（每格 n=100）")
    ax.set_ylabel("固定切图家族")
    for i in range(5):
        for j in range(4):
            value = int(matrix[i, j])
            ax.text(j, i, str(value), ha="center", va="center",
                    color="white" if value > matrix.max() * .58 else "#222222", fontsize=8)
    scale_max = float(matrix.max() + 1)
    for idx in range(20):
        low = scale_max * idx / 20
        cax.add_patch(patches.Rectangle((0, low), 1, scale_max / 20,
                      facecolor=plt.get_cmap("Blues")((idx + .5) / 20),
                      edgecolor="none"))
    cax.set_xlim(0, 1)
    cax.set_ylim(0, scale_max)
    cax.set_xticks([])
    cax.yaxis.tick_right()
    cax.yaxis.set_label_position("right")
    cax.set_yticks([0, int(matrix.max() // 2), int(matrix.max())])
    cax.set_ylabel("严格改善的例数")
    return export(fig, "process_q2_mapping_effect")


def process_portfolio_selection(selected):
    families = [
        ("verified_stage1_global_fallback", "阶段一回退", PALETTE["neutral"]),
        ("scene_b_fixed_partition_mapping", "新映射", PALETTE["primary"]),
        ("recursive_final_winner_inheritance", "递归继承", PALETTE["secondary"]),
    ]
    fig, ax = plt.subplots(figsize=FIGURE_SIZE, layout="constrained")
    bottom = np.zeros(5)
    for name, label, color in families:
        counts = np.asarray([sum(int(r["cores"]) == k and r["winner_family"] == name for r in selected)
                             for k in range(1, 6)])
        ax.bar(range(1, 6), counts, bottom=bottom, width=.62, color=color,
               label=label, edgecolor="white", linewidth=.4)
        for x, count, low in zip(range(1, 6), counts, bottom):
            if count >= 5:
                ax.text(x, low + count / 2, str(count), ha="center", va="center",
                        color="white" if name == "scene_b_fixed_partition_mapping" else "#222222", fontsize=7.5)
        bottom += counts
    ax.set_ylim(0, 112)
    ax.set_xticks(range(1, 6))
    ax.set_xlabel("请求核数")
    ax.set_ylabel("最终赢家用例数（每核 100 例）")
    ax.legend(frameon=False, ncol=3, loc="upper center", bbox_to_anchor=(.5, 1.0))
    light_grid(ax)
    return export(fig, "process_q2_portfolio_selection")


def result_scaling(selected):
    x = np.arange(1, 6)
    values = [[float(r["speedup"]) for r in selected if int(r["cores"]) == k] for k in x]
    means = np.asarray([statistics.fmean(v) for v in values])
    medians = np.asarray([statistics.median(v) for v in values])
    low = np.asarray([percentile(v, 5) for v in values])
    high = np.asarray([percentile(v, 95) for v in values])
    fig, ax = plt.subplots(figsize=FIGURE_SIZE, layout="constrained")
    ax.fill_between(x, low, high, color=PALETTE["sky"], alpha=.22,
                    linewidth=0, label="P05–P95（100 例）")
    ax.plot(x, means, "o-", color=PALETTE["primary"], lw=1.5,
            markersize=5, label="算术平均")
    ax.plot(x, medians, "s--", color=PALETTE["contrast"], lw=1.15,
            markersize=3.5, label="中位数")
    ax.plot(x, x, ":", color=PALETTE["neutral"], lw=.9, label="理想线性加速")
    for core, value in zip(x[1:], means[1:]):
        ax.annotate(f"{value:.2f}", (core, value), xytext=(0, 8),
                    textcoords="offset points", ha="center", fontsize=7.2)
    ax.set_xticks(x)
    ax.set_xlim(.8, 5.2)
    ax.set_ylim(0, max(high[-1], 5.1) * 1.10)
    ax.set_xlabel("请求核数")
    ax.set_ylabel("相对整图 single 的加速比")
    ax.legend(frameon=False, ncol=2, loc="upper left")
    light_grid(ax)
    return export(fig, "result_q2_scaling")


def result_holdout_gain(selected):
    subset = [r for r in selected if r["split"] == "holdout" and int(r["cores"]) >= 2]
    assert len(subset) == 280
    fig, ax = plt.subplots(figsize=FIGURE_SIZE, layout="constrained")
    for core in range(2, 6):
        group = [r for r in subset if int(r["cores"]) == core]
        improved = [r for r in group if float(r["gain_pct"]) > 0]
        ax.scatter([core + jitter(r["case"], .20) for r in improved],
                   [float(r["gain_pct"]) for r in improved],
                   s=15, alpha=.62, color=PALETTE["primary"],
                   edgecolor="none", zorder=3)
        ax.scatter([core], [0], s=33, marker="s", facecolors="white",
                   edgecolors=PALETTE["neutral"], linewidths=1.0, zorder=4)
        ax.text(core, -.35, f"零收益 {70 - len(improved)}", ha="center",
                va="top", fontsize=7.2)
        ax.text(core, 16.9, f"改善 {len(improved)}/70", ha="center", va="top", fontsize=8)
    ax.axhline(0, color=PALETTE["neutral"], lw=.8)
    ax.set_xticks(range(2, 6))
    ax.set_xlim(1.6, 5.4)
    ax.set_ylim(-1.2, 17.5)
    ax.set_xlabel("请求核数（70 个留出用例 / 核）")
    ax.set_ylabel("相对阶段一实际赢家的 Makespan 降幅（%）")
    light_grid(ax)
    return export(fig, "result_q2_holdout_gain")


def result_copy_spill_tradeoff(selected):
    improved = [r for r in selected if int(r["cores"]) >= 2 and float(r["gain_pct"]) > 0]
    assert len(improved) == 124
    fig, axes = plt.subplots(1, 2, figsize=(7.2, 3.4), layout="constrained")
    for ax, key, xlabel in [
        (axes[0], "copy_delta_mib", "新增搬运变化（MiB）"),
        (axes[1], "spill_delta_mib", "Spill 搬运变化（MiB）"),
    ]:
        for core, marker, color in [(2, "o", PALETTE["primary"]),
                                    (3, "s", PALETTE["secondary"]),
                                    (4, "^", PALETTE["positive"]),
                                    (5, "D", PALETTE["contrast"])]:
            rows = [r for r in improved if int(r["cores"]) == core]
            ax.scatter([float(r[key]) for r in rows], [float(r["gain_pct"]) for r in rows],
                       marker=marker, s=18, alpha=.67, color=color,
                       edgecolors="none", label=f"{core} 核")
        ax.axvline(0, color=PALETTE["dark"], ls="--", lw=.8)
        ax.set_xscale("symlog", linthresh=.25)
        ax.xaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value:g}"))
        ax.set_xlabel(xlabel + "；对称对数刻度")
        ax.set_ylim(-.4, 16.5)
        light_grid(ax)
    axes[0].set_ylabel("Makespan 降幅（%）")
    axes[1].set_ylabel("")
    axes[0].legend(frameon=False, ncol=2, loc="upper left")
    return export(fig, "result_q2_copy_spill_tradeoff", (7.2, 3.4))


def flow_model():
    fig, ax = plt.subplots(figsize=FLOW_SIZE)
    ax.set_xlim(0, 10)
    ax.set_ylim(0, 11)
    ax.axis("off")
    boxes = [
        (9.8, "输入：100 张图 + config", "#EDF4F8"),
        (8.3, "六类切图初始化", "#EAF4F0"),
        (6.8, "并列候选：原映射 / 局部映射 / 低核继承", "#EAF4F0"),
        (5.3, "内容哈希、合法性与去重", "#EAF4F0"),
        (3.8, "原版官方场景 B 模拟器评估", "#FFF2DE"),
        (2.3, "字典序选择 + 阶段一回退", "#FFF2DE"),
        (.8, "输出：500 组计划与指标", "#EDF4F8"),
    ]
    for y, label, face in boxes:
        rect = patches.FancyBboxPatch((2.0, y - .35), 6, .7,
                                      boxstyle="round,pad=0.08,rounding_size=0.13",
                                      facecolor=face, edgecolor=PALETTE["neutral"], linewidth=.9)
        ax.add_patch(rect)
        ax.text(5, y, label, ha="center", va="center", fontsize=9)
    for y1, y2 in zip([9.4, 7.9, 6.4, 4.9, 3.4, 1.9],
                       [8.7, 7.2, 5.7, 4.2, 2.7, 1.2]):
        ax.annotate("", xy=(5, y2), xytext=(5, y1),
                    arrowprops={"arrowstyle": "-|>", "lw": 1.0,
                                "color": PALETTE["dark"]})
    return export(fig, "flow_q2_model", FLOW_SIZE)


def validate_snapshot(selected, graphs, tensors, effects):
    assert len(selected) == 500 and len(graphs) == 100 and len(effects) == 2000
    assert len({r["case"] for r in graphs}) == 100
    assert len({(r["case"], r["cores"]) for r in selected}) == 500
    assert all(math.isfinite(float(r["speedup"])) for r in selected)
    gains = [r for r in selected if int(r["cores"]) >= 2 and float(r["gain_pct"]) > 0]
    assert len(gains) == 124
    holdout = [r for r in gains if r["split"] == "holdout"]
    assert len(holdout) == 90 and len({r["case"] for r in holdout}) == 49
    assert len(tensors) >= 200


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-only", action="store_true",
                        help="只用目录中已提交的紧凑数据快照重新出图")
    args = parser.parse_args()
    DATA.mkdir(parents=True, exist_ok=True)
    if not args.snapshot_only:
        prepare()
    selected, graphs, tensors, effects = load_snapshot()
    validate_snapshot(selected, graphs, tensors, effects)
    style()
    reports = [
        raw_graph_scale(graphs), raw_tensor_footprint(tensors), raw_graph_io(graphs),
        process_candidate_coverage(), process_mapping_effect(effects),
        process_portfolio_selection(selected), result_scaling(selected),
        result_holdout_gain(selected), result_copy_spill_tradeoff(selected),
        flow_model(),
    ]
    (QA / "generation_summary.json").write_text(
        json.dumps({
            "source": "official Scene B, frozen problem2_final_v1",
            "snapshot_only": args.snapshot_only,
            "figure_count": len(reports),
            "source_hashes_sha256": sha256(DATA / "source_hashes.json"),
            "reports": reports,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"figures": len(reports), "selected_rows": len(selected),
                      "graphs": len(graphs), "mapping_candidates": len(effects)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
