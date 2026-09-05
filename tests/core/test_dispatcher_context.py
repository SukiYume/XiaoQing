"""URL 过滤、上下文和线性事件流。"""

from __future__ import annotations

import tests.helpers.dispatcher_test_support as _fixture_support
from tests.helpers.dispatcher_test_support import (
    AsyncMock,
    BlockingConcurrencyProbe,
    CommandSpec,
    Dispatcher,
    MagicMock,
    MessageContext,
    Mock,
    PluginExecutionGate,
    SimpleNamespace,
    asyncio,
    inspect,
    logging,
    pytest,
)

dispatcher             = _fixture_support.dispatcher
mock_admin_check       = _fixture_support.mock_admin_check
mock_config_provider   = _fixture_support.mock_config_provider
mock_context_factory   = _fixture_support.mock_context_factory
mock_metrics           = _fixture_support.mock_metrics
mock_router            = _fixture_support.mock_router
mock_session_manager   = _fixture_support.mock_session_manager
sample_message_context = _fixture_support.sample_message_context


class TestUrlFiltering:
    def test_blocks_private_and_loopback_targets(self, dispatcher: Dispatcher):
        assert dispatcher._is_blocked_url_target("http://127.0.0.1/a") is True
        assert dispatcher._is_blocked_url_target("http://[::1]/a") is True
        assert dispatcher._is_blocked_url_target("http://169.254.169.254/latest/meta-data") is True
        assert dispatcher._is_blocked_url_target("http://localhost:8080") is True
        assert dispatcher._is_blocked_url_target("http://2130706433/a") is True
        assert dispatcher._is_blocked_url_target("http://0x7f000001/a") is True

    def test_allows_public_domain_targets(self, dispatcher: Dispatcher):
        assert dispatcher._is_blocked_url_target("https://example.com/path") is False


class TestInvokeUrlParser:
    """_invoke_url_parser 测试"""

    @pytest.mark.asyncio
    async def test_url_invokes_plugin(
        self,
        dispatcher: Dispatcher,
        sample_message_context: MessageContext,
        mock_plugin_registry: MagicMock,
    ):
        mock_plugin                   = MagicMock()
        mock_plugin.module.handle_url = AsyncMock(
            return_value=[{"type": "text", "data": {"text": "URL handled"}}]
        )
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


