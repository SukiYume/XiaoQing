"""执行小聊生成结果，并在投递确认后原子提交相关状态。"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from core.delivery import DeliveryReceipt, DeliverySegments, attach_receipt
from core.plugin_base import build_action, segments

from .brain_chat import get_brain_chat_max_context, get_brain_chat_think_level
from .llm.reply_checker import ReplyRejected
from .memory.memory import active_conversation_suffix
from .message_parts import normalize_message_parts
from .planning.action_history import ActionRecord
from .planning.pfc_engine import PFCRunResult
from .planning.planned_action import PlannedAction
from .task_scheduler import _create_task_safely

if TYPE_CHECKING:
    from .handler_context import HandlerContext
    from .runtime_state import ChatRuntimeState, _ChatRuntime
    from .smalltalk_models import _GeneratedSmalltalkTurn, _PreparedSmalltalkTurn


# 人性化延迟与批次文本估算


def _fallback_idle_reply(runtime: _ChatRuntime) -> str:
    candidates = [
        str(item or "").strip()
        for item in runtime.cfg.fallback_idle_replies
        if str(item or "").strip()
    ]
    if not candidates:
        candidates = ["我在听", "你接着说", "我想一下"]
    return random.choice(candidates)


def _jittered(value: float, ratio: float) -> float:
    if ratio <= 0:
        return value
    factor = 1.0 + random.uniform(-ratio, ratio)
    return max(0.0, value * factor)


def _compute_typing_delay(
    runtime: _ChatRuntime,
    *,
    input_text: str,
    output_text: str,
) -> float:
    """读消息 + 打字的总延迟，单位秒。返回 0 表示禁用。"""
    cfg = runtime.cfg.humanize
    if not bool(cfg.enable_typing_delay):
        return 0.0
    read_base = float(cfg.read_base_seconds)
    read_per = float(cfg.read_per_char_seconds)
    type_per = float(cfg.type_per_char_seconds)
    jitter = float(cfg.jitter_ratio)
    cap = float(cfg.max_total_delay_seconds)
    in_len = len(str(input_text or ""))
    out_len = len(str(output_text or ""))
    raw = (read_base + read_per * in_len) + (type_per * out_len)
    raw = _jittered(raw, jitter)
    if cap > 0:
        raw = min(raw, cap)
    return max(0.0, raw)


def _compute_interbubble_delay(runtime: _ChatRuntime) -> float:
    cfg = runtime.cfg.humanize
    if not bool(cfg.enable_typing_delay):
        return 0.0
    lo = max(0.0, float(cfg.interbubble_min_seconds))
    hi = max(lo, float(cfg.interbubble_max_seconds))
    if hi <= 0:
        return 0.0
    return random.uniform(lo, hi)


def _humanize_apply_to_forced(runtime: _ChatRuntime) -> bool:
    return bool(runtime.cfg.humanize.apply_to_forced)


def _batch_text_length(batch: Any) -> int:
    """估算一批 segment 的文本字符数，用于 inter-bubble 节奏。"""
    if isinstance(batch, dict):
        batch = [batch]
    total = 0
    if not isinstance(batch, (list, tuple)):
        return 0
    for seg in batch:
        if not isinstance(seg, dict):
            continue
        data = seg.get("data") or {}
        text = data.get("text")
        if isinstance(text, str):
            total += len(text)
    return total


@dataclass(frozen=True)
class _SmalltalkFinalization:
    """单次收尾生命周期使用的受限输入与注入操作。"""

    prepared: _PreparedSmalltalkTurn
    generated: _GeneratedSmalltalkTurn
    event: dict[str, Any]
    context: Any
    hctx: HandlerContext
    started_at: float
    get_lock: Any
    most_recent_user_local_id: Any
    cancel_generated_tasks: Any
    build_generated_reply_output: Any
    sync_message_parts_to_registry: Any
    schedule_media_registry_flush: Any
    clear_store_entry: Any
    record_bot_reply: Any
    media_action_detail: Any
    schedule_pfc_state_flush: Any
    schedule_action_history_flush: Any
    spawn_bg_task: Any
    spawn_post_reply_bg_tasks: Any
    display_reply_text: Any
    mark_reply_media_used: Any
    log_step: Any

    @property
    def runtime(self) -> _ChatRuntime:
        return self.hctx.runtime

    @property
    def state(self) -> ChatRuntimeState:
        return self.hctx.state

    @property
    def chat_id(self) -> str:
        return str(self.hctx.chat_id)

    @property
    def bot_name(self) -> str:
        return str(self.hctx.bot_name)


async def _execute_planner_wait(result: PFCRunResult) -> None:
    """在生成层执行一次规划等待，期间不持有会话锁。"""
    action = str(getattr(result, "action", "") or "").strip()
    if action not in {"wait", "listening"} or str(getattr(result, "reply", "") or "").strip():
        return
    wait_seconds = min(30.0, max(0.0, float(getattr(result, "wait_seconds", 0.0) or 0.0)))
    if wait_seconds > 0:
        await asyncio.sleep(wait_seconds)


async def generate_smalltalk_turn_impl(
    prepared: _PreparedSmalltalkTurn,
    event: dict[str, Any],
    context: Any,
    hctx: HandlerContext,
    *,
    generated_turn_factory: Callable[[], _GeneratedSmalltalkTurn],
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
) -> _GeneratedSmalltalkTurn:
    runtime, state, chat_id = hctx.runtime, hctx.state, hctx.chat_id
    bot_name, secrets, data_dir = hctx.bot_name, hctx.secrets, hctx.data_dir
    generated = generated_turn_factory()
    max_context_size = get_brain_chat_max_context(runtime, prepared.brain_chat_active)
    recent_history = await state.memory_store.get_recent_async(chat_id, max_items=max_context_size)
    memory_cfg = getattr(runtime.cfg, "memory", None)
    recent_history = active_conversation_suffix(
        recent_history,
        idle_gap_seconds=float(getattr(memory_cfg, "conversation_idle_gap_seconds", 1800.0) or 0.0),
    )
    think_level = get_brain_chat_think_level(
        runtime,
        prepared.brain_chat_active,
        history_len=len(recent_history),
    )
    planner_enabled = runtime.cfg.planner.enable_planner
    if prepared.brain_chat_active and runtime.cfg.brain_chat.private_planner_always_on:
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
        generated.speculative_memory_task = _create_task_safely(
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
                await _execute_planner_wait(generated.pfc_result)
                log_step(
                    context,
                    runtime,
                    chat_id=chat_id,
                    step="smalltalk.pfc.done",
                    fields={
                        "action": generated.pfc_result.action,
                        "ended": bool(generated.pfc_result.ended),
                        "reason": generated.pfc_result.reason,
                        "wait_seconds": float(
                            getattr(generated.pfc_result, "wait_seconds", 0.0) or 0.0
                        ),
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
                    fields={
                        "is_private": prepared.is_private,
                        "brain_chat": prepared.brain_chat_active,
                    },
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


# 投递前准备与状态提交


def _drop_stale_generated_turn(
    finalization: _SmalltalkFinalization,
    *,
    phase: str,
) -> bool:
    """取消并审计在会话内竞争中失效的生成轮次。"""

    generated = finalization.generated
    latest_local_id = finalization.most_recent_user_local_id(finalization.chat_id)
    if latest_local_id == generated.local_id:
        return False
    finalization.cancel_generated_tasks(generated)
    finalization.log_step(
        finalization.context,
        finalization.runtime,
        chat_id=finalization.chat_id,
        step="smalltalk.stale.drop",
        fields={
            "phase": phase,
            "local_id": generated.local_id,
            "latest_local_id": latest_local_id,
        },
    )
    return True


def _prepare_smalltalk_reply_output(finalization: _SmalltalkFinalization):
    """提交前构造展示输出并同步媒体引用。"""

    generated = finalization.generated
    if generated.reply:
        reply_display_parts = normalize_message_parts(generated.reply_parts)
        generated.reply_output = finalization.build_generated_reply_output(
            finalization.runtime,
            generated,
            brain_chat_active=finalization.prepared.brain_chat_active,
            display_parts=reply_display_parts,
        )
    else:
        reply_display_parts = ()
        generated.reply_output = None
    reply_parts = finalization.sync_message_parts_to_registry(
        finalization.state,
        reply_display_parts,
        context=finalization.context,
        runtime=finalization.runtime,
        schedule_media_registry_flush=finalization.schedule_media_registry_flush,
    )
    generated.reply_parts = reply_parts
    return reply_parts


async def _apply_generated_pfc_state(finalization: _SmalltalkFinalization) -> bool:
    """由持有会话锁的调用方提交生成后的规划器快照。"""

    prepared = finalization.prepared
    generated = finalization.generated
    if prepared.forced or generated.pfc_state_snapshot is None:
        return False
    state = finalization.state
    state.pfc_state_store.set_state(finalization.chat_id, generated.pfc_state_snapshot)
    if finalization.runtime.cfg.goal.enable_goal:
        top_goal = ""
        goal_list = getattr(generated.pfc_state_snapshot, "goal_list", []) or []
        if goal_list and isinstance(goal_list[0], dict):
            top_goal = str(goal_list[0].get("goal", "") or "").strip()
        if top_goal:
            await state.goal_store.set_async(
                finalization.chat_id,
                goal=top_goal,
                source="planner",
            )
        else:
            await finalization.clear_store_entry(state.goal_store, finalization.chat_id)
    return True


async def _finalize_no_reply_turn(finalization: _SmalltalkFinalization) -> list[Any]:
    """提交规划器与不回复状态，不创建投递回执。"""

    generated = finalization.generated
    should_flush_pfc = False
    try:
        async with finalization.get_lock(finalization.chat_id):
            if _drop_stale_generated_turn(finalization, phase="commit"):
                return []
            should_flush_pfc = await _apply_generated_pfc_state(finalization)
            assert generated.pfc_result is not None
            finalization.cancel_generated_tasks(generated)
            await finalization.state.heartflow.on_no_reply_async(chat_id=finalization.chat_id)
            action = str(generated.pfc_result.action or "no_reply").strip() or "no_reply"
            finalization.log_step(
                finalization.context,
                finalization.runtime,
                chat_id=finalization.chat_id,
                step="smalltalk.no_reply",
                fields={
                    "reason": "pfc_no_reply",
                    "action": action,
                    "pfc_reason": generated.pfc_result.reason,
                    "source": generated.reply_source,
                },
            )
            reasoning = str(generated.pfc_result.action or "").strip()
            if generated.pfc_result.reason:
                reasoning += f":{generated.pfc_result.reason}"
            finalization.state.action_history.append(
                finalization.chat_id,
                ActionRecord(
                    ts=time.time(),
                    local_target=generated.local_id,
                    action=action,
                    reasoning=reasoning,
                    detail={"source": generated.reply_source},
                    executed=True,
                ),
            )
            finalization.schedule_action_history_flush(
                finalization.context,
                finalization.runtime,
                chat_id=finalization.chat_id,
            )
    finally:
        if should_flush_pfc:
            finalization.schedule_pfc_state_flush(
                finalization.context,
                finalization.runtime,
                chat_id=finalization.chat_id,
            )
    return []


def _smalltalk_outbound_batches(finalization: _SmalltalkFinalization) -> list[Any]:
    generated = finalization.generated
    outbound_batches = (
        generated.reply_output.payload.outbound_batches
        if generated.reply_output is not None
        else segments(generated.reply)
    )
    if outbound_batches and isinstance(outbound_batches[0], dict):
        outbound_batches = [outbound_batches]
    return list(outbound_batches or [])


async def _record_delivered_reply(
    finalization: _SmalltalkFinalization,
    reply_parts,
) -> list[Any]:
    """只记录已经确认对外投递成功的回复。"""

    prepared = finalization.prepared
    generated = finalization.generated
    common_args = (
        finalization.context,
        finalization.runtime,
        finalization.state,
        finalization.chat_id,
        finalization.bot_name,
        generated.local_id,
    )
    if prepared.forced:
        forced = True
        action_str = "reply"
        reasoning = f"forced_direct:{prepared.force_reason or 'forced'}"
        detail = {
            "source": "forced",
            "force_reason": prepared.force_reason,
            **finalization.media_action_detail(generated.media_marker, reply_parts),
        }
    else:
        assert generated.pfc_result is not None
        forced = False
        action_str = str(generated.pfc_result.action or "reply").strip() or "reply"
        reasoning = str(generated.pfc_result.action or "").strip()
        if generated.pfc_result.reason:
            reasoning += f":{generated.pfc_result.reason}"
        detail = {
            "source": generated.reply_source,
            **finalization.media_action_detail(generated.media_marker, reply_parts),
        }

    history = await finalization.record_bot_reply(
        *common_args,
        forced=forced,
        action_str=action_str,
        reasoning=reasoning,
        detail=detail,
        parts=reply_parts,
    )
    if not isinstance(history, list):
        raise TypeError("recorded memory history must be a list")
    return history


async def _commit_smalltalk_delivery(
    finalization: _SmalltalkFinalization,
    reply_parts,
) -> None:
    """仅在全部外发动作成功后提交内部状态。"""

    should_flush_pfc = False
    history_snapshot: list[Any] = []
    try:
        async with finalization.get_lock(finalization.chat_id):
            latest_local_id = finalization.most_recent_user_local_id(finalization.chat_id)
            if latest_local_id == finalization.generated.local_id:
                should_flush_pfc = await _apply_generated_pfc_state(finalization)
            history_snapshot = await _record_delivered_reply(finalization, reply_parts)
    finally:
        if should_flush_pfc:
            finalization.schedule_pfc_state_flush(
                finalization.context,
                finalization.runtime,
                chat_id=finalization.chat_id,
            )

    finalization.spawn_bg_task(
        finalization.context,
        finalization.spawn_post_reply_bg_tasks(
            finalization.hctx,
            history_snapshot,
            finalization.event,
        ),
        name=f"post_reply:{finalization.chat_id}",
    )
    finalization.spawn_bg_task(
        finalization.context,
        asyncio.to_thread(
            finalization.mark_reply_media_used,
            finalization.context,
            finalization.generated,
        ),
        name=f"reply_media_used:{finalization.chat_id}",
    )
    if finalization.runtime.cfg.debug.log_latency:
        finalization.log_step(
            finalization.context,
            finalization.runtime,
            chat_id=finalization.chat_id,
            step="smalltalk.latency",
            fields={"latency_s": round(time.monotonic() - finalization.started_at, 3)},
        )
    finalization.log_step(
        finalization.context,
        finalization.runtime,
        chat_id=finalization.chat_id,
        step="smalltalk.done",
        fields={
            "elapsed_s": round(time.monotonic() - finalization.started_at, 3),
            "reply_chars": len(finalization.display_reply_text(finalization.generated)),
            "reply": finalization.display_reply_text(finalization.generated),
        },
    )


async def _rollback_smalltalk_delivery(
    finalization: _SmalltalkFinalization,
    *,
    batch_count: int,
) -> None:
    finalization.cancel_generated_tasks(finalization.generated)
    finalization.log_step(
        finalization.context,
        finalization.runtime,
        chat_id=finalization.chat_id,
        step="smalltalk.delivery.rollback",
        fields={"local_id": finalization.generated.local_id, "batches": batch_count},
    )


def _build_smalltalk_delivery_receipt(
    finalization: _SmalltalkFinalization,
    reply_parts,
    *,
    batch_count: int,
) -> DeliveryReceipt:
    async def commit() -> None:
        await _commit_smalltalk_delivery(finalization, reply_parts)

    async def rollback() -> None:
        await _rollback_smalltalk_delivery(finalization, batch_count=batch_count)

    return DeliveryReceipt(
        expected_actions=batch_count,
        commit=commit,
        rollback=rollback,
        # 已提交但回执丢失时保留本轮记忆，避免可能已看到的回复从上下文消失。
        unknown=commit,
    )


async def _apply_smalltalk_presend_delay(
    finalization: _SmalltalkFinalization,
    *,
    has_outbound_batches: bool,
) -> bool:
    apply_humanize = (not finalization.prepared.forced) or _humanize_apply_to_forced(
        finalization.runtime
    )
    if not apply_humanize or not has_outbound_batches:
        return apply_humanize
    delay = _compute_typing_delay(
        finalization.runtime,
        input_text=finalization.prepared.text,
        output_text=finalization.display_reply_text(finalization.generated),
    )
    if delay > 0:
        finalization.log_step(
            finalization.context,
            finalization.runtime,
            chat_id=finalization.chat_id,
            step="smalltalk.humanize.delay",
            fields={"phase": "pre_send", "delay_s": round(delay, 3)},
        )
        await asyncio.sleep(delay)
    return apply_humanize


def _smalltalk_interbubble_delay(runtime: _ChatRuntime, batch: Any) -> float:
    gap = _compute_interbubble_delay(runtime)
    batch_chars = _batch_text_length(batch)
    if not batch_chars:
        return gap
    type_per = float(runtime.cfg.humanize.type_per_char_seconds)
    return gap + min(1.2, type_per * batch_chars * 0.6)


async def _send_smalltalk_intermediate_batches(
    finalization: _SmalltalkFinalization,
    outbound_batches: list[Any],
    receipt: DeliveryReceipt,
    *,
    apply_humanize: bool,
) -> bool:
    """发送末批之前的所有批次；最后一批由调用方返回给核心层。"""

    user_id = finalization.event.get("user_id")
    group_id = finalization.event.get("group_id")
    for batch in outbound_batches[:-1]:
        action = build_action(batch, user_id, group_id)
        if not action:
            await receipt.record(False)
            return False
        try:
            sent = await finalization.context.send_action(attach_receipt(action, receipt))
        except BaseException:
            await asyncio.shield(receipt.record(False))
            raise
        if sent is not True:
            if sent is False:
                await receipt.record(False)
            return False
        if apply_humanize:
            gap = _smalltalk_interbubble_delay(finalization.runtime, batch)
            if gap > 0:
                await asyncio.sleep(gap)
    return True


async def finalize_smalltalk_turn_impl(
    prepared: _PreparedSmalltalkTurn,
    generated: _GeneratedSmalltalkTurn,
    event: dict[str, Any],
    context: Any,
    hctx: HandlerContext,
    *,
    started_at: float,
    get_lock,
    most_recent_user_local_id,
    cancel_generated_tasks,
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
) -> list[dict[str, Any]]:
    """完成生成轮次，并把状态提交绑定到投递确认。"""

    finalization = _SmalltalkFinalization(
        prepared=prepared,
        generated=generated,
        event=event,
        context=context,
        hctx=hctx,
        started_at=started_at,
        get_lock=get_lock,
        most_recent_user_local_id=most_recent_user_local_id,
        cancel_generated_tasks=cancel_generated_tasks,
        build_generated_reply_output=build_generated_reply_output,
        sync_message_parts_to_registry=sync_message_parts_to_registry,
        schedule_media_registry_flush=schedule_media_registry_flush,
        clear_store_entry=clear_store_entry,
        record_bot_reply=record_bot_reply,
        media_action_detail=media_action_detail,
        schedule_pfc_state_flush=schedule_pfc_state_flush,
        schedule_action_history_flush=schedule_action_history_flush,
        spawn_bg_task=spawn_bg_task,
        spawn_post_reply_bg_tasks=spawn_post_reply_bg_tasks,
        display_reply_text=display_reply_text,
        mark_reply_media_used=mark_reply_media_used,
        log_step=log_step,
    )

    async with get_lock(finalization.chat_id):
        if _drop_stale_generated_turn(finalization, phase="before_commit"):
            return []

    reply_parts = _prepare_smalltalk_reply_output(finalization)
    if not generated.reply:
        return await _finalize_no_reply_turn(finalization)

    outbound_batches = _smalltalk_outbound_batches(finalization)
    if not outbound_batches:
        cancel_generated_tasks(generated)
        return []

    receipt = _build_smalltalk_delivery_receipt(
        finalization,
        reply_parts,
        batch_count=len(outbound_batches),
    )
    apply_humanize = await _apply_smalltalk_presend_delay(
        finalization,
        has_outbound_batches=bool(outbound_batches),
    )
    if len(outbound_batches) > 1:
        sent = await _send_smalltalk_intermediate_batches(
            finalization,
            outbound_batches,
            receipt,
            apply_humanize=apply_humanize,
        )
        if not sent:
            return []
        return DeliverySegments(outbound_batches[-1], receipt)

    return DeliverySegments(outbound_batches[0], receipt)
