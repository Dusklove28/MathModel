# 2026 年华为杯 A 题论文工作区

本目录存放 2026 年华为杯 A 题的**三问论文审阅稿**及写作依据。当前附录压缩版是 `draft/2026华为杯A题完整论文V3-精简附录.docx`；V1/V2 均保留为独立历史快照。V3 仍有封面队号与姓名、真实 AI 使用披露和可运行 Python 程序附件待参赛队补齐，**不是可直接提交的终稿**。参赛队仍须人工核对、改写并按当届官方要求导出最终 PDF。

## 本轮写作交接

| 材料 | 用途与边界 |
|---|---|
| [`draft/2026华为杯A题完整论文V3-精简附录.docx`](draft/2026华为杯A题完整论文V3-精简附录.docx) | 当前三问完整 Word 审阅稿；九张必交逐例表仍在附录，诊断性数据改由已有 CSV 保存。队友之间应串行交接。 |
| [`draft/V3附录压缩说明.md`](draft/V3附录压缩说明.md) | V2→V3 的保留/移出表格映射、页数变化、数据核验与提交边界。 |
| [`draft/2026华为杯A题完整论文V2.docx`](draft/2026华为杯A题完整论文V2.docx) | 压缩前的独立快照；[`draft/修改说明.md`](draft/修改说明.md)记录 V1→V2 的改写。 |
| [`notes/V2修改清单与逐条审查.md`](notes/V2修改清单与逐条审查.md) | 对外部修改建议逐项采纳/拒绝的依据、主张—证据映射和核查结果；不属于论文正文。 |
| [`scripts/revise_full_paper_v2.py`](scripts/revise_full_paper_v2.py) | 从已冻结 V1 和问题三官方逐例 CSV 重建 V2 的变更记录；仅供溯源。它需要本机安装 `math-modeling` DOCX 技能工具，跨设备编辑请直接使用 DOCX，不应无条件重跑此脚本覆盖队友的新改稿。 |
| [`scripts/audit_full_paper_v2.py`](scripts/audit_full_paper_v2.py) | 只读核对 15 张附录表的 7,500 个数值格、图表编号和正文工程词；运行命令见下。 |
| [`scripts/compress_full_paper_v3_appendix.py`](scripts/compress_full_paper_v3_appendix.py) | V2→V3 的可复核附录压缩步骤；拒绝覆盖已存在的 V3。 |
| [`scripts/audit_full_paper_v3_appendix.py`](scripts/audit_full_paper_v3_appendix.py) | 只读核对 V3 九张必交附录表的 4,500 个数值格。 |

论文中的数据与实现对应关系：

| 问题 | 冻结逐例证据 | 求解器与官方口径 |
|---|---|---|
| 一 | [`figures/q1/tables/appendix_problem1_wide.csv`](../../figures/q1/tables/appendix_problem1_wide.csv) | [`solver_problem1.py`](../../solver_problem1.py)、[`candidate_manager_problem1.py`](../../candidate_manager_problem1.py)；官方场景 A 评估器 [`code/multicore_cut_evaluate_problem_1.py`](../multicore_cut_evaluate_problem_1.py)。 |
| 二 | [`figures/q2/tables/selected_results.csv`](../../figures/q2/tables/selected_results.csv) | [`solver_problem2.py`](../../solver_problem2.py)、[`candidate_manager_problem2_final.py`](../../candidate_manager_problem2_final.py)；官方场景 B 评估器 [`code/multicore_cut_evaluate_problem_2.py`](../multicore_cut_evaluate_problem_2.py)。 |
| 三 | [`figures/q3/tables/case_core_2x2.csv`](../../figures/q3/tables/case_core_2x2.csv) | [`solver_problem3.py`](../../solver_problem3.py)、[`solver_problem3_mapping.py`](../../solver_problem3_mapping.py)；官方 L2 评估器 [`code/multicore_cut_evaluate_problem_3.py`](../multicore_cut_evaluate_problem_3.py)。 |

