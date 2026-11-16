"""插件重载、计划任务和会话。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
import pytest_asyncio

import tests.helpers.app_test_support as _fixture_support
from tests.helpers.app_test_support import (
    PROBE,
    BlockingConcurrencyProbe,
    DeliveryReceipt,
    DeliverySegments,
    XiaoQingApp,
    current_action_sink,
    load_test_plugin,
    load_text_command_plugin,
    service_declaration,
    set_app_config,
)

temp_app_root                               = _fixture_support.temp_app_root
_isolate_process_global_plugin_import_state = (
    _fixture_support._isolate_process_global_plugin_import_state
)

_NEVER = {"minute": "0", "hour": "0", "day": "1", "month": "1"}


def _trusted_sender(app: XiaoQingApp, *, results=True) -> AsyncMock:
    """在 OneBot 发送器边界放一个已连接、可信的假 WebSocket 连接，返回它的 ``send_action``。

    插件上下文发送时若没有已连接的 WS，会先等待 WS 最多 2 秒再退到 HTTP；放一个已连接的 WS
    让发送立刻到达边界。

    ``results`` 为单个值时每次都返回它；为列表时按顺序逐次返回。
    """

    send = (
        AsyncMock(side_effect=list(results))
        if isinstance(results, list)
        else AsyncMock(return_value=results)
    )
    app.ws_client = SimpleNamespace(
        credentials_trusted = True,
        connected           = lambda: True,
        send_action         = send,
    )
    return send


# 计划任务的处理函数：什么也不做；清单里声明的 ``handler`` 指向它。
SCHEDULED_SOURCE = """
async def scheduled(context):
    return None
"""

# 计划任务到期时记下 Core 交给插件的上下文里的投递目标。
SCHEDULED_TARGETS_SOURCE = """
from tests.helpers.plugin_probe import PROBE

async def scheduled(context):
    PROBE.log("groups").extend(context.default_groups())
    PROBE.log("delivery").append(context.principal.schedule_delivery)
    return []
"""

# 对外发出的消息交给它观察：记下动作、上下文和来源插件。
OBSERVER_SOURCE = """
from tests.helpers.plugin_probe import PROBE

async def observe(action, context, *, source_plugin):
    PROBE.log("observed").append((action, context, source_plugin))
    return []
"""


# 计划任务到期时记下 Core 交给插件的主体，再按 ``PROBE.script("scheduled", ...)`` 预设的结果行事：
# 异常实例就抛出，其他值原样返回。
SCHEDULE_RECORDER_SOURCE = """
from tests.helpers.plugin_probe import PROBE

async def scheduled(context):
    PROBE.log("principals").append(context.principal)
    outcome = PROBE.next_result("scheduled")
    if isinstance(outcome, BaseException):
        raise outcome
    return outcome
