"""规划、目标和群聊参与。"""

from __future__ import annotations

import tests.helpers.xiaoqing_chat_test_support as _fixture_support
from tests.helpers.xiaoqing_chat_test_support import (
    AsyncMock,
    MagicMock,
    Mock,
    Path,
    SimpleNamespace,
    _ack_reply_delivery,
    _make_hctx,
    _reply_draft,
    _set_context_principal,
    asyncio,
    json,
    patch,
    pytest,
)

mock_context = _fixture_support.mock_context
sample_group_event = _fixture_support.sample_group_event


def test_pfc_state_set_state_refreshes_updated_at():
    from plugins.xiaoqing_chat.planning.pfc_state import PFCConversationState, PFCStateStore

    store = PFCStateStore()
    state = PFCConversationState(chat_id="g1", updated_at=1.0)

    before = state.updated_at
    store.set_state("g1", state)

    assert state.updated_at > before
    assert store._cache["g1"] is state


def test_pfc_dirty_state_survives_cache_pressure_until_delayed_save(tmp_path):
    from plugins.xiaoqing_chat.planning.pfc_state import PFCConversationState, PFCStateStore

    store = PFCStateStore()
    store.bind(tmp_path)
    store._MAX_CACHE_SIZE = 2
    dirty = PFCConversationState(
        chat_id="dirty",
        goal_list=[{"goal": "must survive delayed persistence"}],
    )
    store.set_state("dirty", dirty)

    # A debounced save has not run yet while unrelated conversations fill the cache.
    store.get("clean-1")
    store.get("clean-2")

    assert store._cache["dirty"] is dirty

    store.save("dirty")

    reloaded = PFCStateStore()
    reloaded.bind(tmp_path)
    assert reloaded.get("dirty").goal_list == [{"goal": "must survive delayed persistence"}]
    assert len(store._cache) <= store._MAX_CACHE_SIZE


@pytest.mark.asyncio
async def test_smalltalk_goal_path_uses_async_goal_store(mock_context, sample_group_event):
    from plugins.xiaoqing_chat.handlers import _maybe_reply_smalltalk

    state = MagicMock()
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.memory_store.get_recent_async = AsyncMock(return_value=[])
    state.memory_store.append = Mock()
    state.heartflow.on_user_message_async = AsyncMock()
    state.heartflow.on_bot_reply_async = AsyncMock()
    state.heartflow.on_no_reply_async = AsyncMock()
    state.goal_store.set_async = AsyncMock()
    state.goal_store.set.side_effect = AssertionError("sync goal store write should not be used")
    state.pfc_state_store.get_async = AsyncMock(return_value=SimpleNamespace(goal_list=[]))

    runtime = SimpleNamespace(
        cfg=SimpleNamespace(
            enable_smalltalk=True,
            goal=SimpleNamespace(enable_goal=True),
            reflection=SimpleNamespace(
                enable_expression_reflection=False, enable_review_sessions=False
            ),
            brain_chat=SimpleNamespace(enable_private_brain_chat=False),
            planner=SimpleNamespace(resolve_think_level=lambda history_len=0: 0),
            personality=SimpleNamespace(states=[], state_probability=0.0),
            debug=SimpleNamespace(log_latency=False),
        )
    )
    event = dict(sample_group_event)
    event["_xc_command_forced"] = True

    hctx = _make_hctx(runtime=runtime, state=state, context=mock_context)
    with (
        patch("plugins.xiaoqing_chat.handlers.HandlerContext.from_event", return_value=hctx),
        patch("plugins.xiaoqing_chat.handlers._get_lock", return_value=asyncio.Lock()),
        patch("plugins.xiaoqing_chat.handlers._should_ignore_text", return_value=False),
        patch(
            "plugins.xiaoqing_chat.handlers.derive_goal_async", new=AsyncMock(return_value="目标")
        ),
        patch(
            "plugins.xiaoqing_chat.handlers._ensure_user_message_recorded",
            new=AsyncMock(return_value="u1"),
        ),
        patch("plugins.xiaoqing_chat.handlers.is_brain_chat_active", return_value=False),
        patch(
            "plugins.xiaoqing_chat.handlers._generate_reply_draft",
            new=AsyncMock(return_value=_reply_draft("ok")),
        ),
        patch("plugins.xiaoqing_chat.handlers._most_recent_user_local_id", return_value="u1"),
        patch("plugins.xiaoqing_chat.handlers._spawn_post_reply_bg_tasks", new=AsyncMock()),
        patch("plugins.xiaoqing_chat.handlers._schedule_memory_persist"),
        patch("plugins.xiaoqing_chat.handlers._schedule_action_history_flush"),
        patch("plugins.xiaoqing_chat.handlers._freq_record"),
        patch("plugins.xiaoqing_chat.handlers._log_step"),
        patch(
            "plugins.xiaoqing_chat.handlers.maybe_add_mode_indicator",
            side_effect=lambda reply, runtime: reply,
        ),
    ):
        result = await _maybe_reply_smalltalk("你好", event, mock_context)

    assert result == [{"type": "text", "data": {"text": "ok"}}]
    assert state.goal_store.set_async.await_count == 1