三问的正式图、过程图和绘图数据统一在仓库根目录 [`figures/q1`](../../figures/q1/)、[`figures/q2`](../../figures/q2/)、[`figures/q3`](../../figures/q3/)；各目录的 `目录说明.txt` 标明口径和来源。题目原文在仓库根目录 [`通用神经网络处理器下的多核调度问题.docx`](../../通用神经网络处理器下的多核调度问题.docx)。2026 官方格式、模板、上传说明和 AI 规则在本目录的 `templates/` 与 `references/official_2026/`。根目录另存的图稿副本、`paper_2026/official_refs/` 和 `draft/qa/` 为本机工作文件，不是本轮协作的另一套权威来源。

从仓库根目录运行只读数据核对：

```powershell
python code/paper/scripts/audit_full_paper_v3_appendix.py
```

共同写作时，先同步最新 `main`，只由一位队员编辑当前 V3 并完成交接；下一位再继续。若继续修订，请另存 V4 或新提交，不要覆写 V1/V2。程序源码索引不等于题目要求的**可复现 Python 程序附件**；最终附件需另行打包、解压验证并由参赛队上传确认。

## 历史问题一阶段稿

## 文件位置

| 目录 | 内容 |
|---|---|
| [`draft/`](draft/) | 完整 V1/V2/V3 与历史问题一 Word 章节草稿；当前协作主稿为 V3。 |
| [`notes/`](notes/) | 问题一证据大纲、论文目录草案、四篇 2025 年 A 题优秀论文的方法对照矩阵。 |
| [`templates/`](templates/) | 当届官方 Word 模板与格式规范；`官方模板转换.docx` 是为生成阶段稿制作的副本。正式汇编以原始官方模板和规范为准。 |
| [`references/2025_A/`](references/2025_A/) | 方法对照中阅读的四篇 2025 年 A 题优秀论文。 |
| [`references/format/`](references/format/) | 上届论文格式规范，仅供历史对照。 |
| [`references/official_2026/`](references/official_2026/) | 当届官方上传操作手册与 AI 工具使用规定；格式规范和模板原件仍在 `templates/`。 |
| [`scripts/`](scripts/) | Word 章节生成脚本和已核验的原生 Word 公式片段。 |

题目原文仍在仓库根目录的 [`通用神经网络处理器下的多核调度问题.docx`](../../通用神经网络处理器下的多核调度问题.docx)。问题一的五张权威结果表已在 [`artifacts/problem1_full_c4140/`](../../artifacts/problem1_full_c4140/) 与 [`artifacts/problem1_inherited_fallback_c4140/`](../../artifacts/problem1_inherited_fallback_c4140/)；正式图、绘图数据和逐例宽表已在 [`artifacts/problem1_paper_figures/`](../../artifacts/problem1_paper_figures/)。本目录不重复存储这些结果，以免产生两套数据源。

## 使用与复现

直接打开 [`draft/问题一章节草稿.docx`](draft/问题一章节草稿.docx) 编辑，或查看同目录 PDF。生成脚本从仓库内已提交的结果表、图和官方模板转换副本重新构建 Word 章节；它只写论文文件，不运行或修改求解器。

在仓库根目录安装已测试的依赖后运行：

```powershell
python -m pip install -r code/paper/scripts/requirements.txt
python code/paper/scripts/generate_problem1_chapter.py
```

生成脚本会检查 500 行最终结果、400 行原始多核结果、100 例附录宽表、关键统计量与逐例字段的一致性，然后覆盖 `draft/问题一章节草稿.docx`。需要 PDF 时，可用 Word 将该 DOCX 导出为 PDF。`scripts/omml_fragments.json` 保存了本稿公式的 Word 原生 OMML 片段，使脚本无须依赖作者本机的技能目录；修改公式时需同步更新对应片段。

本阶段稿以当届官方模板的页面设置与格式规范排版。第三方 [LaTeX 仓库](https://github.com/1SPECOO/Huawei_Cup_2026_Mathematical_Modeling_latex)仅作为结构参考，不是本稿模板来源。HEFT 式上行秩与 EFT 属于经典调度方法的场景改造，方法归属见对照矩阵。
