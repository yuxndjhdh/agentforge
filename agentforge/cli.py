"""CLI 入口：run / sequence / trace / selftest。

```
python -m agentforge run examples/demo_repo "修复 calc.py 的 bug"
python -m agentforge sequence examples/demo_repo "先看目录" --next "修复 bug" --next "补个测试"
python -m agentforge trace runs/<run_id>/trace.json
python -m agentforge selftest
```
"""

from __future__ import annotations

import argparse
import os
import sys

from . import harness
from .config import load_config
from .trace import RunTrace


def _utf8() -> None:
    if os.name == "nt":
        try:
            sys.stdout.reconfigure(encoding="utf-8")  # 避免 rich 打印 token 时踩 GBK('¥')
        except Exception:
            pass


def cmd_run(args) -> None:
    cfg = load_config()
    res = harness.run_task(cfg, args.task, args.repo, out_dir=args.out)
    print(f"run_id: {res.run_id}")
    print(f"trace:  {res.trace_path}")
    print(f"answer: {res.answer[:500]}")


def cmd_sequence(args) -> None:
    cfg = load_config()
    tasks = [args.task] + list(args.next or [])
    results = harness.run_sequence(cfg, tasks, args.repo, out_dir=args.out)
    for r in results:
        print(f"[{r.run_id}] task done, trace: {r.trace_path}")


def cmd_trace(args) -> None:
    print(RunTrace.load(args.path).render())


def cmd_eval(args) -> None:
    from .code_tasks import BUILTIN_TASKS
    from .eval import evaluate

    cfg = load_config()
    tasks = BUILTIN_TASKS
    if args.tasks:
        names = {n.strip() for n in args.tasks.split(",") if n.strip()}
        tasks = [t for t in tasks if t.name in names]
        if not tasks:
            print(f"错误: 没有匹配的任务名 {args.tasks}（可选: {[t.name for t in BUILTIN_TASKS]}）", file=sys.stderr)
            return 2
    res = evaluate(cfg, tasks, num_trials=args.trials, k=args.k, out_dir=args.out)
    print(f"tasks={res['tasks']}  num_trials={res['num_trials']}")
    print(f"pass@1={res['pass@1']:.3f}  pass@{args.k}={res['pass@k']:.3f}")
    print(f"success: {res['success']}")
    for f in res["failures"]:
        print(f"  FAIL [{f['task']}] {f['verdict']}")
        if f.get("diff"):
            print(f"        diff: {str(f['diff'])[:200]}")


def cmd_selftest(args) -> None:
    res = harness.run_selftest(args.repo, out_dir=args.out)
    print("selftest OK")
    print(f"trace: {res.trace_path}")
    print(res.trace.render())


def cmd_verify(args) -> None:
    from .code_tasks import BUILTIN_TASKS
    from .verify import run_verified

    cfg = load_config()
    verify_task = next((t for t in BUILTIN_TASKS if t.name == args.task), None)
    if verify_task is None:
        print(f"错误: 没有匹配的任务名 {args.task}（可选: {[t.name for t in BUILTIN_TASKS]}）", file=sys.stderr)
        return 2
    res = run_verified(
        cfg, verify_task.instruction, verify_task, max_attempts=args.attempts, out_dir=args.out
    )
    for a in res.attempts:
        print(f"[attempt {a['attempt']}] reward={a['reward']}  answer: {a['answer'][:120]}")
        if not a["reward"]:
            print(f"    自检反馈: {a['feedback'][:200]}")
    print(f"success: {res.success}")
    print(f"workdir: {res.workdir}")
    print(f"trace:   {res.trace_path}")


def main(argv=None) -> int:
    _utf8()
    parser = argparse.ArgumentParser(
        prog="agentforge",
        description="基于 smolagents 的本地代码 Agent Harness（最小 MVP）。",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="在仓库上跑一个任务")
    p_run.add_argument("repo", help="仓库（工作目录）路径")
    p_run.add_argument("task", help="自然语言任务")
    p_run.add_argument("--out", default="runs", help="trace 输出目录")
    p_run.set_defaults(fn=cmd_run)

    p_seq = sub.add_parser("sequence", help="同一 agent 依次处理多个任务（演示断点续跑）")
    p_seq.add_argument("repo", help="仓库（工作目录）路径")
    p_seq.add_argument("task", help="第一个任务")
    p_seq.add_argument("--next", action="append", help="后续任务（可多个，reset=False 续跑）")
    p_seq.add_argument("--out", default="runs", help="trace 输出目录")
    p_seq.set_defaults(fn=cmd_sequence)

    p_trace = sub.add_parser("trace", help="渲染一次运行的轨迹")
    p_trace.add_argument("path", help="runs/.../trace.json")
    p_trace.set_defaults(fn=cmd_trace)

    p_eval = sub.add_parser("eval", help="批量评测任务，输出 pass@1 / pass@k")
    p_eval.add_argument("--trials", type=int, default=1, help="每任务独立运行次数")
    p_eval.add_argument("--k", type=int, default=1, help="计算 pass@k 的 k")
    p_eval.add_argument("--tasks", default="", help="逗号分隔任务名，缺省全部")
    p_eval.add_argument("--out", default="runs/eval", help="失败轨迹落盘目录")
    p_eval.set_defaults(fn=cmd_eval)

    p_self = sub.add_parser("selftest", help="不用 LLM 的自检（工具链 + trace 落盘）")
    p_self.add_argument("--repo", default=None, help="自检用仓库（缺省用临时样例目录）")
    p_self.add_argument("--out", default="runs", help="trace 输出目录")
    p_self.set_defaults(fn=cmd_selftest)

    p_verify = sub.add_parser("verify", help="闭环自检：任务+验真+未过重试")
    p_verify.add_argument("task", help="任务名（BUILTIN_TASKS 里）")
    p_verify.add_argument("--attempts", type=int, default=3, help="最多尝试轮数")
    p_verify.add_argument("--out", default="runs/verify", help="trace 输出目录")
    p_verify.set_defaults(fn=cmd_verify)

    args = parser.parse_args(argv)
    try:
        return args.fn(args) or 0
    except (ValueError, RuntimeError) as e:
        print(f"错误: {e}", file=sys.stderr)
        return 2