@pytest.mark.asyncio
async def test_should_reply_uses_async_goal_and_heartflow_state():
    from plugins.xiaoqing_chat.frequency_control import _should_reply

    runtime = MagicMock()
    runtime.cfg.goal.enable_goal = True
    runtime.cfg.min_reply_interval_seconds = 0.0
    runtime.cfg.max_replies_per_minute = 0
    runtime.cfg.reply_probability_base = 0.6
    runtime.cfg.heartflow.enable_heartflow = True
    runtime.cfg.heartflow.base_score = 0.2
    runtime.cfg.heartflow.weight_question = 0.12
    runtime.cfg.heartflow.weight_goal_match = 0.06
    runtime.cfg.heartflow.weight_short_text = -0.08
    runtime.cfg.heartflow.weight_no_reply_streak = 0.05
    runtime.cfg.heartflow.weight_long_silence = 0.08

    state = MagicMock()
    state.get_last_reply_ts.return_value = 0.0
    state.get_continuous_cooldown_until.return_value = 0.0
    state.get_reply_timestamps.return_value = []
    state.set_reply_timestamps = Mock()
    state.goal_store.get_async = AsyncMock(return_value=SimpleNamespace(goal="回答用户问题"))
    state.goal_store.get.side_effect = AssertionError("sync goal store read should not be used")
    state.heartflow.score_async = AsyncMock(return_value=0.7)
    state.heartflow.get_async = AsyncMock(return_value=SimpleNamespace(no_reply_streak=0))
    state.heartflow._load.side_effect = AssertionError("sync heartflow load should not be used")

    with patch("plugins.xiaoqing_chat.frequency_control.random.random", return_value=0.0):
        result = await _should_reply(runtime, state, "g1", "你好？", False, False)

    assert result is True
    assert state.goal_store.get_async.await_count == 1
    assert state.heartflow.get_async.await_count == 1


