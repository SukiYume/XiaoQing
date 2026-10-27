"""小青 agent：有人叫小青时，判断是否需要动手，并在后台用工具把事情做完。

权限只有一套：agent 能做的就是发起人自己能做的。XiaoQing 命令以发起人身份执行，
照常按各自的权限与场景检查；执行代码、读写本机文件和 ``/shell`` 同权限，SSH、Codex
和 ``/ssh``、``/codex`` 同权限，都只给 Bot 管理员。本插件再检查 ``enabled`` 开关；
关闭或判断为普通聊天时返回 ``None``，xiaoqing_chat 照常回复。
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict
from typing import Any

from core.interfaces import PluginContextProtocol
from core.message import sender_display_name
from core.plugin_base import segments
from core.public_errors import public_error_response
from core.router import resolve_context_command_invocation

from .config import AgentConfig, load_agent_config
from .prompts import configured_bot_name, configured_persona
from .runtime import AgentRuntime
from .tasks import Actor, AgentTask, ChatRef
from .triage import TRIAGE_ACTIONS, TriageDecision, triage_turn

logger = logging.getLogger(__name__)

_RUNTIME_KEY       = "xiaoqing_agent.runtime"
_ROOT_CODE         = "xiaoqing_agent.agent"
_MAX_HISTORY       = 20
_MAX_PERSONA_CHARS = 12000
_MAX_IMAGES        = 6
_DISABLED_TIP      = "agent 没有开启：把 config.plugins.xiaoqing_agent.enabled 设为 true 后再试。"

HELP_TEXT = """🛠 小青 agent

开启后，叫小青做需要动手的事（查日程、查论文、算数、画图、跑代码……），
小青会自己调用工具做完再回复；普通聊天照旧。能做什么和你自己直接发命令一样：
跑代码、读写文件、SSH、Codex 只有 Bot 管理员能用。任务进行中可以直接补充要求，
比如“换成红色”。群里的任务大家都能看，只有发起人和 Bot 管理员能停止或补充。

