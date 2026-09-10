# AgentForge Code Agent Benchmark 完成计划

## 1. 目标

本计划用于完成 AgentForge `v0.3.0` 的真实 Code Agent Benchmark，并产出可以写入 README、Release 和简历的可追溯实验结果。

最终需要回答四个问题：

1. AgentForge 在固定代码任务集上的实际成功率是多少？
2. 上下文压缩是否改善长任务表现或降低 token 成本？
3. Verify retry 是否能提高最终成功率，其额外延迟和成本是多少？
4. 所有汇总数字能否追溯到具体 episode、trace、代码提交和模型配置？

本计划不使用 `agent-eval-harness` 的订单任务成绩代替 Code Agent 成绩。该项目只作为评测设计参考和可选的跨领域对照实验。

## 2. 当前差距

已有能力：

- 23 个固定、带版本信息的 Code Agent benchmark 任务。
- 临时代码仓库、文件断言、命令检查和 gold tree 终态判定。
- `pass@1`、标准 `pass@k`、延迟、步骤数、token、估算成本和失败分类。
- episode、失败 trace 和原子化 JSON 报告。
- 上下文压缩实现和独立的 Verify retry 实现。

本次实现已补齐正式实验所需的软件能力；真实实验仍需要模型凭据：

- `benchmark` 命令现在有显式的上下文压缩和 Verify retry 开关。
- `benchmark` runner 现在复用共享 episode executor，支持同一 agent 会话内的 Verify retry。
- schema 2 报告记录实验变体、Git commit、dirty 状态、attempt、压缩实际触发和增量成本。
- 没有 `HARNESS_LLM_KEY`，尚未运行真实模型实验。
- `agent-eval-harness` 当前使用订单领域任务，且其 `pass@k` 公式不适合作为正式 Code Agent 报告依据。

## 3. 实验定义

四组实验固定使用同一个代码提交、模型、temperature、任务顺序、任务版本和 seed。

| 实验 | 上下文压缩 | Verify retry | 最大尝试次数 | 输出目录 |
| --- | --- | --- | ---: | --- |
| A | 关闭 | 关闭 | 1 | `runs/benchmarks/v0.3.0-A` |
| B | 开启 | 关闭 | 1 | `runs/benchmarks/v0.3.0-B` |
| C | 关闭 | 开启 | 3 | `runs/benchmarks/v0.3.0-C` |
| D | 开启 | 开启 | 3 | `runs/benchmarks/v0.3.0-D` |

每组运行 23 个任务，每个任务 5 个独立 episode，即每组 115 个 episode。C、D 每个 episode 最多包含 3 次尝试，因此正式运行前必须先做单任务成本预估。

指标定义：

- `pass@1`：每个任务单次独立 episode 的平均成功率。
- `pass@3`、`pass@5`：使用 `1 - C(N-c,k) / C(N,k)` 计算至少一次成功概率。
- `first_success_rate`：第一次尝试即成功的任务比例。
- `final_success_rate`：允许 Verify retry 后最终成功的任务比例。
- 成本、token、延迟和步骤数必须包含所有 Verify 尝试，不能只统计最后一次。
- B、D 必须记录实际压缩次数；如果所有 episode 都没有触发压缩，则上下文压缩消融无效，不得据此下结论。

## 4. 实施任务

### P0：冻结实验协议

- [x] 为 benchmark 模式定义结构化配置 `BenchmarkOptions`。
- [x] 固定四组实验的开关、最大尝试次数、任务集、trial 数和 seed 接口。
- [x] 固定上下文预算；开启组使用同一个可触发且合理的预算，关闭组完全绕过压缩逻辑。
- [x] 明确 Verify 的一次 `episode` 可以包含多次 attempt，但 pass@k 的 trial 数仍按 episode 计算。
- [x] 将实验协议写入报告，禁止仅通过输出目录名称推断 A/B/C/D 配置。

验收标准：给定报告即可判断该实验属于 A、B、C、D 中哪一组。

### P1：增加显式上下文压缩开关

- [x] 在 `ModelConfig` 中增加布尔配置 `context_compression_enabled`。
- [x] 支持环境变量 `HARNESS_CONTEXT_COMPRESSION=0|1`。
- [x] `CompactingModel.generate()` 在关闭时原样发送消息，并记录 `disabled` 状态。
- [x] 开启时保留当前字符/token 双预算压缩逻辑。
- [x] trace 和 benchmark report 记录压缩是否启用、是否触发、触发次数、丢弃步骤数和节省字符/token。
- [x] 为开启、关闭、超过预算但不可压缩三种状态增加测试。

验收标准：同一输入在关闭模式下没有压缩事件；开启且超过预算时产生可追溯压缩记录。

### P2：将 Verify retry 接入 benchmark

