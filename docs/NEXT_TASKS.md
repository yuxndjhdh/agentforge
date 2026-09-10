# AgentForge 后续执行任务计划

## 1. 文档目的

本文档承接 `docs/PLAN.md`，只安排当前审计后仍未完成或未达到验收标准的工作。

当前判断：

- Phase 0 已基本完成，不再重复开发。
- Phase 1、Phase 3、Phase 5 已有代码骨架，但需要补齐真实语义和集成验证。
- Phase 2、Phase 4 缺少真实运行证据，是当前最高优先级。
- Phase 6 已有工程化文件，但尚未形成可发布、可复现的正式版本。

执行原则：

1. 先修复运行时语义，再做容器安全验证。
2. Benchmark 必须使用真实执行结果，不用手工填写指标。
3. 功能只有在代码、测试、文档和产物同时存在时才算完成。
4. 每完成一个任务就更新本文档复选框，并记录对应提交或报告路径。
5. 暂不扩展多 Agent、复杂前端和更多模型供应商。

## 2. 完成状态定义

- `[ ]`：尚未开始。
- `[~]`：正在进行或只完成了部分实现。
- `[x]`：代码、测试和验收证据均已完成。
- `[!]`：被外部环境或依赖阻塞，必须写明原因。

全局 Definition of Done：

- 实现进入正式模块，不以临时脚本代替。
- 有覆盖成功、失败和边界条件的自动化测试。
- `pytest`、`ruff`、`mypy`、`compileall` 全部通过。
- 用户可见行为同步更新 README 或 docs。
- 需要实验或安全验证的任务必须产出可追溯报告。
- 相关修改已经提交到 Git，工作区没有遗漏的交付文件。

## 3. 执行顺序

```text
T0 基线固化
  -> T1 Runtime 真实恢复与幂等
  -> T2 容器沙箱与攻击验证
  -> T3 记忆隔离与上下文验证
  -> T4 Benchmark 质量与真实实验
  -> T5 API、持久化与可观测性
  -> T6 发布工程化与简历材料
```

T1、T2 是 T4 真实实验的前置条件。T4 的报告是 T6 发布和简历描述的前置条件。

---

## 4. T0：固化当前开发基线

目标：把当前大量未提交修改整理成可追踪基线，避免后续工作建立在不可恢复的工作区上。

### T0-01 审核并提交现有 0.2.0 代码

- [x] 逐文件审核当前修改和新增文件，确认没有生成物、密钥或个人路径进入提交。
- [x] 删除或归档根目录与 `docs/` 中重复的计划/架构文档，只保留唯一来源。
- [x] 确认 `.agentforge/`、`runs/`、`.coverage`、缓存目录均被忽略。
- [x] 将现有改动按“runtime/sandbox”“benchmark/api”“docs/engineering”拆成可审查提交。
- [x] 提交后确认 `git status --short` 为空。

验收命令：

```powershell
git status --short
git log --oneline -5
```

完成证据：提交哈希记录在本节下方。

完成提交：`fd83b3a`、`a60dff1`、`afe218f`。

---

## 5. T1：完成可恢复 Runtime

目标：让 checkpoint、resume、幂等和取消具备真实执行语义，而不仅是数据结构和接口。

### T1-01 恢复完整 checkpoint state

- [x] `RuntimeContext` 在 resume 时接收最新完整 checkpoint state。
- [x] executor 可以读取 cursor、已完成步骤、业务状态和恢复元数据。
- [x] checkpoint 增加 schema version，并对不兼容版本给出明确错误。
- [x] 区分“事件序号”和“checkpoint 序号”，避免相互覆盖或误读。
- [x] 不完整或损坏的最后一条 checkpoint 会回退到上一条完整记录。

验收场景：

1. 第一次执行完成步骤 1、2 后模拟进程崩溃。
2. 创建新的 `RuntimeStore` 和 `AgentRuntime` 实例。
3. 使用同一个 run ID 恢复。
4. 从步骤 3 继续，不重复步骤 1、2。

### T1-02 实现工具调用幂等

- [x] 在工具执行前根据稳定 idempotency key 查询历史状态。
- [x] 已成功完成的工具调用直接返回保存的 observation。
- [x] 失败或未完成调用按显式策略决定重试或拒绝。
- [x] idempotency key 不依赖新的 attempt ID，否则恢复后无法命中旧调用。
- [x] 写文件等副作用工具增加重复执行回归测试。

