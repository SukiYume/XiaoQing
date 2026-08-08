"""回复完成、事实锚定、定向再生成和安全降级。"""

from __future__ import annotations

import tests.helpers.xiaoqing_chat_test_support as _fixture_support
from tests.helpers.xiaoqing_chat_test_support import (
    AsyncMock,
    MagicMock,
    Mock,
    SimpleNamespace,
    _complete_test_runtime_config,
    _make_hctx,
    asyncio,
    patch,
    pytest,
)

mock_context = _fixture_support.mock_context
sample_group_event = _fixture_support.sample_group_event


@pytest.mark.asyncio
async def test_smalltalk_no_reply_preserves_wait_action_name_in_action_history(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.handlers import _maybe_reply_smalltalk

    lock = asyncio.Lock()
    state = MagicMock()
    state.get_mood_state.return_value = ""
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.memory_store.get_recent_async = AsyncMock(return_value=[])
    state.memory_store.append = Mock()
    state.heartflow.on_user_message_async = AsyncMock()
    state.heartflow.on_bot_reply_async = AsyncMock()
    state.heartflow.on_no_reply_async = AsyncMock()
    state.inc_stats = Mock()
    state.action_history.append = Mock()
    state.pfc_state_store.get_async = AsyncMock(
        return_value=SimpleNamespace(
            chat_id="g67890",
            ignore_until_ts=0.0,
            ended=False,
            last_successful_reply_action="",
            goal_list=[],
            knowledge_list=[],
            planner_fail_ts=[],
            planner_skip_until=0.0,
            updated_at=0.0,
        )
    )
    state.pfc_state_store.set_state = Mock()

    runtime = SimpleNamespace(
        cfg=SimpleNamespace(
            enable_smalltalk=True,
            goal=SimpleNamespace(enable_goal=False),
            reflection=SimpleNamespace(
                enable_expression_reflection=False, enable_review_sessions=False
            ),
            brain_chat=SimpleNamespace(enable_private_brain_chat=False),
            max_context_size=10,
            planner=SimpleNamespace(resolve_think_level=lambda history_len=0: 0),
            personality=SimpleNamespace(states=[], state_probability=0.0),
            debug=SimpleNamespace(log_latency=False),
        )
    )

    hctx = _make_hctx(runtime=runtime, state=state, context=mock_context)
    with (
        patch("plugins.xiaoqing_chat.handlers.HandlerContext.from_event", return_value=hctx),
        patch("plugins.xiaoqing_chat.handlers._get_lock", return_value=lock),
        patch("plugins.xiaoqing_chat.handlers._should_ignore_text", return_value=False),
        patch(
            "plugins.xiaoqing_chat.handlers._ensure_user_message_recorded",
            new=AsyncMock(return_value="u1"),
        ),
        patch("plugins.xiaoqing_chat.handlers._should_reply", new=AsyncMock(return_value=True)),
        patch("plugins.xiaoqing_chat.handlers.is_brain_chat_active", return_value=False),
        patch("plugins.xiaoqing_chat.handlers._build_memory_block", new=AsyncMock(return_value="")),
        patch(
            "plugins.xiaoqing_chat.handlers.run_pfc_once",
            new=AsyncMock(
                return_value=SimpleNamespace(reply="", action="wait", reason="再观察", ended=False)
            ),
        ),
        patch("plugins.xiaoqing_chat.handlers._most_recent_user_local_id", return_value="u1"),
        patch("plugins.xiaoqing_chat.handlers._schedule_memory_persist"),
        patch("plugins.xiaoqing_chat.handlers._schedule_action_history_flush"),
        patch("plugins.xiaoqing_chat.handlers._schedule_pfc_state_flush"),
        patch("plugins.xiaoqing_chat.handlers._log_step"),
    ):
        result = await _maybe_reply_smalltalk("继续看看", sample_group_event, mock_context)

    assert result == []
    record = state.action_history.append.call_args.args[1]
    assert record.action == "wait"


def test_load_latest_topic_and_summary_skips_invalid_tail_entries(tmp_path):
    from plugins.xiaoqing_chat.planning.goal_state import load_latest_topic_and_summary

    data_dir = tmp_path
    chat_id = "goal-parser-dedup"
    summary_path = data_dir / "hippo_memorizer" / f"{chat_id}.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(
        (
            "["
            '{"topic":"火锅选择","summary":"先看预算和口味","updated_at":1},'
            '{"topic":"","summary":""}'
            "]"
        ),
        encoding="utf-8",
    )

    topic, summary = load_latest_topic_and_summary(data_dir, chat_id)

    assert topic == "火锅选择"
    assert summary == "先看预算和口味"