@pytest.mark.asyncio
async def test_ordinary_group_turn_refreshes_goal_after_should_reply_blocks(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.handlers import _maybe_reply_smalltalk

    state = MagicMock()
    call_order: list[str] = []

    async def fake_set_goal(*_args, **_kwargs):
        call_order.append("set_goal")

    async def fake_should_reply(*_args, **_kwargs):
        call_order.append("reply_gate")
        return False

    async def fake_derive_goal(*_args, **_kwargs):
        call_order.append("derive_goal")
        return "保持当前话题"

    state.goal_store.set_async = AsyncMock(side_effect=fake_set_goal)
    state.pfc_state_store.get_async = AsyncMock(return_value=SimpleNamespace(goal_list=[]))

    runtime = SimpleNamespace(
        cfg=SimpleNamespace(
            enable_smalltalk=True,
            goal=SimpleNamespace(enable_goal=True),
            reflection=SimpleNamespace(
                enable_expression_reflection=False, enable_review_sessions=False
            ),
            brain_chat=SimpleNamespace(enable_private_brain_chat=False),
        )
    )

    hctx = _make_hctx(runtime=runtime, state=state, context=mock_context)
    with (
        patch("plugins.xiaoqing_chat.handlers.HandlerContext.from_event", return_value=hctx),
        patch("plugins.xiaoqing_chat.handlers._should_ignore_text", return_value=False),
        patch("plugins.xiaoqing_chat.handlers._is_at_me", return_value=False),
        patch("plugins.xiaoqing_chat.handlers._has_bot_name", return_value=False),
        patch(
            "plugins.xiaoqing_chat.handlers._should_reply",
            new=AsyncMock(side_effect=fake_should_reply),
        ),
        patch(
            "plugins.xiaoqing_chat.handlers.derive_goal_async",
            new=AsyncMock(side_effect=fake_derive_goal),
        ) as mock_derive_goal,
        patch("plugins.xiaoqing_chat.handlers._log_step"),
    ):
        result = await _maybe_reply_smalltalk(
            "今天先同步一下近期排期和联调情况，暂时不需要你立即回复",
            sample_group_event,
            mock_context,
        )

    assert result == []
    mock_derive_goal.assert_awaited_once()
    state.goal_store.set_async.assert_awaited_once()
    assert call_order == ["reply_gate", "derive_goal", "set_goal"]


@pytest.mark.asyncio
async def test_ordinary_group_with_planner_top_goal_skips_heuristic_goal_refresh_before_reply_gate(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.handlers import _maybe_reply_smalltalk

    state = MagicMock()
    state.goal_store.set_async = AsyncMock()
    state.pfc_state_store.get_async = AsyncMock(
        return_value=SimpleNamespace(
            chat_id="g67890",
            ignore_until_ts=0.0,
            ended=False,
            last_successful_reply_action="",
            goal_list=[{"goal": "已有规划目标"}],
            knowledge_list=[],
            planner_fail_ts=[],
            planner_skip_until=0.0,
            updated_at=0.0,
        )
    )

    runtime = SimpleNamespace(
        cfg=SimpleNamespace(
            enable_smalltalk=True,
            goal=SimpleNamespace(enable_goal=True),
            reflection=SimpleNamespace(
                enable_expression_reflection=False, enable_review_sessions=False
            ),
            brain_chat=SimpleNamespace(enable_private_brain_chat=False),
        )
    )

    hctx = _make_hctx(runtime=runtime, state=state, context=mock_context)
    with (
        patch("plugins.xiaoqing_chat.handlers.HandlerContext.from_event", return_value=hctx),
        patch("plugins.xiaoqing_chat.handlers._should_ignore_text", return_value=False),
        patch("plugins.xiaoqing_chat.handlers._is_at_me", return_value=False),
        patch("plugins.xiaoqing_chat.handlers._has_bot_name", return_value=False),
        patch("plugins.xiaoqing_chat.handlers._should_reply", new=AsyncMock(return_value=False)),
        patch(
            "plugins.xiaoqing_chat.handlers.derive_goal_async",
            new=AsyncMock(return_value="保持当前话题"),
        ) as mock_derive_goal,
        patch("plugins.xiaoqing_chat.handlers._log_step"),
    ):
        result = await _maybe_reply_smalltalk(
            "今天先同步一下近期排期和联调情况，暂时不需要你立即回复",
            sample_group_event,
            mock_context,
        )

    assert result == []
    mock_derive_goal.assert_not_awaited()
    state.goal_store.set_async.assert_not_awaited()


@pytest.mark.asyncio
async def test_ordinary_group_with_empty_planner_goal_list_still_refreshes_goal_before_reply_gate(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.handlers import _maybe_reply_smalltalk

    state = MagicMock()
    state.goal_store.set_async = AsyncMock()
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

    runtime = SimpleNamespace(
        cfg=SimpleNamespace(
            enable_smalltalk=True,
            goal=SimpleNamespace(enable_goal=True),
            reflection=SimpleNamespace(
                enable_expression_reflection=False, enable_review_sessions=False
            ),
            brain_chat=SimpleNamespace(enable_private_brain_chat=False),
        )
    )

    hctx = _make_hctx(runtime=runtime, state=state, context=mock_context)
    with (
        patch("plugins.xiaoqing_chat.handlers.HandlerContext.from_event", return_value=hctx),
        patch("plugins.xiaoqing_chat.handlers._should_ignore_text", return_value=False),
        patch("plugins.xiaoqing_chat.handlers._is_at_me", return_value=False),
        patch("plugins.xiaoqing_chat.handlers._has_bot_name", return_value=False),
        patch("plugins.xiaoqing_chat.handlers._should_reply", new=AsyncMock(return_value=False)),
        patch(
            "plugins.xiaoqing_chat.handlers.derive_goal_async",
            new=AsyncMock(return_value="保持当前话题"),
        ) as mock_derive_goal,
        patch("plugins.xiaoqing_chat.handlers._log_step"),
    ):
        result = await _maybe_reply_smalltalk(
            "今天先同步一下近期排期和联调情况，暂时不需要你立即回复",
            sample_group_event,
            mock_context,
        )

    assert result == []
    mock_derive_goal.assert_awaited_once()
    state.goal_store.set_async.assert_awaited_once()


@pytest.mark.asyncio
async def test_ordinary_group_pre_pfc_block_still_updates_no_reply_adaptation(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.handlers import _maybe_reply_smalltalk

    state = MagicMock()
    state.goal_store.set_async = AsyncMock()
    state.heartflow.on_no_reply_async = AsyncMock()
    state.pfc_state_store.get_async = AsyncMock(return_value=SimpleNamespace(goal_list=[]))

    runtime = SimpleNamespace(
        cfg=SimpleNamespace(
            enable_smalltalk=True,
            goal=SimpleNamespace(enable_goal=True),
            reflection=SimpleNamespace(
                enable_expression_reflection=False, enable_review_sessions=False
            ),
            brain_chat=SimpleNamespace(enable_private_brain_chat=False),
        )
    )

    hctx = _make_hctx(runtime=runtime, state=state, context=mock_context)
    with (
        patch("plugins.xiaoqing_chat.handlers.HandlerContext.from_event", return_value=hctx),
        patch("plugins.xiaoqing_chat.handlers._should_ignore_text", return_value=False),
        patch("plugins.xiaoqing_chat.handlers._is_at_me", return_value=False),
        patch("plugins.xiaoqing_chat.handlers._has_bot_name", return_value=False),
        patch("plugins.xiaoqing_chat.handlers._should_reply", new=AsyncMock(return_value=False)),
        patch(
            "plugins.xiaoqing_chat.handlers.derive_goal_async",
            new=AsyncMock(return_value="保持当前话题"),
        ),
        patch("plugins.xiaoqing_chat.handlers._log_step"),
    ):
        result = await _maybe_reply_smalltalk(
            "今天先同步一下近期排期和联调情况，暂时不需要你立即回复",
            sample_group_event,
            mock_context,
        )

    assert result == []
    state.heartflow.on_no_reply_async.assert_awaited_once()


@pytest.mark.asyncio
async def test_expression_reflection_does_not_spawn_before_reply_gate_passes(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.handlers import _maybe_reply_smalltalk

    state = MagicMock()
    state.goal_store.set_async = AsyncMock()
    state.heartflow.on_no_reply_async = AsyncMock()
    state.pfc_state_store.get_async = AsyncMock(return_value=SimpleNamespace(goal_list=[]))

    runtime = SimpleNamespace(
        cfg=SimpleNamespace(
            enable_smalltalk=True,
            goal=SimpleNamespace(enable_goal=True),
            reflection=SimpleNamespace(
                enable_expression_reflection=True,
                enable_review_sessions=False,
                operator_user_id="10001",
                operator_group_id="20001",
                min_interval_seconds=60,
                ask_per_check=1,
            ),
            brain_chat=SimpleNamespace(enable_private_brain_chat=False),
        )
    )

    hctx = _make_hctx(runtime=runtime, state=state, context=mock_context)
    with (
        patch("plugins.xiaoqing_chat.handlers.HandlerContext.from_event", return_value=hctx),
        patch("plugins.xiaoqing_chat.handlers._should_ignore_text", return_value=False),
        patch("plugins.xiaoqing_chat.handlers._is_at_me", return_value=False),
        patch("plugins.xiaoqing_chat.handlers._has_bot_name", return_value=False),
        patch("plugins.xiaoqing_chat.handlers._should_reply", new=AsyncMock(return_value=False)),
        patch(
            "plugins.xiaoqing_chat.handlers.derive_goal_async",
            new=AsyncMock(return_value="保持当前话题"),
        ),
        patch("plugins.xiaoqing_chat.handlers._resolve_llm_config"),
        patch("plugins.xiaoqing_chat.handlers._spawn_bg_task") as mock_spawn_bg_task,
        patch("plugins.xiaoqing_chat.handlers._log_step"),
    ):
        result = await _maybe_reply_smalltalk(
            "今天只是同步状态，不需要立即回复",
            sample_group_event,
            mock_context,
        )

    assert result == []
    mock_spawn_bg_task.assert_not_called()


def test_follow_up_compact_prompt_gives_group_space_after_recent_reply():
    from plugins.xiaoqing_chat.planning.pfc_action_planner import PROMPT_FOLLOW_UP_COMPACT

    assert "默认给其他人留出轮次" in PROMPT_FOLLOW_UP_COMPACT
    assert "不以有没有 @、消息长短或是否包含问号作为单一依据" in PROMPT_FOLLOW_UP_COMPACT


def test_initial_compact_prompt_uses_relevance_and_turn_taking_instead_of_surface_cues():
    from plugins.xiaoqing_chat.planning.pfc_action_planner import PROMPT_INITIAL_REPLY_COMPACT

    assert "消息长度不能单独决定行动" in PROMPT_INITIAL_REPLY_COMPACT
    assert "说话人、目标对象和它与前文的关系" in PROMPT_INITIAL_REPLY_COMPACT
    assert "不自动等于必须回复" in PROMPT_INITIAL_REPLY_COMPACT


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("今晚吃啥啊，我已经纠结半小时了", "open_question"),
        ("你们有没有那种越困越不想睡的时候", "group_invitation"),
        ("我宣布，周一应该从一周里删掉", "opening_banter"),
        ("有没有人跟我站甜粽，咸粽党别打我", "group_invitation"),
        ("阿泽你上次推荐那家店到底好不好吃", ""),
        ("小李，这个文件你看一下", ""),
        ("小李，这个文件放哪里合适？", ""),
        ("哈哈哈哈哈哈", ""),
        ("行，那就这么定了", ""),
    ],
)
def test_group_participation_cue_only_selects_open_group_turns(text, expected):
    from plugins.xiaoqing_chat.planning.pfc_action_planner import _group_participation_cue

    assert _group_participation_cue(text) == expected


@pytest.mark.asyncio
async def test_planner_directly_joins_fresh_open_group_turn(monkeypatch):
    from plugins.xiaoqing_chat.config.config import PersonalityConfig
    from plugins.xiaoqing_chat.planning import pfc_action_planner
    from plugins.xiaoqing_chat.planning.pfc_action_planner import plan_next_action

    captured: dict[str, str] = {}

    async def fake_chat(*_args, **kwargs):
        captured["prompt"] = kwargs["messages"][0]["content"]
        return (
            {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "thinking": "没人点名，先不插话",
                                    "action": "wait",
                                    "reason": "没有直接叫我",
                                    "wait_seconds": 20,
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            },
            "responses",
        )

    monkeypatch.setattr(
        pfc_action_planner,
        "chat_completions_raw_with_fallback_paths",
        fake_chat,
    )
    plan = await plan_next_action(
        secrets={},
        bot_name="小青",
        is_private=False,
        personality=PersonalityConfig(),
        history=[],
        goal_list=[],
        knowledge_list=[],
        action_history_summary="",
        last_action_context="",
        timeout_context="",
        last_successful_reply_action=None,
        temperature=0.7,
        top_p=0.9,
        max_tokens=256,
        timeout_seconds=1.0,
        max_retry=0,
        retry_interval_seconds=0.0,
        current_text="你们有没有那种越困越不想睡的时候",
    )

    assert plan.action == "direct_reply"
    assert plan.reason == "fresh_group_participation:group_invitation"
    assert plan.wait_seconds == 0
    assert captured == {}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("[CQ:face,id=14]", True),
        ("[CQ:face,id=14] ？！", True),
        ("[QQ表情：汪汪大哭]", True),
        ("[CQ:face,id=14] 我也觉得", False),
        ("今晚吃什么？", False),
    ],
)
def test_low_information_group_turn_only_matches_protocol_faces(text, expected):
    from plugins.xiaoqing_chat.participation import is_low_information_group_turn

    assert is_low_information_group_turn(text) is expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("小李，这个文件放哪里合适？", True),
        ("阿泽你看一下这个", True),
        ("大家，这个文件放哪里合适？", False),
        ("这个文件小李放哪里合适？", False),
    ],
)
def test_directed_other_requires_a_clear_leading_addressee(text, expected):
    from plugins.xiaoqing_chat.participation import is_group_turn_directed_to_other

    assert is_group_turn_directed_to_other(text) is expected


