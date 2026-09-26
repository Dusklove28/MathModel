# 2026 年华为杯 A 题论文工作区

本目录存放 2026 年华为杯 A 题的三问论文及写作依据。当前匿名 V3 提交候选为 [`deliverables/2026华为杯A题V3-最终定稿.docx`](deliverables/2026华为杯A题V3-最终定稿.docx) 和由此实际导出的 [`deliverables/A26106560013.pdf`](deliverables/A26106560013.pdf)。压缩附录的 V3、V1、V2 均保留为历史快照。官方 2026 格式禁止在论文中出现答题人身份，因此提交候选已移除旧版身份封面，摘要为第 1 页并从 1 编号。参赛队仍须人工复核 AI 实际型号、文字与上传文件；未经系统提交成功回执，不得声称完成提交。

## 本轮写作交接

| 材料 | 用途与边界 |
|---|---|
| [`draft/2026华为杯A题完整论文V3-精简附录.docx`](draft/2026华为杯A题完整论文V3-精简附录.docx) | 提交候选的冻结起点；九张必交逐例表仍在附录，诊断性数据改由已有 CSV 保存。 |
| [`deliverables/2026华为杯A题V3-最终定稿.docx`](deliverables/2026华为杯A题V3-最终定稿.docx) | 匿名 V3 提交源；按实际源码澄清问题二/三代理与官方目标，图4改为可读的 12 组示例，其余数据不变。 |
| [`deliverables/A26106560013.pdf`](deliverables/A26106560013.pdf) | 从上一 Word 文档导出的 40 页 PDF；内容不含学校、队号或姓名，页码 1—40。生成 PDF 后若再改 DOCX，必须重新导出、计算 MD5 并重新提交识别码。 |
| [`deliverables/A26106560013_可运行程序_待转RAR.zip`](deliverables/A26106560013_可运行程序_待转RAR.zip) | 14.8 MB 可运行程序及完整 100 例输入的交接 ZIP；官方手册要求上传 **RAR**（`A26106560013.rar`、不超过 50 MB），需由参赛队转换并解压核验。 |
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

从仓库根目录运行最终 Word 的只读数据核对：

```powershell
python code/paper/scripts/audit_full_paper_v3_appendix.py --docx code/paper/deliverables/2026华为杯A题V3-最终定稿.docx
```

共同写作时，先同步最新 `main`，只由一位队员编辑当前 V3 并完成交接；下一位再继续。若继续修订，必须另存并重新导出 PDF、复核 MD5，不要覆写历史快照。程序 ZIP 已在全新目录解压并分别试跑问题一 case_001/k2、问题二 case_001/k1、问题三 G1 case_001/k1；这些是接口烟测，不代替耗时的全量复算。官方附件必须由参赛队转成 RAR 后上传。论文内的 AI 披露依据参赛队反馈列出 ChatGPT/Codex、GPT-5.6 与 GPT-6 Sol；两种型号是否都实际用于本队工作，提交前须据账号记录最后核对。

最终 Word 的 OOXML 校验与 4,500 格逐例数值审计均通过；本仓 77 项测试通过。通用 CUMCM 校验器仍报“40 页超过 30 页”和“附录表编号不连续”，这是它把本届华为杯当作另一竞赛处理的规则错配：仓库内 2026 官方格式规范没有 30 页总页数上限，附录表采用 A1/B1/C1 独立编号。不要为了通过该通用检查而删掉题目要求的逐例结果。

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

## 问题三图表协作资料

问题三尚未写入本 Word 阶段稿。可供后续写作的正式图在仓库根目录的 [`figures/`](../../figures/)，图数据、图注、表注与审计记录在 [`results/`](../../results/)。绘图入口为根目录的 `reproduce_q3_figures.py`，其前置校验和数据整理见 `validate_q3_figures.py`、`prepare_q3_figures.py`；依赖版本见 [`scripts/requirements-q3-figures.txt`](scripts/requirements-q3-figures.txt)。

已入库的紧凑原始证据包括 `artifacts/artifacts/problem3_report_final/` 的最终配对表、曲线和摘要，两条分支的 `invocations/` 阶段记录，以及 `artifacts/problem3_timeline_evidence/` 的典型案例摘要。大量逐组评测缓存仍是本地实验产物，不属于本次图表协作包。`results/复现清单.json` 保留原机器绝对路径以记录生成环境；换机核对时，应以仓库相对路径定位同名文件并比对其中的 SHA-256。

```powershell
python -m pip install -r code/paper/scripts/requirements-q3-figures.txt
python reproduce_q3_figures.py
```

一键绘图和审计还需要已安装的 `math-modeling` 图表工具；若它不在当前用户的 `~/.codex/skills/math-modeling`，设置环境变量 `MATH_MODELING_SKILL_ROOT` 为该技能目录。正式 PNG/SVG 和全部绘图数据已直接入库，阅读与引用无需安装该技能。