def test_memory_db_bind_clears_previous_store_when_switching_dirs(tmp_path):
    from plugins.xiaoqing_chat.memory.memory_db import MemoryDB

    first_dir = tmp_path / "a"
    second_dir = tmp_path / "b"
    first_dir.mkdir()
    second_dir.mkdir()

    db = MemoryDB()
    db.bind(first_dir)
    db.upsert_text(
        doc_id="doc1",
        text="旧目录里的记忆",
        meta={"type": "knowledge", "global_approved": True},
    )
    assert db.query_global("旧目录", top_k=5, type_filter="knowledge")

    db.bind(second_dir)

    assert db.query_global("旧目录", top_k=5, type_filter="knowledge") == []


@pytest.mark.asyncio
async def test_generate_reply_forced_soft_rejection_uses_last_contextual_candidate(mock_context):
    from contextlib import ExitStack

    from plugins.xiaoqing_chat.llm.reply_checker import ReplyCheckResult
    from plugins.xiaoqing_chat.planning.planned_action import PlannedAction
    from plugins.xiaoqing_chat.reply_generator import _generate_reply_draft

    runtime = SimpleNamespace(
        cfg=SimpleNamespace(
            personality=SimpleNamespace(
                multiple_reply_style=[],
                multiple_probability=0.0,
                identity="",
                reply_style="",
            ),
            keyword_reaction=SimpleNamespace(keyword_rules=[], regex_rules=[]),
            brain_chat=SimpleNamespace(
                brain_identity="",
                brain_reply_style="",
                brain_max_context_size=None,
                brain_temperature=None,
            ),
            goal=SimpleNamespace(enable_goal=False),
            reflection=SimpleNamespace(enable_review_sessions=False),
            debug=SimpleNamespace(show_reply_prompt=False, log_steps=True),
            max_context_size=20,
            temperature=0.7,
            top_p=0.9,
            max_tokens=128,
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
        reasoning="用户要求直接回复",
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
                "plugins.xiaoqing_chat.reply_generator.build_prompt_messages",
                return_value=[
                    SimpleNamespace(role="system", content="system"),
                    SimpleNamespace(role="user", content="user"),
                ],
            )
        )
        stack.enter_context(
            patch(
                "plugins.xiaoqing_chat.reply_generator.chat_completions_with_fallback_paths",
                new=AsyncMock(return_value=("那确实挺难受的", "primary")),
            )
        )
        stack.enter_context(
            patch(
                "plugins.xiaoqing_chat.reply_generator.process_llm_response",
                return_value=["那确实挺难受的"],
            )
        )
        stack.enter_context(
            patch("plugins.xiaoqing_chat.reply_generator.join_reply", return_value="那确实挺难受的")
        )
        stack.enter_context(
            patch(
                "plugins.xiaoqing_chat.reply_generator._heuristic_check",
                return_value=None,
            )
        )
        stack.enter_context(
            patch(
                "plugins.xiaoqing_chat.reply_generator.check_reply",
                new=AsyncMock(
                    return_value=ReplyCheckResult(
                        suitable=False,
                        reason="风格检查器认为不够自然",
                        need_replan=False,
                        severity="soft",
                    )
                ),
            )
        )

        draft = await _generate_reply_draft(
            text="最烦的是做完也没人知道，像悄悄打完一场仗。",
            event={"group_id": 1, "user_id": 2},
            context=mock_context,
            runtime=runtime,
            state=state,
            forced=True,
            action=action,
            plan_reasoning="用户要求直接回复",
        )

    assert draft is not None
    assert draft.text == "那确实挺难受的"
    logged_payloads = [str(call.args[1]) for call in mock_context.logger.info.call_args_list]
    assert any('"step": "reply.check.exhausted.accept"' in payload for payload in logged_payloads)


def test_finish_forced_structural_rejection_uses_safe_fallback() -> None:
    from plugins.xiaoqing_chat.llm.reply_checker import ReplyCheckResult
    from plugins.xiaoqing_chat.reply_generator import (
        _finish_rejected_candidate,
        _RejectedCandidate,
    )

    rejected = _RejectedCandidate(
        text="重复回复",
        result=ReplyCheckResult(
            suitable=False,
            reason="回复与之前机器人消息完全相同",
            need_replan=True,
        ),
    )

    draft = _finish_rejected_candidate(SimpleNamespace(forced=True), rejected)

    assert draft is not None
    assert draft.text == "看到了，我在。"
    assert draft.text != rejected.text


