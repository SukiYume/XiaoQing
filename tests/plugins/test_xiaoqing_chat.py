"""Tests for xiaoqing_chat plugin"""

import asyncio
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from core.interfaces import PluginCapabilities, PluginPrincipal
from plugins.xiaoqing_chat.handler_context import HandlerContext, handle_errors
from plugins.xiaoqing_chat.llm.reply_checker import ReplyRejected
from plugins.xiaoqing_chat.message_parts import build_text_message_parts, message_parts_to_legacy


def _make_hctx(
    *,
    runtime,
    state,
    context,
    event=None,
    chat_id="g67890",
    bot_name="小青",
    secrets=None,
    data_dir=None,
) -> HandlerContext:
    """Build a HandlerContext without going through from_event."""
    return HandlerContext(
        chat_id=chat_id,
        runtime=runtime,
        state=state,
        secrets=secrets if secrets is not None else {},
        data_dir=data_dir
        if data_dir is not None
        else (
            context.data_dir if context else Path(tempfile.gettempdir()) / "xiaoqing_chat_test_data"
        ),
        bot_name=bot_name,
        context=context,
    )


def _set_context_principal(
    context,
    event: dict[str, Any],
    *,
    group_role: str = "member",
    is_bot_admin: bool = False,
) -> None:
    group_id = event.get("group_id")
    context.principal = PluginPrincipal(
        kind="user",
        user_id=event.get("user_id"),
        group_id=group_id,
        is_bot_admin=is_bot_admin,
        is_private=group_id in (None, ""),
        group_role=group_role,
    )
    context.capabilities = PluginCapabilities(is_bot_admin=is_bot_admin)


def _provider_test_secrets() -> dict[str, Any]:
    return {
        "plugins": {
            "xiaoqing_chat": {
                "default": "deepseek",
                "providers": {
                    "deepseek": {"model": "deepseek-chat", "api_base": "http://a"},
                    "glm": {"model": "glm-4", "api_base": "http://b"},
                },
            }
        }
    }


ROOT = Path(__file__).resolve().parent.parent.parent

# Import the plugin module using the package structure
from plugins.xiaoqing_chat import main as xiaoqing_chat


def _reply_draft(text: str) -> SimpleNamespace:
    return SimpleNamespace(text=text, parts=build_text_message_parts(text))


# ============================================================
# Fixtures
# ============================================================


@pytest.fixture
def mock_context(tmp_path: Path):
    """Create a mock plugin context for xiaoqing_chat"""
    context = MagicMock()
    context.config = {"bot_name": "小青"}
    context.secrets = {
        "openai_api_key": "test_key",
        "plugins": {
            "xiaoqing_chat": {"api_key": "test", "api_base": "http://test", "model": "test-model"}
        },
    }
    context.plugin_name = "xiaoqing_chat"
    context.plugin_dir = tmp_path / "plugins" / "xiaoqing_chat"
    context.data_dir = tmp_path / "data" / "xiaoqing_chat"
    context.http_session = AsyncMock()
    context.send_action = AsyncMock()
    context.reload_config = Mock()
    context.reload_plugins = Mock()
    context.list_commands = Mock(return_value=["help: 查看帮助"])
    context.list_plugins = Mock(return_value=["xiaoqing_chat"])
    context.current_user_id = 12345
    context.current_group_id = 67890
    context.request_id = "test-request-123"
    context.state = {}
    context.logger = MagicMock()
    context.session_manager = None
    context.config_manager = MagicMock()
    return context


@pytest.fixture
def sample_group_event():
    """Create a sample group message event"""
    return {
        "post_type": "message",
        "message_type": "group",
        "time": 1234567890,
        "self_id": 11111,
        "user_id": 12345,
        "group_id": 67890,
        "message": [{"type": "text", "data": {"text": "/xc 你好"}}],
        "raw_message": "/xc 你好",
        "font": 0,
        "sender": {
            "user_id": 12345,
            "nickname": "TestUser",
            "card": "",
            "sex": "unknown",
            "age": 0,
            "area": "",
            "level": "",
            "role": "member",
            "title": "",
        },
        "message_id": 1,
        "message_seq": 1,
    }


@pytest.fixture
def sample_private_event():
    """Create a sample private message event"""
    return {
        "post_type": "message",
        "message_type": "private",
        "time": 1234567890,
        "self_id": 11111,
        "user_id": 12345,
        "message": "你好",
        "raw_message": "你好",
        "font": 0,
        "sender": {
            "user_id": 12345,
            "nickname": "TestUser",
            "sex": "unknown",
            "age": 0,
        },
        "message_id": 1,
    }


# ============================================================
# Main Module Tests
# ============================================================


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_xiaoqing_chat_init(mock_context):
    """Test plugin initialization"""
    # Should not raise
    xiaoqing_chat.init(mock_context)
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
    assert "小青智能对话" in result[0]["data"]["text"]


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
    assert "小青智能对话" in text or "可用命令" in text


@pytest.mark.plugin
@pytest.mark.asyncio
async def test_xiaoqing_chat_handle_exception(mock_context, sample_group_event):
    """Test handle handles exceptions gracefully"""
    from plugins.xiaoqing_chat.main import handle

    with patch("plugins.xiaoqing_chat.main.parse", side_effect=Exception("Test error")):
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


# ============================================================
# Help Text Tests
# ============================================================


@pytest.mark.plugin
def test_xiaoqing_chat_show_help():
    """Test _show_help returns help text"""
    from plugins.xiaoqing_chat.main import _show_help

    help_text = _show_help()

    assert "小青智能对话" in help_text
    assert "/xc" in help_text
    assert "清空" in help_text
    assert "统计" in help_text
    assert "深度" in help_text


@pytest.mark.plugin
def test_xiaoqing_chat_show_help_contains_all_sections():
    """Test help contains all major sections"""
    from plugins.xiaoqing_chat.main import _show_help

    help_text = _show_help()

    assert "基础对话" in help_text or "基础" in help_text
    assert "会话管理" in help_text or "会话" in help_text
    assert "高级功能" in help_text or "高级" in help_text
    assert "使用提示" in help_text or "提示" in help_text


# ============================================================
# Bot Name Only Tests
# ============================================================


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
        assert state.consume_pending_bot_name_call(
            "g67890", 12345, now=time.time() + 1.0
        ) is True
        assert state.consume_pending_bot_name_call(
            "g67890", 12345, now=time.time() + 1.0
        ) is False
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


# ============================================================
# Shutdown Tests
# ============================================================


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
    state._bg_tasks = set()
    state.action_history.flush_all = Mock()
    state.media_store.flush_all = Mock()
    state.memory_db.is_dirty = Mock(return_value=False)

    with patch("plugins.xiaoqing_chat.main._state", return_value=state):
        await shutdown(mock_context)

    state.action_history.flush_all.assert_called_once_with()
    state.media_store.flush_all.assert_called_once_with()


# ============================================================
# Command Variants Tests
# ============================================================


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


# ============================================================
# Integration Tests
# ============================================================


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


# ============================================================
# Edge Cases Tests
# ============================================================


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
    assert "小青智能对话" in result[0]["data"]["text"]


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
    setattr(ChatRuntimeState, "_MAX_TRACKED_CHATS", 2)
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
        setattr(ChatRuntimeState, "_MAX_TRACKED_CHATS", old_limit)


@pytest.mark.asyncio
async def test_runtime_cleanup_stale_prefers_recent_observe_activity():
    from plugins.xiaoqing_chat.runtime_state import ChatRuntimeState

    old_limit = ChatRuntimeState._MAX_TRACKED_CHATS
    setattr(ChatRuntimeState, "_MAX_TRACKED_CHATS", 1)
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
        setattr(ChatRuntimeState, "_MAX_TRACKED_CHATS", old_limit)


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


# ---- reply gate tests ----
class TestReplyGate:
    @pytest.mark.asyncio
    async def test_heartflow_low_score_does_not_reduce_group_base_probability(self):
        from unittest.mock import MagicMock, patch

        runtime = MagicMock()
        runtime.cfg.reply_probability_base = 0.6
        runtime.cfg.min_reply_interval_seconds = 0.0
        runtime.cfg.max_replies_per_minute = 100
        runtime.cfg.continuous_reply_limit = 0
        runtime.cfg.continuous_cooldown_seconds = 0.0
        runtime.cfg.heartflow.enable_heartflow = True
        runtime.cfg.heartflow.base_score = 0.2
        runtime.cfg.heartflow.weight_question = 0.12
        runtime.cfg.heartflow.weight_goal_match = 0.06
        runtime.cfg.heartflow.weight_short_text = -0.08
        runtime.cfg.heartflow.weight_no_reply_streak = 0.05
        runtime.cfg.heartflow.weight_long_silence = 0.08
        runtime.cfg.brain_chat.enable_private_brain_chat = False
        runtime.cfg.goal.enable_goal = False

        state = MagicMock()
        state.get_last_reply_ts.return_value = 0.0
        state.get_continuous_cooldown_until.return_value = 0.0
        state.get_reply_timestamps.return_value = []
        state.goal_store.get_async = AsyncMock(return_value=SimpleNamespace(goal=""))
        state.heartflow.score_async = AsyncMock(return_value=0.2)
        state.heartflow.get_async = AsyncMock(return_value=SimpleNamespace(no_reply_streak=0))

        from plugins.xiaoqing_chat.frequency_control import _should_reply

        with patch("plugins.xiaoqing_chat.frequency_control.random") as mock_rand:
            mock_rand.random.return_value = 0.55
            result = await _should_reply(
                runtime, state, "g1", "今天天气不错", False, False
            )

        assert result is True

    @pytest.mark.asyncio
    async def test_reply_gate_records_random_skip_reason(self):
        from plugins.xiaoqing_chat.frequency_control import _should_reply

        runtime = MagicMock()
        runtime.cfg.reply_probability_base = 0.6
        runtime.cfg.min_reply_interval_seconds = 0.0
        runtime.cfg.max_replies_per_minute = 100
        runtime.cfg.heartflow.enable_heartflow = False
        runtime.cfg.brain_chat.enable_private_brain_chat = False
        runtime.cfg.goal.enable_goal = False

        state = MagicMock()
        state.get_last_reply_ts.return_value = 0.0
        state.get_continuous_cooldown_until.return_value = 0.0
        state.get_reply_timestamps.return_value = []
        state.goal_store.get_async = AsyncMock(return_value=None)
        state.heartflow.get_async = AsyncMock(return_value=SimpleNamespace(no_reply_streak=0))
        state.set_reply_gate_decision = Mock()

        with patch("plugins.xiaoqing_chat.frequency_control.random.random", return_value=0.99):
            result = await _should_reply(
                runtime, state, "g1", "今天天气不错", False, False
            )

        assert result is False
        decision = state.set_reply_gate_decision.call_args.args[1]
        assert decision.reason == "probability"
        assert decision.probability == 0.6
        assert decision.roll == 0.99

    @pytest.mark.asyncio
    async def test_reply_gate_records_min_interval_skip_reason(self):
        from plugins.xiaoqing_chat.frequency_control import _should_reply

        runtime = MagicMock()
        runtime.cfg.reply_probability_base = 0.6
        runtime.cfg.min_reply_interval_seconds = 10.0
        runtime.cfg.max_replies_per_minute = 100
        runtime.cfg.heartflow.enable_heartflow = False
        runtime.cfg.brain_chat.enable_private_brain_chat = False
        runtime.cfg.goal.enable_goal = False

        state = MagicMock()
        state.get_last_reply_ts.return_value = 95.0
        state.get_continuous_cooldown_until.return_value = 0.0
        state.get_reply_timestamps.return_value = []
        state.goal_store.get_async = AsyncMock(return_value=None)
        state.heartflow.get_async = AsyncMock(return_value=SimpleNamespace(no_reply_streak=0))
        state.set_reply_gate_decision = Mock()

        with patch("plugins.xiaoqing_chat.frequency_control.time.time", return_value=100.0):
            result = await _should_reply(
                runtime, state, "g1", "今天天气不错", False, False
            )

        assert result is False
        decision = state.set_reply_gate_decision.call_args.args[1]
        assert decision.reason == "min_interval"
        assert decision.seconds_since_last_reply == 5.0
        assert decision.min_interval_seconds == 10.0

    @pytest.mark.asyncio
    async def test_active_topic_min_interval_uses_configured_value(self):
        from plugins.xiaoqing_chat.frequency_control import _should_reply

        runtime = MagicMock()
        runtime.cfg.reply_probability_base = 0.6
        runtime.cfg.min_reply_interval_seconds = 10.0
        runtime.cfg.active_topic_min_reply_interval = 4.0
        runtime.cfg.max_replies_per_minute = 100
        runtime.cfg.heartflow.enable_heartflow = False
        runtime.cfg.brain_chat.enable_private_brain_chat = False
        runtime.cfg.goal.enable_goal = True

        state = MagicMock()
        state.get_last_reply_ts.return_value = 97.0
        state.get_continuous_cooldown_until.return_value = 0.0
        state.get_reply_timestamps.return_value = []
        state.goal_store.get_async = AsyncMock(
            return_value=SimpleNamespace(goal="自然聊天", ts=95.0)
        )
        state.heartflow.get_async = AsyncMock(return_value=SimpleNamespace(no_reply_streak=0))
        state.set_reply_gate_decision = Mock()

        with patch("plugins.xiaoqing_chat.frequency_control.time.time", return_value=100.0):
            result = await _should_reply(runtime, state, "g1", "继续聊", False, False)

        assert result is False
        decision = state.set_reply_gate_decision.call_args.args[1]
        assert decision.reason == "min_interval"
        assert decision.active_topic is True
        assert decision.min_interval_seconds == 4.0

    @pytest.mark.asyncio
    async def test_group_heartflow_bonus_stays_soft(self):
        from unittest.mock import MagicMock, patch

        runtime = MagicMock()
        runtime.cfg.reply_probability_base = 0.6
        runtime.cfg.min_reply_interval_seconds = 0.0
        runtime.cfg.max_replies_per_minute = 100
        runtime.cfg.continuous_reply_limit = 0
        runtime.cfg.continuous_cooldown_seconds = 0.0
        runtime.cfg.heartflow.enable_heartflow = True
        runtime.cfg.heartflow.base_score = 0.2
        runtime.cfg.heartflow.weight_question = 0.12
        runtime.cfg.heartflow.weight_goal_match = 0.06
        runtime.cfg.heartflow.weight_short_text = -0.08
        runtime.cfg.heartflow.weight_no_reply_streak = 0.05
        runtime.cfg.heartflow.weight_long_silence = 0.08
        runtime.cfg.brain_chat.enable_private_brain_chat = False
        runtime.cfg.goal.enable_goal = False

        state = MagicMock()
        state.get_last_reply_ts.return_value = 0.0
        state.get_continuous_cooldown_until.return_value = 0.0
        state.get_reply_timestamps.return_value = []
        state.goal_store.get_async = AsyncMock(return_value=SimpleNamespace(goal=""))
        state.heartflow.score_async = AsyncMock(return_value=0.32)
        state.heartflow.get_async = AsyncMock(return_value=SimpleNamespace(no_reply_streak=0))

        from plugins.xiaoqing_chat.frequency_control import _should_reply

        with patch("plugins.xiaoqing_chat.frequency_control.random") as mock_rand:
            mock_rand.random.return_value = 0.67
            result = await _should_reply(
                runtime, state, "g1", "今天天气不错？", False, False
            )

        assert result is False

    @pytest.mark.asyncio
    async def test_long_no_reply_streak_can_break_through_low_interest_group_chatter(self):
        from unittest.mock import MagicMock, patch

        runtime = MagicMock()
        runtime.cfg.reply_probability_base = 0.5
        runtime.cfg.min_reply_interval_seconds = 0.0
        runtime.cfg.max_replies_per_minute = 100
        runtime.cfg.continuous_reply_limit = 0
        runtime.cfg.continuous_cooldown_seconds = 0.0
        runtime.cfg.heartflow.enable_heartflow = False
        runtime.cfg.brain_chat.enable_private_brain_chat = False
        runtime.cfg.goal.enable_goal = False

        state = MagicMock()
        state.get_last_reply_ts.return_value = 0.0
        state.get_continuous_cooldown_until.return_value = 0.0
        state.get_reply_timestamps.return_value = []
        state.goal_store.get_async = AsyncMock(return_value=SimpleNamespace(goal=""))
        state.heartflow.score_async = AsyncMock(return_value=1.0)
        state.heartflow.get_async = AsyncMock(return_value=SimpleNamespace(no_reply_streak=12))

        from plugins.xiaoqing_chat.frequency_control import _should_reply

        with patch("plugins.xiaoqing_chat.frequency_control.random") as mock_rand:
            mock_rand.random.return_value = 0.3
            result = await _should_reply(runtime, state, "g1", "哦", False, False)

        assert result is True

