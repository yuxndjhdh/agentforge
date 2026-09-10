"""内置 benchmark 任务。

FIX_ADD：给一个带 bug 的小仓库（add 返回 a-b），要求改对并验证。用验收 checks 判定
（file_contains + command stdout），鲁棒、不受 LLM 措辞/格式影响。seed 独立于
examples/demo_repo（那个已被演示跑动改对），本层自建基线，避免冲突。
"""

from __future__ import annotations

import os
from collections.abc import Iterable

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
        Check(kind="command", command="python main.py", stdout_lines=["5", "6"]),
    ],
)

def _seed_tree(files: dict[str, str]):
    def seed(workdir: str) -> None:
        os.makedirs(workdir, exist_ok=True)
        for rel, content in files.items():
            target = os.path.join(workdir, rel)
            os.makedirs(os.path.dirname(target) or workdir, exist_ok=True)
            with open(target, "w", encoding="utf-8") as stream:
                stream.write(content)

    return seed


def _benchmark_task(
    name: str,
    instruction: str,
    files: dict[str, str],
    checks: list[Check],
    *,
    difficulty: str = "easy",
    tags: tuple[str, ...] = (),
) -> CodeTask:
    return CodeTask(
        name=name,
        instruction=instruction,
        build_seed=_seed_tree(files),
        checks=checks,
        difficulty=difficulty,
        tags=tags,
        resource_limits={"max_seconds": 30, "max_steps": 12},
    )


