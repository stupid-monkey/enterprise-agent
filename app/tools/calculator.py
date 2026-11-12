def calculator(
    a: float,
    b: float,
    operation: str
) -> float:

    if operation == "add":
        return a + b

    if operation == "subtract":
        return a - b

    if operation == "multiply":
        return a * b

    if operation == "divide":
        if b == 0:
            raise ValueError("除数不能为0")

        return a / b

    raise ValueError(
        f"未知运算类型：{operation}"
    )