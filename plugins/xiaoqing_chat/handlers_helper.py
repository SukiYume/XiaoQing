from __future__ import annotations
from typing import Any, TYPE_CHECKING

from .helper_utils import _is_private, _resolve_llm_config
from .task_scheduler import _spawn_bg_task, _schedule_memory_db_save
from .logging_utils import _log_step
from .llm.summarizer import maybe_update_topic_summary
from .expression.bw_message_recorder import extract_and_learn
from .constants import EXPRESSION_LEARN_MIN_INTERVAL, EXPRESSION_LEARN_MIN_MESSAGES
from .memory.review_sessions import maybe_open_goal_strategy_review, maybe_push_session
from .memory.knowledge_extract import maybe_extract_person_facts

if TYPE_CHECKING:
    from .handler_context import HandlerContext


async def _spawn_post_reply_bg_tasks(
    hctx: HandlerContext,
    history_snapshot: list[Any],
    event: dict[str, Any],
) -> None:
    """
    Spawn background tasks after a reply is sent.

    Includes:
    - Topic summarization
    - Expression learning
    - Review session push
    - Fact extraction
    """
    context = hctx.context
    runtime = hctx.runtime
    state = hctx.state
    chat_id = hctx.chat_id
    bot_name = hctx.bot_name
    secrets = hctx.secrets

    bg = _resolve_llm_config(runtime.cfg, secrets, foreground=False)

    # 锁外调度后台任务（不阻塞其他请求）
    if runtime.cfg.summarizer.enable_topic_summarizer and not _is_private(event):

        async def _run_summarizer() -> None:
            await maybe_update_topic_summary(
                data_dir=context.data_dir,
                memory_db=state.memory_db,
                http_session=context.http_session,
                secrets=secrets,
                bot_name=bot_name,
                chat_id=chat_id,
                history=history_snapshot,
                min_messages_per_update=runtime.cfg.summarizer.min_messages_per_update,
                max_cache_topics=runtime.cfg.summarizer.max_cache_topics,
                **bg.to_model_kwargs(),
                **bg.to_dict(),
            )
            _schedule_memory_db_save(context, runtime)

        _spawn_bg_task(context, _run_summarizer(), name=f"summarizer:{chat_id}")
        _log_step(context, runtime, chat_id=chat_id, step="smalltalk.summarizer.spawn", fields={})

    if runtime.cfg.expression.enable_expression_learning:
        _spawn_bg_task(
            context,
            extract_and_learn(
                context=context,
                secrets=secrets,
                bot_name=bot_name,
                chat_id=chat_id,
                memory_store=state.memory_store,
                expr_store=state.bw_expr_store,
                jargon_store=state.bw_jargon_store,
                recorder=state.bw_recorder,
                personality=runtime.cfg.personality,
                min_interval_seconds=EXPRESSION_LEARN_MIN_INTERVAL,
                min_messages=EXPRESSION_LEARN_MIN_MESSAGES,
                self_reflect=True,
                **bg.to_model_kwargs(),
                **bg.to_dict(),
            ),
            name=f"expression_learn:{chat_id}",
        )
        _log_step(
            context, runtime, chat_id=chat_id, step="smalltalk.expression_learn.spawn", fields={}
        )

    if runtime.cfg.reflection.enable_review_sessions:
        state.review_store.cleanup_expired()
        recent = await state.action_history.get_recent_async(chat_id, max_items=8)
        rej_cnt = sum(1 for r in recent if getattr(r, "action", "") == "reply_rejected")
        stats = f"最近拒绝{rej_cnt}次，连续回复{state.get_continuous_reply_count(chat_id)}"
        g = (await state.goal_store.get_async(chat_id)).goal if runtime.cfg.goal.enable_goal else ""
        if rej_cnt > 0 or state.get_continuous_reply_count(chat_id) >= 3:
            sess = maybe_open_goal_strategy_review(
                store=state.review_store,
                chat_id=chat_id,
                goal=g,
                stats=stats,
                timeout_seconds=float(runtime.cfg.reflection.session_timeout_seconds),
                cooldown_seconds=float(runtime.cfg.reflection.session_cooldown_seconds),
            )
        else:
            sess = None
        if sess:
            _spawn_bg_task(
                context,
                maybe_push_session(
                    context=context,
                    store=state.review_store,
                    sess=sess,
                    operator_user_id=int(runtime.cfg.reflection.operator_user_id),
                    operator_group_id=int(runtime.cfg.reflection.operator_group_id),
                    resend_interval_seconds=float(runtime.cfg.reflection.resend_interval_seconds),
                ),
                name=f"review_push:{chat_id}",
            )

    async def _run_fact_extract() -> None:
        await maybe_extract_person_facts(
            data_dir=context.data_dir,
            http_session=context.http_session,
            secrets=secrets,
            memory_db=state.memory_db,
            bot_name=bot_name,
            chat_id=chat_id,
            history=history_snapshot,
            **bg.to_model_kwargs(),
            **bg.to_dict(),
        )
        _schedule_memory_db_save(context, runtime)

    _spawn_bg_task(context, _run_fact_extract(), name=f"facts:{chat_id}")
    _log_step(context, runtime, chat_id=chat_id, step="smalltalk.facts.spawn", fields={})