def test_group_wait_plan_is_preserved_before_first_reply(tmp_path):
    from plugins.xiaoqing_chat.config.config import XiaoQingChatConfig
    from plugins.xiaoqing_chat.memory.memory import MemoryStore
    from plugins.xiaoqing_chat.planning.action_history import ActionHistoryStore
    from plugins.xiaoqing_chat.planning.pfc_action_planner import PFCPlan
    from plugins.xiaoqing_chat.planning.pfc_engine import run_pfc_once
    from plugins.xiaoqing_chat.planning.pfc_state import PFCStateStore

    chat_id = "group-1"
    cfg = XiaoQingChatConfig()
    context = MagicMock()
    context.data_dir = tmp_path
    context.http_session = AsyncMock()

    memory_store = MemoryStore()
    memory_store.append(chat_id, role="user", name="Tester", content="有人在聊火锅")

    action_history = ActionHistoryStore()
    memory_db = MagicMock()
    pfc_state_store = PFCStateStore()
    generate_reply = AsyncMock(return_value="我也想吃火锅")

    with patch(
        "plugins.xiaoqing_chat.planning.pfc_engine.plan_next_action",
        new=AsyncMock(
            return_value=PFCPlan(
                action="wait", reason="先看看", thinking="群里还在聊", wait_seconds=20
            )
        ),
    ):
        result = asyncio.run(
            run_pfc_once(
                context=context,
                runtime_cfg=cfg,
                secrets={"api_base": "http://test", "api_key": "key", "model": "test-model"},
                bot_name="小青",
                is_private=False,
                chat_id=chat_id,
                current_text="有人在聊火锅",
                memory_store=memory_store,
                action_history=action_history,
                memory_db=memory_db,
                pfc_state_store=pfc_state_store,
                generate_reply=generate_reply,
            )
        )

    assert result.action == "wait"
    assert result.reply == ""
    generate_reply.assert_not_awaited()


def test_group_wait_plan_is_preserved_after_bot_already_replied(tmp_path):
    from plugins.xiaoqing_chat.config.config import XiaoQingChatConfig
    from plugins.xiaoqing_chat.memory.memory import MemoryStore
    from plugins.xiaoqing_chat.planning.action_history import ActionHistoryStore
    from plugins.xiaoqing_chat.planning.pfc_action_planner import PFCPlan
    from plugins.xiaoqing_chat.planning.pfc_engine import run_pfc_once
    from plugins.xiaoqing_chat.planning.pfc_state import PFCStateStore

    chat_id = "group-2"
    cfg = XiaoQingChatConfig()
    context = MagicMock()
    context.data_dir = tmp_path
    context.http_session = AsyncMock()

    memory_store = MemoryStore()
    memory_store.append(chat_id, role="assistant", name="小青", content="刚刚说过了")
    memory_store.append(chat_id, role="user", name="Tester", content="收到")

    action_history = ActionHistoryStore()
    memory_db = MagicMock()
    pfc_state_store = PFCStateStore()
    pfc_state_store.bind(tmp_path)
    st = pfc_state_store.get(chat_id)
    st.last_successful_reply_action = "direct_reply"
    pfc_state_store.save(chat_id)
    generate_reply = AsyncMock(return_value="不该发送")

    with patch(
        "plugins.xiaoqing_chat.planning.pfc_engine.plan_next_action",
        new=AsyncMock(
            return_value=PFCPlan(action="wait", reason="先等等", thinking="刚说完", wait_seconds=20)
        ),
    ):
        result = asyncio.run(
            run_pfc_once(
                context=context,
                runtime_cfg=cfg,
                secrets={"api_base": "http://test", "api_key": "key", "model": "test-model"},
                bot_name="小青",
                is_private=False,
                chat_id=chat_id,
                current_text="收到",
                memory_store=memory_store,
                action_history=action_history,
                memory_db=memory_db,
                pfc_state_store=pfc_state_store,
                generate_reply=generate_reply,
            )
        )

    assert result.action == "wait"
    assert result.reply == ""
    generate_reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_pfc_once_drops_stale_followup_reply_action(tmp_path):
    from plugins.xiaoqing_chat.config.config import XiaoQingChatConfig
    from plugins.xiaoqing_chat.memory.memory import MemoryStore
    from plugins.xiaoqing_chat.planning.action_history import ActionHistoryStore
    from plugins.xiaoqing_chat.planning.pfc_action_planner import PFCPlan
    from plugins.xiaoqing_chat.planning.pfc_engine import run_pfc_once
    from plugins.xiaoqing_chat.planning.pfc_state import PFCStateStore

    now = time.time()
    chat_id = "stale-followup-action"
    cfg = XiaoQingChatConfig()
    cfg.pfc_followup_action_window_seconds = 120.0
    context = MagicMock()
    context.data_dir = tmp_path
    context.http_session = AsyncMock()

    memory_store = MemoryStore()
    memory_store.append(chat_id, role="assistant", name="小青", content="很久前说过", ts=now - 3600)
    memory_store.append(chat_id, role="user", name="Tester", content="新图来了", ts=now - 1)

    action_history = ActionHistoryStore()
    memory_db = MagicMock()
    pfc_state_store = PFCStateStore()
    pfc_state_store.bind(tmp_path)
    st = pfc_state_store.get(chat_id)
    st.last_successful_reply_action = "direct_reply"
    pfc_state_store.save(chat_id)
    generate_reply = AsyncMock(return_value="不该发送")
    captured: dict[str, str] = {}

    async def fake_plan_next_action(**kwargs):
        captured["last_successful_reply_action"] = kwargs["last_successful_reply_action"]
        return PFCPlan(action="wait", reason="先看看", thinking="", wait_seconds=0)

    with patch(
        "plugins.xiaoqing_chat.planning.pfc_engine.plan_next_action",
        new=fake_plan_next_action,
    ):
        result = await run_pfc_once(
            context=context,
            runtime_cfg=cfg,
            secrets={"api_base": "http://test", "api_key": "key", "model": "test-model"},
            bot_name="小青",
            is_private=False,
            chat_id=chat_id,
            current_text="新图来了",
            memory_store=memory_store,
            action_history=action_history,
            memory_db=memory_db,
            pfc_state_store=pfc_state_store,
            generate_reply=generate_reply,
        )

    assert captured["last_successful_reply_action"] == ""
    assert pfc_state_store.get(chat_id).last_successful_reply_action == ""
    assert result.action == "wait"
    generate_reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_pfc_once_keeps_recent_followup_reply_action(tmp_path):
    from plugins.xiaoqing_chat.config.config import XiaoQingChatConfig
    from plugins.xiaoqing_chat.memory.memory import MemoryStore
    from plugins.xiaoqing_chat.planning.action_history import ActionHistoryStore
    from plugins.xiaoqing_chat.planning.pfc_action_planner import PFCPlan
    from plugins.xiaoqing_chat.planning.pfc_engine import run_pfc_once
    from plugins.xiaoqing_chat.planning.pfc_state import PFCStateStore

    now = time.time()
    chat_id = "recent-followup-action"
    cfg = XiaoQingChatConfig()
    cfg.pfc_followup_action_window_seconds = 120.0
    context = MagicMock()
    context.data_dir = tmp_path
    context.http_session = AsyncMock()

    memory_store = MemoryStore()
    memory_store.append(chat_id, role="assistant", name="小青", content="刚说过", ts=now - 30)
    memory_store.append(chat_id, role="user", name="Tester", content="接一句", ts=now - 1)

    action_history = ActionHistoryStore()
    memory_db = MagicMock()
    pfc_state_store = PFCStateStore()
    pfc_state_store.bind(tmp_path)
    st = pfc_state_store.get(chat_id)
    st.last_successful_reply_action = "direct_reply"
    pfc_state_store.save(chat_id)
    generate_reply = AsyncMock(return_value="")
    captured: dict[str, str] = {}

    async def fake_plan_next_action(**kwargs):
        captured["last_successful_reply_action"] = kwargs["last_successful_reply_action"]
        return PFCPlan(action="wait", reason="刚说过，等一下", thinking="", wait_seconds=0)

    with patch(
        "plugins.xiaoqing_chat.planning.pfc_engine.plan_next_action",
        new=fake_plan_next_action,
    ):
        result = await run_pfc_once(
            context=context,
            runtime_cfg=cfg,
            secrets={"api_base": "http://test", "api_key": "key", "model": "test-model"},
            bot_name="小青",
            is_private=False,
            chat_id=chat_id,
            current_text="接一句",
            memory_store=memory_store,
            action_history=action_history,
            memory_db=memory_db,
            pfc_state_store=pfc_state_store,
            generate_reply=generate_reply,
        )

    assert captured["last_successful_reply_action"] == "direct_reply"
    assert result.action == "wait"


@pytest.mark.asyncio
async def test_run_pfc_once_promotes_group_wait_plan_for_live_short_followups(tmp_path):
    from plugins.xiaoqing_chat.config.config import XiaoQingChatConfig
    from plugins.xiaoqing_chat.memory.memory import MemoryStore
    from plugins.xiaoqing_chat.planning.action_history import ActionHistoryStore
    from plugins.xiaoqing_chat.planning.pfc_action_planner import PFCPlan
    from plugins.xiaoqing_chat.planning.pfc_engine import run_pfc_once
    from plugins.xiaoqing_chat.planning.pfc_state import PFCStateStore

    now = time.time()
    chat_id = "group-live-short-followups"
    cfg = XiaoQingChatConfig()
    context = MagicMock()
    context.data_dir = tmp_path
    context.http_session = AsyncMock()

    memory_store = MemoryStore()
    memory_store.append(chat_id, role="assistant", name="小青", content="刚刚说过了", ts=now - 25)
    memory_store.append(chat_id, role="user", name="Tester", content="乐", ts=now - 5)
    memory_store.append(chat_id, role="user", name="Tester", content="今儿碰见了", ts=now - 1)

    action_history = ActionHistoryStore()
    memory_db = MagicMock()
    pfc_state_store = PFCStateStore()
    pfc_state_store.bind(tmp_path)
    st = pfc_state_store.get(chat_id)
    st.last_successful_reply_action = "direct_reply"
    pfc_state_store.save(chat_id)
    generate_reply = AsyncMock(return_value="细说，碰见谁了")

    with patch(
        "plugins.xiaoqing_chat.planning.pfc_engine.plan_next_action",
        new=AsyncMock(
            return_value=PFCPlan(
                action="wait",
                reason="没有人在跟我直接对话，先等等",
                thinking="群里在接话，但不是明确问我",
                wait_seconds=20,
            )
        ),
    ):
        result = await run_pfc_once(
            context=context,
            runtime_cfg=cfg,
            secrets={"api_base": "http://test", "api_key": "key", "model": "test-model"},
            bot_name="小青",
            is_private=False,
            chat_id=chat_id,
            current_text="今儿碰见了",
            memory_store=memory_store,
            action_history=action_history,
            memory_db=memory_db,
            pfc_state_store=pfc_state_store,
            generate_reply=generate_reply,
        )

    assert result.action == "send_new_message"
    assert result.reply == "细说，碰见谁了"
    generate_reply.assert_awaited_once()