/agent run <任务>        跳过判断，直接执行
/agent status [任务ID]   查看运行中的任务和计划
/agent stop [任务ID|all] 停止运行中的任务（有多个时需指明）
/agent history           本会话最近的任务
/agent result <任务ID>   重新发送某个已结束任务的结果
/agent help              显示本帮助"""


def get_runtime(context: Any) -> AgentRuntime:
    state   = context.state
    runtime = state.get(_RUNTIME_KEY)
    if not isinstance(runtime, AgentRuntime):
        runtime             = AgentRuntime(context.data_dir)
        state[_RUNTIME_KEY] = runtime
    return runtime


async def init(context: PluginContextProtocol) -> None:
    runtime     = get_runtime(context)
    interrupted = await runtime.store.mark_interrupted()
    if interrupted:
        logger.info("xiaoqing_agent marked %d unfinished task(s) as interrupted", interrupted)


async def shutdown(context: PluginContextProtocol) -> None:
    runtime = context.state.get(_RUNTIME_KEY)
    if isinstance(runtime, AgentRuntime):
        await runtime.shutdown()


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _chat_from(context: Any, user_id: Any = None, group_id: Any = None) -> ChatRef | None:
    user  = _positive_int(user_id) or _positive_int(getattr(context, "current_user_id", None))
    group = _positive_int(group_id) or _positive_int(getattr(context, "current_group_id", None))
    return ChatRef(user, group) if user is not None else None


def _string_list(value: Any, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item.strip()][:limit]


def _history(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)][-_MAX_HISTORY:]


def _stopped_text(stopped: list[AgentTask] | str, *, empty: str) -> str:
    if isinstance(stopped, str):
        return stopped
    if not stopped:
        return empty
    return "已停止: " + ", ".join(task.task_id for task in stopped)


def _actor(context: Any, chat: ChatRef) -> Actor:
    """这一轮说话的人；Bot 管理员身份由 Core 按当前配置签发。"""

    return Actor(chat.user_id, admin=bool(getattr(context.capabilities, "is_bot_admin", False)))


async def _start_and_wait(
    context: Any,
    config: AgentConfig,
    runtime: AgentRuntime,
    chat: ChatRef,
    *,
    goal: str,
    request_text: str,
    source: str,
    sender: str                          = "",
    history: list[dict[str, Any]] | None = None,
    images: list[str] | None             = None,
    image_files: list[str] | None        = None,
    quoted_text: str                     = "",
    persona: str                         = "",
) -> tuple[list[dict[str, Any]], AgentTask | None]:
    """启动并等待当轮结果；后台任务由执行阶段发送实际进度和最终结果。"""

    started = runtime.start(
        context,
        config,
        chat,
        goal         = goal,
        request_text = request_text,
        source       = source,
        sender       = sender,
        admin        = _actor(context, chat).admin,
        history      = history or [],
        images       = images or [],
        image_files  = image_files or [],
        quoted_text  = quoted_text,
        persona      = persona or configured_persona(context),
    )
    if isinstance(started, str):
        return segments(started), None
    outcome = await runtime.wait_inline(context, started, config.inline_wait_seconds)
    if outcome is not None:
        return await runtime.inline_reply(context, started, outcome), started
    return [], started


def _reply_text(message: list[dict[str, Any]]) -> str:
    return "".join(
        str(segment.get("data", {}).get("text", ""))
        for segment in message
        if isinstance(segment, dict) and segment.get("type") == "text"
    ).strip()


# ──────────────────── xiaoqing_chat 服务入口 ────────────────────


def _decision_from(raw: Any) -> TriageDecision | None:
    """``act`` 阶段带回 ``decide`` 给出的判断；字段不对就当没有判断。"""

    if not isinstance(raw, dict):
        return None
    action = str(raw.get("action") or "")
    if action not in TRIAGE_ACTIONS or action == "conversation":
        return None
    return TriageDecision(
        action,
        task_id=str(raw.get("task_id") or "")[:32],
    )


async def handle_turn(
    turn: dict[str, Any], context: PluginContextProtocol
) -> dict[str, Any] | None:
    """xiaoqing_chat 交来的一轮叫小青的消息；返回 ``None`` 表示交回去照常聊天。

    ``phase`` 把一轮拆成两步，聊天侧可以一边判断一边生成闲聊回复：``decide`` 只做判断，
    要接手时返回判断结果；``act`` 带着这个结果去执行。不带 ``phase`` 时一次做完。
    任何内部失败都退回普通聊天，不能让 agent 的问题打断小青原本的回复。
    """

    try:
        config = load_agent_config(context)
        if not config.enabled:
            return None
        text = str(turn.get("text") or "").strip()
        chat = _chat_from(context, turn.get("user_id"), turn.get("group_id"))
        if not text or chat is None:
            return None
        runtime = get_runtime(context)
        if not runtime.accepting:
            return None
        persona  = turn.get("persona")
        persona  = persona.strip()[:_MAX_PERSONA_CHARS] if isinstance(persona, str) else ""
        phase    = str(turn.get("phase") or "")
        history  = _history(turn.get("history"))
        decision = _decision_from(turn.get("decision")) if phase == "act" else None
        if decision is None:
            if phase == "act":
                return None
            decision = await _triage(context, config, runtime, chat, turn, text, history)
            if decision.action == "conversation":
                return None
            if phase == "decide":
                return {"handled": True, "decision": asdict(decision)}
        return await _act(context, config, runtime, chat, turn, text, history, decision, persona)
    except Exception as exc:
        logger.warning("xiaoqing_agent turn fell back to chat error_type=%s", type(exc).__name__)
        return None


async def _triage(
    context: Any,
    config: AgentConfig,
    runtime: AgentRuntime,
    chat: ChatRef,
    turn: dict[str, Any],
    text: str,
    history: list[dict[str, Any]],
) -> TriageDecision:
    running  = runtime.running_in(chat)
    started  = time.perf_counter()
    decision = await triage_turn(
        context,
        bot_name = str(turn.get("bot_name") or "") or configured_bot_name(context),
        text     = text,
        history  = history,
        running  = running,
        recent   = await runtime.recent_records(chat, config.history_tasks),
        quoted   = str(turn.get("quoted_text") or ""),
        images   = len(_string_list(turn.get("images"), _MAX_IMAGES))
        + len(_string_list(turn.get("image_files"), _MAX_IMAGES)),
        initiator=chat.user_id,
    )
    logger.info(
        "xiaoqing_agent triage chat=%s action=%s running=%d elapsed=%.2fs",
        chat.key,
        decision.action,
        len(running),
        time.perf_counter() - started,
    )
    return decision


async def _act(
    context: Any,
    config: AgentConfig,
    runtime: AgentRuntime,
    chat: ChatRef,
    turn: dict[str, Any],
    text: str,
    history: list[dict[str, Any]],
    decision: TriageDecision,
    persona: str,
) -> dict[str, Any]:
    actor  = _actor(context, chat)
    sender = str(turn.get("sender_name") or "")
    if decision.action == "status":
        message = segments(runtime.status_text(chat, decision.task_id, actor))
        return {"handled": True, "segments": message, "reply_text": _reply_text(message)}
    if decision.action == "cancel":
        stopped = await runtime.stop(chat, decision.task_id, actor)
        message = segments(
            "好，停下了。"
            if isinstance(stopped, list) and stopped
            else _stopped_text(stopped, empty="现在没有在跑的任务。"),
        )
        return {"handled": True, "segments": message, "reply_text": _reply_text(message)}
    if decision.action == "update":
        message = segments(runtime.add_note(chat, decision.task_id, text[:2000], actor, sender))
        return {"handled": True, "segments": message, "reply_text": _reply_text(message)}

    message, task = await _start_and_wait(
        context,
        config,
        runtime,
        chat,
        goal         = text[:2000],
        request_text = text,
        source       = "chat",
        sender       = sender,
        history      = history,
        images       = _string_list(turn.get("images"), _MAX_IMAGES),
        image_files  = _string_list(turn.get("image_files"), _MAX_IMAGES),
        quoted_text  = str(turn.get("quoted_text") or ""),
        persona      = persona,
    )
    return {
        "handled": True,
        "segments": message,
        "reply_text": _reply_text(message),
        "task_id": task.task_id if task is not None else "",
    }


async def report_delivery(report: dict[str, Any], context: PluginContextProtocol) -> None:
    """xiaoqing_chat 回报当轮回复是否发出；只改交付状态，结果没发出时历史里会标出来。"""

    runtime   = context.state.get(_RUNTIME_KEY)
    delivered = report.get("delivered")
    if isinstance(runtime, AgentRuntime) and (delivered is None or isinstance(delivered, bool)):
        await runtime.report_delivery(str(report.get("task_id") or ""), delivered)


# ──────────────────── /agent 命令 ────────────────────


def _route(args: str, context: Any) -> tuple[str, str]:
    invocation = resolve_context_command_invocation(context, _ROOT_CODE, args)
    if invocation is not None and len(invocation.chain) > 1:
        return invocation.chain[1].name, invocation.remainder_after(1).strip()
    parts = args.strip().split(maxsplit=1)
    return (parts[0].casefold() if parts else "help"), (parts[1].strip() if len(parts) > 1 else "")


async def handle(
    command: str,
    args: str,
    event: dict[str, Any],
    context: PluginContextProtocol,
) -> list[dict[str, Any]]:
    del command
    try:
        chat = _chat_from(context, event.get("user_id"), event.get("group_id"))
        if chat is None:
            return segments("无法确定当前会话。")
        runtime = get_runtime(context)
        action, rest = _route(str(args or ""), context)
        if action == "help":
            return segments(HELP_TEXT)
        if action == "status":
            if len(rest.split()) > 1:
                return segments("用法: /agent status [任务ID]")
            return segments(runtime.status_text(chat, rest, _actor(context, chat)))
        if action == "stop":
            if len(rest.split()) > 1:
                return segments("用法: /agent stop [任务ID|all]")
            stopped = await runtime.stop(chat, rest, _actor(context, chat))
            return segments(_stopped_text(stopped, empty="没有可以停止的任务。"))
        if action == "history":
            if rest:
                return segments("用法: /agent history")
            return segments(await runtime.history_text(chat))
        if action == "result":
            if not rest or len(rest.split()) > 1:
                return segments("用法: /agent result <任务ID>")
            result = await runtime.result_message(context, chat, rest)
            return segments(result) if isinstance(result, str) else result
        if action == "run":
            if not rest:
                return segments("用法: /agent run <任务>")
            config = load_agent_config(context)
            if not config.enabled:
                return segments(_DISABLED_TIP)
            message, task = await _start_and_wait(
                context,
                config,
                runtime,
                chat,
                goal         = rest,
                request_text = rest,
                source       = "command",
                sender       = sender_display_name(event.get("sender")),
            )
            return runtime.command_reply(task, message) if task is not None else message
        return segments(f"未知子命令: {action}\n\n{HELP_TEXT}")
    except Exception as exc:
        return public_error_response(
            context,
            exc,
            logger    = logger,
            component = "xiaoqing_agent.handle",
        )