"""


@pytest_asyncio.fixture
async def scheduler_app(temp_app_root: Path):
    """真实应用（调度器需要运行中的事件循环）；用例结束时关掉调度器。"""

    app = XiaoQingApp(temp_app_root)
    yield app
    app.scheduler.shutdown()


def _scheduled_job_ids(app: XiaoQingApp) -> list[str]:
    assert app.scheduler.scheduler is not None
    return sorted(job.id for job in app.scheduler.scheduler.get_jobs())


async def _scheduled_job(
    app: XiaoQingApp,
    source: str = SCHEDULE_RECORDER_SOURCE,
    *,
    manifest: dict | None = None,
    **entry,
):
    """装入带一条计划任务 ``poll`` 的真实插件 ``test_plugin``，返回调度器登记的那条任务。

    ``entry`` 是清单里这条计划任务的其他字段（``delivery``、``group_ids``）；到期执行就是
    ``await job.func()``，走的正是 Core 登记的 ``_run_job``。
    """

    await load_test_plugin(
        app,
        "test_plugin",
        source,
        schedule=[{"id": "poll", "handler": "scheduled", "cron": {"minute": "*"}, **entry}],
        **(manifest or {}),
    )
    app._reschedule("startup")
    assert app.scheduler.scheduler is not None
    (job,) = app.scheduler.scheduler.get_jobs()
    return job


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.parametrize(
    "reloaded",
    [
        pytest.param(True, id="all_reloaded"),
        pytest.param(False, id="plugin_quarantined"),
    ],
)
async def test_app_reload_plugins_reports_the_transactional_reload_result(
    temp_app_root: Path, reloaded: bool
):
    """完整重载按代事务重载每个插件（先清掉该插件的会话），并把结果原样交回。"""
    app = XiaoQingApp(temp_app_root)

    app.plugin_manager.reload_all_plugins = AsyncMock(return_value=reloaded)
    app.session_manager.clear_plugin_sessions = AsyncMock()

    assert await app._reload_plugins_async_with_logging() is reloaded

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

    app._reload_plugins()

    # Should create a task
    assert app._reload_task is not None

    # Wait for it to finish to avoid pending task warning
    assert await app._reload_task is True


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_reload_plugins_already_in_progress(temp_app_root: Path):
    """正在重载时再次请求重载：交回同一个后台任务，不再新建。"""
    app = XiaoQingApp(temp_app_root)

    first  = app._reload_plugins()
    second = app._reload_plugins()

    assert first is not None
    assert second is first
    assert app._reload_task is first
    assert await first is True


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_run_job(scheduler_app: XiaoQingApp):
    """到期的计划任务以计划任务主体执行，结果向清单里的每个目标群各发一条。"""
    app  = scheduler_app
    send = _trusted_sender(app)
    PROBE.script("scheduled", [{"type": "text", "data": {"text": "scheduled result"}}])
    job = await _scheduled_job(app, group_ids=[123, 456])

    await job.func()

    # 插件拿到的是计划任务主体；结果向每个目标群各发一条
    (principal,) = PROBE.log("principals")
    assert principal.kind == "scheduled_system"
    assert principal.schedule_delivery == "broadcast"
    assert [call.args[0]["params"]["group_id"] for call in send.await_args_list] == [123, 456]


# 自行向 ``context.default_groups()`` 逐个发送、不返回结果的计划任务。
SELF_DELIVERING_SOURCE = """
from tests.helpers.plugin_probe import PROBE

async def scheduled(context):
    PROBE.log("principals").append(context.principal)
    for group_id in context.default_groups():
        await context.send_action(
            {"action": "send_group_msg", "params": {"group_id": group_id, "message": []}}
        )
    return []
"""


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
    scheduler_app: XiaoQingApp,
    manifest_groups: list[int] | None,
    expected_groups: list[int],
) -> None:
    """自行确认投递结果的插件也只能使用 Core 解析后的调度目标。"""

    app = scheduler_app
    set_app_config(app, default_group_ids=[303])
    send  = _trusted_sender(app)
    entry = {} if manifest_groups is None else {"group_ids": manifest_groups}
    job   = await _scheduled_job(app, SELF_DELIVERING_SOURCE, **entry)

    await job.func()

    sent_groups = [call.args[0]["params"]["group_id"] for call in send.await_args_list]
    assert sent_groups == expected_groups

    assert [
        target.group_id for target in PROBE.log("principals")[0].delivery_targets
    ] == expected_groups


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.parametrize(
    ("schedules", "manifest_groups", "expected_groups"),
    [
        # 部署配置里写了这个定时任务的目标群：优先于清单和全局默认。
        ({"poll": {"group_ids": [909]}}, [101], [909]),
        # 显式空列表也是覆盖：这个任务不向任何群投递。
        ({"poll": {"group_ids": []}}, [101], []),
        # 别的任务的覆盖不影响这一个。
        ({"other": {"group_ids": [909]}}, [101], [101]),
        ({}, None, [303]),
    ],
)
async def test_deployment_config_overrides_schedule_targets(
    scheduler_app: XiaoQingApp,
    schedules: dict,
    manifest_groups: list[int] | None,
    expected_groups: list[int],
) -> None:
    """真实群号只写在私有配置里：``plugins.<插件>.schedules.<任务>.group_ids`` 在执行时生效。"""

    app = scheduler_app
    set_app_config(app, default_group_ids=[303], plugins={"test_plugin": {"schedules": schedules}})
    entry = {} if manifest_groups is None else {"group_ids": manifest_groups}
    job   = await _scheduled_job(app, **entry)

    await job.func()

    assert [
        target.group_id for target in PROBE.log("principals")[0].delivery_targets
    ] == expected_groups


@pytest.mark.asyncio
@pytest.mark.unit
async def test_invalid_schedule_override_skips_the_run(scheduler_app: XiaoQingApp) -> None:
    app = scheduler_app
    set_app_config(app, plugins={"test_plugin": {"schedules": {"poll": {"group_ids": "123"}}}})
    job = await _scheduled_job(app, group_ids=[101])

    await job.func()

    # 处理函数没有被执行，也没有为这次运行签发过主体
    assert PROBE.log("principals") == []


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_run_job_rejects_invalid_group_ids_before_plugin_execution(
    scheduler_app: XiaoQingApp,
):
    app = scheduler_app
    set_app_config(app, default_group_ids=[123, "invalid"])
    job = await _scheduled_job(app)

    await job.func()

    assert PROBE.log("principals") == []


# 静默计划任务：尝试主动发送，并返回一段不该被投递的文字。
SILENT_SCHEDULE_SOURCE = """
from tests.helpers.plugin_probe import PROBE

