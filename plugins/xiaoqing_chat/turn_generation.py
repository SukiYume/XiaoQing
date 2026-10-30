"""单轮生成：在会话锁内记录用户消息，在锁外运行规划器或直接生成回复。"""

from __future__ import annotations

import asyncio
import random
import time
from copy import deepcopy
from typing import TYPE_CHECKING, Any

from .brain_chat import (
    get_brain_chat_max_context,
    get_brain_chat_think_level,
)
from .chat_event import event_chat_id, event_sender_name
from .chat_runtime import chat_lock
from .context_builder import retrieve_memory_context
from .llm.check_result import ReplyRejected
from .logging_utils import log_step
from .memory.local_ids import allocate_local_id
from .memory.memory import active_conversation_suffix
from .memory.person_profile import get_profile_generation
from .message_parts import build_message_parts, build_text_message_parts, normalize_message_parts
from .planning.pfc_engine import PFCRunResult, run_pfc_once
from .planning.planned_action import PlannedAction
from .reply_generator import generate_reply_draft
from .reply_media import (
    event_media_items_for_memory,
    normalize_generated_reply_state,
    sync_message_parts_to_registry,
)
from .smalltalk_models import GeneratedSmalltalkTurn
from .task_scheduler import (
    create_task_safely,
    schedule_memory_persist,
    schedule_pfc_state_flush,
)

if TYPE_CHECKING:
    from .handler_context import HandlerContext
    from .runtime_state import ChatRuntime
    from .smalltalk_models import PreparedSmalltalkTurn


def _fallback_idle_reply(runtime: ChatRuntime) -> str:
    candidates = [
        str(item or "").strip()
        for item in runtime.cfg.fallback_idle_replies
        if str(item or "").strip()
    ]
    if not candidates:
        candidates = ["我在听", "你接着说", "我想一下"]
    return random.choice(candidates)


async def commit_planner_snapshot(state, runtime: ChatRuntime, chat_id: str, snapshot) -> None:
    """写回规划器快照，并让会话目标跟随规划器排第一的目标；调用方持有会话锁。"""

    state.pfc_state_store.set_state(chat_id, snapshot)
    if not runtime.cfg.goal.enable_goal:
        return
    if snapshot.top_goal:
        await state.goal_store.set_async(chat_id, goal=snapshot.top_goal, source="planner")
    else:
        await asyncio.to_thread(state.goal_store.clear, chat_id)


# 规划器要求“告别”或“再补一条”时附加的语气约束。
_PLANNER_MODE_NOTES = {
    "say_goodbye"     : "说一句很短很自然的告别收尾，不要延伸话题。",
    "send_new_message": "你刚发过一条消息，如果要继续发一条新消息，短一点，别轰炸。",
}


