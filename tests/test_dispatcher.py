"""
Dispatcher 单元测试
"""

import asyncio
import pytest
from pathlib import Path
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from core.dispatcher import (
    Dispatcher,
    MessageContext,
    ProcessDecision,
    MessageParser,
    BotNameHandler,
    CommandHandler,
    SessionHandler,
    SmalltalkHandler,
)
from core.router import CommandRouter, CommandSpec
from core.clock import SystemClock, SystemRandom

# ============================================================
# Fixtures
# ============================================================

@pytest.fixture
def mock_config_provider():
    """模拟配置提供者"""
    mock = MagicMock()
    mock.config = {
        "bot_name": "小青",
        "command_prefixes": ["/"],
        "require_bot_name_in_group": True,
        "random_reply_rate": 0.05,
        "plugins": {
            "smalltalk_provider": "smalltalk",
        },
    }
    return mock


@pytest.fixture
def mock_router():
    """模拟路由器"""
    mock = MagicMock(spec=CommandRouter)
    mock.resolve = Mock(return_value=None)  # 默认未匹配任何命令，防止 MagicMock 被误解包
    return mock


@pytest.fixture
def mock_plugin_registry():
    """模拟插件注册表"""
    mock = MagicMock()
    mock.get = Mock(return_value=None)
    return mock


@pytest.fixture
def mock_admin_check():
    """模拟管理员检查"""
    mock = MagicMock()
    mock.is_admin = Mock(return_value=True)
    return mock


@pytest.fixture
def mock_context_factory():
    """模拟上下文工厂"""
    def _factory(*args, **kwargs):
        return MagicMock()
    return _factory


@pytest.fixture
def mock_session_manager():
    """模拟会话管理器"""
    mock = MagicMock()
    mock.get = AsyncMock(return_value=None)
    mock.exists = AsyncMock(return_value=False)
    return mock


@pytest.fixture
def mock_metrics():
    """模拟指标收集器"""
    mock = MagicMock()
    mock.record_plugin_execution = AsyncMock()
    return mock


@pytest.fixture
def sample_message_context() -> MessageContext:
    """创建示例消息上下文"""
    return MessageContext(
        request_id="test_001",
        text="/echo hello",
        clean_text="echo hello",
        user_id=12345,
        group_id=67890,
        is_private=False,
        has_bot_name=False,
        has_prefix=True,
        is_only_bot_name=False,
        is_at_me=False,
        event={},
    )


@pytest.fixture
def dispatcher(
    mock_router: MagicMock,
    mock_config_provider: MagicMock,
    mock_plugin_registry: MagicMock,
    mock_admin_check: MagicMock,
    mock_context_factory: MagicMock,
    mock_session_manager: MagicMock,
    mock_metrics: MagicMock,
):
    """创建 Dispatcher 实例"""
    semaphore = asyncio.Semaphore(10)
    return Dispatcher(
        router=mock_router,
        config_provider=mock_config_provider,
        plugin_registry=mock_plugin_registry,
        admin_check=mock_admin_check,
        build_context=mock_context_factory,
        semaphore=semaphore,
        session_manager=mock_session_manager,
        metrics=mock_metrics,
    )

# ============================================================
# MessageParser 测试
# ============================================================

class TestMessageParser:
    """MessageParser 测试"""

    def test_initialization(self, mock_config_provider: MagicMock):
        """测试初始化"""
        parser = MessageParser(mock_config_provider)
        assert parser._config_provider is mock_config_provider
        assert parser._cached_bot_name == "小青"

    def test_parse_group_message(self, mock_config_provider: MagicMock):
        """测试解析群消息"""
        parser = MessageParser(mock_config_provider)
        event = {
            "post_type": "message",
            "message_type": "group",
            "user_id": 12345,
            "group_id": 67890,
            "self_id": 11111,
            "message": "/echo hello",
        }

        ctx = parser.parse(event)

        assert ctx is not None
        assert ctx.user_id == 12345
        assert ctx.group_id == 67890
        assert ctx.is_private is False

    def test_parse_private_message(self, mock_config_provider: MagicMock):
        """测试解析私聊消息"""
        parser = MessageParser(mock_config_provider)
        event = {
            "post_type": "message",
            "message_type": "private",
            "user_id": 12345,
            "self_id": 11111,
            "message": "你好",
        }

        ctx = parser.parse(event)

        assert ctx is not None
        assert ctx.user_id == 12345
        assert ctx.group_id is None
        assert ctx.is_private is True

    def test_parse_self_message_returns_none(self, mock_config_provider: MagicMock):
        """测试解析自己的消息返回 None"""
        parser = MessageParser(mock_config_provider)
        event = {
            "post_type": "message",
            "message_type": "group",
            "user_id": 11111,  # 和 self_id 相同
            "self_id": 11111,
            "group_id": 67890,
            "message": "test",
        }

        ctx = parser.parse(event)
        assert ctx is None

    def test_parse_empty_message_returns_none(self, mock_config_provider: MagicMock):
        """测试解析空消息返回 None"""
        parser = MessageParser(mock_config_provider)
        event = {
            "post_type": "message",
            "message_type": "group",
            "user_id": 12345,
            "group_id": 67890,
            "message": "",
        }

        ctx = parser.parse(event)
        assert ctx is None

