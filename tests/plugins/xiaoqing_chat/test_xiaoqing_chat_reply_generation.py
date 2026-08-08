"""回复生成上下文、超限重建和草稿结构。"""

from __future__ import annotations

import tests.helpers.xiaoqing_chat_test_support as _fixture_support
from tests.helpers.xiaoqing_chat_test_support import (
    Any,
    AsyncMock,
    MagicMock,
    Mock,
    SimpleNamespace,
    _complete_test_runtime_config,
    cast,
    patch,
    pytest,
)

mock_context = _fixture_support.mock_context
sample_group_event = _fixture_support.sample_group_event


@pytest.mark.asyncio
async def test_generate_reply_prefers_planner_goal_over_review_override_goal_store(mock_context):
    from contextlib import ExitStack

    from plugins.xiaoqing_chat.llm.prompt_builder import ChatMessage
    from plugins.xiaoqing_chat.llm.reply_checker import ReplyCheckResult
    from plugins.xiaoqing_chat.planning.planned_action import PlannedAction
    from plugins.xiaoqing_chat.reply_generator import _generate_reply_draft

    runtime = SimpleNamespace(
        cfg=SimpleNamespace(
            personality=SimpleNamespace(multiple_reply_style=[], multiple_probability=0.0),
            keyword_reaction=SimpleNamespace(keyword_rules=[], regex_rules=[]),
            goal=SimpleNamespace(enable_goal=True),
            reflection=SimpleNamespace(enable_review_sessions=True),
            debug=SimpleNamespace(show_reply_prompt=False, log_steps=False),
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
            postprocess=SimpleNamespace(),
            rewrite=SimpleNamespace(),
        )
    )
    _complete_test_runtime_config(runtime)

    state = MagicMock()
    state.memory_store.get_recent_async = AsyncMock(return_value=[])
    state.goal_store.get_async = AsyncMock(return_value=SimpleNamespace(goal="复盘会话先问感受"))
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
        reasoning="目标: 帮用户选一家合适的火锅店",
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
                "plugins.xiaoqing_chat.reply_generator.build_policy_block",
                return_value="review-policy",
            )
        )
        stack.enter_context(
            patch(
                "plugins.xiaoqing_chat.reply_generator._build_tool_info_block",
                new=AsyncMock(return_value=""),
            )
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
                new=AsyncMock(return_value=("好的", "/v1/chat/completions")),
            )
        )
        stack.enter_context(
            patch(
                "plugins.xiaoqing_chat.reply_generator.process_llm_response", return_value=["好的"]
            )
        )
        stack.enter_context(
            patch("plugins.xiaoqing_chat.reply_generator.join_reply", return_value="好的")
        )
        stack.enter_context(
            patch(
                "plugins.xiaoqing_chat.reply_generator.build_dialogue_prompt",
                return_value="dialogue",
            )
        )
        mock_check_reply = stack.enter_context(
            patch(
                "plugins.xiaoqing_chat.reply_generator.check_reply",
                new=AsyncMock(
                    return_value=ReplyCheckResult(suitable=True, reason="", need_replan=False)
                ),
            )
        )

        result = await _generate_reply_draft(
            text="那你推荐哪家？",
            event={},
            context=mock_context,
            runtime=cast(Any, runtime),
            state=state,
            forced=False,
            action=action,
            plan_reasoning="目标: 帮用户选一家合适的火锅店",
            is_brain_chat=False,
        )

    assert result is not None
    assert result.text == "好的"
    assert mock_check_reply.await_count == 1
    assert mock_check_reply.await_args is not None
    assert "帮用户选一家合适的火锅店" in mock_check_reply.await_args.kwargs["goal"]


@pytest.mark.asyncio
async def test_generate_reply_rebuilds_memory_context_after_request_too_large(mock_context):
    from contextlib import ExitStack

    from plugins.xiaoqing_chat.llm.llm_client import LLMError
    from plugins.xiaoqing_chat.llm.prompt_builder import ChatMessage
    from plugins.xiaoqing_chat.llm.reply_checker import ReplyCheckResult
    from plugins.xiaoqing_chat.memory.memory import StoredMessage
    from plugins.xiaoqing_chat.planning.planned_action import PlannedAction
    from plugins.xiaoqing_chat.reply_generator import _generate_reply_draft

    runtime = SimpleNamespace(
        cfg=SimpleNamespace(
            personality=SimpleNamespace(multiple_reply_style=[], multiple_probability=0.0),
            keyword_reaction=SimpleNamespace(keyword_rules=[], regex_rules=[]),
            goal=SimpleNamespace(enable_goal=False),
            reflection=SimpleNamespace(enable_review_sessions=False),
            debug=SimpleNamespace(show_reply_prompt=False, log_steps=False),
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
            postprocess=SimpleNamespace(),
            rewrite=SimpleNamespace(),
        )
    )
    _complete_test_runtime_config(runtime)

    state = MagicMock()
    state.memory_store.get_recent_async = AsyncMock(
        return_value=[
            StoredMessage(role="user", content="u1", name="用户", ts=1.0),
            StoredMessage(role="assistant", content="a1", name="小青", ts=2.0),
            StoredMessage(role="user", content="u2", name="用户", ts=3.0),
            StoredMessage(role="assistant", content="a2", name="小青", ts=4.0),
        ]
    )
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
        reasoning="继续回复",
        question="",
        unknown_words=[],
    )
    history_sizes: list[int] = []

    async def fake_build_memory_block(**kwargs):
        history_sizes.append(len(kwargs["history"]))
        return f"memory:{len(kwargs['history'])}"

    responses = [
        LLMError("request_too_large"),
        ("好的", "/v1/chat/completions"),
    ]

    async def fake_chat_completions(**_kwargs):
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

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
                new=AsyncMock(side_effect=fake_build_memory_block),
            )
        )
        stack.enter_context(
            patch(
                "plugins.xiaoqing_chat.reply_generator._build_tool_info_block",
                new=AsyncMock(return_value=""),
            )
        )
        stack.enter_context(
            patch(
                "plugins.xiaoqing_chat.reply_generator.get_brain_chat_max_context", return_value=4
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
                side_effect=lambda **kwargs: [
                    ChatMessage(role="system", content=f"mem={kwargs['memory_block']}"),
                    ChatMessage(role="user", content="u"),
                ],
            )
        )
        stack.enter_context(
            patch(
                "plugins.xiaoqing_chat.reply_generator.chat_completions_with_fallback_paths",
                new=AsyncMock(side_effect=fake_chat_completions),
            )
        )
        stack.enter_context(
            patch(
                "plugins.xiaoqing_chat.reply_generator.process_llm_response", return_value=["好的"]
            )
        )
        stack.enter_context(
            patch("plugins.xiaoqing_chat.reply_generator.join_reply", return_value="好的")
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
                new=AsyncMock(
                    return_value=ReplyCheckResult(suitable=True, reason="", need_replan=False)
                ),
            )
        )

        result = await _generate_reply_draft(
            text="那你推荐哪家？",
            event={},
            context=mock_context,
            runtime=cast(Any, runtime),
            state=state,
            forced=False,
            action=action,
            plan_reasoning="继续回复",
            is_brain_chat=False,
        )

    assert result is not None
    assert result.text == "好的"
    assert history_sizes == [4, 2]


