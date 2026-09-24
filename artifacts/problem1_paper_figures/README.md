# 问题一论文图表复现说明

本目录只读取以下冻结实验结果，不修改求解器、官方评估器、配置或源结果：

- `artifacts/problem1_inherited_fallback_c4140/`：最终 aggregate、results、appendix CSV；
- `artifacts/problem1_full_c4140/`：仅用于继承回退前对照。

在 `code` 目录执行：

```powershell
python artifacts/problem1_paper_figures/scripts/generate_all.py
```

也可分别执行：

```powershell
python artifacts/problem1_paper_figures/scripts/prepare_problem1_paper_data.py
python artifacts/problem1_paper_figures/scripts/plot_fig1_average_speedup.py
python artifacts/problem1_paper_figures/scripts/plot_fig2_speedup_distribution.py
python artifacts/problem1_paper_figures/scripts/plot_fig3_fallback_improvements.py
```

脚本默认从 `~/.codex/skills/math-modeling` 加载“科研可视化工具”。若安装位置不同，设置环境变量 `MATH_MODELING_SKILL_ROOT`。

输出结构：

- `data/`：三张图对应的绘图数据及问题一逐例附录表；
- `figures/`：SVG、600 DPI PNG 与灰度预览；严格门禁还会导出 PDF/TIFF 作为补充格式；
- `docs/`：题面要求核对、图表契约、中文图题和自足图注，以及问题二、三待补数据需求；
- `qa/`：数据完整性与程序视觉检查记录。
