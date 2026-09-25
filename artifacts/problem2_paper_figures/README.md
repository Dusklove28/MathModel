# 问题二论文图：官方场景 B 最终结果

本目录只读取冻结的 `artifacts/problem2_final_v1/`、100 张原图、`data/config.txt` 与 `problem2_preregistered_split.json`。不修改官方评估器、求解器、原始结果或问题三文件。全部图为论文候选素材，参赛队提交前须人工核对图注、编号和排版。

## 可直接使用的内容

- `figures/`：9 张数据图（原始数据、求解过程、最终结果各 3 张）与 1 张问题二方法流程图；每张提供可编辑文字的 SVG 和 600 DPI PNG。
- `data/`：可独立重画图的紧凑 CSV 快照及原始输入 SHA-256。无需上传 5 GB 的原始实验目录即可查看与重绘图；要重新从官方结果提取快照则仍需完整原始目录。
- `docs/figure_contracts.md`：每张图的论点、证据口径、图型与尺寸。
- `docs/captions.md`：可供论文手核对、改写的中文图注。
- `qa/`：程序布局检查、彩色预览、灰度预览和生成汇总。
- `results/复现清单.json`：运行环境、参数、输入哈希和唯一复现命令。

建议正文优先使用 `flow_q2_model`、`result_q2_scaling`、`result_q2_holdout_gain` 与 `result_q2_copy_spill_tradeoff`；其余图可放方法或补充材料。所有图号应由最终整篇论文统一编排，当前文件名不预设全文章节图号。

## 复现

在仓库 `code/` 目录运行；先安装 `math-modeling` SKILL 或设置其位置：

```powershell
$env:MATH_MODELING_SKILL_ROOT='C:\Users\17747\.codex\skills\math-modeling'
python artifacts/problem2_paper_figures/scripts/generate_all.py
```

若当前设备没有多 GB 的 `problem2_final_v1`，用已提交的紧凑快照重画：

```powershell
python artifacts/problem2_paper_figures/scripts/generate_all.py --snapshot-only
```

Linux 可用 `export MATH_MODELING_SKILL_ROOT=/path/to/math-modeling`。Python 依赖：`numpy`、`matplotlib`、`Pillow`；本次本机版本与输入哈希见复现清单。绘图全程确定性运行；抖动散点位置由 case 编号固定计算，没有随机采样或人为删点。

## 数据口径与主要结论

- 100 case × 1～5 核 = 500 个合法最终结果；5300 条候选，零失败。
- 1～5 核平均逐例加速比为 `1.0000 / 1.7550 / 2.3568 / 2.8674 / 3.2347`，分子均为同一 case 的整图 `single` 官方 Makespan。
- 2～5 核相对阶段一实际赢家有 124/400 格降低 Makespan，另有 9 格同 Makespan 减少搬运，零退化。
- 冻结的 70 个留出 case 中，90/280 个多核格降低 Makespan，涉及 49/70 个 case；全部留出多核格平均相对改善为 0.6352%。这是保留回退后的组合结果，不能解释为某个映射策略在全部图上均有效。
- 对 124 个 Makespan 改善格，91 格的新增搬运下降、33 格上升；Spill 既可能下降也可能上升。论文应以 Makespan 为主目标，如实讨论权衡。

图中 `map_locality` 候选自身的优胜次数与最终组合赢家次数不是同一统计量。切图、映射和继承候选全部以未修改的官方场景 B 模拟器为最终选择依据。问题一场景 A 的结果只用于候选初始化，没有代入问题二评分。

## 自动门禁

```powershell
$fig='artifacts/problem2_paper_figures/figures'
$paths=@(Get-ChildItem -LiteralPath $fig -File | Where-Object { $_.Extension -in '.svg','.png' } | Select-Object -ExpandProperty FullName)
python "$env:MATH_MODELING_SKILL_ROOT/tools/figure/scripts/check_figure.py" $paths --strict
python "$env:MATH_MODELING_SKILL_ROOT/references/roles/编程手/scripts/figure_audit.py" $fig --questions q2 --strict
```

本次两道门禁均返回 0；`figure_audit` 报告 10/10 张 SVG/PNG 配对、PNG 约 600 DPI、SVG 全部可编辑且无嵌入栅格、问题二原始/过程/结果图各 3 张。问题一与问题三的全篇图表统筹不在本目录的审计范围内。
