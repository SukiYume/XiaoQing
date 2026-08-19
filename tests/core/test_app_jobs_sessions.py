"""插件重载、计划任务和会话。"""

from __future__ import annotations

import tests.helpers.app_test_support as _fixture_support
from core.delivery import ScheduledDelivery
from tests.helpers.app_test_support import (
    AsyncMock,
    BlockingConcurrencyProbe,
    DeliveryReceipt,
    DeliverySegments,
    LoadedPlugin,
    MagicMock,
    Mock,
    Path,
    PluginDefinition,
    SimpleNamespace,
    XiaoQingApp,
    _plugin_context_for,
    _set_app_config,
    asyncio,
    current_action_sink,
    patch,
    pytest,
)

mock_dependencies = _fixture_support.mock_dependencies
temp_app_root = _fixture_support.temp_app_root


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_reload_plugins_async(temp_app_root: Path):
    """A full reload transactionally reloads each generation, then reconciles disk."""
    app = XiaoQingApp(temp_app_root)

    app.plugin_manager.reload_all_plugins = AsyncMock(return_value=True)
    app.session_manager.clear_plugin_sessions = AsyncMock()

    assert await app._reload_plugins_async_with_logging() is True

    app.plugin_manager.reload_all_plugins.assert_awaited_once_with(
        before_reload=app.session_manager.clear_plugin_sessions,
    )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_reload_stops_when_a_plugin_enters_quarantine(temp_app_root: Path):
    app = XiaoQingApp(temp_app_root)
    app.plugin_manager.reload_all_plugins = AsyncMock(return_value=False)
    app.session_manager.clear_plugin_sessions = AsyncMock()

    assert await app._reload_plugins_async_with_logging() is False

    app.plugin_manager.reload_all_plugins.assert_awaited_once_with(
        before_reload=app.session_manager.clear_plugin_sessions,
    )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_reload_failure_returns_false(temp_app_root: Path):
    """普通重载异常写入日志，并通过任务结果通知管理命令失败。"""

    app = XiaoQingApp(temp_app_root)
    app.plugin_manager.reload_all_plugins = AsyncMock(side_effect=RuntimeError("reload failed"))
    app.session_manager.clear_plugin_sessions = AsyncMock()

    assert await app._reload_plugins_async_with_logging() is False


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_reload_plugins_non_blocking(temp_app_root: Path):
    """Test _reload_plugins creates background task"""
    app = XiaoQingApp(temp_app_root)

    app.plugin_manager.reload_all_plugins = AsyncMock(return_value=True)
    app.session_manager.clear_plugin_sessions = AsyncMock()

    app._reload_plugins()

    # Should create a task
    assert app._reload_task is not None

    # Wait for it to finish to avoid pending task warning
    if app._reload_task:
        await app._reload_task


