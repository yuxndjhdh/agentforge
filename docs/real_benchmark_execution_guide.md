# AgentForge 真实 Benchmark 执行指南

## 1. 目的

本指南用于执行 AgentForge `v0.3.0` 的真实模型 A/B/C/D 消融实验：

- 23 个固定 Code Agent 任务；
- 每个任务 5 个独立 trial；
- 每组 115 个 episode；
- 四组共 460 个 episode；
- Verify 组每个 episode 最多执行 3 次 Agent attempt。

最终产物为四组原始 JSON、完整 episode trace，以及由原始数据自动生成的 `docs/benchmark_report.md`。

## 2. 实验定义

| 实验 | 上下文压缩 | Verify retry | 最大尝试次数 | 输出目录 |
| --- | --- | --- | ---: | --- |
| A | 关闭 | 关闭 | 1 | `runs/benchmarks/v0.3.0-A` |
| B | 开启 | 关闭 | 1 | `runs/benchmarks/v0.3.0-B` |
| C | 关闭 | 开启 | 3 | `runs/benchmarks/v0.3.0-C` |
| D | 开启 | 开启 | 3 | `runs/benchmarks/v0.3.0-D` |

除上述两个实验变量外，四组必须使用相同的：

- AgentForge commit；
- 模型 ID 和模型版本/日期；
- temperature 和最大步骤数；
- 上下文预算与 tokenizer；
- sandbox 配置；
- 任务顺序、任务版本、trial 数和 seed；
- 输入/输出 token 单价。

## 3. 当前已验证状态

截至 2026-09-10：

- `HARNESS_LLM_KEY` 已能被 AgentForge 正常读取；
- `deepseek-v4-flash-vision-exp` 完成了 `fix-add` 真实 smoke test；
- smoke test 为 1/1 成功，5 个步骤，输入 13,758 token，输出 590 token，耗时约 10.75 秒；
- smoke report 中 API key 已替换为 `<redacted>`；
- smoke 产物中未检测到疑似 API key；
- A/B/C/D CLI 开关和 benchmark report schema 已实现；
- Docker daemon 可用，但当前 overlayfs 磁盘 quota 无法被项目验证；
- `auto` backend 因此会回退到 local backend；
- 当前工作区不是 clean 状态，不能直接生成正式发布证据；
- 当前输入/输出成本单价为 0，未配置前报告只能记录 token，不能给出有效成本。

已暴露在聊天或日志中的旧 key 必须撤销。正式实验只能使用重新生成、仅保存在本机 `.env` 中的新 key。

## 4. 正式运行前置条件

### 4.1 冻结代码版本

先完成计划内代码、测试和文档修改，再提交到固定 commit。

```powershell
git status --short
git rev-parse HEAD
```

完成标准：

- `git status --short` 没有输出；
- 记录完整 commit SHA；
- 四组实验期间不修改代码或任务定义；
- `report.json` 中 `workspace_dirty` 必须为 `false`；
- 四个报告中的 `git_commit` 必须完全一致。

### 4.2 运行代码质量检查

```powershell
.venv\Scripts\python.exe -m ruff check agentforge tests scripts
.venv\Scripts\python.exe -m mypy agentforge
.venv\Scripts\python.exe -m pytest
.venv\Scripts\python.exe -m compileall -q agentforge
```

所有检查通过后再运行真实实验。失败时先修复并重新冻结 commit，不能带着失败的测试继续采集正式数据。

### 4.3 固定模型配置

在不会提交到 Git 的 `.env` 中配置：

```dotenv
HARNESS_LLM_BASE=https://api.deepseek.com
HARNESS_LLM_MODEL=deepseek-v4-flash-vision-exp
HARNESS_LLM_KEY=<仅保存在本机的新密钥>
HARNESS_LLM_TEMPERATURE=0.0
HARNESS_MAX_STEPS=20
HARNESS_MAX_CONTEXT_CHARS=7500
HARNESS_MAX_CONTEXT_TOKENS=
HARNESS_CONTEXT_MIN_TAIL=4
HARNESS_TOKENIZER=auto
HARNESS_LLM_TIMEOUT=120.0
HARNESS_LLM_RETRIES=2
HARNESS_LLM_BACKOFF=0.5
HARNESS_INPUT_COST_PER_MILLION=<运行当日的真实输入单价>
HARNESS_OUTPUT_COST_PER_MILLION=<运行当日的真实输出单价>
```

模型 ID 必须以供应商控制台实际支持的值为准。若供应商无法提供固定快照，应在报告中记录运行日期，并说明实验模型可能随供应商更新而变化。

只检查 key 是否存在，禁止打印 key：

```powershell
.venv\Scripts\python.exe -c "from agentforge.config import load_config; c=load_config(); print({'base_url': c.base_url, 'model': c.model, 'key_configured': bool(c.api_key), 'temperature': c.temperature})"
```

### 4.4 固定 sandbox 配置

正式 Code Agent 性能实验使用项目内置、可信的 seed 仓库时，可以临时关闭当前 Docker Desktop 无法证明的磁盘 quota，但必须强制使用 Docker，不能允许 `auto` 静默回退：

