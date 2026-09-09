from calc import add, mul, safe_divide

if __name__ == "__main__":
    # 期望 5，但 calc.add 有 bug 会返回 -1
    print(add(2, 3))
    print(mul(2, 3))
    print(mul(4, 5))