@pytest.mark.asyncio
async def test_memory_store_get_async_loads_via_to_thread(tmp_path):
    from plugins.xiaoqing_chat.memory.memory import MemoryStore

    chat_id = "threaded-load"
    store = MemoryStore(tmp_path)
    store.persist(chat_id)
    (tmp_path / f"{chat_id}.json").write_text(
        '[{"role":"user","name":"Tester","content":"hello","ts":1.0}]',
        encoding="utf-8",
    )

    async def fake_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    with patch(
        "plugins.xiaoqing_chat.memory.memory.asyncio.to_thread",
        new=AsyncMock(side_effect=fake_to_thread),
    ) as mock_to_thread:
        history = await store.get_async(chat_id)

    assert mock_to_thread.await_count == 1
    assert len(history) == 1
    assert history[0].content == "hello"


@pytest.mark.asyncio
async def test_memory_store_reloads_after_binding_data_dir(tmp_path):
    from plugins.xiaoqing_chat.memory.memory import MemoryStore

    chat_id = "rebinding-load"
    store = MemoryStore()

    assert await store.get_async(chat_id) == []

    store.bind_data_dir(tmp_path)
    (tmp_path / f"{chat_id}.json").write_text(
        '[{"role":"user","name":"Tester","content":"restored","ts":1.0}]',
        encoding="utf-8",
    )

    history = await store.get_async(chat_id)

    assert len(history) == 1
    assert history[0].content == "restored"


def test_memory_store_append_prunes_in_memory_history():
    from plugins.xiaoqing_chat.memory.memory import MemoryStore

    store = MemoryStore()
    chat_id = "bounded-history"

    for index in range(250):
        store.append(chat_id, role="user", name="Tester", content=f"msg-{index}")

    history = store.get(chat_id)
    assert len(history) == 200
    assert history[0].content == "msg-50"
    assert history[-1].content == "msg-249"


def test_memory_store_persists_media_items(tmp_path):
    import json

    from plugins.xiaoqing_chat.memory.memory import MemoryStore

    chat_id = "media-history"
    store = MemoryStore(tmp_path)
    store.append(
        chat_id,
        role="user",
        name="Tester",
        content="[图片：猫猫在发呆]",
        media_items=[
            {
                "kind": "image",
                "media_hash": "hash-cat",
                "marker": "[图片：猫猫在发呆]",
                "emotion_tags": ["发呆"],
            }
        ],
    )
    store.persist(chat_id)

    persisted = json.loads((tmp_path / f"{chat_id}.json").read_text(encoding="utf-8"))

    reloaded = MemoryStore(tmp_path).get(chat_id)

    assert len(reloaded) == 1
    assert "parts" in persisted[0]
    assert "content" not in persisted[0]
    assert "media_items" not in persisted[0]
    assert reloaded[0].media_items[0]["media_hash"] == "hash-cat"
    assert reloaded[0].media_items[0]["marker"] == "[图片：猫猫在发呆]"


def test_memory_store_keeps_media_only_messages_on_reload(tmp_path):
    from plugins.xiaoqing_chat.memory.memory import MemoryStore

    chat_id = "media-only-history"
    store = MemoryStore(tmp_path)
    store.append(
        chat_id,
        role="assistant",
        name="小青",
        content="[[xc_media_1]]",
        media_items=[
            {
                "kind": "qq_face",
                "face_id": "14",
                "marker": "[QQ表情：微笑]",
            }
        ],
    )
    store.persist(chat_id)

    reloaded = MemoryStore(tmp_path).get(chat_id)

    assert len(reloaded) == 1
    assert reloaded[0].content == "[[xc_media_1]]"
    assert reloaded[0].media_items[0]["face_id"] == "14"


def test_memory_store_persists_message_parts_round_trip(tmp_path):
    from plugins.xiaoqing_chat.memory.memory import MemoryStore

    chat_id = "parts-round-trip"
    store = MemoryStore(tmp_path)
    store.append(
        chat_id,
        role="assistant",
        name="小青",
        content="",
        parts=[
            {"kind": "text", "text": "先看这个"},
            {
                "kind": "emoji",
                "media_hash": "hash-emoji-1",
                "marker": "[表情包：猫猫翻白眼]",
                "description": "猫猫翻白眼",
                "emotion_tags": ["无语"],
            },
            {"kind": "text", "text": "再说"},
            {
                "kind": "qq_face",
                "face_id": "277",
                "marker": "[QQ表情：狗头]",
                "label": "狗头",
            },
        ],
    )
    store.persist(chat_id)

    reloaded = MemoryStore(tmp_path).get(chat_id)

    assert len(reloaded) == 1
    assert reloaded[0].content == "先看这个[[xc_media_1]]再说[[xc_media_2]]"
    assert [part["kind"] for part in reloaded[0].parts] == ["text", "emoji", "text", "qq_face"]
    assert reloaded[0].media_items[0]["media_hash"] == "hash-emoji-1"
    assert reloaded[0].media_items[1]["face_id"] == "277"


def test_memory_store_append_prefers_canonical_parts_over_stale_legacy_fields():
    from plugins.xiaoqing_chat.memory.memory import MemoryStore

    chat_id = "parts-first-append"
    store = MemoryStore()
    store.append(
        chat_id,
        role="assistant",
        name="小青",
        content="旧内容[[xc_media_1]]",
        media_items=[
            {
                "kind": "qq_face",
                "face_id": "14",
                "marker": "[QQ表情：微笑]",
            }
        ],
        parts=[
            {"kind": "text", "text": "先看这个"},
            {
                "kind": "emoji",
                "media_hash": "hash-emoji-1",
                "marker": "[表情包：猫猫翻白眼]",
                "description": "猫猫翻白眼",
                "emotion_tags": ["无语"],
            },
            {"kind": "text", "text": "再说"},
        ],
    )

    history = store.get(chat_id)

    assert len(history) == 1
    assert history[0].content == "先看这个[[xc_media_1]]再说"
    assert history[0].media_items[0]["media_hash"] == "hash-emoji-1"
    assert history[0].media_items[0]["marker"] == "[表情包：猫猫翻白眼]"


