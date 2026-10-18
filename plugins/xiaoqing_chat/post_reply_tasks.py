"""调度回复成功后的摘要、学习、复盘和事实提取任务。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .chat_event import is_private_event
from .chat_runtime import llm_call_config
from .constants import EXPRESSION_LEARN_MIN_INTERVAL, EXPRESSION_LEARN_MIN_MESSAGES
from .expression.bw_message_recorder import extract_and_learn
from .llm.summarizer import maybe_update_topic_summary
from .logging_utils import log_step
from .memory.knowledge_extract import maybe_extract_person_facts
from .memory.person_profile import get_profile_generation
from .memory.review_sessions import maybe_open_goal_strategy_review, maybe_push_session
from .task_scheduler import schedule_memory_db_save, spawn_bg_task

if TYPE_CHECKING:
    from .handler_context import HandlerContext


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


async def spawn_post_reply_tasks(
    hctx: HandlerContext,
    history_snapshot: list[Any],
    event: dict[str, Any],
) -> None:
    """回复发送后启动主题摘要、表达学习、复盘推送和事实提取任务。"""
    context    = hctx.context
    runtime    = hctx.runtime
    state      = hctx.state
    chat_id    = hctx.chat_id
    bot_name   = hctx.bot_name
    secrets    = hctx.secrets
    generation = event.get(
        "_xc_session_generation", get_profile_generation(context.data_dir, chat_id)
    )
    if generation != get_profile_generation(context.data_dir, chat_id):
        return

    bg = llm_call_config(runtime.cfg, foreground=False)

    # 锁外调度后台任务（不阻塞其他请求）
    if runtime.cfg.summarizer.enable_topic_summarizer and not is_private_event(event):

        async def _run_summarizer() -> None:
            await maybe_update_topic_summary(
                data_dir                = context.data_dir,
                memory_db               = state.memory_db,
                secrets                 = secrets,
                bot_name                = bot_name,
                chat_id                 = chat_id,
                history                 = history_snapshot,
                min_messages_per_update = runtime.cfg.summarizer.min_messages_per_update,
                max_cache_topics        = runtime.cfg.summarizer.max_cache_topics,
                expected_generation     = generation,
                llm                     = bg,
            )
            schedule_memory_db_save(context, runtime)

        spawn_bg_task(context, _run_summarizer(), name=f"summarizer:{chat_id}")
        log_step(context, runtime, chat_id=chat_id, step="smalltalk.summarizer.spawn", fields={})

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
        log_step(
            context, runtime, chat_id=chat_id, step="smalltalk.expression_learn.spawn", fields={}
        )

    if runtime.cfg.reflection.enable_review_sessions:
        await _maybe_push_strategy_review(hctx)

    async def _run_fact_extract() -> None:
        await maybe_extract_person_facts(
            data_dir            = context.data_dir,
            secrets             = secrets,
            memory_db           = state.memory_db,
            chat_id             = chat_id,
            history             = history_snapshot,
            expected_generation = generation,
            llm                 = bg,
        )
        schedule_memory_db_save(context, runtime)

    spawn_bg_task(context, _run_fact_extract(), name=f"facts:{chat_id}")
    log_step(context, runtime, chat_id=chat_id, step="smalltalk.facts.spawn", fields={})
