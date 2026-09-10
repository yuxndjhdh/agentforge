# AgentForge 剩余工作执行清单

## 1. 当前结论

核心代码、Runtime、API、记忆、评测框架和工程化骨架已经完成，当前版本为 `v0.3.0`。

本地已验证：

- `111 passed, 1 skipped`
- `ruff check agentforge tests` 通过
- `mypy agentforge` 通过
- `compileall` 通过
- Runtime、API、memory、benchmark fake solver 测试已覆盖
- Git 已有 release tag，但还没有完整 release 验收证据

剩余工作主要受两个外部条件影响：

1. 当前 Windows 主机已通过 Docker Desktop Linux engine 完成基础 live 容器验收，但 overlayfs 磁盘 quota 和更完整的攻击集仍缺证据。
2. 当前环境没有 `HARNESS_LLM_KEY`，无法执行真实模型 benchmark。

本清单只包含尚未完成的工作，不重复 `docs/next_tasks.md` 中已经完成的代码任务。

## 2. 完成状态

- `[ ]` 尚未完成。
- `[~]` 已有代码或 CI 接线，但缺少真实环境证据。
- `[x]` 已完成并有可追溯产物。
- `[!]` 当前环境阻塞，需要外部环境或凭据。

最终完成条件：所有 `[!]` 和影响发布的 `[~]` 都必须解决，且生成安全报告、Benchmark 报告和 clean-clone 验收记录。

## 3. 执行顺序

```text
R1 Linux Docker live 验收
  -> R2 真实模型 Benchmark
  -> R3 远端 CI 验证
  -> R4 clean clone 复现
  -> R5 release 收尾
```

R1 和 R2 可以并行，但 R3、R4、R5 必须在前面的结果可追溯后执行。

---

## R1：完成 Linux Docker 沙箱 live 验收

### R1-01 准备环境

- [ ] 选择一台安装 Docker Engine 的 Linux 主机或启用 GitHub Actions Docker runner。
- [ ] 确认 Docker daemon 可用：

```bash
docker version
docker info
```

- [ ] 拉取并确认 digest 固定的镜像：

```bash
docker pull python:3.13-slim@sha256:9d2e5553305c7c7b0097999bb17187c69b921ccd6bc9d40e4bb5ebe652c00285
```

- [ ] 记录 Docker 版本、内核版本、存储驱动和镜像 digest。

### R1-02 执行集成测试

- [ ] 运行容器 smoke tests：

```bash
python -m agentforge sandbox diagnose
pytest -m integration tests/integration/test_container_sandbox.py -q
```

- [ ] 验证非 root UID。
- [ ] 验证 root filesystem 只读。
- [ ] 验证只有 `/workspace` 可写。
- [ ] 验证默认网络关闭。
- [ ] 验证宿主 API key、Docker socket、Kubeconfig 等变量不会泄露。
- [ ] 验证 timeout、cancel 和输出上限能回收子进程树。

### R1-03 执行攻击用例

- [ ] 路径穿越、绝对路径、符号链接和硬链接逃逸。
- [ ] PowerShell/cmd/Python 间接执行和 shell control character 绕过。
- [ ] 访问宿主用户目录、Docker socket 和敏感环境变量。
- [ ] 网络、DNS 和回连尝试。
- [ ] 无限循环、fork bomb、磁盘填充和超大输出。
- [ ] CPU、内存、PID 和时间限制。

### R1-04 处理磁盘配额

- [ ] 记录 `docker info` 的 storage driver。
- [ ] 验证 `--storage-opt=size=` 是否真实生效。
- [ ] 若 driver 不支持，选择受限临时卷或外部磁盘配额方案。
- [ ] 未能验证配额时，保持 fail-closed，不在文档中宣称已限制。

### R1-05 生成安全报告

- [x] 更新 [security_report.md](C:/Users/YU/Desktop/agentforge/docs/security_report.md)。
- [x] 写入测试环境、命令、镜像 digest、基础集成用例结果和剩余风险。
- [ ] 记录 CI run URL 或日志归档路径。
- [ ] 将容器测试覆盖率提高到计划要求的 80% 以上，或记录明确的例外原因。

R1 完成标准：

- Docker live 集成测试通过。
- 攻击型用例有明确 pass/fail 记录。
- CPU/PID、网络和磁盘限制有运行证据。
- `security_report.md` 不再依赖“当前机器无法运行”的说明作为最终结论。

---

## R2：执行真实模型 Benchmark

### R2-01 准备模型配置

