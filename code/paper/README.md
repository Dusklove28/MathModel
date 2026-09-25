# 2026 年华为杯 A 题论文工作区

本目录存放**问题一阶段稿**及其写作依据。问题二、三尚未并入此稿；完整论文的摘要、结论与全题终检需在后续汇编时完成。

## 文件位置

| 目录 | 内容 |
|---|---|
| [`draft/`](draft/) | 问题一 Word 章节草稿和 PDF 审阅版；Word 是可编辑主稿。 |
| [`notes/`](notes/) | 问题一证据大纲、论文目录草案、四篇 2025 年 A 题优秀论文的方法对照矩阵。 |
| [`templates/`](templates/) | 当届官方 Word 模板与格式规范；`官方模板转换.docx` 是为生成阶段稿制作的副本。正式汇编以原始官方模板和规范为准。 |
| [`references/2025_A/`](references/2025_A/) | 方法对照中阅读的四篇 2025 年 A 题优秀论文。 |
| [`references/format/`](references/format/) | 上届论文格式规范，仅供历史对照。 |
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
