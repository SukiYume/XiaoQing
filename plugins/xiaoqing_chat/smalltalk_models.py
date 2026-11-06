"""定义小聊准备、生成和投递阶段共享的数据结构。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .planning.pfc_engine import PFCRunResult
from .reply_payload import ReplyPayload


@dataclass(frozen=True)
class PreparedSmalltalkTurn:
    text             : str
    mentioned        : bool
    is_private       : bool
    forced           : bool
    force_reason     : str
    brain_chat_active: bool
    mood_text        : str


@dataclass
class GeneratedSmalltalkTurn:
    local_id          : str                 = ""
    session_generation: str | None          = None
    pfc_result        : PFCRunResult | None = None
    pfc_state_snapshot: Any                 = None
    # 取快照时登记的轮次号；提交时据此判断有没有更新的轮次已经写回过。
    pfc_turn               : int                        = 0
    speculative_memory_task: Any                        = None
    reply_source           : str                        = "pfc"
    reply                  : str                        = ""
    reply_parts            : tuple[dict[str, Any], ...] = ()
    reply_payload          : ReplyPayload | None        = None
    media_marker           : Any                        = None
    # 本轮各阶段耗时（规划、每次生成与审查、重写、上下文复核、拟人等待），随 smalltalk.done 记录。
    timings: dict[str, Any] = field(default_factory=dict)

    def cancel_speculative_memory(self) -> None:
        """丢弃本轮时取消仍在后台预取的记忆检索。"""
        task = self.speculative_memory_task
        if task is not None and not task.done():
            task.cancel()
