"""定义小聊准备、生成和投递阶段共享的数据结构。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .planning.pfc_engine import PFCRunResult


@dataclass(frozen=True)
class _PreparedSmalltalkTurn:
    text: str
    mentioned: bool
    is_private: bool
    forced: bool
    force_reason: str
    brain_chat_active: bool
    mood_text: str
    collected_emoji_count: int


@dataclass
class _GeneratedSmalltalkTurn:
    local_id: str = ""
    pfc_result: PFCRunResult | None = None
    pfc_state_snapshot: Any = None
    speculative_memory_task: Any = None
    reply_source: str = "pfc"
    reply: str = ""
    reply_parts: tuple[dict[str, Any], ...] = ()
    reply_output: Any = None
    media_marker: Any = None


@dataclass(frozen=True)
class _ReplyEnvelope:
    text: str
    display_parts: tuple[dict[str, Any], ...]
    send_parts: tuple[dict[str, Any], ...]
    payload: Any
