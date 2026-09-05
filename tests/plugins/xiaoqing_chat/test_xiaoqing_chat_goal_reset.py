"""目标同步、重置和记录。"""

from __future__ import annotations

import tests.helpers.xiaoqing_chat_test_support as _fixture_support
from tests.helpers.xiaoqing_chat_test_support import (
    Any,
    AsyncMock,
    MagicMock,
    Mock,
    SimpleNamespace,
    _ack_reply_delivery,
    _make_hctx,
    _reply_draft,
    _set_context_principal,
    asyncio,
    handle_errors,
    patch,
    pytest,
    time,
)

mock_context       = _fixture_support.mock_context
sample_group_event = _fixture_support.sample_group_event


@pytest.mark.asyncio
async def test_smalltalk_direct_reply_uses_dynamic_history_think_level_when_planner_disabled(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.handlers import _maybe_reply_smalltalk

    lock                              = asyncio.Lock()
    state                             = MagicMock()
    state.get_mood_state.return_value = ""
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.memory_store.get_recent_async = AsyncMock(return_value=[object(), object(), object()])
    state.memory_store.append             = Mock()
    state.heartflow.on_user_message_async = AsyncMock()
    state.heartflow.on_bot_reply_async    = AsyncMock()
    state.heartflow.on_no_reply_async     = AsyncMock()
    state.inc_stats                       = Mock()
    state.action_history.append           = Mock()
    state.pfc_state_store.get_async       = AsyncMock(
        return_value=SimpleNamespace(
            chat_id                      = "g67890",
            ignore_until_ts              = 0.0,
            ended                        = False,
            last_successful_reply_action = "",
            goal_list                    = [],
            knowledge_list               = [],
            planner_fail_ts              = [],
            planner_skip_until           = 0.0,
            updated_at                   = 0.0,
        )
    )
    state.pfc_state_store.set_state = Mock()

    seen: dict[str, Any] = {}

    async def fake_build_memory_block(**_kwargs):
        return "prefetched-memory"

    async def fake_generate_reply_draft(**kwargs):
        seen["action"]                 = kwargs["action"]
        seen["prefetched_memory_task"] = kwargs.get("prefetched_memory_task")
        if seen["prefetched_memory_task"] is not None:
            seen["prefetched_memory"] = await seen["prefetched_memory_task"]
        return _reply_draft("直接回复")

    runtime = SimpleNamespace(
        cfg=SimpleNamespace(
            enable_smalltalk=True,
            goal=SimpleNamespace(enable_goal=False),
            reflection=SimpleNamespace(
                enable_expression_reflection=False, enable_review_sessions=False
            ),
            brain_chat=SimpleNamespace(
                enable_private_brain_chat = False,
                private_planner_always_on = True,
                brain_max_context_size    = 10,
            ),
            max_context_size = 10,
            planner          = SimpleNamespace(
                enable_planner=False,
                resolve_think_level=lambda history_len=0: 2 if history_len >= 3 else 0,
            ),
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
        patch(
            "plugins.xiaoqing_chat.handlers._build_memory_block",
            new=AsyncMock(side_effect=fake_build_memory_block),
        ),
        patch(
            "plugins.xiaoqing_chat.handlers._generate_reply_draft",
            new=AsyncMock(side_effect=fake_generate_reply_draft),
        ),
        patch("plugins.xiaoqing_chat.handlers.run_pfc_once", new=AsyncMock()) as mock_run_pfc_once,
        patch("plugins.xiaoqing_chat.handlers._most_recent_user_local_id", return_value="u1"),
        patch("plugins.xiaoqing_chat.handlers._spawn_post_reply_bg_tasks", new=AsyncMock()),
        patch("plugins.xiaoqing_chat.handlers._schedule_memory_persist"),
        patch("plugins.xiaoqing_chat.handlers._schedule_action_history_flush"),
        patch("plugins.xiaoqing_chat.handlers._schedule_pfc_state_flush"),
        patch("plugins.xiaoqing_chat.handlers._freq_record"),
        patch("plugins.xiaoqing_chat.handlers._log_step"),
        patch(
            "plugins.xiaoqing_chat.handlers.maybe_add_mode_indicator",
            side_effect=lambda reply, runtime: reply,
        ),
    ):
        result = await _maybe_reply_smalltalk("你好", sample_group_event, mock_context)

    assert result == [{"type": "text", "data": {"text": "直接回复"}}]
    assert mock_run_pfc_once.await_count == 0
    assert state.pfc_state_store.get_async.await_count == 0
    state.pfc_state_store.set_state.assert_not_called()
    assert seen["action"].think_level == 2
    assert seen["prefetched_memory_task"] is not None
    assert seen["prefetched_memory"] == "prefetched-memory"


@pytest.mark.asyncio
async def test_smalltalk_private_brain_chat_keeps_planner_when_private_always_on_even_if_disabled(
    mock_context, sample_private_event
):
    from plugins.xiaoqing_chat.handlers import _maybe_reply_smalltalk

    lock                              = asyncio.Lock()
    state                             = MagicMock()
    state.get_mood_state.return_value = ""
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.memory_store.get_recent_async = AsyncMock(return_value=[object()])
    state.memory_store.append             = Mock()
    state.heartflow.on_user_message_async = AsyncMock()
    state.heartflow.on_bot_reply_async    = AsyncMock()
    state.heartflow.on_no_reply_async     = AsyncMock()
    state.inc_stats                       = Mock()
    state.action_history.append           = Mock()
    state.pfc_state_store.get_async       = AsyncMock(
        return_value=SimpleNamespace(
            chat_id                      = "u12345",
            ignore_until_ts              = 0.0,
            ended                        = False,
            last_successful_reply_action = "",
            goal_list                    = [],
            knowledge_list               = [],
            planner_fail_ts              = [],
            planner_skip_until           = 0.0,
            updated_at                   = 0.0,
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
            brain_chat=SimpleNamespace(
                enable_private_brain_chat = True,
                private_planner_always_on = True,
                brain_max_context_size    = 6,
                brain_think_level         = 2,
            ),
            max_context_size = 6,
            planner          = SimpleNamespace(
                enable_planner=False,
                resolve_think_level=lambda history_len=0: 0,
            ),
            personality=SimpleNamespace(states=[], state_probability=0.0),
            debug=SimpleNamespace(log_latency=False),
        )
    )

    async def fake_run_pfc_once(**kwargs):
        return SimpleNamespace(reply="规划回复", action="reply", reason="ok", ended=False)

    hctx = _make_hctx(runtime=runtime, state=state, context=mock_context, chat_id="u12345")
    with (
        patch("plugins.xiaoqing_chat.handlers.HandlerContext.from_event", return_value=hctx),
        patch("plugins.xiaoqing_chat.handlers._get_lock", return_value=lock),
        patch("plugins.xiaoqing_chat.handlers._should_ignore_text", return_value=False),
        patch(
            "plugins.xiaoqing_chat.handlers._ensure_user_message_recorded",
            new=AsyncMock(return_value="u1"),
        ),
        patch("plugins.xiaoqing_chat.handlers._should_reply", new=AsyncMock(return_value=True)),
        patch("plugins.xiaoqing_chat.handlers.is_brain_chat_active", return_value=True),
        patch("plugins.xiaoqing_chat.handlers._build_memory_block", new=AsyncMock(return_value="")),
        patch(
            "plugins.xiaoqing_chat.handlers.run_pfc_once",
            new=AsyncMock(side_effect=fake_run_pfc_once),
        ) as mock_run_pfc_once,
        patch("plugins.xiaoqing_chat.handlers._most_recent_user_local_id", return_value="u1"),
        patch("plugins.xiaoqing_chat.handlers._spawn_post_reply_bg_tasks", new=AsyncMock()),
        patch("plugins.xiaoqing_chat.handlers._schedule_memory_persist"),
        patch("plugins.xiaoqing_chat.handlers._schedule_action_history_flush"),
        patch("plugins.xiaoqing_chat.handlers._schedule_pfc_state_flush"),
        patch("plugins.xiaoqing_chat.handlers._freq_record"),
        patch("plugins.xiaoqing_chat.handlers._log_step"),
        patch(
            "plugins.xiaoqing_chat.handlers.maybe_add_mode_indicator",
            side_effect=lambda reply, runtime: reply,
        ),
    ):
        result = await _maybe_reply_smalltalk("你好", sample_private_event, mock_context)

    assert result == [{"type": "text", "data": {"text": "规划回复"}}]
    assert mock_run_pfc_once.await_count == 1


@pytest.mark.asyncio
async def test_smalltalk_finalize_defers_post_reply_tasks_and_media_marking_to_background(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.handlers import _maybe_reply_smalltalk

    lock                              = asyncio.Lock()
    state                             = MagicMock()
    state.get_mood_state.return_value = ""
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.memory_store.get_recent_async = AsyncMock(return_value=[])
    state.memory_store.append             = Mock()
    state.heartflow.on_user_message_async = AsyncMock()
    state.heartflow.on_bot_reply_async    = AsyncMock()
    state.heartflow.on_no_reply_async     = AsyncMock()
    state.inc_stats                       = Mock()
    state.action_history.append           = Mock()
    state.pfc_state_store.get_async       = AsyncMock()
    state.pfc_state_store.set_state       = Mock()

    runtime = SimpleNamespace(
        cfg=SimpleNamespace(
            enable_smalltalk=True,
            goal=SimpleNamespace(enable_goal=False),
            reflection=SimpleNamespace(
                enable_expression_reflection=False, enable_review_sessions=False
            ),
            brain_chat=SimpleNamespace(enable_private_brain_chat=False),
            planner=SimpleNamespace(
                enable_planner=False, resolve_think_level=lambda history_len=0: 0
            ),
            personality=SimpleNamespace(states=[], state_probability=0.0),
            debug=SimpleNamespace(log_latency=False),
        )
    )
    event                       = dict(sample_group_event)
    event["_xc_command_forced"] = True

    async def fake_build_memory_block(**_kwargs):
        return ""

    async def fake_generate_reply_draft(**kwargs):
        task = kwargs.get("prefetched_memory_task")
        if task is not None:
            await task
        return _reply_draft("后台化测试")

    spawned_names: list[str] = []

    def fake_spawn_bg_task(_context, coro, *, name: str) -> None:
        spawned_names.append(name)
        coro.close()

    hctx = _make_hctx(runtime=runtime, state=state, context=mock_context)
    with (
        patch("plugins.xiaoqing_chat.handlers.HandlerContext.from_event", return_value=hctx),
        patch("plugins.xiaoqing_chat.handlers._get_lock", return_value=lock),
        patch("plugins.xiaoqing_chat.handlers._should_ignore_text", return_value=False),
        patch(
            "plugins.xiaoqing_chat.handlers._ensure_user_message_recorded",
            new=AsyncMock(return_value="u1"),
        ),
        patch("plugins.xiaoqing_chat.handlers.is_brain_chat_active", return_value=False),
        patch(
            "plugins.xiaoqing_chat.handlers._build_memory_block",
            new=AsyncMock(side_effect=fake_build_memory_block),
        ),
        patch(
            "plugins.xiaoqing_chat.handlers._generate_reply_draft",
            new=AsyncMock(side_effect=fake_generate_reply_draft),
        ),
        patch("plugins.xiaoqing_chat.handlers._most_recent_user_local_id", return_value="u1"),
        patch(
            "plugins.xiaoqing_chat.handlers._spawn_post_reply_bg_tasks", new=AsyncMock()
        ) as mock_post_reply,
        patch("plugins.xiaoqing_chat.handlers._mark_reply_media_used") as mock_mark_media,
        patch("plugins.xiaoqing_chat.handlers._spawn_bg_task", side_effect=fake_spawn_bg_task),
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

    await _ack_reply_delivery(result)
    assert result == [{"type": "text", "data": {"text": "后台化测试"}}]
    assert "post_reply:g67890" in spawned_names
    assert "reply_media_used:g67890" in spawned_names
    mock_post_reply.assert_not_awaited()
    mock_mark_media.assert_not_called()


@pytest.mark.asyncio
async def test_prepare_smalltalk_turn_forces_reply_when_bot_name_is_mentioned(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.handlers import _prepare_smalltalk_turn

    state                              = MagicMock()
    state.review_store.cleanup_expired = Mock()
    state.pfc_state_store.get_async    = AsyncMock(
        return_value=SimpleNamespace(goal_list=[], planner_skip_until=0.0)
    )
    state.get_mood_state.return_value = ""

    runtime = SimpleNamespace(
        cfg=SimpleNamespace(
            goal=SimpleNamespace(enable_goal=False),
            reflection=SimpleNamespace(
                enable_expression_reflection = False,
                enable_review_sessions       = False,
            ),
            brain_chat=SimpleNamespace(enable_private_brain_chat=False),
            personality=SimpleNamespace(states=[], state_probability=0.0),
        )
    )
    hctx = _make_hctx(runtime=runtime, state=state, context=mock_context)

    with (
        patch(
            "plugins.xiaoqing_chat.handlers.build_effective_user_text",
            new=AsyncMock(return_value="你好"),
        ),
        patch("plugins.xiaoqing_chat.handlers._should_ignore_text", return_value=False),
        patch("plugins.xiaoqing_chat.handlers._is_at_me", return_value=False),
        patch("plugins.xiaoqing_chat.handlers._has_bot_name", return_value=True),
        patch("plugins.xiaoqing_chat.handlers._is_private", return_value=False),
        patch("plugins.xiaoqing_chat.handlers._should_reply", new=AsyncMock(return_value=True)),
        patch("plugins.xiaoqing_chat.handlers.is_brain_chat_active", return_value=False),
        patch("plugins.xiaoqing_chat.handlers._log_step"),
    ):
        prepared = await _prepare_smalltalk_turn("你好", sample_group_event, mock_context, hctx)

    assert prepared is not None
    assert prepared.mentioned is True
    assert prepared.forced is True


@pytest.mark.asyncio
async def test_prepare_smalltalk_turn_forces_reply_when_recent_coreference_mentions_bot(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.handlers import _prepare_smalltalk_turn
    from plugins.xiaoqing_chat.memory.memory import StoredMessage

    event                               = dict(sample_group_event)
    event["message_id"]                 = 103
    state                               = MagicMock()
    state.memory_store.get_recent_async = AsyncMock(
        return_value=[
            StoredMessage(
                role       = "user",
                name       = "群友",
                ts         = time.time() - 5,
                message_id = 101,
                content    = "小青你在吗",
            ),
            StoredMessage(
                role       = "assistant",
                name       = "小青",
                ts         = time.time() - 4,
                message_id = 102,
                content    = "在呢",
            ),
        ]
    )
    state.pfc_state_store.get_async = AsyncMock(
        return_value=SimpleNamespace(goal_list=[], planner_skip_until=0.0)
    )
    state.get_mood_state.return_value = ""

    runtime = SimpleNamespace(
        cfg=SimpleNamespace(
            goal=SimpleNamespace(enable_goal=False),
            reflection=SimpleNamespace(
                enable_expression_reflection = False,
                enable_review_sessions       = False,
            ),
            brain_chat=SimpleNamespace(enable_private_brain_chat=False),
            personality=SimpleNamespace(states=[], state_probability=0.0),
        )
    )
    hctx = _make_hctx(runtime=runtime, state=state, context=mock_context)

    with (
        patch(
            "plugins.xiaoqing_chat.handlers.build_effective_user_text",
            new=AsyncMock(return_value="不@她能不能听见啊"),
        ),
        patch("plugins.xiaoqing_chat.handlers._should_ignore_text", return_value=False),
        patch("plugins.xiaoqing_chat.handlers._is_at_me", return_value=False),
        patch("plugins.xiaoqing_chat.handlers._has_bot_name", return_value=False),
        patch("plugins.xiaoqing_chat.handlers._is_private", return_value=False),
        patch(
            "plugins.xiaoqing_chat.handlers._should_reply", new=AsyncMock(return_value=False)
        ) as gate,
        patch("plugins.xiaoqing_chat.handlers.is_brain_chat_active", return_value=False),
        patch("plugins.xiaoqing_chat.handlers._log_step"),
    ):
        prepared = await _prepare_smalltalk_turn("不@她能不能听见啊", event, mock_context, hctx)

    assert prepared is not None
    assert prepared.mentioned is True
    assert prepared.forced is True
    assert prepared.force_reason == "coreference_mention"
    gate.assert_not_awaited()


@pytest.mark.asyncio
async def test_prepare_smalltalk_turn_does_not_force_coreference_without_bot_anchor(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.handlers import _prepare_smalltalk_turn
    from plugins.xiaoqing_chat.memory.memory import StoredMessage

    event                               = dict(sample_group_event)
    event["message_id"]                 = 103
    state                               = MagicMock()
    state.memory_store.get_recent_async = AsyncMock(
        return_value=[
            StoredMessage(
                role       = "user",
                name       = "群友",
                ts         = time.time() - 5,
                message_id = 101,
                content    = "她刚才还在说外卖",
            )
        ]
    )
    state.pfc_state_store.get_async = AsyncMock(
        return_value=SimpleNamespace(goal_list=[], planner_skip_until=0.0)
    )
    state.get_mood_state.return_value = ""

    runtime = SimpleNamespace(
        cfg=SimpleNamespace(
            goal=SimpleNamespace(enable_goal=False),
            reflection=SimpleNamespace(
                enable_expression_reflection = False,
                enable_review_sessions       = False,
            ),
            brain_chat=SimpleNamespace(enable_private_brain_chat=False),
            personality=SimpleNamespace(states=[], state_probability=0.0),
        )
    )
    hctx = _make_hctx(runtime=runtime, state=state, context=mock_context)

    with (
        patch(
            "plugins.xiaoqing_chat.handlers.build_effective_user_text",
            new=AsyncMock(return_value="不@她能不能听见啊"),
        ),
        patch("plugins.xiaoqing_chat.handlers._should_ignore_text", return_value=False),
        patch("plugins.xiaoqing_chat.handlers._is_at_me", return_value=False),
        patch("plugins.xiaoqing_chat.handlers._has_bot_name", return_value=False),
        patch("plugins.xiaoqing_chat.handlers._is_private", return_value=False),
        patch(
            "plugins.xiaoqing_chat.handlers._should_reply", new=AsyncMock(return_value=False)
        ) as gate,
        patch("plugins.xiaoqing_chat.handlers.is_brain_chat_active", return_value=False),
        patch("plugins.xiaoqing_chat.handlers._log_step"),
    ):
        prepared = await _prepare_smalltalk_turn("不@她能不能听见啊", event, mock_context, hctx)

    assert prepared is None
    gate.assert_awaited_once()


@pytest.mark.asyncio
async def test_smalltalk_commit_syncs_goal_store_to_top_planner_goal_after_state_override_update(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.handlers import _maybe_reply_smalltalk

    lock                              = asyncio.Lock()
    state                             = MagicMock()
    state.get_mood_state.return_value = ""
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.memory_store.get_recent_async = AsyncMock(return_value=[])
    state.memory_store.append       = Mock()
    state.goal_store.set_async      = AsyncMock()
    state.pfc_state_store.get_async = AsyncMock(
        return_value=SimpleNamespace(
            chat_id                      = "g67890",
            ignore_until_ts              = 0.0,
            ended                        = False,
            last_successful_reply_action = "",
            goal_list                    = [{"goal": "旧规划目标"}],
            knowledge_list               = [],
            planner_fail_ts              = [],
            planner_skip_until           = 0.0,
            updated_at                   = 0.0,
        )
    )
    state.pfc_state_store.set_state = Mock()

    runtime = SimpleNamespace(
        cfg=SimpleNamespace(
            enable_smalltalk=True,
            goal=SimpleNamespace(enable_goal=True),
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
        kwargs["state_override"].goal_list = [{"goal": "新规划目标"}]
        return SimpleNamespace(reply="planner-ok", action="reply", reason="ok", ended=False)

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
            "plugins.xiaoqing_chat.handlers.derive_goal_async",
            new=AsyncMock(return_value="旧门控目标"),
        ),
        patch("plugins.xiaoqing_chat.handlers._record_bot_reply", new=AsyncMock(return_value=[])),
        patch("plugins.xiaoqing_chat.handlers._most_recent_user_local_id", return_value="u1"),
        patch("plugins.xiaoqing_chat.handlers._spawn_post_reply_bg_tasks", new=AsyncMock()),
        patch("plugins.xiaoqing_chat.handlers._schedule_memory_persist"),
        patch("plugins.xiaoqing_chat.handlers._schedule_action_history_flush"),
        patch("plugins.xiaoqing_chat.handlers._schedule_pfc_state_flush"),
        patch("plugins.xiaoqing_chat.handlers._freq_record"),
        patch("plugins.xiaoqing_chat.handlers._log_step"),
    ):
        result = await _maybe_reply_smalltalk("继续聊", sample_group_event, mock_context)

    await _ack_reply_delivery(result)
    assert result == [{"type": "text", "data": {"text": "planner-ok"}}]
    assert state.goal_store.set_async.await_args_list[-1].kwargs == {
        "goal": "新规划目标",
        "source": "planner",
    }


@pytest.mark.asyncio
async def test_smalltalk_commit_clears_goal_store_when_planner_goal_list_becomes_empty(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.handlers import _maybe_reply_smalltalk
    from plugins.xiaoqing_chat.planning.goal_state import GoalStore

    chat_id    = "g67890"
    lock       = asyncio.Lock()
    goal_store = GoalStore()
    await goal_store.set_async(chat_id, goal="围绕旧目标继续", source="user")

    state                             = MagicMock()
    state.get_mood_state.return_value = ""
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.memory_store.get_recent_async = AsyncMock(return_value=[])
    state.memory_store.append       = Mock()
    state.goal_store                = goal_store
    state.pfc_state_store.get_async = AsyncMock(
        return_value=SimpleNamespace(
            chat_id                      = chat_id,
            ignore_until_ts              = 0.0,
            ended                        = False,
            last_successful_reply_action = "",
            goal_list                    = [{"goal": "旧规划目标"}],
            knowledge_list               = [],
            planner_fail_ts              = [],
            planner_skip_until           = 0.0,
            updated_at                   = 0.0,
        )
    )
    state.pfc_state_store.set_state = Mock()

    runtime = SimpleNamespace(
        cfg=SimpleNamespace(
            enable_smalltalk=True,
            goal=SimpleNamespace(enable_goal=True),
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
        kwargs["state_override"].goal_list = []
        return SimpleNamespace(reply="planner-ok", action="reply", reason="ok", ended=False)

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
            "plugins.xiaoqing_chat.handlers.derive_goal_async",
            new=AsyncMock(return_value="围绕旧目标继续"),
        ),
        patch("plugins.xiaoqing_chat.handlers._record_bot_reply", new=AsyncMock(return_value=[])),
        patch("plugins.xiaoqing_chat.handlers._most_recent_user_local_id", return_value="u1"),
        patch("plugins.xiaoqing_chat.handlers._spawn_post_reply_bg_tasks", new=AsyncMock()),
        patch("plugins.xiaoqing_chat.handlers._schedule_memory_persist"),
        patch("plugins.xiaoqing_chat.handlers._schedule_action_history_flush"),
        patch("plugins.xiaoqing_chat.handlers._schedule_pfc_state_flush"),
        patch("plugins.xiaoqing_chat.handlers._freq_record"),
        patch("plugins.xiaoqing_chat.handlers._log_step"),
    ):
        result = await _maybe_reply_smalltalk("继续聊", sample_group_event, mock_context)

    await _ack_reply_delivery(result)
    assert result == [{"type": "text", "data": {"text": "planner-ok"}}]
    assert state.goal_store.get(chat_id).goal == ""


@pytest.mark.asyncio
async def test_smalltalk_pre_gate_clears_goal_store_when_no_goal_is_derived(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.handlers import _maybe_reply_smalltalk
    from plugins.xiaoqing_chat.planning.goal_state import GoalStore

    chat_id    = "g67890"
    lock       = asyncio.Lock()
    goal_store = GoalStore()
    await goal_store.set_async(chat_id, goal="围绕旧目标继续", source="user")

    state                             = MagicMock()
    state.get_mood_state.return_value = ""
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.memory_store.get_recent_async = AsyncMock(return_value=[])
    state.memory_store.append       = Mock()
    state.goal_store                = goal_store
    state.pfc_state_store.get_async = AsyncMock(
        return_value=SimpleNamespace(
            chat_id                      = chat_id,
            ignore_until_ts              = 0.0,
            ended                        = False,
            last_successful_reply_action = "",
            goal_list                    = [],
            knowledge_list               = [],
            planner_fail_ts              = [],
            planner_skip_until           = 0.0,
            updated_at                   = 0.0,
        )
    )
    state.heartflow.on_no_reply_async = AsyncMock()

    runtime = SimpleNamespace(
        cfg=SimpleNamespace(
            enable_smalltalk=True,
            goal=SimpleNamespace(enable_goal=True),
            reflection=SimpleNamespace(
                enable_expression_reflection=False, enable_review_sessions=False
            ),
            brain_chat=SimpleNamespace(enable_private_brain_chat=False),
            personality=SimpleNamespace(states=[], state_probability=0.0),
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
        patch("plugins.xiaoqing_chat.handlers.derive_goal_async", new=AsyncMock(return_value="")),
        patch("plugins.xiaoqing_chat.handlers._should_reply", new=AsyncMock(return_value=False)),
        patch("plugins.xiaoqing_chat.handlers._log_step"),
    ):
        result = await _maybe_reply_smalltalk("继续聊", sample_group_event, mock_context)

    assert result == []
    assert state.goal_store.get(chat_id).goal == ""
    state.heartflow.on_no_reply_async.assert_awaited_once_with(chat_id=chat_id)


@pytest.mark.asyncio
async def test_handle_errors_uses_keyword_context_for_logging():
    context = SimpleNamespace(
        logger     = MagicMock(),
        request_id = "req-xc-handler",
        secrets    = {},
    )

    @handle_errors
    async def failing_handler(*, context=None):
        raise RuntimeError("boom")

    result = await failing_handler(context=context)

    context.logger.error.assert_called_once()
    context.logger.exception.assert_not_called()
    text = result[0]["data"]["text"]
    assert "XQ-PLUGIN-UNEXPECTED" in text
    assert "req-xc-handler" in text
    assert "boom" not in text


@pytest.mark.asyncio
async def test_handle_internal_reset_clears_goal_heartflow_and_action_history(
    mock_context, sample_group_event, tmp_path
):
    from plugins.xiaoqing_chat.expression.bw_expression_store import ExpressionRecord
    from plugins.xiaoqing_chat.expression.bw_jargon_store import JargonRecord, JargonStore
    from plugins.xiaoqing_chat.handlers import handle_internal
    from plugins.xiaoqing_chat.planning.action_history import ActionRecord
    from plugins.xiaoqing_chat.runtime_state import ChatRuntimeState
    from plugins.xiaoqing_chat.store_binding import _bind_all_stores

    chat_id = "g67890"
    runtime = MagicMock()
    state   = ChatRuntimeState()
    _bind_all_stores(state, tmp_path)

    state.memory_store.append(chat_id, role="user", name="Tester", content="hi")
    state.memory_db.upsert_text(
        doc_id = "topic-current-chat",
        text   = "本群旧话题",
        meta   = {"type": "topic_summary", "chat_id": chat_id},
    )
    state.memory_db.upsert_text(
        doc_id = "topic-other-chat",
        text   = "其他群话题",
        meta   = {"type": "topic_summary", "chat_id": "g-other"},
    )
    state.memory_db.save()
    await state.goal_store.set_async(chat_id, goal="围绕旧目标继续", source="user")
    await state.heartflow.on_user_message_async(chat_id=chat_id)
    await state.heartflow.on_bot_reply_async(chat_id=chat_id)
    state.action_history.append(
        chat_id,
        ActionRecord(
            ts           = time.time(),
            local_target = "u1",
            action       = "reply",
            reasoning    = "old",
            detail       = {"source": "pfc"},
            executed     = True,
        ),
    )
    state.set_continuous_reply_count(chat_id, 3)
    state.set_continuous_cooldown_until(chat_id, 600.0)
    state.set_last_observe_ts(chat_id, 500.0)
    state.set_mood_state(chat_id, "旧情绪", duration_seconds=600.0)
    state.set_pending_bot_name_call(chat_id, 1001, ttl_seconds=600.0)
    state.set_reply_gate_decision(chat_id, SimpleNamespace(reason="old"))
    assert state.fetch_and_increment_local_id(chat_id) == 1
    assert state.fetch_and_increment_local_id(chat_id) == 2

    pfc_st                              = await state.pfc_state_store.get_async(chat_id)
    pfc_st.ended                        = True
    pfc_st.ignore_until_ts              = 123.0
    pfc_st.last_successful_reply_action = "say_goodbye"
    pfc_st.goal_list                    = [{"goal": "old"}]
    pfc_st.knowledge_list               = [{"text": "old"}]
    await state.pfc_state_store.save_async(chat_id)
    state.bw_expr_store.save(
        [
            ExpressionRecord("expr-current", chat_id, "旧情景", "旧风格"),
            ExpressionRecord("expr-other", "g-other", "其它情景", "其它风格"),
        ]
    )
    state.bw_jargon_store.save(
        [
            JargonRecord(content="本群黑话", scope_chat_id=chat_id),
            JargonRecord(content="其它群黑话", scope_chat_id="g-other"),
            JargonRecord(
                content        = "全局黑话",
                is_global      = True,
                chat_id_counts = [[chat_id, 3], ["g-other", 1]],
            ),
        ]
    )
    state.bw_recorder.set_last_time(chat_id, 123.0)
    state.bw_tracker_store.set_tracker(chat_id, "expr-current")
    state.bw_tracker_store.set_tracker("g-other", "expr-other")
    state.media_store.upsert_media_items(
        [{"media_key": "shared-media", "kind": "image", "description": "共享图片"}]
    )

    _set_context_principal(mock_context, sample_group_event, group_role="admin")
    hctx = _make_hctx(runtime=runtime, state=state, context=mock_context, data_dir=tmp_path)
    with patch("plugins.xiaoqing_chat.handlers.HandlerContext.from_event", return_value=hctx):
        result = await handle_internal("重置", "确认", sample_group_event, mock_context)

    assert result == [{"type": "text", "data": {"text": "✅ 已重置会话记忆"}}]
    assert state.memory_store.get(chat_id) == []
    assert state.memory_db.query("旧话题", chat_id=chat_id, min_score=-1.0) == []
    assert [
        item.doc_id for item in state.memory_db.query("话题", chat_id="g-other", min_score=-1.0)
    ] == ["topic-other-chat"]
    reloaded_memory_db = type(state.memory_db)()
    reloaded_memory_db.bind(tmp_path)
    assert reloaded_memory_db.query("旧话题", chat_id=chat_id, min_score=-1.0) == []
    assert [
        item.doc_id for item in reloaded_memory_db.query("话题", chat_id="g-other", min_score=-1.0)
    ] == ["topic-other-chat"]
    assert state.goal_store.get(chat_id).goal == ""
    heartflow_state = await state.heartflow.get_async(chat_id)
    assert heartflow_state.reply_streak == 0
    assert heartflow_state.no_reply_streak == 0
    assert state.get_continuous_reply_count(chat_id) == 0
    assert state.get_continuous_cooldown_until(chat_id) == 0.0
    assert await state.action_history.get_recent_async(chat_id, max_items=20) == []
    assert state.get_last_observe_ts(chat_id) == 0.0
    assert state.get_mood_state(chat_id) == ""
    assert state.consume_pending_bot_name_call(chat_id, 1001) is False
    assert state.get_reply_gate_decision(chat_id) is None
    assert state.fetch_and_increment_local_id(chat_id) == 1
    assert [item.expression_id for item in state.bw_expr_store.load()] == ["expr-other"]
    jargon = state.bw_jargon_store.load()
    assert JargonStore.key_for("本群黑话", chat_id) not in jargon
    assert JargonStore.key_for("其它群黑话", "g-other") in jargon
    assert jargon[JargonStore.key_for("全局黑话")].chat_id_counts == [["g-other", 1]]
    assert state.bw_recorder.get_last_time(chat_id) == 0.0
    assert state.bw_tracker_store.get_trackers(chat_id) == []
    assert [item.expression_id for item in state.bw_tracker_store.get_trackers("g-other")] == [
        "expr-other"
    ]
    assert (
        state.media_store.resolve_media_items([{"media_key": "shared-media"}])[0]["description"]
        == "共享图片"
    )


@pytest.mark.asyncio
async def test_handle_internal_reset_removes_per_chat_files_and_atomic_backups(
    mock_context, sample_group_event, tmp_path
):
    """重置后不得从旁路缓存或原子备份恢复旧会话。"""
    from core.plugin_base import write_json
    from plugins.xiaoqing_chat.handlers import handle_internal
    from plugins.xiaoqing_chat.runtime_state import ChatRuntimeState
    from plugins.xiaoqing_chat.store_binding import _bind_all_stores

    chat_id = "g67890"
    runtime = MagicMock()
    state   = ChatRuntimeState()
    _bind_all_stores(state, tmp_path)

    json_paths = [
        tmp_path / f"{chat_id}.json",
        tmp_path / "goal_state" / f"{chat_id}.json",
        tmp_path / "heartflow" / f"{chat_id}.json",
        tmp_path / "action_history" / f"{chat_id}.json",
        tmp_path / "pfc_state" / f"{chat_id}.json",
        tmp_path / "hippo_memorizer" / f"{chat_id}.json",
    ]
    for path in json_paths:
        write_json(path, {"old": "conversation"})
        write_json(path, {"new": "conversation"})
        assert path.with_name(f"{path.name}.bak").exists()

    thinking_back_path = tmp_path / "thinking_back" / f"{chat_id}.jsonl"
    thinking_back_path.parent.mkdir(parents=True, exist_ok=True)
    thinking_back_path.write_text(
        '{"question":"旧问题","answer":"旧回答"}\n',
        encoding="utf-8",
    )

    _set_context_principal(mock_context, sample_group_event, group_role="admin")
    hctx = _make_hctx(runtime=runtime, state=state, context=mock_context, data_dir=tmp_path)
    with patch("plugins.xiaoqing_chat.handlers.HandlerContext.from_event", return_value=hctx):
        result = await handle_internal("重置", "确认", sample_group_event, mock_context)

    assert result == [{"type": "text", "data": {"text": "✅ 已重置会话记忆"}}]
    for path in json_paths:
        assert not path.exists()
        assert not path.with_name(f"{path.name}.bak").exists()
    assert not thinking_back_path.exists()

    pfc_state = await state.pfc_state_store.get_async(chat_id)
    assert pfc_state.goal_list == []
    assert pfc_state.knowledge_list == []
    assert not (tmp_path / "pfc_state" / f"{chat_id}.json").exists()


@pytest.mark.asyncio
async def test_handle_internal_reset_clears_review_policy_and_sessions_for_current_chat(
    mock_context, sample_group_event, tmp_path
):
    from plugins.xiaoqing_chat.handlers import handle_internal
    from plugins.xiaoqing_chat.memory.review_sessions import ReviewPolicy
    from plugins.xiaoqing_chat.runtime_state import ChatRuntimeState
    from plugins.xiaoqing_chat.store_binding import _bind_all_stores

    chat_id = "g67890"
    runtime = MagicMock()
    state   = ChatRuntimeState()
    _bind_all_stores(state, tmp_path)

    state.review_store.save_policy(
        chat_id,
        ReviewPolicy(
            goal_override  = "旧目标覆写",
            strategy_note  = "旧策略备注",
            avoid_patterns = ["避免句式A", "避免句式B"],
        ),
    )
    # 连续写入以生成原子备份，验证重置不会只清理主文件。
    state.review_store.save_policy(
        chat_id,
        ReviewPolicy(goal_override="旧目标覆写", strategy_note="旧策略备注"),
    )
    session = state.review_store.open_session_if_allowed(
        kind             = "goal_strategy",
        chat_id          = chat_id,
        payload          = {"goal": "旧目标", "stats": "旧统计"},
        timeout_seconds  = 600.0,
        cooldown_seconds = 0.0,
    )
    assert session is not None
    second_session = state.review_store.open_session_if_allowed(
        kind             = "reply_style",
        chat_id          = chat_id,
        payload          = {"style": "旧风格"},
        timeout_seconds  = 600.0,
        cooldown_seconds = 0.0,
    )
    assert second_session is not None

    _set_context_principal(mock_context, sample_group_event, group_role="admin")
    hctx = _make_hctx(runtime=runtime, state=state, context=mock_context, data_dir=tmp_path)
    with patch("plugins.xiaoqing_chat.handlers.HandlerContext.from_event", return_value=hctx):
        result = await handle_internal("重置", "确认", sample_group_event, mock_context)

    assert result == [{"type": "text", "data": {"text": "✅ 已重置会话记忆"}}]

    policy_after_reset = state.review_store.get_policy(chat_id)
    assert policy_after_reset.goal_override == ""
    assert policy_after_reset.strategy_note == ""
    assert policy_after_reset.avoid_patterns == []

    sessions_after_reset = [x for x in state.review_store.list_sessions() if x.chat_id == chat_id]
    assert sessions_after_reset == []

    policy_path = tmp_path / "review_sessions" / "policies" / f"{chat_id}.json"
    assert not policy_path.exists()
    assert not policy_path.with_name(f"{policy_path.name}.bak").exists()
    sessions_path = tmp_path / "review_sessions" / "sessions.json"
    for candidate in (sessions_path, sessions_path.with_name(f"{sessions_path.name}.bak")):
        assert chat_id not in candidate.read_text(encoding="utf-8")


@pytest.mark.parametrize("planner_action", ["send_new_message", "say_goodbye"])
@pytest.mark.asyncio
async def test_smalltalk_action_history_preserves_real_planner_action_name(
    mock_context, sample_group_event, planner_action
):
    from plugins.xiaoqing_chat.handlers import _maybe_reply_smalltalk

    lock                              = asyncio.Lock()
    state                             = MagicMock()
    state.get_mood_state.return_value = ""
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.memory_store.get_recent_async = AsyncMock(return_value=[])
    state.memory_store.append             = Mock()
    state.heartflow.on_user_message_async = AsyncMock()
    state.heartflow.on_bot_reply_async    = AsyncMock()
    state.heartflow.on_no_reply_async     = AsyncMock()
    state.inc_stats                       = Mock()
    state.action_history.append           = Mock()
    state.pfc_state_store.get_async       = AsyncMock(
        return_value=SimpleNamespace(
            chat_id                      = "g67890",
            ignore_until_ts              = 0.0,
            ended                        = False,
            last_successful_reply_action = "",
            goal_list                    = [],
            knowledge_list               = [],
            planner_fail_ts              = [],
            planner_skip_until           = 0.0,
            updated_at                   = 0.0,
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
                return_value=SimpleNamespace(
                    reply="补一句", action=planner_action, reason="planner", ended=False
                )
            ),
        ),
        patch("plugins.xiaoqing_chat.handlers._most_recent_user_local_id", return_value="u1"),
        patch("plugins.xiaoqing_chat.handlers._spawn_post_reply_bg_tasks", new=AsyncMock()),
        patch("plugins.xiaoqing_chat.handlers._schedule_memory_persist"),
        patch("plugins.xiaoqing_chat.handlers._schedule_action_history_flush"),
        patch("plugins.xiaoqing_chat.handlers._schedule_pfc_state_flush"),
        patch("plugins.xiaoqing_chat.handlers._freq_record"),
        patch("plugins.xiaoqing_chat.handlers._log_step"),
        patch(
            "plugins.xiaoqing_chat.handlers.maybe_add_mode_indicator",
            side_effect=lambda reply, runtime: reply,
        ),
    ):
        result = await _maybe_reply_smalltalk("继续", sample_group_event, mock_context)
        await _ack_reply_delivery(result)

    assert result == [{"type": "text", "data": {"text": "补一句"}}]
    record = state.action_history.append.call_args.args[1]
    assert record.action == planner_action


@pytest.mark.asyncio
async def test_run_pfc_once_rethink_goal_passes_replanned_goal_focus_to_reply_generator(tmp_path):
    from plugins.xiaoqing_chat.config.config import XiaoQingChatConfig
    from plugins.xiaoqing_chat.memory.memory import MemoryStore
    from plugins.xiaoqing_chat.planning.action_history import ActionHistoryStore
    from plugins.xiaoqing_chat.planning.pfc_action_planner import PFCPlan
    from plugins.xiaoqing_chat.planning.pfc_engine import run_pfc_once
    from plugins.xiaoqing_chat.planning.pfc_state import PFCStateStore

    chat_id              = "pfc-goal-replan"
    cfg                  = XiaoQingChatConfig()
    context              = MagicMock()
    context.data_dir     = tmp_path
    context.http_session = AsyncMock()

    memory_store = MemoryStore()
    memory_store.append(chat_id, role="user", name="Tester", content="今天去哪家火锅店")
    action_history  = ActionHistoryStore()
    memory_db       = MagicMock()
    pfc_state_store = PFCStateStore()

    generate_reply = AsyncMock(return_value="ok")

    with (
        patch(
            "plugins.xiaoqing_chat.planning.pfc_engine.plan_next_action",
            new=AsyncMock(
                side_effect=[
                    PFCPlan(
                        action       = "rethink_goal",
                        reason       = "当前目标过时，需要重设",
                        thinking     = "先确认预算和口味偏好",
                        wait_seconds = 0,
                    ),
                    PFCPlan(
                        action       = "direct_reply",
                        reason       = "继续回复",
                        thinking     = "",
                        wait_seconds = 0,
                    ),
                ]
            ),
        ),
        patch(
            "plugins.xiaoqing_chat.planning.pfc_engine.analyze_goals",
            new=AsyncMock(
                return_value=[
                    {
                        "goal": "帮用户选一家合适的火锅店",
                        "focus": "预算与口味偏好",
                    }
                ]
            ),
        ),
    ):
        result = await run_pfc_once(
            context         = context,
            runtime_cfg     = cfg,
            secrets         = {"api_base": "http://test", "api_key": "key", "model": "test-model"},
            bot_name        = "小青",
            is_private      = False,
            chat_id         = chat_id,
            current_text    = "今天去哪家火锅店",
            memory_store    = memory_store,
            action_history  = action_history,
            memory_db       = memory_db,
            pfc_state_store = pfc_state_store,
            generate_reply  = generate_reply,
        )

    assert result.reply == "ok"
    assert generate_reply.await_count == 1
    assert generate_reply.await_args is not None
    _, planner_reason, extra_reason = generate_reply.await_args.args
    merged_reason = (f"{planner_reason}\n{extra_reason}").strip()
    assert "帮用户选一家合适的火锅店" in merged_reason
    assert "预算与口味偏好" in merged_reason
