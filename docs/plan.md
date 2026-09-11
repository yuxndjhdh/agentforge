# AgentForge 项目升级计划

## 1. 项目定位

将 AgentForge 从一个基于 `smolagents` 的本地代码 Agent MVP，升级为：

> 面向代码仓库的可靠 Agent 执行、评测与安全沙箱平台。

核心工程闭环：

```text
任务输入
  -> Agent 执行
  -> 工具权限控制
  -> 运行状态与 checkpoint
  -> 自动验收
  -> 失败反馈与重试
  -> trace / 指标 / 成本记录
  -> benchmark 对比
```

项目最终应能证明三件事：

1. Agent 能够稳定完成代码仓库任务。
2. Agent 的行为、成本、失败原因和最终结果可审计、可复现。
3. Agent 的执行边界受到真正的进程级和资源级约束。

## 2. 当前基线

### 已有能力

- 基于 `smolagents` 的 Tool Calling Agent。
- 文件读取、写入、目录浏览、grep、命令执行和分层记忆工具。
- OpenAI 兼容模型接入和请求级上下文压缩。
- JSON trace 落盘以及同一进程内的 `reset=False` 续跑。
- 临时副本上的终态 hash 和 checks 评测。
- verify -> feedback -> retry 闭环。
- 工具级白名单、拒绝规则、只读模式、超时和环境变量清理。
- 49 个单元测试，`selftest` 和 `compileall` 已通过。

### 当前主要问题

- `pass@k` 实现不是标准的“至少一次成功”定义，且失败任务没有进入平均分母。
- command check 不检查退出码，并将 stderr 与 stdout 混合。
- `run_command` 仍使用 `shell=True`，当前沙箱不是进程级隔离。
- Windows 下 PowerShell/cmd 等执行路径的拒绝规则不完整。
- Grep 对符号链接文件没有再次进行真实路径校验。
- durable memory 内容直接注入系统提示，存在提示注入和记忆污染风险。
- sequence 产生的 trace 包含整个 agent 历史，缺少严格的 run 边界。
- eval/verify 创建的临时工作目录没有清理。
- 示例仓库已经是修复后状态，但 README 和注释仍描述为有 bug。
- 没有统一打包配置、锁定依赖、CI 和端到端集成测试。

## 3. 目标架构

```text
agentforge/
  runtime/       运行状态机、checkpoint、取消、重试
  providers/     LLM provider、重试、限流、成本统计
  tools/         工具定义和工具调用协议
  sandbox/       策略校验、容器执行、资源限制
  memory/        working / daily / durable 记忆
  evaluation/    task schema、checks、gold patch、metrics
  tracing/       事件模型、trace 存储、导出
  api/           FastAPI 服务
  cli/           命令行入口
tests/
  unit/
  integration/
  e2e/
benchmarks/
docs/
deploy/
```

核心领域对象：

```text
Task
  -> Run
      -> Attempt
          -> Step
              -> ToolCall
              -> Observation
          -> Verification
```

所有对象都应有稳定 ID、时间戳、状态和关联关系。运行状态建议至少包括：

`pending / running / verifying / succeeded / failed / cancelled`。

## 4. 分阶段实施计划

### Phase 0：修复基线正确性

目标：让当前项目的结果可信，避免在错误指标上继续扩展。

任务：

- 重写 `pass_at_k`，使用标准公式：
  `1 - C(N - c, k) / C(N, k)`。
- 评测时为每个任务保留成功次数，包含成功次数为 0 的任务。
- 校验 `num_trials >= 1` 且 `1 <= k <= num_trials`。
- command check 要求退出码为 0，并区分 stdout/stderr 语义。
- 没有 gold 或 checks 的 `CodeTask` 直接报配置错误。
- 所有验收路径统一使用工作目录边界校验。
- 修复示例仓库，使 seed、README、注释和实际输出一致。
- 清理过期 docstring 和 README 描述。

验收标准：

- 两个任务一成一败时，`pass@1 == 0.5`。
- `N=2, c=1, k=2` 时，标准 `pass@2 == 1.0`。
- 非零退出码的命令不能通过验收。
- 新增回归测试覆盖所有上述情况。

### Phase 1：实现独立 Runtime 和可恢复运行