@pytest.mark.asyncio
async def test_planner_preserves_wait_for_message_directed_to_someone_else(monkeypatch):
    from plugins.xiaoqing_chat.config.config import PersonalityConfig
    from plugins.xiaoqing_chat.planning import pfc_action_planner
    from plugins.xiaoqing_chat.planning.pfc_action_planner import plan_next_action

    async def fake_chat(*_args, **_kwargs):
        return (
            {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "thinking": "这是在问阿泽",
                                    "action": "wait",
                                    "reason": "消息定向给其他人",
                                    "wait_seconds": 10,
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            },
            "responses",
        )

    monkeypatch.setattr(
        pfc_action_planner,
        "chat_completions_raw_with_fallback_paths",
        fake_chat,
    )
    plan = await plan_next_action(
        secrets={},
        bot_name="小青",
        is_private=False,
        personality=PersonalityConfig(),
        history=[],
        goal_list=[],
        knowledge_list=[],
        action_history_summary="",
        last_action_context="",
        timeout_context="",
        last_successful_reply_action=None,
        temperature=0.7,
        top_p=0.9,
        max_tokens=256,
        timeout_seconds=1.0,
        max_retry=0,
        retry_interval_seconds=0.0,
        current_text="阿泽你上次推荐那家店到底好不好吃",
    )

    assert plan.action == "wait"