class _TurnGenerator:
    """一轮生成的共享上下文：强制回复、规划器和直接回复三条路径共用同一套起草参数。"""

    def __init__(
        self,
        prepared: PreparedSmalltalkTurn,
        event: dict[str, Any],
        context: Any,
        hctx: HandlerContext,
        generated: GeneratedSmalltalkTurn,
        recent_history: list[Any],
    ) -> None:
        self.prepared       = prepared
        self.event          = event
        self.context        = context
        self.hctx           = hctx
        self.generated      = generated
        self.recent_history = recent_history
        self.max_context    = get_brain_chat_max_context(hctx.runtime, prepared.brain_chat_active)
        self.think_level    = get_brain_chat_think_level(
            hctx.runtime, prepared.brain_chat_active, history_len=len(recent_history)
        )

    def log(self, step: str, fields: dict[str, Any]) -> None:
        log_step(
            self.context, self.hctx.runtime, chat_id=self.hctx.chat_id, step=step, fields=fields
        )

    def start_memory_prefetch(self) -> None:
        """回复生成前先在后台检索记忆，生成阶段直接复用这份结果。"""
        if self.generated.speculative_memory_task is not None:
            return
        hctx = self.hctx
        history = self.recent_history[-self.max_context :] if self.max_context > 0 else []
        self.generated.speculative_memory_task = create_task_safely(
            retrieve_memory_context(
                context          = self.context,
                runtime          = hctx.runtime,
                state            = hctx.state,
                secrets          = hctx.secrets,
                data_dir         = hctx.data_dir,
                chat_id          = hctx.chat_id,
                history          = history,
                current_text     = self.prepared.text,
                planner_question = "",
                bot_name         = hctx.bot_name,
            )
        )

    async def draft(
        self,
        *,
        forced: bool,
        reasoning: str,
        plan_reasoning: str,
        turn_note: str = "",
    ) -> tuple[str, tuple[dict[str, Any], ...], Any]:
        hctx = self.hctx
        return await _generate_reply_result(
            text    = self.prepared.text,
            event   = self.event,
            context = self.context,
            runtime = hctx.runtime,
            state   = hctx.state,
            forced  = forced,
            action  = PlannedAction(
                action        = "reply",
                think_level   = self.think_level,
                reasoning     = reasoning,
                question      = "",
                unknown_words = [],
            ),
            plan_reasoning         = plan_reasoning,
            bot_name               = hctx.bot_name,
            secrets                = hctx.secrets,
            turn_note              = turn_note,
            state_text             = self.prepared.mood_text,
            is_brain_chat          = self.prepared.brain_chat_active,
            prefetched_memory_task = self.generated.speculative_memory_task,
        )

    async def draft_or_idle(self, *, forced: bool, reasoning: str, plan_reasoning: str) -> None:
        """直接生成一条回复；模型给不出内容时用配置的空闲短句兜底。"""
        generated = self.generated
        generated.reply, generated.reply_parts, generated.media_marker = await self.draft(
            forced=forced, reasoning=reasoning, plan_reasoning=plan_reasoning
        )
        if not generated.reply:
            generated.reply       = _fallback_idle_reply(self.hctx.runtime)
            generated.reply_parts = build_text_message_parts(generated.reply)

    async def planner_reply(self, mode: str, planner_reason: str, extra_reason: str) -> str:
        """规划器决定发言时的回调：按其模式和理由生成回复文本。"""
        planner_reason = str(planner_reason or "").strip()
        plan_reasoning = "\n".join(
            part for part in (planner_reason, str(extra_reason or "").strip()) if part
        )
        reply, self.generated.reply_parts, self.generated.media_marker = await self.draft(
            forced         = self.prepared.forced,
            reasoning      = planner_reason,
            plan_reasoning = plan_reasoning,
            turn_note      = _PLANNER_MODE_NOTES.get(mode, ""),
        )
        return reply or ""

    async def run_forced(self) -> None:
        force_reason                = self.prepared.force_reason
        self.generated.reply_source = "forced"
        self.log("smalltalk.forced_direct", {"force_reason": force_reason})
        self.start_memory_prefetch()
        await self.draft_or_idle(
            forced         = True,
            reasoning      = f"用户直接发起对话，需要回复({force_reason or 'forced'})",
            plan_reasoning = "用户直接发起对话，需要回复",
        )

    async def run_planner(self) -> None:
        hctx, generated = self.hctx, self.generated
        generated.reply_source = "pfc"
        self.start_memory_prefetch()
        result = await run_pfc_once(
            context         = self.context,
            runtime_cfg     = hctx.runtime.cfg,
            secrets         = hctx.secrets,
            bot_name        = hctx.bot_name,
            is_private      = self.prepared.is_private,
            chat_id         = hctx.chat_id,
            current_text    = self.prepared.text,
            memory_store    = hctx.state.memory_store,
            action_history  = hctx.state.action_history,
            memory_db       = hctx.state.memory_db,
            pfc_state_store = hctx.state.pfc_state_store,
            generate_reply  = self.planner_reply,
            state_override  = generated.pfc_state_snapshot,
            persist_state   = False,
        )
        # wait / listening 直接按不发言结束本轮，不在这里睡 wait_seconds：Core 按“群 + 用户”
        # 串行处理事件，这里每睡一秒，同一个人接着发的消息就晚一秒进来；本轮还占着生成
        # 配额，别人的新消息进来也会被挡掉。下一条消息自然会触发新一轮判断。
        generated.pfc_result = result
        generated.reply      = (result.reply or "").strip()
        self.log(
            "smalltalk.pfc.done",
            {
                "action"      : result.action,
                "ended"       : bool(result.ended),
                "reason"      : result.reason,
                "wait_seconds": float(result.wait_seconds),
                "reply_chars" : len(generated.reply),
            },
        )

    async def run_direct(self) -> None:
        prepared                    = self.prepared
        self.generated.reply_source = "direct"
        self.log(
            "smalltalk.planner.disabled",
            {"is_private": prepared.is_private, "brain_chat": prepared.brain_chat_active},
        )
        self.start_memory_prefetch()
        await self.draft_or_idle(
            forced=False, reasoning="planner_disabled", plan_reasoning="planner_disabled"
        )
        self.generated.pfc_result = PFCRunResult(
            reply=self.generated.reply, action="reply", reason="planner_disabled", ended=False
        )