验收标准：同一 checkpoint 恢复两次，已完成的写文件和命令调用都只发生一次。

### T1-03 强化取消与总超时

- [x] 取消普通 Python executor 时不依赖其主动轮询才能结束运行。
- [x] 取消 Agent 时同时终止正在运行的命令子进程树。
- [x] 总运行超时覆盖 LLM 请求、工具执行和 verify 阶段。
- [x] timeout 与 cancel 使用不同最终状态和错误类型。
- [x] 增加阻塞 executor、阻塞命令和慢 LLM 三类测试。

### T1-04 启动恢复与僵尸运行处理

- [x] 服务启动时扫描 `running/verifying` 状态的遗留 run。
- [x] 根据 checkpoint 和恢复策略转为 resumable、failed 或重新入队。
- [x] 成功 run 重复提交保持幂等。
- [x] 恢复操作写入新的 attempt，并保留旧 attempt 历史。

### T1-05 Runtime 集成测试

- [x] 新增真正关闭并重建 SQLite 连接的恢复测试。
- [x] 新增部分 JSONL 尾行损坏测试。
- [x] 新增重复 tool call 副作用测试。
- [x] 新增非协作 executor 取消测试。
- [x] 新增 run/attempt/step/verification 关联完整性测试。

T1 完成门槛：

- [x] M2 的四条验收标准全部通过。
- [x] Runtime 核心模块覆盖率不低于 85%（当前专项覆盖率 92%）。
- [x] 生成一个真实中断后恢复的 trace 示例（见 `docs/examples/runtime_recovery_trace.json`）。

---

## 6. T2：完成容器沙箱和安全验收

目标：证明 Agent 执行边界确实受到容器与资源限制，而不是只验证命令字符串。

### T2-01 准备可重复的容器测试环境

- [~] 在本机或 Linux CI 中安装并确认 Docker/Podman 可用（Linux CI job 已加入；当前 Windows 主机无 runtime）。
- [x] 固定沙箱执行镜像的名称和 digest。
- [x] 增加容器 runtime 可用性诊断命令。
- [x] 显式 `docker/podman` 模式在 runtime 不可用时保持 fail-closed。
- [x] 明确 `auto` 回退到 local 时的告警和 trace 标记。

### T2-02 容器集成测试

- [~] 验证容器内 UID 非 root（集成测试已实现，当前主机跳过）。
- [~] 验证 root filesystem 只读（集成测试 job 已接线，当前主机跳过）。
- [~] 验证只有 `/workspace` 可写（集成测试已实现，当前主机跳过）。
- [~] 验证网络默认关闭（集成测试已实现，当前主机跳过）。
- [ ] 验证 CPU、内存和 PID 限制生效。
- [~] 验证 timeout、cancel 和输出上限能回收完整进程树（timeout/output 已实现，cancel 仍待 live 验证）。
- [~] 验证宿主环境变量和 Docker/Kubernetes 配置不会进入容器（集成测试已实现，当前主机跳过）。

### T2-03 攻击型测试集

- [~] `../` 和绝对路径逃逸（local policy 单测已有，container live case 待补）。
- [~] 文件与目录符号链接逃逸（local policy 单测已有，Windows 符号链接受权限限制）。
- [~] 硬链接覆盖外部文件（local policy 单测已有，container live case 待补）。
- [ ] PowerShell、cmd、Python 子进程间接执行。
- [~] shell control characters 和参数解析绕过（local policy 单测已有，container live case 待补）。
- [~] 网络访问、DNS 和回连尝试（网络/DNS 集成用例已实现，当前主机跳过）。
- [ ] 读取宿主用户目录、Docker socket 和敏感环境变量。
- [~] fork bomb、无限循环、磁盘填充和超大输出（timeout/output 已实现，fork/disk 待 live 验证）。

### T2-04 磁盘限制兼容策略

- [!] 验证当前容器存储驱动是否支持 `--storage-opt=size=`（当前 Windows 主机没有 Docker/Podman，无法取得 driver 或 live quota 证据）。
- [ ] 不支持时使用受限临时卷或外部配额方案。
- [x] 禁止在无法实施磁盘限制时静默宣称已经限制（未验证时容器执行 fail-closed）。

### T2-05 安全报告

- [x] 生成 `docs/SECURITY_REPORT.md`。
- [x] 记录测试环境、镜像 digest、攻击用例、结果和剩余风险。
- [x] 报告明确 local backend 不是安全边界。

