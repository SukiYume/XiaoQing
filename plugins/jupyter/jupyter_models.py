"""Jupyter 执行结果及其有界文本呈现。"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .jupyter_config import MAX_OUTPUT_LENGTH

_TRUNCATION_SUFFIX = "\n…（输出已截断）"


@dataclass(slots=True)
class ExecutionResult:
    """一次内核执行的内存结果；图片必须是已验证 PNG 字节。"""

    success: bool = True
    stdout: str   = ""
    stderr: str   = ""
    result: str   = ""
    images: list[bytes] = field(default_factory=list)
    error: str            = ""
    execution_time: float = 0.0

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
        if len(output) <= MAX_OUTPUT_LENGTH:
            return output
        kept_chars = MAX_OUTPUT_LENGTH - len(_TRUNCATION_SUFFIX)
        return f"{output[:kept_chars]}{_TRUNCATION_SUFFIX}"
