"""消息解析和分发主流程。"""

from __future__ import annotations

import tests.helpers.dispatcher_test_support as _fixture_support
from tests.helpers.dispatcher_test_support import (
    AsyncMock,
    CommandSpec,
    Dispatcher,
    MagicMock,
    MessageParser,
    Mock,
    pytest,
)

dispatcher = _fixture_support.dispatcher
mock_admin_check = _fixture_support.mock_admin_check
mock_config_provider = _fixture_support.mock_config_provider
mock_context_factory = _fixture_support.mock_context_factory
mock_metrics = _fixture_support.mock_metrics
mock_router = _fixture_support.mock_router
mock_session_manager = _fixture_support.mock_session_manager
sample_message_context = _fixture_support.sample_message_context


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

    def test_parse_empty_message_can_be_retained_for_session_routing(
        self, mock_config_provider: MagicMock
    ):
        parser = MessageParser(mock_config_provider)
        event = {
            "post_type": "message",
            "message_type": "private",
            "user_id": 12345,
            "message": " ",
        }

        ctx = parser.parse(event, allow_empty_session_input=True)

        assert ctx is not None
        assert ctx.is_empty is True
        assert ctx.clean_text == ""

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
        ctx = parser.parse(
            {
                "post_type": "message",
                "message_type": "group",
                "user_id": 12345,
                "group_id": 67890,
                "self_id": 11111,
                "message": "/help",
            }
        )
        assert ctx is not None
        assert ctx.has_command_prefix is True
        assert ctx.has_prefix is True
        assert ctx.is_url_only is False

    def test_parse_has_prefix_from_bot_name_in_middle(self, mock_config_provider: MagicMock):
        parser = MessageParser(mock_config_provider)
        ctx = parser.parse(
            {
                "post_type": "message",
                "message_type": "group",
                "user_id": 12345,
                "group_id": 67890,
                "self_id": 11111,
                "message": "你好啊小青",
            }
        )
        assert ctx is not None
        assert ctx.has_command_prefix is False
        assert ctx.has_bot_name is True
        assert ctx.has_prefix is True

    def test_parse_is_url_only_with_bot_name_prefix(self, mock_config_provider: MagicMock):
        parser = MessageParser(mock_config_provider)
        ctx = parser.parse(
            {
                "post_type": "message",
                "message_type": "group",
                "user_id": 12345,
                "group_id": 67890,
                "self_id": 11111,
                "message": "小青 https://example.com",
            }
        )
        assert ctx is not None
        assert ctx.is_url_only is True
        assert ctx.clean_text == "https://example.com"

    def test_parse_url_with_extra_text_is_not_url_only(self, mock_config_provider: MagicMock):
        parser = MessageParser(mock_config_provider)
        ctx = parser.parse(
            {
                "post_type": "message",
                "message_type": "group",
                "user_id": 12345,
                "group_id": 67890,
                "self_id": 11111,
                "message": "看看 https://example.com",
            }
        )
        assert ctx is not None
        assert ctx.is_url_only is False

    def test_parse_at_me_with_empty_text_is_only_bot_name(self, mock_config_provider: MagicMock):
        """@me with no following text yields is_only_bot_name=True and is_url_only=False."""
        parser = MessageParser(mock_config_provider)
        ctx = parser.parse(
            {
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
            }
        )
        assert ctx is not None
        assert ctx.is_at_me is True
        assert ctx.is_only_bot_name is True
        assert ctx.is_url_only is False
        assert ctx.has_prefix is True


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
    async def test_url_route_skips_chat_observer_and_keeps_signed_query_out_of_logs(
        self,
        dispatcher: Dispatcher,
        mock_plugin_registry: MagicMock,
        mock_config_provider: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ):
        """URL 仍交给解析器，但签名参数不得进入聊天记忆或日志。"""
        mock_config_provider.config["plugins"]["smalltalk_provider"] = "xiaoqing_chat"
        mock_config_provider.config["require_bot_name_in_group"] = False

        xq_plugin = MagicMock()
        xq_plugin.module.observe_message = AsyncMock(return_value=[])

        url_plugin = MagicMock()
        url_plugin.module.handle_url = AsyncMock(
            return_value=[{"type": "text", "data": {"text": "url ok"}}]
        )

        def _get_plugin(name: str):
            if name == "xiaoqing_chat":
                return xq_plugin
            if name == "url_parser":
                return url_plugin
            return None

        mock_plugin_registry.get = Mock(side_effect=_get_plugin)

        signed_url = "https://example.com/file?token=super-secret#private-fragment"
        event = {
            "post_type": "message",
            "message_type": "group",
            "user_id": 12345,
            "group_id": 67890,
            "self_id": 11111,
            "message": signed_url,
            "raw_message": signed_url,
            "message_id": 88,
        }

        with caplog.at_level("INFO"):
            result = await dispatcher.handle_event(event)

        assert result == [{"type": "text", "data": {"text": "url ok"}}]
        xq_plugin.module.observe_message.assert_not_awaited()
        url_plugin.module.handle_url.assert_awaited_once()
        assert url_plugin.module.handle_url.await_args.args[0] == signed_url
        assert "super-secret" not in caplog.text
        assert "private-fragment" not in caplog.text

    @pytest.mark.asyncio
    async def test_observe_message_skips_command_short_circuit(
        self,
        dispatcher: Dispatcher,
        mock_plugin_registry: MagicMock,
        mock_config_provider: MagicMock,
        mock_router: MagicMock,
    ):
        """命令正文属于结构化输入，不应进入闲聊记忆。"""
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
        xq_plugin.module.observe_message.assert_not_awaited()
        cmd_handler.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_observe_message_skips_denied_admin_command(
        self,
        dispatcher: Dispatcher,
        mock_plugin_registry: MagicMock,
        mock_config_provider: MagicMock,
        mock_router: MagicMock,
        mock_admin_check: MagicMock,
    ):
        mock_config_provider.config["plugins"]["smalltalk_provider"] = "xiaoqing_chat"
        mock_admin_check.is_admin.return_value = False
        xq_plugin = MagicMock()
        xq_plugin.module.observe_message = AsyncMock(return_value=[])
        mock_plugin_registry.get.side_effect = lambda name: (
            xq_plugin if name == "xiaoqing_chat" else None
        )
        handler = AsyncMock()
        spec = CommandSpec(
            plugin="shell",
            name="shell",
            triggers=["shell"],
            help_text="shell",
            admin_only=True,
            handler=handler,
        )
        mock_router.resolve.return_value = (spec, "authorization=Bearer-canary")
        event = {
            "post_type": "message",
            "message_type": "private",
            "user_id": 12345,
            "self_id": 11111,
            "message": "/shell authorization=Bearer-canary",
            "raw_message": "/shell authorization=Bearer-canary",
        }

        result = await dispatcher.handle_event(event)

        assert result == [{"type": "text", "data": {"text": "权限不足"}}]
        xq_plugin.module.observe_message.assert_not_awaited()
        handler.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_observe_message_skips_unknown_prefixed_command(
        self,
        dispatcher: Dispatcher,
        mock_plugin_registry: MagicMock,
        mock_config_provider: MagicMock,
        mock_router: MagicMock,
    ):
        mock_config_provider.config["plugins"]["smalltalk_provider"] = "xiaoqing_chat"
        mock_router.resolve.return_value = None
        xq_plugin = MagicMock()
        xq_plugin.module.observe_message = AsyncMock(return_value=[])
        mock_plugin_registry.get.side_effect = lambda name: (
            xq_plugin if name == "xiaoqing_chat" else None
        )
        event = {
            "post_type": "message",
            "message_type": "group",
            "user_id": 12345,
            "group_id": 67890,
            "self_id": 11111,
            "message": "/unknown authorization=Bearer-canary",
            "raw_message": "/unknown authorization=Bearer-canary",
        }

        result = await dispatcher.handle_event(event)

        assert result[0]["data"]["text"].startswith("❓ 未知命令")
        xq_plugin.module.observe_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_observe_message_skips_privileged_session_input(
        self,
        dispatcher: Dispatcher,
        mock_plugin_registry: MagicMock,
        mock_config_provider: MagicMock,
        mock_router: MagicMock,
        mock_session_manager: MagicMock,
    ):
        mock_config_provider.config["plugins"]["smalltalk_provider"] = "xiaoqing_chat"
        mock_router.resolve.return_value = None
        session = MagicMock()
        session.plugin_name = "qingssh"
        mock_session_manager.get.return_value = session

        xq_plugin = MagicMock()
        xq_plugin.module.observe_message = AsyncMock(return_value=[])
        ssh_plugin = MagicMock()
        ssh_plugin.module.handle_session = AsyncMock(
            return_value=[{"type": "text", "data": {"text": "configured"}}]
        )
        mock_plugin_registry.get.side_effect = lambda name: {
            "xiaoqing_chat": xq_plugin,
            "qingssh": ssh_plugin,
        }.get(name)
        event = {
            "post_type": "message",
            "message_type": "private",
            "user_id": 12345,
            "self_id": 11111,
            "message": "ssh-password-canary",
            "raw_message": "ssh-password-canary",
        }

        result = await dispatcher.handle_event(event)

        assert result == [{"type": "text", "data": {"text": "configured"}}]
        xq_plugin.module.observe_message.assert_not_awaited()
        ssh_plugin.module.handle_session.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_observe_message_keeps_plain_group_chatter(
        self,
        dispatcher: Dispatcher,
        mock_plugin_registry: MagicMock,
        mock_config_provider: MagicMock,
        mock_router: MagicMock,
        mock_session_manager: MagicMock,
    ):
        mock_config_provider.config["plugins"]["smalltalk_provider"] = "xiaoqing_chat"
        mock_router.resolve.return_value = None
        mock_session_manager.get.return_value = None
        xq_plugin = MagicMock()
        xq_plugin.module.observe_message = AsyncMock(return_value=[])
        xq_plugin.module.handle_smalltalk = AsyncMock(return_value=[])
        mock_plugin_registry.get.side_effect = lambda name: (
            xq_plugin if name == "xiaoqing_chat" else None
        )
        event = {
            "post_type": "message",
            "message_type": "group",
            "user_id": 12345,
            "group_id": 67890,
            "self_id": 11111,
            "message": "普通群聊内容",
            "raw_message": "普通群聊内容",
        }

        await dispatcher.handle_event(event)

        xq_plugin.module.observe_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_raw_message_only_event_reaches_command_handler(
        self,
        dispatcher: Dispatcher,
        mock_router: MagicMock,
    ):
        """Direct upstream events follow the same raw-message normalization path."""
        cmd_handler = AsyncMock(return_value=[{"type": "text", "data": {"text": "ok"}}])
        mock_router.resolve = Mock(
            return_value=(
                CommandSpec(
                    plugin="echo",
                    name="echo",
                    triggers=["echo"],
                    help_text="echo",
                    admin_only=False,
                    handler=cmd_handler,
                    priority=0,
                ),
                "hello",
            )
        )
        event = {
            "post_type": "message",
            "message_type": "private",
            "user_id": 12345,
            "self_id": 11111,
            "raw_message": "/echo hello",
        }

        result = await dispatcher.handle_event(event)

        assert result == [{"type": "text", "data": {"text": "ok"}}]
        cmd_handler.assert_awaited_once()