```dotenv
HARNESS_SANDBOX_BACKEND=docker
HARNESS_SANDBOX_DISK_MB=0
HARNESS_SANDBOX_NETWORK=0
HARNESS_SANDBOX_CPU=1.0
HARNESS_SANDBOX_MEMORY_MB=512
HARNESS_SANDBOX_PIDS=128
```

执行诊断：

```powershell
docker version
.venv\Scripts\python.exe -m agentforge sandbox diagnose
```

这只能用于可信 seed 的性能 benchmark。磁盘 quota 被关闭，不能把本次结果作为 R1 完整容器安全验收证据。R1 仍需在支持并能验证磁盘 quota 的 Linux Docker runner 上单独完成。

## 5. 正式实验前 smoke test

### 5.1 基础模型 smoke

```powershell
.venv\Scripts\python.exe -m agentforge benchmark --tasks fix-add --trials 1 --k 1 --no-context-compression --no-verify-retry --out runs/benchmarks/smoke-A
```

完成标准：

- `tasks=1`、`episodes=1`；
- reward 为 1；
- trace 文件存在；
- 有非零输入/输出 token；
- report 中 key 为 `<redacted>`；
- 日志明确使用 Docker，未出现 local fallback。

### 5.2 上下文压缩 smoke

```powershell
.venv\Scripts\python.exe -m agentforge benchmark --tasks fix-add --trials 1 --k 1 --context-compression --no-verify-retry --out runs/benchmarks/smoke-B
```

检查 `summary.compression_triggered_episodes`。如果为 0，应使用更长的代表性任务继续预检。若所有任务都无法在固定预算下触发压缩，则 B/D 消融无效，不能对压缩效果下结论。

如果决定调整上下文预算，应在正式实验开始前完成，并在 A/B/C/D 四组中保持同一个预算值。不能查看正式结果后再为某一组单独调整预算。

本次正式实验固定使用 `7500` 字符预算：`fix-add` 和 `fix-cache` 在原始
`45000` 字符预算下都没有触发压缩；`fix-cache` 在 `8000` 字符预算下最后
一次请求为约 `7944` 字符，仍未触发，因此统一降至 `7500` 后再验证 B/D
的实际触发情况。

### 5.3 Verify retry smoke

```powershell
.venv\Scripts\python.exe -m agentforge benchmark --tasks fix-add --trials 1 --k 1 --no-context-compression --verify-retry --verify-attempts 3 --out runs/benchmarks/smoke-C
```

检查 episode 的 `attempts` 和 `attempt_count`。如果第一次尝试已经成功，`attempt_count=1` 是正确行为；还应使用 fake agent 自动测试证明“首次失败、第二次成功”的 retry 路径已经覆盖。

## 6. 成本与时间预估

刚才的单任务 smoke test 消耗：

- 输入 token：13,758；
- 输出 token：590；
- 耗时：约 10.75 秒。

以该样本粗略估算：

- 不发生额外 retry：约 460 次 Agent 执行；
- C/D 全部用满 3 次尝试：最多约 920 次 Agent 执行；
- 粗略输入量约为 633 万至 1,266 万 token；
- 粗略输出量约为 27 万至 54 万 token；
- 仅按该样本线性估算约 1.4 至 2.8 小时。

不同任务复杂度、模型限流和重试会显著影响结果。正式费用计算公式为：

```text
总成本 = 输入 token / 1,000,000 × 输入单价
       + 输出 token / 1,000,000 × 输出单价
```

在供应商控制台确认预算足够后再开始正式运行。

## 7. 执行 A/B/C/D

四组顺序运行，不建议并行，以减少本机资源竞争和 API 限流对实验的干扰。

运行前确认目标目录不存在：

```powershell
Test-Path runs/benchmarks/v0.3.0-A
Test-Path runs/benchmarks/v0.3.0-B
Test-Path runs/benchmarks/v0.3.0-C
Test-Path runs/benchmarks/v0.3.0-D
```

如果已有旧结果，不要把新旧 episode 混在同一目录中；改用带日期或 revision 的新目录。

### 7.1 实验 A：无压缩、无 Verify

```powershell
.venv\Scripts\python.exe -m agentforge benchmark --trials 5 --k 5 --seed 0 --no-context-compression --no-verify-retry --out runs/benchmarks/v0.3.0-A
```

### 7.2 实验 B：启用压缩、无 Verify

```powershell
.venv\Scripts\python.exe -m agentforge benchmark --trials 5 --k 5 --seed 0 --context-compression --no-verify-retry --out runs/benchmarks/v0.3.0-B
```

### 7.3 实验 C：无压缩、启用 Verify

```powershell
.venv\Scripts\python.exe -m agentforge benchmark --trials 5 --k 5 --seed 0 --no-context-compression --verify-retry --verify-attempts 3 --out runs/benchmarks/v0.3.0-C
```

### 7.4 实验 D：启用压缩和 Verify

