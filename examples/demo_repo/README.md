# demo_repo

一个微型示例仓库，供 AgentForge 演示"读代码 → 定位 bug → 修改代码"。

- `calc.py`：`add(a, b)` 故意把 `a + b` 写成 `a - b`。
- `main.py`：调用 `add(2, 3)`，期望输出 5，实际输出 -1。
