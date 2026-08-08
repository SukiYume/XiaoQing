"""回复检查异常与内部重置的任务生命周期。"""

from __future__ import annotations

import tests.helpers.xiaoqing_chat_test_support as _fixture_support
from tests.helpers.xiaoqing_chat_test_support import (
    AsyncMock,
    MagicMock,
    Mock,
    SimpleNamespace,
    _complete_test_runtime_config,
    _make_hctx,
    _set_context_principal,
    asyncio,
    patch,
    pytest,
)

mock_context = _fixture_support.mock_context
sample_group_event = _fixture_support.sample_group_event


@pytest.mark.asyncio
async def test_generate_reply_checker_unexpected_error_logs_error_not_timeout(mock_context):
    from contextlib import ExitStack

    from plugins.xiaoqing_chat.config.config import ResponsePostProcessConfig
    from plugins.xiaoqing_chat.llm.prompt_builder import ChatMessage
    from plugins.xiaoqing_chat.planning.planned_action import PlannedAction
    from plugins.xiaoqing_chat.reply_generator import _generate_reply_draft

    runtime = SimpleNamespace(
        cfg=SimpleNamespace(
            personality=SimpleNamespace(multiple_reply_style=[], multiple_probability=0.0),
            keyword_reaction=SimpleNamespace(keyword_rules=[], regex_rules=[]),
            goal=SimpleNamespace(enable_goal=False),
            reflection=SimpleNamespace(enable_review_sessions=False),
            debug=SimpleNamespace(show_reply_prompt=False, log_steps=False),
            max_context_size=20,
            top_p=0.9,
            max_tokens=256,
            timeout_seconds=3.0,
            reply_check=SimpleNamespace(
                enable_reply_checker=True,
                enable_llm_checker=False,
                max_repeat_compare=5,
                similarity_threshold=0.9,
                max_assistant_in_row=3,
                max_regen=0,
            ),
            postprocess=ResponsePostProcessConfig(),
        )
    )
    _complete_test_runtime_config(runtime)

    state = MagicMock()
    state.memory_store.get_recent_async = AsyncMock(return_value=[])
    state.goal_store.get_async = AsyncMock(return_value=SimpleNamespace(goal=""))
    state.review_store.bind = Mock()
    state.inc_stats = Mock()

    fg = SimpleNamespace(
        timeout_seconds=3.0,
        max_retry=0,
        retry_interval_seconds=0.2,
        to_dict=lambda: {
            "timeout_seconds": 3.0,
            "max_retry": 0,
            "retry_interval_seconds": 0.2,
        },
    )
    action = PlannedAction(
        action="reply",
        think_level=1,
        reasoning="正常回复",
        question="",
        unknown_words=[],
    )

    with ExitStack() as stack:
        stack.enter_context(
            patch("plugins.xiaoqing_chat.reply_generator._get_bot_name", return_value="小青")
        )
        stack.enter_context(
            patch("plugins.xiaoqing_chat.reply_generator._chat_id", return_value="g1")
        )
        stack.enter_context(
            patch("plugins.xiaoqing_chat.reply_generator._is_private", return_value=False)
        )
        stack.enter_context(
            patch(
                "plugins.xiaoqing_chat.reply_generator._get_ai_route_context",
                return_value={"api_base": "http://test", "api_key": "key", "model": "test-model"},
            )
        )
        stack.enter_context(
            patch("plugins.xiaoqing_chat.reply_generator._resolve_llm_config", return_value=fg)
        )
        stack.enter_context(
            patch("plugins.xiaoqing_chat.reply_generator._build_profile_block", return_value="")
        )
        stack.enter_context(
            patch("plugins.xiaoqing_chat.reply_generator._build_knowledge_block", return_value="")
        )
        stack.enter_context(
            patch("plugins.xiaoqing_chat.reply_generator._build_expression_block", return_value="")
        )
        stack.enter_context(
            patch(
                "plugins.xiaoqing_chat.reply_generator._build_jargon_explanation", return_value=""
            )
        )
        stack.enter_context(
            patch(
                "plugins.xiaoqing_chat.reply_generator._build_memory_block",
                new=AsyncMock(return_value=""),
            )
        )
        stack.enter_context(
            patch(
                "plugins.xiaoqing_chat.reply_generator._build_tool_info_block",
                new=AsyncMock(return_value=""),
            )
        )
        stack.enter_context(
            patch("plugins.xiaoqing_chat.reply_generator.build_policy_block", return_value="")
        )
        stack.enter_context(
            patch(
                "plugins.xiaoqing_chat.reply_generator.get_brain_chat_max_context", return_value=10
            )
        )
        stack.enter_context(
            patch(
                "plugins.xiaoqing_chat.reply_generator.get_brain_chat_temperature", return_value=0.7
            )
        )
        stack.enter_context(
            patch("plugins.xiaoqing_chat.reply_generator.get_brain_chat_identity", return_value="")
        )
        stack.enter_context(
            patch(
                "plugins.xiaoqing_chat.reply_generator.get_brain_chat_reply_style", return_value=""
            )
        )
        stack.enter_context(
            patch(
                "plugins.xiaoqing_chat.reply_generator.build_prompt_messages",
                return_value=[
                    ChatMessage(role="system", content="s"),
                    ChatMessage(role="user", content="u"),
                ],
            )
        )
        stack.enter_context(
            patch(
                "plugins.xiaoqing_chat.reply_generator.chat_completions_with_fallback_paths",
                new=AsyncMock(return_value=("这条回复需要检查", "/v1/chat/completions")),
            )
        )
        stack.enter_context(
            patch(
                "plugins.xiaoqing_chat.reply_generator.process_llm_response",
                return_value=["这条回复需要检查"],
            )
        )
        stack.enter_context(
            patch(
                "plugins.xiaoqing_chat.reply_generator.join_reply", return_value="这条回复需要检查"
            )
        )
        stack.enter_context(
            patch(
                "plugins.xiaoqing_chat.reply_generator.build_dialogue_prompt",
                return_value="dialogue",
            )
        )
        stack.enter_context(
            patch(
                "plugins.xiaoqing_chat.reply_generator.check_reply",
                new=AsyncMock(side_effect=RuntimeError("checker bug")),
            )
        )
        mock_log_step = stack.enter_context(
            patch("plugins.xiaoqing_chat.reply_generator._log_step")
        )

        draft = await _generate_reply_draft(
            text="你好",
            event={"message_id": 1, "user_id": 1},
            context=mock_context,
            runtime=runtime,
            state=state,
            forced=False,
            action=action,
            plan_reasoning="",
            bot_name="小青",
            secrets=None,
        )
    assert draft is not None
    assert draft.text == "这条回复需要检查"
    assert any(
        call.kwargs.get("step") == "reply.check.error" for call in mock_log_step.call_args_list
    )
    assert not any(
        call.kwargs.get("step") == "reply.check.timeout" for call in mock_log_step.call_args_list
    )


