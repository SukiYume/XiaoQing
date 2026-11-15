"""编排 PFC 会话准备、动作规划、等待、重想和状态提交。"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from ..config.config import XiaoQingChatConfig
from ..llm.llm_config import LLMCallConfig
from ..logging_utils import log_step
from ..memory import client as memory_client
from ..memory.memory import (
    MemoryStore,
    StoredMessage,
    active_conversation_suffix,
    is_conversation_reply,
)
from ..utils.wall_clock import format_local
from .action_history import ActionHistoryStore
from .pfc_action_planner import PFCPlan, decide_say_bye, plan_next_action
from .pfc_goal_analyzer import analyze_goals
from .pfc_state import PFCConversationState


@dataclass(frozen=True)
class PFCRunResult:
    """单次 PFC（基于上下文规划）执行结果。"""

    reply       : str
    action      : str
    reason      : str
    ended       : bool
    wait_seconds: float = 0.0


GenerateReplyFn = Callable[[str, str, str], Awaitable[str]]


_PASSIVE_ACTIONS = frozenset({"wait", "listening"})


def _passive_run_line(run: Sequence[Any]) -> str:
    start = format_local("%H:%M:%S", run[0].ts)
    if len(run) == 1:
        return f"- {start} 旁听 1 条，没开口"
    end = format_local("%H:%M:%S", run[-1].ts)
    return f"- {start}~{end} 连续旁听 {len(run)} 条，没开口"


async def _action_history_summary_async(store: ActionHistoryStore, chat_id: str) -> tuple[str, str]:
    """最近行动概要。连续的旁听只合成一行条数，不再逐条附上当时不开口的理由。

    规划器每轮都会读到这份概要。逐条列出“这是群友之间的对话，我先听着”时，前几轮的
    沉默理由成了下一轮的先例，越听越不开口。
    """

    recent = await store.get_recent_async(chat_id, max_items=40)
    if not recent:
        return "", ""
    lines: list[str] = []
    run  : list[Any] = []
    for r in recent:
        if r.action in _PASSIVE_ACTIONS:
            run.append(r)
            continue
        if run:
            lines.append(_passive_run_line(run))
            run = []
        ts  = format_local("%H:%M:%S", r.ts)
        tgt = r.local_target or "-"
        ok  = "ok" if r.executed else "fail"
        lines.append(f"- {ts} {r.action} {tgt} {ok} {r.reasoning}".strip())
    if run:
        lines.append(_passive_run_line(run))
    summary = "\n".join(lines[-8:]).strip()
    last    = recent[-1]
    if last.action in _PASSIVE_ACTIONS:
        last_ctx = f"action={last.action}（已连续旁听 {len(run)} 条）"
    else:
        last_ctx = (
            f"action={last.action}\nreason={last.reasoning}\n"
            f"executed={last.executed}\ndetail={last.detail}"
        )
    return summary, last_ctx


def _timeout_context(history: Sequence[StoredMessage], *, minutes: int = 6) -> str:
    now          = time.time()
    last_user_ts = 0.0
    for msg in reversed(history[-200:]):
        if msg.role == "user":
            if msg.ts is None:
                continue
            last_user_ts = float(msg.ts)
            break
    if not last_user_ts:
        return ""
    diff = now - last_user_ts
    if diff < float(minutes) * 60.0:
        return ""
    mm = max(1, int(diff // 60))
    return f"重要提示：对方已经长时间（约{mm}分钟）没有回复你的消息了（这可能代表对方繁忙/不想回复/没注意到你的消息等情况，或在对方看来本次聊天已告一段落），请基于此情况规划下一步。\n"


def _seconds_since_last_assistant(history: Sequence[StoredMessage], *, now: float) -> float:
    for msg in reversed(history[-60:]):
        if is_conversation_reply(msg):
            return max(0.0, now - float(msg.ts or now))
    return 9999.0


def _recent_successful_reply_action(
    history: Sequence[StoredMessage],
    action: str,
    *,
    now: float,
    window_seconds: float,
) -> str:
    normalized = (action or "").strip()
    if normalized not in ("direct_reply", "send_new_message"):
        return ""
    if _seconds_since_last_assistant(history, now=now) <= max(0.0, float(window_seconds)):
        return normalized
    return ""


def _wait_run_result(plan: PFCPlan, action: str) -> PFCRunResult:
    wait_seconds = float(plan.wait_seconds if plan.wait_seconds > 0 else 0)
    reason       = plan.reason
    if plan.thinking:
        reason = f"[thinking: {plan.thinking[:120]}] {plan.reason}"
    return PFCRunResult(
        reply        = "",
        action       = action,
        reason       = reason,
        ended        = False,
        wait_seconds = wait_seconds,
    )


@dataclass
class _PFCSession:
    context            : Any
    runtime_cfg        : XiaoQingChatConfig
    secrets            : dict[str, Any]
    bot_name           : str
    is_private         : bool
    chat_id            : str
    current_text       : str
    state              : PFCConversationState
    generate_reply     : GenerateReplyFn
    history            : Sequence[StoredMessage]
    action_summary     : str
    last_action_context: str
    timeout_context    : str
    recent_reply_action: str
    now                : float

    @property
    def planner_llm(self) -> LLMCallConfig:
        """规划调用沿用 reasoning 路由的请求参数。"""
        return self.runtime_cfg.llm_config("reasoning")

    planner_goal_context: str = ""
    # 统一记忆里关于说话的人和这个群的那几行（和生成拿到的是同一次取用）。
    memory_text: str = ""


async def _prepare_pfc_session(
    *,
    context: Any,
    runtime_cfg: XiaoQingChatConfig,
    secrets: dict[str, Any],
    bot_name: str,
    is_private: bool,
    chat_id: str,
    current_text: str,
    memory_store: MemoryStore,
    action_history: ActionHistoryStore,
    state: PFCConversationState,
    generate_reply: GenerateReplyFn,
    now: float,
    memory_text: str = "",
) -> _PFCSession:
    history = await memory_store.get_recent_async(
        chat_id,
        max_items=max(60, int(runtime_cfg.max_context_size) * 3),
    )
    history = active_conversation_suffix(
        history,
        idle_gap_seconds=float(runtime_cfg.memory.conversation_idle_gap_seconds),
    )
    followup_action_window = runtime_cfg.pfc_followup_action_window_seconds
    recent_reply_action    = _recent_successful_reply_action(
        history,
        state.last_successful_reply_action or "",
        now            = now,
        window_seconds = followup_action_window,
    )
    action_summary, last_action_context = await _action_history_summary_async(
        action_history,
        chat_id,
    )
    timeout_context = _timeout_context(history)
    if (
        state.last_successful_reply_action in ("direct_reply", "send_new_message")
        and not recent_reply_action
    ):
        state.last_successful_reply_action = ""
    return _PFCSession(
        context             = context,
        runtime_cfg         = runtime_cfg,
        secrets             = secrets,
        bot_name            = bot_name,
        is_private          = is_private,
        chat_id             = chat_id,
        current_text        = current_text,
        state               = state,
        generate_reply      = generate_reply,
        history             = history,
        action_summary      = action_summary,
        last_action_context = last_action_context,
        timeout_context     = timeout_context,
        recent_reply_action = recent_reply_action,
        now                 = now,
        memory_text         = memory_text,
    )


def _planner_kwargs(session: _PFCSession) -> dict[str, Any]:
    return {
        "secrets"                     : session.secrets,
        "bot_name"                    : session.bot_name,
        "is_private"                  : session.is_private,
        "personality"                 : session.runtime_cfg.personality,
        "history"                     : session.history,
        "action_history_summary"      : session.action_summary,
        "last_action_context"         : session.last_action_context,
        "timeout_context"             : session.timeout_context,
        "last_successful_reply_action": session.recent_reply_action,
        "current_text"                : session.current_text,
        "memory_text"                 : session.memory_text,
        "llm"                         : session.planner_llm,
    }


async def _plan_pfc_action(session: _PFCSession) -> PFCPlan:
    started_at = time.monotonic()
    log_step(
        session.context,
        session.runtime_cfg,
        chat_id = session.chat_id,
        step    = "pfc.plan.start",
        fields  = {"history_items": len(session.history)},
    )
    plan = await plan_next_action(
        goal_list      = session.state.goal_list,
        knowledge_list = session.state.knowledge_list,
        **_planner_kwargs(session),
    )
    log_step(
        session.context,
        session.runtime_cfg,
        chat_id = session.chat_id,
        step    = "pfc.plan.done",
        fields  = {
            "elapsed_s"   : round(time.monotonic() - started_at, 3),
            "action"      : plan.action,
            "reason"      : plan.reason,
            "thinking"    : plan.thinking[:100] if plan.thinking else "",
            "wait_seconds": plan.wait_seconds,
        },
    )
    return plan


def _record_planner_health(session: _PFCSession, plan: PFCPlan) -> None:
    state = session.state
    if plan.reason in {
        "planner_failed",
        "planner_invalid_action",
        "planner_invalid_response",
        "planner_timeout",
    }:
        window_seconds  = session.runtime_cfg.pfc_planner_fail_window_seconds
        threshold       = session.runtime_cfg.pfc_planner_fail_threshold
        backoff_seconds = session.runtime_cfg.pfc_planner_backoff_seconds
        failures        = [
            value for value in state.planner_fail_ts if session.now - float(value) < window_seconds
        ]
        failures.append(session.now)
        state.planner_fail_ts = failures
        if len(failures) >= max(1, threshold):
            state.planner_skip_until = session.now + max(0.0, backoff_seconds)
            log_step(
                session.context,
                session.runtime_cfg,
                chat_id = session.chat_id,
                step    = "pfc.plan.backoff",
                fields  = {
                    "fails"    : len(failures),
                    "window_s" : window_seconds,
                    "backoff_s": backoff_seconds,
                },
            )
    elif state.planner_fail_ts or state.planner_skip_until:
        state.planner_fail_ts    = []
        state.planner_skip_until = 0.0


def _passive_pfc_result(
    session: _PFCSession,
    plan: PFCPlan,
    action: str,
    *,
    allow_block: bool,
    after: str = "",
) -> PFCRunResult | None:
    if allow_block and action == "block_and_ignore":
        log_step(
            session.context,
            session.runtime_cfg,
            chat_id = session.chat_id,
            step    = "pfc.block",
            fields  = {"reason": plan.reason},
        )
        return PFCRunResult(
            reply  = "",
            action = "wait",
            reason = f"current_message_blocked: {plan.reason}",
            ended  = False,
        )
    if action not in ("wait", "listening"):
        return None
    fields: dict[str, Any] = {
        "action"      : action,
        "reason"      : plan.reason,
        "wait_seconds": plan.wait_seconds if plan.wait_seconds > 0 else 0,
    }
    if after:
        fields["after"] = after
    log_step(
        session.context,
        session.runtime_cfg,
        chat_id = session.chat_id,
        step    = "pfc.no_reply",
        fields  = fields,
    )
    return _wait_run_result(plan, action)


async def _rethink_pfc_goal(session: _PFCSession) -> PFCPlan:
    log_step(
        session.context,
        session.runtime_cfg,
        chat_id = session.chat_id,
        step    = "pfc.rethink_goal.start",
        fields  = {},
    )
    session.state.goal_list = await analyze_goals(
        secrets             = session.secrets,
        bot_name            = session.bot_name,
        personality         = session.runtime_cfg.personality,
        history             = session.history,
        current_goal_list   = session.state.goal_list,
        action_history_text = session.action_summary,
        llm                 = session.planner_llm,
    )
    log_step(
        session.context,
        session.runtime_cfg,
        chat_id = session.chat_id,
        step    = "pfc.rethink_goal.done",
        fields  = {"goals": len(session.state.goal_list or [])},
    )
    top_goal                     = session.state.top_goal
    session.planner_goal_context = f"目标: {top_goal}" if top_goal else ""
    return await _plan_pfc_action(session)


async def _fetch_pfc_knowledge(session: _PFCSession, plan: PFCPlan) -> PFCPlan:
    log_step(
        session.context,
        session.runtime_cfg,
        chat_id = session.chat_id,
        step    = "pfc.fetch_knowledge.start",
        fields  = {},
    )
    items = await memory_client.search(
        session.context,
        session.chat_id,
        {
            "query"         : f"{session.current_text} {plan.reason or ''}".strip(),
            "include_events": True,
            "limit"         : 6,
        },
    )
    memory = "\n".join(f"- {item.get('text')}" for item in items if item.get("text")).strip()
    if memory:
        session.state.knowledge_list.append({"text": memory, "ts": time.time()})
        session.state.knowledge_list = session.state.knowledge_list[-10:]
    log_step(
        session.context,
        session.runtime_cfg,
        chat_id = session.chat_id,
        step    = "pfc.fetch_knowledge.done",
        fields  = {
            "mem_chars"      : len(memory),
            "knowledge_items": len(session.state.knowledge_list or []),
        },
    )
    return await _plan_pfc_action(session)


async def _finish_pfc_action(
    session: _PFCSession,
    plan: PFCPlan,
    action: str,
) -> PFCRunResult:
    if action == "end_conversation":
        say_bye, why = await decide_say_bye(
            secrets     = session.secrets,
            bot_name    = session.bot_name,
            is_private  = session.is_private,
            personality = session.runtime_cfg.personality,
            history     = session.history,
            llm         = session.planner_llm,
        )
        session.state.ended = True
        if not say_bye:
            return PFCRunResult(reply="", action=action, reason=plan.reason, ended=True)
        reply = await session.generate_reply("say_goodbye", plan.reason, why)
        if reply:
            session.state.last_successful_reply_action = "say_goodbye"
        return PFCRunResult(
            reply  = reply,
            action = "say_goodbye",
            reason = plan.reason,
            ended  = True,
        )
    if action in ("direct_reply", "send_new_message"):
        combined_reason = plan.reason
        if plan.thinking:
            combined_reason = f"[thinking: {plan.thinking[:200]}] {plan.reason}"
        reply = await session.generate_reply(
            action,
            combined_reason,
            session.planner_goal_context,
        )
        if reply:
            session.state.last_successful_reply_action = action
        return PFCRunResult(
            reply  = reply,
            action = action,
            reason = plan.reason,
            ended  = False,
        )
    return PFCRunResult(
        reply  = "",
        action = "wait",
        reason = "planner_incomplete",
        ended  = False,
    )


async def _skipped_planner_result(session: _PFCSession) -> PFCRunResult:
    """规划器连续失败后的冷却期：群聊安静等待，私聊直接回复。"""
    log_step(
        session.context,
        session.runtime_cfg,
        chat_id = session.chat_id,
        step    = "pfc.plan.skip",
        fields  = {"skip_left_s": round(float(session.state.planner_skip_until) - session.now, 2)},
    )
    if not session.is_private:
        return PFCRunResult(reply="", action="wait", reason="planner_skipped", ended=False)
    reply = await session.generate_reply("direct_reply", "", "")
    return PFCRunResult(reply=reply, action="direct_reply", reason="planner_skipped", ended=False)


def _settle(
    session: _PFCSession, plan: PFCPlan, after: str
) -> tuple[PFCPlan, str, PFCRunResult | None]:
    """重新规划或补充知识之后，再判断一次是否应保持被动。"""
    action = plan.action
    return plan, action, _passive_pfc_result(session, plan, action, allow_block=True, after=after)


async def run_pfc_once(
    *,
    context,
    runtime_cfg: XiaoQingChatConfig,
    secrets: dict[str, Any],
    bot_name: str,
    is_private: bool,
    chat_id: str,
    current_text: str,
    memory_store: MemoryStore,
    action_history: ActionHistoryStore,
    state: PFCConversationState,
    generate_reply: GenerateReplyFn,
    memory_text: str = "",
) -> PFCRunResult:
    """执行一轮基于上下文的规划。

    规划只改调用方给的这份 ``state``（这一轮开始时取的快照）；等回复确认送达后，由
    ``commit_planner_snapshot`` 把它写回存储。
    """
    now = time.time()
    if state.ended:
        log_step(context, runtime_cfg, chat_id=chat_id, step="pfc.ended", fields={})
        return PFCRunResult(
            reply  = "",
            action = "end_conversation",
            reason = "ended",
            ended  = True,
        )
    session = await _prepare_pfc_session(
        context        = context,
        runtime_cfg    = runtime_cfg,
        secrets        = secrets,
        bot_name       = bot_name,
        is_private     = is_private,
        chat_id        = chat_id,
        current_text   = current_text,
        memory_store   = memory_store,
        action_history = action_history,
        state          = state,
        generate_reply = generate_reply,
        now            = now,
        memory_text    = memory_text,
    )
    if float(state.planner_skip_until or 0.0) > now:
        return await _skipped_planner_result(session)

    plan = await _plan_pfc_action(session)
    _record_planner_health(session, plan)
    # 规划器已经看过完整群聊上下文；后续简单规则不得推翻它的 wait 决策。
    action = plan.action
    passive = _passive_pfc_result(session, plan, action, allow_block=True)
    if passive is None and action == "rethink_goal":
        plan, action, passive = _settle(session, await _rethink_pfc_goal(session), "rethink_goal")
    if passive is None and action == "fetch_knowledge":
        plan, action, passive = _settle(
            session, await _fetch_pfc_knowledge(session, plan), "fetch_knowledge"
        )
    if passive is not None:
        return passive
    return await _finish_pfc_action(session, plan, action)
