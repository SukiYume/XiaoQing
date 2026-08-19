"""命令、帮助和运行态边界。"""

from __future__ import annotations

import tests.helpers.xiaoqing_chat_test_support as _fixture_support
from tests.helpers.xiaoqing_chat_test_support import (
    Any,
    AsyncMock,
    MagicMock,
    Mock,
    SimpleNamespace,
    _build_xiaoqing_catalog,
    _make_hctx,
    asyncio,
    cast,
    patch,
    pytest,
    time,
    xiaoqing_chat,
)

mock_context = _fixture_support.mock_context
sample_group_event = _fixture_support.sample_group_event


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_xiaoqing_chat_init(mock_context):
    """Test plugin initialization"""
    # Should not raise
    await xiaoqing_chat.init(mock_context)
    mock_context.logger.info.assert_called()


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_xiaoqing_chat_handle_chat_command(mock_context, sample_group_event):
    """Test handle with chat command"""
    from plugins.xiaoqing_chat.main import handle

    with patch(
        "plugins.xiaoqing_chat.main.handle_smalltalk", new=AsyncMock(return_value=[])
    ) as mock_smalltalk:
        await handle(
            command="xc",
            args="你好",
            event=sample_group_event,
            context=mock_context,
        )

        mock_smalltalk.assert_called_once()


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_xiaoqing_chat_handle_help_command(mock_context, sample_group_event):
    """Test handle with help subcommand"""
    result = await xiaoqing_chat.handle(
        command="xc",
        args="help",
        event=sample_group_event,
        context=mock_context,
    )

    assert len(result) > 0
    assert "智能对话" in result[0]["data"]["text"]


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_xiaoqing_chat_help_with_extra_arg_does_not_fall_through_to_chat(
    mock_context, sample_group_event
):
    with patch(
        "plugins.xiaoqing_chat.main.handle_smalltalk", new=AsyncMock(return_value=[])
    ) as mock_smalltalk:
        result = await xiaoqing_chat.handle(
            command="xc",
            args="help extra",
            event=sample_group_event,
            context=mock_context,
        )

    assert "不接受额外参数" in result[0]["data"]["text"]
    assert "/xc help" in result[0]["data"]["text"]
    mock_smalltalk.assert_not_awaited()


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_xiaoqing_chat_handle_reset_command(mock_context, sample_group_event):
    """Test handle with reset subcommand"""
    from plugins.xiaoqing_chat.main import handle

    with patch(
        "plugins.xiaoqing_chat.main.handle_internal", new=AsyncMock(return_value=[])
    ) as mock_internal:
        await handle(
            command="xc",
            args="reset",
            event=sample_group_event,
            context=mock_context,
        )

        mock_internal.assert_called_once()


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_xiaoqing_chat_handle_stats_command(mock_context, sample_group_event):
    """Test handle with stats subcommand"""
    from plugins.xiaoqing_chat.main import handle

    with patch(
        "plugins.xiaoqing_chat.main.handle_internal", new=AsyncMock(return_value=[])
    ) as mock_internal:
        await handle(
            command="xc",
            args="stats",
            event=sample_group_event,
            context=mock_context,
        )

        mock_internal.assert_called_once()


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_xiaoqing_chat_handle_brain_command(mock_context, sample_group_event):
    """Test handle with brain subcommand"""
    from plugins.xiaoqing_chat.main import handle

    with patch(
        "plugins.xiaoqing_chat.main.handle_internal", new=AsyncMock(return_value=[])
    ) as mock_internal:
        await handle(
            command="xc",
            args="brain",
            event=sample_group_event,
            context=mock_context,
        )

        mock_internal.assert_called_once()


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_xiaoqing_chat_handle_unknown_command(mock_context, sample_group_event):
    """Test handle with unknown subcommand"""
    from plugins.xiaoqing_chat.main import handle

    with patch(
        "plugins.xiaoqing_chat.main.handle_smalltalk",
        new=AsyncMock(return_value=[{"type": "text", "data": {"text": "mock_response"}}]),
    ) as mock_smalltalk:
        result = await handle(
            command="xc",
            args="unknown_subcommand",
            event=sample_group_event,
            context=mock_context,
        )

        # Should be treated as smalltalk
        mock_smalltalk.assert_called_once()
        assert len(result) > 0


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_xiaoqing_chat_handle_empty_args(mock_context, sample_group_event):
    """Test handle with empty args"""
    from plugins.xiaoqing_chat.main import handle

    result = await handle(
        command="xc",
        args="",
        event=sample_group_event,
        context=mock_context,
    )

    assert len(result) > 0
    text = result[0]["data"]["text"]
    assert "智能对话" in text or "可用命令" in text


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_xiaoqing_chat_handle_exception(mock_context, sample_group_event):
    """Test handle handles exceptions gracefully"""
    from plugins.xiaoqing_chat.main import handle

    with patch(
        "plugins.xiaoqing_chat.main._resolve_invocation",
        side_effect=Exception("Test error"),
    ):
        result = await handle(
            command="xc",
            args="test",
            event=sample_group_event,
            context=mock_context,
        )

        # Should return error message
        assert len(result) > 0


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_xiaoqing_chat_handle_config_command(mock_context, sample_group_event):
    """Test handle with /xc 配置 subcommand"""
    from plugins.xiaoqing_chat.main import handle

    result = await handle(
        command="xc",
        args="配置",
        event=sample_group_event,
        context=mock_context,
    )

    assert len(result) > 0
    assert "配置" in result[0]["data"]["text"]


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_xiaoqing_chat_handle_memory_command(mock_context, sample_group_event):
    """Test handle with /xc 记忆 subcommand"""
    from plugins.xiaoqing_chat.main import handle

    result = await handle(
        command="xc",
        args="记忆",
        event=sample_group_event,
        context=mock_context,
    )

    assert len(result) > 0
    assert "记忆" in result[0]["data"]["text"]
    assert "/xc 记忆 <关键词>" in result[0]["data"]["text"]
    assert "/xc记忆" not in result[0]["data"]["text"]


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_xiaoqing_chat_handle_expression_command(mock_context, sample_group_event):
    """Test handle with /xc 表达 subcommand"""
    from plugins.xiaoqing_chat.main import handle

    result = await handle(
        command="xc",
        args="表达",
        event=sample_group_event,
        context=mock_context,
    )

    assert len(result) > 0
    assert "表达" in result[0]["data"]["text"]