@pytest.mark.asyncio
async def test_planner_unavailable_fails_closed_in_group_but_replies_in_private(monkeypatch):
    from plugins.xiaoqing_chat.config.config import PersonalityConfig
    from plugins.xiaoqing_chat.planning import pfc_action_planner
    from plugins.xiaoqing_chat.planning.pfc_action_planner import plan_next_action

    kwargs = {
        "secrets": {"_ai": None},
        "bot_name": "小青",
        "personality": PersonalityConfig(),
        "history": [],
        "goal_list": [],
        "knowledge_list": [],
        "action_history_summary": "",
        "last_action_context": "",
        "timeout_context": "",
        "last_successful_reply_action": None,
        "temperature": 0.7,
        "top_p": 0.9,
        "max_tokens": 256,
        "timeout_seconds": 1.0,
        "max_retry": 0,
        "retry_interval_seconds": 0.0,
    }

    group_plan = await plan_next_action(is_private=False, **kwargs)
    private_plan = await plan_next_action(is_private=True, **kwargs)

    assert group_plan.action == "wait"
    assert private_plan.action == "direct_reply"

    async def _raise_timeout(*_args, **_kwargs):
        raise asyncio.TimeoutError

    monkeypatch.setattr(
        pfc_action_planner,
        "chat_completions_raw_with_fallback_paths",
        _raise_timeout,
    )
    timeout_kwargs = {**kwargs, "secrets": {}}
    group_timeout_plan = await plan_next_action(is_private=False, **timeout_kwargs)
    private_timeout_plan = await plan_next_action(is_private=True, **timeout_kwargs)

    assert group_timeout_plan.action == "wait"
    assert private_timeout_plan.action == "direct_reply"


@pytest.mark.parametrize(
    ("is_private", "expected_action"),
    [(False, "wait"), (True, "direct_reply")],
)
@pytest.mark.asyncio
async def test_planner_unknown_action_uses_scope_safe_fallback(
    monkeypatch,
    is_private: bool,
    expected_action: str,
):
    from plugins.xiaoqing_chat.config.config import PersonalityConfig
    from plugins.xiaoqing_chat.planning import pfc_action_planner
    from plugins.xiaoqing_chat.planning.pfc_action_planner import plan_next_action

    async def fake_chat(*_args, **_kwargs):
        return (
            {"choices": [{"message": {"content": '{"action":"speak_anyway","reason":"bad"}'}}]},
            "responses",
        )

    monkeypatch.setattr(
        pfc_action_planner,
        "chat_completions_raw_with_fallback_paths",
        fake_chat,
    )
    plan = await plan_next_action(
        secrets={},
        bot_name="小青",
        is_private=is_private,
        personality=PersonalityConfig(),
        history=[],
        goal_list=[],
        knowledge_list=[],
        action_history_summary="",
        last_action_context="",
        timeout_context="",
        last_successful_reply_action=None,
        temperature=0.7,
        top_p=0.9,
        max_tokens=256,
        timeout_seconds=1.0,
        max_retry=0,
        retry_interval_seconds=0.0,
    )

    assert plan.action == expected_action
    assert plan.reason == "planner_invalid_action"


def test_low_information_turn_does_not_replace_existing_topic_goal():
    from plugins.xiaoqing_chat.planning.goal_state import (
        _derive_goal_from_context,
        is_low_information_turn,
    )

    assert is_low_information_turn("乐") is True
    assert is_low_information_turn("噔噔咚") is True
    assert is_low_information_turn("[QQ表情：汪汪大哭]") is True
    assert is_low_information_turn("6 公斤每立方厘米") is False
    assert (
        _derive_goal_from_context(
            current_text="乐",
            planner_reasoning="",
            topic="提瓦特物理参数",
        )
        == '围绕话题"提瓦特物理参数"自然聊天'
    )


