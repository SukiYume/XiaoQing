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
    image_plan: Any = None
    emoji_plan: Any = None
    face_plan: Any = None


@dataclass(frozen=True)
class _ReplyEnvelope:
    text: str
    display_parts: tuple[dict[str, Any], ...]
    send_parts: tuple[dict[str, Any], ...]
    payload: Any