def test_finish_forced_persona_grounding_rejection_keeps_conversation_alive(mock_context) -> None:
    from plugins.xiaoqing_chat.llm.reply_checker import ReplyCheckResult
    from plugins.xiaoqing_chat.reply_generator import (
        _PERSONA_GROUNDING_FALLBACKS,
        _finish_rejected_candidate,
        _RejectedCandidate,
    )

    plan = SimpleNamespace(
        forced=True,
        request_id="grounding-test",
        text="一个要求角色补全未知人物信息的问题",
        history=[],
        bot_name="小青",
        chat_id="group:1",
        context=mock_context,
        runtime=SimpleNamespace(
            cfg=SimpleNamespace(
                debug=SimpleNamespace(log_steps=True),
                reply_check=SimpleNamespace(
                    max_repeat_compare=8,
                    similarity_threshold=0.92,
                    max_assistant_in_row=3,
                ),
            )
        ),
    )
    rejected = _RejectedCandidate(
        text="一条无依据的人物信息",
        result=ReplyCheckResult(
            suitable=False,
            reason="人物陈述没有直接证据",
            need_replan=True,
            failure_code="persona_grounding",
        ),
    )

    draft = _finish_rejected_candidate(plan, rejected)

    assert draft is not None
    assert draft.text in _PERSONA_GROUNDING_FALLBACKS
    assert rejected.text not in draft.text
    logged_payloads = [str(call.args[1]) for call in mock_context.logger.info.call_args_list]
    assert any("reply.check.exhausted.persona_grounding" in item for item in logged_payloads)


def test_finish_persona_grounding_identity_query_uses_only_known_name(mock_context) -> None:
    from plugins.xiaoqing_chat.llm.reply_checker import ReplyCheckResult
    from plugins.xiaoqing_chat.reply_generator import (
        _finish_rejected_candidate,
        _RejectedCandidate,
    )

    plan = SimpleNamespace(
        forced=True,
        request_id="identity-test",
        text="你究竟是谁？",
        history=[],
        bot_name="小青",
        chat_id="group:1",
        context=mock_context,
        runtime=SimpleNamespace(
            cfg=SimpleNamespace(
                debug=SimpleNamespace(log_steps=False),
                reply_check=SimpleNamespace(
                    max_repeat_compare=8,
                    similarity_threshold=0.92,
                    max_assistant_in_row=3,
                ),
            )
        ),
    )
    rejected = _RejectedCandidate(
        text="一条夹带了未知身份细节的回复",
        result=ReplyCheckResult(
            suitable=False,
            reason="人物陈述没有直接证据",
            need_replan=True,
            failure_code="persona_grounding",
        ),
    )

    draft = _finish_rejected_candidate(plan, rejected)

    assert draft is not None
    assert draft.text == "我叫小青。"


@pytest.mark.parametrize(
    ("text", "expected_fragment"),
    [
        (
            "小青，你具体在哪所学校、哪个城市、读什么专业？",
            "具体到现实资料我就不展开",
        ),
        (
            "小青，说说你是个什么样的人，像群友自我介绍。",
            "按公开人设来说",
        ),
    ],
    ids=("precise-profile", "self-intro"),
)
def test_finish_persona_grounding_uses_configured_persona_fallbacks(
    mock_context,
    text,
    expected_fragment,
) -> None:
    from plugins.xiaoqing_chat.llm.reply_checker import ReplyCheckResult
    from plugins.xiaoqing_chat.reply_generator import (
        _finish_rejected_candidate,
        _RejectedCandidate,
    )

    configured_identity = (
        "你叫小青，是一名海边书店店员，喜欢旧唱片；"
        "具体学校、专业、城市没有设定，不主动补成真实资料。"
    )
    plan = SimpleNamespace(
        forced=True,
        request_id="profile-fallback",
        text=text,
        effective_identity=configured_identity,
        history=[],
        bot_name="小青",
        chat_id="group:1",
        context=mock_context,
        runtime=SimpleNamespace(
            cfg=SimpleNamespace(
                debug=SimpleNamespace(log_steps=False),
                reply_check=SimpleNamespace(
                    max_repeat_compare=8,
                    similarity_threshold=0.92,
                    max_assistant_in_row=3,
                ),
            )
        ),
    )
    rejected = _RejectedCandidate(
        text="一条越界人物回复",
        result=ReplyCheckResult(
            suitable=False,
            reason="人物边界不满足",
            need_replan=True,
            failure_code="persona_grounding",
        ),
    )

    draft = _finish_rejected_candidate(plan, rejected)

    assert draft is not None
    assert expected_fragment in draft.text
    assert "海边书店店员" in draft.text
    assert "旧唱片" in draft.text
    assert "不主动补成真实资料" not in draft.text
    assert len(draft.text) <= 120
    assert "住校" not in draft.text
    assert "理工" not in draft.text