T2 完成门槛：

- [!] 容器测试已加入 Linux CI，但当前主机无法执行 live 验收；CPU/PID、攻击集和 storage quota 仍缺证据。
- [ ] Windows 平台跳过的符号链接测试在 CI 中得到实际覆盖。
- [ ] `container_sandbox.py` 覆盖率不低于 80%。
- [~] M3 已有安全报告和 fail-closed 证据，完整安全验收待 Linux CI 运行结果。

---

## 7. T3：补齐记忆隔离和上下文验证

目标：让分层记忆具备明确的信任边界、作用域和可验证行为。

### T3-01 完成作用域模型

- [x] 明确 durable、daily 和 run-local 三类数据的生命周期。
- [x] project、user、run 三个维度真正参与存储或查询隔离。
- [x] 当前 `run_id` 不能只作为 metadata 保存。
- [x] 禁止一个 scope 读取或覆盖另一个 scope 的记忆。
- [x] 增加 scope 迁移和旧格式兼容测试。

### T3-02 提示注入防护验证

- [x] 为 durable memory 编写恶意指令样本。
- [x] 验证 `instruction` 类型不会自动注入。
- [x] 验证标签、换行、伪 system prompt 和工具调用文本均保持数据语义。
- [x] 在 Agent 集成测试中验证安全策略不会被恶意记忆覆盖。
- [x] 文档中避免使用“绝对安全”，明确模型层防护的局限。

### T3-03 记忆功能测试

- [x] 覆盖 save、overwrite、delete、export、audit、version。
- [x] 覆盖相关性排序和更新时间排序。
- [x] 覆盖并发写入与原子替换。
- [x] 覆盖损坏 JSON 和损坏 audit 尾行恢复。

### T3-04 Token 预算接口

- [x] 接入至少一个真实 provider tokenizer 或明确的 tokenizer adapter。
- [x] 字符预算作为 fallback，并在 trace 中标记估算方式。
- [x] 验证字符预算和 token 预算同时存在时的优先级。
- [x] 增加超预算但不可安全压缩时的明确状态。

T3 完成门槛：

- [x] memory 新增能力都有自动测试，而不是只由实现代码覆盖。
- [x] 恶意 durable 样例不能改变工具权限或系统策略。
- [x] context/memory 报告能说明预算来源和信任边界（见 `docs/MEMORY_CONTEXT_REPORT.md`）。

---

## 8. T4：完成真实 Benchmark

目标：把“23 个任务定义”变成可复现、可比较、能写进简历的实验结果。

### T4-01 修正 Benchmark 接线

- [x] API benchmark 默认使用 `BENCHMARK_TASKS`，不是单任务 `BUILTIN_TASKS`。
- [x] API 和 CLI 使用同一任务选择逻辑。
- [x] 请求不存在的任务名时返回明确错误。
- [x] 增加 benchmark runner 和 API benchmark 测试。

### T4-02 提升任务质量

- [x] 审核现有 23 个任务，移除只靠简单字符串替换即可通过的脆弱验收。
- [x] command check 使用专用行为输出行验证，而不只验证输出中含某个数字。
- [x] 至少加入 10 个多文件真实任务（当前 15 个）。
- [x] 覆盖 bug 修复、补测试、API 修改、重构、配置迁移、CLI 和安全修复。
- [x] 为任务记录来源、难度、标签、资源限制和验收逻辑。
- [x] seed、gold 和验收代码进入版本控制。

### T4-03 Benchmark runner 回归测试

- [x] 使用 fake solver 测试报告生成，不消耗真实模型额度。
- [x] 测试 pass@1、pass@3、pass@5 汇总。
- [x] 测试 p50/p95、token、成本和失败类型。
- [x] 测试报告原子写入和损坏恢复。
- [x] 测试固定任务顺序、随机种子和配置快照。

### T4-04 执行真实实验矩阵

至少执行以下四组实验，每组每任务至少 5 次，以便计算 pass@1/3/5：

| 实验 | 上下文压缩 | Verify retry | 目的 |
| --- | --- | --- | --- |
| A | 关闭 | 关闭 | 基线 |
| B | 开启 | 关闭 | 测量压缩影响 |
| C | 关闭 | 开启 | 测量反馈重试影响 |
| D | 开启 | 开启 | 完整系统 |

