# 问题三图表契约与数据剖析

本文件在正式绘图前冻结图表论点。仅覆盖问题三；正式编号待全题定稿。后端为 Python/Matplotlib；正式宽度预设 7.2 in 双栏，SVG 可编辑文字、PNG 300 DPI，另出灰度预览。所有比值均先按同一 case-core 配对计算，再对 case 取算术平均；不以均值之比替代。

## 数据剖析

- 最终配对表：500 行、100 例×1–5 核，每核 n=100；100% 合法，无缺格。`makespan` 单位 cycle，跨例右偏，比较方案用配对比值。
- 预注册划分：开发 12、验证 18、留出 70 例，互不重叠。smoke 为开发例中的 3 例×4 核，重复运行记录，不是独立样本。
- 原始 JSON：100 份，逐例 `ops` 766–38,666（中位 4,223）；`edges` 1,841–108,272（中位 11,187）；`tensors` 816–40,287（中位 4,590.5）。逐 Tensor 记录 747,786 个，其中 DDR 64,612、L1 409,838、UB 273,336；`size` 2–294,912 B（中位 3,072 B），右偏。Tensor 记录有算例内依赖，仅作输入结构描述。
- 过程调用：每分支四个 invocation；组数、严格改善数、官方调用数均来自阶段文件。官方调用是本次 invocation 的调用数，开发包含已运行的 smoke 组。
- `summary.json` 的 95% 区间是按 case bootstrap 的逐例比值算术均值百分位区间；只作区间估计，不绘制显著性星号。

| 图名 | 核心结论与证据链 | 数据字段与统计口径 | 图型、版型与尺寸 |
|---|---|---|---|
| `raw_q3_graph_scale` | 输入图规模异质，展示 100 例 ops 数的分布与中位数 | `data/case_*.json` 的 `len(ops)`；每例一值，无删除 | 直方图；quantitative grid；7.2×3.5 in |
| `raw_q3_tensor_size` | 原始 Tensor 大小随存储位置分布，描述可能的容量压力 | 原始 `tensors[].pos,size`，三类位置全量 ECDF；不把 Tensor 当独立算例推断 | 对数横轴 ECDF；quantitative grid；7.2×3.8 in |
| `raw_q3_graph_bytes` | 输入图操作数和 DDR 标记 Tensor 的名义字节总和共同变化 | 每例 `len(ops)`、`sum(size where pos=DDR)`，后者不是 DDR 实际流量 | 100 点双对数散点；quantitative grid；7.2×4.0 in |
| `process_q3_coverage` | 预注册 12/18/70 对应多核组 48/72/280，smoke 12 组为开发子集 | split JSON、invocations 的 group/case 计数，绝不将 smoke 加到开发 | 横条与子集标记；schematic-led composite；7.2×3.2 in |
| `process_q3_improvements` | 排序和映射在各阶段有不同严格改善数 | 每 invocation `progress[].strict_improvement`，相对各自输入方案；点旁标改善组/总组 | 双分支点图；quantitative grid；7.2×3.7 in |
| `process_q3_calls` | 各阶段官方候选评估调用成本可追溯 | `official_calls_this_invocation`；四阶段调用可累计，但样本组不可把 smoke 与开发相加 | 分组柱状；quantitative grid；7.2×3.7 in |
| `result_q3_scaling` | 四格 2×2 的逐例平均并行加速随请求核数增加，最终有 L2 5 核为约 3.3009× | `curve_1_to_5.csv` 的四列 `mean_of_case_parallel_speedups_*_vs_k1`；同例请求 k=1 的冻结赢家作分母；每核 n=100 | 四条带不同线型/marker 的有序核数曲线；quantitative grid；7.2×4.2 in |
| `result_q3_effects` | 配对硬件效应与方案效应应分开解释；无 L2 的方案收益相近 | `summary.json` 四类逐例比值均值和各自按 case bootstrap 95% CI；每核 n=100 | 两面板偏移点与区间；quantitative grid；7.2×4.0 in |
| `result_q3_holdout` | 留出集 133/280 严格改善，零改善是实际质量点 | 留出 70 例×2–5 核的 `baseline_l2/final_l2`，逐例点、箱体、等于 1 的质量点；无下采样 | 箱线+逐例点；quantitative grid；7.2×4.0 in |
| `flow_q3_model` | 问题三从输入与冻结方案，先冻结预注册划分，再经配对、合法候选、官方择优与回退、分阶段评估至消融 | 已确认的 G1/G2/G3 代码、预注册 JSON 与状态文件，箭头表示真实求解顺序 | 原生 Matplotlib 流程图；schematic-led composite；7.2×6.0 in |

典型案例只出表。现有 `artifacts/problem3_timeline_evidence/` 提供汇总路径统计与若干首事件，缺少完整逐操作起止记录，不构造完整甘特图。`case_067/k2` 是 G1 同方案硬件反例，不能称为最终方案相对 G1 的退化。

统计解释：硬件比值为同方案 `T_noL2/T_L2`；方案比值为同硬件 `T_baseline/T_final`。缓存命中率按 `hit_bytes/(hit_bytes+miss_bytes)`，`miss_bytes` 仅为 COPY_IN 未命中字节，不等于总 DDR 字节；`added_copy_bytes` 是名义新增搬运字节。无 L2 下调度收益与有 L2 下相近，图文均不把全部收益归因于缓存，也不以命中率代替 makespan 评分。