def test_scientific_numeric_turn_uses_reasoning_route_classifier():
    from plugins.xiaoqing_chat.reply_generator import _needs_reasoning_route

    assert _needs_reasoning_route("6 公斤每立方厘米") is True
    assert _needs_reasoning_route("半径和重力知道了，能算密度吗") is True
    assert _needs_reasoning_route("今天吃什么") is False


def test_long_non_question_turn_does_not_fallback_to_stale_topic_title():
    from plugins.xiaoqing_chat.planning.goal_state import _derive_goal_from_context

    goal = _derive_goal_from_context(
        current_text="今天我主要在整理部署流程、补文档、顺便看了下监控曲线，没有新的问题要问",
        planner_reasoning="",
        topic="昨晚旧话题",
    )

    assert goal == "自然聊天"


@pytest.mark.asyncio
async def test_topic_summarizer_refreshes_from_observed_traffic_not_only_exact_modulo(tmp_path):
    from plugins.xiaoqing_chat.llm.summarizer import maybe_update_topic_summary
    from plugins.xiaoqing_chat.memory.memory import StoredMessage

    data_dir = tmp_path
    chat_id = "g-refresh"
    cache_path = data_dir / "hippo_memorizer" / f"{chat_id}.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        '[{"topic_id":"1","topic":"旧话题","keywords":["旧"],"summary":"旧摘要","key_points":["旧要点"],"updated_at":1.0}]',
        encoding="utf-8",
    )

    history = [
        StoredMessage(role="user", content="A", name="U", ts=1.0),
        StoredMessage(role="assistant", content="B", name="小青", ts=2.0),
        StoredMessage(role="user", content="C", name="U", ts=3.0),
        StoredMessage(role="assistant", content="D", name="小青", ts=4.0),
        StoredMessage(role="user", content="E", name="U", ts=5.0),
    ]

    memory_db = MagicMock()
    with patch(
        "plugins.xiaoqing_chat.llm.summarizer.chat_completions",
        new=AsyncMock(
            return_value='{"topic":"新话题","keywords":["新"],"summary":"新摘要","key_points":["要点"]}'
        ),
    ) as mock_chat:
        await maybe_update_topic_summary(
            data_dir=data_dir,
            memory_db=memory_db,
            http_session=AsyncMock(),
            secrets={"api_base": "http://test", "api_key": "k", "model": "m"},
            bot_name="小青",
            chat_id=chat_id,
            history=history,
            min_messages_per_update=4,
            max_cache_topics=5,
            temperature=0.6,
            top_p=0.9,
            max_tokens=256,
            timeout_seconds=1.0,
            max_retry=0,
            retry_interval_seconds=0.1,
        )

    mock_chat.assert_awaited_once()


@pytest.mark.asyncio
async def test_topic_summarizer_does_not_repeat_when_capped_length_divides_interval(tmp_path):
    from plugins.xiaoqing_chat.llm.summarizer import maybe_update_topic_summary
    from plugins.xiaoqing_chat.memory.memory import StoredMessage

    chat_id = "g-divisor"
    cache_path = tmp_path / "hippo_memorizer" / f"{chat_id}.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        '[{"topic_id":"1","topic":"旧话题","keywords":[],"summary":"旧摘要",'
        '"key_points":[],"updated_at":200.0}]',
        encoding="utf-8",
    )
    complete = AsyncMock(
        return_value='{"topic":"新话题","keywords":[],"summary":"新摘要","key_points":[]}'
    )
    memory_db = MagicMock()
    common = {
        "data_dir": tmp_path,
        "memory_db": memory_db,
        "http_session": AsyncMock(),
        "secrets": {"api_base": "http://test", "api_key": "k", "model": "m"},
        "bot_name": "小青",
        "chat_id": chat_id,
        "min_messages_per_update": 20,
        "max_cache_topics": 5,
        "temperature": 0.6,
        "top_p": 0.9,
        "max_tokens": 256,
        "timeout_seconds": 1.0,
        "max_retry": 0,
        "retry_interval_seconds": 0.1,
    }

    unchanged = [
        StoredMessage(role="user", content=str(index), name="U", ts=float(index))
        for index in range(1, 201)
    ]
    twenty_new = [
        StoredMessage(role="user", content=str(index), name="U", ts=float(index))
        for index in range(21, 221)
    ]
    with patch(
        "plugins.xiaoqing_chat.llm.summarizer.chat_completions",
        new=complete,
    ):
        await maybe_update_topic_summary(history=unchanged, **common)
        complete.assert_not_awaited()
        await maybe_update_topic_summary(history=twenty_new, **common)

    complete.assert_awaited_once()


