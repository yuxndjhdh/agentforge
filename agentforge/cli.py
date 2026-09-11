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
import json
import os
import sys
from dataclasses import replace
from pathlib import Path

from . import harness
from .config import load_config
from .trace import RunTrace


def _utf8() -> None:
    if os.name == "nt":
        try:
            reconfigure = getattr(sys.stdout, "reconfigure", None)
            if callable(reconfigure):
                reconfigure(encoding="utf-8")  # 避免 rich 打印 token 时踩 GBK('¥')
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


def cmd_eval(args) -> int:
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
    return 0


def cmd_benchmark(args) -> int:
    if args.dataset:
        return cmd_external_benchmark(args)
    from .benchmark import BenchmarkOptions, run_benchmark
    from .code_tasks import select_benchmark_tasks

    cfg = load_config()
    names = [name.strip() for name in args.tasks.split(",") if name.strip()] if args.tasks else None
    try:
        tasks = select_benchmark_tasks(names)
        options = BenchmarkOptions(
            context_compression_enabled=(
                cfg.context_compression_enabled
                if args.context_compression is None
                else args.context_compression
            ),
            verify_enabled=args.verify_retry,
            max_attempts=(
                args.verify_attempts
                if args.verify_attempts is not None
                else (3 if args.verify_retry else 1)
            ),
        )
    except ValueError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    report = run_benchmark(
        cfg,
        tasks,
        num_trials=args.trials,
        k=args.k,
        out_dir=args.out,
        seed=args.seed,
        options=options,
    )
    summary = report["summary"]
    print(
        f"benchmark: {report['benchmark_version']}  experiment={report['experiment_id']} "
        f"tasks={summary['tasks']} episodes={summary['episodes']}"
    )
    print(f"pass@1={summary['pass@1']:.3f}  pass@{args.k}={summary['pass@k']:.3f}")
    print(f"p50={summary['p50_latency_seconds']:.3f}s  p95={summary['p95_latency_seconds']:.3f}s")
    print(f"report: {report['report_path']}")
    return 0


