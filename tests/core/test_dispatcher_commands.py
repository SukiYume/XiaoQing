"""禁言、命令执行和会话错误。"""

from __future__ import annotations

import tests.helpers.dispatcher_test_support as _fixture_support
from core.router import CommandCatalogNode
from tests.helpers.dispatcher_test_support import (
    AsyncMock,
    CommandSpec,
    Dispatcher,
    MagicMock,
    MessageContext,
    Mock,
    PluginExecutionGate,
    SimpleNamespace,
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


class TestDispatcherMuteControl:
    """Dispatcher 静音控制测试"""

    def test_mute_group(self, dispatcher: Dispatcher):
        """测试静音群"""
        dispatcher.mute_group(12345, 10.0)
        assert dispatcher.is_muted(12345) is True

    @pytest.mark.parametrize("group_id", [0, -1, True, "12345"])
    def test_mute_group_rejects_invalid_group_id(self, dispatcher: Dispatcher, group_id):
        with pytest.raises(ValueError, match="group_id"):
            dispatcher.mute_group(group_id, 10.0)

    @pytest.mark.parametrize(
        "duration",
        [0, -1, True, float("nan"), float("inf"), float("-inf")],
    )
    def test_mute_group_rejects_invalid_duration(self, dispatcher: Dispatcher, duration):
        with pytest.raises(ValueError, match="positive finite"):
            dispatcher.mute_group(12345, duration)

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

    def test_mute_check_prunes_expired_entries_from_other_groups(self, dispatcher: Dispatcher):
        """访问任意群时都回收历史过期项，而不是等待原群再次出现。"""

        now = dispatcher.clock.now()
        dispatcher._muted_groups.update({111: now - 1, 222: now + 60})

        assert dispatcher.is_muted(222) is True
        assert dispatcher._muted_groups == {222: now + 60}


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

        handler = AsyncMock()
        spec    = CommandSpec(
            plugin     = "admin",
            name       = "reload",
            triggers   = ["reload"],
            help_text  = "重载",
            admin_only = True,
            handler    = handler,
            priority   = 0,
        )

        result = await dispatcher._execute_command((spec, ""), sample_message_context)
        assert result is not None
        assert result[0]["data"]["text"] == "权限不足"
        handler.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("replacement_loaded", [False, True])
    async def test_stale_resolved_command_never_runs_after_its_generation_closes(
        self,
        dispatcher: Dispatcher,
        sample_message_context: MessageContext,
        mock_plugin_registry: MagicMock,
        replacement_loaded: bool,
    ) -> None:
        """A resolved command is admitted only through the generation that registered it."""

        old_gate = PluginExecutionGate("parallel", plugin_name="stateful")
        new_gate = PluginExecutionGate("parallel", plugin_name="stateful")
        old_handler = AsyncMock(return_value=[{"type": "text", "data": {"text": "stale"}}])
        old_spec = CommandSpec(
            plugin         = "stateful",
            name           = "work",
            triggers       = ["work"],
            help_text      = "work",
            admin_only     = False,
            handler        = old_handler,
            execution_gate = old_gate,
        )

        # Model the exact race: routing retained the old spec, then unload or
        # reload closed that generation before command admission.
        await old_gate.close()
        mock_plugin_registry.get.return_value = (
            SimpleNamespace(execution_gate=new_gate) if replacement_loaded else None
        )

        result = await dispatcher._execute_command(
            (old_spec, ""),
            sample_message_context,
        )

        assert result == [{"type": "text", "data": {"text": "⚠️ 插件暂时不可用，请稍后重试"}}]
        old_handler.assert_not_awaited()
        assert new_gate.drained is True

    @pytest.mark.asyncio
    async def test_unexpected_command_error_uses_redacted_public_response(
        self,
        dispatcher: Dispatcher,
        sample_message_context: MessageContext,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        canary  = "CR219_DISPATCHER_SECRET"
        context = SimpleNamespace(
            request_id = sample_message_context.request_id,
            secrets    = {"token": canary},
        )
        dispatcher.build_context = Mock(return_value=context)
        handler = AsyncMock(
            side_effect=RuntimeError(f"Authorization: Bearer {canary} C:\\private\\{canary}.txt")
        )
        spec = CommandSpec(
            plugin     = "public_demo",
            name       = "demo",
            triggers   = ["demo"],
            help_text  = "demo",
            admin_only = False,
            handler    = handler,
        )

        with caplog.at_level("ERROR"):
            result = await dispatcher._execute_command((spec, ""), sample_message_context)

        assert result is not None
        response_text = result[0]["data"]["text"]
        log_text      = "\n".join(record.getMessage() for record in caplog.records)
        assert "XQ-PLUGIN-UNEXPECTED" in response_text
        assert sample_message_context.request_id in response_text
        assert sample_message_context.request_id in log_text
        assert "RuntimeError" in log_text
        assert canary not in response_text
        assert canary not in log_text
        assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.asyncio
async def test_unexpected_session_error_is_redacted_and_closes_session(
    dispatcher: Dispatcher,
    sample_message_context: MessageContext,
    mock_plugin_registry: MagicMock,
    mock_session_manager: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    canary = "CR219_SESSION_SECRET"
    session = SimpleNamespace(plugin_name="public_session", session_id="public-session-1")
    mock_session_manager.get.return_value = session
    mock_session_manager.delete = AsyncMock(return_value=True)
    plugin = SimpleNamespace(
        module=SimpleNamespace(
            handle_session=AsyncMock(
                side_effect=RuntimeError(f"https://user:{canary}@example.test/?token={canary}")
            )
        ),
        execution_gate=None,
    )
    mock_plugin_registry.get.return_value = plugin
    context                               = SimpleNamespace(
        request_id = sample_message_context.request_id,
        secrets    = {"password": canary},
    )
    dispatcher.build_context = Mock(return_value=context)

    with caplog.at_level("ERROR"):
        result = await dispatcher._try_handle_session(sample_message_context)

    assert result is not None
    response_text = result[0]["data"]["text"]
    log_text      = "\n".join(record.getMessage() for record in caplog.records)
    assert "XQ-PLUGIN-UNEXPECTED" in response_text
    assert sample_message_context.request_id in response_text
    assert canary not in response_text
    assert canary not in log_text
    mock_session_manager.delete.assert_awaited_once_with(
        sample_message_context.user_id,
        sample_message_context.group_id,
    )


@pytest.mark.asyncio
async def test_session_continuation_obeys_published_command_contexts(
    dispatcher: Dispatcher,
    sample_message_context: MessageContext,
    mock_plugin_registry: MagicMock,
    mock_router: MagicMock,
    mock_session_manager: MagicMock,
) -> None:
    session = SimpleNamespace(plugin_name="private_tool", session_id="session-1")
    mock_session_manager.get.return_value = session
    mock_session_manager.delete = AsyncMock(return_value=True)
    close_session = AsyncMock()
    handle_session = AsyncMock(return_value=[{"type": "text", "data": {"text": "secret"}}])
    mock_plugin_registry.get.return_value = SimpleNamespace(
        module=SimpleNamespace(close_session=close_session, handle_session=handle_session),
        execution_gate=None,
    )
    mock_router.get_command_catalog.return_value = (
        CommandCatalogNode(
            code      = "private_tool.run",
            plugin    = "private_tool",
            path      = ("run",),
            name      = "run",
            aliases   = (),
            help_text = "run",
            usage     = "/run",
            contexts  = ("private",),
        ),
    )

    result = await dispatcher._try_handle_session(sample_message_context)

    assert result == [{"type": "text", "data": {"text": "当前会话类型不再受支持，会话已关闭"}}]
    close_session.assert_awaited_once()
    mock_session_manager.delete.assert_awaited_once_with(
        sample_message_context.user_id,
        sample_message_context.group_id,
    )
    handle_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_admin_session_recheck_uses_manifest_capability_not_plugin_name(
    dispatcher: Dispatcher,
    sample_message_context: MessageContext,
    mock_plugin_registry: MagicMock,
    mock_router: MagicMock,
    mock_session_manager: MagicMock,
    mock_admin_check: MagicMock,
) -> None:
    plugin_name = "declared_privileged_tool"
    session = SimpleNamespace(plugin_name=plugin_name, session_id="session-privileged")
    mock_session_manager.get.return_value = session
    mock_session_manager.delete = AsyncMock(return_value=True)
    mock_admin_check.is_admin.return_value           = False
    mock_plugin_registry.has_capability.return_value = True
    close_session                                    = AsyncMock()
    handle_session = AsyncMock(return_value=[{"type": "text", "data": {"text": "secret"}}])
    mock_plugin_registry.get.return_value = SimpleNamespace(
        module=SimpleNamespace(close_session=close_session, handle_session=handle_session),
        execution_gate=None,
    )
    mock_router.get_command_catalog.return_value = (
        CommandCatalogNode(
            code      = f"{plugin_name}.run",
            plugin    = plugin_name,
            path      = ("run",),
            name      = "run",
            aliases   = (),
            help_text = "run",
            usage     = "/run",
            contexts  = ("group",),
        ),
    )

    result = await dispatcher._try_handle_session(sample_message_context)

    assert result == [{"type": "text", "data": {"text": "权限已变更，高权限会话已关闭"}}]
    mock_plugin_registry.has_capability.assert_called_with(plugin_name, "admin_sessions")
    close_session.assert_awaited_once()
    handle_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_admin_plugin_name_without_manifest_capability_does_not_change_session_policy(
    dispatcher: Dispatcher,
    sample_message_context: MessageContext,
    mock_plugin_registry: MagicMock,
    mock_router: MagicMock,
    mock_session_manager: MagicMock,
    mock_admin_check: MagicMock,
) -> None:
    plugin_name = "qingssh"
    session = SimpleNamespace(plugin_name=plugin_name, session_id="session-name-only")
    mock_session_manager.get.return_value            = session
    mock_admin_check.is_admin.return_value           = False
    mock_plugin_registry.has_capability.return_value = False
    handle_session = AsyncMock(return_value=[{"type": "text", "data": {"text": "continued"}}])
    mock_plugin_registry.get.return_value = SimpleNamespace(
        module=SimpleNamespace(handle_session=handle_session),
        execution_gate=None,
    )
    mock_router.get_command_catalog.return_value = (
        CommandCatalogNode(
            code      = f"{plugin_name}.run",
            plugin    = plugin_name,
            path      = ("run",),
            name      = "run",
            aliases   = (),
            help_text = "run",
            usage     = "/run",
            contexts  = ("group",),
        ),
    )

    result = await dispatcher._try_handle_session(sample_message_context)

    assert result == [{"type": "text", "data": {"text": "continued"}}]
    handle_session.assert_awaited_once()