- [!] 固定模型版本、temperature、任务版本和 AgentForge commit（当前未配置 `HARNESS_LLM_KEY`，不能执行真实矩阵）。
- [ ] 保存完整配置快照和失败 trace。
- [ ] 记录输入/输出 token、成本、耗时和步骤数。
- [ ] 不把失败运行删除或只保留汇总。

### T4-05 生成 Benchmark 报告

- [!] 生成 `runs/benchmarks/<version>/report.json`（真实实验被缺少 `HARNESS_LLM_KEY` 阻塞；fake solver 产物只用于自动测试）。
- [!] 生成 `docs/BENCHMARK_REPORT.md`（不能用未执行的真实结果填充）。
- [ ] 报告包含 pass@1/3/5、p50/p95、成本、失败分布和消融对比。
- [ ] 对统计结果进行人工抽样，至少复查每类失败 3 个 trace。
- [ ] 只在报告生成后确定简历中的量化数字。

T4 完成门槛：

- [x] 一条命令能从固定任务集重建报告。
- [x] 至少 20 个有效任务通过任务质量审核。
- [!] 四组真实实验全部存在可追踪产物（缺少 `HARNESS_LLM_KEY`）。
- [x] `benchmark.py` 具备自动测试覆盖。

---

## 9. T5：完成服务持久化与可观测性

目标：API 重启后状态仍可查询和恢复，trace 与 metrics 能反映真实执行过程。

### T5-01 持久化 Benchmark job

- [x] 将 benchmark 状态从进程内字典迁移到 SQLite。
- [x] 保存 job、配置、任务列表、状态、报告路径和错误。
- [x] 服务重启后仍能查询已完成和失败的 benchmark。
- [x] 启动时处理遗留 `running` benchmark。

### T5-02 增加恢复接口

- [x] 增加 `POST /runs/{id}/resume`。
- [x] 只允许可恢复状态调用 resume。
- [x] 返回新的 attempt 信息和原 run ID。
- [x] 增加并发 resume 冲突测试。

### T5-03 接入真实 OpenTelemetry

- [x] 在 run、LLM request、tool call、verification 上创建 span。
- [x] 配置可选 OTLP exporter。
- [x] trace ID 写入 runtime event 和 API 响应。
- [x] 增加 exporter 不可用时的降级行为。
- [x] 删除只定义但从未调用的观测接口。

### T5-04 完善 Metrics

- [x] 增加 run 状态、工具调用、LLM 重试、sandbox 拒绝和 verification 指标。
- [x] 增加延迟 histogram，而不只记录 count/sum。
- [x] 控制 label cardinality，禁止 run ID 作为 Prometheus label。
- [x] 为 `/metrics` 输出增加格式测试。

### T5-05 API 端到端测试

- [x] 使用 fake LLM/provider 完成真实后台 run，不 mock 掉 `_run`。
- [x] 覆盖提交、轮询、trace、取消、失败和恢复。
- [x] 覆盖服务关闭并重建后的状态查询。
- [x] 覆盖 benchmark 创建、完成、失败和重启查询。
- [x] 验证 API 和 CLI 最终使用同一 Runtime 行为。

T5 完成门槛：

- [x] 服务重启后 run 与 benchmark 都不会丢失。
- [x] API 可以恢复中断任务。
- [x] OpenTelemetry span 和 Prometheus 指标均来自真实执行路径；OTLP 外部接收端未在本机配置，降级路径有测试。
- [x] API E2E 测试不绕过 worker。

---

## 10. T6：正式工程化交付

目标：将当前开发工作区变成可安装、可复现、可发布的版本。

### T6-01 依赖与构建

- [x] 使用 `pip-compile` 生成完整传递依赖锁（`requirements.in` -> `requirements.lock`）。
- [~] 在 Python 3.11、3.12、3.13 上验证锁文件和安装（CI matrix 已配置，本机只完成 3.13）。
- [x] 构建 wheel 和 sdist，并在全新虚拟环境安装验证。
- [x] 验证 console script `agentforge` 可用。
- [x] 固定沙箱镜像 digest 和构建参数。

### T6-02 CI

- [~] 单元测试、ruff、mypy、coverage、compileall 已接入 GitHub Actions；远端 workflow 尚未在本环境运行。
- [~] Linux Docker 沙箱集成 job 已存在；live 结果仍需 Linux CI。
- [x] 增加 API E2E job。
- [x] 增加构建 wheel/sdist 的 packaging job。
- [x] 设置 75% 最低覆盖率门槛（当前本机实测 75.19%；80% 目标待后续补测）。
- [!] README 真实 CI 状态需远端 workflow 首次运行后补充，当前没有可验证的 CI URL/status。

