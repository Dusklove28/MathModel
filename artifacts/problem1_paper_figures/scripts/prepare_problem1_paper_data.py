from __future__ import annotations

import argparse
import hashlib
import json
import textwrap
from pathlib import Path

import numpy as np
import pandas as pd

from common import OUTPUT_ROOT, PROJECT_ROOT


FINAL_FILES = {
    "aggregate": "problem1_inherited_fallback_aggregate.csv",
    "results": "problem1_inherited_fallback_results.csv",
    "appendix": "problem1_inherited_fallback_appendix.csv",
}
BASELINE_FILES = {
    "aggregate": "problem1_full_aggregate.csv",
    "results": "problem1_full_results.csv",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _require_columns(frame: pd.DataFrame, required: set[str], label: str) -> None:
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise ValueError(f"{label} 缺少字段：{missing}")


def _case_number(series: pd.Series) -> pd.Series:
    return series.str.extract(r"(\d+)$", expand=False).astype(int)


def load_and_verify(final_root: Path, baseline_root: Path) -> tuple[dict, dict]:
    final_paths = {key: final_root / name for key, name in FINAL_FILES.items()}
    baseline_paths = {key: baseline_root / name for key, name in BASELINE_FILES.items()}
    for path in [*final_paths.values(), *baseline_paths.values()]:
        if not path.is_file():
            raise FileNotFoundError(path)

    final_aggregate = pd.read_csv(final_paths["aggregate"])
    final_results = pd.read_csv(final_paths["results"])
    final_appendix = pd.read_csv(final_paths["appendix"])
    baseline_aggregate = pd.read_csv(baseline_paths["aggregate"])
    baseline_results = pd.read_csv(baseline_paths["results"])

    _require_columns(
        final_aggregate,
        {
            "cores",
            "case_count",
            "reported_arithmetic_mean_speedup",
            "all_reported_values_officially_measured",
            "improved_case_count",
        },
        "final aggregate",
    )
    _require_columns(
        final_results,
        {
            "case",
            "cores",
            "t_i_1",
            "old_makespan",
            "old_added_copy_bytes",
            "final_makespan",
            "final_added_copy_bytes",
            "final_speedup",
            "officially_measured",
            "makespan_improvement",
            "added_copy_bytes_change",
        },
        "final results",
    )
    _require_columns(
        final_appendix,
        {
            "case",
            "cores",
            "measured_fallback_best_source_cores",
            "old_makespan",
            "old_added_copy_bytes",
            "final_makespan",
            "final_added_copy_bytes",
            "final_speedup",
            "makespan_improvement",
            "added_copy_bytes_change",
        },
        "final appendix",
    )
    _require_columns(
        baseline_aggregate,
        {"cores", "completed_cases", "arithmetic_mean_speedup"},
        "baseline aggregate",
    )
    _require_columns(
        baseline_results,
        {"case", "cores", "t_i_1", "t_i_k", "added_copy_bytes"},
        "baseline results",
    )

    expected_cores = [1, 2, 3, 4, 5]
    if len(final_results) != 500 or final_results["case"].nunique() != 100:
        raise AssertionError("最终 results 必须包含 100 个 case × 5 个核数，共 500 行")
    if sorted(final_results["cores"].unique().tolist()) != expected_cores:
        raise AssertionError("最终 results 的核数必须完整覆盖 1～5")
    if final_results.duplicated(["case", "cores"]).any():
        raise AssertionError("最终 results 存在重复的 case/cores 组合")
    per_core = final_results.groupby("cores", observed=True).size()
    if per_core.to_dict() != {core: 100 for core in expected_cores}:
        raise AssertionError(f"各核数样本量异常：{per_core.to_dict()}")
    if not bool(final_results["officially_measured"].all()):
        raise AssertionError("最终 results 存在非官方实测行")

    computed_speedup = final_results["t_i_1"] / final_results["final_makespan"]
    speedup_error = float(np.max(np.abs(computed_speedup - final_results["final_speedup"])))
    if speedup_error > 1e-12:
        raise AssertionError(f"逐例加速比公式不一致，最大误差 {speedup_error}")

    computed_means = final_results.groupby("cores", observed=True)["final_speedup"].mean()
    aggregate_means = final_aggregate.set_index("cores")["reported_arithmetic_mean_speedup"]
    mean_error = float(np.max(np.abs(computed_means - aggregate_means)))
    if mean_error > 1e-12:
        raise AssertionError(f"算术平均加速比与 aggregate 不一致，最大误差 {mean_error}")
    if not bool(final_aggregate["all_reported_values_officially_measured"].all()):
        raise AssertionError("aggregate 未全部标记为官方实测")

    appendix_ordered = final_appendix.sort_values(["case", "cores"]).reset_index(drop=True)
    results_ordered = final_results.sort_values(["case", "cores"]).reset_index(drop=True)
    for column in ["final_makespan", "final_added_copy_bytes", "final_speedup"]:
        if not np.allclose(appendix_ordered[column], results_ordered[column], rtol=0, atol=1e-12):
            raise AssertionError(f"appendix 与 results 的 {column} 不一致")

    baseline_join = baseline_results.merge(
        final_results.loc[final_results["cores"] > 1],
        on=["case", "cores"],
        how="inner",
        validate="one_to_one",
    )
    if len(baseline_join) != 400:
        raise AssertionError("回退前对照应包含 100 个 case × 2～5 核，共 400 行")
    if not np.array_equal(baseline_join["t_i_k"], baseline_join["old_makespan"]):
        raise AssertionError("回退前 full results 与最终表中的 old_makespan 不一致")
    if not np.array_equal(
        baseline_join["added_copy_bytes"], baseline_join["old_added_copy_bytes"]
    ):
        raise AssertionError("回退前 full results 与最终表中的 old_added_copy_bytes 不一致")

    old_from_final = final_aggregate.loc[
        final_aggregate["cores"] > 1, "old_arithmetic_mean_speedup"
    ].to_numpy()
    old_from_baseline = baseline_aggregate.sort_values("cores")[
        "arithmetic_mean_speedup"
    ].to_numpy()
    if not np.allclose(old_from_final, old_from_baseline, rtol=0, atol=1e-12):
        raise AssertionError("回退前 full aggregate 与最终 aggregate 的 old 均值不一致")

    improved = final_appendix.loc[final_appendix["makespan_improvement"] > 0].copy()
    improved_by_core = improved.groupby("cores", observed=True).size().to_dict()
    if len(improved) != 36 or improved_by_core != {3: 3, 4: 9, 5: 24}:
        raise AssertionError(
            f"继承回退改善组合应为 36 组且按核数为 3/9/24，实际 {len(improved)}、{improved_by_core}"
        )
    if not np.array_equal(
        improved["old_makespan"] - improved["final_makespan"],
        improved["makespan_improvement"],
    ):
        raise AssertionError("改善组的 makespan_improvement 与新旧差值不一致")
    if not bool((improved["added_copy_bytes_change"] <= 0).all()):
        raise AssertionError("存在 Makespan 改善但额外搬运量增加的组合")

    verification = {
        "complete": True,
        "case_count": int(final_results["case"].nunique()),
        "core_counts": expected_cores,
        "row_count": int(len(final_results)),
        "rows_per_core": {str(k): int(v) for k, v in per_core.items()},
        "all_values_officially_measured": True,
        "max_speedup_formula_abs_error": speedup_error,
        "max_aggregate_mean_abs_error": mean_error,
        "arithmetic_mean_speedup": {
            str(k): float(v) for k, v in computed_means.items()
        },
        "improved_group_count": int(len(improved)),
        "improved_case_count": int(improved["case"].nunique()),
        "improved_groups_by_core": {str(k): int(v) for k, v in improved_by_core.items()},
        "improved_groups_with_lower_added_copy_bytes": int(
            (improved["added_copy_bytes_change"] < 0).sum()
        ),
        "improved_groups_with_equal_added_copy_bytes": int(
            (improved["added_copy_bytes_change"] == 0).sum()
        ),
        "source_sha256": {
            str(path): sha256(path)
            for path in [*final_paths.values(), *baseline_paths.values()]
        },
    }
    frames = {
        "final_aggregate": final_aggregate,
        "final_results": final_results,
        "final_appendix": final_appendix,
        "baseline_aggregate": baseline_aggregate,
        "baseline_results": baseline_results,
        "improved": improved,
    }
    return frames, verification


def build_outputs(frames: dict, verification: dict, output_root: Path) -> None:
    data_dir = output_root / "data"
    docs_dir = output_root / "docs"
    qa_dir = output_root / "qa"
    for directory in (data_dir, docs_dir, qa_dir):
        directory.mkdir(parents=True, exist_ok=True)

    final_aggregate = frames["final_aggregate"].sort_values("cores")
    baseline_aggregate = frames["baseline_aggregate"].sort_values("cores")
    final_results = frames["final_results"].copy()
    final_appendix = frames["final_appendix"].copy()
    improved = frames["improved"].copy()

    baseline_map = dict(
        zip(
            baseline_aggregate["cores"].astype(int),
            baseline_aggregate["arithmetic_mean_speedup"].astype(float),
        )
    )
    fig1_rows = []
    for row in final_aggregate.itertuples(index=False):
        cores = int(row.cores)
        fig1_rows.append(
            {
                "cores": cores,
                "case_count": int(row.case_count),
                "pre_fallback_arithmetic_mean_speedup": 1.0
                if cores == 1
                else baseline_map[cores],
                "final_arithmetic_mean_speedup": float(
                    row.reported_arithmetic_mean_speedup
                ),
                "final_minus_pre_fallback": float(
                    row.reported_arithmetic_mean_speedup
                    - (1.0 if cores == 1 else baseline_map[cores])
                ),
                "value_kind": "officially_measured",
            }
        )
    pd.DataFrame(fig1_rows).to_csv(
        data_dir / "fig1_average_speedup.csv", index=False, encoding="utf-8-sig"
    )

    fig2 = final_results.loc[
        final_results["cores"].between(2, 5),
        [
            "case",
            "cores",
            "t_i_1",
            "final_makespan",
            "final_speedup",
            "final_added_copy_bytes",
            "officially_measured",
        ],
    ].copy()
    fig2["case_number"] = _case_number(fig2["case"])
    fig2 = fig2.sort_values(["cores", "case_number"]).drop(columns="case_number")
    fig2.to_csv(
        data_dir / "fig2_case_speedup_distribution.csv",
        index=False,
        encoding="utf-8-sig",
    )

    improved["source_cores"] = improved[
        "measured_fallback_best_source_cores"
    ].astype(int)
    improved["makespan_reduction_cycles"] = (
        improved["old_makespan"] - improved["final_makespan"]
    )
    improved["makespan_reduction_pct"] = (
        100.0 * improved["makespan_reduction_cycles"] / improved["old_makespan"]
    )
    improved["added_copy_reduction_bytes"] = (
        improved["old_added_copy_bytes"] - improved["final_added_copy_bytes"]
    )
    improved["added_copy_reduction_mib"] = (
        improved["added_copy_reduction_bytes"] / (1024.0**2)
    )
    improved["case_core_label"] = improved.apply(
        lambda row: f"{row['case']} / {int(row['cores'])}核", axis=1
    )
    fig3_columns = [
        "case",
        "cores",
        "source_cores",
        "case_core_label",
        "old_makespan",
        "final_makespan",
        "makespan_reduction_cycles",
        "makespan_reduction_pct",
        "old_added_copy_bytes",
        "final_added_copy_bytes",
        "added_copy_reduction_bytes",
        "added_copy_reduction_mib",
        "final_winner",
        "officially_measured",
    ]
    improved = improved.sort_values(
        ["makespan_reduction_pct", "case", "cores"], ascending=[False, True, True]
    )
    improved[fig3_columns].to_csv(
        data_dir / "fig3_fallback_improvements.csv",
        index=False,
        encoding="utf-8-sig",
    )

    appendix_long = final_appendix.loc[
        :,
        [
            "case",
            "cores",
            "final_makespan",
            "final_added_copy_bytes",
            "final_speedup",
            "final_winner",
            "reported_value_kind",
            "officially_measured",
        ],
    ].rename(
        columns={
            "final_makespan": "makespan_cycles",
            "final_added_copy_bytes": "added_copy_bytes",
            "final_speedup": "speedup_t1_over_tk",
            "final_winner": "selected_plan",
        }
    )
    appendix_long["case_number"] = _case_number(appendix_long["case"])
    appendix_long = appendix_long.sort_values(["case_number", "cores"]).drop(
        columns="case_number"
    )
    appendix_long.to_csv(
        data_dir / "appendix_problem1_long.csv", index=False, encoding="utf-8-sig"
    )

    wide = pd.DataFrame({"case": sorted(appendix_long["case"].unique())})
    for metric, prefix in [
        ("makespan_cycles", "makespan_k"),
        ("added_copy_bytes", "added_copy_bytes_k"),
        ("speedup_t1_over_tk", "speedup_k"),
    ]:
        pivot = appendix_long.pivot(index="case", columns="cores", values=metric)
        pivot = pivot.rename(columns={core: f"{prefix}{core}" for core in pivot.columns})
        wide = wide.merge(pivot.reset_index(), on="case", how="left", validate="one_to_one")
    wide.to_csv(
        data_dir / "appendix_problem1_wide.csv", index=False, encoding="utf-8-sig"
    )

    (qa_dir / "verification_summary.json").write_text(
        json.dumps(verification, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    contracts = textwrap.dedent(
        """\
        # 问题一图表契约

        ## 图 1 1～5 核平均加速比

        - 核心结论：最终继承回退方案的平均加速比随核心数增加而提高，并在 3～5 核优于回退前方案。
        - 证据链：横轴为核心数，纵轴为 100 个用例逐例加速比的算术平均；实线为最终结果，虚线为回退前对照。
        - 图型：单面板折线图；核心数是有序定量变量，连线只表达随核数变化的趋势。
        - 后端与版型：Python/Matplotlib，中文论文通栏，6.3 × 3.8 in。
        - 导出：可编辑文字 SVG；600 DPI PNG；灰度预览。

        ## 图 2 2～5 核逐例加速比分布

        - 核心结论：平均加速提升并非由少数用例单独驱动，各核数下 100 个用例的完整分布可见。
        - 证据链：全部原始点展示个体差异；箱体展示中位数与四分位距；菱形展示算术平均。
        - 图型：箱线图叠加确定性抖动散点；不采用裸均值柱状图。
        - 后端与版型：Python/Matplotlib，中文论文通栏，6.3 × 4.1 in。
        - 导出：可编辑文字 SVG；600 DPI PNG；灰度预览。

        ## 图 3 继承回退改善

        - 核心结论：36 个严格改善组合均降低 Makespan，且额外搬运量不增加。
        - 证据链：左面板展示逐组合 Makespan 相对降幅；右面板对齐展示额外搬运减少量；颜色与标记形状共同编码目标核数。
        - 图型：双面板水平点图；避免跨数量级的原始 Makespan 直接共轴比较。
        - 后端与版型：Python/Matplotlib，中文论文整页宽图，6.5 × 8.6 in。
        - 导出：可编辑文字 SVG；600 DPI PNG；灰度预览。
        """
    )
    (docs_dir / "figure_contracts.md").write_text(contracts, encoding="utf-8")

    requirements = textwrap.dedent(
        """\
        # 2026 华为杯 A 题三问图表与附录要求核对

        核对来源：项目中的《通用神经网络处理器下的多核调度问题.docx》。以下段落编号来自只读提取结果。

        ## 问题一 场景 A

        - 正文（P048）：给出 1～5 核平均加速比折线图。逐例加速比定义为单核基准 Makespan 除以 k 核官方评估 Makespan，单核加速比定义为 1。
        - 附录（P049）：逐用例列出官方评估程序给出的 Makespan 与总额外数据搬运量。
        - 附件（P050）：提供可复现 Python 脚本。

        ## 问题二 场景 B

        - 正文（P055）：给出场景 B 下 1～5 核平均加速比折线图，定义与问题一相同。
        - 附录（P056）：逐用例列出官方 Makespan 与总额外数据搬运量。
        - 附件（P057）：提供可复现 Python 脚本。

        ## 问题三 共享 L2

        - 正文（P061）：给出 1～5 核下“无 L2”与“只读 Cache”两种配置的对比曲线，并报告相同核数下只读 Cache 相对无 L2 的加速比。
        - 附录（P062）：逐用例列出两种配置的官方 Makespan、总额外数据搬运量和 Cache 命中率。

        本目录仅制作问题一的正式图与附录表；问题二、三不填数值，也不生成空结果图。
        """
    )
    (docs_dir / "official_requirements_check.md").write_text(
        requirements, encoding="utf-8"
    )

    captions = textwrap.dedent(
        f"""\
        # 问题一中文图题与自足图注

        ## 图 1 问题一 1～5 核平均加速比

        图注：在固定官方配置下，对全部 100 个测试用例计算逐例加速比
        $S_{{i,k}}=T_{{i,1}}/T_{{i,k}}$，其中 $T_{{i,1}}$ 为用例 $i$ 的官方单核基准
        Makespan，$T_{{i,k}}$ 为同一用例在 $k$ 核最终方案下的官方 Makespan，且
        $S_{{i,1}}=1$。曲线给出算术平均
        $\\bar S_k=\\frac{{1}}{{100}}\\sum_{{i=1}}^{{100}}S_{{i,k}}$；实线为继承回退复评后的
        最终实测结果，虚线为回退前全量实验的官方实测对照。每个核数均含 100 个用例。

        ## 图 2 问题一 2～5 核逐例加速比分布

        图注：展示 2～5 核下全部 100 个测试用例的逐例加速比
        $S_{{i,k}}=T_{{i,1}}/T_{{i,k}}$，所有 Makespan 均来自官方评估器实测。
        每个灰点代表一个用例；箱体为第 25～75 百分位范围，中线为中位数，须线延伸至
        1.5 倍四分位距内的最远观测，黑色菱形为算术平均；水平虚线表示无加速基线
        $S=1$。每组 $n=100$；散点采用确定性等间距置换抖动，仅用于避免遮挡，
        不改变纵轴数值。

        ## 图 3 问题一 继承回退带来的逐例改善

        图注：展示继承较少核心数优胜方案后获得严格 Makespan 改善的 36 个
        case–核数组合（涉及 {verification['improved_case_count']} 个不同用例；3、4、5 核分别为
        3、9、24 组）。左图相对降幅定义为
        $100(T_{{old}}-T_{{final}})/T_{{old}}$；右图额外搬运减少量定义为
        $(B_{{old}}-B_{{final}})/2^{{20}}$ MiB。新旧分数均为原版官方评估器实测值。
        36 组均降低 Makespan，其中 {verification['improved_groups_with_lower_added_copy_bytes']} 组同时减少额外搬运，
        {verification['improved_groups_with_equal_added_copy_bytes']} 组额外搬运不变；颜色和点形共同表示目标核数。
        """
    )
    (docs_dir / "figure_captions.md").write_text(captions, encoding="utf-8")

    pending = textwrap.dedent(
        """\
        # 问题二与问题三待补图表数据需求

        本文件仅列数据需求，不填入任何实验数值，也不生成空结果图。

        ## 问题二 场景 B

        正文折线图需要：

        - 每个正式测试用例在 1～5 核下的官方 Makespan；
        - 同一口径的单核基准 $T_{i,1}$；
        - 逐例加速比 $S_{i,k}=T_{i,1}/T_{i,k}$；
        - 按核数计算的算术平均加速比、样本数和失败状态；
        - 固定 config、评估器与求解器的哈希，用于追溯。

        逐例附录需要：

        - `case`、`cores`、官方 `makespan_cycles`；
        - 官方 `added_copy_bytes`；
        - 方案状态、失败原因与是否官方实测。

        ## 问题三 共享 L2

        正文对比曲线需要：

        - 每个正式测试用例、每个核数在“无 L2”与“只读 Cache”两种配置下的官方 Makespan；
        - 同核相对加速比 $T_{i,k}^{no\\ L2}/T_{i,k}^{cache}$；
        - 两种配置按核数聚合的曲线数据、样本数和失败状态；
        - L2 容量、L2 带宽、DDR 配置及评估器哈希。

        逐例附录需要：

        - `case`、`cores`、配置类型；
        - 官方 `makespan_cycles` 与 `added_copy_bytes`；
        - 官方 Cache 命中率及其分子、分母口径；
        - 方案状态、失败原因与是否官方实测。
        """
    )
    (docs_dir / "problem2_problem3_pending_data_requirements.md").write_text(
        pending, encoding="utf-8"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="核验并整理问题一论文绘图数据")
    parser.add_argument(
        "--final-root",
        type=Path,
        default=PROJECT_ROOT / "artifacts" / "problem1_inherited_fallback_c4140",
    )
    parser.add_argument(
        "--baseline-root",
        type=Path,
        default=PROJECT_ROOT / "artifacts" / "problem1_full_c4140",
    )
    parser.add_argument("--output-root", type=Path, default=OUTPUT_ROOT)
    args = parser.parse_args()

    frames, verification = load_and_verify(
        args.final_root.resolve(), args.baseline_root.resolve()
    )
    build_outputs(frames, verification, args.output_root.resolve())
    print(json.dumps(verification, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