@pytest.mark.asyncio
async def test_spawn_post_reply_bg_tasks_uses_async_goal_and_action_history_reads(mock_context):
    from plugins.xiaoqing_chat.handlers_helper import _spawn_post_reply_bg_tasks

    runtime = MagicMock()
    runtime.cfg.summarizer.enable_topic_summarizer = False
    runtime.cfg.expression.enable_expression_learning = False
    runtime.cfg.reflection.enable_review_sessions = True
    runtime.cfg.reflection.session_timeout_seconds = 60.0
    runtime.cfg.reflection.session_cooldown_seconds = 60.0
    runtime.cfg.reflection.operator_user_id = 1
    runtime.cfg.reflection.operator_group_id = 2
    runtime.cfg.reflection.resend_interval_seconds = 30.0

    state = MagicMock()
    state.review_store.cleanup_expired = Mock()
    state.action_history.get_recent_async = AsyncMock(return_value=[])
    state.goal_store.get_async = AsyncMock(return_value=SimpleNamespace(goal="自然聊天"))
    state.goal_store.get.side_effect = AssertionError("sync goal store read should not be used")
    state.get_continuous_reply_count.return_value = 0

    with (
        patch(
            "plugins.xiaoqing_chat.handlers_helper._spawn_bg_task",
            side_effect=lambda _context, coro, name=None: coro.close(),
        ),
        patch(
            "plugins.xiaoqing_chat.handlers_helper.maybe_open_goal_strategy_review",
            return_value=None,
        ),
        patch("plugins.xiaoqing_chat.handlers_helper._log_step"),
    ):
        hctx = _make_hctx(runtime=runtime, state=state, context=mock_context, chat_id="g1")
        await _spawn_post_reply_bg_tasks(
            hctx=hctx,
            history_snapshot=[],
            event={"message_type": "group"},
        )

    assert state.action_history.get_recent_async.await_count == 1
    assert state.goal_store.get_async.await_count == 1


@pytest.mark.asyncio
async def test_group_reset_requires_admin_confirmation_and_writes_audit_log(mock_context):
    from plugins.xiaoqing_chat.handlers_internal import handle_internal_impl

    state = MagicMock()
    state.pop_persist_task.return_value = None
    state.inc_stats = Mock()
    data_dir = Path("test-data")
    hctx = SimpleNamespace(chat_id="g67890", runtime=MagicMock(), state=state, data_dir=data_dir)
    reset_chat_session = AsyncMock()
    event = {"message_type": "group", "group_id": 67890, "user_id": 12345}

    async def call_reset(args: str, is_admin: bool):
        return await handle_internal_impl(
            "重置",
            args,
            event,
            mock_context,
            handler_context_from_event=lambda _event, _context: hctx,
            get_lock=lambda _chat_id: asyncio.Lock(),
            reset_chat_session=reset_chat_session,
            cancel_pending_task=Mock(),
            is_admin_operator_fn=lambda _event, _context: is_admin,
        )

    denied = await call_reset("确认", is_admin=False)
    assert "仅限" in denied[0]["data"]["text"]
    reset_chat_session.assert_not_awaited()

    preview = await call_reset("", is_admin=True)
    assert "会清空本群" in preview[0]["data"]["text"]
    assert "/xc 重置 确认" in preview[0]["data"]["text"]
    reset_chat_session.assert_not_awaited()

    completed = await call_reset("确认", is_admin=True)
    assert completed == [{"type": "text", "data": {"text": "✅ 已重置会话记忆"}}]
    reset_chat_session.assert_awaited_once_with(state, "g67890", data_dir)
    state.inc_stats.assert_called_once_with("g67890", "resets")
    mock_context.logger.info.assert_called_once()
    assert "reset_audit" in mock_context.logger.info.call_args.args[0]


@pytest.mark.asyncio
async def test_private_reset_remains_limited_to_callers_private_scope(mock_context):
    from plugins.xiaoqing_chat.handlers_internal import handle_internal_impl

    state = MagicMock()
    state.pop_persist_task.return_value = None
    data_dir = Path("test-data")
    hctx = SimpleNamespace(chat_id="u12345", runtime=MagicMock(), state=state, data_dir=data_dir)
    reset_chat_session = AsyncMock()
    event = {"message_type": "private", "user_id": 12345}

    result = await handle_internal_impl(
        "重置",
        "",
        event,
        mock_context,
        handler_context_from_event=lambda _event, _context: hctx,
        get_lock=lambda _chat_id: asyncio.Lock(),
        reset_chat_session=reset_chat_session,
        cancel_pending_task=Mock(),
        is_admin_operator_fn=lambda _event, _context: False,
    )

    assert result == [{"type": "text", "data": {"text": "✅ 已重置会话记忆"}}]
    reset_chat_session.assert_awaited_once_with(state, "u12345", data_dir)
    assert "private" in mock_context.logger.info.call_args.args


@pytest.mark.asyncio
async def test_handle_internal_reset_uses_async_pfc_clear(mock_context, sample_group_event):
    from plugins.xiaoqing_chat.handlers import handle_internal

    runtime = MagicMock()
    state = MagicMock()
    state.pfc_state_store.clear_async = AsyncMock()
    state.pfc_state_store.clear.side_effect = AssertionError(
        "sync pfc state clear should not be used"
    )

    _set_context_principal(mock_context, sample_group_event, group_role="admin")
    hctx = _make_hctx(runtime=runtime, state=state, context=mock_context)
    with patch("plugins.xiaoqing_chat.handlers.HandlerContext.from_event", return_value=hctx):
        result = await handle_internal("重置", "确认", sample_group_event, mock_context)

    assert result == [{"type": "text", "data": {"text": "✅ 已重置会话记忆"}}]
    state.pfc_state_store.clear_async.assert_awaited_once_with("g67890")


