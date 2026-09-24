# 问题二 P2-0 交接包

这是原机器上的场景 B 基线评估快照，非当前 Git 提交的重新测量。实验覆盖 3 个 case × 3 种核数 × 6 种问题一方案，共 54 项，成功 54 项。结论与限制见 `P2-0_交叉评估分析.md`，逐项指标见 `p20_results.csv`，汇总见 `p20_summary.json`。

目录内容：`plans/` 为当时参与评估的方案，`records/` 为评估记录，`p20_identity.json` 为历史代码和配置哈希，`frozen_groups/` 为九组冻结候选的签名记录。历史记录中包含原机器的绝对路径、原 Git HEAD 和源码哈希；它们用于溯源，不应视为新设备上的有效缓存。原报告中的本机绝对路径命令也仅供溯源。

从仓库根目录复现实验（需本机 Python 环境）：

```powershell
python .\run_problem2_p20.py --repo . --output .\local_results\problem2_p20
```

脚本优先读取本地 `artifacts/problem1_full_c4140/groups/`，若不存在则使用本目录的 `frozen_groups/`。它会在目标输出目录写入新结果；建议先选新目录，不要覆盖本交接包。原问题一候选文件若不存在，脚本会用仓库内问题一求解器重建，并以冻结签名校验。方案生成可能需要数分钟。更小的冒烟测试可用 `--cases case_001 --cores 2 --limit 2`。

供另一窗口使用的完整实现提示词在 `docs/problem2_implementation_prompt.md`。本交接包不包含原机器的整个 `artifacts/` 目录、未提交修改或外部文献 PDF。
