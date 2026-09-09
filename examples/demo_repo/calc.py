"""一个故意带 bug 的小模块，供 AgentForge 演示代码修复。"""


def add(a, b):
    # 修复后：返回 a + b
    return a + b


def mul(a, b):
    return a * b


def safe_divide(a, b):
    if b == 0:
        raise ZeroDivisionError("division by zero")
    return a / b