# ============================================================
# Dispatcher.handle_event 测试
# ============================================================

class TestDispatcherHandleEvent:
    """Dispatcher.handle_event 测试"""

    @pytest.mark.asyncio
    async def test_handles_message_event(self, dispatcher: Dispatcher):
        """测试处理消息事件"""
        event = {
            "post_type": "message",
            "message_type": "group",
            "user_id": 12345,
            "group_id": 67890,
            "self_id": 11111,
            "message": "test",
        }

        result = await dispatcher.handle_event(event)
        assert isinstance(result, list)

    @pytest.mark.asyncio
    async def test_ignores_non_message_event(self, dispatcher: Dispatcher):
        """测试忽略非消息事件"""
        event = {
            "post_type": "notice",
            "notice_type": "group_increase",
        }

        result = await dispatcher.handle_event(event)
        assert result == []

    @pytest.mark.asyncio
    async def test_observe_message_runs_before_url_short_circuit(
        self,
        dispatcher: Dispatcher,
        mock_plugin_registry: MagicMock,
        mock_config_provider: MagicMock,
    ):
        """即使 URL 被提前处理，也应先调用 smalltalk provider 的 observe_message。"""
        mock_config_provider.config["plugins"]["smalltalk_provider"] = "xiaoqing_chat"

        xq_plugin = MagicMock()
        xq_plugin.module.observe_message = AsyncMock(return_value=[])

        url_plugin = MagicMock()
        url_plugin.module.handle_url = AsyncMock(return_value=[{"type": "text", "data": {"text": "url ok"}}])

        def _get_plugin(name: str):
            if name == "xiaoqing_chat":
                return xq_plugin
            if name == "url_parser":
                return url_plugin
            return None

        mock_plugin_registry.get = Mock(side_effect=_get_plugin)

        event = {
            "post_type": "message",
            "message_type": "group",
            "user_id": 12345,
            "group_id": 67890,
            "self_id": 11111,
            "message": "https://example.com",
            "raw_message": "https://example.com",
            "message_id": 88,
        }

        result = await dispatcher.handle_event(event)

        assert result == [{"type": "text", "data": {"text": "url ok"}}]
        xq_plugin.module.observe_message.assert_awaited_once()
        url_plugin.module.handle_url.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_observe_message_runs_before_command_short_circuit(
        self,
        dispatcher: Dispatcher,
        mock_plugin_registry: MagicMock,
        mock_config_provider: MagicMock,
        mock_router: MagicMock,
    ):
        """即使命令命中提前返回，也应先调用 observe_message。"""
        mock_config_provider.config["plugins"]["smalltalk_provider"] = "xiaoqing_chat"

        xq_plugin = MagicMock()
        xq_plugin.module.observe_message = AsyncMock(return_value=[])

        def _get_plugin(name: str):
            if name == "xiaoqing_chat":
                return xq_plugin
            return None

        mock_plugin_registry.get = Mock(side_effect=_get_plugin)

        cmd_handler = AsyncMock(return_value=[{"type": "text", "data": {"text": "command ok"}}])
        spec = CommandSpec(
            plugin="echo",
            name="echo",
            triggers=["echo"],
            help_text="echo",
            admin_only=False,
            handler=cmd_handler,
            priority=0,
        )
        mock_router.resolve = Mock(return_value=(spec, "hello"))

        event = {
            "post_type": "message",
            "message_type": "group",
            "user_id": 12345,
            "group_id": 67890,
            "self_id": 11111,
            "message": "/echo hello",
            "raw_message": "/echo hello",
            "message_id": 99,
        }

        result = await dispatcher.handle_event(event)

        assert result == [{"type": "text", "data": {"text": "command ok"}}]
        xq_plugin.module.observe_message.assert_awaited_once()
        cmd_handler.assert_awaited_once()