def test_memory_store_load_prefers_canonical_parts_over_stale_legacy_fields(tmp_path):
    import json

    from plugins.xiaoqing_chat.memory.memory import MemoryStore

    chat_id = "parts-first-load"
    (tmp_path / f"{chat_id}.json").write_text(
        json.dumps(
            [
                {
                    "role": "assistant",
                    "name": "小青",
                    "content": "旧内容[[xc_media_1]]",
                    "media_items": [
                        {
                            "kind": "qq_face",
                            "face_id": "14",
                            "marker": "[QQ表情：微笑]",
                        }
                    ],
                    "parts": [
                        {"kind": "text", "text": "先看这个"},
                        {
                            "kind": "emoji",
                            "media_hash": "hash-emoji-1",
                            "marker": "[表情包：猫猫翻白眼]",
                            "description": "猫猫翻白眼",
                            "emotion_tags": ["无语"],
                        },
                        {"kind": "text", "text": "再说"},
                    ],
                    "ts": 1.0,
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    reloaded = MemoryStore(tmp_path).get(chat_id)

    assert len(reloaded) == 1
    assert reloaded[0].content == "先看这个[[xc_media_1]]再说"
    assert reloaded[0].media_items[0]["media_hash"] == "hash-emoji-1"
    assert reloaded[0].media_items[0]["marker"] == "[表情包：猫猫翻白眼]"


def test_get_data_dir_prefers_context_data_dir_even_when_plugin_data_exists(mock_context):
    from plugins.xiaoqing_chat.handlers import _get_data_dir

    assert _get_data_dir(mock_context) == mock_context.data_dir


@pytest.mark.asyncio
async def test_handle_internal_stats_uses_async_memory_read(mock_context, sample_group_event):
    from plugins.xiaoqing_chat.handlers import handle_internal

    runtime = SimpleNamespace(
        cfg=SimpleNamespace(
            brain_chat=SimpleNamespace(
                enable_private_brain_chat=False,
                brain_max_context_size=30,
                brain_think_level=2,
                brain_temperature=0.7,
            ),
            memory=SimpleNamespace(enable_memory_retrieval=True, top_k=5, min_score=0.1),
            expression=SimpleNamespace(
                enable_expression_learning=True, max_injected=5, max_store=200
            ),
            reply_probability_base=0.6,
            min_reply_interval_seconds=12.0,
            max_replies_per_minute=6,
            max_context_size=30,
        )
    )

    state = MagicMock()
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.memory_store.get.side_effect = AssertionError("sync memory read should not be used")
    state.bw_expr_store.load.return_value = []
    state.bw_jargon_store.load.return_value = {}
    state.action_history.get_recent_async = AsyncMock(return_value=[])
    state.action_history.get_recent.return_value = []
    state.get_stats.return_value = {"replies": 1, "resets": 0}

    hctx = _make_hctx(runtime=runtime, state=state, context=mock_context)
    with patch("plugins.xiaoqing_chat.handlers.HandlerContext.from_event", return_value=hctx):
        result = await handle_internal("统计", "", sample_group_event, mock_context)

    assert state.memory_store.get_async.await_count == 1
    assert "会话统计" in result[0]["data"]["text"]


@pytest.mark.asyncio
async def test_handle_internal_stats_uses_async_action_history_read(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.handlers import handle_internal

    runtime = SimpleNamespace(
        cfg=SimpleNamespace(
            brain_chat=SimpleNamespace(
                enable_private_brain_chat=False,
                brain_max_context_size=30,
                brain_think_level=2,
                brain_temperature=0.7,
            ),
            memory=SimpleNamespace(enable_memory_retrieval=True, top_k=5, min_score=0.1),
            expression=SimpleNamespace(
                enable_expression_learning=True, max_injected=5, max_store=200
            ),
            reply_probability_base=0.6,
            min_reply_interval_seconds=12.0,
            max_replies_per_minute=6,
            max_context_size=30,
        )
    )

    state = MagicMock()
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.bw_expr_store.load.return_value = []
    state.bw_jargon_store.load.return_value = {}
    state.action_history.get_recent_async = AsyncMock(return_value=[])
    state.action_history.get_recent.side_effect = AssertionError(
        "sync action history read should not be used"
    )
    state.get_stats.return_value = {"replies": 1, "resets": 0}

    hctx = _make_hctx(runtime=runtime, state=state, context=mock_context)
    with patch("plugins.xiaoqing_chat.handlers.HandlerContext.from_event", return_value=hctx):
        result = await handle_internal("统计", "", sample_group_event, mock_context)

    assert state.action_history.get_recent_async.await_count == 1
    assert "近期行动记录" in result[0]["data"]["text"]


@pytest.mark.asyncio
async def test_extract_and_learn_uses_async_memory_read(mock_context):
    from plugins.xiaoqing_chat.expression.bw_message_recorder import (
        MessageRecorder,
        extract_and_learn,
    )

    memory_store = MagicMock()
    memory_store.get_async = AsyncMock(return_value=[])
    memory_store.get.side_effect = AssertionError("sync memory read should not be used")
    expr_store = MagicMock()
    recorder = MessageRecorder()

    changed = await extract_and_learn(
        context=mock_context,
        secrets={},
        bot_name="小青",
        chat_id="g1",
        memory_store=memory_store,
        expr_store=expr_store,
        jargon_store=None,
        recorder=recorder,
        personality=MagicMock(),
        min_interval_seconds=0.0,
        min_messages=10,
        self_reflect=True,
        temperature=0.7,
        top_p=0.9,
        max_tokens=128,
        timeout_seconds=1.0,
        max_retry=0,
        retry_interval_seconds=0.0,
        proxy="",
        endpoint_path="/v1/chat/completions",
    )

    assert changed == 0
    assert memory_store.get_async.await_count == 1


@pytest.mark.asyncio
async def test_extract_and_learn_skips_when_same_chat_inflight(mock_context):
    from plugins.xiaoqing_chat.expression.bw_message_recorder import (
        MessageRecorder,
        extract_and_learn,
    )

    memory_store = MagicMock()
    memory_store.get_async = AsyncMock(side_effect=AssertionError("inflight run should skip early"))
    recorder = MessageRecorder()
    assert recorder.try_begin("g1") is True

    try:
        changed = await extract_and_learn(
            context=mock_context,
            secrets={},
            bot_name="小青",
            chat_id="g1",
            memory_store=memory_store,
            expr_store=MagicMock(),
            jargon_store=None,
            recorder=recorder,
            personality=MagicMock(),
            min_interval_seconds=0.0,
            min_messages=10,
            self_reflect=True,
            temperature=0.7,
            top_p=0.9,
            max_tokens=128,
            timeout_seconds=1.0,
            max_retry=0,
            retry_interval_seconds=0.0,
            proxy="",
            endpoint_path="/v1/chat/completions",
        )
    finally:
        recorder.end("g1")

    assert changed == 0
    assert memory_store.get_async.await_count == 0


@pytest.mark.asyncio
async def test_extract_and_learn_jargon_empty_response_does_not_fail_task(
    mock_context, monkeypatch
):
    from plugins.xiaoqing_chat.expression.bw_expression_store import ExpressionStore
    from plugins.xiaoqing_chat.expression.bw_jargon_store import JargonStore
    from plugins.xiaoqing_chat.expression.bw_message_recorder import (
        MessageRecorder,
        extract_and_learn,
    )
    from plugins.xiaoqing_chat.llm.llm_client import LLMError
    from plugins.xiaoqing_chat.memory.memory import MemoryStore

    memory_store = MemoryStore()
    for idx in range(10):
        memory_store.append(
            "g1",
            role="user",
            name="User",
            content=f"测试消息{idx}",
            local_id=f"m{idx + 1}",
            ts=float(idx + 1),
        )

    async def fake_learn_from_messages(**_kwargs):
        return [{"text": "哈哈", "description": "轻松口语"}]

    async def fake_upsert_learned(**_kwargs):
        return 2

    async def raise_empty_response(**_kwargs):
        raise LLMError("empty_response")

    monkeypatch.setattr(
        "plugins.xiaoqing_chat.expression.bw_message_recorder.learn_from_messages",
        fake_learn_from_messages,
    )
    monkeypatch.setattr(
        "plugins.xiaoqing_chat.expression.bw_message_recorder.upsert_learned",
        fake_upsert_learned,
    )
    monkeypatch.setattr(
        "plugins.xiaoqing_chat.expression.bw_jargon_miner.chat_completions_raw_with_fallback_paths",
        raise_empty_response,
    )

    recorder = MessageRecorder()
    changed = await extract_and_learn(
        context=mock_context,
        secrets={"api_base": "https://example.com", "api_key": "k", "model": "m"},
        bot_name="小青",
        chat_id="g1",
        memory_store=memory_store,
        expr_store=ExpressionStore(),
        jargon_store=JargonStore(),
        recorder=recorder,
        personality=MagicMock(),
        min_interval_seconds=0.0,
        min_messages=10,
        self_reflect=True,
        temperature=0.7,
        top_p=0.9,
        max_tokens=128,
        timeout_seconds=1.0,
        max_retry=0,
        retry_interval_seconds=0.0,
        proxy="",
        endpoint_path="/v1/chat/completions",
    )

    assert changed == 2
    assert recorder.get_last_time("g1") > 0
    assert recorder.try_begin("g1") is True
    recorder.end("g1")


@pytest.mark.asyncio
async def test_tick_reflect_tracker_uses_async_memory_read(mock_context):
    from plugins.xiaoqing_chat.expression.bw_reflect_tracker import (
        ReflectTrackerState,
        tick_reflect_tracker,
    )

    tracker_store = MagicMock()
    tracker_store.get_tracker.return_value = ReflectTrackerState(
        operator_chat_id="g1",
        expression_id="expr-1",
        created_time=1.0,
        last_check_count=0,
    )
    expr_store = MagicMock()
    expr_store.load.return_value = []
    memory_store = MagicMock()
    memory_store.get_async = AsyncMock(return_value=[])
    memory_store.get.side_effect = AssertionError("sync memory read should not be used")

    with patch("plugins.xiaoqing_chat.expression.bw_reflect_tracker.time.time", return_value=10.0):
        result = await tick_reflect_tracker(
            context=mock_context,
            operator_chat_id="g1",
            memory_store=memory_store,
            expr_store=expr_store,
            tracker_store=tracker_store,
            secrets={},
            timeout_seconds=1.0,
            max_retry=0,
            retry_interval_seconds=0.0,
            proxy="",
            endpoint_path="/v1/chat/completions",
            max_duration_seconds=900.0,
            max_message_count=30,
        )

    assert result is False
    assert memory_store.get_async.await_count == 1


@pytest.mark.asyncio
async def test_action_history_get_recent_async_loads_via_to_thread(tmp_path):
    from plugins.xiaoqing_chat.planning.action_history import ActionHistoryStore

    chat_id = "action-history-load"
    store = ActionHistoryStore()
    store.bind(tmp_path)
    action_dir = tmp_path / "action_history"
    action_dir.mkdir(parents=True, exist_ok=True)
    (action_dir / f"{chat_id}.json").write_text(
        '[{"ts":1.0,"local_target":"u1","action":"reply","reasoning":"ok","detail":{},"executed":true}]',
        encoding="utf-8",
    )

    items = await store.get_recent_async(chat_id, max_items=10)

    assert len(items) == 1
    assert items[0].action == "reply"


@pytest.mark.asyncio
async def test_build_tool_info_block_uses_async_action_history_read(mock_context):
    from plugins.xiaoqing_chat.context_builder import _build_tool_info_block

    state = MagicMock()
    state.get_last_reply_ts.return_value = 0.0
    state.get_continuous_cooldown_until.return_value = 0.0
    state.get_reply_timestamps.return_value = []
    state.get_continuous_reply_count.return_value = 0
    state.action_history.get_recent_async = AsyncMock(return_value=[])
    state.action_history.get_recent.side_effect = AssertionError(
        "sync action history read should not be used"
    )

    block = await _build_tool_info_block(
        runtime=MagicMock(),
        state=state,
        data_dir=mock_context.data_dir,
        bot_name="小青",
        chat_id="g1",
        event={"user_id": 1},
        goal="",
    )

    assert isinstance(block, str)
    assert state.action_history.get_recent_async.await_count == 1


@pytest.mark.asyncio
async def test_ensure_user_message_recorded_uses_async_heartflow(mock_context, sample_group_event):
    from plugins.xiaoqing_chat.handlers import _ensure_user_message_recorded

    runtime = MagicMock()
    state = MagicMock()
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.memory_store.append = Mock()
    state.heartflow.on_user_message_async = AsyncMock()
    state.heartflow.on_user_message.side_effect = AssertionError(
        "sync heartflow update should not be used"
    )

    with (
        patch("plugins.xiaoqing_chat.handlers._state", return_value=state),
        patch("plugins.xiaoqing_chat.handlers._get_data_dir", return_value=mock_context.data_dir),
        patch("plugins.xiaoqing_chat.handlers._bind_all_stores"),
        patch("plugins.xiaoqing_chat.handlers._schedule_memory_persist"),
    ):
        local_id = await _ensure_user_message_recorded(
            "你好", sample_group_event, mock_context, runtime
        )

    assert local_id
    assert state.heartflow.on_user_message_async.await_count == 1


@pytest.mark.asyncio
async def test_observe_message_skips_prefixed_xc_command(mock_context, sample_group_event):
    from plugins.xiaoqing_chat.handlers import observe_message

    runtime = SimpleNamespace(cfg=SimpleNamespace(enable_smalltalk=True))
    event = dict(sample_group_event)
    event["message"] = [{"type": "text", "data": {"text": "/xc 你好"}}]
    event["raw_message"] = "/xc 你好"

    with (
        patch("plugins.xiaoqing_chat.handlers._load_runtime", return_value=runtime),
        patch(
            "plugins.xiaoqing_chat.handlers._ensure_user_message_recorded",
            new=AsyncMock(),
        ) as mock_record,
    ):
        result = await observe_message("xc 你好", event, mock_context)

    assert result == []
    mock_record.assert_not_awaited()


@pytest.mark.asyncio
async def test_observe_outgoing_action_records_external_plugin_text_only(mock_context):
    from plugins.xiaoqing_chat.handlers import observe_outgoing_action

    runtime = SimpleNamespace(
        cfg=SimpleNamespace(enable_smalltalk=True, ban_words=[]),
        compiled_ban_regex=[],
    )
    state = MagicMock()
    state.memory_store.append = Mock()
    state.heartflow.on_bot_reply_async = AsyncMock()
    state.action_history.append = Mock()
    action = {
        "action": "send_group_msg",
        "params": {
            "group_id": 67890,
            "message": [
                {
                    "type": "text",
                    "data": {
                        "text": "中国地震台网正式测定：04月24日11时18分在地中海东部发生5.7级地震。"
                    },
                },
                {"type": "image", "data": {"file": "file:///tmp/quake.jpg"}},
            ],
        },
    }

    with (
        patch("plugins.xiaoqing_chat.handlers._load_runtime", return_value=runtime),
        patch("plugins.xiaoqing_chat.handlers._get_bound_state", return_value=state),
        patch("plugins.xiaoqing_chat.handlers._get_lock", return_value=asyncio.Lock()),
        patch("plugins.xiaoqing_chat.handlers._next_local_id", return_value="m-out"),
        patch("plugins.xiaoqing_chat.handlers._schedule_memory_persist"),
        patch("plugins.xiaoqing_chat.handlers._schedule_action_history_flush"),
    ):
        result = await observe_outgoing_action(action, mock_context, source_plugin="earthquake")

    assert result == []
    append_kwargs = state.memory_store.append.call_args.kwargs
    assert state.memory_store.append.call_args.args[0] == "g67890"
    assert append_kwargs["role"] == "assistant"
    assert append_kwargs["name"] == "小青"
    content, media_items = message_parts_to_legacy(append_kwargs["parts"])
    assert "地中海东部" in content
    assert "5.7级地震" in content
    assert "[图片" not in content
    assert media_items == []
    state.heartflow.on_bot_reply_async.assert_awaited_once_with(chat_id="g67890")


@pytest.mark.asyncio
async def test_observe_outgoing_action_skips_sensitive_external_plugin_output(mock_context):
    from plugins.xiaoqing_chat.handlers import observe_outgoing_action

    runtime = SimpleNamespace(
        cfg=SimpleNamespace(enable_smalltalk=True, ban_words=[]),
        compiled_ban_regex=[],
    )
    state = MagicMock()
    state.memory_store.append = Mock()
    action = {
        "action": "send_private_msg",
        "params": {
            "user_id": 1000000001,
            "message": [
                {
                    "type": "text",
                    "data": {
                        "text": (
                            "🔑 Pendo Web 登录 Token\n"
                            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
                            "eyJzdWIiOiIxMjMifQ.signature"
                        )
                    },
                },
            ],
        },
    }

    with (
        patch("plugins.xiaoqing_chat.handlers._load_runtime", return_value=runtime),
        patch("plugins.xiaoqing_chat.handlers._get_bound_state", return_value=state),
    ):
        result = await observe_outgoing_action(action, mock_context, source_plugin="pendo")

    assert result == []
    state.memory_store.append.assert_not_called()


@pytest.mark.asyncio
async def test_observe_outgoing_action_skips_xiaoqing_source(mock_context):
    from plugins.xiaoqing_chat.handlers import observe_outgoing_action

    state = MagicMock()
    action = {
        "action": "send_group_msg",
        "params": {
            "group_id": 67890,
            "message": [{"type": "text", "data": {"text": "这是小青自己回复的内容"}}],
        },
    }

    with patch("plugins.xiaoqing_chat.handlers._get_bound_state", return_value=state):
        result = await observe_outgoing_action(action, mock_context, source_plugin="xiaoqing_chat")

    assert result == []
    state.memory_store.append.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_user_message_recorded_persists_rendered_media_items(mock_context, sample_group_event):
    from plugins.xiaoqing_chat.handlers import _ensure_user_message_recorded

    runtime = MagicMock()
    state = MagicMock()
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.memory_store.get_recent_async = AsyncMock(return_value=[])
    state.memory_store.append = Mock()
    state.heartflow.on_user_message_async = AsyncMock()
    state.media_store.upsert_media_items = Mock(
        return_value=[
            {
                "kind": "image",
                "media_hash": "hash-cat",
                "media_key": "media:hash-cat",
                "marker": "[图片：猫猫在发呆]",
                "description": "猫猫在发呆",
                "emotion_tags": ["发呆"],
                "file_path": str(mock_context.data_dir / "cat.png"),
            }
        ]
    )
    sample_group_event["_xc_rendered_media_items"] = [
        SimpleNamespace(
            kind="image",
            media_hash="hash-cat",
            description="猫猫在发呆",
            emotion_tags=("发呆",),
            marker="[图片：猫猫在发呆]",
            cached_path=mock_context.data_dir / "cat.png",
        )
    ]

    with (
        patch("plugins.xiaoqing_chat.handlers._state", return_value=state),
        patch("plugins.xiaoqing_chat.handlers._get_data_dir", return_value=mock_context.data_dir),
        patch("plugins.xiaoqing_chat.handlers._bind_all_stores"),
        patch("plugins.xiaoqing_chat.handlers._schedule_memory_persist"),
        patch("plugins.xiaoqing_chat.handlers._schedule_media_registry_flush") as mock_media_flush,
    ):
        await _ensure_user_message_recorded("[图片：猫猫在发呆]", sample_group_event, mock_context, runtime)

    append_kwargs = state.memory_store.append.call_args.kwargs
    parts = append_kwargs["parts"]
    content, media_items = message_parts_to_legacy(parts)
    assert content == "[[xc_media_1]]"
    assert media_items[0]["media_hash"] == "hash-cat"
    assert media_items[0]["marker"] == "[图片：猫猫在发呆]"
    assert media_items[0]["media_key"] == "media:hash-cat"
    assert [part["kind"] for part in parts] == ["image"]
    assert "description" not in media_items[0]
    assert "emotion_tags" not in media_items[0]
    mock_media_flush.assert_called_once_with(mock_context, runtime)


@pytest.mark.asyncio
async def test_ensure_user_message_recorded_reuses_cached_effective_parts(mock_context):
    from plugins.xiaoqing_chat.handlers import _ensure_user_message_recorded
    from plugins.xiaoqing_chat.media.event_media import RenderedMedia, build_effective_user_text

    runtime = MagicMock()
    state = MagicMock()
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.memory_store.get_recent_async = AsyncMock(return_value=[])
    state.memory_store.append = Mock()
    state.heartflow.on_user_message_async = AsyncMock()
    state.review_store.cleanup_expired = Mock()
    state.set_last_observe_ts = Mock()
    state.media_store.upsert_media_items = Mock(
        return_value=[
            {
                "kind": "image",
                "media_hash": "hash-cat",
                "media_key": "media:hash-cat",
                "marker": "[图片：猫猫在发呆]",
                "description": "猫猫在发呆",
                "file_path": str(mock_context.data_dir / "cat.png"),
            }
        ]
    )
    event = {
        "post_type": "message",
        "message_type": "group",
        "user_id": 12345,
        "group_id": 67890,
        "message_id": 1,
        "message": [
            {"type": "text", "data": {"text": "看这个"}},
            {"type": "image", "data": {"url": "https://example.com/cat.png"}},
            {"type": "text", "data": {"text": "笑死"}},
        ],
        "_xc_rendered_media_items": [
            RenderedMedia(
                media_hash="hash-cat",
                kind="image",
                description="猫猫在发呆",
                emotion_tags=tuple(),
                marker="[图片：猫猫在发呆]",
                cached_path=mock_context.data_dir / "cat.png",
            )
        ],
    }

    text = await build_effective_user_text(
        "看这个笑死",
        event,
        context=mock_context,
        runtime=runtime,
    )

    with (
        patch("plugins.xiaoqing_chat.handlers._state", return_value=state),
        patch("plugins.xiaoqing_chat.handlers._get_data_dir", return_value=mock_context.data_dir),
        patch("plugins.xiaoqing_chat.handlers._bind_all_stores"),
        patch("plugins.xiaoqing_chat.handlers._schedule_memory_persist"),
        patch(
            "plugins.xiaoqing_chat.handlers.build_message_parts",
            side_effect=AssertionError("cached effective parts should bypass rebuild"),
        ),
    ):
        await _ensure_user_message_recorded(text, event, mock_context, runtime)

    append_kwargs = state.memory_store.append.call_args.kwargs
    parts = append_kwargs["parts"]
    content, media_items = message_parts_to_legacy(parts)
    assert text == "看这个\n[图片：猫猫在发呆]\n笑死"
    assert [part["kind"] for part in event["_xc_effective_user_parts"]] == ["text", "image", "text"]
    assert [part["kind"] for part in parts] == ["text", "image", "text"]
    assert content == "看这个[[xc_media_1]]笑死"
    assert media_items[0]["media_hash"] == "hash-cat"
    assert media_items[0]["media_key"] == "media:hash-cat"


@pytest.mark.asyncio
async def test_run_pfc_once_uses_async_pfc_state_store(tmp_path):
    from plugins.xiaoqing_chat.config.config import XiaoQingChatConfig
    from plugins.xiaoqing_chat.memory.memory import MemoryStore
    from plugins.xiaoqing_chat.planning.action_history import ActionHistoryStore
    from plugins.xiaoqing_chat.planning.pfc_action_planner import PFCPlan
    from plugins.xiaoqing_chat.planning.pfc_engine import run_pfc_once

    chat_id = "pfc-async-state"
    cfg = XiaoQingChatConfig()
    context = MagicMock()
    context.data_dir = tmp_path
    context.http_session = AsyncMock()

    memory_store = MemoryStore()
    memory_store.append(chat_id, role="user", name="Tester", content="你好")
    action_history = ActionHistoryStore()
    memory_db = MagicMock()
    pfc_state_store = MagicMock()
    pfc_state_store.get_async = AsyncMock(
        return_value=SimpleNamespace(
            ignore_until_ts=0.0,
            ended=False,
            last_successful_reply_action="",
            goal_list=[],
            knowledge_list=[],
            planner_fail_ts=[],
            planner_skip_until=0.0,
        )
    )
    pfc_state_store.get.side_effect = AssertionError("sync pfc state read should not be used")
    pfc_state_store.save_async = AsyncMock()
    pfc_state_store.save.side_effect = AssertionError("sync pfc state write should not be used")
    generate_reply = AsyncMock(return_value="ok")

    with patch(
        "plugins.xiaoqing_chat.planning.pfc_engine.plan_next_action",
        new=AsyncMock(
            return_value=PFCPlan(action="wait", reason="先等等", thinking="观察中", wait_seconds=20)
        ),
    ):
        result = await run_pfc_once(
            context=context,
            runtime_cfg=cfg,
            secrets={"api_base": "http://test", "api_key": "key", "model": "test-model"},
            bot_name="小青",
            is_private=False,
            chat_id=chat_id,
            current_text="你好",
            memory_store=memory_store,
            action_history=action_history,
            memory_db=memory_db,
            pfc_state_store=pfc_state_store,
            generate_reply=generate_reply,
        )

    assert result.action == "wait"
    assert pfc_state_store.get_async.await_count == 1


def test_pfc_state_set_state_refreshes_updated_at():
    from plugins.xiaoqing_chat.planning.pfc_state import PFCConversationState, PFCStateStore

    store = PFCStateStore()
    state = PFCConversationState(chat_id="g1", updated_at=1.0)

    before = state.updated_at
    store.set_state("g1", state)

    assert state.updated_at > before
    assert store._cache["g1"] is state


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
async def test_ordinary_group_turn_refreshes_goal_before_should_reply_blocks(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.handlers import _maybe_reply_smalltalk

    state = MagicMock()
    state.goal_store.set_async = AsyncMock()
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


def test_follow_up_compact_prompt_explicitly_allows_brief_interjection_on_new_group_content():
    from plugins.xiaoqing_chat.planning.pfc_action_planner import PROMPT_FOLLOW_UP_COMPACT

    assert "有新内容时可以简短插一句" in PROMPT_FOLLOW_UP_COMPACT
    assert "不要仅因为刚发过言就机械 wait" in PROMPT_FOLLOW_UP_COMPACT


def test_initial_compact_prompt_tells_group_planner_not_to_mechanically_wait():
    from plugins.xiaoqing_chat.planning.pfc_action_planner import PROMPT_INITIAL_REPLY_COMPACT

    assert "不要因为“不是在跟我说”就机械 wait" in PROMPT_INITIAL_REPLY_COMPACT


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
            proxy="",
            endpoint_path="/v1/chat/completions",
        )

    mock_chat.assert_awaited_once()


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
    state.action_history.get_recent.side_effect = AssertionError(
        "sync action history read should not be used"
    )
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
    hctx = SimpleNamespace(chat_id="g67890", runtime=MagicMock(), state=state)
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
    reset_chat_session.assert_awaited_once_with(state, "g67890")
    state.inc_stats.assert_called_once_with("g67890", "resets")
    mock_context.logger.info.assert_called_once()
    assert "reset_audit" in mock_context.logger.info.call_args.args[0]


@pytest.mark.asyncio
async def test_private_reset_remains_limited_to_callers_private_scope(mock_context):
    from plugins.xiaoqing_chat.handlers_internal import handle_internal_impl

    state = MagicMock()
    state.pop_persist_task.return_value = None
    hctx = SimpleNamespace(chat_id="u12345", runtime=MagicMock(), state=state)
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
    reset_chat_session.assert_awaited_once_with(state, "u12345")
    assert "private" in mock_context.logger.info.call_args.args


@pytest.mark.asyncio
async def test_handle_internal_reset_uses_async_pfc_state_store(mock_context, sample_group_event):
    from plugins.xiaoqing_chat.handlers import handle_internal

    runtime = MagicMock()
    state = MagicMock()
    pfc_state = SimpleNamespace(
        ended=True,
        ignore_until_ts=1.0,
        last_successful_reply_action="reply",
        goal_list=[{"goal": "x"}],
        knowledge_list=[{"text": "y"}],
    )
    state.pfc_state_store.get_async = AsyncMock(return_value=pfc_state)
    state.pfc_state_store.get.side_effect = AssertionError("sync pfc state read should not be used")
    state.pfc_state_store.save_async = AsyncMock()
    state.pfc_state_store.save.side_effect = AssertionError(
        "sync pfc state write should not be used"
    )

    _set_context_principal(mock_context, sample_group_event, group_role="admin")
    hctx = _make_hctx(runtime=runtime, state=state, context=mock_context)
    with patch("plugins.xiaoqing_chat.handlers.HandlerContext.from_event", return_value=hctx):
        result = await handle_internal("重置", "确认", sample_group_event, mock_context)

    assert result == [{"type": "text", "data": {"text": "✅ 已重置会话记忆"}}]
    assert state.pfc_state_store.get_async.await_count == 1
    assert state.pfc_state_store.save_async.await_count == 1


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
    state.memory_store.get_recent.return_value = []
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

    assert result == [{"type": "text", "data": {"text": "planner-ok"}}]
    state.pfc_state_store.set_state.assert_called_once()
    mock_schedule_pfc_state_flush.assert_called_once()


@pytest.mark.asyncio
async def test_smalltalk_rejection_persists_updated_pfc_state_and_cancels_speculative_memory(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.handlers import _maybe_reply_smalltalk
    from plugins.xiaoqing_chat.llm.reply_checker import ReplyRejected

    lock = asyncio.Lock()
    cancel_seen = asyncio.Event()
    blocker = asyncio.Event()
    state = MagicMock()
    state.get_lock.return_value = lock
    state.get_mood_state.return_value = ""
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
    state.pfc_state_store.set_state = Mock()
    state.heartflow.on_user_message_async = AsyncMock()
    state.heartflow.on_no_reply_async = AsyncMock()

    runtime = SimpleNamespace(
        cfg=SimpleNamespace(
            enable_smalltalk=True,
            goal=SimpleNamespace(enable_goal=False),
            reflection=SimpleNamespace(
                enable_expression_reflection=False, enable_review_sessions=False
            ),
            brain_chat=SimpleNamespace(enable_private_brain_chat=False),
            max_context_size=6,
            planner=SimpleNamespace(resolve_think_level=lambda history_len=0: 0),
            personality=SimpleNamespace(states=[], state_probability=0.0),
            debug=SimpleNamespace(log_latency=False),
        )
    )

    async def fake_build_memory_block(**_kwargs):
        try:
            await blocker.wait()
        except asyncio.CancelledError:
            cancel_seen.set()
            raise
        return ""

    async def fake_run_pfc_once(**kwargs):
        snapshot = kwargs["state_override"]
        snapshot.goal_list = [{"goal": "重规划后的目标"}]
        snapshot.planner_skip_until = 30.0
        await asyncio.sleep(0)
        raise ReplyRejected("需要重新规划", True)

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
            "plugins.xiaoqing_chat.handlers.run_pfc_once",
            new=AsyncMock(side_effect=fake_run_pfc_once),
        ),
        patch("plugins.xiaoqing_chat.handlers._schedule_memory_persist"),
        patch("plugins.xiaoqing_chat.handlers._schedule_action_history_flush"),
        patch("plugins.xiaoqing_chat.handlers._schedule_pfc_state_flush") as mock_flush,
        patch("plugins.xiaoqing_chat.handlers._log_step"),
    ):
        with pytest.raises(ReplyRejected, match="需要重新规划"):
            await _maybe_reply_smalltalk("你好", sample_group_event, mock_context)

    await asyncio.wait_for(cancel_seen.wait(), timeout=1.0)
    state.pfc_state_store.set_state.assert_called_once()
    persisted = state.pfc_state_store.set_state.call_args.args[1]
    assert persisted.goal_list == [{"goal": "重规划后的目标"}]
    assert persisted.planner_skip_until == 30.0
    mock_flush.assert_called_once()


@pytest.mark.asyncio
async def test_smalltalk_forced_reply_generation_runs_outside_chat_lock_but_commits_inside(
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
    state.pfc_state_store.get_async = AsyncMock()
    state.pfc_state_store.set_state_async = AsyncMock()
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
            planner=SimpleNamespace(resolve_think_level=lambda history_len=0: 0),
            personality=SimpleNamespace(states=[], state_probability=0.0),
            debug=SimpleNamespace(log_latency=False),
        )
    )
    event = dict(sample_group_event)
    event["_xc_command_forced"] = True

    async def fake_generate_reply_draft(**kwargs):
        assert not lock.locked()
        return _reply_draft("forced-ok")

    async def fake_record_bot_reply(*args, **kwargs):
        assert lock.locked()
        return []

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
            "plugins.xiaoqing_chat.handlers._generate_reply_draft",
            new=AsyncMock(side_effect=fake_generate_reply_draft),
        ),
        patch(
            "plugins.xiaoqing_chat.handlers._record_bot_reply",
            new=AsyncMock(side_effect=fake_record_bot_reply),
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

    assert result == [{"type": "text", "data": {"text": "forced-ok"}}]


@pytest.mark.asyncio
async def test_smalltalk_stale_generation_is_dropped_before_commit(
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
    state.pfc_state_store.set_state = Mock()
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
                    reply="planner-ok", action="reply", reason="ok", ended=False
                )
            ),
        ),
        patch("plugins.xiaoqing_chat.handlers._most_recent_user_local_id", return_value="u2"),
        patch(
            "plugins.xiaoqing_chat.handlers._record_bot_reply", new=AsyncMock()
        ) as mock_record_bot_reply,
        patch(
            "plugins.xiaoqing_chat.handlers._schedule_pfc_state_flush",
            create=True,
        ) as mock_schedule_pfc_state_flush,
        patch("plugins.xiaoqing_chat.handlers._spawn_post_reply_bg_tasks", new=AsyncMock()),
        patch("plugins.xiaoqing_chat.handlers._schedule_memory_persist"),
        patch("plugins.xiaoqing_chat.handlers._schedule_action_history_flush"),
        patch("plugins.xiaoqing_chat.handlers._freq_record"),
        patch("plugins.xiaoqing_chat.handlers._log_step") as mock_log_step,
    ):
        result = await _maybe_reply_smalltalk("你好", sample_group_event, mock_context)

    assert result == []
    mock_record_bot_reply.assert_not_awaited()
    state.heartflow.on_no_reply_async.assert_not_awaited()
    state.action_history.append.assert_not_called()
    state.pfc_state_store.set_state.assert_not_called()
    mock_schedule_pfc_state_flush.assert_not_called()
    assert any(
        call.kwargs.get("step") == "smalltalk.stale.drop"
        for call in mock_log_step.call_args_list
    )


