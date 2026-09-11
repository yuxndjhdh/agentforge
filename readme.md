# AgentForge

[![CI](https://github.com/yuxndjhdh/agentforge/actions/workflows/ci.yml/badge.svg)](https://github.com/yuxndjhdh/agentforge/actions/workflows/ci.yml)

AgentForge 是面向代码仓库的 Agent 执行、验收、追踪和 benchmark 平台。它把任务输入、工具权限、运行状态、自动验收、失败反馈、trace 和成本指标放在同一条可恢复链路里。

要求 Python 3.11+（CI 覆盖 3.11 / 3.12 / 3.13）。

下文命令示例按 bash 写；PowerShell 下把行尾续行符 `\` 换成反引号，`cp` 换成 `copy`。

## 安装

```bash
python -m venv .venv

# Linux / macOS
source .venv/bin/activate
cp .env.example .env

# Windows (PowerShell)
# .venv\Scripts\Activate.ps1
# copy .env.example .env

python -m pip install -e .
```

`pip install -e .` 把 `agentforge` 以可编辑模式装进环境。只装 `requirements.txt` 的依赖而不装包，`import agentforge` 会直接失败。

可选依赖：

```bash
python -m pip install -e ".[api]"            # FastAPI 服务
python -m pip install -e ".[observability]"  # Prometheus 指标与 OTLP 导出
python -m pip install -e ".[tokenizer]"      # tiktoken 精确 token 计数
```

依赖分三层：`requirements.in` 是直接依赖，`requirements.lock` 是锁定版本，`requirements.txt` 只是 `-r requirements.lock`；包元数据和 extras 在 `pyproject.toml`。

## 快速开始

无模型自检（不需要 API key，3～5 分钟可重复）：

```bash
python -m agentforge selftest
python scripts/demo_selftest.py
```

`scripts/demo_selftest.py` 输出最终变更、验收结果、步骤数、token、耗时和 trace 路径。

需要模型时，在 `.env` 中填 `HARNESS_LLM_KEY`：

```bash
python -m agentforge run examples/demo_repo "检查并修复 calc.py 的 add 函数"
python -m agentforge verify fix-add --attempts 3
```

## CLI

```text
run        在仓库上跑一个任务
sequence   同一 agent 依次处理多个任务（演示断点续跑）
trace      渲染一次运行的轨迹
verify     闭环自检：任务 + 验真 + 未过重试
eval       批量评测任务，输出 pass@1 / pass@k
benchmark  运行可复现 benchmark 并生成报告
serve      启动 FastAPI 服务
memory     管理项目记忆
sandbox    检查容器沙箱运行时
selftest   不用 LLM 的自检（工具链 + trace 落盘）
```

## 评测与 benchmark

`eval` 和 `benchmark` 使用 23 个固定 seed 任务，报告写入 `runs/benchmarks/latest/report.json`，包含：

- **运行元数据**：模型、版本、实验配置、任务顺序、seed、任务规格
- **指标**：pass@1 / pass@3 / pass@5 / pass@k、p50 / p95 延迟、步数、token
- **证据**：完整 episode、失败 trace、失败类型
- **成本**：没有可验证的供应商费率时记为 `unavailable`，不用估算值冒充真实账单

任务审核见 `docs/benchmark_task_audit.md`，结果见 `docs/benchmark_report.md`。

消融实验用显式开关区分 A/B/C/D，Verify retry 的额外尝试、token、成本和 trace 保留在同一个 episode 内：

| 组 | 上下文压缩 | Verify retry |
| --- | --- | --- |
| A | 关（`--no-context-compression`） | 关（`--no-verify-retry`） |
| B | 开（`--context-compression`） | 关 |
| C | 关 | 开（`--verify-retry --verify-attempts 3`） |
| D | 开 | 开 |

```bash
python -m agentforge benchmark --trials 5 --k 5 \
  --context-compression --verify-retry --verify-attempts 3 \
  --out runs/benchmarks/v0.3.0-D-20260911

python scripts/summarize_benchmark.py \
  runs/benchmarks/v0.3.0-A-20260911/report.json \
  runs/benchmarks/v0.3.0-B-20260911/report.json \
  runs/benchmarks/v0.3.0-C-20260911/report.json \
  runs/benchmarks/v0.3.0-D-20260911/report.json \
  --out docs/benchmark_report.md
```

## API

```bash
python -m pip install -e ".[api]"
python -m agentforge serve --port 8000
# no-model workbench/API acceptance smoke
python scripts/workbench_smoke.py
```

```text
GET  /config                  POST /runs
GET  /runs/{id}               GET  /runs/{id}/summary
GET  /runs/{id}/trace         GET  /runs/{id}/export
POST /runs/{id}/cancel        POST /runs/{id}/resume
POST /benchmarks              GET  /benchmarks/{id}
GET  /metrics
```

`GET /` 是一个可提交任务、选择模型和 sandbox backend、配置独立验收、轮询状态、审查 Diff/Attempt/Verification/Tool Call/Trace 并执行 cancel/resume 的运行工作台。`POST /runs` 支持 `model`、`sandbox_backend`、`max_steps`、`max_duration`、`verify_command` 和 `verify_attempts`；验收命令通过选定 sandbox 执行，失败输出会回喂给同一 Agent，且所有 checks 保存在 run trace 中。`GET /runs/{id}/export` 返回可下载的完整 JSON 摘要。CLI 与 API 调用同一个 `AgentRuntime`。`AGENTFORGE_STATE_DB` 保存 run 和 benchmark job 的状态、配置、任务列表、报告路径及错误，`AGENTFORGE_TRACE_DIR` 保存事件、trace 和 benchmark 报告。`/metrics` 输出 Prometheus counters 和延迟 histograms，OTLP exporter 通过 `OTEL_EXPORTER_OTLP_ENDPOINT` 可选启用；未安装观测依赖或 exporter 不可用时仍可本地运行。

Impact 汇总由原始 benchmark 报告自动生成，不手工录入数字：

```bash
python scripts/summarize_impact.py \
  runs/benchmarks/v0.3.0-A-20260911/report.json \
  runs/benchmarks/v0.3.0-B-20260911/report.json \
  runs/benchmarks/v0.3.0-C-20260911/report.json \
  runs/benchmarks/v0.3.0-D-20260911/report.json \
  --out docs/impact_summary.json
```

`docs/impact_summary.json` 保存原始报告 SHA-256、实验配置、分母、公式和限制。当前数据是单模型固定任务集的描述性 benchmark，不是用户 Impact 证据；没有供应商费率时成本为 `unavailable`。

## Runtime 与恢复

每个 run 有稳定 ID，事件追加到 `events.jsonl`，元数据存入 SQLite，trace 只导出当前 run 的关联步骤。服务重启时把遗留的 run 和 benchmark 标记为可诊断的失败状态；失败或取消的 run 可以通过 API 新建 attempt 继续执行；重复提交已成功的 run ID 返回已保存结果，不重新执行。

新的 runtime 不依赖 smolagents 的 memory 作为 checkpoint 协议。

## 评测语义

`command` check 必须退出码为 0；`stdout_contains` 只匹配 stdout，`stderr_contains` 只匹配 stderr。验收命令使用 argv 和 `shell=False`，并共享超时、进程组和输出上限。

标准组合估计器：

```text
pass@k = 1 - C(N - c, k) / C(N, k)
```

每个任务的成功次数 `c` 都保留，成功次数为 0 的任务也进入平均分母；要求 `N >= 1` 且 `1 <= k <= N`。

## 主要模块

```text
agentforge/
  runtime/              Run/Attempt/Step/ToolCall/Verification、checkpoint、SQLite 与 JSONL
  api/                  FastAPI 服务和运行详情页面
  providers/            模型 provider 适配
  evaluation/           评测执行
  tracing/              trace 采集
  harness.py            agent 主循环
  context.py            上下文预算与压缩
  memory.py             durable/daily/run-local 记忆和审计
  sandbox.py            策略、进程组、超时、输出限制
  container_sandbox.py  Docker/Podman 适配
  tools.py              文件、grep、命令、记忆工具
  eval.py               gold tree、checks、标准 pass@k
  benchmark.py          可复现实验和报告
  tokenizer.py          provider tokenizer 与字符预算 fallback
  llm.py                模型调用和重试
  observability.py      Prometheus 指标与 OTLP
  config.py             配置加载
```

以上只列主要模块，不含全部文件。

## 安全边界

- 文件工具拒绝绝对路径、路径穿越、符号链接和写入硬链接；写入使用同目录临时文件加原子替换。
- 本地命令不用 shell，使用独立进程组、超时和输出上限；Windows 超时通过 `taskkill /T` 回收子进程树。
- 容器默认非 root、无网络、只读根文件系统、仅挂载工作目录、丢弃 capabilities，并限制 CPU、内存和 PID。
- 子进程环境会清理 API key、token、secret、password、Docker socket/context、Kubeconfig 等变量。命令策略决策会记录策略版本、允许/拒绝结果和原因。
- durable memory 是不可信数据，使用 JSON 编码和边界标记注入；`instruction` 类型不会自动注入系统提示。记忆支持 scope、覆盖、删除、导出和审计。
- `project_id`、`user_id` 和 `run_id` 都参与记忆隔离；`run-local` 使用独立目录，不能被其他 run 查询或覆盖。上下文压缩优先使用 `tiktoken`，缺失时明确标记 `chars-div-4` 估算。

### 已知限制

- **磁盘配额未验证，且会 fail-closed。** `HARNESS_SANDBOX_DISK_MB > 0`（默认 `1024`）时，适配器要求存储驱动能证明 `--storage-opt=size` 生效，而当前检测不会返回 `supported`，因此容器后端会**拒绝执行**并报 `disk quota cannot be verified`。要使用容器后端必须设 `HARNESS_SANDBOX_DISK_MB=0`，此时容器磁盘写入不受限额约束。另外注意 `--storage-opt=size` 约束的是容器可写层，而 agent 写入的 `/workspace` 是宿主 bind mount，不在该配额覆盖范围内。详见 `docs/security_report.md`。
- **本地 fallback 不是内核级隔离。** `HARNESS_SANDBOX_BACKEND=auto` 会在容器不可用时回退到本地进程（保留可观测标记），不要用于不可信仓库；生产环境应显式设置为 `docker` 或 `podman`，容器不可用时失败关闭。
- **镜像必须按 sha256 digest 固定**，未固定的镜像会被容器执行器拒绝。

威胁模型见 `docs/threat_model.md`，运行手册见 `docs/runbook.md`。

## 开发与验证

```bash
python -m pytest tests/ -q
python -m compileall -q agentforge
ruff check agentforge tests scripts
mypy agentforge
```

CI 在 push 和 PR 上执行 lint、类型检查、覆盖率（门槛 75%）、单元测试、API E2E、打包和容器沙箱集成测试，矩阵覆盖 Python 3.11 / 3.12 / 3.13。

本机与 live 环境的验证结论不写在本文件里，以免过期：容器安全见 `docs/security_report.md`，记忆与上下文见 `docs/memory_context_report.md`，benchmark 见 `docs/benchmark_report.md`。

## 文档

| 文档 | 内容 |
| --- | --- |
| `docs/architecture.md` | 架构与模块职责 |
| `docs/runbook.md` | 本地检查、跑模型、API、恢复与清理 |
| `docs/threat_model.md` | 威胁模型 |
| `docs/security_report.md` | 容器安全验证结论与已知缺口 |
| `docs/memory_context_report.md` | 记忆与上下文证据 |
| `docs/benchmark_report.md` | benchmark 结果 |
| `docs/benchmark_task_audit.md` | 23 个任务的人工审核 |

贡献流程见 `contributing.md`，版本记录见 `changelog.md`，许可见 `LICENSE`。
