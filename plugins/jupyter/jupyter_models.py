"""Jupyter 执行结果及其有界文本呈现。"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .jupyter_config import MAX_OUTPUT_LENGTH

_TRUNCATION_SUFFIX = "\n…（输出已截断）"


def truncate_with_suffix(text: str, max_chars: int, suffix: str) -> str:
    """超过 ``max_chars`` 时保留开头并接上 ``suffix``，结果总长不超过 ``max_chars``。"""

    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - len(suffix)]}{suffix}"


@dataclass(slots=True)
class ExecutionResult:
    """一次内核执行的内存结果；图片必须是已验证 PNG 字节。

    ``failure`` 是执行器自身出的异常（不是用户代码报错）：插件入口记审计日志，并把 ``error``
    换成公开错误说明（原因、类型、位置和 request id），异常消息不展示。
    """

    success       : bool = True
    stdout        : str  = ""
    stderr        : str  = ""
    result        : str  = ""
    images        : list[bytes] = field(default_factory=list)
    error         : str                  = ""
    execution_time: float                = 0.0
    failure       : BaseException | None = None

    def format_output(self) -> str:
        """合并标准流、表达式结果和错误，并严格限制最终字符数。"""

        parts: list[str] = []
        if self.stdout:
            parts.append(self.stdout.strip())
        if self.result and self.result != "None":
            parts.append(f">>> {self.result}")
        if self.stderr:
            parts.append(f"⚠️ {self.stderr.strip()}")
        if self.error:
            parts.append(f"❌ {self.error}")
        if math.isfinite(self.execution_time) and self.execution_time > 0:
            parts.append(f"\n⏱️ {self.execution_time:.2f}s")

        output = "\n".join(parts) if parts else "(无输出)"
        return truncate_with_suffix(output, MAX_OUTPUT_LENGTH, _TRUNCATION_SUFFIX)