@pytest.mark.asyncio
async def test_handle_internal_reset_cancels_pending_persist_task(
    mock_context, sample_group_event, tmp_path
):
    from contextlib import suppress

    from plugins.xiaoqing_chat.handlers import handle_internal
    from plugins.xiaoqing_chat.runtime_state import ChatRuntimeState
    from plugins.xiaoqing_chat.store_binding import _bind_all_stores

    chat_id = "g67890"
    runtime = MagicMock()
    state = ChatRuntimeState()
    _bind_all_stores(state, tmp_path)

    async def _pending_persist():
        await asyncio.sleep(30)

    persist_task = asyncio.create_task(_pending_persist())
    state.set_persist_task(chat_id, persist_task)

    _set_context_principal(mock_context, sample_group_event, group_role="admin")
    hctx = _make_hctx(runtime=runtime, state=state, context=mock_context, data_dir=tmp_path)
    with patch("plugins.xiaoqing_chat.handlers.HandlerContext.from_event", return_value=hctx):
        result = await handle_internal("重置", "确认", sample_group_event, mock_context)

    assert result == [{"type": "text", "data": {"text": "✅ 已重置会话记忆"}}]
    assert state.get_persist_task(chat_id) is None
    assert persist_task.cancelled()
    with suppress(asyncio.CancelledError):
        await persist_task
