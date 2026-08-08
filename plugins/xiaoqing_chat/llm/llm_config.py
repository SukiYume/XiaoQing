"""插件级模型调用参数；provider 连接信息由 core AI registry 管理。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypedDict


class LLMRequestControls(TypedDict):
    """超时与重试关键字参数。"""

    timeout_seconds: float
    max_retry: int
    retry_interval_seconds: float


class LLMModelKwargs(TypedDict):
    """模型采样与输出预算关键字参数。"""

    temperature: float
    top_p: float
    max_tokens: int


@dataclass(frozen=True)
class LLMCallConfig:
    """模型 API 调用的通用参数。"""

    timeout_seconds: float = 30.0
    max_retry: int = 1
    retry_interval_seconds: float = 0.5
    temperature: float = 0.7
    top_p: float = 1.0
    max_tokens: int = 256

    def to_dict(self) -> LLMRequestControls:
        """以关键字参数形式返回调用控制参数。"""
        payload: LLMRequestControls = {
            "timeout_seconds": self.timeout_seconds,
            "max_retry": self.max_retry,
            "retry_interval_seconds": self.retry_interval_seconds,
        }
        return payload

    def to_model_kwargs(self) -> LLMModelKwargs:
        """以关键字参数形式返回 temperature、top_p 和 max_tokens。"""
        return {
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_tokens": self.max_tokens,
        }
