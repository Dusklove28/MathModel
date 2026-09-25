# 问题三图注草稿（编号待全题统一）

正式图保存在 `figures/`，同名 SVG 与 300 DPI PNG 为论文用图；`figures/qa/` 中的预览和灰度图只供检查。全部源数据表在 `results/`。本次只有问题三，图号、全题图数及与问题二的术语待队友定稿后统一。

1. **`raw_q3_graph_scale` 原始输入图操作数分布。** 100 份 `data/case_001.json` 至 `case_100.json` 中的 `len(ops)`；每例一值，纵轴为算例数，虚线为中位数 4,223 个。源数据：`q3_raw_case_graph.csv`；n=100 例，无推断区间。
2. **`raw_q3_tensor_size` 原始 Tensor 大小的存储位置分布。** 原始 JSON 的 `tensors[].size`（B）和 `tensors[].pos`，三条精确经验累积分布分别为 DDR/L1/UB；横轴对数，仅改变展示尺度。n=747,786 个 Tensor 条目，嵌套在 100 例中；条目不视为独立算例。源数据：`q3_raw_tensors.csv`。
3. **`raw_q3_graph_bytes` 图规模与 DDR 标记 Tensor 大小。** 横轴 `len(ops)`，纵轴 `sum(tensors[].size | pos='DDR')`，每点为一例，颜色/形状指开发、验证、留出集；两轴为对数尺度。该字节和是原始图中的名义大小，不是官方运行 DDR 总搬运量。源数据：`q3_raw_case_graph.csv`；n=100 例。
4. **`process_q3_coverage` 预注册阶段覆盖。** 12/18/70 例在 2–5 核形成 48/72/280 个唯一 case-core 组。深色 12 组 smoke 是开发 48 组的子集，不另加到样本量。来源：`problem3_preregistered_split.json` 与两个分支 `invocations/`；单位为组和例。
5. **`process_q3_improvements` 两分支严格改善比例。** 点为阶段内严格改善组数除以该阶段成功组数，点旁数字为严格改善组数；排序相对 G1 冻结方案，映射相对排序赢家。四阶段为 smoke/开发/验证/留出，smoke 与开发重叠。来源：两分支 `invocations/*.json`；无置信区间或显著性检验。
6. **`process_q3_calls` 官方候选评估调用。** 柱为各 invocation 的 `official_calls_this_invocation`，单位次；排序累计 524 次，映射累计 332 次。smoke 与开发例重叠，但已发生的调用数可以按运行阶段相加。来源：两分支 `invocations/*.json`。
7. **`result_q3_scaling` 1–5 核并行加速曲线。** 四条曲线分别为冻结/最终方案 × 无/有 L2，均为 100 例各自加速比的算术均值。每例分母为该例**请求 k=1 时的冻结问题二赢家**在相应硬件下的 makespan；分子为同例请求 k 核时该方案的 makespan。连线仅引导有序核数阅读。5 核最终有 L2 为 3.300881×。来源：`curve_1_to_5.csv` 与 `case_core_2x2.csv`；n=100/核；不使用“均值之比”。
8. **`result_q3_effects` 硬件与方案的配对效应。** a：同方案 `T_noL2/T_L2`，冻结与最终方案并列；b：同硬件 `T_冻结方案/T_最终方案`，无 L2 与有 L2 并列。点为 100 例逐例比值的算术均值，竖线为按 case bootstrap 的 95% 百分位区间，水平线为 1。来源：最终 `summary.json` 和 `case_core_2x2.csv`；n=100/核；无显著性检验。无 L2 下方案收益接近有 L2 下，不把全部收益归因于缓存。
9. **`result_q3_holdout` 留出集逐例方案收益。** 70 例在 2–5 核的点为 `T_冻结,L2/T_最终,L2`；实心表示严格改善，空心表示等于 1，箱体为四分位数、中线为中位数，须线按 1.5×IQR 截断，离群点仍以逐例点显示。顶部为严格改善例数，各核 29/37/32/35，总计 133/280 组；零改善点全部保留。来源：最终 `case_core_2x2.csv` 与预注册 split；n=70/核；无显著性检验。
10. **`flow_q3_model` 问题三求解与验证流程。** 输入图与官方语义 → 冻结问题二方案 → **预注册划分冻结（先于问题三收益检查）** → G1 同方案配对评估 → 固定映射排序候选 → 共享输入映射候选 → 官方 makespan 择优/回退 → 分阶段验证与留出评估 → 2×2 消融与曲线。节点与顺序来自 `problem3_preregistered_split.json`、`run_problem3_g1.py`、`run_problem3_g2.py`、`solver_problem3.py`、`solver_problem3_mapping.py` 及实验状态文件；本图为方法示意，无样本误差棒。

典型案例的 `artifacts/problem3_timeline_evidence/` 只有汇总统计及首个命中/淘汰等选定事件，见 `q3_paper_tables.md`。不将其画为完整甘特图。缓存命中率按 `hit_bytes/(hit_bytes+miss_bytes)`；`miss_bytes` 不是总 DDR 字节，`added_copy_bytes` 是名义新增搬运量；评分始终用官方 makespan。
