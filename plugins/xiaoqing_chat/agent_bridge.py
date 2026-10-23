"""把明确叫小青的一轮先交给 xiaoqing_agent 判断。

agent 能用哪些工具由发起人自己的权限决定。能力缺失、agent 关闭、判断为闲聊或
调用失败时都返回 ``None``，本轮按原有聊天流程继续，行为与未安装 agent 时完全一致。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from core.delivery import DeliveryReceipt, DeliverySegments
from core.interfaces import COMMAND_RUNNER_EVENT_KEY
from core.message import extract_text, sender_display_name

from .chat_event import event_sender_name
from .chat_runtime import chat_lock
from .handler_context import HandlerContext
from .logging_utils import log_step
from .memory.local_ids import allocate_local_id
from .message_parts import build_text_message_parts
from .planning.action_history import ActionRecord
from .smalltalk_models import PreparedSmalltalkTurn
from .task_scheduler import schedule_action_history_flush, schedule_memory_persist
from .turn_generation import ensure_user_message_recorded

AGENT_PLUGIN = "xiaoqing_agent"
# agent 交付的结果就是小青说的话：写进聊天记录时太长就截短，不像其他插件的长输出那样整条跳过。
MAX_AGENT_MEMORY_CHARS = 1000
# agent 自己会把判断和当轮等待控制在这之内；这里再兜一层，防止拖住整条会话。
_AGENT_DECIDE_TIMEOUT_SECONDS = 25.0
_AGENT_TURN_TIMEOUT_SECONDS   = 50.0
_HISTORY_ITEMS                = 12
_QUOTE_TIMEOUT_SECONDS        = 5.0
_MAX_QUOTED_CHARS             = 2000


def _image_urls(message: Any) -> list[str]:
    if not isinstance(message, list):
        return []
    urls: list[str] = []
    for segment in message:
        if not isinstance(segment, dict) or segment.get("type") != "image":
            continue
        raw  = segment.get("data")
        data = raw if isinstance(raw, dict) else {}
        url  = str(data.get("url") or data.get("file") or "")
        if url.startswith(("http://", "https://")):
            urls.append(url)
    return urls


def _reply_message_id(event: dict[str, Any]) -> str:
    message = event.get("message")
    for segment in message if isinstance(message, list) else ():
        if isinstance(segment, dict) and segment.get("type") == "reply":
            raw = segment.get("data")
            return str((raw if isinstance(raw, dict) else {}).get("id") or "").strip()
    return ""


async def _quoted_message(event: dict[str, Any], context: Any) -> tuple[str, list[str]]:
    """取回被引用消息的文字与图片；“把这张图……”这类要求常常指向引用的消息。"""

    message_id = _reply_message_id(event)
    media      = getattr(getattr(context, "capabilities", None), "onebot_media", None)
    if not message_id or media is None:
        return "", []
    try:
        data = await asyncio.wait_for(media.get_message(message_id), _QUOTE_TIMEOUT_SECONDS)
    except Exception:
        return "", []
    message = data.get("message")
    name    = sender_display_name(data.get("sender"))
    text    = extract_text(message).strip()[:_MAX_QUOTED_CHARS]
    if text and name:
        text = f"{name}: {text}"
    return text, _image_urls(message)


def _image_files(event: dict[str, Any]) -> list[str]:
    """xiaoqing_chat 已经下载并校验过的本轮图片，agent 可以直接打开本地文件。"""

    items = event.get("_xc_rendered_media_items")
    paths = [getattr(item, "cached_path", None) for item in items or ()]
    return [str(path) for path in paths if path is not None and path.is_file()]


async def _recent_history(hctx: HandlerContext, event: dict[str, Any]) -> list[dict[str, Any]]:
    current = event.get("message_id")
    history = await hctx.state.memory_store.get_recent_async(
        hctx.chat_id,
        max_items=_HISTORY_ITEMS + 1,
    )
    # 带上说话人的 QQ 号：agent 据此分清发起人本人和群里其他人。
    return [
        {"role": item.role, "name": item.name, "user_id": item.user_id, "text": item.content}
        for item in history
        if not (item.role == "user" and current is not None and item.message_id == current)
    ][-_HISTORY_ITEMS:]


async def _record_agent_request(
    text: str,
    task_id: str,
    event: dict[str, Any],
    context: Any,
    hctx: HandlerContext,
) -> None:
    """agent 接手的一轮同样写进聊天记录，之后的闲聊能接上这件事。"""

    runtime, state, chat_id = hctx.runtime, hctx.state, hctx.chat_id
    async with chat_lock(chat_id):
        user_local_id = await ensure_user_message_recorded(
            text,
            event,
            context,
            runtime,
            state=state,
        )
        state.action_history.append(
            chat_id,
            ActionRecord(
                ts           = time.time(),
                local_target = user_local_id,
                action       = "agent_task",
                reasoning    = "handled_by=xiaoqing_agent",
                detail       = {"task_id": task_id},
                executed     = True,
            ),
        )
        schedule_action_history_flush(context, runtime, chat_id=chat_id)


async def _record_agent_reply(
    reply_text: str,
    context: Any,
    hctx: HandlerContext,
    message_ids: tuple[Any, ...],
) -> None:
    """回复确认发出后才记成小青的发言，连同发出的全部消息 ID 一起写入；
    群友引用其中任何一条接着说时，都能认出是在跟小青说。
    """

    runtime, state, chat_id = hctx.runtime, hctx.state, hctx.chat_id
    async with chat_lock(chat_id):
        if reply_text:
            state.memory_store.append(
                chat_id,
                role          = "assistant",
                name          = hctx.bot_name,
                local_id      = allocate_local_id(chat_id),
                parts         = build_text_message_parts(reply_text[:MAX_AGENT_MEMORY_CHARS]),
                source_plugin = AGENT_PLUGIN,
                message_ids   = message_ids,
            )
            schedule_memory_persist(context, runtime, chat_id=chat_id)
        state.set_last_reply_ts(chat_id, time.time())


async def _report_delivery(context: Any, task_id: str, delivered: bool | None) -> None:
    """把当轮回复发没发出去告诉 agent，任务记录据此标出没送达的结果。"""

    agent = getattr(getattr(context, "capabilities", None), "agent_turn", None)
    if agent is None or not task_id:
        return
    try:
        await agent.report_delivery(task_id, delivered)
    except Exception as exc:
        context.logger.info(
            "xiaoqing_chat agent delivery report failed error_type=%s",
            type(exc).__name__,
        )


def _agent_reply_receipt(
    reply_text: str,
    task_id: str,
    context: Any,
    hctx: HandlerContext,
) -> DeliveryReceipt:
    async def commit() -> None:
        await _record_agent_reply(reply_text, context, hctx, receipt.message_ids)
        await _report_delivery(context, task_id, True)

    async def rollback() -> None:
        log_step(
            context,
            hctx.runtime,
            chat_id = hctx.chat_id,
            step    = "smalltalk.agent.delivery_failed",
            fields  = {"reply_chars": len(reply_text)},
        )
        await _report_delivery(context, task_id, False)

    async def unknown() -> None:
        # 回执丢失时按已发出记进聊天记录：对方可能已经看到了这条回复。
        await _record_agent_reply(reply_text, context, hctx, receipt.message_ids)
        await _report_delivery(context, task_id, None)

    receipt = DeliveryReceipt(expected_actions=1, commit=commit, rollback=rollback, unknown=unknown)
    return receipt


def agent_may_take_over(
    prepared: PreparedSmalltalkTurn, event: dict[str, Any], context: Any
) -> bool:
    """这一轮要不要先问 agent：明确叫小青或私聊、且不是插件替人执行的命令。"""

    agent = getattr(getattr(context, "capabilities", None), "agent_turn", None)
    # 私聊里每句话都是对角色说的；开启私聊脑内对话时私聊不算强制召唤，但同样要交给 agent。
    return (
        agent is not None
        and bool(prepared.forced or prepared.is_private)
        and not event.get(COMMAND_RUNNER_EVENT_KEY)
    )


@dataclass(frozen=True)
class _AgentPlan:
    """agent 判断要接手的一轮：交给它的材料、它的判断，以及开始判断的时刻。"""

    turn: dict[str, Any]
    decision: Any
    started: float


def _log_agent_step(context: Any, hctx: HandlerContext, step: str, **fields: Any) -> None:
    log_step(context, hctx.runtime, chat_id=hctx.chat_id, step=step, fields=fields)


def _fall_back_to_chat(context: Any, exc: Exception) -> None:
    """agent 这一路任何一步出错都回到普通聊天，只记下错误类型。"""

    context.logger.info(
        "xiaoqing_chat agent bridge fell back to chat error_type=%s",
        type(exc).__name__,
    )


async def _decide(
    prepared: PreparedSmalltalkTurn,
    event: dict[str, Any],
    context: Any,
    hctx: HandlerContext,
) -> _AgentPlan | None:
    """只让 agent 判断这一轮要不要接手（通常 1–2 秒）；不接手或出错时返回 ``None``。"""

    if not agent_may_take_over(prepared, event, context):
        return None
    agent   = context.capabilities.agent_turn
    started = time.monotonic()
    try:
        quoted_text, quoted_images = await _quoted_message(event, context)
        turn = {
            "text": prepared.text,
            "user_id": event.get("user_id"),
            "group_id": event.get("group_id"),
            "message_id": event.get("message_id"),
            "is_private": prepared.is_private,
            "bot_name": hctx.bot_name,
            "sender_name": event_sender_name(event),
            "history": await _recent_history(hctx, event),
            "images": [*_image_urls(event.get("message")), *quoted_images],
            "image_files": _image_files(event),
            "quoted_text": quoted_text,
            # 人设只写在人设卡里：agent 拿人物事实和〔办事〕各节，和平时聊天是同一个人。
            "persona": hctx.runtime.cfg.personality.card.for_agent(hctx.bot_name),
        }
        result = await asyncio.wait_for(
            agent.handle_turn({**turn, "phase": "decide"}),
            timeout=_AGENT_DECIDE_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        _fall_back_to_chat(context, exc)
        return None
    elapsed_ms = round((time.monotonic() - started) * 1000)
    if not isinstance(result, dict) or not result.get("handled"):
        _log_agent_step(context, hctx, "smalltalk.agent.declined", elapsed_ms=elapsed_ms)
        return None
    _log_agent_step(context, hctx, "smalltalk.agent.decided", elapsed_ms=elapsed_ms)
    return _AgentPlan(turn, result.get("decision"), started)


async def _act(
    plan: _AgentPlan,
    prepared: PreparedSmalltalkTurn,
    event: dict[str, Any],
    context: Any,
    hctx: HandlerContext,
) -> list[dict[str, Any]] | None:
    """让 agent 按判断执行并取回本轮回复；失败时返回 ``None`` 回到普通聊天。"""

    try:
        result = await asyncio.wait_for(
            context.capabilities.agent_turn.handle_turn(
                {**plan.turn, "phase": "act", "decision": plan.decision}
            ),
            timeout=_AGENT_TURN_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        _fall_back_to_chat(context, exc)
        return None
    message = result.get("segments") if isinstance(result, dict) else None
    if not isinstance(result, dict) or not result.get("handled") or not isinstance(message, list):
        return None
    if not message:
        return None
    reply_text = str(result.get("reply_text") or "")
    task_id    = str(result.get("task_id") or "")
    await _record_agent_request(prepared.text, task_id, event, context, hctx)
    _log_agent_step(
        context,
        hctx,
        "smalltalk.agent.handled",
        task_id     = task_id,
        reply_chars = len(reply_text),
        elapsed_ms  = round((time.monotonic() - plan.started) * 1000),
    )
    return DeliverySegments(message, _agent_reply_receipt(reply_text, task_id, context, hctx))


async def _discard(task: asyncio.Task[Any]) -> None:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def answer_with_agent_or_chat(
    prepared: PreparedSmalltalkTurn,
    event: dict[str, Any],
    context: Any,
    hctx: HandlerContext,
    chat: Callable[[asyncio.Task[Any] | None], Coroutine[Any, Any, list[dict[str, Any]]]],
) -> list[dict[str, Any]]:
    """agent 判断和聊天生成同时开始，闲聊不用先等判断。

    判断为闲聊时直接用已经在生成的回复；agent 接手时立刻放弃这次生成（让出会话的生成
    名额），聊天回复不会发出。``chat`` 收到判断任务：生成比判断先完成时，要等判断出来
    再决定发不发。
    """

    deciding = asyncio.create_task(_decide(prepared, event, context, hctx))
    chatting: asyncio.Task[list[dict[str, Any]]] = asyncio.create_task(chat(deciding))
    try:
        plan = await asyncio.shield(deciding)
    except BaseException:
        await _discard(deciding)
        await _discard(chatting)
        raise
    if plan is None:
        return await chatting
    await _discard(chatting)
    reply = await _act(plan, prepared, event, context, hctx)
    if reply is not None:
        return reply
    # 判断要接手、执行时却出了问题：这一轮照常聊天。
    return await chat(None)
