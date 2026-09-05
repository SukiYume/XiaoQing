"""回复生成器在复核超时和草稿缺失时的降级行为。"""

from __future__ import annotations

from contextlib import ExitStack

import tests.helpers.xiaoqing_chat_test_support as _fixture_support
from tests.helpers.xiaoqing_chat_test_support import (
    AsyncMock,
    MagicMock,
    Mock,
    SimpleNamespace,
    _complete_test_runtime_config,
    asyncio,
    patch,
    pytest,
)

mock_context = _fixture_support.mock_context


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["always", "risk"])
@pytest.mark.parametrize("failure", ["timeout", "error", "protocol"])
async def test_checker_infrastructure_failure_respects_review_mode(monkeypatch, mode, failure):
    from plugins.xiaoqing_chat import reply_generator as generator
    from plugins.xiaoqing_chat.config.config import XiaoQingChatConfig
    from plugins.xiaoqing_chat.llm.reply_checker import ReplyCheckResult

    cfg                              = XiaoQingChatConfig()
    cfg.reply_check.llm_checker_mode = mode
    plan                             = SimpleNamespace(
        runtime=SimpleNamespace(cfg=cfg),
        context=SimpleNamespace(http_session=None),
        chat_id            = "test",
        bot_name           = "小青",
        text               = "随便聊聊",
        event              = {},
        secrets            = {},
        effective_goal     = "聊天",
        merged_reasoning   = "",
        policy_block       = "",
        effective_identity = "",
        state_text         = "",
        profile_block      = "",
        forced             = True,
    )
    remote = AsyncMock(
        side_effect=TimeoutError()
        if failure == "timeout"
        else RuntimeError()
        if failure == "error"
        else None,
        return_value=ReplyCheckResult(True, "检查不可用", False, "infra"),
    )
    monkeypatch.setattr(generator, "check_reply", remote)
    monkeypatch.setattr(generator, "_log_step", Mock())
    draft    = generator._build_reply_draft(("未经审查的候选。",))
    rejected = await generator._check_candidate_draft(plan, draft, [], "")
    if mode == "risk":
        assert rejected is None
        return
    assert rejected.result.suitable is False
    assert rejected.result.failure_code == "checker_unavailable"
    assert rejected.allow_after_regen_exhausted is False
    attempt = SimpleNamespace(regen_used=0)
    assert not generator._queue_reply_regeneration(plan, attempt, rejected, step="test")
    assert attempt.regen_used == 0
    assert (
        generator._finish_rejected_candidate(plan, rejected).text
        == "这条回复暂时没准备好，稍后再试。"
    )
    plan.forced = False
    assert generator._finish_rejected_candidate(plan, rejected) is None
    remote.assert_awaited_once()