def test_finish_context_grounding_uses_varied_evidence_boundary_fallback(mock_context) -> None:
    from plugins.xiaoqing_chat.llm.reply_checker import ReplyCheckResult
    from plugins.xiaoqing_chat.reply_generator import (
        _CONTEXT_GROUNDING_FALLBACKS,
        _finish_rejected_candidate,
        _RejectedCandidate,
    )

    plan = SimpleNamespace(
        forced=True,
        request_id="context-grounding-test",
        text="一个要求猜测他人状态的问题",
        history=[],
        bot_name="小青",
        chat_id="group:1",
        context=mock_context,
        runtime=SimpleNamespace(
            cfg=SimpleNamespace(
                debug=SimpleNamespace(log_steps=False),
                reply_check=SimpleNamespace(
                    max_repeat_compare=8,
                    similarity_threshold=0.92,
                    max_assistant_in_row=3,
                ),
            )
        ),
    )
    rejected = _RejectedCandidate(
        text="一条没有依据的他人状态推断",
        result=ReplyCheckResult(
            suitable=False,
            reason="第三方陈述没有直接证据",
            need_replan=True,
            failure_code="context_grounding",
        ),
    )

    draft = _finish_rejected_candidate(plan, rejected)

    assert draft is not None
    assert draft.text in _CONTEXT_GROUNDING_FALLBACKS
    assert rejected.text not in draft.text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("你以前参加过吗？", True),
        ("你更倾向哪一个？", True),
        ("你现在方便吗？", True),
        ("你觉得这个方案稳不稳？", False),
        ("你觉得你平时会怎么选？", True),
        ("小青，冻饺子怎么煮？", False),
        ("小青，两个列表怎么合并？", False),
        ("小青，你以前参加过吗？", True),
        ("小青多大？", True),
        ("今天还挺热闹的", False),
    ],
)
def test_checker_unavailable_persona_guard_uses_general_syntax(text, expected) -> None:
    from plugins.xiaoqing_chat.reply_generator import (
        _needs_persona_guard_when_checker_unavailable,
    )

    assert _needs_persona_guard_when_checker_unavailable(text, bot_name="小青") is expected


def test_persona_grounding_rejection_gets_one_targeted_regeneration(mock_context) -> None:
    from plugins.xiaoqing_chat.llm.reply_checker import ReplyCheckResult
    from plugins.xiaoqing_chat.reply_generator import (
        _queue_reply_regeneration,
        _RejectedCandidate,
        _ReplyAttemptState,
    )

    plan = SimpleNamespace(
        forced=True,
        context=mock_context,
        chat_id="group:1",
        runtime=SimpleNamespace(
            cfg=SimpleNamespace(
                debug=SimpleNamespace(log_steps=False),
                reply_check=SimpleNamespace(max_regen=1),
            )
        ),
    )
    attempt = _ReplyAttemptState(max_items=10)
    rejected = _RejectedCandidate(
        text="我以前经常这么做",
        result=ReplyCheckResult(
            suitable=False,
            reason="人物经历没有依据",
            need_replan=True,
            failure_code="persona_grounding",
        ),
    )

    queued = _queue_reply_regeneration(
        plan,
        attempt,
        rejected,
        step="reply.check.regen",
    )

    assert queued is True
    assert attempt.regen_used == 1
    assert "普通知识" in attempt.extra_check_hint
    assert "句首称呼" in attempt.extra_check_hint


def test_postprocess_keeps_quoted_sentence_and_closing_quote_together() -> None:
    from plugins.xiaoqing_chat.config.config import ResponsePostProcessConfig
    from plugins.xiaoqing_chat.llm.postprocess import process_llm_response

    parts = process_llm_response(
        "她盯着耳机线说：“你这耳机想泡个澡是吧。”然后把碗推远了。",
        ResponsePostProcessConfig(),
        bot_name="小青",
    )

    assert parts == [
        "她盯着耳机线说：“你这耳机想泡个澡是吧。”",
        "然后把碗推远了。",
    ]
    assert all(part != "”" for part in parts)


def test_proactive_grounding_exhaustion_prefers_silence_to_full_replan(mock_context) -> None:
    from plugins.xiaoqing_chat.llm.reply_checker import ReplyCheckResult
    from plugins.xiaoqing_chat.reply_generator import (
        _finish_rejected_candidate,
        _RejectedCandidate,
    )

    plan = SimpleNamespace(
        forced=False,
        context=mock_context,
        runtime=SimpleNamespace(cfg=SimpleNamespace(debug=SimpleNamespace(log_steps=False))),
        chat_id="group:1",
    )
    rejected = _RejectedCandidate(
        text="一条无依据的生活片段",
        result=ReplyCheckResult(
            suitable=False,
            reason="人物经历没有依据",
            need_replan=True,
            failure_code="persona_grounding",
        ),
    )

    assert _finish_rejected_candidate(plan, rejected) is None
