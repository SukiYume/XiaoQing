"""各子命令共用的数值解析。"""

import math


class NonFiniteError(ValueError):
    """文本能解析成浮点数，但结果是 NaN 或无穷。"""


def finite_float(value: str) -> float:
    """解析有限浮点数；无法解析抛 ``ValueError``，NaN/Inf 抛其子类 ``NonFiniteError``。"""

    result = float(value)
    if not math.isfinite(result):
        raise NonFiniteError("value must be finite")
    return result