@pytest.mark.asyncio
async def test_generate_reply_checker_timeout_allows_non_forced_reply(mock_context):
    from plugins.xiaoqing_chat.llm.prompt_builder import ChatMessage
    from plugins.xiaoqing_chat.planning.planned_action import PlannedAction
    from plugins.xiaoqing_chat.reply_generator import _generate_reply_draft

    runtime = SimpleNamespace(
        cfg=SimpleNamespace(
            personality=SimpleNamespace(
                multiple_reply_style = [],
                multiple_probability = 0.0,
                identity             = "",
                reply_style          = "",
            ),
            keyword_reaction=SimpleNamespace(keyword_rules=[], regex_rules=[]),
            brain_chat=SimpleNamespace(
                brain_identity         = "",
                brain_reply_style      = "",
                brain_max_context_size = None,
                brain_temperature      = None,
            ),
            goal=SimpleNamespace(enable_goal=False),
            reflection=SimpleNamespace(enable_review_sessions=False),
            debug=SimpleNamespace(show_reply_prompt=False, log_steps=False),
            max_context_size = 20,
            temperature      = 0.7,
            top_p            = 0.9,
            max_tokens       = 128,
            timeout_seconds  = 3.0,
            reply_check      = SimpleNamespace(
                enable_reply_checker = True,
                enable_llm_checker   = False,
                max_repeat_compare   = 5,
                similarity_threshold = 0.9,
                max_assistant_in_row = 3,
                max_regen            = 0,
            ),
            postprocess = SimpleNamespace(),
            rewrite     = SimpleNamespace(),
        )
    )
    _complete_test_runtime_config(runtime)
    runtime.cfg.reply_check.llm_checker_mode = "risk"
    state                                    = MagicMock()
    state.memory_store.get_recent_async = AsyncMock(return_value=[])
    state.goal_store.get_async = AsyncMock(return_value=SimpleNamespace(goal=""))
    state.review_store.bind = Mock()
    state.inc_stats         = Mock()

    fg = SimpleNamespace(
        timeout_seconds        = 3.0,
        max_retry              = 0,
        retry_interval_seconds = 0.2,
        to_dict                = lambda: {
            "timeout_seconds": 3.0,
            "max_retry": 0,
            "retry_interval_seconds": 0.2,
        },
    )
    action = PlannedAction(
        action        = "reply",
        think_level   = 1,
        reasoning     = "正常回复",
        question      = "",
        unknown_words = [],
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
            patch("plugins.xiaoqing_chat.reply_generator._build_memory_block", return_value="")
        )
        stack.enter_context(
            patch("plugins.xiaoqing_chat.reply_generator._build_tool_info_block", return_value="")
        )
        stack.enter_context(
            patch("plugins.xiaoqing_chat.reply_generator.build_policy_block", return_value="")
        )
        stack.enter_context(
            patch(
                "plugins.xiaoqing_chat.reply_generator.build_prompt_messages",
                return_value=[ChatMessage(role="user", content="hi")],
            )
        )
        stack.enter_context(
            patch(
                "plugins.xiaoqing_chat.reply_generator.chat_completions_with_fallback_paths",
                new=AsyncMock(return_value=("这条回复需要检查", "")),
            )
        )
        stack.enter_context(
            patch(
                "plugins.xiaoqing_chat.reply_generator.process_llm_response",
                return_value=("这条回复需要检查", "", []),
            )
        )
        stack.enter_context(
            patch(
                "plugins.xiaoqing_chat.reply_generator.check_reply",
                new=AsyncMock(side_effect=asyncio.TimeoutError()),
            )
        )

        draft = await _generate_reply_draft(
            text           = "你好",
            event          = {"message_id": 1, "user_id": 1},
            context        = mock_context,
            runtime        = runtime,
            state          = state,
            forced         = False,
            action         = action,
            plan_reasoning = "",
            bot_name       = "小青",
            secrets        = None,
        )

    assert draft is not None
    assert draft.text == "这条回复需要检查"


@pytest.mark.asyncio
async def test_generate_reply_result_handles_missing_draft():
    from plugins.xiaoqing_chat.handlers import _generate_reply_result

    draft_mock = AsyncMock(return_value=None)

    with patch("plugins.xiaoqing_chat.handlers._generate_reply_draft", new=draft_mock):
        text, parts, media_marker = await _generate_reply_result(text="你好")

    assert text == ""
    assert parts == ()
    assert media_marker is None


def test_context_grounding_fallback_uses_current_explicit_opinion(mock_context) -> None:
    from types import SimpleNamespace

    from plugins.xiaoqing_chat.llm.reply_checker import ReplyCheckResult
    from plugins.xiaoqing_chat.reply_generator import (
        _finish_rejected_candidate,
        _RejectedCandidate,
    )

    plan = SimpleNamespace(
        forced     = True,
        request_id = "opinion-fallback",
        text       = "我觉得所有方案只要加缓存就都能变快，你必须同意我。",
        history    = [],
        bot_name   = "小青",
        chat_id    = "group:1",
        context    = mock_context,
        runtime    = SimpleNamespace(
            cfg=SimpleNamespace(
                debug=SimpleNamespace(log_steps=False),
                reply_check=SimpleNamespace(
                    max_repeat_compare   = 8,
                    similarity_threshold = 0.92,
                    max_assistant_in_row = 3,
                ),
            )
        ),
    )
    rejected = _RejectedCandidate(
        text   = "一条被上下文证据检查拒绝的候选",
        result = ReplyCheckResult(
            suitable     = False,
            reason       = "上下文证据不足",
            need_replan  = True,
            severity     = "hard",
            failure_code = "context_grounding",
        ),
    )

    draft = _finish_rejected_candidate(plan, rejected)

    assert draft is not None
    assert draft.text == "这个我不完全同意，所有方案只要加缓存就都能变快说得太满了。"
