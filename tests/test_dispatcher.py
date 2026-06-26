"""
Dispatcher 单元测试
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, Mock

from core.dispatcher import (
    Dispatcher,
    MessageContext,
    MessageParser,
)
from core.router import CommandRouter, CommandSpec

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
        has_command_prefix=True,
        is_only_bot_name=False,
        is_at_me=False,
        is_url_only=False,
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

    def test_parse_uses_cached_prefixes_until_explicit_refresh(self):
        """测试 parse 热路径不重复读取配置"""
        class CountingConfigProvider:
            def __init__(self):
                self.reads = 0
                self.payload = {
                    "bot_name": "小青",
                    "command_prefixes": ["/"],
                    "plugins": {},
                }

            @property
            def config(self):
                self.reads += 1
                return self.payload

        provider = CountingConfigProvider()
        parser = MessageParser(provider)
        event = {
            "post_type": "message",
            "message_type": "group",
            "user_id": 12345,
            "group_id": 67890,
            "self_id": 11111,
            "message": "/help",
        }

        assert provider.reads == 1
        assert parser.parse(event).has_command_prefix is True
        assert provider.reads == 1

        provider.payload["command_prefixes"] = ["!"]
        assert parser.parse(event).has_command_prefix is True
        assert provider.reads == 1

        parser.refresh_prefix_cache()
        assert provider.reads == 2
        assert parser.parse(event).has_command_prefix is False
        assert provider.reads == 2

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

    def test_parse_image_only_message_keeps_context(self, mock_config_provider: MagicMock):
        """测试纯图片消息不会被 parser 丢弃"""
        parser = MessageParser(mock_config_provider)
        event = {
            "post_type": "message",
            "message_type": "group",
            "user_id": 12345,
            "group_id": 67890,
            "self_id": 11111,
            "message": [{"type": "image", "data": {"file": "file:///tmp/test.png"}}],
        }

        ctx = parser.parse(event)

        assert ctx is not None
        assert ctx.text == ""
        assert ctx.clean_text == ""
        assert ctx.user_id == 12345
        assert ctx.group_id == 67890

    def test_parse_face_only_message_keeps_context(self, mock_config_provider: MagicMock):
        """测试纯 QQ 表情消息不会被 parser 丢弃"""
        parser = MessageParser(mock_config_provider)
        event = {
            "post_type": "message",
            "message_type": "group",
            "user_id": 12345,
            "group_id": 67890,
            "self_id": 11111,
            "message": [{"type": "face", "data": {"id": "14"}}],
        }

        ctx = parser.parse(event)

        assert ctx is not None
        assert ctx.text == ""
        assert ctx.clean_text == ""
        assert ctx.user_id == 12345
        assert ctx.group_id == 67890

    def test_parse_at_only_without_media_is_bot_name_only(self, mock_config_provider: MagicMock):
        """测试只 @ 机器人等同于只喊机器人名字"""
        parser = MessageParser(mock_config_provider)
        event = {
            "post_type": "message",
            "message_type": "group",
            "user_id": 12345,
            "group_id": 67890,
            "self_id": 11111,
            "message": [{"type": "at", "data": {"qq": "11111"}}],
        }

        ctx = parser.parse(event)
        assert ctx is not None
        assert ctx.text == ""
        assert ctx.clean_text == ""
        assert ctx.has_prefix is True
        assert ctx.is_at_me is True
        assert ctx.is_only_bot_name is True
        assert ctx.is_url_only is False

    def test_parse_populates_has_command_prefix(self, mock_config_provider: MagicMock):
        parser = MessageParser(mock_config_provider)
        ctx = parser.parse({
            "post_type": "message",
            "message_type": "group",
            "user_id": 12345,
            "group_id": 67890,
            "self_id": 11111,
            "message": "/help",
        })
        assert ctx is not None
        assert ctx.has_command_prefix is True
        assert ctx.has_prefix is True
        assert ctx.is_url_only is False

    def test_parse_has_prefix_from_bot_name_in_middle(self, mock_config_provider: MagicMock):
        parser = MessageParser(mock_config_provider)
        ctx = parser.parse({
            "post_type": "message",
            "message_type": "group",
            "user_id": 12345,
            "group_id": 67890,
            "self_id": 11111,
            "message": "你好啊小青",
        })
        assert ctx is not None
        assert ctx.has_command_prefix is False
        assert ctx.has_bot_name is True
        assert ctx.has_prefix is True

    def test_parse_is_url_only_with_bot_name_prefix(self, mock_config_provider: MagicMock):
        parser = MessageParser(mock_config_provider)
        ctx = parser.parse({
            "post_type": "message",
            "message_type": "group",
            "user_id": 12345,
            "group_id": 67890,
            "self_id": 11111,
            "message": "小青 https://example.com",
        })
        assert ctx is not None
        assert ctx.is_url_only is True
        assert ctx.clean_text == "https://example.com"

    def test_parse_url_with_extra_text_is_not_url_only(self, mock_config_provider: MagicMock):
        parser = MessageParser(mock_config_provider)
        ctx = parser.parse({
            "post_type": "message",
            "message_type": "group",
            "user_id": 12345,
            "group_id": 67890,
            "self_id": 11111,
            "message": "看看 https://example.com",
        })
        assert ctx is not None
        assert ctx.is_url_only is False

    def test_parse_at_me_with_empty_text_is_only_bot_name(self, mock_config_provider: MagicMock):
        """@me with no following text yields is_only_bot_name=True and is_url_only=False."""
        parser = MessageParser(mock_config_provider)
        ctx = parser.parse({
            "post_type": "message",
            "message_type": "group",
            "user_id": 12345,
            "group_id": 67890,
            "self_id": 11111,
            "message": [
                {"type": "at", "data": {"qq": "11111"}},
                {"type": "face", "data": {"id": "14"}},
            ],
            "raw_message": "[CQ:at,qq=11111][CQ:face,id=14]",
        })
        assert ctx is not None
        assert ctx.is_at_me is True
        assert ctx.is_only_bot_name is True
        assert ctx.is_url_only is False
        assert ctx.has_prefix is True

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
# Dispatcher._execute_command 测试
# ============================================================

class TestExecuteCommand:
    """_execute_command 测试"""

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

        result = await dispatcher._execute_command((spec, ""), sample_message_context)
        assert result is not None
        assert result[0]["data"]["text"] == "权限不足"


class TestUrlFiltering:
    def test_blocks_private_and_loopback_targets(self, dispatcher: Dispatcher):
        assert dispatcher._is_blocked_url_target("http://127.0.0.1/a") is True
        assert dispatcher._is_blocked_url_target("http://[::1]/a") is True
        assert dispatcher._is_blocked_url_target("http://169.254.169.254/latest/meta-data") is True
        assert dispatcher._is_blocked_url_target("http://localhost:8080") is True

    def test_allows_public_domain_targets(self, dispatcher: Dispatcher):
        assert dispatcher._is_blocked_url_target("https://example.com/path") is False

# ============================================================
# Dispatcher URL 处理测试
# ============================================================

class TestInvokeUrlParser:
    """_invoke_url_parser 测试"""

    @pytest.mark.asyncio
    async def test_url_invokes_plugin(
        self,
        dispatcher: Dispatcher,
        sample_message_context: MessageContext,
        mock_plugin_registry: MagicMock,
    ):
        mock_plugin = MagicMock()
        mock_plugin.module.handle_url = AsyncMock(return_value=[{"type": "text", "data": {"text": "URL handled"}}])
        mock_plugin_registry.get = Mock(return_value=mock_plugin)

        result = await dispatcher._invoke_url_parser(sample_message_context, "https://example.com")
        assert result is not None

    @pytest.mark.asyncio
    async def test_missing_url_plugin_returns_none(
        self,
        dispatcher: Dispatcher,
        sample_message_context: MessageContext,
        mock_plugin_registry: MagicMock,
    ):
        mock_plugin_registry.get = Mock(return_value=None)

        result = await dispatcher._invoke_url_parser(sample_message_context, "https://example.com")
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
            has_command_prefix=True,
            is_only_bot_name=False,
            is_at_me=False,
            is_url_only=False,
            event={},
        )

        assert ctx.request_id == "test_001"
        assert ctx.text == "/echo hello"
        assert ctx.clean_text == "echo hello"
        assert ctx.user_id == 12345
        assert ctx.group_id == 67890
        assert ctx.is_private is False

class TestProcessEventLinearFlow:
    """Tests for the linear Step A-G flow in _process_event."""

    @pytest.fixture
    def event_template(self):
        def _build(**overrides):
            base = {
                "post_type": "message",
                "message_type": "group",
                "user_id": 12345,
                "group_id": 67890,
                "self_id": 11111,
                "message": "",
            }
            base.update(overrides)
            return base

        return _build

    @pytest.mark.asyncio
    async def test_group_plain_text_dropped(self, dispatcher, event_template):
        result = await dispatcher.handle_event(event_template(message="今天天气真好"))
        assert result == []

    @pytest.mark.asyncio
    async def test_group_command_prefix_executes(
        self, dispatcher, mock_router, event_template, mock_plugin_registry
    ):
        spec = CommandSpec(
            plugin="echo",
            name="echo",
            triggers=["echo"],
            help_text="echo",
            handler=AsyncMock(return_value=[{"type": "text", "data": {"text": "hi"}}]),
            admin_only=False,
        )
        mock_router.resolve.return_value = (spec, "")
        result = await dispatcher.handle_event(event_template(message="/echo"))
        assert result and result[0]["data"]["text"] == "hi"

    @pytest.mark.asyncio
    async def test_group_bot_name_in_middle_falls_to_smalltalk(
        self, dispatcher, mock_router, mock_plugin_registry, event_template
    ):
        mock_router.resolve.return_value = None
        smalltalk = MagicMock()
        smalltalk.module.handle_smalltalk = AsyncMock(
            return_value=[{"type": "text", "data": {"text": "嗯嗯"}}]
        )
        mock_plugin_registry.get.return_value = smalltalk
        result = await dispatcher.handle_event(event_template(message="你好啊小青"))
        assert result and result[0]["data"]["text"] == "嗯嗯"

    @pytest.mark.asyncio
    async def test_group_url_only_invokes_url_parser_without_prefix(
        self, dispatcher, mock_plugin_registry, event_template
    ):
        url_parser = MagicMock()
        url_parser.module.handle_url = AsyncMock(
            return_value=[{"type": "text", "data": {"text": "parsed"}}]
        )
        mock_plugin_registry.get.side_effect = lambda name: url_parser if name == "url_parser" else None
        result = await dispatcher.handle_event(event_template(message="https://example.com"))
        assert result and result[0]["data"]["text"] == "parsed"

    @pytest.mark.asyncio
    async def test_group_url_with_text_not_url_parser(
        self, dispatcher, mock_plugin_registry, event_template
    ):
        url_parser = MagicMock()
        url_parser.module.handle_url = AsyncMock(return_value=[{"type": "text", "data": {"text": "x"}}])
        mock_plugin_registry.get.side_effect = lambda name: url_parser if name == "url_parser" else None
        result = await dispatcher.handle_event(event_template(message="看看 https://example.com"))
        assert result == []
        url_parser.module.handle_url.assert_not_called()

    @pytest.mark.asyncio
    async def test_private_plain_text_falls_to_smalltalk(
        self, dispatcher, mock_router, mock_plugin_registry, event_template
    ):
        mock_router.resolve.return_value = None
        smalltalk = MagicMock()
        smalltalk.module.handle_smalltalk = AsyncMock(
            return_value=[{"type": "text", "data": {"text": "嗯"}}]
        )
        mock_plugin_registry.get.return_value = smalltalk
        event = event_template(message_type="private", message="在吗")
        event.pop("group_id")
        result = await dispatcher.handle_event(event)
        assert result and result[0]["data"]["text"] == "嗯"

    @pytest.mark.asyncio
    async def test_private_url_invokes_url_parser(
        self, dispatcher, mock_plugin_registry, event_template
    ):
        url_parser = MagicMock()
        url_parser.module.handle_url = AsyncMock(return_value=[{"type": "text", "data": {"text": "p"}}])
        mock_plugin_registry.get.side_effect = lambda name: url_parser if name == "url_parser" else None
        event = event_template(message_type="private", message="https://example.com")
        event.pop("group_id")
        result = await dispatcher.handle_event(event)
        assert result and result[0]["data"]["text"] == "p"

    @pytest.mark.asyncio
    async def test_group_mute_blocks_smalltalk_only(
        self, dispatcher, mock_router, mock_plugin_registry, event_template
    ):
        dispatcher.mute_group(67890, 5.0)
        try:
            mock_router.resolve.return_value = None
            smalltalk = MagicMock()
            smalltalk.module.handle_smalltalk = AsyncMock(return_value=[{"type": "text", "data": {"text": "no"}}])
            mock_plugin_registry.get.return_value = smalltalk
            result = await dispatcher.handle_event(event_template(message="小青 你好"))
            assert result == []
            smalltalk.module.handle_smalltalk.assert_not_called()
        finally:
            dispatcher.unmute_group(67890)

    @pytest.mark.asyncio
    async def test_group_mute_does_not_block_command(
        self, dispatcher, mock_router, event_template
    ):
        dispatcher.mute_group(67890, 5.0)
        try:
            spec = CommandSpec(
                plugin="echo",
                name="echo",
                triggers=["echo"],
                help_text="echo",
                handler=AsyncMock(return_value=[{"type": "text", "data": {"text": "ok"}}]),
                admin_only=False,
            )
            mock_router.resolve.return_value = (spec, "")
            result = await dispatcher.handle_event(event_template(message="/echo"))
            assert result and result[0]["data"]["text"] == "ok"
        finally:
            dispatcher.unmute_group(67890)

    @pytest.mark.asyncio
    async def test_group_mute_does_not_block_url(
        self, dispatcher, mock_plugin_registry, event_template
    ):
        dispatcher.mute_group(67890, 5.0)
        try:
            url_parser = MagicMock()
            url_parser.module.handle_url = AsyncMock(
                return_value=[{"type": "text", "data": {"text": "p"}}]
            )
            mock_plugin_registry.get.side_effect = lambda name: url_parser if name == "url_parser" else None
            result = await dispatcher.handle_event(event_template(message="https://example.com"))
            assert result and result[0]["data"]["text"] == "p"
        finally:
            dispatcher.unmute_group(67890)

    @pytest.mark.asyncio
    async def test_require_bot_name_false_processes_group_plain_text(
        self, dispatcher, mock_router, mock_config_provider, mock_plugin_registry, event_template
    ):
        mock_config_provider.config["require_bot_name_in_group"] = False
        try:
            mock_router.resolve.return_value = None
            smalltalk = MagicMock()
            smalltalk.module.handle_smalltalk = AsyncMock(
                return_value=[{"type": "text", "data": {"text": "ok"}}]
            )
            mock_plugin_registry.get.return_value = smalltalk
            result = await dispatcher.handle_event(event_template(message="任意话题"))
            assert result and result[0]["data"]["text"] == "ok"
        finally:
            mock_config_provider.config["require_bot_name_in_group"] = True

    @pytest.mark.asyncio
    async def test_xiaoqing_provider_receives_group_plain_text_for_own_reply_gate(
        self, dispatcher, mock_router, mock_config_provider, mock_plugin_registry, event_template
    ):
        mock_config_provider.config["require_bot_name_in_group"] = True
        mock_config_provider.config["plugins"]["smalltalk_provider"] = "xiaoqing_chat"
        mock_router.resolve.return_value = None
        xiaoqing = MagicMock()
        xiaoqing.module.handle_smalltalk = AsyncMock(
            return_value=[{"type": "text", "data": {"text": "接一句"}}]
        )
        mock_plugin_registry.get.return_value = xiaoqing

        result = await dispatcher.handle_event(event_template(message="普通群聊消息"))

        assert result and result[0]["data"]["text"] == "接一句"
        xiaoqing.module.handle_smalltalk.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unknown_command_hint_only_for_slash_prefix(
        self, dispatcher, mock_router, mock_plugin_registry, event_template
    ):
        mock_router.resolve.return_value = None
        smalltalk = MagicMock()
        smalltalk.module.handle_smalltalk = AsyncMock(
            return_value=[{"type": "text", "data": {"text": "嗯"}}]
        )
        mock_plugin_registry.get.return_value = smalltalk
        result = await dispatcher.handle_event(event_template(message="小青 不存在的指令"))
        assert "未知命令" not in (result[0]["data"]["text"] if result else "")

    @pytest.mark.asyncio
    async def test_unknown_command_hint_for_slash_prefix(
        self, dispatcher, mock_router, event_template
    ):
        mock_router.resolve.return_value = None
        result = await dispatcher.handle_event(event_template(message="/未知命令"))
        assert result and "未知命令" in result[0]["data"]["text"]

    @pytest.mark.asyncio
    async def test_group_at_mention_triggers_has_prefix(
        self, dispatcher, mock_router, mock_plugin_registry, event_template
    ):
        mock_router.resolve.return_value = None
        smalltalk = MagicMock()
        smalltalk.module.handle_smalltalk = AsyncMock(
            return_value=[{"type": "text", "data": {"text": "嗯"}}]
        )
        mock_plugin_registry.get.return_value = smalltalk
        event = event_template(message=[
            {"type": "at", "data": {"qq": "11111"}},
            {"type": "text", "data": {"text": " 帮我看看"}},
        ])
        event["raw_message"] = "[CQ:at,qq=11111] 帮我看看"
        result = await dispatcher.handle_event(event)
        assert result and result[0]["data"]["text"] == "嗯"

    @pytest.mark.asyncio
    async def test_group_at_only_calls_bot_name_only(
        self, dispatcher, mock_plugin_registry, event_template
    ):
        smalltalk = MagicMock()
        smalltalk.module.call_bot_name_only = AsyncMock(
            return_value=[{"type": "text", "data": {"text": "在的"}}]
        )
        mock_plugin_registry.get.return_value = smalltalk
        event = event_template(message=[{"type": "at", "data": {"qq": "11111"}}])
        event["raw_message"] = "[CQ:at,qq=11111]"
        result = await dispatcher.handle_event(event)
        assert result and result[0]["data"]["text"] == "在的"

    @pytest.mark.asyncio
    async def test_private_only_bot_name_calls_bot_name_only(
        self, dispatcher, mock_plugin_registry, event_template
    ):
        smalltalk = MagicMock()
        smalltalk.module.call_bot_name_only = AsyncMock(
            return_value=[{"type": "text", "data": {"text": "在的"}}]
        )
        mock_plugin_registry.get.return_value = smalltalk
        event = event_template(message_type="private", message="小青")
        event.pop("group_id")
        result = await dispatcher.handle_event(event)
        assert result and result[0]["data"]["text"] == "在的"

    @pytest.mark.asyncio
    async def test_group_no_prefix_with_active_session_routes_to_session(
        self, dispatcher, mock_router, mock_plugin_registry, mock_session_manager, event_template
    ):
        mock_router.resolve.return_value = None
        session = MagicMock()
        session.plugin_name = "pendo"
        mock_session_manager.get = AsyncMock(return_value=session)

        pendo = MagicMock()
        pendo.module.handle_session = AsyncMock(
            return_value=[{"type": "text", "data": {"text": "session reply"}}]
        )
        mock_plugin_registry.get.return_value = pendo

        result = await dispatcher.handle_event(event_template(message="第三个选项"))
        assert result and result[0]["data"]["text"] == "session reply"

    @pytest.mark.asyncio
    async def test_group_only_bot_name_with_active_session_does_not_preempt(
        self, dispatcher, mock_plugin_registry, mock_session_manager, event_template
    ):
        session = MagicMock()
        session.plugin_name = "pendo"
        mock_session_manager.get = AsyncMock(return_value=session)

        smalltalk = MagicMock()
        smalltalk.module.call_bot_name_only = AsyncMock(
            return_value=[{"type": "text", "data": {"text": "在的"}}]
        )
        pendo = MagicMock()
        pendo.module.handle_session = AsyncMock(return_value=[{"type": "text", "data": {"text": "WRONG"}}])

        def _get(name):
            return {"pendo": pendo}.get(name, smalltalk)

        mock_plugin_registry.get.side_effect = _get

        result = await dispatcher.handle_event(event_template(message="小青"))
        assert result and result[0]["data"]["text"] == "在的"
        pendo.module.handle_session.assert_not_called()

# ============================================================
# 运行测试
# ============================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