# ============================================================
# Dispatcher._decide_process 测试
# ============================================================

class TestDecideProcess:
    """_decide_process 测试"""

    def test_private_always_process(self, dispatcher: Dispatcher, sample_message_context: MessageContext):
        """测试私聊消息始终处理"""
        ctx = sample_message_context
        ctx.is_private = True

        decision = dispatcher._decide_process(ctx)

        assert decision.should_process is True

    def test_prefix_triggers_processing(self, dispatcher: Dispatcher, sample_message_context: MessageContext):
        """测试命令前缀触发处理"""
        ctx = sample_message_context
        ctx.has_prefix = True

        decision = dispatcher._decide_process(ctx)

        assert decision.should_process is True
        assert decision.smalltalk_mode is False

    def test_bot_name_triggers_processing(self, dispatcher: Dispatcher, sample_message_context: MessageContext):
        """测试 bot_name 触发处理"""
        ctx = sample_message_context
        ctx.has_bot_name = True

        decision = dispatcher._decide_process(ctx)

        assert decision.should_process is True

    def test_at_me_triggers_processing(self, dispatcher: Dispatcher, sample_message_context: MessageContext):
        """测试 @ 机器人触发处理"""
        ctx = sample_message_context
        ctx.is_at_me = True

        decision = dispatcher._decide_process(ctx)

        assert decision.should_process is True

# ============================================================
# Dispatcher 静音控制测试
# ============================================================

class TestDispatcherMuteControl:
    """Dispatcher 静音控制测试"""

    def test_mute_group(self, dispatcher: Dispatcher):
        """测试静音群"""
        dispatcher.mute_group(12345, 10.0)
        assert dispatcher.is_muted(12345) is True

    def test_unmute_group(self, dispatcher: Dispatcher):
        """测试取消静音"""
        dispatcher.mute_group(12345, 10.0)
        result = dispatcher.unmute_group(12345)
        assert result is True
        assert dispatcher.is_muted(12345) is False

    def test_unmute_non_muted_group(self, dispatcher: Dispatcher):
        """测试取消静音未静音的群"""
        result = dispatcher.unmute_group(12345)
        assert result is False

    def test_private_never_muted(self, dispatcher: Dispatcher):
        """测试私聊不受静音影响"""
        dispatcher.mute_group(12345, 10.0)
        assert dispatcher.is_muted(None) is False

    def test_mute_expiration(self, dispatcher: Dispatcher):
        """测试静音过期"""
        # 使用短时间的 mock clock
        dispatcher.mute_group(12345, 0.001)  # 非常短的静音时间
        # 等待过期
        import time
        time.sleep(0.1)
        assert dispatcher.is_muted(12345) is False

    def test_get_mute_remaining(self, dispatcher: Dispatcher):
        """测试获取剩余静音时间"""
        dispatcher.mute_group(12345, 10.0)
        remaining = dispatcher.get_mute_remaining(12345)
        assert 0 < remaining <= 10

    def test_get_mute_remaining_no_mute(self, dispatcher: Dispatcher):
        """测试未静音时获取剩余时间"""
        remaining = dispatcher.get_mute_remaining(12345)
        assert remaining == 0

# ============================================================
# Dispatcher._try_handle_command 测试
# ============================================================