@pytest.mark.plugin
def test_xiaoqing_chat_show_help():
    """帮助直接由 Core 命令目录生成。"""
    from plugins.xiaoqing_chat.main import _help_text

    root = _build_xiaoqing_catalog()
    help_text = _help_text(
        SimpleNamespace(
            command_invocation=None,
            get_command_catalog=lambda: (root,),
            get_settings_snapshot=lambda: SimpleNamespace(config={"bot_name": "小青"}),
        )
    )

    assert "智能对话" in help_text
    assert "/xc" in help_text
    assert "清空" in help_text
    assert "统计" in help_text
    assert "深度" in help_text


@pytest.mark.plugin
def test_xiaoqing_chat_show_help_contains_all_sections():
    """帮助包含全部结构化子命令码。"""
    from plugins.xiaoqing_chat.main import _help_text

    root = _build_xiaoqing_catalog()
    help_text = _help_text(
        SimpleNamespace(
            command_invocation=None,
            get_command_catalog=lambda: (root,),
            get_settings_snapshot=lambda: SimpleNamespace(config={"bot_name": "小青"}),
        )
    )

    assert all(node.code in help_text for node in root.walk())


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_call_bot_name_only_internal(mock_context):
    """Test call_bot_name_only_internal returns response"""
    from plugins.xiaoqing_chat.main import call_bot_name_only_internal

    result = await call_bot_name_only_internal(mock_context)

    assert isinstance(result, list)
    assert len(result) > 0
    assert result[0]["type"] == "text"


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_call_bot_name_only_internal_uses_configured_replies(mock_context):
    from plugins.xiaoqing_chat.main import call_bot_name_only_internal

    mock_context.config = {
        "bot_name": "小青",
        "plugins": {"xiaoqing_chat": {"bot_name_only_replies": ["收到"]}},
    }

    result = await call_bot_name_only_internal(mock_context)

    assert result[0]["data"]["text"] == "收到"


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_call_bot_name_only_internal_varies_response(mock_context):
    """Test call_bot_name_only_internal can return different responses"""
    from plugins.xiaoqing_chat.main import call_bot_name_only_internal

    responses = set()
    for _ in range(20):
        result = await call_bot_name_only_internal(mock_context)
        if result:
            responses.add(result[0]["data"]["text"])

    # Should have at least some variety
    assert len(responses) > 0


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_call_bot_name_only_internal_marks_followup_pending(mock_context):
    """Calling only the bot name should force the next same-user group turn."""
    from plugins.xiaoqing_chat.main import call_bot_name_only_internal
    from plugins.xiaoqing_chat.runtime_state import get_state, reset_global_state

    reset_global_state()
    try:
        result = await call_bot_name_only_internal(mock_context)

        assert result
        state = get_state()
        assert state.consume_pending_bot_name_call("g67890", 12345, now=time.time() + 1.0) is True
        assert state.consume_pending_bot_name_call("g67890", 12345, now=time.time() + 1.0) is False
    finally:
        reset_global_state()


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_pending_bot_name_followup_bypasses_reply_gate(mock_context, sample_group_event):
    from plugins.xiaoqing_chat.handlers import _prepare_smalltalk_turn

    event = dict(sample_group_event)
    event["raw_message"] = "起床了没"
    event["message"] = [{"type": "text", "data": {"text": "起床了没"}}]

    state = MagicMock()
    state.consume_pending_bot_name_call = Mock(return_value=True)
    state.get_mood_state.return_value = ""

    runtime = SimpleNamespace(
        cfg=SimpleNamespace(
            goal=SimpleNamespace(enable_goal=False),
            reflection=SimpleNamespace(enable_expression_reflection=False),
            brain_chat=SimpleNamespace(enable_private_brain_chat=False),
            personality=SimpleNamespace(states=[], state_probability=0.0),
        )
    )
    hctx = _make_hctx(runtime=runtime, state=state, context=mock_context)

    with (
        patch("plugins.xiaoqing_chat.handlers._should_ignore_text", return_value=False),
        patch("plugins.xiaoqing_chat.handlers._is_at_me", return_value=False),
        patch("plugins.xiaoqing_chat.handlers._has_bot_name", return_value=False),
        patch("plugins.xiaoqing_chat.handlers._is_private", return_value=False),
        patch(
            "plugins.xiaoqing_chat.handlers._should_reply", new=AsyncMock(return_value=False)
        ) as gate,
    ):
        prepared = await _prepare_smalltalk_turn("起床了没", event, mock_context, hctx)

    assert prepared is not None
    assert prepared.forced is True
    assert prepared.force_reason == "bot_name_followup"
    state.consume_pending_bot_name_call.assert_called_once_with("g67890", 12345)
    gate.assert_not_awaited()


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_shutdown(mock_context):
    """Test plugin shutdown"""
    from plugins.xiaoqing_chat.main import shutdown

    # Should not raise
    await shutdown(mock_context)
    mock_context.logger.info.assert_called()


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_shutdown_flushes_media_store(mock_context):
    from plugins.xiaoqing_chat.main import shutdown

    state = MagicMock()
    state.background_tasks = Mock(return_value=set())
    state.action_history.flush = Mock()
    state.media_store.flush = Mock()
    state.memory_db.is_dirty = Mock(return_value=False)

    with patch("plugins.xiaoqing_chat.main._state", return_value=state):
        await shutdown(mock_context)

    state.action_history.flush.assert_called_once_with()
    state.media_store.flush.assert_called_once_with()


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_handle_with_all_chat_command_variants(mock_context, sample_group_event):
    """Test handle recognizes chat via /xc <content>"""
    from plugins.xiaoqing_chat.main import handle

    with patch(
        "plugins.xiaoqing_chat.main.handle_smalltalk", new=AsyncMock(return_value=[])
    ) as mock_smalltalk:
        await handle(
            command="xc",
            args="test",
            event=sample_group_event,
            context=mock_context,
        )

        assert mock_smalltalk.call_count == 1


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_handle_with_all_reset_command_variants(mock_context, sample_group_event):
    """Test handle recognizes reset subcommand aliases"""
    from plugins.xiaoqing_chat.main import handle

    subcommands = ["reset", "重置", "清空"]

    with patch(
        "plugins.xiaoqing_chat.main.handle_internal", new=AsyncMock(return_value=[])
    ) as mock_internal:
        for sub in subcommands:
            await handle(
                command="xc",
                args=sub,
                event=sample_group_event,
                context=mock_context,
            )

        assert mock_internal.call_count == len(subcommands)


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_handle_with_all_stats_command_variants(mock_context, sample_group_event):
    """Test handle recognizes stats subcommand aliases"""
    from plugins.xiaoqing_chat.main import handle

    subcommands = ["stats", "统计", "状态"]

    with patch(
        "plugins.xiaoqing_chat.main.handle_internal", new=AsyncMock(return_value=[])
    ) as mock_internal:
        for sub in subcommands:
            await handle(
                command="xc",
                args=sub,
                event=sample_group_event,
                context=mock_context,
            )

        assert mock_internal.call_count == len(subcommands)


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_handle_with_all_brain_command_variants(mock_context, sample_group_event):
    """Test handle recognizes brain subcommand aliases"""
    from plugins.xiaoqing_chat.main import handle

    subcommands = ["brain", "深度"]

    with patch(
        "plugins.xiaoqing_chat.main.handle_internal", new=AsyncMock(return_value=[])
    ) as mock_internal:
        for sub in subcommands:
            await handle(
                command="xc",
                args=sub,
                event=sample_group_event,
                context=mock_context,
            )

        assert mock_internal.call_count == len(subcommands)


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_handle_smalltalk_called_with_correct_params(mock_context, sample_group_event):
    """Test handle_smalltalk is called with correct parameters"""
    from plugins.xiaoqing_chat.main import handle

    sample_group_event["raw_message"] = "/xc 测试消息"

    with patch(
        "plugins.xiaoqing_chat.main.handle_smalltalk", new=AsyncMock(return_value=[])
    ) as mock_smalltalk:
        await handle(
            command="xc",
            args="测试消息",
            event=sample_group_event,
            context=mock_context,
        )

        # Verify handle_smalltalk was called with args (not raw_message)
        call_args = mock_smalltalk.call_args
        assert call_args[0][0] == "测试消息"  # clean_text
        assert call_args[0][1] == sample_group_event  # event
        assert call_args[0][2] == mock_context  # context


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_handle_internal_called_with_correct_params(mock_context, sample_group_event):
    """Test handle_internal is called with correct parameters"""
    from plugins.xiaoqing_chat.main import handle

    with patch(
        "plugins.xiaoqing_chat.main.handle_internal", new=AsyncMock(return_value=[])
    ) as mock_internal:
        await handle(
            command="xc",
            args="reset",
            event=sample_group_event,
            context=mock_context,
        )

        # Verify handle_internal was called
        call_args = mock_internal.call_args
        assert call_args[0][0] == "重置"  # command


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_handle_with_no_raw_message(mock_context):
    """Test handle with event missing raw_message routes to smalltalk"""
    from plugins.xiaoqing_chat.main import handle

    event = {"post_type": "message"}  # No raw_message

    with patch(
        "plugins.xiaoqing_chat.main.handle_smalltalk", new=AsyncMock(return_value=[])
    ) as mock_st:
        result = await handle(
            command="xc",
            args="test",
            event=event,
            context=mock_context,
        )

        # Should route to handle_smalltalk with forced flag
        assert result == []
        assert mock_st.called
        assert event.get("_xc_command_forced") is True


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_handle_with_empty_raw_message(mock_context, sample_group_event):
    """Test handle with empty raw_message"""
    from plugins.xiaoqing_chat.main import handle

    sample_group_event["raw_message"] = ""

    result = await handle(
        command="xc",
        args="",
        event=sample_group_event,
        context=mock_context,
    )

    # Should return help text when no args provided
    assert len(result) > 0
    assert "智能对话" in result[0]["data"]["text"]


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_handle_with_whitespace_args(mock_context, sample_group_event):
    """Test handle with whitespace-only args"""
    from plugins.xiaoqing_chat.main import handle

    with patch("core.args.parse") as mock_parse:
        mock_parse.return_value = MagicMock(first=None, rest=Mock(return_value=""))

        result = await handle(
            command="xc",
            args="   ",
            event=sample_group_event,
            context=mock_context,
        )

        # Should handle gracefully and return help
        assert len(result) > 0