async def generate_smalltalk_turn(
    prepared: PreparedSmalltalkTurn,
    event: dict[str, Any],
    context: Any,
    hctx: HandlerContext,
) -> GeneratedSmalltalkTurn:
    """记录本轮消息后生成回复；被审查拒绝时写回规划器快照，让下一轮从新状态出发。"""

    runtime, state, chat_id = hctx.runtime, hctx.state, hctx.chat_id
    generated                       = GeneratedSmalltalkTurn()
    generated.session_generation    = get_profile_generation(context.data_dir, chat_id)
    event["_xc_session_generation"] = generated.session_generation
    recent                          = await state.memory_store.get_recent_async(
        chat_id, max_items=get_brain_chat_max_context(runtime, prepared.brain_chat_active)
    )
    recent_history = active_conversation_suffix(
        recent, idle_gap_seconds=float(runtime.cfg.memory.conversation_idle_gap_seconds)
    )
    planner_enabled = runtime.cfg.planner.enable_planner or (
        prepared.brain_chat_active and runtime.cfg.brain_chat.private_planner_always_on
    )

    async with chat_lock(chat_id):
        generated.local_id = await ensure_user_message_recorded(
            prepared.text, event, context, runtime, state=state
        )
        if not prepared.forced and planner_enabled:
            generated.pfc_state_snapshot = deepcopy(await state.pfc_state_store.get_async(chat_id))
            if generated.pfc_state_snapshot and generated.pfc_state_snapshot.ended:
                generated.pfc_state_snapshot.ended = False

    turn = _TurnGenerator(prepared, event, context, hctx, generated, recent_history)
    try:
        if prepared.forced:
            await turn.run_forced()
        elif planner_enabled:
            await turn.run_planner()
        else:
            await turn.run_direct()
        normalize_generated_reply_state(
            generated, reply_text=generated.reply, reply_parts=generated.reply_parts
        )
        return generated
    except ReplyRejected:
        generated.cancel_speculative_memory()
        if not prepared.forced and generated.pfc_state_snapshot is not None:
            committed = False
            async with chat_lock(chat_id):
                # 生成在锁外进行：会话被重置后，旧轮次的规划快照不能把已清除的目标写回去。
                current_generation = get_profile_generation(context.data_dir, chat_id)
                if (
                    generated.session_generation is None
                    or generated.session_generation == current_generation
                ):
                    await commit_planner_snapshot(
                        state, runtime, chat_id, generated.pfc_state_snapshot
                    )
                    committed = True
            if committed:
                schedule_pfc_state_flush(context, runtime, chat_id=chat_id)
        raise
    finally:
        task = generated.speculative_memory_task
        if isinstance(task, asyncio.Task):
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def _event_message_timestamp(event: dict[str, Any]) -> float | None:
    """采用可信 OneBot 秒级时间；明显无效或来自未来的值回退到本机时钟。"""

    raw = event.get("time")
    if raw is None or isinstance(raw, bool):
        return None
    try:
        timestamp = float(raw)
    except (TypeError, ValueError):
        return None
    if timestamp <= 0 or timestamp > time.time() + 300.0:
        return None
    return timestamp


async def ensure_user_message_recorded(
    text: str,
    event: dict[str, Any],
    context,
    runtime,
    *,
    state,
) -> str:
    chat_id = event_chat_id(event)

    state.review_store.cleanup_expired()
    state.set_last_observe_ts(chat_id, time.time())

    msg_id            = event.get("message_id")
    history           = await state.memory_store.get_async(chat_id)
    existing_local_id = str(event.get("_xc_user_recorded_local_id") or "").strip()

    if not existing_local_id and msg_id is not None:
        for msg in reversed(history[-40:]):
            if msg.role == "user" and msg.message_id == msg_id:
                existing_local_id = (msg.local_id or "").strip()
                if existing_local_id:
                    break

    if existing_local_id:
        event["_xc_user_recorded"]          = True
        event["_xc_user_recorded_local_id"] = existing_local_id
        return existing_local_id

    local_id               = allocate_local_id(chat_id)
    cached_effective_parts = normalize_message_parts(event.get("_xc_effective_user_parts"))
    if cached_effective_parts:
        message_parts = sync_message_parts_to_registry(
            state,
            cached_effective_parts,
            context = context,
            runtime = runtime,
        )
    else:
        message_parts = sync_message_parts_to_registry(
            state,
            build_message_parts(
                text,
                await event_media_items_for_memory(event, context=context, runtime=runtime),
                store=state.media_store,
            ),
            context = context,
            runtime = runtime,
        )
    state.memory_store.append(
        chat_id,
        role       = "user",
        name       = event_sender_name(event),
        user_id    = event.get("user_id"),
        message_id = msg_id,
        local_id   = local_id,
        parts      = message_parts,
        ts         = _event_message_timestamp(event),
    )
    schedule_memory_persist(context, runtime, chat_id=chat_id)
    await state.heartflow.on_user_message_async(chat_id=chat_id)
    log_step(
        context,
        runtime,
        chat_id = chat_id,
        step    = "smalltalk.memory.append_user",
        fields  = {"local_id": local_id},
    )
    event["_xc_user_recorded"]          = True
    event["_xc_user_recorded_local_id"] = local_id
    return local_id


async def _generate_reply_result(**kwargs) -> tuple[str, tuple[dict[str, Any], ...], Any]:
    draft = await generate_reply_draft(**kwargs)
    if draft is None:
        return "", (), None
    reply_text  = draft.text.strip()
    reply_parts = normalize_message_parts(draft.parts)
    if reply_text and not reply_parts:
        reply_parts = build_text_message_parts(reply_text)
    return reply_text, reply_parts, draft.media_marker