- [x] 为 `solve_task()` 和 `evaluate()` 增加 `verify_enabled`、`max_attempts` 参数。
- [x] 关闭 Verify 时保持当前单次执行行为。
- [x] 开启 Verify 时，在同一临时仓库、同一 Agent 会话中执行“运行、验收、反馈、重试”闭环。
- [x] 每次 attempt 都保存 reward、feedback、trace 步骤、token、耗时和失败类型。
- [x] episode 的最终 reward 取最后成功状态；成功后立即停止，不继续消耗模型调用。
- [x] 临时目录的创建、清理和 `keep_workdir` 行为在两种模式下保持一致。
- [x] `eval.py` 与 `verify.py` 复用共享 episode executor。

验收标准：构造“首次失败、第二次成功”的 fake agent，关闭 Verify 得 0，开启 Verify 得 1，且报告包含两次 attempt。

### P3：扩展 CLI 和报告 Schema

- [x] `agentforge benchmark` 增加显式开关：
  - `--context-compression` / `--no-context-compression`
  - `--verify-retry` / `--no-verify-retry`
  - `--verify-attempts 3`
- [x] 对冲突参数和非法尝试次数快速失败。
- [x] 报告增加 `experiment_id`、实验开关、最大尝试次数、Git commit、工作区 dirty 状态和任务集摘要。
- [x] 报告按 episode 保存所有 attempt，而不是覆盖前一次结果。
- [x] 汇总增加压缩触发率、平均尝试次数、首次/最终成功率差值和 Verify 增量成本。
- [x] 更新 `schema_version`，并为旧报告加载保留清晰的兼容或拒绝策略。
- [x] API benchmark 使用同一组选项和报告生成路径，并持久化实验元数据。

验收标准：CLI、API 和 Python runner 对同一配置生成等价的实验元数据。

### P4：自动化测试与本地无模型验证

- [x] 测试 A/B/C/D 四组配置到执行选项的映射。
- [x] 测试上下文压缩关闭、开启和真实触发记录。
- [x] 测试 Verify 首次成功、重试成功、达到上限仍失败。
- [x] 测试多 attempt 的 token、耗时、步骤和成本聚合。
- [x] 测试 0 次成功的任务仍进入 `pass@k` 分母。
- [x] 测试报告包含 commit、dirty 状态、任务版本和所有 trace 路径。
- [x] 使用 fake model/JSON 生成四组小型报告，验证 schema 和对比脚本。
- [x] 运行 `ruff`、`mypy`、`pytest`、coverage 和 `compileall`。

验收标准：无 API key 时也能证明实验编排、指标计算和报告完整性正确。

### P5：真实模型预检

- [ ] 在不提交 Git 的 `.env` 中配置 `HARNESS_LLM_BASE`、`HARNESS_LLM_MODEL`、`HARNESS_LLM_KEY`。
- [ ] 配置模型版本或快照日期、temperature、最大步数、tokenizer 和输入/输出单价。
- [ ] 确认 key 不会写入 config 快照、trace、异常或 CI 日志。
- [ ] 使用 Docker backend；若测试仓库完全可信，可先用 local backend 做成本 smoke test，但不得作为正式安全实验。
- [ ] 先运行 1 个任务、1 个 trial，检查工具调用、验收、trace、token 和实际账单。
- [ ] 再分别 smoke test B 的压缩触发和 C 的 Verify 重试。
- [ ] 根据 smoke test 估算 460 个正式 episode 及最多 920 次 Agent 尝试的时间和费用。

验收标准：小规模真实运行成功，报告不泄露密钥，B 确实触发压缩，C 能记录多 attempt。

### P6：运行 A/B/C/D 正式实验

PowerShell 命令模板：

```powershell
$env:HARNESS_CONTEXT_COMPRESSION = "0"
python -m agentforge benchmark --trials 5 --k 5 --no-context-compression --no-verify-retry --out runs/benchmarks/v0.3.0-A

$env:HARNESS_CONTEXT_COMPRESSION = "1"
python -m agentforge benchmark --trials 5 --k 5 --context-compression --no-verify-retry --out runs/benchmarks/v0.3.0-B

$env:HARNESS_CONTEXT_COMPRESSION = "0"
python -m agentforge benchmark --trials 5 --k 5 --no-context-compression --verify-retry --verify-attempts 3 --out runs/benchmarks/v0.3.0-C

$env:HARNESS_CONTEXT_COMPRESSION = "1"
python -m agentforge benchmark --trials 5 --k 5 --context-compression --verify-retry --verify-attempts 3 --out runs/benchmarks/v0.3.0-D
```

以上命令是目标接口，必须在 P1～P3 实现并通过测试后执行。

每组运行完成后立即检查：

- [ ] 恰好包含 23 个任务、每任务 5 个 episode。
- [ ] 所有 episode 都有 reward、trace、耗时、步骤数和 token 数据。
- [ ] 所有失败和重试都有完整 trace。
- [ ] 实验开关与目录名称一致。
- [ ] 模型配置、commit、task version 和 seed 完整。
- [ ] B、D 的压缩触发数据有效。
- [ ] C、D 的 attempt 数据和最终 reward 一致。

### P7：生成正式 Benchmark 报告

