"""判断对小青说的这句话是闲聊、新任务，还是在问、停或补充正在跑的任务。"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .loop import AGENT_ROUTE
from .prompts import build_triage_messages, capability_summary, current_time
from .tasks import AgentTask

TRIAGE_ROUTE    = "triage"
TRIAGE_ACTIONS  = frozenset({"chat", "task", "cancel", "status", "update"})
_MAX_TASK_ID    = 32
_TRIAGE_TIMEOUT = 20.0
_FENCE_RE       = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class TriageDecision:
    action: str
    goal: str    = ""
    reply: str   = ""
    task_id: str = ""


CHAT = TriageDecision("chat")


def _first_json_object(text: str) -> dict[str, Any] | None:
    cleaned = _FENCE_RE.sub("", text.strip())
    decoder = json.JSONDecoder()
    for index, char in enumerate(cleaned):
        if char != "{":
            continue
        try:
            value, _end = decoder.raw_decode(cleaned[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _finished_ids(task_id: str, running: Sequence[AgentTask]) -> set[str]:
    """``task_id`` 指名了一个不在本会话运行中的任务时返回它，其他情况返回空集。"""

    if not task_id or task_id.casefold() == "all":
        return set()
    return set() if any(task.task_id == task_id for task in running) else {task_id}


def parse_decision(
    text: str,
    *,
    request_text: str,
    running: Sequence[AgentTask],
) -> TriageDecision:
    """解析模型输出；任何不确定都回到普通聊天，不让判断失误打断正常对话。"""

    payload = _first_json_object(text or "")
    if payload is None:
        return CHAT
    action = str(payload.get("action") or "").strip().lower()
    if action not in TRIAGE_ACTIONS or action == "chat":
        return CHAT
    # 任务 ID 原样保留：写错或已经结束的 ID 要报“找不到”，不能当成没指定而扩大范围。
    task_id = str(payload.get("task_id") or "").strip()[:_MAX_TASK_ID]
    goal    = str(payload.get("goal") or "").strip() or request_text.strip()
    if action in {"cancel", "status"}:
        if not running:
            return CHAT
        return TriageDecision(action, task_id=task_id)
    # 指向已经结束的任务的“补充”其实是要改它的结果，另起任务去做；运行中的任务不受影响。
    if action == "update" and running and task_id not in _finished_ids(task_id, running):
        return TriageDecision("update", goal=goal[:2000], task_id=task_id)
    reply = str(payload.get("reply") or "").strip()
    return TriageDecision("task", goal=goal[:2000], reply=reply[:200])


async def triage_turn(
    context: Any,
    *,
    bot_name: str,
    text: str,
    history: Sequence[dict[str, Any]],
    running: Sequence[AgentTask],
    recent: Sequence[dict[str, Any]],
    quoted: str           = "",
    images: int           = 0,
    initiator: int | None = None,
    persona: str          = "",
) -> TriageDecision:
    """未单独配置 ``triage`` route 时复用 ``agent`` route。"""

    ai = context.capabilities.ai
    if ai is None:
        return CHAT
    route    = TRIAGE_ROUTE if context.get_config("ai.routes.triage") is not None else AGENT_ROUTE
    messages = build_triage_messages(
        bot_name     = bot_name,
        text         = text,
        history      = history,
        running      = running,
        recent       = recent,
        quoted       = quoted,
        images       = images,
        capabilities = capability_summary(
            context.get_command_catalog(),
            getattr(context, "principal", None),
        ),
        initiator = initiator,
        persona   = persona,
        now       = "（".join(current_time(context)) + "）",
    )
    result = await ai.complete(
        route,
        messages,
        temperature           = 0.1,
        total_timeout_seconds = _TRIAGE_TIMEOUT,
    )
    return parse_decision(result.content, request_text=text, running=running)