@pytest.mark.asyncio
async def test_schedule_action_history_flush_uses_to_thread(mock_context):
    from plugins.xiaoqing_chat.task_scheduler import (
        _action_flush_tasks,
        _schedule_action_history_flush,
    )

    runtime = MagicMock()
    runtime.cfg.io_persist_debounce_seconds = 0.0
    state = MagicMock()

    async def fake_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    with (
        patch("plugins.xiaoqing_chat.task_scheduler._state", return_value=state),
        patch("plugins.xiaoqing_chat.task_scheduler._track_bg_task"),
        patch(
            "plugins.xiaoqing_chat.task_scheduler.asyncio.to_thread",
            new=AsyncMock(side_effect=fake_to_thread),
        ) as mock_to_thread,
    ):
        _schedule_action_history_flush(mock_context, runtime, chat_id="g1")
        task = _action_flush_tasks.get("g1")
        assert task is not None
        await task

    assert mock_to_thread.await_count == 1
    state.action_history.flush.assert_called_once_with("g1")
    # cleanup
    _action_flush_tasks.pop("g1", None)


@pytest.mark.asyncio
async def test_schedule_pfc_state_flush_uses_to_thread(mock_context):
    from plugins.xiaoqing_chat.task_scheduler import (
        _pfc_state_flush_tasks,
        _schedule_pfc_state_flush,
    )

    runtime = MagicMock()
    runtime.cfg.io_persist_debounce_seconds = 0.0
    state = MagicMock()

    async def fake_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    with (
        patch("plugins.xiaoqing_chat.task_scheduler._state", return_value=state),
        patch("plugins.xiaoqing_chat.task_scheduler._track_bg_task"),
        patch(
            "plugins.xiaoqing_chat.task_scheduler.asyncio.to_thread",
            new=AsyncMock(side_effect=fake_to_thread),
        ) as mock_to_thread,
    ):
        _schedule_pfc_state_flush(mock_context, runtime, chat_id="g1")
        task = _pfc_state_flush_tasks.get("g1")
        assert task is not None
        await task

    assert mock_to_thread.await_count == 1
    state.pfc_state_store.save.assert_called_once_with("g1")
    _pfc_state_flush_tasks.pop("g1", None)


@pytest.mark.asyncio
async def test_smalltalk_planner_runs_outside_chat_lock_but_commits_inside(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.handlers import _maybe_reply_smalltalk

    lock = asyncio.Lock()
    state = MagicMock()
    state.get_lock.return_value = lock
    state.get_mood_state.return_value = ""
    state.memory_store.get.return_value = []
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.memory_store.get_recent_async = AsyncMock(return_value=[])
    state.memory_store.append = Mock()
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
    state.heartflow.on_user_message_async = AsyncMock()
    state.heartflow.on_bot_reply_async = AsyncMock()
    state.heartflow.on_no_reply_async = AsyncMock()
    state.inc_stats = Mock()
    state.action_history.append = Mock()

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

    async def fake_run_pfc_once(**kwargs):
        assert not lock.locked()
        return SimpleNamespace(reply="planner-ok", action="reply", reason="ok", ended=False)

    async def fake_record_bot_reply(*args, **kwargs):
        assert lock.locked()
        return []

    def fake_set_state(chat_id, snapshot):
        assert lock.locked()

    def fake_schedule_pfc_state_flush(context, runtime, *, chat_id):
        assert not lock.locked()

    state.pfc_state_store.set_state = Mock(side_effect=fake_set_state)

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
            new=AsyncMock(side_effect=fake_run_pfc_once),
        ),
        patch(
            "plugins.xiaoqing_chat.handlers._record_bot_reply",
            new=AsyncMock(side_effect=fake_record_bot_reply),
        ),
        patch(
            "plugins.xiaoqing_chat.handlers._schedule_pfc_state_flush",
            side_effect=fake_schedule_pfc_state_flush,
            create=True,
        ) as mock_schedule_pfc_state_flush,
        patch("plugins.xiaoqing_chat.handlers._most_recent_user_local_id", return_value="u1"),
        patch("plugins.xiaoqing_chat.handlers._spawn_post_reply_bg_tasks", new=AsyncMock()),
        patch("plugins.xiaoqing_chat.handlers._schedule_memory_persist"),
        patch("plugins.xiaoqing_chat.handlers._schedule_action_history_flush"),
        patch("plugins.xiaoqing_chat.handlers._freq_record"),
        patch("plugins.xiaoqing_chat.handlers._log_step"),
        patch(
            "plugins.xiaoqing_chat.handlers.maybe_add_mode_indicator",
            side_effect=lambda reply, runtime: reply,
        ),
    ):
        result = await _maybe_reply_smalltalk("你好", sample_group_event, mock_context)

    state.pfc_state_store.set_state.assert_not_called()
    await _ack_reply_delivery(result)
    assert result == [{"type": "text", "data": {"text": "planner-ok"}}]
    state.pfc_state_store.set_state.assert_called_once()
    mock_schedule_pfc_state_flush.assert_called_once()