- [x] 增加确定性的四组报告汇总脚本 `scripts/summarize_benchmark.py`，输入为四个 `report.json`。
- [ ] 生成 `docs/benchmark_report.md`，不手工抄写实验数字。
- [ ] 报告包含 pass@1/pass@3/pass@5、首次/最终成功率、p50/p95 延迟、平均步骤、token、成本和失败分类。
- [ ] 对比 A→B、A→C、A→D，分别说明压缩、Verify 及组合效果。
- [ ] 报告置信区间或至少说明 5 trials 的统计限制。
- [ ] 人工抽查每种主要失败类型至少 3 个 trace，并记录代表性案例。
- [ ] 对未触发的能力或无统计显著性的差异明确写“无法得出结论”。

验收标准：报告中的每一个数值都能回溯到 JSON episode 和 trace，重新运行汇总脚本可得到相同结果。

## 5. agent-eval-harness 的使用边界

### 正式 Benchmark

不使用 `agent-eval-harness` 代替 AgentForge 自带 benchmark，原因如下：

- 其 `Task`、工具和 `Env` 绑定订单数据库，不是代码仓库。
- 终态 reward 只判断订单状态和回复文本，不运行代码测试。
- 当前 `pass@k` 不是“至少一次成功”的标准公式，并且零成功任务可能不进入平均分母。
- 它没有 AgentForge 所需的 sandbox、代码 trace、上下文压缩和 Verify 消融字段。

### 可选对照实验

在正式 Code Agent 报告完成后，可以单独做一个跨领域附录：

- [ ] 修正 `agent-eval-harness` 的 `pass@k` 公式并补充零成功任务测试。
- [ ] 使用相同模型分别运行 Tau Tool Calling 和 smolagents adapter。
- [ ] 报告订单任务的 pass@1/pass@k 和失败类型。
- [ ] 明确标注为“通用工具调用能力对照”，不与 Code Agent 成绩合并。

只有在需要展示“同一个评测思想可跨 Agent 框架和任务领域复用”时，才值得进一步实现 `CodeRepoEnv` 和 `AgentForgeAgentAdapter`。这不是完成 R2 的前置条件。

## 6. 后续发布链路

Benchmark 完成后按以下顺序收尾：

1. 在 Linux Docker 环境完成容器安全 live 验收并生成最终安全报告。
2. 推送 release 分支，取得 Python 3.11/3.12/3.13、API E2E 和 packaging 的远端 CI 记录。
3. 从 clean clone 复现安装、测试、CLI、API 和 benchmark 报告生成。
4. 将 `benchmark_report.md`、安全报告、CI 链接和 commit/tag 绑定到同一个 release。
5. 仅使用正式报告中有证据支持的数据更新 README 和简历描述。

## 7. 完成定义

以下条件全部满足后，Code Agent Benchmark 才算完成：

- [ ] A/B/C/D 是代码显式控制的真实消融，不是四个不同目录名。
- [ ] 四组真实模型实验均为 23 tasks × 5 trials。
- [ ] 标准 `pass@k` 包含零成功任务。
- [ ] 所有 episode、attempt、失败 trace 和配置快照完整。
- [ ] 压缩和 Verify 的实际触发情况可验证。
- [ ] `docs/benchmark_report.md` 可由原始 JSON 自动重建。
- [ ] 报告绑定模型信息、任务版本和 Git commit。
- [ ] 密钥未进入仓库、报告、trace 或日志。
- [ ] 正式结论不混用订单任务和代码任务成绩。

## 8. 推荐执行顺序

```text
P0 冻结实验协议
  -> P1 上下文压缩开关
  -> P2 Benchmark 接入 Verify
  -> P3 CLI/API/报告 Schema
  -> P4 无模型自动化验证
  -> P5 真实模型小规模预检
  -> P6 A/B/C/D 正式运行
  -> P7 自动生成报告
  -> Docker/CI/clean-clone/release 收尾
```

近期最优先工作是 P0～P4。它们不依赖 API key 或 Docker，可以先把真实实验所需的软件能力补齐，避免取得凭据后才发现实验协议无法执行。

## 9. 本次执行记录

执行日期：2026-09-10（Windows，Python 3.13.5）。

- P0～P4：已实现并通过无模型验证；完整测试为 `111 passed, 1 skipped`，覆盖率 `77.87%`，`ruff`、`mypy`、`compileall`、`pip check` 和 `git diff --check` 通过。
- P7 软件部分：`scripts/summarize_benchmark.py` 已实现并通过四份 fake report 的 schema、变体和 delta 测试；没有真实输入时不生成正式 `docs/benchmark_report.md`。
- Docker 预检：固定 digest 镜像已拉取，容器 live integration tests 为 `3 passed`；overlayfs 的磁盘 quota 仍为 `requires-live-probe`，未标记为已验证。
- P5 未完成：当前环境没有 `HARNESS_LLM_KEY`，因此未执行真实模型 smoke test，也没有记录账单或正式模型结论。
- P6 未完成：没有运行 23 tasks × 5 trials 的 A/B/C/D 真实实验，未生成四组正式 `report.json`。
- 正式报告与发布仍保持阻塞，避免把 fake solver 或本地 fallback 结果写入 benchmark 结论。