目标：不再把第三方 Agent 的内部 memory 当作 checkpoint 协议。

任务：

- 定义 `Run / Attempt / Step / ToolCall / Verification` 数据模型。
- 为每次运行生成稳定的 run ID 和 attempt ID。
- trace 改为事件增量写入，并支持追加恢复。
- 支持从 checkpoint 恢复未完成任务。
- sequence 和 verify 只写当前运行的步骤，历史通过关联 ID 查询。
- 增加运行取消、最大时间、最大步数和失败状态。
- 对 LLM 请求增加超时、指数退避和有限次数重试。
- 写入时使用临时文件 + 原子替换，避免 trace 损坏。

验收标准：

- 进程中断后可以从最后一个完整 checkpoint 继续。
- 重复提交同一个 run 不会重复执行已完成的 tool call。
- 每个 run 的 trace 不包含其他 run 的步骤。
- 取消运行后，子进程能够被回收。

### Phase 2：实现进程级安全沙箱

目标：将当前“字符串检查型沙箱”升级为可验证的执行隔离。

任务：

- 默认使用 Docker/Podman 临时容器执行 `run_command`。
- 容器使用非 root 用户，默认关闭网络。
- 限制 CPU、内存、进程数、磁盘空间和执行时间。
- 工作目录以临时卷挂载，结果通过 patch/artifact 导出。
- 禁止访问宿主机凭据、Unix socket、Windows 用户目录和 Docker socket。
- 对 Windows 环境明确支持或明确要求 WSL2/Docker Desktop。
- 为文件工具增加符号链接、硬链接和路径穿越检查。
- 将策略决策记录到 trace：允许、拒绝、拒绝原因和策略版本。

安全验收：

- 不能通过 `../`、符号链接或绝对路径写出工作目录。
- 不能通过 PowerShell/cmd/python 子进程访问宿主机敏感文件。
- 不能访问网络或读取 API key。
- fork bomb、无限循环和超大输出会被限制并回收。
- 所有攻击用例都进入自动化测试。

### Phase 3：重构记忆与上下文管理

目标：保留跨会话能力，同时避免记忆污染和上下文失控。

任务：

- durable memory 作为不可信数据注入，不能直接拼接为系统指令。
- 明确区分“事实记忆”和“行为指令”。
- 对记忆内容做长度、来源、更新时间和版本记录。
- 增加记忆删除、覆盖、导出和审计能力。
- 采用相关性排序，而不是只按文件顺序扫描。
- 为上下文压缩记录输入长度、输出长度、丢弃步数和压缩原因。
- 增加 token 预算与字符预算的可插拔接口。

验收标准：

- 恶意 durable 内容不能改变系统策略。
- 记忆可按项目、运行和用户隔离。
- 长任务在固定预算内稳定运行。
- 压缩前后不会产生悬空 tool response。

### Phase 4：建设可复现 Benchmark

目标：从单个 `fix-add` 示例升级为可比较的 Agent 评测集。

任务：

- 建立 20～50 个任务，覆盖 bug 修复、补测试、API 修改、重构、配置迁移和安全修复。
- 每个任务包含 seed repository、instruction、验收规则、资源限制和难度等级。
- 支持 gold patch、gold tree、测试命令和结构化 checks。
- 记录每个 episode 的 reward、步数、耗时、输入输出 token、估算成本和失败类型。
- 支持固定随机种子、固定模型配置和实验配置快照。
- 对比以下变量：上下文压缩、verify retry、不同模型、不同工具集合。

核心指标：

- pass@1、pass@3、pass@5。
- 首次成功率和最终成功率。
- 平均步数、p50/p95 延迟。
- 平均 token 和单任务成本。
- 工具错误率、沙箱拦截率、重试成功率。
- 失败类型分布：did_not_act、tool_error、test_failure、state_mismatch、timeout。

验收标准：

- 一条命令可以重建 benchmark 并生成报告。
- 报告包含配置、模型、版本和完整失败 trace。
- 所有指标有单元测试和人工抽样核验。

### Phase 5：服务化与可观测性

目标：让项目具备可演示、可集成的工程形态。

任务：