async def scheduled(context):
    PROBE.log("principals").append(context.principal)
    PROBE.log("active_send").append(
        await context.send_action(
            {"action": "send_group_msg", "params": {"group_id": 123, "message": []}}
        )
    )
    return [{"type": "text", "data": {"text": "must stay silent"}}]
"""


@pytest.mark.asyncio
@pytest.mark.unit
async def test_silent_schedule_runs_without_any_delivery_capability(scheduler_app: XiaoQingApp):
    app = scheduler_app
    set_app_config(app, default_group_ids=[123, "invalid"])
    send = _trusted_sender(app)
    job = await _scheduled_job(app, SILENT_SCHEDULE_SOURCE, delivery="silent")

    await job.func()

    (principal,) = PROBE.log("principals")
    assert principal.schedule_delivery == "silent"
    assert principal.delivery_targets == ()
    assert PROBE.log("active_send") == [False]
    send.assert_not_awaited()


# 定向计划任务：主动发送三条，再把三份定向结果交回 Core。
TARGETED_SCHEDULE_SOURCE = """
from core.delivery import ScheduledDelivery
from tests.helpers.plugin_probe import PROBE

async def scheduled(context):
    for action in (
        {"action": "send_group_msg", "params": {"group_id": 101, "message": []}},
        {"action": "send_group_msg", "params": {"group_id": 999, "message": []}},
        {"action": "send_private_msg", "params": {"user_id": 42, "message": []}},
    ):
        PROBE.log("active_outcomes").append(await context.send_action(action))
    return [
        ScheduledDelivery.group(101, [{"type": "text", "data": {"text": "allowed group"}}]),
        ScheduledDelivery.group(999, [{"type": "text", "data": {"text": "blocked group"}}]),
        ScheduledDelivery.private(42, [{"type": "text", "data": {"text": "allowed private"}}]),
    ]
"""


@pytest.mark.asyncio
@pytest.mark.unit
async def test_targeted_schedule_validates_returned_and_active_targets(scheduler_app: XiaoQingApp):
    app  = scheduler_app
    send = _trusted_sender(app)
    job = await _scheduled_job(app, TARGETED_SCHEDULE_SOURCE, delivery="targeted", group_ids=[101])

    await job.func()

    assert PROBE.log("active_outcomes") == [True, False, True]
    actions = [call.args[0] for call in send.await_args_list]
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
    scheduler_app: XiaoQingApp,
):
    app         = scheduler_app
    committed   = Mock()
    rolled_back = Mock()
    receipt     = DeliveryReceipt(
        expected_actions = 1,
        commit           = committed,
        rollback         = rolled_back,
        unknown          = rolled_back,
    )
    PROBE.script(
        "scheduled",
        DeliverySegments([{"type": "text", "data": {"text": "scheduled result"}}], receipt),
    )
    _trusted_sender(app, results=[True, False])
    job = await _scheduled_job(app, group_ids=[123, 456])

    await job.func()

    assert receipt.resolved is True
    assert receipt.committed is False
    committed.assert_not_called()
    rolled_back.assert_called_once_with()


# 到期时交给测试放进 ``PROBE.slots["probe"]`` 的并发探针执行。
BLOCKING_SCHEDULE_SOURCE = """
from tests.helpers.plugin_probe import PROBE