@pytest.mark.asyncio
async def test_smalltalk_no_reply_current_turn_schedules_pfc_state_flush(
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
            new=AsyncMock(
                return_value=SimpleNamespace(reply="", action="wait", reason="later", ended=False)
            ),
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
        patch("plugins.xiaoqing_chat.handlers._log_step") as mock_log_step,
    ):
        result = await _maybe_reply_smalltalk("你好", sample_group_event, mock_context)

    assert result == []
    state.pfc_state_store.set_state.assert_called_once()
    mock_schedule_pfc_state_flush.assert_called_once()
    state.heartflow.on_no_reply_async.assert_awaited_once()
    no_reply_calls = [
        call for call in mock_log_step.call_args_list
        if call.kwargs.get("step") == "smalltalk.no_reply"
    ]
    assert no_reply_calls
    assert no_reply_calls[-1].kwargs["fields"]["reason"] == "pfc_no_reply"


@pytest.mark.asyncio
async def test_smalltalk_schedules_pfc_flush_even_if_post_commit_reply_record_fails(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.handlers import _maybe_reply_smalltalk

    lock = asyncio.Lock()
    state = MagicMock()
    state.get_lock.return_value = lock
    state.get_mood_state.return_value = ""
    state.memory_store.get.return_value = []
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.memory_store.get_recent.return_value = []
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
            new=AsyncMock(
                return_value=SimpleNamespace(
                    reply="planner-ok", action="reply", reason="ok", ended=False
                )
            ),
        ),
        patch(
            "plugins.xiaoqing_chat.handlers._record_bot_reply",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ),
        patch(
            "plugins.xiaoqing_chat.handlers._schedule_pfc_state_flush",
            side_effect=fake_schedule_pfc_state_flush,
        ) as mock_schedule_pfc_state_flush,
        patch("plugins.xiaoqing_chat.handlers._most_recent_user_local_id", return_value="u1"),
        patch("plugins.xiaoqing_chat.handlers._spawn_post_reply_bg_tasks", new=AsyncMock()),
        patch("plugins.xiaoqing_chat.handlers._schedule_memory_persist"),
        patch("plugins.xiaoqing_chat.handlers._schedule_action_history_flush"),
        patch("plugins.xiaoqing_chat.handlers._freq_record"),
        patch("plugins.xiaoqing_chat.handlers._log_step"),
    ):
        with pytest.raises(RuntimeError, match="boom"):
            await _maybe_reply_smalltalk("你好", sample_group_event, mock_context)

    state.pfc_state_store.set_state.assert_called_once()
    mock_schedule_pfc_state_flush.assert_called_once()


