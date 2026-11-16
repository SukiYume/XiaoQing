"""回复检查器测试共享的消息构造器和审查模型回答。"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from typing import Any

from plugins.xiaoqing_chat.memory.memory import StoredMessage
from tests.helpers.xiaoqing_chat_test_support import ScriptedChatModel


def stored_message(role: str, content: str, name: str = "") -> StoredMessage:
    """构造带当前时间戳的最小历史消息。"""

    return StoredMessage(role=role, content=content, name=name, ts=time.time())


def checker_verdict(*, suitable: bool = True, reason: str = "", **fields: Any) -> str:
    """审查模型的一次回答：完整的证据审查 JSON，默认各判定轴都通过。

    ``suitable=False`` 时默认是硬拒绝并要求重新规划；``fields`` 覆盖任何字段
    （某条判定轴、``need_replan``、``severity``、各类 ``*_claims`` 等）。
    """

    payload: dict[str, Any] = {
        "suitable"                : suitable,
        "reason"                  : reason,
        "need_replan"             : not suitable,
        "severity"                : "soft" if suitable else "hard",
        "context_coherent"        : True,
        "speaker_correct"         : True,
        "instruction_followed"    : True,
        "persona_grounded"        : True,
        "factually_plausible"     : True,
        "non_template"            : True,
        "image_evidence_respected": True,
        "persona_scan_complete"   : True,
        "persona_claims"          : [],
        "context_scan_complete"   : True,
        "context_claims"          : [],
        "visual_scan_complete"    : True,
        "visual_claims"           : [],
        **fields,
    }
    return json.dumps(payload, ensure_ascii=False)


def semantic_checker_model(claims: Sequence[Sequence[dict[str, Any]]]) -> ScriptedChatModel:
    """审查路由依次给出每组“对话事实清单”；实际证据校验仍由生产检查器执行。"""

    return ScriptedChatModel(checker=[checker_verdict(context_claims=claim) for claim in claims])
