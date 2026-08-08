"""编排 PFC 会话准备、动作规划、等待、重想和状态提交。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from ..config.config import XiaoQingChatConfig
from ..logging_utils import _log_step
from ..memory.memory import MemoryStore, StoredMessage, active_conversation_suffix
from ..memory.memory_db import MemoryDB
from ..memory.memory_retrieval import build_memory_block
from .action_history import ActionHistoryStore
from .pfc_action_planner import PFCPlan, decide_say_bye, plan_next_action
from .pfc_goal_analyzer import analyze_goals
from .pfc_state import PFCConversationState, PFCStateStore


@dataclass(frozen=True)
class PFCRunResult:
    """单次 PFC（基于上下文规划）执行结果。"""

    reply: str
    action: str
    reason: str
    ended: bool
    wait_seconds: float = 0.0


GenerateReplyFn = Callable[[str, str, str], Awaitable[str]]


def _build_goal_focus_context(goal_list: Sequence[dict[str, Any]]) -> str:
    if not goal_list:
        return ""
    first = goal_list[0] if isinstance(goal_list[0], dict) else {}
    goal = str(first.get("goal", "") or "").strip()
    focus = str(first.get("focus", "") or "").strip()
    lines: list[str] = []
    if goal:
        lines.append(f"目标: {goal}")
    if focus:
        lines.append(f"焦点: {focus}")
    return "\n".join(lines).strip()


async def _action_history_summary_async(store: ActionHistoryStore, chat_id: str) -> tuple[str, str]:
    recent = await store.get_recent_async(chat_id, max_items=10)
    if not recent:
        return "", ""
    lines = []
    for r in recent[-8:]:
        ts = time.strftime("%H:%M:%S", time.localtime(r.ts))
        tgt = r.local_target or "-"
        ok = "ok" if r.executed else "fail"
        lines.append(f"- {ts} {r.action} {tgt} {ok} {r.reasoning}".strip())
    summary = "\n".join(lines).strip()
    last = recent[-1]
    last_ctx = f"action={last.action}\nreason={last.reasoning}\nexecuted={last.executed}\ndetail={last.detail}"
    return summary, last_ctx


def _timeout_context(history: Sequence[StoredMessage], *, minutes: int = 6) -> str:
    now = time.time()
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
        if msg.role == "assistant":
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


_ACTION_ALIASES: dict[str, str] = {
    "reply": "direct_reply",
    "directreply": "direct_reply",
    "direct_reply": "direct_reply",
    "send_new": "send_new_message",
    "send_newmessage": "send_new_message",
    "new_message": "send_new_message",
    "send_new_message": "send_new_message",
    "end": "end_conversation",
    "end_conversation": "end_conversation",
    "block": "block_and_ignore",
    "ignore": "block_and_ignore",
    "block_and_ignore": "block_and_ignore",
    "wait": "wait",
    "listening": "listening",
    "rethink": "rethink_goal",
    "rethink_goal": "rethink_goal",
    "fetch": "fetch_knowledge",
    "fetch_knowledge": "fetch_knowledge",
}


def _normalize_action(action: str) -> str:
    return _ACTION_ALIASES.get((action or "").strip().lower(), "direct_reply")


def _wait_run_result(plan: PFCPlan, action: str) -> PFCRunResult:
    wait_seconds = float(plan.wait_seconds if plan.wait_seconds > 0 else 0)
    reason = plan.reason
    if plan.thinking:
        reason = f"[thinking: {plan.thinking[:120]}] {plan.reason}"
    return PFCRunResult(
        reply="",
        action=action,
        reason=reason,
        ended=False,
        wait_seconds=wait_seconds,
    )


@dataclass
class _PFCSession:
    context: Any
    runtime_cfg: XiaoQingChatConfig
    secrets: dict[str, Any]
    bot_name: str
    is_private: bool
    chat_id: str
    current_text: str
    memory_db: MemoryDB
    state: PFCConversationState
    generate_reply: GenerateReplyFn
    history: Sequence[StoredMessage]
    action_summary: str
    last_action_context: str
    timeout_context: str
    recent_reply_action: str
    planner_timeout: float
    now: float
    dirty: bool = False
    planner_goal_context: str = ""


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
    memory_db: MemoryDB,
    state: PFCConversationState,
    generate_reply: GenerateReplyFn,
    now: float,
    dirty: bool,
) -> _PFCSession:
    history = await memory_store.get_recent_async(
        chat_id,
        max_items=max(60, int(runtime_cfg.max_context_size) * 3),
    )
    history = active_conversation_suffix(
        history,
        idle_gap_seconds=float(
            getattr(runtime_cfg.memory, "conversation_idle_gap_seconds", 1800.0) or 0.0
        ),
    )
    followup_action_window = runtime_cfg.pfc_followup_action_window_seconds
    recent_reply_action = _recent_successful_reply_action(
        history,
        state.last_successful_reply_action or "",
        now=now,
        window_seconds=followup_action_window,
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
        dirty = True
    planner_timeout = min(runtime_cfg.pfc_planner_timeout_seconds, runtime_cfg.timeout_seconds)
    return _PFCSession(
        context=context,
        runtime_cfg=runtime_cfg,
        secrets=secrets,
        bot_name=bot_name,
        is_private=is_private,
        chat_id=chat_id,
        current_text=current_text,
        memory_db=memory_db,
        state=state,
        generate_reply=generate_reply,
        history=history,
        action_summary=action_summary,
        last_action_context=last_action_context,
        timeout_context=timeout_context,
        recent_reply_action=recent_reply_action,
        planner_timeout=planner_timeout,
        now=now,
        dirty=dirty,
    )


def _planner_kwargs(session: _PFCSession) -> dict[str, Any]:
    return {
        "secrets": session.secrets,
        "bot_name": session.bot_name,
        "is_private": session.is_private,
        "personality": session.runtime_cfg.personality,
        "history": session.history,
        "action_history_summary": session.action_summary,
        "last_action_context": session.last_action_context,
        "timeout_context": session.timeout_context,
        "last_successful_reply_action": session.recent_reply_action,
        "current_text": session.current_text,
        "temperature": session.runtime_cfg.temperature,
        "top_p": session.runtime_cfg.top_p,
        "max_tokens": session.runtime_cfg.max_tokens,
        "timeout_seconds": session.planner_timeout,
        "max_retry": 0,
        "retry_interval_seconds": 0.2,
    }


async def _plan_pfc_action(session: _PFCSession) -> PFCPlan:
    started_at = time.monotonic()
    _log_step(
        session.context,
        session.runtime_cfg,
        chat_id=session.chat_id,
        step="pfc.plan.start",
        fields={"history_items": len(session.history)},
    )
    plan = await plan_next_action(
        goal_list=session.state.goal_list,
        knowledge_list=session.state.knowledge_list,
        **_planner_kwargs(session),
    )
    _log_step(
        session.context,
        session.runtime_cfg,
        chat_id=session.chat_id,
        step="pfc.plan.done",
        fields={
            "elapsed_s": round(time.monotonic() - started_at, 3),
            "action": plan.action,
            "reason": plan.reason,
            "thinking": plan.thinking[:100] if plan.thinking else "",
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
        window_seconds = session.runtime_cfg.pfc_planner_fail_window_seconds
        threshold = session.runtime_cfg.pfc_planner_fail_threshold
        backoff_seconds = session.runtime_cfg.pfc_planner_backoff_seconds
        failures = [
            value for value in state.planner_fail_ts if session.now - float(value) < window_seconds
        ]
        failures.append(session.now)
        state.planner_fail_ts = failures
        if len(failures) >= max(1, threshold):
            state.planner_skip_until = session.now + max(0.0, backoff_seconds)
            _log_step(
                session.context,
                session.runtime_cfg,
                chat_id=session.chat_id,
                step="pfc.plan.backoff",
                fields={
                    "fails": len(failures),
                    "window_s": window_seconds,
                    "backoff_s": backoff_seconds,
                },
            )
        session.dirty = True
    elif state.planner_fail_ts or state.planner_skip_until:
        state.planner_fail_ts = []
        state.planner_skip_until = 0.0
        session.dirty = True


def _passive_pfc_result(
    session: _PFCSession,
    plan: PFCPlan,
    action: str,
    *,
    allow_block: bool,
    after: str = "",
) -> PFCRunResult | None:
    if allow_block and action == "block_and_ignore":
        _log_step(
            session.context,
            session.runtime_cfg,
            chat_id=session.chat_id,
            step="pfc.block",
            fields={"reason": plan.reason},
        )
        return PFCRunResult(
            reply="",
            action="wait",
            reason=f"current_message_blocked: {plan.reason}",
            ended=False,
        )
    if action not in ("wait", "listening"):
        return None
    fields: dict[str, Any] = {
        "action": action,
        "reason": plan.reason,
        "wait_seconds": plan.wait_seconds if plan.wait_seconds > 0 else 0,
    }
    if after:
        fields["after"] = after
    _log_step(
        session.context,
        session.runtime_cfg,
        chat_id=session.chat_id,
        step="pfc.no_reply",
        fields=fields,
    )
    return _wait_run_result(plan, action)


async def _rethink_pfc_goal(session: _PFCSession) -> PFCPlan:
    _log_step(
        session.context,
        session.runtime_cfg,
        chat_id=session.chat_id,
        step="pfc.rethink_goal.start",
        fields={},
    )
    session.state.goal_list = await analyze_goals(
        secrets=session.secrets,
        bot_name=session.bot_name,
        personality=session.runtime_cfg.personality,
        history=session.history,
        current_goal_list=session.state.goal_list,
        action_history_text=session.action_summary,
        temperature=session.runtime_cfg.temperature,
        top_p=session.runtime_cfg.top_p,
        max_tokens=session.runtime_cfg.max_tokens,
        timeout_seconds=session.planner_timeout,
        max_retry=0,
        retry_interval_seconds=0.2,
    )
    session.dirty = True
    _log_step(
        session.context,
        session.runtime_cfg,
        chat_id=session.chat_id,
        step="pfc.rethink_goal.done",
        fields={"goals": len(session.state.goal_list or [])},
    )
    session.planner_goal_context = _build_goal_focus_context(session.state.goal_list or [])
    return await _plan_pfc_action(session)


async def _fetch_pfc_knowledge(session: _PFCSession, plan: PFCPlan) -> PFCPlan:
    _log_step(
        session.context,
        session.runtime_cfg,
        chat_id=session.chat_id,
        step="pfc.fetch_knowledge.start",
        fields={},
    )
    await asyncio.to_thread(session.memory_db.bind, session.context.data_dir)
    memory = await build_memory_block(
        data_dir=session.context.data_dir,
        chat_id=session.chat_id,
        secrets=session.secrets,
        cfg=session.runtime_cfg.memory,
        bot_name=session.bot_name,
        history=session.history,
        current_text=session.current_text,
        planner_question=plan.reason or session.current_text,
        memory_db=session.memory_db,
        temperature=session.runtime_cfg.temperature,
        top_p=session.runtime_cfg.top_p,
        max_tokens=session.runtime_cfg.max_tokens,
        timeout_seconds=session.planner_timeout,
    )
    memory = (memory or "").strip()
    if memory:
        session.state.knowledge_list.append({"text": memory, "ts": time.time()})
        session.state.knowledge_list = session.state.knowledge_list[-10:]
        session.dirty = True
    _log_step(
        session.context,
        session.runtime_cfg,
        chat_id=session.chat_id,
        step="pfc.fetch_knowledge.done",
        fields={
            "mem_chars": len(memory),
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
            secrets=session.secrets,
            bot_name=session.bot_name,
            is_private=session.is_private,
            personality=session.runtime_cfg.personality,
            history=session.history,
            temperature=session.runtime_cfg.temperature,
            top_p=session.runtime_cfg.top_p,
            max_tokens=session.runtime_cfg.max_tokens,
            timeout_seconds=session.planner_timeout,
            max_retry=0,
            retry_interval_seconds=0.2,
        )
        session.state.ended = True
        session.dirty = True
        if not say_bye:
            return PFCRunResult(reply="", action=action, reason=plan.reason, ended=True)
        reply = await session.generate_reply("say_goodbye", plan.reason, why)
        if reply:
            session.state.last_successful_reply_action = "say_goodbye"
        return PFCRunResult(
            reply=reply,
            action="say_goodbye",
            reason=plan.reason,
            ended=True,
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
            session.dirty = True
        return PFCRunResult(
            reply=reply,
            action=action,
            reason=plan.reason,
            ended=False,
        )
    reply = await session.generate_reply("direct_reply", plan.reason, "")
    if reply:
        session.state.last_successful_reply_action = "direct_reply"
        session.dirty = True
    return PFCRunResult(
        reply=reply,
        action="direct_reply",
        reason=plan.reason,
        ended=False,
    )


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
    memory_db: MemoryDB,
    pfc_state_store: PFCStateStore,
    generate_reply: GenerateReplyFn,
    state_override: PFCConversationState | None = None,
    persist_state: bool = True,
) -> PFCRunResult:
    """执行一轮基于上下文的规划，并持久化每次状态转换。"""
    pfc_state_store.bind(context.data_dir)
    state = state_override or await pfc_state_store.get_async(chat_id)
    now = time.time()
    dirty = False
    session: _PFCSession | None = None
    if state.ignore_until_ts:
        # 旧版本允许模型控制整个会话的忽略窗口，存在安全风险，不再沿用。
        state.ignore_until_ts = 0.0
        dirty = True
    try:
        if state.ended:
            _log_step(context, runtime_cfg, chat_id=chat_id, step="pfc.ended", fields={})
            return PFCRunResult(
                reply="",
                action="end_conversation",
                reason="ended",
                ended=True,
            )
        session = await _prepare_pfc_session(
            context=context,
            runtime_cfg=runtime_cfg,
            secrets=secrets,
            bot_name=bot_name,
            is_private=is_private,
            chat_id=chat_id,
            current_text=current_text,
            memory_store=memory_store,
            action_history=action_history,
            memory_db=memory_db,
            state=state,
            generate_reply=generate_reply,
            now=now,
            dirty=dirty,
        )
        skip_until = float(state.planner_skip_until or 0.0)
        if skip_until and now < skip_until:
            _log_step(
                context,
                runtime_cfg,
                chat_id=chat_id,
                step="pfc.plan.skip",
                fields={"skip_left_s": round(skip_until - now, 2)},
            )
            if not is_private:
                return PFCRunResult(
                    reply="",
                    action="wait",
                    reason="planner_skipped",
                    ended=False,
                )
            reply = await generate_reply("direct_reply", "planner_skipped", "")
            return PFCRunResult(
                reply=reply,
                action="direct_reply",
                reason="planner_skipped",
                ended=False,
            )

        plan = await _plan_pfc_action(session)
        _record_planner_health(session, plan)
        # 规划器已经看过完整群聊上下文；后续简单规则不得推翻它的 wait 决策。
        action = _normalize_action(plan.action)
        passive_result = _passive_pfc_result(
            session,
            plan,
            action,
            allow_block=True,
        )
        if passive_result is not None:
            return passive_result

        if action == "rethink_goal":
            plan = await _rethink_pfc_goal(session)
            action = _normalize_action(plan.action)
            passive_result = _passive_pfc_result(
                session,
                plan,
                action,
                allow_block=False,
                after="rethink_goal",
            )
            if passive_result is not None:
                return passive_result

        if action == "fetch_knowledge":
            plan = await _fetch_pfc_knowledge(session, plan)
            action = _normalize_action(plan.action)
            passive_result = _passive_pfc_result(
                session,
                plan,
                action,
                allow_block=False,
                after="fetch_knowledge",
            )
            if passive_result is not None:
                return passive_result

        return await _finish_pfc_action(session, plan, action)
    finally:
        should_save = session.dirty if session is not None else dirty
        if should_save and persist_state:
            # 规划器会原地修改 ``get_async`` 返回的对象；保存前重新登记，避免并发缓存
            # 淘汰让这次写入悄悄变成未命中。
            pfc_state_store.set_state(chat_id, state)
            await pfc_state_store.save_async(chat_id)