- [ ] 在安全的本地 `.env` 中配置 `HARNESS_LLM_BASE`、`HARNESS_LLM_MODEL` 和 `HARNESS_LLM_KEY`。
- [ ] 不把 key 写入 Git、报告、trace 或 CI 日志。
- [ ] 记录模型名称、模型版本/日期、temperature、最大步数和成本单价。
- [ ] 确认真实实验使用 Docker backend；不可信仓库不能使用 local fallback。

### R2-02 执行四组实验

每组至少运行 5 次，每个任务使用同一版本 seed、配置和模型。

| 实验 | 上下文压缩 | Verify retry | 产物目录 |
| --- | --- | --- | --- |
| A | 关闭 | 关闭 | `runs/benchmarks/v0.3.0-A` |
| B | 开启 | 关闭 | `runs/benchmarks/v0.3.0-B` |
| C | 关闭 | 开启 | `runs/benchmarks/v0.3.0-C` |
| D | 开启 | 开启 | `runs/benchmarks/v0.3.0-D` |

建议命令模板：

```bash
python -m agentforge benchmark --trials 5 --k 5 \
  --out runs/benchmarks/v0.3.0-A
```

四组实验必须通过配置或 CLI 开关明确关闭/开启对应能力，不能只修改目录名称。

### R2-03 检查实验完整性

- [ ] 每个任务都有 5 个 episode。
- [ ] 每个 episode 都有 reward、trace、步骤数、耗时、输入/输出 token。
- [ ] 失败 episode 保留完整 trace，不只保留汇总。
- [ ] 配置快照包含模型、版本、temperature、sandbox backend 和任务版本。
- [ ] 记录估算成本单价；单价未知时明确标记为 0 或 unavailable。
- [ ] 人工抽查每类失败至少 3 个 trace。

### R2-04 生成 Benchmark 报告

- [ ] 生成 `runs/benchmarks/<version>/report.json`。
- [ ] 生成 `docs/benchmark_report.md`。
- [ ] 报告包含：
  - pass@1、pass@3、pass@5；
  - 首次成功率和最终成功率；
  - p50/p95 延迟；
  - 平均步数、输入/输出 token 和成本；
  - tool error、timeout、state mismatch 等失败分类；
  - A/B/C/D 消融对比；
  - 模型、代码 commit 和任务版本。

R2 完成标准：

- 真实模型运行产出完整报告。
- 报告可由一条命令重建。
- 报告中的数字都能追溯到 episode 或 trace。
- 不在 README 或简历中使用报告之外的成功率、成本或延迟数字。

---

## R3：完成远端 CI 验证

- [ ] 推送当前 release 分支到远端。
- [ ] 运行 Python 3.11、3.12、3.13 matrix。
- [ ] 确认 ruff、mypy、pytest、coverage、compileall 全部通过。
- [ ] 确认 API E2E job 通过。
- [ ] 确认 packaging job 能安装 wheel 并执行 `agentforge --help`。
- [ ] 确认 Docker sandbox job 真实执行 integration tests，而不是被条件跳过。
- [ ] 记录每个 workflow 的 URL、commit SHA 和结果。
- [ ] 将 README 中的 CI 状态替换为真实 badge 或明确的 workflow 链接。

R3 完成标准：

- 所有 required checks 为绿色。
- Docker job 至少有一次真实通过记录。
- coverage 达到 workflow 的最低门槛。

---

## R4：执行 clean clone 复现

### R4-01 构建和安装

- [ ] 从 `v0.3.0` 或最终 release tag 创建全新 clone。
- [ ] 使用 Python 3.13 新建虚拟环境。
- [ ] 安装 `requirements.lock` 和项目 wheel。
- [ ] 验证 console script：

```bash
agentforge --help
```

### R4-02 运行无模型演示

```bash
python scripts/demo_selftest.py --out runs/clean-demo
```

- [ ] 输出 status 为 passed。
- [ ] 输出最终 diff、checks、steps、耗时和 trace 路径。
- [ ] trace 文件可用 `agentforge trace` 重新渲染。

### R4-03 运行服务和 API

- [ ] 启动 `agentforge serve`。
- [ ] 提交任务、轮询状态、读取 trace、取消任务。
- [ ] 重启服务后继续查询 run 和 benchmark 状态。
- [ ] 在 Docker backend 下完成至少一个真实安全任务。

### R4-04 复现 Benchmark

- [ ] 按 `docs/benchmark_report.md` 重新执行一个小规模实验。
- [ ] 比较报告 schema、配置快照和指标字段。
- [ ] 记录环境差异和允许的非确定性。

R4 完成标准：