@pytest.mark.unit
def test_app_reload_plugins_already_in_progress(temp_app_root: Path):
    """Test _reload_plugins when already in progress"""
    app = XiaoQingApp(temp_app_root)

    # Create a mock "in progress" task
    app._reload_task = MagicMock()
    app._reload_task.done = Mock(return_value=False)

    # Mock plugin manager
    app.plugin_manager.unload_plugin = AsyncMock()

    app._reload_plugins()

    # Should not create new task
    assert app._reload_task.done.called is False or app.plugin_manager.unload_plugin.call_count == 0


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_run_job(temp_app_root: Path):
    """Test _run_job executes scheduled job"""
    app = XiaoQingApp(temp_app_root)

    # Create a mock handler
    async def mock_handler(context):
        return [{"type": "text", "data": {"text": "scheduled result"}}]

    # Mock context building
    app.plugin_manager.build_context = Mock(return_value=MagicMock())
    mock_context = app.plugin_manager.build_context.return_value
    mock_context.default_groups = Mock(return_value=[123, 456])

    # Mock http_sender
    app.http_sender = AsyncMock()

    await app._run_job(mock_handler, "test_plugin", [123, 456])

    # Verify context was built
    app.plugin_manager.build_context.assert_called_once()
    call = app.plugin_manager.build_context.call_args
    assert call.args == ("test_plugin",)
    assert call.kwargs["principal"].kind == "scheduled_system"
    assert call.kwargs["principal"].schedule_delivery == "broadcast"


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.parametrize(
    ("manifest_groups", "expected_groups"),
    [
        ([101, 202], [101, 202]),
        (None, [303]),
        ([], []),
    ],
)
async def test_app_run_job_exposes_core_resolved_groups_to_self_delivering_plugins(
    temp_app_root: Path,
    manifest_groups: list[int] | None,
    expected_groups: list[int],
) -> None:
    """自行确认投递结果的插件也只能使用 Core 解析后的调度目标。"""

    app = XiaoQingApp(temp_app_root)
    _set_app_config(app, default_group_ids=[303])
    app._send_action = AsyncMock(return_value=True)

    def build_context(plugin_name: str, *, principal):
        return _plugin_context_for(
            app,
            plugin_name,
            principal=principal,
        )

    app.plugin_manager.build_context = Mock(side_effect=build_context)

    async def self_delivering_handler(context):
        for group_id in context.default_groups():
            await context.send_action(
                {
                    "action": "send_group_msg",
                    "params": {"group_id": group_id, "message": []},
                }
            )
        return []

    await app._run_job(
        self_delivering_handler,
        "test_plugin",
        manifest_groups,
        loaded_plugin=SimpleNamespace(execution_gate=None),
    )

    sent_groups = [call.args[0]["params"]["group_id"] for call in app._send_action.await_args_list]
    assert sent_groups == expected_groups

    principal = app.plugin_manager.build_context.call_args.kwargs["principal"]
    assert [target.group_id for target in principal.delivery_targets] == expected_groups


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_run_job_rejects_invalid_group_ids_before_plugin_execution(temp_app_root: Path):
    app = XiaoQingApp(temp_app_root)
    _set_app_config(app, default_group_ids=[123, "invalid"])
    handler = AsyncMock()
    app.plugin_manager.build_context = Mock()

    await app._run_job(handler, "test_plugin")

    handler.assert_not_awaited()
    app.plugin_manager.build_context.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_silent_schedule_runs_without_any_delivery_capability(temp_app_root: Path):
    app = XiaoQingApp(temp_app_root)
    _set_app_config(app, default_group_ids=[123, "invalid"])
    app._send_action = AsyncMock(return_value=True)
    observed: dict[str, object] = {}

    def build_context(plugin_name: str, *, principal):
        observed["principal"] = principal
        return _plugin_context_for(app, plugin_name, principal=principal)

    app.plugin_manager.build_context = Mock(side_effect=build_context)

    async def handler(context):
        observed["active_send"] = await context.send_action(
            {
                "action": "send_group_msg",
                "params": {"group_id": 123, "message": []},
            }
        )
        return [{"type": "text", "data": {"text": "must stay silent"}}]

    await app._run_job(
        handler,
        "silent_plugin",
        delivery="silent",
        loaded_plugin=SimpleNamespace(execution_gate=None),
    )

    principal = observed["principal"]
    assert principal.schedule_delivery == "silent"
    assert principal.delivery_targets == ()
    assert observed["active_send"] is False
    app._send_action.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_targeted_schedule_validates_returned_and_active_targets(temp_app_root: Path):
    app = XiaoQingApp(temp_app_root)
    app._send_action = AsyncMock(return_value=True)

    def build_context(plugin_name: str, *, principal):
        return _plugin_context_for(app, plugin_name, principal=principal)

    app.plugin_manager.build_context = Mock(side_effect=build_context)
    active_outcomes: list[bool | None] = []

    async def handler(context):
        for action in (
            {"action": "send_group_msg", "params": {"group_id": 101, "message": []}},
            {"action": "send_group_msg", "params": {"group_id": 999, "message": []}},
            {"action": "send_private_msg", "params": {"user_id": 42, "message": []}},
        ):
            active_outcomes.append(await context.send_action(action))
        return [
            ScheduledDelivery.group(
                101,
                [{"type": "text", "data": {"text": "allowed group"}}],
            ),
            ScheduledDelivery.group(
                999,
                [{"type": "text", "data": {"text": "blocked group"}}],
            ),
            ScheduledDelivery.private(
                42,
                [{"type": "text", "data": {"text": "allowed private"}}],
            ),
        ]

    await app._run_job(
        handler,
        "targeted_plugin",
        [101],
        delivery="targeted",
        loaded_plugin=SimpleNamespace(execution_gate=None),
    )

    assert active_outcomes == [True, False, True]
    actions = [call.args[0] for call in app._send_action.await_args_list]
    assert [
        (action["action"], action["params"].get("group_id"), action["params"].get("user_id"))
        for action in actions
    ] == [
        ("send_group_msg", 101, None),
        ("send_private_msg", None, 42),
        ("send_group_msg", 101, None),
        ("send_private_msg", None, 42),
    ]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_scheduled_delivery_receipt_rolls_back_on_partial_group_failure(
    temp_app_root: Path,
):
    app = XiaoQingApp(temp_app_root)
    committed = Mock()
    rolled_back = Mock()
    receipt = DeliveryReceipt(
        expected_actions=1,
        commit=committed,
        rollback=rolled_back,
        unknown=rolled_back,
    )

    async def handler(_context):
        return DeliverySegments(
            [{"type": "text", "data": {"text": "scheduled result"}}],
            receipt,
        )

    app.plugin_manager.build_context = Mock(return_value=MagicMock())
    app._send_single_action = AsyncMock(side_effect=[True, False])

    await app._run_job(handler, "test_plugin", [123, 456])

    assert receipt.resolved is True
    assert receipt.committed is False
    committed.assert_not_called()
    rolled_back.assert_called_once_with()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_run_job_uses_plugin_sequential_gate(temp_app_root: Path):
    """Scheduled calls share the same manifest gate as message handlers."""
    from core.plugin_execution import PluginExecutionGate

    app = XiaoQingApp(temp_app_root)
    gate = PluginExecutionGate("sequential")
    loaded = SimpleNamespace(execution_gate=gate)
    entered = asyncio.Event()
    release = asyncio.Event()
    probe = BlockingConcurrencyProbe(entered, release)

    app.plugin_manager.get = Mock(return_value=loaded)
    app.plugin_manager.build_context = Mock(return_value=MagicMock())

    first = asyncio.create_task(app._run_job(probe.run, "stateful"))
    await entered.wait()
    second = asyncio.create_task(app._run_job(probe.run, "stateful"))
    await asyncio.sleep(0)
    assert probe.maximum_active == 1

    release.set()
    await asyncio.gather(first, second)
    assert probe.maximum_active == 1


