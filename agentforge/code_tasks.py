"""内置 benchmark 任务。

FIX_ADD：给一个带 bug 的小仓库（add 返回 a-b），要求改对并验证。用验收 checks 判定
（file_contains + command stdout），鲁棒、不受 LLM 措辞/格式影响。seed 独立于
examples/demo_repo（那个已被演示跑动改对），本层自建基线，避免冲突。
"""

from __future__ import annotations

import os

from .eval import Check, CodeTask

CALC_BUGGY = """def add(a, b):
    return a - b


def mul(a, b):
    return a * b
"""

MAIN = """from calc import add, mul

if __name__ == "__main__":
    print(add(2, 3))
    print(mul(2, 3))
"""

README = """# buggy repo

`calc.add` 把 `a + b` 写成了 `a - b`，`main.py` 期望输出 5 和 6。
"""


def _seed_fix_add(workdir: str) -> None:
    os.makedirs(workdir, exist_ok=True)
    for rel, content in (("calc.py", CALC_BUGGY), ("main.py", MAIN), ("README.md", README)):
        with open(os.path.join(workdir, rel), "w", encoding="utf-8") as f:
            f.write(content)


FIX_ADD = CodeTask(
    name="fix-add",
    instruction=(
        "仓库里的 calc.py 中 add 函数返回 a-b（应为 a+b）。"
        "请把 add 改成返回 a+b，函数名和签名保持不变；然后运行 python main.py 确认输出 5 和 6。"
    ),
    build_seed=_seed_fix_add,
    checks=[
        Check(kind="file_contains", path="calc.py", needles=["return a + b"]),
        Check(kind="command", command="python main.py", stdout_contains=["5", "6"]),
    ],
)

BUILTIN_TASKS = [FIX_ADD]