def cmd_external_benchmark(args) -> int:
    from .external_tasks import (
        ExternalDataset,
        RepositoryMaterializer,
        run_external_episode,
        summarize_external_episodes,
    )

    cfg = load_config()
    if args.model:
        cfg = replace(cfg, model=args.model)
    dataset = ExternalDataset.load(args.dataset, split=args.split)
    names = {name.strip() for name in args.tasks.split(",") if name.strip()} if args.tasks else None
    tasks = [task for task in dataset.tasks if names is None or task.id in names]
    if not tasks:
        raise ValueError("no external tasks matched --tasks")
    verify_enabled = bool(args.verify_retry)
    max_attempts = args.verify_attempts if args.verify_attempts is not None else (3 if verify_enabled else 1)
    materializer = RepositoryMaterializer()
    episodes = []
    for trial in range(args.trials):
        for task in tasks:
            episode = run_external_episode(
                cfg,
                task,
                materializer=materializer,
                out_dir=Path(args.out) / "traces",
                verify_enabled=verify_enabled,
                max_attempts=max_attempts,
            )
            episode["trial"] = trial
            episode["seed"] = args.seed + trial
            episodes.append(episode)
    summary = summarize_external_episodes(episodes, seed=args.seed)
    report = {
        "schema_version": 1,
        "benchmark_version": "external-v1",
        "scope": "pipeline_smoke" if any(task.is_local_fixture for task in tasks) else "external_candidate",
        "dataset_version": dataset.version,
        "dataset_sha256": dataset.dataset_sha256,
        "manifest_sha256": dataset.manifest_sha256,
        "split": args.split,
        "model": cfg.model,
        "config": {"model": cfg.model, "temperature": cfg.temperature, "max_steps": cfg.max_steps},
        "task_order": [task.id for task in tasks],
        "trials": args.trials,
        "seed": args.seed,
        "summary": summary,
        "episodes": episodes,
        "limitations": summary["limitations"],
    }
    target = Path(args.out)
    target.mkdir(parents=True, exist_ok=True)
    (target / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (target / "episodes.json").write_text(json.dumps(episodes, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"external benchmark: scope={report['scope']} dataset={dataset.version} "
        f"split={args.split} tasks={len(tasks)} episodes={len(episodes)}"
    )
    print(f"final_success={summary['final_success']['numerator']}/{summary['final_success']['denominator']}")
    print(f"report: {target / 'report.json'}")
    return 0


def cmd_serve(args) -> None:
    from .api import create_app

    try:
        import uvicorn
    except ImportError as exc:
        raise RuntimeError("serve requires uvicorn; install agentforge[api]") from exc
    uvicorn.run(create_app(load_config()), host=args.host, port=args.port)


def cmd_memory(args) -> None:
    from .memory import MemoryStore

    store = MemoryStore(args.repo, project_id=args.project, user_id=args.user, run_id=args.run_id)
    if args.memory_cmd == "delete":
        print(store.delete(args.tier, args.key))
    elif args.memory_cmd == "export":
        import json

        print(json.dumps(store.export(), ensure_ascii=False, indent=2))
    elif args.memory_cmd == "audit":
        import json

        print(json.dumps(store.audit(args.limit), ensure_ascii=False, indent=2))


def cmd_selftest(args) -> None:
    res = harness.run_selftest(args.repo, out_dir=args.out)
    print("selftest OK")
    print(f"trace: {res.trace_path}")
    print(res.trace.render())


def cmd_sandbox_diagnose(args) -> None:
    import json

    from .sandbox import Sandbox

    cfg = load_config()
    print(json.dumps(Sandbox.from_config(cfg).container_diagnostics(), ensure_ascii=False, indent=2))


def cmd_verify(args) -> int:
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
    return 0


def main(argv=None) -> int:
    _utf8()
    parser = argparse.ArgumentParser(
        prog="agentforge",
        description="面向代码仓库的 Agent 执行、验收、追踪和安全沙箱平台。",
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

    p_bench = sub.add_parser("benchmark", help="运行可复现 benchmark 并生成报告")
    p_bench.add_argument("--trials", type=int, default=1, help="每任务独立运行次数")
    p_bench.add_argument("--k", type=int, default=1, help="计算 pass@k 的 k")
    p_bench.add_argument("--tasks", default="", help="逗号分隔任务名，缺省为 benchmark 全部任务")
    p_bench.add_argument("--out", default="runs/benchmarks/latest", help="报告输出目录")
    p_bench.add_argument("--seed", type=int, default=0, help="固定实验 seed（任务本身为确定性 seed）")
    p_bench.add_argument("--dataset", default="", help="声明式 external dataset manifest/directory")
    p_bench.add_argument("--split", choices=("validation", "holdout"), default="validation")
    p_bench.add_argument("--model", default="", help="external benchmark 使用的显式模型名")
    compression_group = p_bench.add_mutually_exclusive_group()
    compression_group.add_argument(
        "--context-compression",
        dest="context_compression",
        action="store_true",
        help="启用上下文压缩",
    )
    compression_group.add_argument(
        "--no-context-compression",
        dest="context_compression",
        action="store_false",
        help="关闭上下文压缩",
    )
    p_bench.set_defaults(context_compression=None)
    retry_group = p_bench.add_mutually_exclusive_group()
    retry_group.add_argument(
        "--verify-retry",
        dest="verify_retry",
        action="store_true",
        help="启用验收失败反馈重试",
    )
    retry_group.add_argument(
        "--no-verify-retry",
        dest="verify_retry",
        action="store_false",
        help="关闭验收失败反馈重试",
    )
    p_bench.set_defaults(verify_retry=False)
    p_bench.add_argument(
        "--verify-attempts",
        type=int,
        default=None,
        help="启用 Verify retry 时的最大尝试次数，缺省为 3",
    )
    p_bench.set_defaults(fn=cmd_benchmark)

    p_serve = sub.add_parser("serve", help="启动 FastAPI 服务")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.set_defaults(fn=cmd_serve)

    p_memory = sub.add_parser("memory", help="管理项目记忆")
    p_memory.add_argument("repo", help="仓库路径")
    p_memory.add_argument("--project", default="default")
    p_memory.add_argument("--user", default="default")
    p_memory.add_argument("--run-id", default=None, help="run-local 记忆作用域")
    memory_sub = p_memory.add_subparsers(dest="memory_cmd", required=True)
    p_delete = memory_sub.add_parser("delete", help="删除一条记忆")
    p_delete.add_argument("tier", choices=("durable", "daily", "run-local"))
    p_delete.add_argument("key")
    p_delete.set_defaults(fn=cmd_memory)
    p_export = memory_sub.add_parser("export", help="导出记忆")
    p_export.set_defaults(fn=cmd_memory)
    p_audit = memory_sub.add_parser("audit", help="查看记忆审计日志")
    p_audit.add_argument("--limit", type=int, default=100)
    p_audit.set_defaults(fn=cmd_memory)

    p_self = sub.add_parser("selftest", help="不用 LLM 的自检（工具链 + trace 落盘）")
    p_self.add_argument("--repo", default=None, help="自检用仓库（缺省用临时样例目录）")
    p_self.add_argument("--out", default="runs", help="trace 输出目录")
    p_self.set_defaults(fn=cmd_selftest)

    p_sandbox = sub.add_parser("sandbox", help="检查容器沙箱运行时")
    sandbox_sub = p_sandbox.add_subparsers(dest="sandbox_cmd", required=True)
    p_sandbox_diag = sandbox_sub.add_parser("diagnose", help="诊断 Docker/Podman 和磁盘限制能力")
    p_sandbox_diag.set_defaults(fn=cmd_sandbox_diagnose)

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