class TestTryHandleCommand:
    """_try_handle_command 测试"""

    @pytest.mark.asyncio
    async def test_command_not_matched_returns_none(
        self,
        dispatcher: Dispatcher,
        sample_message_context: MessageContext,
        mock_router: MagicMock,
    ):
        """测试未匹配命令返回 None（无前缀情况）"""
        mock_router.resolve = Mock(return_value=None)

        # 创建一个没有前缀的上下文
        no_prefix_ctx = MessageContext(
            request_id="test_002",
            text="hello world",
            clean_text="hello world",
            user_id=12345,
            group_id=67890,
            is_private=False,
            has_bot_name=False,
            has_prefix=False,  # 没有命令前缀
            is_only_bot_name=False,
            is_at_me=False,
            event={},
        )

        result = await dispatcher._try_handle_command(no_prefix_ctx)
        assert result is None

    @pytest.mark.asyncio
    async def test_command_not_matched_with_prefix_returns_help(
        self,
        dispatcher: Dispatcher,
        sample_message_context: MessageContext,
        mock_router: MagicMock,
    ):
        """测试有前缀但未匹配命令时返回帮助提示"""
        mock_router.resolve = Mock(return_value=None)

        result = await dispatcher._try_handle_command(sample_message_context)
        assert result is not None
        assert len(result) == 1
        assert "未知命令" in result[0]["data"]["text"]
        assert "/help" in result[0]["data"]["text"]

    @pytest.mark.asyncio
    async def test_admin_only_denied(
        self,
        dispatcher: Dispatcher,
        sample_message_context: MessageContext,
        mock_router: MagicMock,
        mock_admin_check: MagicMock,
    ):
        """测试管理员命令权限检查"""
        mock_admin_check.is_admin = Mock(return_value=False)

        spec = CommandSpec(
            plugin="admin",
            name="reload",
            triggers=["reload"],
            help_text="重载",
            admin_only=True,
            handler=AsyncMock(),
            priority=0,
        )
        mock_router.resolve = Mock(return_value=(spec, ""))

        result = await dispatcher._try_handle_command(sample_message_context)
        assert result is not None
        assert result[0]["data"]["text"] == "权限不足"

# ============================================================
# Dispatcher URL 处理测试
# ============================================================

class TestTryHandleUrl:
    """_try_handle_url 测试"""

    @pytest.mark.asyncio
    async def test_url_detected_and_handled(
        self,
        dispatcher: Dispatcher,
        sample_message_context: MessageContext,
        mock_plugin_registry: MagicMock,
    ):
        """测试 URL 检测和处理"""
        ctx = sample_message_context
        ctx.text = "Check out https://example.com"
        ctx.has_prefix = False

        # Mock url_parser 插件
        mock_plugin = MagicMock()
        mock_plugin.module.handle_url = AsyncMock(return_value=[{"type": "text", "data": {"text": "URL handled"}}])
        mock_plugin_registry.get = Mock(return_value=mock_plugin)

        result = await dispatcher._try_handle_url(ctx)
        assert result is not None

    @pytest.mark.asyncio
    async def test_url_not_detected(
        self,
        dispatcher: Dispatcher,
        sample_message_context: MessageContext,
    ):
        """测试无 URL 时返回 None"""
        ctx = sample_message_context
        ctx.text = "No URL here"
        ctx.has_prefix = False

        result = await dispatcher._try_handle_url(ctx)
        assert result is None

    @pytest.mark.asyncio
    async def test_url_with_prefix_ignored(
        self,
        dispatcher: Dispatcher,
        sample_message_context: MessageContext,
    ):
        """测试有命令前缀时不处理 URL"""
        ctx = sample_message_context
        ctx.text = "/command https://example.com"
        ctx.has_prefix = True

        result = await dispatcher._try_handle_url(ctx)
        assert result is None

# ============================================================
# MessageContext 测试
# ============================================================

class TestMessageContext:
    """MessageContext 数据类测试"""

    def test_create_message_context(self):
        """测试创建消息上下文"""
        ctx = MessageContext(
            request_id="test_001",
            text="/echo hello",
            clean_text="echo hello",
            user_id=12345,
            group_id=67890,
            is_private=False,
            has_bot_name=False,
            has_prefix=True,
            is_only_bot_name=False,
            is_at_me=False,
            event={},
        )

        assert ctx.request_id == "test_001"
        assert ctx.text == "/echo hello"
        assert ctx.clean_text == "echo hello"
        assert ctx.user_id == 12345
        assert ctx.group_id == 67890
        assert ctx.is_private is False

# ============================================================
# ProcessDecision 测试
# ============================================================

class TestProcessDecision:
    """ProcessDecision 数据类测试"""

    def test_create_process_decision(self):
        """测试创建处理决策"""
        decision = ProcessDecision(should_process=True, smalltalk_mode=False)
        assert decision.should_process is True
        assert decision.smalltalk_mode is False

# ============================================================
# 运行测试
# ============================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