@pytest.mark.asyncio
@pytest.mark.unit
async def test_send_action_resolves_declared_observer_for_external_plugin_text(
    temp_app_root: Path,
):
    app = XiaoQingApp(temp_app_root)
    observer = AsyncMock(return_value=[])
    loaded = SimpleNamespace()
    service = SimpleNamespace(owner="observer_plugin", callback=observer)
    xiaoqing_context = MagicMock()
    app.plugin_manager.resolve_service = Mock(return_value=(loaded, service))
    app.plugin_manager.build_context = Mock(return_value=xiaoqing_context)
    app._send_single_action = AsyncMock(return_value=True)

    action = {
        "action": "send_group_msg",
        "params": {
            "group_id": 123,
            "message": [{"type": "text", "data": {"text": "地震播报"}}],
        },
        "_source_plugin": "earthquake",
    }

    await app._send_action(action)

    app.plugin_manager.resolve_service.assert_called_once_with(
        caller_plugin="core",
        service_name="core.observe_outgoing_action",
    )
    app.plugin_manager.build_context.assert_called_once()
    build_call = app.plugin_manager.build_context.call_args
    assert build_call.args == ("observer_plugin",)
    assert build_call.kwargs["user_id"] is None
    assert build_call.kwargs["group_id"] == 123
    assert build_call.kwargs["principal"].kind == "lifecycle"
    assert build_call.kwargs["principal"].group_id is None
    observer.assert_awaited_once_with(
        action,
        xiaoqing_context,
        source_plugin="earthquake",
    )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_send_action_does_not_notify_xiaoqing_for_xiaoqing_source(temp_app_root: Path):
    app = XiaoQingApp(temp_app_root)
    app.plugin_manager.resolve_service = Mock()

    await app._send_action(
        {
            "action": "send_group_msg",
            "params": {
                "group_id": 123,
                "message": [{"type": "text", "data": {"text": "小青回复"}}],
            },
            "_source_plugin": "xiaoqing_chat",
        }
    )

    app.plugin_manager.resolve_service.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_run_job_no_result(temp_app_root: Path):
    """Test _run_job with handler returning no result"""
    app = XiaoQingApp(temp_app_root)

    # Create a mock handler returning None
    async def mock_handler(context):
        return None

    context = MagicMock()
    app.plugin_manager.build_context = Mock(return_value=context)

    await app._run_job(mock_handler, "test_plugin")

    app.plugin_manager.build_context.assert_called_once()
    assert app.plugin_manager.build_context.call_args.args[0] == "test_plugin"
    assert context.send_action.call_count == 0


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_run_job_with_error(temp_app_root: Path):
    """Test _run_job handles handler errors"""
    app = XiaoQingApp(temp_app_root)

    # Create a mock handler that raises
    async def mock_handler(context):
        raise ValueError("Test error")

    app.plugin_manager.build_context = Mock(return_value=MagicMock())

    # Should not raise
    await app._run_job(mock_handler, "test_plugin")


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_run_job_swallows_cancelled_error(temp_app_root: Path):
    """Test _run_job treats cancellation as normal shutdown."""
    app = XiaoQingApp(temp_app_root)

    async def mock_handler(context):
        raise asyncio.CancelledError()

    app.plugin_manager.build_context = Mock(return_value=MagicMock())

    await app._run_job(mock_handler, "test_plugin")


