"""调度回复成功后的表达学习、复盘、未完话头结算和生活记录任务。

关于人和群的长期记忆由统一记忆从事件日志里每晚整理，这里不再提取人物事实。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from core.log import LogEvent

from .chat_runtime import llm_call_config
from .constants import EXPRESSION_LEARN_MIN_INTERVAL, EXPRESSION_LEARN_MIN_MESSAGES
from .expression.bw_message_recorder import extract_and_learn
from .life.extract import extract_life_records
from .life.open_loops import mentions as reply_mentions_loop
from .memory import client as memory_client
from .memory.person_profile import session_is_current
from .memory.review_sessions import maybe_open_goal_strategy_review, maybe_push_session
from .message_parts import render_stored_message
from .task_scheduler import spawn_bg_task

if TYPE_CHECKING:
    from .handler_context import HandlerContext

log = logging.getLogger(__name__)


async def _maybe_push_strategy_review(hctx: HandlerContext) -> None:
    """最近回复被拒或连续回复过多时，开一个目标策略复盘并推送给管理员。"""
    runtime, state, chat_id = hctx.runtime, hctx.state, hctx.chat_id
    reflection = runtime.cfg.reflection
    state.review_store.cleanup_expired()
    recent = await state.action_history.get_recent_async(chat_id, max_items=8)
    rejected_count   = sum(1 for r in recent if r.action == "reply_rejected")
    continuous_count = state.get_continuous_reply_count(chat_id)
    if rejected_count == 0 and continuous_count < 3:
        return
    goal = (await state.goal_store.get_async(chat_id)).goal if runtime.cfg.goal.enable_goal else ""
    sess = maybe_open_goal_strategy_review(
        store            = state.review_store,
        chat_id          = chat_id,
        goal             = goal,
        stats            = f"最近拒绝{rejected_count}次，连续回复{continuous_count}",
        timeout_seconds  = float(reflection.session_timeout_seconds),
        cooldown_seconds = float(reflection.session_cooldown_seconds),
        max_pending      = int(reflection.max_pending),
    )
    if not sess:
        return
    spawn_bg_task(
        hctx.context,
        maybe_push_session(
            context                 = hctx.context,
            store                   = state.review_store,
            sess                    = sess,
            operator_user_id        = int(reflection.operator_user_id),
            operator_group_id       = int(reflection.operator_group_id),
            resend_interval_seconds = float(reflection.resend_interval_seconds),
        ),
        name=f"review_push:{chat_id}",
    )


def spawn_life_extraction(
    context: Any,
    runtime: Any,
    state: Any,
    *,
    chat_id: str,
    bot_name: str,
    secrets: dict[str, Any],
    history: Sequence[Any],
    source: str,
) -> None:
    """角色的一条发言确认发出后，记下她说过的近况与承诺、群友提到的待办时间点。

    ``history`` 的最后一条必须是这条角色发言。
    """

    life = runtime.cfg.life
    if not (life.enable_self_timeline or life.enable_open_loops) or not history:
        return
    spawn_bg_task(
        context,
        extract_life_records(
            context  = context,
            state    = state,
            life_cfg = life,
            secrets  = secrets,
            llm=llm_call_config(runtime.cfg, foreground=False),
            chat_id  = chat_id,
            bot_name = bot_name,
            history  = list(history),
            source   = source,
        ),
        name=f"life_extract:{chat_id}",
    )


async def _settle_surfaced_open_loop(
    hctx: HandlerContext, history: Sequence[Any], event: dict[str, Any]
) -> None:
    """这一轮提示过的未完话头：真的问出口了就记为已问，对方答过的那一轮之后结束。"""

    surfaced = event.get("_xc_open_loop_surfaced")
    if not isinstance(surfaced, tuple) or len(surfaced) != 2 or not history:
        return
    loop, mode = surfaced
    if mode == "review":
        op = {"op": "close", "id": loop.loop_id, "status": "done"}
    elif reply_mentions_loop(render_stored_message(history[-1]), loop):
        op = {
            "op"  : "update",
            "id"  : loop.loop_id,
            "data": {"loop": "asked", "asked_at": time.time()},
        }
    else:
        # 换了说法问的，由随后的生活记录抽取认出并记为已问。
        op = {"op": "update", "id": loop.loop_id, "data": {"surfaced": loop.surfaced + 1}}
    await memory_client.push_pending(
        hctx.context, hctx.state, hctx.chat_id, bot_name=hctx.bot_name, ops=[op]
    )


async def spawn_post_reply_tasks(
    hctx: HandlerContext,
    history_snapshot: list[Any],
    event: dict[str, Any],
) -> None:
    """回复发送后启动表达学习、复盘推送、话头结算和生活记录抽取。"""
    context  = hctx.context
    runtime  = hctx.runtime
    state    = hctx.state
    chat_id  = hctx.chat_id
    bot_name = hctx.bot_name
    secrets  = hctx.secrets
    if not session_is_current(context.data_dir, chat_id, event.get("_xc_session_generation")):
        return

    bg = llm_call_config(runtime.cfg, foreground=False)

    # 锁外调度后台任务（不阻塞其他请求）
    if runtime.cfg.expression.enable_expression_learning:
        spawn_bg_task(
            context,
            extract_and_learn(
                context              = context,
                secrets              = secrets,
                bot_name             = bot_name,
                chat_id              = chat_id,
                memory_store         = state.memory_store,
                expr_store           = state.bw_expr_store,
                jargon_store         = state.bw_jargon_store,
                recorder             = state.bw_recorder,
                personality          = runtime.cfg.personality,
                min_interval_seconds = EXPRESSION_LEARN_MIN_INTERVAL,
                min_messages         = EXPRESSION_LEARN_MIN_MESSAGES,
                self_reflect         = True,
                max_store            = int(runtime.cfg.expression.max_store),
                llm                  = bg,
            ),
            name=f"expression_learn:{chat_id}",
        )
        log.info(LogEvent("smalltalk.expression_learn.spawn", chat_id=chat_id))

    if runtime.cfg.reflection.enable_review_sessions:
        await _maybe_push_strategy_review(hctx)

    await _settle_surfaced_open_loop(hctx, history_snapshot, event)
    if history_snapshot and history_snapshot[-1].role == "assistant":
        spawn_life_extraction(
            context,
            runtime,
            state,
            chat_id  = chat_id,
            bot_name = bot_name,
            secrets  = secrets,
            history  = history_snapshot,
            source   = "chat",
        )