@pytest.mark.asyncio
async def test_smalltalk_schedules_pfc_flush_even_if_post_commit_no_reply_hook_fails(
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
    state.heartflow.on_no_reply_async = AsyncMock(side_effect=RuntimeError("no-reply boom"))
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
            new=AsyncMock(
                return_value=SimpleNamespace(reply="", action="wait", reason="later", ended=False)
            ),
        ),
        patch(
            "plugins.xiaoqing_chat.handlers._schedule_pfc_state_flush",
            side_effect=fake_schedule_pfc_state_flush,
        ) as mock_schedule_pfc_state_flush,
        patch("plugins.xiaoqing_chat.handlers._most_recent_user_local_id", return_value="u1"),
        patch("plugins.xiaoqing_chat.handlers._spawn_post_reply_bg_tasks", new=AsyncMock()),
        patch("plugins.xiaoqing_chat.handlers._schedule_memory_persist"),
        patch("plugins.xiaoqing_chat.handlers._schedule_action_history_flush"),
        patch("plugins.xiaoqing_chat.handlers._freq_record"),
        patch("plugins.xiaoqing_chat.handlers._log_step"),
    ):
        with pytest.raises(RuntimeError, match="no-reply boom"):
            await _maybe_reply_smalltalk("你好", sample_group_event, mock_context)

    state.pfc_state_store.set_state.assert_called_once()
    mock_schedule_pfc_state_flush.assert_called_once()
    state.action_history.append.assert_not_called()


@pytest.mark.asyncio
async def test_mode_indicator_only_applies_in_brain_chat(mock_context, sample_group_event):
    from plugins.xiaoqing_chat.handlers import _maybe_reply_smalltalk

    state = MagicMock()
    state.get_mood_state.return_value = ""
    state.memory_store.get.return_value = []
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.memory_store.get_recent_async = AsyncMock(return_value=[])
    state.memory_store.append = Mock()
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
            brain_chat=SimpleNamespace(
                enable_private_brain_chat=False,
                show_mode_indicator=True,
                brain_mode_indicator="[brain]",
            ),
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
    ):
        result = await _maybe_reply_smalltalk("你好", event, mock_context)

    assert result == [{"type": "text", "data": {"text": "ok"}}]
    mock_context.send_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_mode_indicator_is_emitted_when_brain_chat_is_active(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.handlers import _maybe_reply_smalltalk

    state = MagicMock()
    state.get_mood_state.return_value = ""
    state.memory_store.get.return_value = []
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.memory_store.get_recent_async = AsyncMock(return_value=[])
    state.memory_store.append = Mock()
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
            brain_chat=SimpleNamespace(
                enable_private_brain_chat=True,
                show_mode_indicator=True,
                brain_mode_indicator="[brain]",
            ),
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
            "plugins.xiaoqing_chat.handlers._ensure_user_message_recorded",
            new=AsyncMock(return_value="u1"),
        ),
        patch("plugins.xiaoqing_chat.handlers.is_brain_chat_active", return_value=True),
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
    ):
        result = await _maybe_reply_smalltalk("你好", event, mock_context)

    assert result == [{"type": "text", "data": {"text": "[brain]\nok"}}]
    mock_context.send_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_provider_denies_switch_for_non_admin(mock_context, sample_group_event):
    from plugins.xiaoqing_chat.handlers import handle_provider
    from plugins.xiaoqing_chat.runtime_state import ChatRuntimeState

    state = ChatRuntimeState()
    mock_context.secrets = _provider_test_secrets()
    _set_context_principal(mock_context, sample_group_event, group_role="member")

    with patch("plugins.xiaoqing_chat.handlers._state", return_value=state):
        result = await handle_provider("glm", sample_group_event, mock_context)

    assert state.get_chat_provider("g67890") is None
    assert "管理员" in result[0]["data"]["text"]


@pytest.mark.asyncio
async def test_handle_provider_list_mode_remains_public(mock_context, sample_group_event):
    from plugins.xiaoqing_chat.handlers import handle_provider
    from plugins.xiaoqing_chat.runtime_state import ChatRuntimeState

    state = ChatRuntimeState()
    mock_context.secrets = _provider_test_secrets()
    _set_context_principal(mock_context, sample_group_event, group_role="member")

    with patch("plugins.xiaoqing_chat.handlers._state", return_value=state):
        result = await handle_provider("", sample_group_event, mock_context)

    assert state.get_chat_provider("g67890") is None
    assert "LLM 供应商" in result[0]["data"]["text"]
    assert "当前会话覆盖" in result[0]["data"]["text"]


@pytest.mark.asyncio
async def test_handle_provider_group_admin_switch_is_scoped_to_current_group(
    mock_context,
    sample_group_event,
):
    from plugins.xiaoqing_chat.handlers import handle_provider
    from plugins.xiaoqing_chat.helper_utils import _get_llm_secrets
    from plugins.xiaoqing_chat.runtime_state import ChatRuntimeState

    state = ChatRuntimeState()
    mock_context.secrets = _provider_test_secrets()
    _set_context_principal(mock_context, sample_group_event, group_role="admin")

    with (
        patch("plugins.xiaoqing_chat.handlers._state", return_value=state),
        patch("plugins.xiaoqing_chat.helper_utils._state", return_value=state),
    ):
        result = await handle_provider("glm", sample_group_event, mock_context)
        group_a = _get_llm_secrets(mock_context, chat_id="g67890")
        group_b = _get_llm_secrets(mock_context, chat_id="g99999")

    assert state.get_chat_provider("g67890") == "glm"
    assert state.global_active_provider is None
    assert group_a["_provider_name"] == "glm"
    assert group_b["_provider_name"] == "deepseek"
    assert "已将当前会话供应商切换到" in result[0]["data"]["text"]