async def scheduled(context):
    return await PROBE.slots["probe"].run(context)
"""


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_run_job_uses_plugin_sequential_gate(scheduler_app: XiaoQingApp):
    """Scheduled calls share the same manifest gate as message handlers."""
    app                  = scheduler_app
    entered              = asyncio.Event()
    release              = asyncio.Event()
    probe                = BlockingConcurrencyProbe(entered, release)
    PROBE.slots["probe"] = probe
    job                  = await _scheduled_job(
        app, BLOCKING_SCHEDULE_SOURCE, manifest={"concurrency": "sequential"}
    )

    first = asyncio.create_task(job.func())
    await asyncio.wait_for(entered.wait(), timeout=5)
    second = asyncio.create_task(job.func())
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
    _trusted_sender(app)
    observed = PROBE.log("observed")
    await load_test_plugin(
        app,
        "xiaoqing_chat",
        OBSERVER_SOURCE,
        services=[service_declaration("core.observe_outgoing_action", "observe", ("core",))],
    )

    action = {
        "action": "send_group_msg",
        "params": {
            "group_id": 123,
            "message" : [{"type": "text", "data": {"text": "地震播报"}}],
        },
        "_source_plugin": "earthquake",
    }

    await app._send_action(action)

    assert len(observed) == 1
    observed_action, context, source_plugin = observed[0]
    assert observed_action == action
    assert source_plugin == "earthquake"
    assert context.plugin_name == "xiaoqing_chat"
    assert context.current_user_id is None
    assert context.current_group_id == 123
    assert context.principal.kind == "lifecycle"
    assert context.principal.group_id is None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_send_action_passes_own_source_to_declared_observer(temp_app_root: Path):
    app  = XiaoQingApp(temp_app_root)
    send = _trusted_sender(app)
    await load_test_plugin(
        app,
        "xiaoqing_chat",
        OBSERVER_SOURCE,
        services=[service_declaration("core.observe_outgoing_action", "observe", ("core",))],
    )
    result = await app._send_action(
        {
            "action": "send_group_msg",
            "params": {
                "group_id": 123,
                "message" : [{"type": "text", "data": {"text": "小青回复"}}],
            },
            "_source_plugin": "xiaoqing_chat",
        }
    )

    assert result is True
    send.assert_awaited_once()
    assert [source for _action, _context, source in PROBE.log("observed")] == ["xiaoqing_chat"]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_run_job_no_result(scheduler_app: XiaoQingApp):
    """Test the scheduled job with a handler returning no result"""
    app  = scheduler_app
    send = _trusted_sender(app)
    job = await _scheduled_job(app, group_ids=[123])

    await job.func()

    assert len(PROBE.log("principals")) == 1
    send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_run_job_with_error(scheduler_app: XiaoQingApp):
    """Test the scheduled job handles handler errors"""
    app  = scheduler_app
    send = _trusted_sender(app)
    PROBE.script("scheduled", ValueError("Test error"))
    job = await _scheduled_job(app, group_ids=[123])

    # Should not raise
    await job.func()

    assert len(PROBE.log("principals")) == 1
    send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_run_job_swallows_cancelled_error(scheduler_app: XiaoQingApp):
    """Test the scheduled job treats cancellation as normal shutdown."""
    app = scheduler_app
    PROBE.script("scheduled", asyncio.CancelledError())
    job = await _scheduled_job(app)

    await job.func()

    assert len(PROBE.log("principals")) == 1


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_reschedule_startup(scheduler_app: XiaoQingApp):
    """Test _reschedule with startup event"""
    app = scheduler_app
    app.scheduler.add_job("plugin.stale.job", lambda: None, _NEVER)
    app.scheduler.add_job("other.keep", lambda: None, _NEVER)

    app._reschedule("startup")

    # 启动时的整体重排：没有任何插件声明计划任务，旧的插件任务被清掉，非插件任务不受影响
    assert _scheduled_job_ids(app) == ["other.keep"]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_reschedule_single_plugin(scheduler_app: XiaoQingApp):
    """Test _reschedule for single plugin"""
    app = scheduler_app
    await load_test_plugin(app, "test_plugin", "")
    app.scheduler.add_job("plugin.test_plugin.stale", lambda: None, _NEVER)
    app.scheduler.add_job("plugin.other.keep", lambda: None, _NEVER)

    app._reschedule("test_plugin")

    assert _scheduled_job_ids(app) == ["plugin.other.keep"]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_reschedule_skips_manifest_disabled_schedule(scheduler_app: XiaoQingApp):
    app = scheduler_app
    await load_test_plugin(
        app,
        "test_plugin",
        SCHEDULED_SOURCE,
        schedule=[
            {
                "id"     : "disabled",
                "handler": "scheduled",
                "cron"   : {"minute": "*"},
                "enabled": False,
            }
        ],
    )

    app._reschedule("startup")

    assert _scheduled_job_ids(app) == []


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_reschedule_preserves_manifest_schedule_description(scheduler_app: XiaoQingApp):
    app = scheduler_app
    await load_test_plugin(
        app,
        "test_plugin",
        SCHEDULED_SOURCE,
        schedule=[
            {
                "id"         : "enabled",
                "handler"    : "scheduled",
                "cron"       : {"minute": "*"},
                "description": "visible scheduler metadata",
                "enabled"    : True,
            }
        ],
    )

    app._reschedule("startup")

    assert app.scheduler.scheduler is not None
    jobs = app.scheduler.scheduler.get_jobs()
    assert len(jobs) == 1
    assert jobs[0].id == "plugin.test_plugin.enabled"
    assert jobs[0].name == "visible scheduler metadata"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_manifest_schedule_group_ids_reach_plugin_context(temp_app_root: Path) -> None:
    """Manifest 目标由 Core 解析，并传入实际到期执行的插件上下文。"""

    app = XiaoQingApp(temp_app_root)
    set_app_config(app, default_group_ids=[303])
    await load_test_plugin(
        app,
        "test_plugin",
        SCHEDULED_TARGETS_SOURCE,
        schedule=[
            {
                "id"       : "targeted",
                "handler"  : "scheduled",
                "cron"     : {"minute": "*"},
                "delivery" : "targeted",
                "group_ids": [101, 202],
            }
        ],
    )

    app._reschedule("startup")
    assert app.scheduler.scheduler is not None
    (job,) = app.scheduler.scheduler.get_jobs()
    await job.func()

    assert PROBE.log("groups") == [101, 202]
    assert PROBE.log("delivery") == ["targeted"]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_reschedule_rejects_missing_handler_before_replacement(
    scheduler_app: XiaoQingApp,
):
    """任何一条任务的处理函数缺失就整体不替换：旧任务还在，合格的那条也没有加进去。"""

    app = scheduler_app
    await load_test_plugin(
        app,
        "test_plugin",
        SCHEDULED_SOURCE,
        schedule=[
            {"id": "valid", "handler": "scheduled", "cron": {"minute": "*"}},
            {"id": "missing", "handler": "absent", "cron": {"minute": "*"}},
        ],
    )
    app.scheduler.add_job("plugin.old.keep", lambda: None, _NEVER)

    with pytest.raises(ValueError, match="missing or not callable"):
        app._reschedule("startup")

    assert _scheduled_job_ids(app) == ["plugin.old.keep"]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_reschedule_removes_retired_jobs_when_reloaded_handler_is_missing(
    scheduler_app: XiaoQingApp,
):
    app = scheduler_app
    await load_test_plugin(
        app,
        "test_plugin",
        "",
        schedule=[{"id": "missing", "handler": "absent", "cron": {"minute": "*"}}],
    )
    app.scheduler.add_job("plugin.test_plugin.retired", lambda: None, _NEVER)
    app.scheduler.add_job("plugin.other.keep", lambda: None, _NEVER)

    with pytest.raises(ValueError, match="missing or not callable"):
        app._reschedule("test_plugin")

    # 重载后的处理函数缺失：该插件旧的任务被清掉，别的插件的任务不动
    assert _scheduled_job_ids(app) == ["plugin.other.keep"]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_collect_actions_with_sink(temp_app_root: Path):
    """Test _collect_actions_for_event with active sink"""
    app = XiaoQingApp(temp_app_root)
    await load_text_command_plugin(app)

    # Track sink calls
    sink_calls = []

    async def mock_sink(action):
        sink_calls.append(action)

    # Set sink
    token = current_action_sink.set(mock_sink)

    event = {
        "post_type"   : "message",
        "message_type": "private",
        "user_id"     : 12345,
        "message"     : "/echo",
    }
    try:
        result = await app._collect_actions_for_event(event, default_source="test")
    finally:
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
    await load_text_command_plugin(app)

    # Make sure no sink is set
    token = current_action_sink.set(None)
    current_action_sink.reset(token)

    event = {
        "post_type"   : "message",
        "message_type": "private",
        "user_id"     : 12345,
        "message"     : "/echo",
    }
    result = await app._collect_actions_for_event(event, default_source="test")

    # Should return collected actions
    assert isinstance(result, list)
    assert len(result) > 0


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_cleanup_sessions_loop(temp_app_root: Path):
    """会话清理循环在后台回收过期会话，不影响未过期的会话。"""
    app = XiaoQingApp(temp_app_root)
    await app.session_manager.create(1, None, "test_plugin", timeout=0.001)
    await app.session_manager.create(2, None, "test_plugin", timeout=300.0)
    await asyncio.sleep(0.02)

    task = asyncio.create_task(app._cleanup_sessions_loop())
    try:
        for _ in range(100):
            if await app.session_manager.count() == 1:
                break
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert await app.session_manager.count() == 1
    assert await app.session_manager.exists(2, None) is True


def _connected_ws_client() -> SimpleNamespace:
    """一个已连接、凭据可信的假 OneBot WebSocket 连接（边界）。"""

    return SimpleNamespace(
        credentials_trusted = True,
        connected           = lambda: True,
        send_action=AsyncMock(return_value=True),
    )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_on_ws_connected(temp_app_root: Path):
    """Test _on_ws_connected sends notification to default groups"""
    app = XiaoQingApp(temp_app_root)

    # Set default groups (update internal config directly for test)
    set_app_config(app, default_group_ids=[123, 456])
    app.ws_client = _connected_ws_client()

    await app._on_ws_connected()

    # Verify messages were sent
    assert app.ws_client.send_action.await_count == 2
    groups = [
        call.args[0]["params"]["group_id"] for call in app.ws_client.send_action.await_args_list
    ]
    assert groups == [123, 456]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_on_ws_connected_no_groups(temp_app_root: Path):
    """Test _on_ws_connected with no default groups"""
    app = XiaoQingApp(temp_app_root)

    # No default groups
    set_app_config(app, default_group_ids=[])
    app.ws_client = _connected_ws_client()

    await app._on_ws_connected()

    # No messages should be sent
    app.ws_client.send_action.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_on_ws_connected_rejects_invalid_group_ids(temp_app_root: Path):
    app = XiaoQingApp(temp_app_root)
    set_app_config(app, default_group_ids=[123, "invalid"])
    app.ws_client = _connected_ws_client()

    await app._on_ws_connected()

    app.ws_client.send_action.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_on_ws_connected_uses_configured_name_in_default_message(
    temp_app_root: Path,
):
    app = XiaoQingApp(temp_app_root)
    set_app_config(
        app,
        bot_name             = "阿澄",
        default_group_ids    = [123],
        connect_notification = None,
    )
    app.ws_client = _connected_ws_client()

    await app._on_ws_connected()

    action = app.ws_client.send_action.await_args.args[0]
    assert action["params"]["message"][0]["data"]["text"] == "🟢 阿澄已上线~"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_on_ws_connected_throttles_reconnect_notifications(temp_app_root: Path):
    """Test _on_ws_connected suppresses repeated reconnect notifications"""
    app = XiaoQingApp(temp_app_root)
    set_app_config(
        app,
        default_group_ids                         = [123],
        connect_notification_min_interval_seconds = 300,
    )
    app.ws_client = _connected_ws_client()

    with patch("core.app_delivery.time.monotonic", side_effect=[100.0, 120.0, 450.0]):
        await app._on_ws_connected()
        await app._on_ws_connected()
        await app._on_ws_connected()

    assert app.ws_client.send_action.await_count == 2