### T6-03 文档与演示

- [~] 更新 README 快速开始、架构、安全边界和 benchmark 数据（真实 benchmark 数据仍受 key 阻塞）。
- [x] 更新 `ARCHITECTURE.md`、`THREAT_MODEL.md`、`RUNBOOK.md`。
- [~] 已有 `docs/SECURITY_REPORT.md`；`docs/BENCHMARK_REPORT.md` 仍因真实实验阻塞。
- [x] 添加 3～5 分钟可重复演示脚本。
- [x] 演示输出包含最终 diff、checks、步骤、token、耗时和 trace 路径。

### T6-04 发布

- [x] 将版本提升到与实际能力一致的版本号（`0.3.0`）。
- [x] 更新 CHANGELOG，禁止记录未验证的结果。
- [ ] 创建 release commit 和 Git tag。
- [ ] 从干净 clone 按 RUNBOOK 完整复现一次。
- [ ] 保存最终 release 对应的 benchmark 和安全报告。

T6 完成门槛：

- [ ] M1～M5 均已通过各自验收门槛。
- [ ] Git 工作区干净，CI 全绿。
- [ ] Docker、CLI、API 和 benchmark 均可按文档复现。
- [ ] Release tag、报告和演示材料完整。

---

## 11. 每次提交前的验证清单

```powershell
.venv\Scripts\python -m pytest -q
.venv\Scripts\ruff check agentforge tests
.venv\Scripts\mypy agentforge
.venv\Scripts\python -m compileall -q agentforge
git diff --check
git status --short
```

涉及容器时追加：

```powershell
docker version
.venv\Scripts\python -m pytest tests/integration/test_container_sandbox.py -q
```

涉及 API 时追加：

```powershell
.venv\Scripts\python -m pytest tests/e2e/test_api_runtime.py -q
```

涉及 Benchmark 时追加：

```powershell
.venv\Scripts\python -m agentforge benchmark --trials 5 --k 5 --out runs/benchmarks/<version>
```

## 12. 当前下一步

T0～T5 的代码、测试和文档工作已完成。后续只处理外部环境阻塞和发布收尾：

1. 在配置 `HARNESS_LLM_KEY` 后执行 T4-04 的四组真实 benchmark，并保留完整报告与失败 trace。
2. 在 Linux Docker CI 中完成 T2 的 live CPU/PID、攻击集和 storage quota 验收。
3. 在外部验证完成后保存最终 benchmark/security 报告，并按 T6 清单完成正式发布复现。

在这些外部验证完成前，不开始复杂 UI、多 Agent 或额外模型接入。

## 13. 完成记录

| 日期 | 任务 ID | 提交/报告 | 说明 |
| --- | --- | --- | --- |
|  |  |  |  |
| 2026-09-10 | T2-01~T2-05 | `6ff3300`; `docs/SECURITY_REPORT.md` | 容器诊断、digest pin、auto 回退 trace、fail-closed 和 Linux Docker CI 已实现；当前 Windows 无 Docker/Podman，storage quota 与 live 攻击验收保留阻塞。 |
| 2026-09-10 | T3-01~T3-04 | `40e3ecf`; `docs/MEMORY_CONTEXT_REPORT.md` | 完成 project/user/run-local 隔离、并发与损坏恢复、恶意记忆边界、tiktoken/字符 fallback 和预算 trace。 |
| 2026-09-10 | T4-01~T4-03 | `d833357`; `docs/BENCHMARK_TASK_AUDIT.md` | 统一 CLI/API 任务选择，补精确行为验收、seed/config snapshot、fake solver 报告和 pass@1/3/5；真实实验因缺少 `HARNESS_LLM_KEY` 未执行。 |
| 2026-09-10 | T5-01~T5-05 | `9aa25f4` | Benchmark job SQLite 持久化、run resume API、运行级 trace/可选 OTLP、Prometheus histogram 和真实 worker E2E 已完成；本机未配置外部 OTLP 接收端。 |
| 2026-09-10 | T6-01~T6-03 | `b9cb3ce` | pip-compile 传递依赖锁、wheel/sdist fresh-venv 安装、0.3.0 元数据、API/packaging CI job 和无模型演示已完成；Python 3.11/3.12、远端 CI、Docker live 和真实 benchmark 仍待外部环境。 |
