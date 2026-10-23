"""插件级模型调用参数；provider 连接信息由 core AI registry 管理。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TypedDict

# 明确要求长文时输出预算的上限（按每字 1.5 个 token 算约五千字）。
_LONG_OUTPUT_MAX_TOKENS = 8192


class LLMRequestControls(TypedDict):
    """超时与重试关键字参数。"""

    timeout_seconds: float
    max_retry: int
    retry_interval_seconds: float


@dataclass(frozen=True)
class LLMCallConfig:
    """一次模型调用的采样、输出预算、超时与重试参数。"""

    timeout_seconds: float        = 30.0
    max_retry: int                = 1
    retry_interval_seconds: float = 0.5
    temperature: float            = 0.7
    top_p: float                  = 1.0
    max_tokens: int               = 256

    def request_controls(self) -> LLMRequestControls:
        """只含超时与重试的关键字参数。"""
        return {
            "timeout_seconds": self.timeout_seconds,
            "max_retry": self.max_retry,
            "retry_interval_seconds": self.retry_interval_seconds,
        }

    def as_kwargs(self) -> dict[str, float | int]:
        """调用模型所需的全部采样、预算与重试关键字参数。"""
        return {
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_tokens": self.max_tokens,
            "timeout_seconds": self.timeout_seconds,
            "max_retry": self.max_retry,
            "retry_interval_seconds": self.retry_interval_seconds,
        }

    def with_room_for(self, chars: int) -> LLMCallConfig:
        """对方明确要长文时放宽输出预算和超时，按每字约 1.5 个 token 留足余量。"""
        tokens = min(_LONG_OUTPUT_MAX_TOKENS, int(chars * 1.5) + 200)
        return replace(
            self,
            max_tokens      = max(int(self.max_tokens), tokens),
            timeout_seconds = max(float(self.timeout_seconds), min(90.0, 10.0 + chars / 60)),
        )

    def bounded(
        self, *, max_temperature: float, max_tokens: int, min_tokens: int = 0
    ) -> LLMCallConfig:
        """为单个任务收紧采样温度，并把输出预算限制在 [min_tokens, max_tokens]。"""
        return replace(
            self,
            temperature = min(max_temperature, float(self.temperature)),
            max_tokens  = min(max_tokens, max(min_tokens, int(self.max_tokens))),
        )
