from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional, Sequence

from .action_history import ActionHistoryStore
from ..config.config import XiaoQingChatConfig
from ..logging_utils import _log_step, _short_text
from ..memory.memory import MemoryStore, StoredMessage
from ..memory.memory_db import MemoryDB
from ..memory.memory_retrieval import build_memory_block
from .pfc_action_planner import PFCPlan, decide_say_bye, plan_next_action
from .pfc_goal_analyzer import analyze_goals
from .pfc_state import PFCConversationState, PFCStateStore

@dataclass(frozen=True)
class PFCRunResult:
    """Result of a single PFC (Plan-From-Context) execution."""
    reply: str
    action: str
    reason: str
    ended: bool

GenerateReplyFn = Callable[[str, str, str], Awaitable[str]]



def _action_history_summary(store: ActionHistoryStore, chat_id: str) -> tuple[str, str]:
    recent = store.get_recent(chat_id, max_items=10)
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
            last_user_ts = float(msg.ts or 0.0)
            break
    if not last_user_ts:
        return ""
    diff = now - last_user_ts
    if diff < float(minutes) * 60.0:
        return ""
    mm = max(1, int(diff // 60))
    return (
        f"重要提示：对方已经长时间（约{mm}分钟）没有回复你的消息了（这可能代表对方繁忙/不想回复/没注意到你的消息等情况，或在对方看来本次聊天已告一段落），请基于此情况规划下一步。\n"
    )

def _normalize_action(action: str) -> str:
    a = (action or "").strip().lower()
    mapping = {
        "reply": "direct_reply",
        "directreply": "direct_reply",
        "send_new": "send_new_message",
        "send_newmessage": "send_new_message",
        "new_message": "send_new_message",
        "end": "end_conversation",
        "block": "block_and_ignore",
        "ignore": "block_and_ignore",
        "wait": "wait",
        "listening": "listening",
        "rethink": "rethink_goal",
        "rethink_goal": "rethink_goal",
        "fetch": "fetch_knowledge",
        "fetch_knowledge": "fetch_knowledge",
    }
    if a in mapping:
        return mapping[a]
    if a in (
        "fetch_knowledge",
        "listening",
        "direct_reply",
        "rethink_goal",
        "end_conversation",
        "block_and_ignore",
        "wait",
        "send_new_message",
    ):
        return a
    return "direct_reply"

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
) -> PFCRunResult:
    """
    Run a single PFC (Plan-From-Context) planning iteration.

    PFC is the core planning system that decides whether and how to respond
    to a user message. It considers conversation history, goals, knowledge,
    action history, and timeout context to make an intelligent decision.

    The function handles several special actions:
    - "block_and_ignore": Ignore user messages for a period (for abusive behavior)
    - "wait"/"listening": Don't reply but stay engaged
    - "rethink_goal": Re-analyze and update conversation goals
    - "fetch_knowledge": Retrieve relevant memory/knowledge for context
    - "end_conversation": End the current conversation session
    - "direct_reply"/"send_new_message": Generate a reply

    Args:
        context: The plugin context
        runtime_cfg: The chat runtime configuration
        secrets: API secrets and credentials
        bot_name: The bot's name
        is_private: Whether this is a private chat
        chat_id: The chat/group identifier
        current_text: The user's message text
        memory_store: The conversation memory store
        action_history: The action history store
        memory_db: The vector memory database
        pfc_state_store: The PFC conversation state store
        generate_reply: Async function to generate reply text

    Returns:
        PFCRunResult containing the reply text, action type, reasoning,
        and whether the conversation has ended.
    """
    pfc_state_store.bind(context.data_dir)
    st: PFCConversationState = pfc_state_store.get(chat_id)
    now = time.time()

    _proxy = secrets.get("proxy", "") or ""
    _endpoint_path = secrets.get("endpoint_path", "") or runtime_cfg.endpoint_path
    if st.ignore_until_ts and now < float(st.ignore_until_ts):
        _log_step(context, runtime_cfg, chat_id=chat_id, step="pfc.ignore_window", fields={"until_ts": st.ignore_until_ts})
        return PFCRunResult(reply="", action="block_and_ignore", reason="ignore_window", ended=st.ended)
    if st.ignore_until_ts and now >= float(st.ignore_until_ts):
        # Ignore window expired, reset state
        st.ignore_until_ts = 0.0
        pfc_state_store.save(chat_id)
    if st.ended:
        _log_step(context, runtime_cfg, chat_id=chat_id, step="pfc.ended", fields={})
        return PFCRunResult(reply="", action="end_conversation", reason="ended", ended=True)

    history = memory_store.get_recent(chat_id, max_items=max(60, int(runtime_cfg.max_context_size) * 3))
    action_summary, last_ctx = _action_history_summary(action_history, chat_id)
    timeout_ctx = _timeout_context(history)

    skip_until = float(st.planner_skip_until or 0.0)
    if skip_until and now < skip_until:
        _log_step(context, runtime_cfg, chat_id=chat_id, step="pfc.plan.skip", fields={"skip_left_s": round(skip_until - now, 2)})
        act = "direct_reply"
        reply = await generate_reply(act, "planner_skipped", "")
        return PFCRunResult(reply=reply, action=act, reason="planner_skipped", ended=False)

    planner_timeout = min(float(getattr(runtime_cfg, "pfc_planner_timeout_seconds", 2.0)), float(runtime_cfg.timeout_seconds))
    planner_max_retry = 0
    planner_retry_interval = 0.2

    t0 = time.monotonic()
    _log_step(context, runtime_cfg, chat_id=chat_id, step="pfc.plan.start", fields={"history_items": len(history)})
    plan: PFCPlan = await plan_next_action(
        http_session=context.http_session,
        secrets=secrets,
        bot_name=bot_name,
        is_private=is_private,
        personality=runtime_cfg.personality,
        history=history,
        goal_list=st.goal_list,
        knowledge_list=st.knowledge_list,
        action_history_summary=action_summary,
        last_action_context=last_ctx,
        timeout_context=timeout_ctx,
        last_successful_reply_action=st.last_successful_reply_action or "",
        temperature=runtime_cfg.temperature,
        top_p=runtime_cfg.top_p,
        max_tokens=runtime_cfg.max_tokens,
        timeout_seconds=planner_timeout,
        max_retry=planner_max_retry,
        retry_interval_seconds=planner_retry_interval,
        proxy=_proxy,
        endpoint_path=_endpoint_path,
    )
    _log_step(
        context,
        runtime_cfg,
        chat_id=chat_id,
        step="pfc.plan.done",
        fields={"elapsed_s": round(time.monotonic() - t0, 3), "action": plan.action, "reason": plan.reason, "thinking": plan.thinking[:100] if plan.thinking else "", "wait_seconds": plan.wait_seconds},
    )
    if plan.reason in ("planner_timeout", "planner_failed"):
        window_s = float(getattr(runtime_cfg, "pfc_planner_fail_window_seconds", 60.0))
        threshold = int(getattr(runtime_cfg, "pfc_planner_fail_threshold", 2))
        backoff_s = float(getattr(runtime_cfg, "pfc_planner_backoff_seconds", 120.0))
        lst = list(st.planner_fail_ts)
        lst = [x for x in lst if now - float(x) < window_s]
        lst.append(now)
        st.planner_fail_ts = lst
        if len(lst) >= max(1, threshold):
            st.planner_skip_until = now + max(0.0, backoff_s)
            pfc_state_store.save(chat_id)
            _log_step(
                context,
                runtime_cfg,
                chat_id=chat_id,
                step="pfc.plan.backoff",
                fields={"fails": len(lst), "window_s": window_s, "backoff_s": backoff_s},
            )
        else:
            pfc_state_store.save(chat_id)
    else:
        if st.planner_fail_ts or st.planner_skip_until:
            st.planner_fail_ts = []
            st.planner_skip_until = 0.0
            pfc_state_store.save(chat_id)
    act = _normalize_action(plan.action)

    if act == "block_and_ignore":
        st.ignore_until_ts = now + 3600.0
        # Don't set ended=True permanently - the ignore window will expire naturally
        pfc_state_store.save(chat_id)
        _log_step(context, runtime_cfg, chat_id=chat_id, step="pfc.block", fields={"reason": plan.reason})
        return PFCRunResult(reply="", action=act, reason=plan.reason, ended=False)

    if act in ("wait", "listening"):
        wait_s = plan.wait_seconds if plan.wait_seconds > 0 else 0
        _log_step(context, runtime_cfg, chat_id=chat_id, step="pfc.no_reply", fields={"action": act, "reason": plan.reason, "wait_seconds": wait_s})
        # Store planner's precise wait duration for upstream to use
        combined_reason = plan.reason
        if plan.thinking:
            combined_reason = f"[thinking: {plan.thinking[:120]}] {plan.reason}"
        return PFCRunResult(reply="", action=act, reason=combined_reason, ended=False)

    if act == "rethink_goal":
        _log_step(context, runtime_cfg, chat_id=chat_id, step="pfc.rethink_goal.start", fields={})
        st.goal_list = await analyze_goals(
            http_session=context.http_session,
            secrets=secrets,
            bot_name=bot_name,
            personality=runtime_cfg.personality,
            history=history,
            current_goal_list=st.goal_list,
            action_history_text=action_summary,
            temperature=runtime_cfg.temperature,
            top_p=runtime_cfg.top_p,
            max_tokens=runtime_cfg.max_tokens,
            timeout_seconds=planner_timeout,
            max_retry=planner_max_retry,
            retry_interval_seconds=planner_retry_interval,
            proxy=_proxy,
            endpoint_path=_endpoint_path,
        )
        pfc_state_store.save(chat_id)
        _log_step(context, runtime_cfg, chat_id=chat_id, step="pfc.rethink_goal.done", fields={"goals": len(st.goal_list or [])})
        plan = await plan_next_action(
            http_session=context.http_session,
            secrets=secrets,
            bot_name=bot_name,
            is_private=is_private,
            personality=runtime_cfg.personality,
            history=history,
            goal_list=st.goal_list,
            knowledge_list=st.knowledge_list,
            action_history_summary=action_summary,
            last_action_context=last_ctx,
            timeout_context=timeout_ctx,
            last_successful_reply_action=st.last_successful_reply_action or "",
            temperature=runtime_cfg.temperature,
            top_p=runtime_cfg.top_p,
            max_tokens=runtime_cfg.max_tokens,
            timeout_seconds=planner_timeout,
            max_retry=planner_max_retry,
            retry_interval_seconds=planner_retry_interval,
            proxy=_proxy,
            endpoint_path=_endpoint_path,
        )
        act = _normalize_action(plan.action)

    if act == "fetch_knowledge":
        _log_step(context, runtime_cfg, chat_id=chat_id, step="pfc.fetch_knowledge.start", fields={})
        memory_db.bind(context.data_dir)
        mem = await build_memory_block(
            data_dir=context.data_dir,
            chat_id=chat_id,
            http_session=context.http_session,
            secrets=secrets,
            cfg=runtime_cfg.memory,
            bot_name=bot_name,
            history=history,
            current_text=current_text,
            planner_question="",
            memory_db=memory_db,
            temperature=runtime_cfg.temperature,
            top_p=runtime_cfg.top_p,
            max_tokens=runtime_cfg.max_tokens,
            timeout_seconds=planner_timeout,
            max_retry=planner_max_retry,
            retry_interval_seconds=planner_retry_interval,
            proxy=_proxy,
            endpoint_path=_endpoint_path,
        )
        mem = (mem or "").strip()
        if mem:
            st.knowledge_list.append({"text": mem, "ts": time.time()})
            st.knowledge_list = st.knowledge_list[-10:]
            pfc_state_store.save(chat_id)
        _log_step(context, runtime_cfg, chat_id=chat_id, step="pfc.fetch_knowledge.done", fields={"mem_chars": len(mem), "knowledge_items": len(st.knowledge_list or [])})
        plan = await plan_next_action(
            http_session=context.http_session,
            secrets=secrets,
            bot_name=bot_name,
            is_private=is_private,
            personality=runtime_cfg.personality,
            history=history,
            goal_list=st.goal_list,
            knowledge_list=st.knowledge_list,
            action_history_summary=action_summary,
            last_action_context=last_ctx,
            timeout_context=timeout_ctx,
            last_successful_reply_action=st.last_successful_reply_action or "",
            temperature=runtime_cfg.temperature,
            top_p=runtime_cfg.top_p,
            max_tokens=runtime_cfg.max_tokens,
            timeout_seconds=planner_timeout,
            max_retry=planner_max_retry,
            retry_interval_seconds=planner_retry_interval,
            proxy=_proxy,
            endpoint_path=_endpoint_path,
        )
        act = _normalize_action(plan.action)

    if act == "end_conversation":
        say_bye, why = await decide_say_bye(
            http_session=context.http_session,
            secrets=secrets,
            bot_name=bot_name,
            is_private=is_private,
            personality=runtime_cfg.personality,
            history=history,
            temperature=runtime_cfg.temperature,
            top_p=runtime_cfg.top_p,
            max_tokens=runtime_cfg.max_tokens,
            timeout_seconds=planner_timeout,
            max_retry=planner_max_retry,
            retry_interval_seconds=planner_retry_interval,
            proxy=_proxy,
            endpoint_path=_endpoint_path,
        )
        st.ended = True
        pfc_state_store.save(chat_id)
        if not say_bye:
            return PFCRunResult(reply="", action=act, reason=plan.reason, ended=True)
        reply = await generate_reply("say_goodbye", plan.reason, why)
        if reply:
            st.last_successful_reply_action = "say_goodbye"
            pfc_state_store.save(chat_id)
        return PFCRunResult(reply=reply, action="say_goodbye", reason=plan.reason, ended=True)

    if act in ("direct_reply", "send_new_message"):
        # Combine thinking + reason for richer context passed to reply generator
        combined_reason = plan.reason
        if plan.thinking:
            combined_reason = f"[thinking: {plan.thinking[:200]}] {plan.reason}"
        reply = await generate_reply(act, combined_reason, "")
        if reply:
            st.last_successful_reply_action = act
            pfc_state_store.save(chat_id)
        return PFCRunResult(reply=reply, action=act, reason=plan.reason, ended=False)

    reply = await generate_reply("direct_reply", plan.reason, "")
    if reply:
        st.last_successful_reply_action = "direct_reply"
        pfc_state_store.save(chat_id)
    return PFCRunResult(reply=reply, action="direct_reply", reason=plan.reason, ended=False)