BENCHMARK_TASKS = [
    FIX_ADD,
    _benchmark_task(
        "fix-subtract",
        "修复 calc.py 的 subtract，使其返回 a-b，并运行 python main.py 验证输出 5。",
        {"calc.py": "def subtract(a, b):\n    return a + b\n", "main.py": "from calc import subtract\nprint(subtract(8, 3))\n"},
        [Check(kind="file_contains", path="calc.py", needles=["return a - b"]), Check(kind="command", command="python main.py", stdout_lines=["5"])],
    ),
    _benchmark_task(
        "fix-uppercase",
        "修复 normalize，使输入文本转换为大写，然后运行 main.py。",
        {"text.py": "def normalize(value):\n    return value.lower()\n", "main.py": "from text import normalize\nprint(normalize('agent'))\n"},
        [Check(kind="file_contains", path="text.py", needles=["return value.upper()"]), Check(kind="command", command="python main.py", stdout_lines=["AGENT"])],
    ),
    _benchmark_task(
        "fix-clamp",
        "修复 clamp，确保低于最小值返回 lo，高于最大值返回 hi，并运行 main.py。",
        {"numbers.py": "def clamp(value, lo, hi):\n    return value\n", "main.py": "from numbers import clamp\nprint(clamp(9, 0, 5))\n"},
        [Check(kind="file_contains", path="numbers.py", needles=["min(hi, max(lo, value))"]), Check(kind="command", command="python main.py", stdout_lines=["5"])],
    ),
    _benchmark_task(
        "fix-average",
        "修复 average，使空列表返回 0 且非空列表返回算术平均值。",
        {"stats.py": "def average(values):\n    return sum(values) / len(values)\n", "main.py": "from stats import average\nprint(average([]))\n"},
        [Check(kind="file_contains", path="stats.py", needles=["if not values", "return 0"]), Check(kind="command", command="python main.py", stdout_lines=["0"])],
        tags=("bug-fix", "edge-case"),
    ),
    _benchmark_task(
        "fix-json-key",
        "修复 user_payload，返回名为 user_id 的字段，并运行 main.py。",
        {"payload.py": "def user_payload(user_id):\n    return {'id': user_id}\n", "main.py": "from payload import user_payload\nprint(user_payload(7)['user_id'])\n"},
        [Check(kind="file_contains", path="payload.py", needles=["'user_id': user_id"]), Check(kind="command", command="python main.py", stdout_lines=["7"])],
        tags=("api", "bug-fix"),
    ),
    _benchmark_task(
        "fix-safe-divide",
        "给 divide 增加除数为零时返回 None 的行为，并运行 main.py。",
        {"mathlib.py": "def divide(a, b):\n    return a / b\n", "main.py": "from mathlib import divide\nprint(divide(4, 0))\n"},
        [Check(kind="file_contains", path="mathlib.py", needles=["if b == 0", "return None"]), Check(kind="command", command="python main.py", stdout_lines=["None"])],
        tags=("bug-fix", "security"),
    ),
    _benchmark_task(
        "fix-cache",
        "修复 memoize，使同一个 key 的第二次读取使用缓存值，并运行 main.py 输出 1。",
        {"cache.py": "class Cache:\n    def __init__(self):\n        self.values = {}\n\n    def get(self, key, factory):\n        return factory()\n", "main.py": "from cache import Cache\nc = Cache()\nprint(c.get('x', lambda: 1))\n"},
        [Check(kind="file_contains", path="cache.py", needles=["if key not in self.values", "self.values[key]"])],
        tags=("bug-fix", "state"),
    ),
    _benchmark_task(
        "fix-retry",
        "修复 retry，使 attempts 表示最多尝试次数，并在成功后停止。",
        {"retry.py": "def retry(fn, attempts):\n    for _ in range(attempts):\n        return fn()\n    raise RuntimeError('failed')\n"},
        [Check(kind="file_contains", path="retry.py", needles=["for attempt in range(attempts)", "except Exception"]),],
        difficulty="medium",
        tags=("bug-fix", "control-flow"),
    ),
    _benchmark_task(
        "add-unit-test",
        "为 calc.add 增加 unittest 回归测试，测试 add(2,3)==5，并运行测试。",
        {"calc.py": "def add(a, b):\n    return a + b\n"},
        [Check(kind="file_contains", path="test_calc.py", needles=["unittest", "assertEqual", "add(2, 3)"]), Check(kind="command", command="python -m unittest -q")],
        tags=("tests",),
    ),
    _benchmark_task(
        "add-api-field",
        "让 user() 返回 active=True 字段并保持已有 name 字段，运行 main.py。",
        {"service.py": "def user(name):\n    return {'name': name}\n", "main.py": "from service import user\nprint(user('a')['active'])\n"},
        [Check(kind="file_contains", path="service.py", needles=["'active': True"]), Check(kind="command", command="python main.py", stdout_lines=["True"])],
        tags=("api",),
    ),
    _benchmark_task(
        "api-status-code",
        "让 response() 在 ok=True 时返回 status=200，否则返回 status=400。",
        {"http.py": "def response(ok):\n    return {'status': 200}\n"},
        [Check(kind="file_contains", path="http.py", needles=["200 if ok else 400"])],
        tags=("api", "edge-case"),
    ),
    _benchmark_task(
        "cli-argument",
        "让 CLI 使用第一个命令行参数作为名字，缺省时使用 world，并运行 python main.py agent。",
        {"main.py": "import sys\nname = 'world'\nprint('hello ' + name)\n"},
        [Check(kind="file_contains", path="main.py", needles=["sys.argv", "hello "]), Check(kind="command", command="python main.py agent", stdout_lines=["hello agent"])],
        tags=("cli",),
    ),
    _benchmark_task(
        "config-default",
        "将配置的 timeout 默认值改为 30，并运行 main.py。",
        {"config.py": "TIMEOUT = 10\n", "main.py": "from config import TIMEOUT\nprint(TIMEOUT)\n"},
        [Check(kind="file_contains", path="config.py", needles=["TIMEOUT = 30"]), Check(kind="command", command="python main.py", stdout_lines=["30"])],
        tags=("config",),
    ),
    _benchmark_task(
        "config-env",
        "让配置从 APP_MODE 环境变量读取 mode，缺省为 dev，并运行 main.py。",
        {"config.py": "mode = 'prod'\n", "main.py": "from config import mode\nprint(mode)\n"},
        [Check(kind="file_contains", path="config.py", needles=["os.environ.get('APP_MODE', 'dev')"]), Check(kind="command", command="python main.py", stdout_lines=["dev"])],
        tags=("config", "env"),
    ),
    _benchmark_task(
        "refactor-helper",
        "把重复的格式化逻辑提取为 format_name(name)，并让两个调用点使用它。",
        {"names.py": "def first(name):\n    return name.strip().title()\n\ndef second(name):\n    return name.strip().title()\n"},
        [Check(kind="file_contains", path="names.py", needles=["def format_name", "return format_name(name)"])],
        difficulty="medium",
        tags=("refactor",),
    ),
    _benchmark_task(
        "security-path",
        "修复 read_name，拒绝绝对路径，只允许读取 base 目录内的文件。",
        {"safe.py": "import os\ndef read_name(base, name):\n    return open(os.path.join(base, name)).read()\n"},
        [Check(kind="file_contains", path="safe.py", needles=["os.path.abspath", "startswith"]),],
        difficulty="medium",
        tags=("security", "path"),
    ),
    _benchmark_task(
        "security-secret",
        "修复 log_config，不要把 password 的明文写入返回值，只返回 password=***。",
        {"logging.py": "def log_config(password):\n    return f'password={password}'\n"},
        [Check(kind="file_contains", path="logging.py", needles=["password=***"]),],
        tags=("security",),
    ),
    _benchmark_task(
        "docs-command",
        "在 README.md 增加运行命令 python main.py，并保持项目标题。",
        {"README.md": "# sample\n\nA small project.\n"},
        [Check(kind="file_contains", path="README.md", needles=["python main.py"])],
        tags=("docs",),
    ),
    _benchmark_task(
        "type-contract",
        "为 add 增加 int 类型标注，并保持返回 a+b。",
        {"typed.py": "def add(a, b):\n    return a + b\n"},
        [Check(kind="file_contains", path="typed.py", needles=["def add(a: int, b: int) -> int"]),],
        tags=("api", "types"),
    ),
    _benchmark_task(
        "fix-date-format",
        "让 format_date 返回 YYYY-MM-DD 格式，并运行 main.py。",
        {"dates.py": "def format_date(year, month, day):\n    return f'{year}/{month}/{day}'\n", "main.py": "from dates import format_date\nprint(format_date(2026, 1, 2))\n"},
        [Check(kind="file_contains", path="dates.py", needles=["04d", "02d"]), Check(kind="command", command="python main.py", stdout_lines=["2026-01-02"])],
        tags=("bug-fix",),
    ),
    _benchmark_task(
        "fix-filter",
        "让 positive_only 只返回大于零的数字，并运行 main.py。",
        {"filters.py": "def positive_only(values):\n    return [value for value in values]\n", "main.py": "from filters import positive_only\nprint(positive_only([-1, 0, 2]))\n"},
        [Check(kind="file_contains", path="filters.py", needles=["if value > 0"]), Check(kind="command", command="python main.py", stdout_lines=["[2]"])],
        tags=("bug-fix",),
    ),
    _benchmark_task(
        "fix-immutability",
        "让 add_tag 返回新列表，不修改传入的 tags，并运行 main.py。",
        {"tags.py": "def add_tag(tags, tag):\n    tags.append(tag)\n    return tags\n", "main.py": "from tags import add_tag\nitems = ['a']\nprint(items, add_tag(items, 'b'))\n"},
        [Check(kind="file_contains", path="tags.py", needles=["return [*tags, tag]"]), Check(kind="command", command="python main.py", stdout_lines=["['a'] ['a', 'b']"])],
        difficulty="medium",
        tags=("bug-fix", "state"),
    ),
]

# The original one-task list remains the CLI's quick smoke benchmark. The
# larger list is used by ``agentforge benchmark`` and the reproducibility docs.
BUILTIN_TASKS = [FIX_ADD]


def select_benchmark_tasks(names: Iterable[str] | None = None) -> list[CodeTask]:
    """Select the canonical benchmark order and reject unknown task names."""
    if names is None:
        return list(BENCHMARK_TASKS)
    requested = [str(name).strip() for name in names if str(name).strip()]
    known = {task.name for task in BENCHMARK_TASKS}
    unknown = [name for name in requested if name not in known]
    if unknown:
        raise ValueError(
            f"unknown benchmark task(s): {', '.join(unknown)}; "
            f"available: {', '.join(task.name for task in BENCHMARK_TASKS)}"
        )
    wanted = set(requested)
    return [task for task in BENCHMARK_TASKS if task.name in wanted]