- 新 clone 不依赖原工作区缓存、`.agentforge` 或旧 `runs` 数据。
- RUNBOOK 中的关键流程全部可执行。
- 复现结果与报告格式一致。

---

## R5：完成正式发布收尾

- [ ] 根据 R1～R4 结果更新 `changelog.md`。
- [ ] 只有真实验证过的能力才写入 README 和简历描述。
- [ ] 更新版本号到最终 release 版本。
- [ ] 创建 release commit。
- [ ] 创建对应 Git tag。
- [ ] 确认 Git 工作区干净。
- [ ] 保存最终 `benchmark_report.md` 和 `security_report.md`。
- [ ] 记录 release commit、tag、CI URL 和报告路径。

建议 release 记录格式：

```text
Version: v0.3.1
Commit: <sha>
CI: <workflow-url>
Security report: docs/security_report.md
Benchmark report: docs/benchmark_report.md
Docker image digest: <digest>
```

## 4. 不要做的事情

在 R1～R5 完成前，不新增以下范围：

- 多 Agent 编排。
- 复杂 Web 前端。
- 大量新的模型 provider。
- 更多非核心工具。
- 没有实验数据支撑的性能优化。

当前项目的含金量取决于可验证的可靠性、安全性和实验数据，而不是功能数量。

## 5. 最终提交前检查

```bash
python -m pytest -q
ruff check agentforge tests scripts
mypy agentforge
python -m compileall -q agentforge
git diff --check
git status --short
```

最终发布必须额外满足：

```text
Docker live security: PASS
Real benchmark A/B/C/D: PASS
Remote CI: PASS
Clean clone reproduction: PASS
Release tag and reports: PRESENT
```

## 6. 完成记录

| 日期 | 任务 | 证据 | 结果 |
| --- | --- | --- | --- |
| 2026-09-10 | R1 | `sandbox diagnose`；容器集成测试 | PARTIAL：Docker Desktop Linux engine 可用，集成测试 `3 passed`；overlayfs 磁盘 quota 仍为 `requires-live-probe`。 |
| 2026-09-10 | R2 | 环境变量检查；fake solver 测试 | BLOCKED：无 `HARNESS_LLM_KEY`，未执行真实 A/B/C/D。 |
| 2026-09-10 | R3 | `git ls-remote`；本地 CI 等价检查 | BLOCKED：无可验证远端 workflow run，未推送。 |
| 2026-09-10 | R4 | 本地 tag clean clone；锁依赖、wheel、demo、pytest | PASS（本机）：`111 passed, 1 skipped`；当前源码 wheel 的 fresh-venv 入口和 selftest 也通过。 |
| 2026-09-10 | R5 | tag/release 状态检查 | BLOCKED：R1～R3 外部证据未齐，未完成正式发布收尾。 |

## 7. 本次执行记录

执行日期：2026-09-10（Windows，Python 3.13.5）。

- 本工作区自测：`111 passed, 1 skipped`，覆盖率 `77.87%`；`ruff check agentforge tests scripts`、`mypy agentforge`、`compileall`、`pip check`、`git diff --check` 均通过。
- 无模型演示和 CLI selftest 均通过；重新构建的 `v0.3.0` wheel/sdist 成功。
- R4 clean clone 已从本地 `v0.3.0` tag 复现：锁定依赖安装、wheel 构建和安装、`agentforge --help`、demo、完整 pytest、ruff、mypy、compileall、pip check 均通过；本轮安装后 wheel 的 CLI 参数校验和 selftest 也通过。
- R1 部分完成：`agentforge sandbox diagnose` 报告 Docker Desktop Linux engine 和固定 digest 镜像可用，容器集成测试 `3 passed`；没有宣称 overlayfs 磁盘 quota 或完整攻击集已验证。
- R2 未完成：`HARNESS_LLM_KEY` 未配置；没有执行真实 A/B/C/D benchmark，也没有生成 `docs/benchmark_report.md` 或伪造实验数字。
- R3 未完成：远端 `master` 已指向当前提交，但没有可验证的 workflow run 结果；GitHub Actions API 查询受匿名接口 403 限流，远端没有 `v0.3.0` tag 证据。
- R5 未完成：R1～R3 的外部证据缺失，因此没有创建新的 release commit、没有推送 tag，也没有把安全报告或 benchmark 报告标记为最终发布证据。

当前结论：本机可执行部分已完成并通过；R1 仍需 quota/攻击集证据，R2、R3 以及依赖它们的 R5 仍需模型凭据和远端 CI/发布权限后才能完成。
