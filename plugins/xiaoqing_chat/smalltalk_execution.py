from __future__ import annotations

import asyncio
import random
import time
from copy import deepcopy
from typing import Any

from core.plugin_base import build_action, segments

from .brain_chat import get_brain_chat_max_context, get_brain_chat_think_level
from .llm.reply_checker import ReplyRejected
from .planning.action_history import ActionRecord
from .planning.planned_action import PlannedAction
from .planning.pfc_engine import PFCRunResult


def _fallback_idle_reply(runtime) -> str:
    candidates = [
        str(item or "").strip()
        for item in getattr(getattr(runtime, "cfg", None), "fallback_idle_replies", []) or []
        if str(item or "").strip()
    ]
    if not candidates:
        candidates = ["嗯", "啊这", "我在听", "你继续", "等我想下"]
    return random.choice(candidates)


async def generate_smalltalk_turn_impl(
    prepared,
    event: dict[str, Any],
    context,
    hctx,
    *,
    generated_turn_factory,
    ensure_user_message_recorded,
    get_lock,
    generate_reply_result,
    build_memory_block,
    run_pfc_once,
    normalize_generated_reply_state,
    cancel_generated_tasks,
    schedule_pfc_state_flush,
    clear_store_entry,
    log_step,
    build_text_message_parts,
):
    runtime, state, chat_id = hctx.runtime, hctx.state, hctx.chat_id
    bot_name, secrets, data_dir = hctx.bot_name, hctx.secrets, hctx.data_dir
    generated = generated_turn_factory()
    max_context_size = get_brain_chat_max_context(runtime, prepared.brain_chat_active)
    recent_history = await state.memory_store.get_recent_async(chat_id, max_items=max_context_size)
    think_level = get_brain_chat_think_level(
        runtime,
        prepared.brain_chat_active,
        history_len=len(recent_history),
    )
    planner_cfg = getattr(runtime.cfg, "planner", None)
    planner_enabled = bool(getattr(planner_cfg, "enable_planner", True))
    if prepared.brain_chat_active and bool(
        getattr(runtime.cfg.brain_chat, "private_planner_always_on", True)
    ):
        planner_enabled = True

    async with get_lock(chat_id):
        generated.local_id = await ensure_user_message_recorded(
            prepared.text,
            event,
            context,
            runtime,
            state=state,
        )
        if not prepared.forced and planner_enabled:
            generated.pfc_state_snapshot = deepcopy(await state.pfc_state_store.get_async(chat_id))
            if generated.pfc_state_snapshot and generated.pfc_state_snapshot.ended:
                generated.pfc_state_snapshot.ended = False

    async def _pfc_generate(mode: str, planner_reason: str, extra_reason: str) -> str:
        style_override = ""
        if mode == "say_goodbye":
            style_override = "说一句很短很自然的告别收尾，不要延伸话题。"
        elif mode == "send_new_message":
            style_override = "你刚发过一条消息，如果要继续发一条新消息，短一点，别轰炸。"
        act = PlannedAction(
            action="reply",
            think_level=think_level,
            reasoning=str(planner_reason or "").strip(),
            question="",
            unknown_words=[],
        )
        plan_reasoning = (planner_reason or "").strip()
        if extra_reason:
            plan_reasoning = (plan_reasoning + "\n" + str(extra_reason).strip()).strip()
        out, out_parts, out_marker = await generate_reply_result(
            text=prepared.text,
            event=event,
            context=context,
            runtime=runtime,
            state=state,
            forced=prepared.forced,
            action=act,
            plan_reasoning=plan_reasoning,
            bot_name=bot_name,
            secrets=secrets,
            reply_style_override=style_override,
            state_text=prepared.mood_text,
            is_brain_chat=prepared.brain_chat_active,
            prefetched_memory_task=generated.speculative_memory_task,
        )
        generated.media_marker = out_marker
        generated.reply_parts = out_parts
        return out or ""

    def _ensure_speculative_memory_task(planner_question: str = "") -> Any:
        if generated.speculative_memory_task is not None:
            return generated.speculative_memory_task
        speculative_history = recent_history[-max_context_size:] if max_context_size > 0 else []
        generated.speculative_memory_task = asyncio.create_task(
            build_memory_block(
                context=context,
                runtime=runtime,
                state=state,
                secrets=secrets,
                data_dir=data_dir,
                chat_id=chat_id,
                history=speculative_history,
                current_text=prepared.text,
                planner_question=planner_question,
                bot_name=bot_name,
            )
        )
        return generated.speculative_memory_task

    try:
        if prepared.forced:
            generated.reply_source = "forced"
            log_step(
                context,
                runtime,
                chat_id=chat_id,
                step="smalltalk.forced_direct",
                fields={"force_reason": prepared.force_reason},
            )
            _ensure_speculative_memory_task()
            direct_act = PlannedAction(
                action="reply",
                think_level=think_level,
                reasoning=f"用户直接发起对话，需要回复({prepared.force_reason or 'forced'})",
                question="",
                unknown_words=[],
            )
            (
                generated.reply,
                generated.reply_parts,
                generated.media_marker,
            ) = await generate_reply_result(
                text=prepared.text,
                event=event,
                context=context,
                runtime=runtime,
                state=state,
                forced=True,
                action=direct_act,
                plan_reasoning="用户直接发起对话，需要回复",
                bot_name=bot_name,
                secrets=secrets,
                state_text=prepared.mood_text,
                is_brain_chat=prepared.brain_chat_active,
                prefetched_memory_task=generated.speculative_memory_task,
            )
            if not generated.reply:
                generated.reply = _fallback_idle_reply(runtime)
                generated.reply_parts = build_text_message_parts(generated.reply)
        else:
            if planner_enabled:
                generated.reply_source = "pfc"
                _ensure_speculative_memory_task()
                generated.pfc_result = await run_pfc_once(
                    context=context,
                    runtime_cfg=runtime.cfg,
                    secrets=secrets,
                    bot_name=bot_name,
                    is_private=prepared.is_private,
                    chat_id=chat_id,
                    current_text=prepared.text,
                    memory_store=state.memory_store,
                    action_history=state.action_history,
                    memory_db=state.memory_db,
                    pfc_state_store=state.pfc_state_store,
                    generate_reply=_pfc_generate,
                    state_override=generated.pfc_state_snapshot,
                    persist_state=False,
                )
                log_step(
                    context,
                    runtime,
                    chat_id=chat_id,
                    step="smalltalk.pfc.done",
                    fields={
                        "action": generated.pfc_result.action,
                        "ended": bool(generated.pfc_result.ended),
                        "reason": generated.pfc_result.reason,
                        "reply_chars": len((generated.pfc_result.reply or "").strip()),
                    },
                )
                generated.reply = (generated.pfc_result.reply or "").strip()
            else:
                generated.reply_source = "direct"
                log_step(
                    context,
                    runtime,
                    chat_id=chat_id,
                    step="smalltalk.planner.disabled",
                    fields={"is_private": prepared.is_private, "brain_chat": prepared.brain_chat_active},
                )
                _ensure_speculative_memory_task()
                direct_act = PlannedAction(
                    action="reply",
                    think_level=think_level,
                    reasoning="planner_disabled",
                    question="",
                    unknown_words=[],
                )
                (
                    generated.reply,
                    generated.reply_parts,
                    generated.media_marker,
                ) = await generate_reply_result(
                    text=prepared.text,
                    event=event,
                    context=context,
                    runtime=runtime,
                    state=state,
                    forced=False,
                    action=direct_act,
                    plan_reasoning="planner_disabled",
                    bot_name=bot_name,
                    secrets=secrets,
                    state_text=prepared.mood_text,
                    is_brain_chat=prepared.brain_chat_active,
                    prefetched_memory_task=generated.speculative_memory_task,
                )
                if not generated.reply:
                    generated.reply = _fallback_idle_reply(runtime)
                    generated.reply_parts = build_text_message_parts(generated.reply)
                generated.pfc_result = PFCRunResult(
                    reply=generated.reply,
                    action="reply",
                    reason="planner_disabled",
                    ended=False,
                )

        normalize_generated_reply_state(
            generated,
            reply_text=generated.reply,
            reply_parts=generated.reply_parts,
        )
        return generated
    except ReplyRejected:
        cancel_generated_tasks(generated)
        if not prepared.forced and generated.pfc_state_snapshot is not None:
            async with get_lock(chat_id):
                state.pfc_state_store.set_state(chat_id, generated.pfc_state_snapshot)
                if runtime.cfg.goal.enable_goal:
                    top_goal = ""
                    goal_list = getattr(generated.pfc_state_snapshot, "goal_list", []) or []
                    if goal_list and isinstance(goal_list[0], dict):
                        top_goal = str(goal_list[0].get("goal", "") or "").strip()
                    if top_goal:
                        await state.goal_store.set_async(chat_id, goal=top_goal, source="planner")
                    else:
                        await clear_store_entry(state.goal_store, chat_id)
            schedule_pfc_state_flush(context, runtime, chat_id=chat_id)
        raise


