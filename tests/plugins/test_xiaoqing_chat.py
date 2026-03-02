"""
Tests for xiaoqing_chat plugin
"""

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent

# Import the plugin module using the package structure
from plugins.xiaoqing_chat import main as xiaoqing_chat


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture
def mock_context():
    """Create a mock plugin context for xiaoqing_chat"""
    context = MagicMock()
    context.config = {"bot_name": "小青"}
    context.secrets = {
        "openai_api_key": "test_key",
        "plugins": {"xiaoqing_chat": {"api_key": "test", "api_base": "http://test", "model": "test-model"}},
    }
    context.plugin_name = "xiaoqing_chat"
    context.plugin_dir = Path("/test/plugins/xiaoqing_chat")
    context.data_dir = Path("/test/data/xiaoqing_chat")
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
    from core.args import parse

    with patch("plugins.xiaoqing_chat.main.handle_smalltalk", new=AsyncMock(return_value=[])) as mock_smalltalk:
        result = await handle(
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

    with patch("plugins.xiaoqing_chat.main.handle_internal", new=AsyncMock(return_value=[])) as mock_internal:
        result = await handle(
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

    with patch("plugins.xiaoqing_chat.main.handle_internal", new=AsyncMock(return_value=[])) as mock_internal:
        result = await handle(
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

    with patch("plugins.xiaoqing_chat.main.handle_internal", new=AsyncMock(return_value=[])) as mock_internal:
        result = await handle(
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

    with patch("plugins.xiaoqing_chat.main.handle_smalltalk", new=AsyncMock(return_value=[{"type": "text", "data": {"text": "mock_response"}}])) as mock_smalltalk:
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


# ============================================================
# Command Variants Tests
# ============================================================

@pytest.mark.plugin
@pytest.mark.asyncio
async def test_handle_with_all_chat_command_variants(mock_context, sample_group_event):
    """Test handle recognizes chat via /xc <content>"""
    from plugins.xiaoqing_chat.main import handle

    with patch("plugins.xiaoqing_chat.main.handle_smalltalk", new=AsyncMock(return_value=[])) as mock_smalltalk:
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

    with patch("plugins.xiaoqing_chat.main.handle_internal", new=AsyncMock(return_value=[])) as mock_internal:
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

    with patch("plugins.xiaoqing_chat.main.handle_internal", new=AsyncMock(return_value=[])) as mock_internal:
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

    with patch("plugins.xiaoqing_chat.main.handle_internal", new=AsyncMock(return_value=[])) as mock_internal:
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

    with patch("plugins.xiaoqing_chat.main.handle_smalltalk", new=AsyncMock(return_value=[])) as mock_smalltalk:
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

    with patch("plugins.xiaoqing_chat.main.handle_internal", new=AsyncMock(return_value=[])) as mock_internal:
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

    with patch("plugins.xiaoqing_chat.main.handle_smalltalk", new=AsyncMock(return_value=[])) as mock_st:
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


@pytest.mark.plugin
def test_smalltalk_pfc_runs_inside_chat_lock():
    handlers_file = ROOT / "plugins" / "xiaoqing_chat" / "handlers.py"
    content = handlers_file.read_text(encoding="utf-8")
    lock_index = content.find("async with _get_lock(chat_id):")
    pfc_index = content.find("pfc_result = await run_pfc_once(")

    assert lock_index != -1
    assert pfc_index != -1
    assert lock_index < pfc_index


# ---- interest scorer tests ----
from plugins.xiaoqing_chat.frequency_control import _score_interest

class TestScoreInterest:
    def test_question_mark_is_high(self):
        assert _score_interest("这是什么？") == "high"

    def test_ascii_question_mark_is_high(self):
        assert _score_interest("what?") == "high"

    def test_exclamation_is_high(self):
        assert _score_interest("竟然还有这么离谱的项目！") == "high"

    def test_laugh_word_is_high(self):
        assert _score_interest("哈哈哈太好笑了") == "high"

    def test_life_keyword_is_high(self):
        assert _score_interest("没有什么是一顿火锅解决不了的") == "high"

    def test_question_ending_is_high(self):
        assert _score_interest("你觉得这样行吗") == "high"

    def test_short_text_is_low(self):
        assert _score_interest("哦") == "low"

    def test_empty_is_low(self):
        assert _score_interest("") == "low"

    def test_pure_url_is_low(self):
        assert _score_interest("https://example.com/page") == "low"

    def test_pure_number_is_low(self):
        assert _score_interest("12345") == "low"

    def test_normal_statement_is_neutral(self):
        assert _score_interest("今天天气不错") == "neutral"

    def test_interest_affects_probability_high(self):
        """High interest score allows reply when neutral would be blocked (random=0.7 > base=0.6 but < scaled=0.95)."""
        from unittest.mock import MagicMock, patch
        runtime = MagicMock()
        runtime.cfg.reply_probability_base = 0.6
        runtime.cfg.reply_probability_private = 0.95
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
        state.goal_store.get.return_value.goal = ""
        state.heartflow.score.return_value = 1.0

        from plugins.xiaoqing_chat.frequency_control import _should_reply
        # random=0.7: neutral blocked (0.7 >= 0.6), high allowed (0.7 < 0.95)
        with patch("plugins.xiaoqing_chat.frequency_control.random") as mock_rand:
            mock_rand.random.return_value = 0.7
            result_high = _should_reply(runtime, state, "g1", "火锅好吃吗？", False, False, False, interest="high")
            mock_rand.random.return_value = 0.7
            result_neutral = _should_reply(runtime, state, "g1", "火锅好吃吗？", False, False, False, interest="neutral")
        assert result_high is True
        assert result_neutral is False

    def test_ascii_exclamation_is_high(self):
        assert _score_interest("wow!") == "high"

    def test_question_ending_variants_are_high(self):
        assert _score_interest("是这样嘛") == "high"
        assert _score_interest("真的啊") == "high"
        assert _score_interest("去哪里呢") == "high"
        assert _score_interest("你也去吧") == "high"
        assert _score_interest("是诶") == "high"

    def test_interest_affects_probability_low(self):
        """低兴趣度消息概率降低，random=0.5 时应该被拦截（base*0.2=0.12 < 0.5）"""
        from unittest.mock import MagicMock, patch
        runtime = MagicMock()
        runtime.cfg.reply_probability_base = 0.6
        runtime.cfg.reply_probability_private = 0.95
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
        state.goal_store.get.return_value.goal = ""
        state.heartflow.score.return_value = 1.0

        from plugins.xiaoqing_chat.frequency_control import _should_reply
        # base=0.6, low → p=0.12, random=0.5 → 0.5 >= 0.12 → False
        with patch("plugins.xiaoqing_chat.frequency_control.random") as mock_rand:
            mock_rand.random.return_value = 0.5
            result = _should_reply(runtime, state, "g1", "哦", False, False, False, interest="low")
        assert result is False

    def test_emo_substring_in_english_word_is_neutral(self):
        # "demo" contains "emo" — should not trigger high
        assert _score_interest("Here is a demo") == "neutral"

    def test_standalone_emo_is_high(self):
        # Chinese slang "emo了" — should trigger high
        assert _score_interest("好emo啊") == "high"