@pytest.mark.asyncio
async def test_runtime_cleanup_eviction_also_cleans_locks_and_persist_tasks():
    from plugins.xiaoqing_chat.runtime_state import ChatRuntimeState

    old_limit = ChatRuntimeState._MAX_TRACKED_CHATS
    ChatRuntimeState._MAX_TRACKED_CHATS = 2
    state = ChatRuntimeState()
    try:
        loop = asyncio.get_running_loop()
        for i in range(5):
            cid = f"g{i}"
            state.get_lock(cid)
            state.set_last_reply_ts(cid, float(i))
            done_task = loop.create_future()
            done_task.set_result(None)
            state.set_persist_task(cid, cast(Any, done_task))

        state.cleanup_stale_chats()

        assert len(state._per_chat.locks) <= 2
        assert len(state._per_chat.persist_tasks) <= 2
    finally:
        ChatRuntimeState._MAX_TRACKED_CHATS = old_limit


@pytest.mark.asyncio
async def test_runtime_cleanup_stale_prefers_recent_observe_activity():
    from plugins.xiaoqing_chat.runtime_state import ChatRuntimeState

    old_limit = ChatRuntimeState._MAX_TRACKED_CHATS
    ChatRuntimeState._MAX_TRACKED_CHATS = 1
    state = ChatRuntimeState()
    try:
        state.get_lock("g_observe")
        state.get_lock("g_reply")
        state.set_last_reply_ts("g_reply", 100.0)
        state.set_last_observe_ts("g_reply", 100.0)
        state.set_last_observe_ts("g_observe", 200.0)

        state.cleanup_stale_chats()

        assert "g_observe" in state._per_chat.locks
        assert "g_reply" not in state._per_chat.locks
    finally:
        ChatRuntimeState._MAX_TRACKED_CHATS = old_limit


@pytest.mark.asyncio
async def test_action_history_flush_task_entry_removed_after_task_done():
    from plugins.xiaoqing_chat.task_scheduler import (
        _action_flush_tasks,
        _schedule_action_history_flush,
    )

    context = MagicMock()
    runtime = cast(Any, SimpleNamespace(cfg=SimpleNamespace(io_persist_debounce_seconds=0.0)))
    fake_state = MagicMock()

    _action_flush_tasks.clear()
    try:
        with (
            patch("plugins.xiaoqing_chat.task_scheduler._state", return_value=fake_state),
            patch(
                "plugins.xiaoqing_chat.task_scheduler.asyncio.to_thread",
                new=AsyncMock(return_value=None),
            ),
        ):
            _schedule_action_history_flush(context, runtime, chat_id="g1")
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        assert "g1" not in _action_flush_tasks
    finally:
        _action_flush_tasks.clear()