class TestMessageContext:
    """MessageContext 数据类测试"""

    def test_create_message_context(self):
        """测试创建消息上下文"""
        ctx = MessageContext(
            request_id         = "test_001",
            text               = "/echo hello",
            clean_text         = "echo hello",
            user_id            = 12345,
            group_id           = 67890,
            is_private         = False,
            has_bot_name       = False,
            has_prefix         = True,
            has_command_prefix = True,
            is_only_bot_name   = False,
            is_at_me           = False,
            is_url_only        = False,
            event              = {},
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
            plugin    = "echo",
            name      = "echo",
            triggers  = ["echo"],
            help_text = "echo",
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
        mock_router.resolve.return_value  = None
        smalltalk                         = MagicMock()
        smalltalk.module.handle_smalltalk = AsyncMock(
            return_value=[{"type": "text", "data": {"text": "嗯嗯"}}]
        )
        mock_plugin_registry.get.return_value = smalltalk
        result = await dispatcher.handle_event(event_template(message="你好啊小青"))
        assert result and result[0]["data"]["text"] == "嗯嗯"

    @pytest.mark.asyncio
    async def test_group_url_only_is_blocked_by_process_gate(
        self, dispatcher, mock_plugin_registry, event_template
    ):
        url_parser                   = MagicMock()
        url_parser.module.handle_url = AsyncMock(
            return_value=[{"type": "text", "data": {"text": "parsed"}}]
        )
        mock_plugin_registry.get.side_effect = lambda name: (
            url_parser if name == "url_parser" else None
        )
        result = await dispatcher.handle_event(event_template(message="https://example.com"))
        assert result == []
        url_parser.module.handle_url.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_group_url_only_invokes_url_parser_when_group_processing_enabled(
        self, dispatcher, mock_config_provider, mock_plugin_registry, event_template
    ):
        mock_config_provider.config["require_bot_name_in_group"] = False
        url_parser                                               = MagicMock()
        url_parser.module.handle_url                             = AsyncMock(
            return_value=[{"type": "text", "data": {"text": "parsed"}}]
        )
        mock_plugin_registry.get.side_effect = lambda name: (
            url_parser if name == "url_parser" else None
        )
        result = await dispatcher.handle_event(event_template(message="https://example.com"))
        assert result and result[0]["data"]["text"] == "parsed"

    @pytest.mark.asyncio
    async def test_group_url_with_text_not_url_parser(
        self, dispatcher, mock_plugin_registry, event_template
    ):
        url_parser                   = MagicMock()
        url_parser.module.handle_url = AsyncMock(
            return_value=[{"type": "text", "data": {"text": "x"}}]
        )
        mock_plugin_registry.get.side_effect = lambda name: (
            url_parser if name == "url_parser" else None
        )
        result = await dispatcher.handle_event(event_template(message="看看 https://example.com"))
        assert result == []
        url_parser.module.handle_url.assert_not_called()

    @pytest.mark.asyncio
    async def test_private_plain_text_falls_to_smalltalk(
        self, dispatcher, mock_router, mock_plugin_registry, event_template
    ):
        mock_router.resolve.return_value  = None
        smalltalk                         = MagicMock()
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
        url_parser                   = MagicMock()
        url_parser.module.handle_url = AsyncMock(
            return_value=[{"type": "text", "data": {"text": "p"}}]
        )
        mock_plugin_registry.get.side_effect = lambda name: (
            url_parser if name == "url_parser" else None
        )
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
            mock_router.resolve.return_value  = None
            smalltalk                         = MagicMock()
            smalltalk.module.handle_smalltalk = AsyncMock(
                return_value=[{"type": "text", "data": {"text": "no"}}]
            )
            mock_plugin_registry.get.return_value = smalltalk
            result = await dispatcher.handle_event(event_template(message="小青 你好"))
            assert result == []
            smalltalk.module.handle_smalltalk.assert_not_called()
        finally:
            dispatcher.unmute_group(67890)

    @pytest.mark.asyncio
    async def test_group_mute_does_not_block_command(self, dispatcher, mock_router, event_template):
        dispatcher.mute_group(67890, 5.0)
        try:
            spec = CommandSpec(
                plugin    = "echo",
                name      = "echo",
                triggers  = ["echo"],
                help_text = "echo",
                handler=AsyncMock(return_value=[{"type": "text", "data": {"text": "ok"}}]),
                admin_only=False,
            )
            mock_router.resolve.return_value = (spec, "")
            result = await dispatcher.handle_event(event_template(message="/echo"))
            assert result and result[0]["data"]["text"] == "ok"
        finally:
            dispatcher.unmute_group(67890)

    @pytest.mark.asyncio
    async def test_group_mute_blocks_url(self, dispatcher, mock_plugin_registry, event_template):
        dispatcher.mute_group(67890, 5.0)
        try:
            url_parser                   = MagicMock()
            url_parser.module.handle_url = AsyncMock(
                return_value=[{"type": "text", "data": {"text": "p"}}]
            )
            mock_plugin_registry.get.side_effect = lambda name: (
                url_parser if name == "url_parser" else None
            )
            result = await dispatcher.handle_event(event_template(message="https://example.com"))
            assert result == []
            url_parser.module.handle_url.assert_not_awaited()
        finally:
            dispatcher.unmute_group(67890)

    @pytest.mark.asyncio
    async def test_require_bot_name_false_processes_group_plain_text(
        self, dispatcher, mock_router, mock_config_provider, mock_plugin_registry, event_template
    ):
        mock_config_provider.config["require_bot_name_in_group"] = False
        try:
            mock_router.resolve.return_value  = None
            smalltalk                         = MagicMock()
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
        mock_config_provider.config["require_bot_name_in_group"]     = True
        mock_config_provider.config["plugins"]["smalltalk_provider"] = "xiaoqing_chat"
        mock_router.resolve.return_value                             = None
        xiaoqing                                                     = MagicMock()
        xiaoqing.module.observe_message = AsyncMock(return_value=[])
        xiaoqing.module.handle_smalltalk = AsyncMock(
            return_value=[{"type": "text", "data": {"text": "接一句"}}]
        )
        generic                         = MagicMock()
        generic.module.handle_smalltalk = AsyncMock(
            return_value=[{"type": "text", "data": {"text": "错误的通用回退"}}]
        )
        mock_plugin_registry.get.side_effect = lambda name: {
            "xiaoqing_chat": xiaoqing,
            "smalltalk": generic,
        }.get(name)

        result = await dispatcher.handle_event(event_template(message="普通群聊消息"))

        assert result and result[0]["data"]["text"] == "接一句"
        xiaoqing.module.handle_smalltalk.assert_awaited_once()

        # 有状态的人格插件故障时宁可静默，不能让无状态 provider 冒充小青接话。
        xiaoqing.module.handle_smalltalk = AsyncMock(side_effect=RuntimeError("provider failed"))
        result = await dispatcher.handle_event(event_template(message="另一条普通群聊消息"))

        assert result == []
        generic.module.handle_smalltalk.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unknown_command_hint_only_for_slash_prefix(
        self, dispatcher, mock_router, mock_plugin_registry, event_template
    ):
        mock_router.resolve.return_value  = None
        smalltalk                         = MagicMock()
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
        mock_router.resolve.return_value  = None
        smalltalk                         = MagicMock()
        smalltalk.module.handle_smalltalk = AsyncMock(
            return_value=[{"type": "text", "data": {"text": "嗯"}}]
        )
        mock_plugin_registry.get.return_value = smalltalk
        event                                 = event_template(
            message=[
                {"type": "at", "data": {"qq": "11111"}},
                {"type": "text", "data": {"text": " 帮我看看"}},
            ]
        )
        event["raw_message"] = "[CQ:at,qq=11111] 帮我看看"
        result               = await dispatcher.handle_event(event)
        assert result and result[0]["data"]["text"] == "嗯"

    @pytest.mark.asyncio
    async def test_group_at_only_calls_bot_name_only(
        self, dispatcher, mock_plugin_registry, event_template
    ):
        smalltalk                           = MagicMock()
        smalltalk.module.call_bot_name_only = AsyncMock(
            return_value=[{"type": "text", "data": {"text": "在的"}}]
        )
        mock_plugin_registry.get.return_value = smalltalk
        event = event_template(message=[{"type": "at", "data": {"qq": "11111"}}])
        event["raw_message"] = "[CQ:at,qq=11111]"
        result               = await dispatcher.handle_event(event)
        assert result and result[0]["data"]["text"] == "在的"

    @pytest.mark.asyncio
    async def test_private_only_bot_name_calls_bot_name_only(
        self, dispatcher, mock_plugin_registry, event_template
    ):
        smalltalk                           = MagicMock()
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
        session                          = MagicMock()
        session.plugin_name              = "pendo"
        mock_session_manager.get = AsyncMock(return_value=session)

        pendo                       = MagicMock()
        pendo.module.handle_session = AsyncMock(
            return_value=[{"type": "text", "data": {"text": "session reply"}}]
        )
        mock_plugin_registry.get.return_value = pendo

        result = await dispatcher.handle_event(event_template(message="第三个选项"))
        assert result and result[0]["data"]["text"] == "session reply"

    @pytest.mark.asyncio
    async def test_session_routing_probes_with_peek_without_refreshing_get(
        self,
        dispatcher,
        mock_router,
        mock_plugin_registry,
        mock_session_manager,
        event_template,
    ):
        mock_router.resolve.return_value = None
        session = SimpleNamespace(plugin_name="pendo", session_id="pendo-session-1")
        mock_session_manager.get = AsyncMock(
            side_effect=AssertionError("routing probes must not touch the session lease")
        )
        mock_session_manager.peek.side_effect  = None
        mock_session_manager.peek.return_value = session

        async def update(_user_id, _group_id, callback):
            result = callback(session)
            return await result if inspect.isawaitable(result) else result

        mock_session_manager.update.side_effect = update
        loaded                                  = SimpleNamespace(
            module=SimpleNamespace(
                handle_session=AsyncMock(
                    return_value=[{"type": "text", "data": {"text": "session reply"}}]
                )
            ),
            execution_gate=None,
        )
        mock_plugin_registry.get.return_value = loaded

        result = await dispatcher.handle_event(event_template(message="继续"))

        assert result == [{"type": "text", "data": {"text": "session reply"}}]
        assert mock_session_manager.peek.await_count >= 2
        mock_session_manager.get.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_active_session_preempts_resolved_global_command(
        self,
        dispatcher,
        mock_router,
        mock_plugin_registry,
        mock_session_manager,
        event_template,
    ):
        session = SimpleNamespace(plugin_name="qingssh", session_id="ssh-session-1")
        mock_session_manager.get.return_value = session
        global_handler = AsyncMock(return_value=[{"type": "text", "data": {"text": "global"}}])
        mock_router.resolve.return_value = (
            CommandSpec(
                plugin     = "echo",
                name       = "echo",
                triggers   = ["echo"],
                help_text  = "echo",
                handler    = global_handler,
                admin_only = False,
            ),
            "from-session",
        )
        session_handler = AsyncMock(return_value=[{"type": "text", "data": {"text": "session"}}])
        mock_plugin_registry.get.return_value = SimpleNamespace(
            module=SimpleNamespace(handle_session=session_handler),
            execution_gate=None,
        )

        result = await dispatcher.handle_event(event_template(message="echo from-session"))

        assert result == [{"type": "text", "data": {"text": "session"}}]
        session_handler.assert_awaited_once()
        global_handler.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_blank_input_reaches_active_session_but_not_smalltalk(
        self,
        dispatcher,
        mock_router,
        mock_plugin_registry,
        mock_session_manager,
        event_template,
    ):
        session = SimpleNamespace(plugin_name="qingssh", session_id="ssh-session-blank")
        mock_session_manager.get.return_value = session
        mock_router.resolve.return_value      = None
        session_handler                       = AsyncMock(
            return_value=[{"type": "text", "data": {"text": "default accepted"}}]
        )
        mock_plugin_registry.get.return_value = SimpleNamespace(
            module=SimpleNamespace(handle_session=session_handler),
            execution_gate=None,
        )
        event = event_template(message_type="private", message=" ")
        event.pop("group_id")

        result = await dispatcher.handle_event(event)

        assert result == [{"type": "text", "data": {"text": "default accepted"}}]
        session_handler.assert_awaited_once()

        mock_session_manager.get.return_value  = None
        mock_session_manager.peek.return_value = None
        mock_session_manager.peek.side_effect  = None
        result                                 = await dispatcher.handle_event(event)
        assert result == []

    @pytest.mark.asyncio
    async def test_group_only_bot_name_with_active_session_does_not_preempt(
        self, dispatcher, mock_plugin_registry, mock_session_manager, event_template
    ):
        session             = MagicMock()
        session.plugin_name = "pendo"
        mock_session_manager.get = AsyncMock(return_value=session)

        smalltalk                           = MagicMock()
        smalltalk.module.call_bot_name_only = AsyncMock(
            return_value=[{"type": "text", "data": {"text": "在的"}}]
        )
        pendo                       = MagicMock()
        pendo.module.handle_session = AsyncMock(
            return_value=[{"type": "text", "data": {"text": "WRONG"}}]
        )

        def _get(name):
            return {"pendo": pendo}.get(name, smalltalk)

        mock_plugin_registry.get.side_effect = _get

        result = await dispatcher.handle_event(event_template(message="小青"))
        assert result and result[0]["data"]["text"] == "在的"
        pendo.module.handle_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_command_uses_loaded_plugin_sequential_gate(
        self, dispatcher, mock_router, mock_plugin_registry, event_template
    ):
        gate       = PluginExecutionGate("sequential")
        entered    = asyncio.Event()
        release    = asyncio.Event()
        active     = 0
        max_active = 0

        async def slow_handler(*_args):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            entered.set()
            await release.wait()
            active -= 1
            return [{"type": "text", "data": {"text": "ok"}}]

        spec = CommandSpec(
            plugin         = "stateful",
            name           = "work",
            triggers       = ["work"],
            help_text      = "work",
            handler        = slow_handler,
            admin_only     = False,
            execution_gate = gate,
        )
        mock_router.resolve.return_value = (spec, "")
        loaded = SimpleNamespace(execution_gate=gate)
        mock_plugin_registry.get.side_effect = lambda name: loaded if name == "stateful" else None

        first = asyncio.create_task(dispatcher.handle_event(event_template(message="/work")))
        await entered.wait()
        second = asyncio.create_task(dispatcher.handle_event(event_template(message="/work")))
        await asyncio.sleep(0)
        assert max_active == 1

        release.set()
        assert await first
        assert await second
        assert max_active == 1

    @pytest.mark.asyncio
    async def test_provider_events_use_the_same_sequential_gate(
        self, dispatcher, mock_plugin_registry, sample_message_context
    ):
        gate    = PluginExecutionGate("sequential")
        entered = asyncio.Event()
        release = asyncio.Event()
        probe   = BlockingConcurrencyProbe(entered, release)

        mock_plugin_registry.get.return_value = SimpleNamespace(
            module=SimpleNamespace(observe_message=probe.run),
            execution_gate=gate,
        )

        first = asyncio.create_task(
            dispatcher._call_provider(
                "stateful",
                "observe_message",
                sample_message_context,
                ("one", {}),
            )
        )
        await entered.wait()
        second = asyncio.create_task(
            dispatcher._call_provider(
                "stateful",
                "observe_message",
                sample_message_context,
                ("two", {}),
            )
        )
        await asyncio.sleep(0)
        assert probe.maximum_active == 1

        release.set()
        await asyncio.gather(first, second)
        assert probe.maximum_active == 1

    @pytest.mark.asyncio
    async def test_provider_failure_logs_only_correlated_redacted_diagnostics(
        self,
        dispatcher,
        mock_plugin_registry,
        sample_message_context,
        caplog,
    ):
        canary = "CR219_PROVIDER_SECRET"

        async def observe_message(*_args):
            raise RuntimeError(
                f"Authorization: Bearer {canary} "
                f"https://user:password@example.test/{canary} "
                rf"C:\private\{canary}.txt"
            )

        mock_plugin_registry.get.return_value = SimpleNamespace(
            module=SimpleNamespace(observe_message=observe_message),
            execution_gate=PluginExecutionGate("parallel"),
        )

        with caplog.at_level(logging.ERROR):
            result = await dispatcher._call_provider(
                "stateful",
                "observe_message",
                sample_message_context,
                ("hello", {}),
            )

        log_text = "\n".join(record.getMessage() for record in caplog.records)
        assert result is None
        assert "XQ-PLUGIN-UNEXPECTED" in log_text
        assert canary not in log_text
        assert "user:password" not in log_text
        assert "C:\\private" not in log_text
        assert all(record.exc_info is None for record in caplog.records)