- 增加 FastAPI 接口：
  - `POST /runs`
  - `GET /runs/{id}`
  - `GET /runs/{id}/trace`
  - `POST /runs/{id}/cancel`
  - `POST /benchmarks`
  - `GET /benchmarks/{id}`
- 使用 SQLite 保存运行元数据和状态。
- 使用 JSONL 或 Parquet 保存事件和评测记录。
- 接入 OpenTelemetry trace 和 Prometheus metrics。
- 提供一个简单的运行详情页面，展示步骤、工具调用、耗时、验收和失败原因。
- CLI 和 API 共用同一套 Runtime，不复制业务逻辑。

验收标准：

- 可以通过 API 启动、查询、取消和恢复运行。
- 页面可以从 trace 重建一次运行。
- 服务重启后运行状态不丢失。

### Phase 6：工程化交付

目标：让项目达到可公开展示和可长期维护的状态。

任务：

- 增加 `pyproject.toml` 和版本化发布配置。
- 锁定直接依赖版本，记录 Python 和 Docker 版本。
- 接入 `ruff`、`mypy`、`pytest-cov` 和 pre-commit。
- 建立 GitHub Actions：lint、类型检查、单测、集成测试和安全测试。
- 增加 Dockerfile 和一键启动脚本。
- 编写架构文档、威胁模型、运行手册和 benchmark 报告。
- 添加 changelog、license 和贡献指南。
- 删除或隔离本地运行产生的 `.agentforge` 和 `runs` 状态。

## 5. 测试策略

### 单元测试

- 路径边界和符号链接。
- sandbox policy 和环境变量清理。
- context compression 边界。
- trace 序列化和恢复。
- memory CRUD、隔离和提示注入防护。
- evaluator、checks、gold tree 和 pass@k。

### 集成测试

- fake LLM 驱动完整 runtime。
- LLM 超时、重试、限流和失败恢复。
- tool call -> observation -> verification 闭环。
- 容器沙箱的文件、网络和资源限制。
- SQLite 状态和服务重启恢复。

### 端到端测试

- 从 CLI/API 提交任务，到最终 patch 和 trace 生成。
- 多任务 sequence。
- verify 多轮重试。
- benchmark 批量运行和报告生成。

## 6. 里程碑与交付物

| 里程碑 | 交付物 | 判断标准 |
| --- | --- | --- |
| M1 | 正确评测基线 | pass@k、checks、示例仓库全部修正并有回归测试 |
| M2 | 可恢复 Runtime | 中断后可恢复，trace 按 run 隔离 |
| M3 | 安全执行环境 | 容器、资源限制、无网络、攻击测试通过 |
| M4 | Benchmark v1 | 20+ 任务和可复现实验报告 |
| M5 | API 与观测 | API、SQLite、trace 页面和指标可用 |
| M6 | Release v1.0 | CI、Docker、文档、版本发布和演示脚本齐全 |

## 7. 简历证明材料

最终仓库必须保留可验证证据，不要只写功能名称：

- benchmark 运行报告，包含模型、版本、配置和指标。
- 沙箱攻击测试报告。
- 上下文压缩前后 token、延迟和成本对比。
- verify retry 对成功率的影响对比。
- 完整 trace 示例。
- CI badge、Docker 启动方式和 API 示例。
- 一个 3～5 分钟可重复的演示脚本。

简历描述模板：

> 设计并实现面向代码仓库的 Agent 执行与评测平台，构建可恢复运行时、工具授权、容器化沙箱、自动验收和反馈重试闭环；搭建包含 N 个任务的可复现 benchmark，统计 pass@1/pass@k、延迟、token 成本和失败类型，并通过 CI、Docker 和 OpenTelemetry 完成工程化交付。

其中的 `N`、成功率、p95 延迟和成本数据必须来自仓库中的实际实验，不能凭估计填写。

## 8. 优先级原则

实施顺序固定为：

1. 先保证评测正确。
2. 再保证运行可恢复和 trace 可审计。
3. 再做进程级安全隔离。
4. 再扩充 benchmark 和实验数据。
5. 最后做 API、页面和发布包装。

不要优先堆更多工具或复杂前端。项目的核心竞争力应是：可靠性、安全性、评测可信度和可复现实验。
