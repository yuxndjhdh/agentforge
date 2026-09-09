# AgentForge

基于 [smolagents](https://github.com/huggingface/smolagents) 的 **本地代码 Agent Harness**（最小 MVP）。

给 agent 一个代码仓库和一个自然语言任务，它就能**读代码 → 定位 → 改代码**，整个过程落成一份可审计的 trace，并支持在同一 agent 上断点续跑。

## 它是什么 / 不是什么

- **是**：跑在代码仓库上的 agent harness——执行循环、代码工具、工作目录收敛、trace 落盘、断点续跑，以及**终态 hash 评测**（能自动判对错、批量出 pass@k）。
- **不是**：完整的 JCode。分层记忆、工具级沙箱属后续迭代，仓库结构已为其留位。

## 架构

```
                ┌───────────────────────────── ToolCallingAgent (smolagents) ─────────────────────────┐
                │  model = CompactingModel(OpenAICompatServerModel)                                  │
                │             ├ 剥 tool_choice，兼容 DeepSeek                                          │
                │             └ 消息串超预算时折叠最旧步（请求级上下文压缩）                                 │
                │  tools = read_file / write_file / list_dir / grep / run_command                       │
                └──────────────┬──────────────────────────────────────────────┬───────────────────────┘
                               │ agent.run(task)                              │ run(task, reset=False)=断点续跑
                               ▼                                              ▼
                        agent.memory.steps ──抽取──► RunTrace ──dump──► runs/<run_id>/trace.json
                                                                          （含 compression 统计）
```

- `agentforge/config.py`：环境变量 → `ModelConfig`（与 agent-eval-harness 共用 `HARNESS_LLM_*`，外加压缩预算与沙箱开关）。
- `agentforge/llm.py`：`OpenAICompatServerModel` 剥 `tool_choice`（DeepSeek 推理模式兼容）；`CompactingModel` 在其上加预算压缩。
- `agentforge/context.py`：`compact_messages` 纯函数——超预算时把最旧整步折叠成确定性摘要（工具名+观察片段）。
- `agentforge/sandbox.py`：`Sandbox` dataclass——白名单 + 默认拒绝命令/模式 + 读环境变量消杀 + 只读开关（工具级沙箱）。
- `agentforge/tools.py`：代码域工具，统一 workdir 收敛 + 防路径穿越 + 沙箱请求检查，参数错返回 `Error: ...` 观测。
- `agentforge/harness.py`：`make_agent` / `run_task` / `run_sequence`（断点续跑）/ `run_selftest`（无 LLM）。
- `agentforge/eval.py` / `code_tasks.py`：终态 hash 评测 + 内置 benchmark（见「评测层」）。
- `agentforge/trace.py`：`RunTrace` dump/load/render；MVP 里 trace 兼作 checkpoint，含压缩统计。

## 安装

```bash
cd Desktop/agentforge
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
cp .env.example .env    # 填入你的 DeepSeek key（或网关 token）
```

## 使用

```bash
# 无 LLM 自检：工具链 + trace 落盘
.venv/Scripts/python -m agentforge selftest

# 在示例仓库上跑一个任务
export HARNESS_LLM_KEY=<your-key>        # 或写进 .env
.venv/Scripts/python -m agentforge run examples/demo_repo "修复 calc.py 里 add 的 bug"

# 同一 agent 依次处理多个任务（演示断点续跑：后续任务 reset=False）
.venv/Scripts/python -m agentforge sequence examples/demo_repo "先看目录结构" --next "修复 add 的 bug"

# 渲染某次运行的轨迹
.venv/Scripts/python -m agentforge trace runs/<run_id>/trace.json

# 批量评测：内置 benchmark，输出 pass@1 / pass@k
.venv/Scripts/python -m agentforge eval --trials 1 --k 1

# 闭环自检：任务 + 权威验真，未过则同一 agent 带反馈重试（最多 N 轮）
.venv/Scripts/python -m agentforge verify fix-add --attempts 3
```

> Windows 注意：smolagents 用 rich 打印 token 用量会踩 GBK（'¥' 编码失败），跑之前
> `export PYTHONUTF8=1 PYTHONIOENCODING=utf-8`（cli 里已 try 设 UTF-8 兜底）。

## 上下文压缩（context compression）

smolagents 每步都会从 `memory.steps` 重新组装完整消息串再送模型，长任务（10-20 步、每步塞大段
代码观察）会让输入 token 单调上涨。`CompactingModel` 在模型层包一层：**消息串超预算时把最旧
整步折叠成一条确定性摘要**（工具名 + 观察片段，规则生成、无额外 LLM 调用），保留最近整步与 system
prompt。

- **注入点**：`generate()` 是步生成 / 规划 / 总结的单一汇聚点，包一层即全量覆盖，不动执行循环。
- **粒度**：按「整步」（assistant→tool_call→user→tool_response）丢弃，绝不拆碎 assistant↔observation
  配对，保证上下文完好。
- **请求级**：不改 `memory.steps`，每步重新压缩，最旧内容逐步让位给摘要——符合「本次请求控制 token」的目标。
- **可审计**：trace 落盘 `compression` 字段，`run_id/trace.json` 渲染出 `[压缩] 共 N 次，累计省 X 字符`。

环境变量：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `HARNESS_MAX_CONTEXT_CHARS` | `45000` | 消息串预算（字符数，避免引入 tiktoken/与 DeepSeek tokenizer 不匹配） |
| `HARNESS_CONTEXT_MIN_TAIL` | `4` | 至少保留的最近整步数，防止把模型正要用的最近观察压掉 |

## 分层记忆（working / daily / durable）

smolagents 的 `memory.steps` 只在进程内存里，进程一结束就丢。分层记忆让 agent **跨会话记得**：

| 层 | 载体 | 生命周期 | 读写方式 |
| --- | --- | --- | --- |
| working | smolagents `memory.steps` | 循环内 | 现成的事中记忆 |
| daily | `<workdir>/.agentforge/memory/daily-<日期>.json` | 当天 | `memory_save(tier="daily")` / `memory_search` |
| durable | `.agentforge/memory/durable.json` | 跨会话 | `memory_save(tier="durable")`；**启动即注入系统提示** |

- **工具**：`memory_save(tier, key, content)` 分层写入；`memory_search(query)` 跨 durable+daily 按关键词检索。
- **注入**：`make_agent` 用 `instructions=memory_instructions(workdir)` 把 durable 注入系统提示——
  **换进程、重跑也能想起来**。daily 靠 `memory_search` 按需拉取，避免系统提示膨胀。
- **隔离**：记忆目录以 `.` 开头，不污染 `eval.hash_tree`（hash_tree 忽略 `.` 前缀），grep 也已跳过 `.agentforge`；
  eval/verify 用临时副本所以记忆库为空，评测不串味。

```bash
# 第一次跑：agent 写一条 durable
.venv/Scripts/python -m agentforge run examples/demo_repo "用 memory_save tier=durable 记 key=calc-add → add 返回 a+b" --out runs
# 第二次跑（新进程）：durable 已注入系统提示，agent 直接答对上一步的 key
.venv/Scripts/python -m agentforge run examples/demo_repo "根据持久记忆告诉我 calc.add 返回什么" --out runs
```

## 测试 / 验证

```bash
.venv/Scripts/python -m pytest tests/ -q       # 工具 + trace 往返
.venv/Scripts/python -m agentforge selftest    # 跑 demo_repo，产出 runs/selftest/trace.json
```

## 工具级沙箱（白名单 / 权限 / 隔离环境）

给工具统一注入一个 `Sandbox`（四道闸，全是工具内请求检查，不改执行循环）：

1. **白名单**`whitelist`——非空才启用；命令首 token 必须命中（`python`、`ls` 等）。空 = 默认松（allow-anything-not-denied）。
2. **默认拒绝命令/模式**`deny_commands` + `deny_patterns`——默认拦 `rm -rf` / `curl` / `wget` / `sudo` / `nc` / `ssh` / `scp` / `rsync` / `sh -c` / `pip install` / `git push|reset --hard|clean` / `cd /`。
3. **只读开关**`readonly`——一键全禁写：`write_file` / `memory_save` / `run_command` 全部返回 Error。
4. **环境消杀**`env()`——抹掉名字含 `KEY`/`TOKEN`/`SECRET`/`PASSWORD` 的变量，防 agent 读到 `HARNESS_LLM_KEY`；加 `AGENTFORGE_SANDBOXED=1`。

`run_command` 还带沙箱 `timeout`；子进程用消杀后的 `env` + `cwd=workdir`。`make_agent` 从 `Sandbox.from_config(cfg)` 构建，注入 `all_code_tools(workdir, sandbox)`。

环境变量（默认全空/关 = 默认松）：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `HARNESS_SANDBOX_WHITELIST` | 空 | 逗号分隔命令白名单，空=不启用 |
| `HARNESS_SANDBOX_DENYLIST` | 空 | 逗号分隔，**追加**到默认拒绝首 token 列表 |
| `HARNESS_SANDBOX_DENY_PATTERNS` | 空 | 逗号分隔，**追加**到默认拒绝模式 |
| `HARNESS_SANDBOX_READONLY` | `0` | `1` = 全禁写/跑 |
| `HARNESS_SANDBOX_TIMEOUT` | `30.0` | run_command 超时（秒） |

```bash
# 只读沙箱压测：写/跑全挡，read/grep 仍可用
HARNESS_SANDBOX_READONLY=1 .venv/Scripts/python -m agentforge run examples/demo_repo "尝试写文件"
# 白名单：只允许 python/ls，其它命令一律被拦
HARNESS_SANDBOX_WHITELIST=python,ls .venv/Scripts/python -m agentforge run examples/demo_repo "试试 cat"
```

## 评测层（终态 hash）

`agentforge eval` 让 agent 在**每份全新副本**上跑任务，再用最终仓库状态判定对错（二值 reward），批量出 pass@1 / pass@k（组合估计器）。借鉴 agent-eval-harness 已验证的思路；状态对象换成**仓库文件树** `hash_tree`。

gold 两种、可并用、需同时通过：
- **严格 hash**：`gold_tree`（期望终态文件树），`实际树 hash == gold 树 hash`。
- **验收 checks**：`file_contains / command(stdout_contains) / file_absent`（Final Gate 式，鲁棒，内置演示任务用这个）。

内置任务见 `agentforge/code_tasks.py`（`FIX_ADD`：修 `a-b` 为 `a+b`）。无 LLM 部分由 `tests/test_eval.py` 覆盖。

## 闭环自检与反馈（self-verify + retry）

`eval` 是**外部计分器**（跑完才打分，agent 不知道对错）。闭环把它升级为**运行内自我验证**：

```
agent 在新鲜副本上修任务 → 权威 check 验真（eval_reward 0/1）
  ├─ 通过 → 结案
  └─ 未过 → failure_feedback 给出可读失败详情 → 塞回同一 agent 重试（run(reset=False)）
```

- **机制**：复用 `run(reset=False)` 的断点续跑（smolagens 在 memory.steps 追加 TaskStep），
  **同一 agent、同一副本**贯穿所有轮，不动执行循环。
- **权威判定**：`eval_reward`（checks / gold_tree 二值），与 `eval` 语义一致，不另立判据。
- **反馈**：`failure_feedback` 把未通过项写成中文（`- calc.py 应包含 "return a + b"` 等）。
- **可审计**：每轮 `verify` 步骤落进 trace（attempt/reward/feedback）。

```bash
# fix-add 在带 bug 副本上修对，首轮通过即停
.venv/Scripts/python -m agentforge verify fix-add --attempts 3
#   [attempt 0] reward=1  answer: ...
#   success: True
#   trace:   runs/verify/<run_id>/trace.json
```

## 已知要点与坑

- OpenAI 兼容 servers 要求 `tools[i]` 带顶层 `type: "function"`（smolagens 已处理）。
- DeepSeek 推理模型返回 `reasoning_content`，回传 assistant 消息须原样带回——smolagens 内部处理；我们只负责剥 `tool_choice`。
- 断点续跑（`run(reset=False)`）只在同一进程、同一 agent 实例上成立；**跨进程**重建完整 transcript 依赖 smolagens 内部 memory 序列化（非公开 API），是后续迭代点。
- `run_command` 受**工具级沙箱**约束（见「工具级沙箱」）：默认拦 `rm -rf`/`curl`/`wget`/`sudo`/`nc`/`sh -c`/`git reset --hard` 等，`env()` 抹掉 LLM key；但**非进程级强隔离**，不可信环境仍建议叠加容器。
- 上下文压缩用**字符数**做预算（确定性、无新依赖）；DeepSeek tokenizer 与 tiktoken 不完全一致，字符只是近似，不是 token 精确计费。
- 压缩是**请求级**：不改 `memory.steps`；`eval.reward` / `pass@k` 语义不受影响。

## 后续迭代（对齐 JCode）

- ~上下文压缩（本层已做：预算压测 + 确定性摘要折叠）~。
- ~分层记忆（Working / Daily / Durable）~。
- ~工具级沙箱（白名单、权限、隔离环境）~（现为工具级请求检查；进程级强隔离仍留待后续）。
- ~终态 hash 二值评测判定（复用 agent-eval-harness 的思路）~。

## 已实现的层

1. **执行 + trace + 断点续跑**：`run` / `sequence` / `trace` / `selftest`。
2. **终态 hash 评测**：`eval`，`hash_tree` + checks，`pass@1` / `pass@k`。
3. **上下文压缩**：`CompactingModel`，超预算折叠最旧整步，trace 记录压缩统计。
4. **闭环自检 + 反馈**：`verify`，权威 check 验真 + 失败反馈回喂同一 agent 重试。
5. **分层记忆**：`memory_save` / `memory_search` + durable 启动注入，跨会话记得项目知识。
6. **工具级沙箱**：`Sandbox` 白名单 + 默认拒绝命令/模式 + 环境变量消杀 + 只读开关，四道闸约束不可信 agent。
