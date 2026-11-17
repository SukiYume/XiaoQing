"""agent 和统一记忆（xiaoqing_memory）之间的两件事。

- 开新办事会话时取一次发起人的记忆：办事的固定偏好、以前踩过的坑、答应过他的事。结果存在会话里，
  会话内不再更新，系统提示保持不变，提示缓存继续命中。来源链里有网页文字的内容记忆插件一律不给。
- 一次办事结束时写一条 ``task`` 事件：原话、运行中补充的要求、依次执行的命令和各自结局、最终回复
  （截断）和任务结局。不含工具的原始输出；完整往返仍在 ``transcripts/`` 里，用任务 ID 对应。

记忆插件不在、超时或出错时什么也不做，办事照常进行。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Sequence
from typing import Any

from core.log import LogEvent

from .tasks import AgentTask, Outcome

log = logging.getLogger(__name__)

RECALL_TIMEOUT    = 1.5
RECORD_TIMEOUT    = 5.0
_MAX_STEPS        = 12
_MAX_REPLY_CHARS  = 300
_MAX_RESULT_CHARS = 80
_MAX_TEXT_CHARS   = 1800
_STATUS_WORDS     = {
    "done"       : "办成了",
    "needs_user" : "等对方补充",
    "blocked"    : "受阻",
    "unfinished" : "没做完",
    "failed"     : "失败",
    "cancelled"  : "被停下",
    "interrupted": "被打断",
}


def _service(context: Any) -> Any:
    capabilities = getattr(context, "capabilities", None)
    return getattr(capabilities, "memory", None) if capabilities is not None else None


async def recall_for_session(context: Any, task: AgentTask) -> str:
    """开新会话时取一次；没有可用内容时为空串。"""

    service = _service(context)
    if service is None:
        return ""
    payload = {
        "purpose": "task",
        "scope"  : task.chat.key,
        "speaker": {"user_id": task.chat.user_id, "name": task.sender},
        "text"   : task.request_text[:300],
    }
    try:
        result = await asyncio.wait_for(service.recall(payload), timeout=RECALL_TIMEOUT)
    except Exception as exc:
        log.warning(LogEvent("memory.recall.skipped", error=exc))
        return ""
    blocks = result.get("blocks") if isinstance(result, dict) else None
    if not isinstance(blocks, dict):
        return ""
    parts    = [str(blocks.get("speaker") or "").strip()]
    promises = str(blocks.get("promises") or "").strip()
    if promises:
        parts.append("你答应过他、还没办完的事：\n" + promises)
    return "\n".join(part for part in parts if part)


def _short(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _calls(messages: Sequence[dict[str, Any]]) -> list[str]:
    """这一轮依次执行的命令和各自结局（工具回报的第一行）。"""

    results = {
        str(message.get("tool_call_id")): str(message.get("content") or "")
        for message in messages
        if message.get("role") == "tool"
    }
    lines: list[str] = []
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or ():
            function = call.get("function") if isinstance(call, dict) else None
            if not isinstance(function, dict):
                continue
            name = str(function.get("name") or "")
            if name == "final_answer":
                continue
            try:
                arguments = json.loads(function.get("arguments") or "{}")
            except ValueError:
                arguments = {}
            commands = arguments.get("commands") if isinstance(arguments, dict) else None
            if name == "xiaoqing_command" and isinstance(commands, list):
                action = "；".join(_short(item, 120) for item in commands if isinstance(item, str))
            else:
                action = f"{name} {_short(json.dumps(arguments, ensure_ascii=False), 100)}"
            outcome = results.get(str(call.get("id")), "").strip().splitlines()
            lines.append(
                f"- {action} → {_short(outcome[0] if outcome else '（没有回报）', _MAX_RESULT_CHARS)}"
            )
    return lines[-_MAX_STEPS:]


def task_event(
    task: AgentTask,
    updates: Sequence[str],
    messages: Sequence[dict[str, Any]],
    outcome: Outcome | None,
) -> dict[str, Any]:
    status = task.status
    rows   = [f"原话：{task.request_text}"]
    rows.extend(f"补充：{text}" for text in updates)
    calls = _calls(messages)
    if calls:
        rows.append("执行：")
        rows.extend(calls)
    if outcome is not None and outcome.text:
        rows.append(f"回复：{_short(outcome.text, _MAX_REPLY_CHARS)}")
    rows.append(f"结局：{_STATUS_WORDS.get(status, status)}")
    summary = _short(f"{task.request_text}（{_STATUS_WORDS.get(status, status)}）", 80)
    return {
        "kind"  : "task",
        "ts"    : float(task.finished_at or task.created_at),
        "actor" : {"role": "user", "user_id": task.chat.user_id, "name": task.sender},
        "origin": "tool",
        "text"  : "\n".join(rows)[:_MAX_TEXT_CHARS],
        "refs"  : {"task_id": task.task_id, "status": status, "summary": summary},
        "key"   : f"task:{task.task_id}",
    }


async def record_task(
    context: Any,
    task: AgentTask,
    updates: Sequence[str],
    messages: Sequence[dict[str, Any]],
    outcome: Outcome | None,
) -> None:
    service = _service(context)
    if service is None:
        return
    payload = {"scope": task.chat.key, "events": [task_event(task, updates, messages, outcome)]}
    try:
        await asyncio.wait_for(service.record(payload), timeout=RECORD_TIMEOUT)
    except Exception as exc:
        log.warning(LogEvent("memory.record.skipped", error=exc))
