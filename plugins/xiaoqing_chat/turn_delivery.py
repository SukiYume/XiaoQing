"""单轮投递：拟人化延迟、分批外发，并只在投递确认后提交记忆、规划与行动状态。"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from core.delivery import DeliveryReceipt, DeliverySegments, send_with_receipt
from core.interfaces import ACTION_BYPASS_SINK_KEY
from core.plugin_base import build_action, segments

from .brain_chat import (
    maybe_add_mode_indicator,
)
from .chat_runtime import chat_lock
from .frequency_control import record_reply_frequency
from .logging_utils import log_step
from .memory.local_ids import allocate_local_id, most_recent_user_local_id
from .memory.person_profile import get_profile_generation
from .message_parts import build_text_message_parts, normalize_message_parts
from .planning.action_history import ActionRecord
from .post_reply_tasks import spawn_post_reply_tasks
from .reply_media import (
    display_reply_text,
    mark_reply_media_used,
    media_action_detail,
    prefix_reply_parts,
    reply_send_prefix,
    sync_message_parts_to_registry,
)
from .reply_payload import ReplyPayload, build_reply_payload_from_parts
from .smalltalk_models import GeneratedSmalltalkTurn
from .task_scheduler import (
    schedule_action_history_flush,
    schedule_memory_persist,
    schedule_pfc_state_flush,
    spawn_bg_task,
)
from .turn_generation import commit_planner_snapshot

if TYPE_CHECKING:
    from .handler_context import HandlerContext
    from .runtime_state import ChatRuntime
    from .smalltalk_models import PreparedSmalltalkTurn

logger = logging.getLogger(__name__)


def _jittered(value: float, ratio: float) -> float:
    if ratio <= 0:
        return value
    factor = 1.0 + random.uniform(-ratio, ratio)
    return max(0.0, value * factor)


def _compute_typing_delay(
    runtime: ChatRuntime,
    *,
    input_text: str,
    output_text: str,
    factor: float = 1.0,
) -> float:
    """读消息 + 打字的总延迟，单位秒。返回 0 表示禁用。

    ``factor`` 来自作息：上课或睡觉时整体放慢，封顶也随之放宽。
    """
    cfg = runtime.cfg.humanize
    if not bool(cfg.enable_typing_delay):
        return 0.0
    read_base = float(cfg.read_base_seconds)
    read_per  = float(cfg.read_per_char_seconds)
    type_per  = float(cfg.type_per_char_seconds)
    jitter    = float(cfg.jitter_ratio)
    cap       = float(cfg.max_total_delay_seconds)
    in_len    = len(str(input_text or ""))
    out_len   = len(str(output_text or ""))
    scale     = max(1.0, float(factor))
    raw       = ((read_base + read_per * in_len) + (type_per * out_len)) * scale
    raw       = _jittered(raw, jitter)
    if cap > 0:
        raw = min(raw, cap * scale)
    return max(0.0, raw)


def _compute_interbubble_delay(runtime: ChatRuntime) -> float:
    cfg = runtime.cfg.humanize
    if not bool(cfg.enable_typing_delay):
        return 0.0
    lo = max(0.0, float(cfg.interbubble_min_seconds))
    hi = max(lo, float(cfg.interbubble_max_seconds))
    if hi <= 0:
        return 0.0
    return random.uniform(lo, hi)


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
    """单次收尾生命周期使用的输入。"""

    prepared  : PreparedSmalltalkTurn
    generated : GeneratedSmalltalkTurn
    event     : dict[str, Any]
    context   : Any
    hctx      : HandlerContext
    started_at: float


async def _record_bot_reply(
    context,
    runtime,
    state,
    chat_id: str,
    bot_name: str,
    user_local_id: str,
    *,
    forced: bool,
    action_str: str,
    reasoning: str,
    detail: dict[str, Any],
    parts: tuple[dict[str, Any], ...] | list[dict[str, Any]] | None = None,
    message_ids: tuple[Any, ...]                                    = (),
) -> list[Any]:
    """将助手回复写入记忆和动作历史，并返回最新历史快照。

    ``message_ids`` 是这条回复发出后的全部 QQ 消息 ID（来自发送回执），和记录一起写入。
    调用方必须已经持有 ``chat_id`` 对应的异步锁。
    """
    assistant_local_id = allocate_local_id(chat_id)
    state.memory_store.append(
        chat_id,
        role        = "assistant",
        name        = bot_name,
        local_id    = assistant_local_id,
        parts       = parts,
        message_ids = message_ids,
    )
    schedule_memory_persist(context, runtime, chat_id=chat_id)
    log_step(
        context,
        runtime,
        chat_id = chat_id,
        step    = "smalltalk.memory.append_bot",
        fields  = {"local_id": assistant_local_id},
    )
    history_snapshot: list[Any] = await state.memory_store.get_async(chat_id)
    record_reply_frequency(chat_id, runtime, state, forced=forced)
    await state.heartflow.on_bot_reply_async(chat_id=chat_id)
    state.inc_stats(chat_id, "replies")
    state.action_history.append(
        chat_id,
        ActionRecord(
            ts           = time.time(),
            local_target = user_local_id,
            action       = action_str,
            reasoning    = reasoning,
            detail       = detail,
            executed     = True,
        ),
    )
    schedule_action_history_flush(context, runtime, chat_id=chat_id)
    return history_snapshot


def _build_reply_payload(
    runtime,
    generated: GeneratedSmalltalkTurn,
    *,
    brain_chat_active: bool,
    display_parts: tuple[dict[str, Any], ...] | list[dict[str, Any]] | None,
) -> ReplyPayload | None:
    normalized_display_parts = normalize_message_parts(display_parts)
    if not generated.reply and not normalized_display_parts:
        return None

    reply_for_send = (
        maybe_add_mode_indicator(generated.reply, runtime) if brain_chat_active else generated.reply
    )
    send_parts = prefix_reply_parts(
        normalized_display_parts,
        reply_send_prefix(generated.reply, reply_for_send),
    )
    return build_reply_payload_from_parts(send_parts, display_parts=normalized_display_parts)


def _drop_stale_generated_turn(
    finalization: _SmalltalkFinalization,
    *,
    phase: str,
) -> bool:
    """取消并审计在会话内竞争中失效的生成轮次。"""

    generated          = finalization.generated
    latest_local_id    = most_recent_user_local_id(finalization.hctx.chat_id)
    current_generation = get_profile_generation(
        finalization.context.data_dir, finalization.hctx.chat_id
    )
    same_generation = (
        generated.session_generation is None or generated.session_generation == current_generation
    )
    if same_generation and latest_local_id == generated.local_id:
        return False
    generated.cancel_speculative_memory()
    log_step(
        finalization.context,
        finalization.hctx.runtime,
        chat_id = finalization.hctx.chat_id,
        step    = "smalltalk.stale.drop",
        fields  = {
            "phase"          : phase,
            "local_id"       : generated.local_id,
            "latest_local_id": latest_local_id,
        },
    )
    return True


def _prepare_smalltalk_reply_output(finalization: _SmalltalkFinalization):
    """提交前构造展示输出并同步媒体引用。"""

    generated = finalization.generated
    if generated.reply:
        reply_display_parts     = normalize_message_parts(generated.reply_parts)
        generated.reply_payload = _build_reply_payload(
            finalization.hctx.runtime,
            generated,
            brain_chat_active = finalization.prepared.brain_chat_active,
            display_parts     = reply_display_parts,
        )
    else:
        reply_display_parts     = ()
        generated.reply_payload = None
    reply_parts = sync_message_parts_to_registry(
        finalization.hctx.state,
        reply_display_parts,
        context = finalization.context,
        runtime = finalization.hctx.runtime,
    )
    generated.reply_parts = reply_parts
    return reply_parts


async def _apply_generated_pfc_state(finalization: _SmalltalkFinalization) -> bool:
    """由持有会话锁的调用方提交生成后的规划器快照。"""

    if finalization.prepared.forced or finalization.generated.pfc_state_snapshot is None:
        return False
    await commit_planner_snapshot(
        finalization.hctx.state,
        finalization.hctx.runtime,
        finalization.hctx.chat_id,
        finalization.generated.pfc_state_snapshot,
    )
    return True


async def _finalize_no_reply_turn(finalization: _SmalltalkFinalization) -> list[Any]:
    """提交规划器与不回复状态，不创建投递回执。"""

    generated        = finalization.generated
    should_flush_pfc = False
    try:
        async with chat_lock(finalization.hctx.chat_id):
            if _drop_stale_generated_turn(finalization, phase="commit"):
                return []
            should_flush_pfc = await _apply_generated_pfc_state(finalization)
            assert generated.pfc_result is not None
            generated.cancel_speculative_memory()
            await finalization.hctx.state.heartflow.on_no_reply_async(
                chat_id=finalization.hctx.chat_id
            )
            action = str(generated.pfc_result.action or "no_reply").strip() or "no_reply"
            log_step(
                finalization.context,
                finalization.hctx.runtime,
                chat_id = finalization.hctx.chat_id,
                step    = "smalltalk.no_reply",
                fields  = {
                    "reason"    : "pfc_no_reply",
                    "action"    : action,
                    "pfc_reason": generated.pfc_result.reason,
                    "source"    : generated.reply_source,
                },
            )
            reasoning = str(generated.pfc_result.action or "").strip()
            if generated.pfc_result.reason:
                reasoning += f":{generated.pfc_result.reason}"
            finalization.hctx.state.action_history.append(
                finalization.hctx.chat_id,
                ActionRecord(
                    ts           = time.time(),
                    local_target = generated.local_id,
                    action       = action,
                    reasoning    = reasoning,
                    detail       = {"source": generated.reply_source},
                    executed     = True,
                ),
            )
            schedule_action_history_flush(
                finalization.context,
                finalization.hctx.runtime,
                chat_id=finalization.hctx.chat_id,
            )
    finally:
        if should_flush_pfc:
            schedule_pfc_state_flush(
                finalization.context,
                finalization.hctx.runtime,
                chat_id=finalization.hctx.chat_id,
            )
    return []


def _smalltalk_outbound_batches(finalization: _SmalltalkFinalization) -> list[Any]:
    generated        = finalization.generated
    outbound_batches = (
        generated.reply_payload.outbound_batches
        if generated.reply_payload is not None
        else segments(generated.reply)
    )
    if outbound_batches and isinstance(outbound_batches[0], dict):
        outbound_batches = [outbound_batches]
    return list(outbound_batches or [])


async def _record_delivered_reply(
    finalization: _SmalltalkFinalization,
    reply_parts,
    message_ids: tuple[Any, ...],
) -> list[Any]:
    """只记录已经确认对外投递成功的回复。"""

    prepared    = finalization.prepared
    generated   = finalization.generated
    common_args = (
        finalization.context,
        finalization.hctx.runtime,
        finalization.hctx.state,
        finalization.hctx.chat_id,
        finalization.hctx.bot_name,
        generated.local_id,
    )
    if prepared.forced:
        forced     = True
        action_str = "reply"
        reasoning  = f"forced_direct:{prepared.force_reason or 'forced'}"
        detail     = {
            "source"      : "forced",
            "force_reason": prepared.force_reason,
            **media_action_detail(generated.media_marker, reply_parts),
        }
    else:
        assert generated.pfc_result is not None
        forced     = False
        action_str = str(generated.pfc_result.action or "reply").strip() or "reply"
        reasoning  = str(generated.pfc_result.action or "").strip()
        if generated.pfc_result.reason:
            reasoning += f":{generated.pfc_result.reason}"
        detail = {
            "source": generated.reply_source,
            **media_action_detail(generated.media_marker, reply_parts),
        }

    return await _record_bot_reply(
        *common_args,
        forced      = forced,
        action_str  = action_str,
        reasoning   = reasoning,
        detail      = detail,
        parts       = reply_parts,
        message_ids = message_ids,
    )


async def _commit_smalltalk_delivery(
    finalization: _SmalltalkFinalization,
    reply_parts,
    message_ids: tuple[Any, ...] = (),
) -> None:
    """仅在全部外发动作成功后提交内部状态；消息 ID 随记录一起写入。"""

    should_flush_pfc            = False
    history_snapshot: list[Any] = []
    try:
        async with chat_lock(finalization.hctx.chat_id):
            expected = finalization.generated.session_generation
            if (
                expected is not None
                and get_profile_generation(finalization.context.data_dir, finalization.hctx.chat_id)
                != expected
            ):
                return
            latest_local_id = most_recent_user_local_id(finalization.hctx.chat_id)
            if latest_local_id == finalization.generated.local_id:
                should_flush_pfc = await _apply_generated_pfc_state(finalization)
            history_snapshot = await _record_delivered_reply(
                finalization,
                reply_parts,
                message_ids,
            )
    finally:
        if should_flush_pfc:
            schedule_pfc_state_flush(
                finalization.context,
                finalization.hctx.runtime,
                chat_id=finalization.hctx.chat_id,
            )

    spawn_bg_task(
        finalization.context,
        spawn_post_reply_tasks(
            finalization.hctx,
            history_snapshot,
            finalization.event,
        ),
        name=f"post_reply:{finalization.hctx.chat_id}",
    )
    spawn_bg_task(
        finalization.context,
        asyncio.to_thread(
            mark_reply_media_used,
            finalization.context,
            finalization.generated,
        ),
        name=f"reply_media_used:{finalization.hctx.chat_id}",
    )
    if finalization.hctx.runtime.cfg.debug.log_latency:
        log_step(
            finalization.context,
            finalization.hctx.runtime,
            chat_id = finalization.hctx.chat_id,
            step    = "smalltalk.latency",
            fields  = {"latency_s": round(time.monotonic() - finalization.started_at, 3)},
        )
    log_step(
        finalization.context,
        finalization.hctx.runtime,
        chat_id = finalization.hctx.chat_id,
        step    = "smalltalk.done",
        fields  = {
            "elapsed_s"  : round(time.monotonic() - finalization.started_at, 3),
            "reply_chars": len(display_reply_text(finalization.generated)),
            "reply"      : display_reply_text(finalization.generated),
        },
    )


async def _rollback_smalltalk_delivery(
    finalization: _SmalltalkFinalization,
    *,
    batch_count: int,
) -> None:
    finalization.generated.cancel_speculative_memory()
    log_step(
        finalization.context,
        finalization.hctx.runtime,
        chat_id = finalization.hctx.chat_id,
        step    = "smalltalk.delivery.rollback",
        fields  = {"local_id": finalization.generated.local_id, "batches": batch_count},
    )


class _ReplyBubbles:
    """一条回复的各个气泡分别结算，都有了结果再按真实发出的气泡结算整条回复。

    Core 在 ``send_action()`` 返回之前就按传输结果结算动作上的回执。整条回复共用一张
    回执时，第二个气泡明确失败会当场回滚整条回复，结果未知会当场按整条回复提交，插件
    事后再补记已发出的气泡都来不及。所以每个气泡单独领一张回执，结果先记在这里；不再
    发新气泡、并且发出去的都有了结果之后，才结算整条回复，且只结算一次：

    - 每个气泡都确认发出：提交整条回复；
    - 每个气泡都发了，只是有的结果未知：可能已被看到，仍按整条回复记；
    - 只发出了一部分：只记确认发出或结果未知的气泡，没发出去的不算她说过；
    - 一个都没发出去：回滚。

    结算在最后一个有结果的气泡回执的回调里进行，提交失败会记在那张回执上。
    """

    def __init__(self, finalization: _SmalltalkFinalization, reply_parts, batches: list[Any]):
        self._finalization                            = finalization
        self._reply_parts                             = reply_parts
        self._batches                                 = list(batches)
        self._outcomes: dict[int, bool | None]        = {}
        self._message_ids: dict[int, tuple[Any, ...]] = {}
        self._dispatched                              = 0
        self._closed                                  = False
        self._settled                                 = False

    def receipt(self, index: int) -> DeliveryReceipt:
        """第 ``index`` 个气泡交给发送层之前领取它自己的回执。"""

        self._dispatched = max(self._dispatched, index + 1)

        def settle(outcome: bool | None):
            async def callback() -> None:
                self._outcomes[index]    = outcome
                self._message_ids[index] = receipt.message_ids
                await self._maybe_settle()

            return callback

        receipt = DeliveryReceipt(
            expected_actions = 1,
            commit           = settle(True),
            rollback         = settle(False),
            unknown          = settle(None),
        )
        return receipt

    async def close(self) -> None:
        """不再发新气泡；已经发出的都有了结果就立即结算整条回复。"""

        self._closed = True
        try:
            await self._maybe_settle()
        except Exception:
            # 这里没有回执兜底：和回执回调失败一样只记日志，不打断本轮处理。
            logger.exception("xiaoqing_chat reply settlement failed")

    def _seen(self) -> list[int]:
        """确认发出或结果未知（可能已被看到）的气泡，按发送顺序。"""

        return [
            index for index in range(self._dispatched) if self._outcomes.get(index) is not False
        ]

    async def _maybe_settle(self) -> None:
        if self._settled or not self._closed or len(self._outcomes) < self._dispatched:
            return
        # 先占住结算权再 await，之后到达的回调不会重复提交或回滚。
        self._settled = True
        seen          = self._seen()
        if not seen:
            await _rollback_smalltalk_delivery(self._finalization, batch_count=len(self._batches))
            return
        message_ids = tuple(item for index in seen for item in self._message_ids.get(index, ()))
        if len(seen) == len(self._batches):
            parts = self._reply_parts
        else:
            parts = _sent_bubble_parts([self._batches[index] for index in seen])
            log_step(
                self._finalization.context,
                self._finalization.hctx.runtime,
                chat_id = self._finalization.hctx.chat_id,
                step    = "smalltalk.delivery.partial",
                fields  = {
                    "local_id": self._finalization.generated.local_id,
                    "sent"    : len(seen),
                    "batches" : len(self._batches),
                },
            )
        await _commit_smalltalk_delivery(self._finalization, parts, message_ids)


async def _apply_smalltalk_presend_delay(
    finalization: _SmalltalkFinalization,
    *,
    has_outbound_batches: bool,
) -> bool:
    apply_humanize = (
        not finalization.prepared.forced or finalization.hctx.runtime.cfg.humanize.apply_to_forced
    )
    if not apply_humanize or not has_outbound_batches:
        return apply_humanize
    target = _compute_typing_delay(
        finalization.hctx.runtime,
        input_text  = finalization.prepared.text,
        output_text = display_reply_text(finalization.generated),
        factor      = float(finalization.event.get("_xc_delay_factor") or 1.0),
    )
    # 读消息和打字的时间从收到消息算起；理解、规划和生成已经花掉的时间不再重复等待。
    elapsed = max(0.0, time.monotonic() - finalization.started_at)
    delay   = max(0.0, target - elapsed)
    if delay > 0:
        log_step(
            finalization.context,
            finalization.hctx.runtime,
            chat_id = finalization.hctx.chat_id,
            step    = "smalltalk.humanize.delay",
            fields  = {
                "phase"    : "pre_send",
                "delay_s"  : round(delay, 3),
                "target_s" : round(target, 3),
                "elapsed_s": round(elapsed, 3),
            },
        )
        await asyncio.sleep(delay)
    return apply_humanize


def _smalltalk_interbubble_delay(runtime: ChatRuntime, batch: Any) -> float:
    gap         = _compute_interbubble_delay(runtime)
    batch_chars = _batch_text_length(batch)
    if not batch_chars:
        return gap
    type_per = float(runtime.cfg.humanize.type_per_char_seconds)
    return gap + min(1.2, type_per * batch_chars * 0.6)


def _sent_bubble_parts(batches: list[Any]) -> tuple[dict[str, Any], ...]:
    """已经发出的气泡里的文字；气泡之间空一行，和整条回复的记法一致。"""

    texts = (
        "".join(
            str((segment.get("data") or {}).get("text") or "")
            for segment in batch
            if isinstance(segment, dict) and segment.get("type") == "text"
        ).strip()
        for batch in batches
    )
    return cast(
        tuple[dict[str, Any], ...],
        build_text_message_parts("\n\n".join(text for text in texts if text)),
    )


async def _send_smalltalk_intermediate_batches(
    finalization: _SmalltalkFinalization,
    outbound_batches: list[Any],
    bubbles: _ReplyBubbles,
    *,
    apply_humanize: bool,
) -> bool:
    """发送末批之前的所有批次；最后一批由调用方返回给核心层。

    正向 WS 下，Core 会把处理事件期间发出的消息先收起来、处理完再一起发，气泡之间的
    停顿就没了。这时中间的气泡绕过收集器直接发出，停顿才是对方真实看到的停顿；
    其他接入方式没有这个问题，保持原样。

    每个气泡用自己的回执发出；中途停发时整条回复按 :class:`_ReplyBubbles` 的规则结算。
    """

    user_id  = finalization.event.get("user_id")
    group_id = finalization.event.get("group_id")
    direct   = finalization.event.get("_source") == "upstream_ws"
    for index, batch in enumerate(outbound_batches[:-1]):
        async with chat_lock(finalization.hctx.chat_id):
            stale = _drop_stale_generated_turn(finalization, phase="between_batches")
        if stale:
            await bubbles.close()
            return False
        action = build_action(batch, user_id, group_id)
        if not action:
            await bubbles.close()
            return False
        if direct:
            action[ACTION_BYPASS_SINK_KEY] = True
        receipt = bubbles.receipt(index)
        try:
            delivered = await send_with_receipt(finalization.context.send_action, action, receipt)
        except BaseException:
            # Core 已结算时这里不会改写结果；没走到 Core 的按没发出处理。
            await asyncio.shield(receipt.record(False))
            raise
        if delivered is not True:
            await bubbles.close()
            return False
        if apply_humanize or finalization.hctx.runtime.cfg.humanize.interbubble_applies_to_forced:
            gap = _smalltalk_interbubble_delay(finalization.hctx.runtime, batch)
            if gap > 0:
                await asyncio.sleep(gap)
    return True


async def finalize_smalltalk_turn(
    prepared: PreparedSmalltalkTurn,
    generated: GeneratedSmalltalkTurn,
    event: dict[str, Any],
    context: Any,
    hctx: HandlerContext,
    *,
    started_at: float,
) -> list[dict[str, Any]]:
    """完成生成轮次，并把状态提交绑定到投递确认。"""

    finalization = _SmalltalkFinalization(
        prepared   = prepared,
        generated  = generated,
        event      = event,
        context    = context,
        hctx       = hctx,
        started_at = started_at,
    )

    async with chat_lock(finalization.hctx.chat_id):
        if _drop_stale_generated_turn(finalization, phase="before_commit"):
            return []

    reply_parts = _prepare_smalltalk_reply_output(finalization)
    if not generated.reply:
        return await _finalize_no_reply_turn(finalization)

    outbound_batches = _smalltalk_outbound_batches(finalization)
    if not outbound_batches:
        generated.cancel_speculative_memory()
        return []

    bubbles        = _ReplyBubbles(finalization, reply_parts, outbound_batches)
    apply_humanize = await _apply_smalltalk_presend_delay(
        finalization,
        has_outbound_batches=bool(outbound_batches),
    )
    async with chat_lock(finalization.hctx.chat_id):
        if _drop_stale_generated_turn(finalization, phase="after_presend_delay"):
            return []
    last = len(outbound_batches) - 1
    try:
        if last > 0:
            sent = await _send_smalltalk_intermediate_batches(
                finalization,
                outbound_batches,
                bubbles,
                apply_humanize=apply_humanize,
            )
            if not sent:
                return []
            async with chat_lock(finalization.hctx.chat_id):
                stale = _drop_stale_generated_turn(finalization, phase="before_last_batch")
            if stale:
                # 前面的气泡已经发出，最后一个不再发。
                await bubbles.close()
                return []
        receipt = bubbles.receipt(last)
        await bubbles.close()
    except BaseException:
        # 中途被取消时不再发后面的气泡，已经发出的照常结算。
        await asyncio.shield(bubbles.close())
        raise
    return DeliverySegments(outbound_batches[last], receipt)
