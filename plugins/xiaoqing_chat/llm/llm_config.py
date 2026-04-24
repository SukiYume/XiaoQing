"""LLM call configuration -- bundles timeout/retry/proxy settings."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class LLMCallConfig:
    """Common parameters for LLM API calls."""
    timeout_seconds: float = 30.0
    max_retry: int = 1
    retry_interval_seconds: float = 0.5
    proxy: str = ""
    endpoint_path: str = "/v1/chat/completions"
    temperature: float = 0.7
    top_p: float = 1.0
    max_tokens: int = 256
    extra_payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return timeout/retry/proxy settings as kwargs."""
        payload = {
            "timeout_seconds": self.timeout_seconds,
            "max_retry": self.max_retry,
            "retry_interval_seconds": self.retry_interval_seconds,
            "proxy": self.proxy,
            "endpoint_path": self.endpoint_path,
        }
        if self.extra_payload:
            payload["extra_payload"] = dict(self.extra_payload)
        return payload

    def to_model_kwargs(self) -> dict[str, Any]:
        """Return temperature/top_p/max_tokens as kwargs for LLM calls."""
        return {
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_tokens": self.max_tokens,
        }