@pytest.mark.asyncio
async def test_generate_reply_prefers_plan_reasoning_goal_when_action_reasoning_has_no_goal(
    mock_context,
):
    from contextlib import ExitStack

    from plugins.xiaoqing_chat.llm.prompt_builder import ChatMessage
    from plugins.xiaoqing_chat.llm.reply_checker import ReplyCheckResult
    from plugins.xiaoqing_chat.planning.planned_action import PlannedAction
    from plugins.xiaoqing_chat.reply_generator import _generate_reply_draft

    runtime = SimpleNamespace(
        cfg=SimpleNamespace(
            personality=SimpleNamespace(multiple_reply_style=[], multiple_probability=0.0),
            keyword_reaction=SimpleNamespace(keyword_rules=[], regex_rules=[]),
            goal=SimpleNamespace(enable_goal=True),
            reflection=SimpleNamespace(enable_review_sessions=False),
            debug=SimpleNamespace(show_reply_prompt=False, log_steps=False),
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
            postprocess=SimpleNamespace(),
            rewrite=SimpleNamespace(),
        )
    )
    _complete_test_runtime_config(runtime)

    state = MagicMock()
    state.memory_store.get_recent_async = AsyncMock(return_value=[])
    state.goal_store.get_async = AsyncMock(return_value=SimpleNamespace(goal="旧目标"))
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
        reasoning="继续回复",
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
                new=AsyncMock(return_value=("好的", "/v1/chat/completions")),
            )
        )
        stack.enter_context(
            patch(
                "plugins.xiaoqing_chat.reply_generator.process_llm_response", return_value=["好的"]
            )
        )
        stack.enter_context(
            patch("plugins.xiaoqing_chat.reply_generator.join_reply", return_value="好的")
        )
        stack.enter_context(
            patch(
                "plugins.xiaoqing_chat.reply_generator.build_dialogue_prompt",
                return_value="dialogue",
            )
        )
        mock_check_reply = stack.enter_context(
            patch(
                "plugins.xiaoqing_chat.reply_generator.check_reply",
                new=AsyncMock(
                    return_value=ReplyCheckResult(suitable=True, reason="", need_replan=False)
                ),
            )
        )

        result = await _generate_reply_draft(
            text="那你推荐哪家？",
            event={},
            context=mock_context,
            runtime=cast(Any, runtime),
            state=state,
            forced=False,
            action=action,
            plan_reasoning="继续回复\n目标: 帮用户选一家合适的火锅店\n焦点: 预算和口味偏好",
            is_brain_chat=False,
        )

    assert result is not None
    assert result.text == "好的"
    assert mock_check_reply.await_args is not None
    assert "帮用户选一家合适的火锅店" in mock_check_reply.await_args.kwargs["goal"]


@pytest.mark.asyncio
async def test_generate_reply_draft_exposes_canonical_text_parts(mock_context):
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
                enable_reply_checker=False,
                enable_llm_checker=False,
                max_repeat_compare=5,
                similarity_threshold=0.9,
                max_assistant_in_row=3,
                max_regen=0,
            ),
            postprocess=ResponsePostProcessConfig(),
            rewrite=SimpleNamespace(),
        )
    )

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
                new=AsyncMock(return_value=("第一句。第二句", "/v1/chat/completions")),
            )
        )

        draft = await _generate_reply_draft(
            text="你好",
            event={"message_id": 1, "user_id": 1},
            context=mock_context,
            runtime=cast(Any, runtime),
            state=state,
            forced=False,
            action=action,
            plan_reasoning="正常回复",
            bot_name="小青",
            secrets=None,
        )

    assert draft is not None
    assert draft.text == "第一句。\n第二句"
    assert draft.text_parts == ("第一句。", "第二句")
    assert [part["kind"] for part in draft.parts] == ["text", "text"]
    assert draft.parts[0]["text"] == "第一句。\n"
    assert draft.parts[1]["text"] == "第二句"