@pytest.mark.asyncio
async def test_handle_provider_global_scope_requires_bot_admin(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.handlers import handle_provider
    from plugins.xiaoqing_chat.helper_utils import _get_llm_secrets
    from plugins.xiaoqing_chat.runtime_state import ChatRuntimeState

    state = ChatRuntimeState()
    mock_context.secrets = _provider_test_secrets()
    _set_context_principal(mock_context, sample_group_event, group_role="owner")

    with (
        patch("plugins.xiaoqing_chat.handlers._state", return_value=state),
        patch("plugins.xiaoqing_chat.helper_utils._state", return_value=state),
    ):
        denied = await handle_provider("global glm", sample_group_event, mock_context)
        _set_context_principal(
            mock_context,
            sample_group_event,
            group_role="member",
            is_bot_admin=True,
        )
        allowed = await handle_provider("global glm", sample_group_event, mock_context)
        other_group = _get_llm_secrets(mock_context, chat_id="g99999")
        await handle_provider("deepseek", sample_group_event, mock_context)
        local = _get_llm_secrets(mock_context, chat_id="g67890")
        await handle_provider("default", sample_group_event, mock_context)
        inherited = _get_llm_secrets(mock_context, chat_id="g67890")
        await handle_provider("global default", sample_group_event, mock_context)
        reset_global = _get_llm_secrets(mock_context, chat_id="g99999")

    assert "Bot 全局管理员" in denied[0]["data"]["text"]
    assert "全局运行时供应商" in allowed[0]["data"]["text"]
    assert other_group["_provider_name"] == "glm"
    assert local["_provider_name"] == "deepseek"
    assert inherited["_provider_name"] == "glm"
    assert reset_global["_provider_name"] == "deepseek"
    assert state.global_active_provider is None


@pytest.mark.asyncio
async def test_handle_provider_private_chat_requires_bot_admin(
    mock_context,
    sample_private_event,
):
    from plugins.xiaoqing_chat.handlers import handle_provider
    from plugins.xiaoqing_chat.runtime_state import ChatRuntimeState

    state = ChatRuntimeState()
    mock_context.secrets = _provider_test_secrets()
    _set_context_principal(mock_context, sample_private_event)

    with patch("plugins.xiaoqing_chat.handlers._state", return_value=state):
        denied = await handle_provider("glm", sample_private_event, mock_context)
        _set_context_principal(mock_context, sample_private_event, is_bot_admin=True)
        allowed = await handle_provider("glm", sample_private_event, mock_context)

    assert "管理员" in denied[0]["data"]["text"]
    assert "已将当前会话供应商切换到" in allowed[0]["data"]["text"]
    assert state.get_chat_provider(f"u{sample_private_event['user_id']}") == "glm"


@pytest.mark.asyncio
async def test_handle_provider_does_not_trust_raw_sender_role(
    mock_context,
    sample_group_event,
):
    from plugins.xiaoqing_chat.handlers import handle_provider
    from plugins.xiaoqing_chat.runtime_state import ChatRuntimeState

    state = ChatRuntimeState()
    event = dict(sample_group_event)
    event["sender"] = dict(sample_group_event["sender"], role="owner")
    mock_context.secrets = _provider_test_secrets()
    _set_context_principal(mock_context, event, group_role="unknown")

    with patch("plugins.xiaoqing_chat.handlers._state", return_value=state):
        result = await handle_provider("glm", event, mock_context)

    assert "管理员" in result[0]["data"]["text"]
    assert state.get_chat_provider("g67890") is None


@pytest.mark.asyncio
async def test_handle_provider_concurrent_groups_do_not_overwrite_each_other():
    from plugins.xiaoqing_chat.handlers import handle_provider
    from plugins.xiaoqing_chat.runtime_state import ChatRuntimeState

    state = ChatRuntimeState()
    secrets = _provider_test_secrets()
    event_a = {"user_id": 1, "group_id": 10}
    event_b = {"user_id": 2, "group_id": 20}
    context_a = SimpleNamespace(
        secrets=secrets,
        principal=PluginPrincipal(kind="user", user_id=1, group_id=10, group_role="admin"),
        capabilities=PluginCapabilities(),
        logger=MagicMock(),
        request_id="provider-a",
    )
    context_b = SimpleNamespace(
        secrets=secrets,
        principal=PluginPrincipal(kind="user", user_id=2, group_id=20, group_role="owner"),
        capabilities=PluginCapabilities(),
        logger=MagicMock(),
        request_id="provider-b",
    )

    with patch("plugins.xiaoqing_chat.handlers._state", return_value=state):
        await asyncio.gather(
            handle_provider("glm", event_a, context_a),
            handle_provider("deepseek", event_b, context_b),
        )

    assert state.provider_overrides() == {"g10": "glm", "g20": "deepseek"}


def test_provider_resolution_prunes_removed_provider_overrides() -> None:
    from plugins.xiaoqing_chat.runtime_state import ChatRuntimeState

    state = ChatRuntimeState()
    state.set_chat_provider("g1", "removed")
    state.set_chat_provider("g2", "still-present")
    state.set_global_provider("removed")

    resolved = state.resolve_provider_name(
        "g1",
        ["default-provider", "still-present"],
        "default-provider",
    )

    assert resolved == "default-provider"
    assert state.global_active_provider is None
    assert state.provider_overrides() == {"g2": "still-present"}


@pytest.mark.asyncio
async def test_ensure_user_message_recorded_uses_passed_bound_state(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.handlers import _ensure_user_message_recorded

    state = MagicMock()
    state.review_store.cleanup_expired = Mock()
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.memory_store.append = Mock()
    state.heartflow.on_user_message_async = AsyncMock()
    state.set_last_observe_ts = Mock()

    runtime = MagicMock()

    with (
        patch(
            "plugins.xiaoqing_chat.handlers._get_bound_state",
            side_effect=AssertionError("bound state should not be reloaded"),
        ),
        patch("plugins.xiaoqing_chat.handlers._next_local_id", return_value="m1"),
        patch("plugins.xiaoqing_chat.handlers._schedule_memory_persist"),
        patch("plugins.xiaoqing_chat.handlers._log_step"),
    ):
        local_id = await _ensure_user_message_recorded(
            "你好",
            sample_group_event,
            mock_context,
            runtime,
            state=state,
        )

    assert local_id == "m1"
    called_chat_id, called_ts = state.set_last_observe_ts.call_args.args
    assert called_chat_id == "g67890"
    assert isinstance(called_ts, float)


def test_next_local_id_atomic():
    """fetch_and_increment_local_id should be atomic read-and-bump."""
    from plugins.xiaoqing_chat.runtime_state import ChatRuntimeState

    state = ChatRuntimeState()
    result = state.fetch_and_increment_local_id("test_chat")
    assert result == 1
    result2 = state.fetch_and_increment_local_id("test_chat")
    assert result2 == 2


def test_chat_id_requires_group_or_user_identifier():
    from plugins.xiaoqing_chat.helper_utils import _chat_id

    with pytest.raises(ValueError, match="missing chat identifier"):
        _chat_id({"message_id": 1})


@pytest.mark.asyncio
async def test_smalltalk_new_user_turn_clears_sticky_pfc_ended_before_planner_runs(
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
            ended=True,
            last_successful_reply_action="say_goodbye",
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

    captured: dict[str, Any] = {}

    async def fake_run_pfc_once(**kwargs):
        captured["state_override"] = kwargs["state_override"]
        return SimpleNamespace(reply="", action="wait", reason="再观察", ended=False)

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
        patch("plugins.xiaoqing_chat.handlers._most_recent_user_local_id", return_value="u1"),
        patch("plugins.xiaoqing_chat.handlers._spawn_post_reply_bg_tasks", new=AsyncMock()),
        patch("plugins.xiaoqing_chat.handlers._schedule_memory_persist"),
        patch("plugins.xiaoqing_chat.handlers._schedule_action_history_flush"),
        patch("plugins.xiaoqing_chat.handlers._schedule_pfc_state_flush"),
        patch("plugins.xiaoqing_chat.handlers._freq_record"),
        patch("plugins.xiaoqing_chat.handlers._log_step"),
    ):
        result = await _maybe_reply_smalltalk("新消息", sample_group_event, mock_context)

    assert result == []
    assert captured["state_override"].ended is False


@pytest.mark.asyncio
async def test_smalltalk_direct_reply_uses_dynamic_history_think_level_when_planner_disabled(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.handlers import _maybe_reply_smalltalk

    lock = asyncio.Lock()
    state = MagicMock()
    state.get_mood_state.return_value = ""
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.memory_store.get_recent_async = AsyncMock(return_value=[object(), object(), object()])
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

    seen: dict[str, Any] = {}

    async def fake_build_memory_block(**_kwargs):
        return "prefetched-memory"

    async def fake_generate_reply_draft(**kwargs):
        seen["action"] = kwargs["action"]
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
                enable_private_brain_chat=False,
                private_planner_always_on=True,
                brain_max_context_size=10,
            ),
            max_context_size=10,
            planner=SimpleNamespace(
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

    lock = asyncio.Lock()
    state = MagicMock()
    state.get_mood_state.return_value = ""
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.memory_store.get_recent_async = AsyncMock(return_value=[object()])
    state.memory_store.append = Mock()
    state.heartflow.on_user_message_async = AsyncMock()
    state.heartflow.on_bot_reply_async = AsyncMock()
    state.heartflow.on_no_reply_async = AsyncMock()
    state.inc_stats = Mock()
    state.action_history.append = Mock()
    state.pfc_state_store.get_async = AsyncMock(
        return_value=SimpleNamespace(
            chat_id="u12345",
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
            brain_chat=SimpleNamespace(
                enable_private_brain_chat=True,
                private_planner_always_on=True,
                brain_max_context_size=6,
                brain_think_level=2,
            ),
            max_context_size=6,
            planner=SimpleNamespace(
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
    state.pfc_state_store.get_async = AsyncMock()
    state.pfc_state_store.set_state = Mock()

    runtime = SimpleNamespace(
        cfg=SimpleNamespace(
            enable_smalltalk=True,
            goal=SimpleNamespace(enable_goal=False),
            reflection=SimpleNamespace(
                enable_expression_reflection=False, enable_review_sessions=False
            ),
            brain_chat=SimpleNamespace(enable_private_brain_chat=False),
            planner=SimpleNamespace(enable_planner=False, resolve_think_level=lambda history_len=0: 0),
            personality=SimpleNamespace(states=[], state_probability=0.0),
            debug=SimpleNamespace(log_latency=False),
        )
    )
    event = dict(sample_group_event)
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

    state = MagicMock()
    state.review_store.cleanup_expired = Mock()
    state.pfc_state_store.get_async = AsyncMock(
        return_value=SimpleNamespace(goal_list=[], planner_skip_until=0.0)
    )
    state.get_mood_state.return_value = ""

    runtime = SimpleNamespace(
        cfg=SimpleNamespace(
            goal=SimpleNamespace(enable_goal=False),
            reflection=SimpleNamespace(
                enable_expression_reflection=False,
                enable_review_sessions=False,
            ),
            brain_chat=SimpleNamespace(enable_private_brain_chat=False),
            personality=SimpleNamespace(states=[], state_probability=0.0),
        )
    )
    hctx = _make_hctx(runtime=runtime, state=state, context=mock_context)

    with (
        patch("plugins.xiaoqing_chat.handlers.build_effective_user_text", new=AsyncMock(return_value="你好")),
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

    event = dict(sample_group_event)
    event["message_id"] = 103
    state = MagicMock()
    state.memory_store.get_recent_async = AsyncMock(
        return_value=[
            StoredMessage(
                role="user",
                name="群友",
                ts=time.time() - 5,
                message_id=101,
                content="小青你在吗",
            ),
            StoredMessage(
                role="assistant",
                name="小青",
                ts=time.time() - 4,
                message_id=102,
                content="在呢",
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
                enable_expression_reflection=False,
                enable_review_sessions=False,
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
        patch("plugins.xiaoqing_chat.handlers._should_reply", new=AsyncMock(return_value=False)) as gate,
        patch("plugins.xiaoqing_chat.handlers.is_brain_chat_active", return_value=False),
        patch("plugins.xiaoqing_chat.handlers._log_step"),
    ):
        prepared = await _prepare_smalltalk_turn(
            "不@她能不能听见啊", event, mock_context, hctx
        )

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

    event = dict(sample_group_event)
    event["message_id"] = 103
    state = MagicMock()
    state.memory_store.get_recent_async = AsyncMock(
        return_value=[
            StoredMessage(
                role="user",
                name="群友",
                ts=time.time() - 5,
                message_id=101,
                content="她刚才还在说外卖",
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
                enable_expression_reflection=False,
                enable_review_sessions=False,
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
        patch("plugins.xiaoqing_chat.handlers._should_reply", new=AsyncMock(return_value=False)) as gate,
        patch("plugins.xiaoqing_chat.handlers.is_brain_chat_active", return_value=False),
        patch("plugins.xiaoqing_chat.handlers._log_step"),
    ):
        prepared = await _prepare_smalltalk_turn(
            "不@她能不能听见啊", event, mock_context, hctx
        )

    assert prepared is None
    gate.assert_awaited_once()


@pytest.mark.asyncio
async def test_smalltalk_commit_syncs_goal_store_to_top_planner_goal_after_state_override_update(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.handlers import _maybe_reply_smalltalk

    lock = asyncio.Lock()
    state = MagicMock()
    state.get_mood_state.return_value = ""
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.memory_store.get_recent_async = AsyncMock(return_value=[])
    state.memory_store.append = Mock()
    state.goal_store.set_async = AsyncMock()
    state.pfc_state_store.get_async = AsyncMock(
        return_value=SimpleNamespace(
            chat_id="g67890",
            ignore_until_ts=0.0,
            ended=False,
            last_successful_reply_action="",
            goal_list=[{"goal": "旧规划目标"}],
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

    chat_id = "g67890"
    lock = asyncio.Lock()
    goal_store = GoalStore()
    await goal_store.set_async(chat_id, goal="围绕旧目标继续", source="user")

    state = MagicMock()
    state.get_mood_state.return_value = ""
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.memory_store.get_recent_async = AsyncMock(return_value=[])
    state.memory_store.append = Mock()
    state.goal_store = goal_store
    state.pfc_state_store.get_async = AsyncMock(
        return_value=SimpleNamespace(
            chat_id=chat_id,
            ignore_until_ts=0.0,
            ended=False,
            last_successful_reply_action="",
            goal_list=[{"goal": "旧规划目标"}],
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

    assert result == [{"type": "text", "data": {"text": "planner-ok"}}]
    assert state.goal_store.get(chat_id).goal == ""


@pytest.mark.asyncio
async def test_smalltalk_pre_gate_clears_goal_store_when_no_goal_is_derived(
    mock_context, sample_group_event
):
    from plugins.xiaoqing_chat.handlers import _maybe_reply_smalltalk
    from plugins.xiaoqing_chat.planning.goal_state import GoalStore

    chat_id = "g67890"
    lock = asyncio.Lock()
    goal_store = GoalStore()
    await goal_store.set_async(chat_id, goal="围绕旧目标继续", source="user")

    state = MagicMock()
    state.get_mood_state.return_value = ""
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.memory_store.get_recent_async = AsyncMock(return_value=[])
    state.memory_store.append = Mock()
    state.goal_store = goal_store
    state.pfc_state_store.get_async = AsyncMock(
        return_value=SimpleNamespace(
            chat_id=chat_id,
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
    context = SimpleNamespace(logger=MagicMock())

    @handle_errors("测试")
    async def failing_handler(*, context=None):
        raise RuntimeError("boom")

    result = await failing_handler(context=context)

    context.logger.exception.assert_called_once()
    assert result[0]["data"]["text"].startswith("❌ 测试暂时不可用，请稍后重试（请求ID: ")


@pytest.mark.asyncio
async def test_handle_internal_reset_clears_goal_heartflow_and_action_history(
    mock_context, sample_group_event, tmp_path
):
    from plugins.xiaoqing_chat.handlers import handle_internal
    from plugins.xiaoqing_chat.planning.action_history import ActionRecord
    from plugins.xiaoqing_chat.runtime_state import ChatRuntimeState
    from plugins.xiaoqing_chat.store_binding import _bind_all_stores

    chat_id = "g67890"
    runtime = MagicMock()
    state = ChatRuntimeState()
    _bind_all_stores(state, tmp_path)

    state.memory_store.append(chat_id, role="user", name="Tester", content="hi")
    await state.goal_store.set_async(chat_id, goal="围绕旧目标继续", source="user")
    await state.heartflow.on_user_message_async(chat_id=chat_id)
    await state.heartflow.on_bot_reply_async(chat_id=chat_id)
    state.action_history.append(
        chat_id,
        ActionRecord(
            ts=time.time(),
            local_target="u1",
            action="reply",
            reasoning="old",
            detail={"source": "pfc"},
            executed=True,
        ),
    )
    state.set_continuous_reply_count(chat_id, 3)
    state.set_continuous_cooldown_until(chat_id, 600.0)

    pfc_st = await state.pfc_state_store.get_async(chat_id)
    pfc_st.ended = True
    pfc_st.ignore_until_ts = 123.0
    pfc_st.last_successful_reply_action = "say_goodbye"
    pfc_st.goal_list = [{"goal": "old"}]
    pfc_st.knowledge_list = [{"text": "old"}]
    await state.pfc_state_store.save_async(chat_id)

    _set_context_principal(mock_context, sample_group_event, group_role="admin")
    hctx = _make_hctx(runtime=runtime, state=state, context=mock_context, data_dir=tmp_path)
    with patch("plugins.xiaoqing_chat.handlers.HandlerContext.from_event", return_value=hctx):
        result = await handle_internal("重置", "确认", sample_group_event, mock_context)

    assert result == [{"type": "text", "data": {"text": "✅ 已重置会话记忆"}}]
    assert state.memory_store.get(chat_id) == []
    assert state.goal_store.get(chat_id).goal == ""
    heartflow_state = await state.heartflow.get_async(chat_id)
    assert heartflow_state.reply_streak == 0
    assert heartflow_state.no_reply_streak == 0
    assert state.get_continuous_reply_count(chat_id) == 0
    assert state.get_continuous_cooldown_until(chat_id) == 0.0
    assert state.action_history.get_recent(chat_id, max_items=20) == []


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
    state = ChatRuntimeState()
    _bind_all_stores(state, tmp_path)

    state.review_store.save_policy(
        chat_id,
        ReviewPolicy(
            goal_override="旧目标覆写",
            strategy_note="旧策略备注",
            avoid_patterns=["避免句式A", "避免句式B"],
        ),
    )
    session = state.review_store.open_session_if_allowed(
        kind="goal_strategy",
        chat_id=chat_id,
        payload={"goal": "旧目标", "stats": "旧统计"},
        timeout_seconds=600.0,
        cooldown_seconds=0.0,
    )
    assert session is not None

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


@pytest.mark.parametrize("planner_action", ["send_new_message", "say_goodbye"])
@pytest.mark.asyncio
async def test_smalltalk_action_history_preserves_real_planner_action_name(
    mock_context, sample_group_event, planner_action
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

    chat_id = "pfc-goal-replan"
    cfg = XiaoQingChatConfig()
    context = MagicMock()
    context.data_dir = tmp_path
    context.http_session = AsyncMock()

    memory_store = MemoryStore()
    memory_store.append(chat_id, role="user", name="Tester", content="今天去哪家火锅店")
    action_history = ActionHistoryStore()
    memory_db = MagicMock()
    pfc_state_store = PFCStateStore()

    generate_reply = AsyncMock(return_value="ok")

    with (
        patch(
            "plugins.xiaoqing_chat.planning.pfc_engine.plan_next_action",
            new=AsyncMock(
                side_effect=[
                    PFCPlan(
                        action="rethink_goal",
                        reason="当前目标过时，需要重设",
                        thinking="先确认预算和口味偏好",
                        wait_seconds=0,
                    ),
                    PFCPlan(
                        action="direct_reply",
                        reason="继续回复",
                        thinking="",
                        wait_seconds=0,
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
            context=context,
            runtime_cfg=cfg,
            secrets={"api_base": "http://test", "api_key": "key", "model": "test-model"},
            bot_name="小青",
            is_private=False,
            chat_id=chat_id,
            current_text="今天去哪家火锅店",
            memory_store=memory_store,
            action_history=action_history,
            memory_db=memory_db,
            pfc_state_store=pfc_state_store,
            generate_reply=generate_reply,
        )

    assert result.reply == "ok"
    assert generate_reply.await_count == 1
    assert generate_reply.await_args is not None
    _, planner_reason, extra_reason = generate_reply.await_args.args
    merged_reason = (f"{planner_reason}\n{extra_reason}").strip()
    assert "帮用户选一家合适的火锅店" in merged_reason
    assert "预算与口味偏好" in merged_reason


@pytest.mark.asyncio
async def test_generate_reply_prefers_planner_goal_over_review_override_goal_store(mock_context):
    from contextlib import ExitStack

    from plugins.xiaoqing_chat.llm.prompt_builder import ChatMessage
    from plugins.xiaoqing_chat.llm.reply_checker import ReplyCheckResult
    from plugins.xiaoqing_chat.planning.planner import PlannedAction
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

    state = MagicMock()
    state.memory_store.get_recent_async = AsyncMock(return_value=[])
    state.goal_store.get_async = AsyncMock(return_value=SimpleNamespace(goal="复盘会话先问感受"))
    state.review_store.bind = Mock()
    state.inc_stats = Mock()

    fg = SimpleNamespace(
        proxy="",
        endpoint_path="/v1/chat/completions",
        timeout_seconds=3.0,
        max_retry=0,
        retry_interval_seconds=0.2,
        to_dict=lambda: {
            "timeout_seconds": 3.0,
            "max_retry": 0,
            "retry_interval_seconds": 0.2,
            "proxy": "",
            "endpoint_path": "/v1/chat/completions",
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
                "plugins.xiaoqing_chat.reply_generator._get_llm_secrets",
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
    from plugins.xiaoqing_chat.planning.planner import PlannedAction
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
        proxy="",
        endpoint_path="/v1/chat/completions",
        timeout_seconds=3.0,
        max_retry=0,
        retry_interval_seconds=0.2,
        to_dict=lambda: {
            "timeout_seconds": 3.0,
            "max_retry": 0,
            "retry_interval_seconds": 0.2,
            "proxy": "",
            "endpoint_path": "/v1/chat/completions",
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
                "plugins.xiaoqing_chat.reply_generator._get_llm_secrets",
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
    from plugins.xiaoqing_chat.planning.planner import PlannedAction
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

    state = MagicMock()
    state.memory_store.get_recent_async = AsyncMock(return_value=[])
    state.goal_store.get_async = AsyncMock(return_value=SimpleNamespace(goal="旧目标"))
    state.review_store.bind = Mock()
    state.inc_stats = Mock()

    fg = SimpleNamespace(
        proxy="",
        endpoint_path="/v1/chat/completions",
        timeout_seconds=3.0,
        max_retry=0,
        retry_interval_seconds=0.2,
        to_dict=lambda: {
            "timeout_seconds": 3.0,
            "max_retry": 0,
            "retry_interval_seconds": 0.2,
            "proxy": "",
            "endpoint_path": "/v1/chat/completions",
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
                "plugins.xiaoqing_chat.reply_generator._get_llm_secrets",
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
    from plugins.xiaoqing_chat.planning.planner import PlannedAction
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
        proxy="",
        endpoint_path="/v1/chat/completions",
        timeout_seconds=3.0,
        max_retry=0,
        retry_interval_seconds=0.2,
        to_dict=lambda: {
            "timeout_seconds": 3.0,
            "max_retry": 0,
            "retry_interval_seconds": 0.2,
            "proxy": "",
            "endpoint_path": "/v1/chat/completions",
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
                "plugins.xiaoqing_chat.reply_generator._get_llm_secrets",
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
async def test_generate_reply_forced_rejection_uses_safe_fallback(mock_context):
    from contextlib import ExitStack

    from plugins.xiaoqing_chat.llm.reply_checker import ReplyCheckResult
    from plugins.xiaoqing_chat.planning.planner import PlannedAction
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
    state = MagicMock()
    state.memory_store.get_recent_async = AsyncMock(return_value=[])
    state.goal_store.get_async = AsyncMock(return_value=SimpleNamespace(goal=""))
    state.review_store.bind = Mock()
    state.inc_stats = Mock()

    fg = SimpleNamespace(
        proxy="",
        endpoint_path="/v1/chat/completions",
        timeout_seconds=3.0,
        max_retry=0,
        retry_interval_seconds=0.2,
        to_dict=lambda: {
            "timeout_seconds": 3.0,
            "max_retry": 0,
            "retry_interval_seconds": 0.2,
            "proxy": "",
            "endpoint_path": "/v1/chat/completions",
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
                "plugins.xiaoqing_chat.reply_generator._get_llm_secrets",
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
            patch("plugins.xiaoqing_chat.reply_generator._build_jargon_explanation", return_value="")
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
                new=AsyncMock(return_value=("不合适的回复", "primary")),
            )
        )
        stack.enter_context(
            patch(
                "plugins.xiaoqing_chat.reply_generator.process_llm_response",
                return_value=["不合适的回复"],
            )
        )
        stack.enter_context(
            patch("plugins.xiaoqing_chat.reply_generator.join_reply", return_value="不合适的回复")
        )
        stack.enter_context(
            patch(
                "plugins.xiaoqing_chat.reply_generator._heuristic_check",
                return_value=ReplyCheckResult(
                    suitable=False,
                    reason="重复且不自然",
                    need_replan=False,
                ),
            )
        )

        draft = await _generate_reply_draft(
            text="你好",
            event={"group_id": 1, "user_id": 2},
            context=mock_context,
            runtime=runtime,
            state=state,
            forced=True,
            action=action,
            plan_reasoning="用户要求直接回复",
        )

    assert draft is not None
    assert draft.text == "嗯，我先换个说法。"
    logged_payloads = [str(call.args[1]) for call in mock_context.logger.info.call_args_list]
    assert any('"step": "reply.checker.skip"' in payload for payload in logged_payloads)


@pytest.mark.asyncio
async def test_generate_reply_checker_timeout_allows_non_forced_reply(mock_context):
    from contextlib import ExitStack

    from plugins.xiaoqing_chat.llm.prompt_builder import ChatMessage
    from plugins.xiaoqing_chat.planning.planner import PlannedAction
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
            debug=SimpleNamespace(show_reply_prompt=False, log_steps=False),
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
    state = MagicMock()
    state.memory_store.get_recent_async = AsyncMock(return_value=[])
    state.goal_store.get_async = AsyncMock(return_value=SimpleNamespace(goal=""))
    state.review_store.bind = Mock()
    state.inc_stats = Mock()

    fg = SimpleNamespace(
        proxy="",
        endpoint_path="/v1/chat/completions",
        timeout_seconds=3.0,
        max_retry=0,
        retry_interval_seconds=0.2,
        to_dict=lambda: {
            "timeout_seconds": 3.0,
            "max_retry": 0,
            "retry_interval_seconds": 0.2,
            "proxy": "",
            "endpoint_path": "/v1/chat/completions",
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
                "plugins.xiaoqing_chat.reply_generator._get_llm_secrets",
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
            patch("plugins.xiaoqing_chat.reply_generator._build_jargon_explanation", return_value="")
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
                new=AsyncMock(return_value=({"content": "这条回复需要检查"}, "")),
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

        with pytest.raises(ReplyRejected):
            await _generate_reply_draft(
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


@pytest.mark.asyncio
async def test_generate_reply_checker_unexpected_error_logs_error_not_timeout(mock_context):
    from contextlib import ExitStack

    from plugins.xiaoqing_chat.config.config import ResponsePostProcessConfig
    from plugins.xiaoqing_chat.llm.prompt_builder import ChatMessage
    from plugins.xiaoqing_chat.planning.planner import PlannedAction
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

    state = MagicMock()
    state.memory_store.get_recent_async = AsyncMock(return_value=[])
    state.goal_store.get_async = AsyncMock(return_value=SimpleNamespace(goal=""))
    state.review_store.bind = Mock()
    state.inc_stats = Mock()

    fg = SimpleNamespace(
        proxy="",
        endpoint_path="/v1/chat/completions",
        timeout_seconds=3.0,
        max_retry=0,
        retry_interval_seconds=0.2,
        to_dict=lambda: {
            "timeout_seconds": 3.0,
            "max_retry": 0,
            "retry_interval_seconds": 0.2,
            "proxy": "",
            "endpoint_path": "/v1/chat/completions",
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
                "plugins.xiaoqing_chat.reply_generator._get_llm_secrets",
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
            patch("plugins.xiaoqing_chat.reply_generator._build_jargon_explanation", return_value="")
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
            patch("plugins.xiaoqing_chat.reply_generator.join_reply", return_value="这条回复需要检查")
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

        with pytest.raises(ReplyRejected):
            await _generate_reply_draft(
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
    assert any(call.kwargs.get("step") == "reply.check.error" for call in mock_log_step.call_args_list)
    assert not any(
        call.kwargs.get("step") == "reply.check.timeout" for call in mock_log_step.call_args_list
    )


@pytest.mark.asyncio
async def test_generate_reply_result_handles_missing_draft():
    from plugins.xiaoqing_chat.handlers import _generate_reply_result

    draft_mock = AsyncMock(return_value=None)

    with patch("plugins.xiaoqing_chat.handlers._generate_reply_draft", new=draft_mock):
        text, parts, media_marker = await _generate_reply_result(text="你好")

    assert text == ""
    assert parts == ()
    assert media_marker is None


def test_assistant_reply_parts_keeps_single_face_when_generator_already_attached_it():
    from plugins.xiaoqing_chat.smalltalk_media_helpers import _assistant_reply_parts
    from plugins.xiaoqing_chat.smalltalk_models import _GeneratedSmalltalkTurn

    generated = _GeneratedSmalltalkTurn(
        reply="懂了",
        reply_parts=(
            {"kind": "text", "text": "懂了"},
            {"kind": "qq_face", "face_id": "277", "marker": "[QQ表情：狗头]"},
        ),
        media_marker=SimpleNamespace(
            kind="qq_face",
            entry=SimpleNamespace(face_id="277", label="狗头", aliases=()),
            marker="[QQ表情：狗头]",
            mode="text_with_face",
        ),
    )

    parts = _assistant_reply_parts(SimpleNamespace(), generated)

    assert [part["kind"] for part in parts] == ["text", "qq_face"]
    assert str(parts[1].get("face_id", "")) == "277"


def test_assistant_reply_parts_keeps_single_emoji_when_generator_already_attached_it():
    from plugins.xiaoqing_chat.smalltalk_media_helpers import _assistant_reply_parts
    from plugins.xiaoqing_chat.smalltalk_models import _GeneratedSmalltalkTurn

    context = SimpleNamespace(plugin_dir=Path("/tmp"))
    generated = _GeneratedSmalltalkTurn(
        reply="懂了",
        reply_parts=(
            {"kind": "text", "text": "懂了"},
            {
                "kind": "emoji",
                "media_hash": "hash-1",
                "marker": "[表情包：无语]",
                "description": "猫猫翻白眼",
                "emotion_tags": ["无语"],
                "file_path": "/tmp/emoji.png",
            },
        ),
        media_marker=SimpleNamespace(
            kind="emoji",
            entry=SimpleNamespace(
                media_hash="hash-1",
                description="猫猫翻白眼",
                emotion_tags=("无语",),
                file_path="/tmp/emoji.png",
            ),
            marker="[表情包：无语]",
            mode="text_with_emoji",
        ),
    )

    parts = _assistant_reply_parts(context, generated)

    assert [part["kind"] for part in parts] == ["text", "emoji"]
    assert str(parts[1].get("media_hash", "")) == "hash-1"


def test_assistant_reply_parts_keeps_attached_image_without_reselecting():
    from plugins.xiaoqing_chat.smalltalk_media_helpers import _assistant_reply_parts
    from plugins.xiaoqing_chat.smalltalk_models import _GeneratedSmalltalkTurn

    generated = _GeneratedSmalltalkTurn(
        reply="懂了",
        reply_parts=(
            {"kind": "text", "text": "懂了"},
            {
                "kind": "image",
                "media_key": "media:hash-1",
                "media_hash": "hash-1",
                "marker": "[图片：海边落日]",
                "description": "海边落日",
                "file_path": "/tmp/sunset.png",
            },
        ),
        media_marker=SimpleNamespace(
            kind="image",
            entry=SimpleNamespace(
                media_key="media:hash-1",
                media_hash="hash-1",
                marker="[图片：海边落日]",
                description="海边落日",
                file_path="/tmp/sunset.png",
            ),
            marker="[图片：海边落日]",
            mode="text_with_image",
        ),
    )

    parts = _assistant_reply_parts(SimpleNamespace(), generated)

    assert [part["kind"] for part in parts] == ["text", "image"]
    assert str(parts[1].get("media_hash", "")) == "hash-1"


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
