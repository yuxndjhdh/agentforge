# AgentForge

AgentForge 是面向代码仓库的 Agent 执行、验收、追踪和 benchmark 平台。它把任务输入、工具权限、运行状态、自动验收、失败反馈、trace 和成本指标放在同一条可恢复链路里。

## 快速开始

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
copy .env.example .env
```

无模型自检：

```bash
.venv/Scripts/python -m agentforge selftest
.venv/Scripts/python scripts/demo_selftest.py
```

`scripts/demo_selftest.py` 是一个 3～5 分钟内可重复的无模型演示，输出最终变更、验收结果、步骤数、token、耗时和 trace 路径。

需要 LLM 时，在 `.env` 中填写 `HARNESS_LLM_KEY`，然后运行：

```bash
.venv/Scripts/python -m agentforge run examples/demo_repo "检查并修复 calc.py 的 add 函数"
.venv/Scripts/python -m agentforge verify fix-add --attempts 3
```

评测与报告：

```bash
.venv/Scripts/python -m agentforge eval --trials 2 --k 1
.venv/Scripts/python -m agentforge benchmark --trials 1 --k 1 --seed 0
.venv/Scripts/python -m agentforge sandbox diagnose
```

`benchmark` 使用 23 个固定 seed 任务，报告写入 `runs/benchmarks/latest/report.json`，其中包含模型、版本、实验配置、任务顺序、seed、任务规格、完整 episode、失败 trace、pass@1/pass@3/pass@5/pass@k、p50/p95 延迟、步数、token、估算成本和失败类型。没有配置单价时成本记录为 0，不会用估算值冒充真实账单。任务审核见 `docs/BENCHMARK_TASK_AUDIT.md`。

## 结构

```text
agentforge/
  runtime/       Run/Attempt/Step/ToolCall/Verification、checkpoint、SQLite、JSONL
  api/           FastAPI 服务和运行详情页面
  benchmark.py   可复现实验和报告
  eval.py        gold tree、checks、标准 pass@k
  tools.py       文件、grep、命令、记忆工具
  sandbox.py     策略、进程组、超时、输出限制
  container_sandbox.py  Docker/Podman 适配
  memory.py      durable/daily/run-local 数据和审计
  tokenizer.py   provider tokenizer 与字符预算 fallback
  trace.py       原子 trace 导出与渲染
```

新的 runtime 不依赖 smolagents 的 memory 作为 checkpoint 协议。每个 run 有稳定 ID，事件追加到 `events.jsonl`，元数据存入 SQLite，trace 只导出当前 run 的关联步骤。服务重启时会把遗留的运行和 benchmark 标记为可诊断的失败状态；失败或取消的 run 可以通过 API 创建新的 attempt 继续执行，重复提交已成功的 run ID 会返回已保存结果，不重新执行。

## API

安装 API 可选依赖后启动：

```bash
.venv/Scripts/python -m pip install -e ".[api]"
.venv/Scripts/python -m agentforge serve --port 8000
```

接口包括：

```text
POST /runs
GET  /runs/{id}
GET  /runs/{id}/trace
POST /runs/{id}/cancel
POST /runs/{id}/resume
POST /benchmarks
GET  /benchmarks/{id}
GET  /metrics
```

`AGENTFORGE_STATE_DB` 保存 run 和 benchmark job 的状态、配置、任务列表、报告路径及错误；`AGENTFORGE_TRACE_DIR` 保存事件、trace 和 benchmark 报告。`GET /` 是一个可提交任务、轮询状态和查看 trace 的简易运行页面。CLI 与 API 都调用同一个 `AgentRuntime`。`/metrics` 输出 Prometheus counters 和延迟 histograms，OTLP exporter 通过 `OTEL_EXPORTER_OTLP_ENDPOINT` 可选启用，未安装观测依赖或 exporter 不可用时仍可本地运行。

## 评测语义

`command` check 必须满足退出码为 0；`stdout_contains` 只匹配 stdout，`stderr_contains` 只匹配 stderr。验收命令使用 argv 和 `shell=False`，并共享超时、进程组和输出上限。

标准组合估计器为：

```text
pass@k = 1 - C(N - c, k) / C(N, k)
```

其中每个任务的成功次数 `c` 都会保留，成功次数为 0 的任务也进入平均分母；要求 `N >= 1` 且 `1 <= k <= N`。

## 安全边界

- 文件工具拒绝绝对路径、路径穿越、符号链接和写入硬链接；写入使用同目录临时文件加原子替换。
- 本地命令不用 shell，使用独立进程组、超时和输出上限；Windows 超时通过 `taskkill /T` 回收子进程树。
- `HARNESS_SANDBOX_BACKEND=auto` 在 Docker/Podman 可用时使用容器，否则回退到本地进程并保留可观测标记。生产环境应使用 `docker` 或 `podman` 显式模式，容器不可用时失败关闭。
- 容器默认非 root、无网络、只读根文件系统、仅挂载工作目录、丢弃 capabilities，并限制 CPU、内存、PID、临时目录和磁盘配额。
- 子进程环境会清理 API key、token、secret、password、Docker socket/context、Kubeconfig 等变量。命令策略决策会记录策略版本、允许/拒绝结果和原因。
- durable memory 是不可信数据，使用 JSON 编码和边界标记注入；`instruction` 类型不会自动注入系统提示。记忆支持 scope、覆盖、删除、导出和审计。
- `project_id`、`user_id` 和 `run_id` 都参与记忆隔离；`run-local` 使用独立目录，不能被其他 run 查询或覆盖。上下文压缩优先使用 `tiktoken`，缺失时明确标记 `chars-div-4` 估算。

本地 fallback 不是内核级隔离；不可信代码的生产执行必须配置可用的 Docker/Podman 或外部沙箱。威胁模型和运行手册见 `docs/THREAT_MODEL.md` 与 `docs/RUNBOOK.md`。

## 开发验证

```bash
.venv/Scripts/python -m pytest tests/ -q
.venv/Scripts/python -m compileall -q agentforge
```

CI 执行 lint、类型检查、覆盖率、单元测试和安全回归。直接依赖版本记录在 `requirements.lock`；发布元数据在 `pyproject.toml`。

当前工作区已实际验证：99 个测试通过、4 个平台/环境限制用例跳过；`ruff check agentforge tests`、`mypy agentforge`、`compileall` 和 FastAPI E2E smoke test 均通过。Docker/Podman live 安全验收需在 Linux CI 执行，结果见 `docs/SECURITY_REPORT.md`；memory/context 证据见 `docs/MEMORY_CONTEXT_REPORT.md`。