async def finalize_smalltalk_turn_impl(
    prepared,
    generated,
    event: dict[str, Any],
    context,
    hctx,
    *,
    started_at: float,
    get_lock,
    most_recent_user_local_id,
    cancel_generated_tasks,
    assistant_reply_parts,
    build_generated_reply_output,
    sync_message_parts_to_registry,
    schedule_media_registry_flush,
    clear_store_entry,
    record_bot_reply,
    media_action_detail,
    schedule_pfc_state_flush,
    schedule_action_history_flush,
    spawn_bg_task,
    spawn_post_reply_bg_tasks,
    display_reply_text,
    mark_reply_media_used,
    log_step,
):
    runtime, state, chat_id = hctx.runtime, hctx.state, hctx.chat_id
    bot_name = hctx.bot_name

    history_snapshot: list[Any] = []
    should_schedule_pfc_state_flush = False
    should_return_empty = False
    commit_error: Exception | None = None

    async with get_lock(chat_id):
        latest_local_id = most_recent_user_local_id(chat_id)
        if latest_local_id != generated.local_id:
            cancel_generated_tasks(generated)
            log_step(
                context,
                runtime,
                chat_id=chat_id,
                step="smalltalk.stale.drop",
                fields={
                    "phase": "before_commit",
                    "local_id": generated.local_id,
                    "latest_local_id": latest_local_id,
                },
            )
            return []

    if generated.reply:
        reply_display_parts = assistant_reply_parts(context, generated)
        generated.reply_output = build_generated_reply_output(
            context,
            runtime,
            generated,
            brain_chat_active=prepared.brain_chat_active,
            display_parts=reply_display_parts,
        )
    else:
        reply_display_parts = ()
        generated.reply_output = None
    reply_parts = sync_message_parts_to_registry(
        state,
        reply_display_parts,
        context=context,
        runtime=runtime,
        schedule_media_registry_flush=schedule_media_registry_flush,
    )
    generated.reply_parts = reply_parts

    try:
        async with get_lock(chat_id):
            latest_local_id = most_recent_user_local_id(chat_id)
            if latest_local_id != generated.local_id:
                cancel_generated_tasks(generated)
                log_step(
                    context,
                    runtime,
                    chat_id=chat_id,
                    step="smalltalk.stale.drop",
                    fields={
                        "phase": "commit",
                        "local_id": generated.local_id,
                        "latest_local_id": latest_local_id,
                    },
                )
                return []

            if not prepared.forced and generated.pfc_state_snapshot is not None:
                state.pfc_state_store.set_state(chat_id, generated.pfc_state_snapshot)
                should_schedule_pfc_state_flush = True
                if runtime.cfg.goal.enable_goal:
                    top_goal = ""
                    goal_list = getattr(generated.pfc_state_snapshot, "goal_list", []) or []
                    if goal_list and isinstance(goal_list[0], dict):
                        top_goal = str(goal_list[0].get("goal", "") or "").strip()
                    if top_goal:
                        await state.goal_store.set_async(chat_id, goal=top_goal, source="planner")
                    else:
                        await clear_store_entry(state.goal_store, chat_id)

            if prepared.forced:
                history_snapshot = await record_bot_reply(
                    context,
                    runtime,
                    state,
                    chat_id,
                    bot_name,
                    generated.local_id,
                    forced=True,
                    action_str="reply",
                    reasoning=f"forced_direct:{prepared.force_reason or 'forced'}",
                    detail={
                        "source": "forced",
                        "force_reason": prepared.force_reason,
                        **media_action_detail(generated.media_marker, reply_parts),
                    },
                    parts=reply_parts,
                )
            else:
                assert generated.pfc_result is not None
                if not generated.reply:
                    cancel_generated_tasks(generated)
                    await state.heartflow.on_no_reply_async(chat_id=chat_id)
                    log_step(
                        context,
                        runtime,
                        chat_id=chat_id,
                        step="smalltalk.no_reply",
                        fields={
                            "reason": "pfc_no_reply",
                            "action": str(generated.pfc_result.action or "no_reply").strip()
                            or "no_reply",
                            "pfc_reason": generated.pfc_result.reason,
                            "source": generated.reply_source,
                        },
                    )
                    state.action_history.append(
                        chat_id,
                        ActionRecord(
                            ts=time.time(),
                            local_target=generated.local_id,
                            action=str(generated.pfc_result.action or "no_reply").strip() or "no_reply",
                            reasoning=str(generated.pfc_result.action or "").strip()
                            + (
                                f":{generated.pfc_result.reason}"
                                if generated.pfc_result.reason
                                else ""
                            ),
                            detail={"source": generated.reply_source},
                            executed=True,
                        ),
                    )
                    schedule_action_history_flush(context, runtime, chat_id=chat_id)
                    should_return_empty = True
                else:
                    pfc_reasoning = str(generated.pfc_result.action or "").strip()
                    if generated.pfc_result.reason:
                        pfc_reasoning += f":{generated.pfc_result.reason}"
                    history_snapshot = await record_bot_reply(
                        context,
                        runtime,
                        state,
                        chat_id,
                        bot_name,
                        generated.local_id,
                        forced=False,
                        action_str=str(generated.pfc_result.action or "reply").strip() or "reply",
                        reasoning=pfc_reasoning,
                        detail={
                            "source": generated.reply_source,
                            **media_action_detail(generated.media_marker, reply_parts),
                        },
                        parts=reply_parts,
                    )
    except Exception as exc:
        commit_error = exc

    if should_schedule_pfc_state_flush:
        schedule_pfc_state_flush(context, runtime, chat_id=chat_id)

    if commit_error is not None:
        raise commit_error

    if should_return_empty:
        return []

    spawn_bg_task(
        context,
        spawn_post_reply_bg_tasks(hctx, history_snapshot, event),
        name=f"post_reply:{chat_id}",
    )

    if runtime.cfg.debug.log_latency:
        context.logger.info(
            "xiaoqing_chat smalltalk chat_id=%s latency=%.3fs", chat_id, time.monotonic() - started_at
        )
    log_step(
        context,
        runtime,
        chat_id=chat_id,
        step="smalltalk.done",
        fields={
            "elapsed_s": round(time.monotonic() - started_at, 3),
            "reply_chars": len(display_reply_text(generated)),
            "reply": display_reply_text(generated),
        },
    )

    outbound_batches = (
        generated.reply_output.payload.outbound_batches
        if generated.reply_output is not None
        else segments(generated.reply)
    )
    if outbound_batches and isinstance(outbound_batches[0], dict):
        outbound_batches = [outbound_batches]

    spawn_bg_task(
        context,
        asyncio.to_thread(mark_reply_media_used, context, runtime, generated),
        name=f"reply_media_used:{chat_id}",
    )

    if len(outbound_batches) > 1:
        user_id = event.get("user_id")
        group_id = event.get("group_id")
        for batch in outbound_batches[:-1]:
            action = build_action(batch, user_id, group_id)
            if action:
                await context.send_action(action)
        return outbound_batches[-1]

    return outbound_batches[0] if outbound_batches else []