@pytest.mark.unit
def test_app_reschedule_startup(temp_app_root: Path):
    """Test _reschedule with startup event"""
    app = XiaoQingApp(temp_app_root)

    # Mock scheduler and plugin manager
    app.scheduler.replace_prefix = Mock()
    app.plugin_manager.schedule_definitions = Mock(return_value=[])

    app._reschedule("startup")

    app.scheduler.replace_prefix.assert_called_once_with("plugin.", [])
    app.plugin_manager.schedule_definitions.assert_called_once()


@pytest.mark.unit
def test_app_reschedule_single_plugin(temp_app_root: Path):
    """Test _reschedule for single plugin"""
    app = XiaoQingApp(temp_app_root)

    # Create mock loaded plugin
    mock_plugin = MagicMock()
    mock_plugin.definition.name = "test_plugin"
    mock_plugin.definition.schedule = []

    # Mock scheduler and plugin manager
    app.scheduler.replace_prefix = Mock()
    app.plugin_manager.get = Mock(return_value=mock_plugin)

    app._reschedule("test_plugin")

    app.scheduler.replace_prefix.assert_called_once_with("plugin.test_plugin.", [])


@pytest.mark.unit
def test_app_reschedule_skips_manifest_disabled_schedule(temp_app_root: Path):
    app = XiaoQingApp(temp_app_root)
    module = MagicMock()
    definition = PluginDefinition(
        name="test_plugin",
        version="1.0.0",
        entry="main.py",
        commands=[],
        schedule=[
            {
                "id": "disabled",
                "handler": "scheduled",
                "cron": {"minute": "*"},
                "enabled": False,
            }
        ],
        concurrency="parallel",
    )
    loaded = LoadedPlugin(definition=definition, module=module, mtime=0.0)
    app.scheduler.replace_prefix = Mock()
    app.plugin_manager.schedule_definitions = Mock(return_value=[loaded])

    app._reschedule("startup")

    app.scheduler.replace_prefix.assert_called_once_with("plugin.", [])


@pytest.mark.unit
def test_app_reschedule_preserves_manifest_schedule_description(temp_app_root: Path):
    app = XiaoQingApp(temp_app_root)
    module = MagicMock()
    module.scheduled = AsyncMock()
    definition = PluginDefinition(
        name="test_plugin",
        version="1.0.0",
        entry="main.py",
        commands=[],
        schedule=[
            {
                "id": "enabled",
                "handler": "scheduled",
                "cron": {"minute": "*"},
                "description": "visible scheduler metadata",
                "enabled": True,
            }
        ],
        concurrency="parallel",
    )
    loaded = LoadedPlugin(definition=definition, module=module, mtime=0.0)
    app.scheduler.replace_prefix = Mock()
    app.plugin_manager.schedule_definitions = Mock(return_value=[loaded])

    app._reschedule("startup")

    app.scheduler.replace_prefix.assert_called_once()
    prefix, jobs = app.scheduler.replace_prefix.call_args.args
    assert prefix == "plugin."
    assert len(jobs) == 1
    assert jobs[0].job_id == "plugin.test_plugin.enabled"
    assert jobs[0].description == "visible scheduler metadata"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_manifest_schedule_group_ids_reach_plugin_context(temp_app_root: Path) -> None:
    """Manifest 目标由 Core 解析，并传入实际到期执行的插件上下文。"""

    app = XiaoQingApp(temp_app_root)
    _set_app_config(app, default_group_ids=[303])
    observed_groups: list[int] = []

    async def scheduled(context):
        observed_groups.extend(context.default_groups())
        return []

    definition = PluginDefinition(
        name="test_plugin",
        version="1.0.0",
        entry="main.py",
        commands=[],
        schedule=[
            {
                "id": "targeted",
                "handler": "scheduled",
                "cron": {"minute": "*"},
                "delivery": "targeted",
                "group_ids": [101, 202],
            }
        ],
        concurrency="parallel",
    )
    loaded = LoadedPlugin(
        definition=definition,
        module=SimpleNamespace(scheduled=scheduled),
        mtime=0.0,
    )
    app.scheduler.replace_prefix = Mock()
    app.plugin_manager.schedule_definitions = Mock(return_value=[loaded])
    app.plugin_manager.build_context = Mock(
        side_effect=lambda plugin_name, *, principal: _plugin_context_for(
            app,
            plugin_name,
            principal=principal,
        )
    )

    app._reschedule("startup")
    _prefix, jobs = app.scheduler.replace_prefix.call_args.args
    await jobs[0].func()

    assert observed_groups == [101, 202]
    principal = app.plugin_manager.build_context.call_args.kwargs["principal"]
    assert principal.schedule_delivery == "targeted"