```powershell
.venv\Scripts\python.exe -m agentforge benchmark --trials 5 --k 5 --seed 0 --context-compression --verify-retry --verify-attempts 3 --out runs/benchmarks/v0.3.0-D
```

当前 runner 在每组结束时写入最终 `report.json`。运行中断后不要把残留目录直接当作完整报告，应使用新的输出目录重新运行该组，或在实现可靠的 episode 级断点续跑后再恢复。

## 8. 每组结果校验

每运行完一组，执行以下 PowerShell 检查，将 `A` 替换为对应实验字母：

```powershell
$variant = "A"
$path = "runs/benchmarks/v0.3.0-$variant/report.json"
$report = Get-Content $path -Raw | ConvertFrom-Json

if ($report.experiment_id -ne $variant) { throw "experiment_id mismatch" }
if ($report.summary.tasks -ne 23) { throw "expected 23 tasks" }
if ($report.summary.episodes -ne 115) { throw "expected 115 episodes" }
if ($report.summary.num_trials -ne 5) { throw "expected 5 trials" }
if ($report.workspace_dirty -ne $false) { throw "workspace was dirty" }
if ($report.config.api_key -ne "<redacted>") { throw "API key is not redacted" }

$badGroups = $report.episodes | Group-Object task | Where-Object Count -ne 5
if ($badGroups) { throw "one or more tasks do not have 5 episodes" }

$missingTraces = $report.episodes | Where-Object { -not (Test-Path $_.trace_path) }
if ($missingTraces) { throw "one or more trace files are missing" }

$report.summary | Format-List
```

校验 B、D：

```powershell
if ($report.summary.compression_triggered_episodes -eq 0) {
    Write-Warning "compression did not trigger; no compression conclusion is valid"
}
```

校验 C、D：

```powershell
if ($report.summary.avg_attempts -le 1) {
    Write-Warning "Verify was enabled but no episode retried"
}
```

## 9. 跨组一致性检查

```powershell
$reports = "A", "B", "C", "D" | ForEach-Object {
    Get-Content "runs/benchmarks/v0.3.0-$_/report.json" -Raw | ConvertFrom-Json
}

if (($reports.git_commit | Select-Object -Unique).Count -ne 1) {
    throw "reports use different commits"
}

if (($reports.model | Select-Object -Unique).Count -ne 1) {
    throw "reports use different models"
}

$taskOrders = $reports | ForEach-Object { $_.task_order -join "," }
if (($taskOrders | Select-Object -Unique).Count -ne 1) {
    throw "reports use different task orders"
}
```

除 `context_compression_enabled`、`verify_enabled` 和 `max_attempts` 外，还应人工比较四个报告的 `config`，确认没有其他影响实验的配置变化。

## 10. 生成 Markdown 报告

四组全部通过校验后执行：

```powershell
.venv\Scripts\python.exe scripts\summarize_benchmark.py `
  runs\benchmarks\v0.3.0-A\report.json `
  runs\benchmarks\v0.3.0-B\report.json `
  runs\benchmarks\v0.3.0-C\report.json `
  runs\benchmarks\v0.3.0-D\report.json `
  --out docs\benchmark_report.md
```

生成的 `docs/benchmark_report.md` 应包含：

- A/B/C/D 配置矩阵；
- pass@1、pass@3、pass@5；
- 首次成功率和最终成功率；
- Verify 增益和额外成本；
- p50/p95 延迟；
- 平均步骤和 token；
- 压缩触发率；
- A→B、A→C、A→D 的差值；
- commit、模型和任务信息；
- 5 trials 的统计限制说明。

## 11. 人工审计

- [ ] 从每种失败类型中至少抽查 3 个 trace。
- [ ] 确认失败分类与实际行为一致。
- [ ] 确认 Verify 没有通过修改验收脚本获得 reward。
- [ ] 确认所有命令检查来自独立终态验收，而不是模型自述。
- [ ] 确认 B/D 实际发生压缩后再讨论压缩效果。
- [ ] 确认 C/D 实际发生 retry 后再讨论 Verify 增益。
- [ ] 对没有触发或样本不足的能力明确写“无法得出结论”。
- [ ] `readme.md`、简历和 release 只引用 `benchmark_report.md` 中的数据。

## 12. 完成定义

以下条件全部满足后，才能将“真实 A/B/C/D 已执行”标记为完成：

- [ ] 四组报告来自同一 clean commit。
- [ ] 每组都是 23 tasks × 5 trials。
- [ ] 四组模型和非实验变量完全一致。
- [ ] 所有 episode、attempt 和 trace 均可访问。
- [ ] API key 未进入报告、trace、日志或 Git。
- [ ] pass@k 使用标准“至少一次成功”公式并保留零成功任务。
- [ ] 成本单价不是未说明的 0。
- [ ] B/D 压缩触发情况已验证。
- [ ] C/D Verify attempt 情况已验证。
- [ ] `docs/benchmark_report.md` 由四份原始 JSON 自动生成。
- [ ] Docker 性能实验与 R1 磁盘 quota 安全缺口被明确区分。
