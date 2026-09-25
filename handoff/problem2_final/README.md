# 问题二最终全量运行手册

> 本手册的数值口径唯一来自未修改的官方场景 B 评估器。最终组合不修改问题一、问题三算法或官方代码。

## 1. 冻结方案

每个 2–5 核 case-core 显式保留 12 个逻辑候选：

1. 阶段一 `selected_results.csv` 的实际最终赢家，作为全局回退；
2. 题目要求的字面 `single`；
3. `B0、B1、B2A-w4、B2A-w8、B2A-w16` 五个切图的原映射；
4. 上述五个切图的冻结 `map_locality` 映射，参数为 `max_moves=2, search_width=12`。

3–5 核再增加“上一核数最终赢家追加空核”的递归继承候选。所有新候选都通过官方 plan 接口、合法性检查和官方场景 B 评估；最终按 `(Makespan, added_copy_bytes)` 字典序选择，完全平局时按冻结候选优先级确定性破局。

阶段三已验证的两个核内排序候选在 3 个预注册探针上未带来任何最终 case-core 改善，因此不扩展该分支。截止时间下不把未验证的切图边界修补放入最终留出集；这是资源决策，不是宣称这类方法普遍无效。

## 2. 服务器预检

在仓库 `code/` 目录中执行：

```bash
git status --short
git rev-parse HEAD
python -m unittest discover -s tests -v
df -h / /media/dell/data
du -sh artifacts/problem2_stage1_baseline_cold_audit \
  /media/dell/data/yn/problem2_final_cache_v1 2>/dev/null
```

要求：新增测试与全仓回归全部通过；阶段一冷审计目录存在；不覆盖同名但身份不同的 output。当 sda5 或 sda6 可用空间低于 15 GiB 时先停止扩大。

## 3. 建议的分阶段执行

以下命令共用同一 output 和 cache，可以安全断点续跑。cache 放在 sda6，正式报告与最终 plan 放在当前 sda5 工作区。

### 3.1 12 例开发集技术验收

```bash
python -u run_problem2_final.py \
  --repo . \
  --baseline artifacts/problem2_stage1_baseline_cold_audit \
  --output artifacts/problem2_final_v1 \
  --cache /media/dell/data/yn/problem2_final_cache_v1 \
  --phase development \
  --workers 24
```

继续条件：`requested_complete=true`，60/60 case-core 完成，`fatal_invocation_failures=0`，`candidate_failures=0`，所有最终格子相对阶段一回退无退化。

### 3.2 18 例验证集

```bash
python -u run_problem2_final.py \
  --repo . \
  --baseline artifacts/problem2_stage1_baseline_cold_audit \
  --output artifacts/problem2_final_v1 \
  --cache /media/dell/data/yn/problem2_final_cache_v1 \
  --phase validation \
  --workers 24
```

继续条件：90/90 新请求的 case-core 完成，零基础设施失败，新候选失败已定位或为零。科学收益无论正负都如实保留，不再修改规则。

### 3.3 完成全部 100 例

```bash
python -u run_problem2_final.py \
  --repo . \
  --baseline artifacts/problem2_stage1_baseline_cold_audit \
  --output artifacts/problem2_final_v1 \
  --cache /media/dell/data/yn/problem2_final_cache_v1 \
  --phase all \
  --workers 24
```

`--phase all` 会复用前两批已通过严格身份和文件哈希验证的结果，只执行缺失组。最终要求 `recorded_case_core_cells=500`、`full_500_complete=true`、`fatal_failures=0`。候选失败不会删除该格子的阶段一官方回退，但所有失败仍必须在 `failures.csv` 报告。

如果时间极紧，可直接运行 3.3；上述分阶段方式更便于在不消耗留出集前发现环境问题。

## 4. 断点续跑与监控

```bash
watch -n 60 'df -h / /media/dell/data; du -sh artifacts/problem2_final_v1 /media/dell/data/yn/problem2_final_cache_v1 2>/dev/null'
```

中断后原样重跑同一命令即可。每个分组只有在运行身份、最终 plan、官方结果及对应哈希同时一致时才会续跑；损坏或旧版分组会局部失效并重算。不得让两个主进程同时写入同一 output/cache。

## 5. 最终交付文件

`artifacts/problem2_final_v1/` 中应至少包含：

- `selected_results.csv`：500 行最终官方结果及相对阶段一的严格配对差；
- `candidate_results.csv`：全部候选、去重、合法性、缓存、时间与哈希；
- `aggregate_by_core.csv`：1–5 核逐例加速比的算术平均、中位数、尾部分位数、最差值、搬运、Spill、跨核负载和利用率；
- `paired_candidate_summary.csv`：各逻辑策略相对阶段一实际赢家的配对效应量与符号检验；
- `tail_degradation.csv`：多核加速比低于 1 的尾部格子；
- `runtime_by_candidate.csv`：求解、合法性、缓存查找、官方评估、实际墙钟与冷缓存估计；
- `failures.csv`：候选失败和基础设施失败，即使为空也保留固定表头；
- `summary.json`、`run_identity.json`、`cache_key_summary.json`、`invocation_history.json`：完整性、输入/配置/实现/评估器哈希和断点续跑历史；
- `groups/<case>/k*/`：最终 plan、官方原始结果以及递归继承 plan；`mapping/` 保留五切图原映射与 `map_locality` 的完整子记录。

## 6. 已通过的本机最小验收

`artifacts/problem2_final_smoke_p1_k3_v4` 覆盖 `case_001` 的 1–3 核：3/3 case-core 成功，27 条候选记录，0 候选失败，0 基础设施失败；2 核显式记录 12 个候选，3 核记录 13 个并已实际评估 `inherit_final_k2`。第一次端到端墙钟 58.788 秒（身份和基线哈希预检 53.076 秒，实际执行 5.712 秒）；第二次原样调用为 `executed=0, resumed=3`。该 smoke 只证明技术链路，不代表 100 例科学结论。