@pytest.mark.unit
def test_app_reschedule_validates_every_manifest_before_replacement(temp_app_root: Path):
    app = XiaoQingApp(temp_app_root)
    module = SimpleNamespace(scheduled=AsyncMock())
    definition = PluginDefinition(
        name="test_plugin",
        version="1.0.0",
        entry="main.py",
        commands=[],
        schedule=[
            {"id": "valid", "handler": "scheduled", "cron": {"minute": "*"}},
            {
                "id": "invalid",
                "handler": "scheduled",
                "cron": {"minute": "*"},
                "group_ids": [0],
            },
        ],
        concurrency="parallel",
    )
    app.scheduler.replace_prefix = Mock()
    app.plugin_manager.schedule_definitions = Mock(
        return_value=[LoadedPlugin(definition=definition, module=module, mtime=0.0)]
    )

    with pytest.raises(ValueError, match="group_ids"):
        app._reschedule("startup")

    app.scheduler.replace_prefix.assert_not_called()


@pytest.mark.unit
def test_app_reschedule_rejects_missing_handler_before_replacement(temp_app_root: Path):
    app = XiaoQingApp(temp_app_root)
    definition = PluginDefinition(
        name="test_plugin",
        version="1.0.0",
        entry="main.py",
        commands=[],
        schedule=[{"id": "missing", "handler": "absent", "cron": {"minute": "*"}}],
        concurrency="parallel",
    )
    app.scheduler.replace_prefix = Mock()
    app.plugin_manager.schedule_definitions = Mock(
        return_value=[LoadedPlugin(definition=definition, module=SimpleNamespace(), mtime=0.0)]
    )

    with pytest.raises(ValueError, match="missing or not callable"):
        app._reschedule("startup")

    app.scheduler.replace_prefix.assert_not_called()


@pytest.mark.unit
def test_app_reschedule_removes_retired_jobs_when_reloaded_handler_is_missing(
    temp_app_root: Path,
):
    app = XiaoQingApp(temp_app_root)
    definition = PluginDefinition(
        name="test_plugin",
        version="1.0.0",
        entry="main.py",
        commands=[],
        schedule=[{"id": "missing", "handler": "absent", "cron": {"minute": "*"}}],
        concurrency="parallel",
    )
    app.scheduler.replace_prefix = Mock()
    app.plugin_manager.get = Mock(
        return_value=LoadedPlugin(
            definition=definition,
            module=SimpleNamespace(),
            mtime=0.0,
        )
    )

    with pytest.raises(ValueError, match="missing or not callable"):
        app._reschedule("test_plugin")

    app.scheduler.replace_prefix.assert_called_once_with("plugin.test_plugin.", [])


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_collect_actions_with_sink(temp_app_root: Path):
    """Test _collect_actions_for_event with active sink"""
    app = XiaoQingApp(temp_app_root)

    # Track sink calls
    sink_calls = []

    async def mock_sink(action):
        sink_calls.append(action)

    # Set sink
    token = current_action_sink.set(mock_sink)

    # Mock dispatcher
    app.dispatcher.handle_event = AsyncMock(
        return_value=[{"type": "text", "data": {"text": "test"}}]
    )

    event = {"post_type": "message", "message_type": "private", "user_id": 12345, "message": "test"}
    result = await app._collect_actions_for_event(event, default_source="test")

    # Reset sink
    current_action_sink.reset(token)

    # Verify result (sink is NOT called by design of _collect_actions_for_event)
    assert len(result) > 0
    assert result[0]["action"] == "send_private_msg"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_collect_actions_without_sink(temp_app_root: Path):
    """Test _collect_actions_for_event without active sink"""
    app = XiaoQingApp(temp_app_root)

    # Make sure no sink is set
    token = current_action_sink.set(None)
    current_action_sink.reset(token)

    # Mock dispatcher
    app.dispatcher.handle_event = AsyncMock(
        return_value=[{"type": "text", "data": {"text": "test"}}]
    )

    event = {"post_type": "message", "message_type": "private", "user_id": 12345, "message": "test"}
    result = await app._collect_actions_for_event(event, default_source="test")

    # Should return collected actions
    assert isinstance(result, list)
    assert len(result) > 0


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_cleanup_sessions_loop(temp_app_root: Path):
    """Test _cleanup_sessions_loop runs periodically"""
    app = XiaoQingApp(temp_app_root)

    # Mock session manager
    app.session_manager.cleanup_expired = AsyncMock()

    # Mock asyncio.sleep to return immediately first time, then raise CancelledError
    # This ensures one loop iteration runs
    stop_exc = asyncio.CancelledError("Stop loop")

    with patch("asyncio.sleep", side_effect=[None, stop_exc]):
        try:
            await app._cleanup_sessions_loop()
        except asyncio.CancelledError:
            pass

    # Verify cleanup was called
    app.session_manager.cleanup_expired.assert_called()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_on_ws_connected(temp_app_root: Path):
    """Test _on_ws_connected sends notification to default groups"""
    app = XiaoQingApp(temp_app_root)

    # Set default groups (update internal config directly for test)
    _set_app_config(app, default_group_ids=[123, 456])

    # Also update via config property just in case (though it's read-only usually, this updates the temp dict if property logic changed)
    # But strictly speaking we need to update what ConfigManager returns.

    # Mock ws_client
    app.ws_client = MagicMock()
    app.ws_client.send_action = AsyncMock()

    # Mock _send_action
    with patch.object(app, "_send_action", new=AsyncMock()) as mock_send:
        await app._on_ws_connected()

        # Verify messages were sent
        assert mock_send.call_count == 2


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_on_ws_connected_no_groups(temp_app_root: Path):
    """Test _on_ws_connected with no default groups"""
    app = XiaoQingApp(temp_app_root)

    # No default groups
    _set_app_config(app, default_group_ids=[])

    # Mock ws_client
    app.ws_client = MagicMock()
    app.ws_client.send_action = AsyncMock()

    # Mock _send_action
    with patch.object(app, "_send_action", new=AsyncMock()) as mock_send:
        await app._on_ws_connected()

        # No messages should be sent
        mock_send.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_on_ws_connected_rejects_invalid_group_ids(temp_app_root: Path):
    app = XiaoQingApp(temp_app_root)
    _set_app_config(app, default_group_ids=[123, "invalid"])
    app.ws_client = MagicMock()

    with patch.object(app, "_send_action", new=AsyncMock()) as mock_send:
        await app._on_ws_connected()

    mock_send.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_on_ws_connected_uses_configured_name_in_default_message(
    temp_app_root: Path,
):
    app = XiaoQingApp(temp_app_root)
    _set_app_config(
        app,
        bot_name="阿澄",
        default_group_ids=[123],
        connect_notification=None,
    )
    app.ws_client = MagicMock()

    with patch.object(app, "_send_action", new=AsyncMock()) as mock_send:
        await app._on_ws_connected()

    action = mock_send.await_args.args[0]
    assert action["params"]["message"][0]["data"]["text"] == "🟢 阿澄已上线~"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_on_ws_connected_throttles_reconnect_notifications(temp_app_root: Path):
    """Test _on_ws_connected suppresses repeated reconnect notifications"""
    app = XiaoQingApp(temp_app_root)
    _set_app_config(
        app,
        default_group_ids=[123],
        connect_notification_min_interval_seconds=300,
    )
    app.ws_client = MagicMock()

    with (
        patch("core.app_delivery.time.monotonic", side_effect=[100.0, 120.0, 450.0]),
        patch.object(app, "_send_action", new=AsyncMock()) as mock_send,
    ):
        await app._on_ws_connected()
        await app._on_ws_connected()
        await app._on_ws_connected()

        assert mock_send.call_count == 2
