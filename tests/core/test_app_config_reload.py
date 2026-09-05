"""应用配置发布、插件订阅和并发限流更新。"""

from __future__ import annotations

import tests.helpers.app_test_support as _fixture_support
from tests.helpers.app_test_support import (
    Any,
    Mapping,
    Mock,
    Path,
    XiaoQingApp,
    _plugin_context_for,
    asyncio,
    json,
    pytest,
)

mock_dependencies = _fixture_support.mock_dependencies
temp_app_root     = _fixture_support.temp_app_root


@pytest.mark.unit
def test_manual_reload_notifies_pendo_config_subscription(temp_app_root: Path):
    app     = XiaoQingApp(temp_app_root)
    context = _plugin_context_for(
        app,
        "pendo",
        manifest_capabilities=frozenset({"config_subscription"}),
    )
    subscription = context.capabilities.config_subscription
    assert subscription is not None
    received: list[Mapping[str, Any]] = []
    subscription.subscribe(received.append)
    config            = app.config_manager.snapshot().mutable_config()
    config["plugins"] = {"pendo": {"manual_revision": "seen"}}
    app.config_manager.config_path.write_text(json.dumps(config), encoding="utf-8")

    app.reload_config()

    assert received[-1]["plugins"]["pendo"]["manual_revision"] == "seen"
    app.scheduler.shutdown()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_pendo_subscription_preserves_each_fast_revision_snapshot(temp_app_root: Path):
    app     = XiaoQingApp(temp_app_root)
    context = _plugin_context_for(
        app,
        "pendo",
        manifest_capabilities=frozenset({"config_subscription"}),
    )
    subscription = context.capabilities.config_subscription
    assert subscription is not None
    received: list[str] = []
    first_entered       = asyncio.Event()
    first_release       = asyncio.Event()

    async def receive(view: Mapping[str, Any]) -> None:
        received.append(str(view["plugins"]["pendo"]["revision_value"]))
        if len(received) == 1:
            first_entered.set()
            await first_release.wait()

    subscription.subscribe(receive)
    first_config            = app.config_manager.snapshot().mutable_config()
    first_config["plugins"] = {"pendo": {"revision_value": "rev-1"}}
    app.config_manager.config_path.write_text(json.dumps(first_config), encoding="utf-8")
    app.config_manager.reload(notify=True)

    second_config            = app.config_manager.snapshot().mutable_config()
    second_config["plugins"] = {"pendo": {"revision_value": "rev-2"}}
    app.config_manager.config_path.write_text(json.dumps(second_config), encoding="utf-8")
    app.config_manager.reload(notify=True)

    await first_entered.wait()
    assert received == ["rev-1"]
    first_release.set()
    for _ in range(20):
        if len(received) == 2:
            break
        await asyncio.sleep(0)

    assert received == ["rev-1", "rev-2"]
    await asyncio.gather(*tuple(app._config_apply_tasks), return_exceptions=True)
    app.scheduler.shutdown()


@pytest.mark.unit
def test_app_reload_config(temp_app_root: Path):
    """Test reload_config triggers config reload"""
    from core.config import ConfigSnapshot
    from core.exceptions import ConfigLoadError

    app        = XiaoQingApp(temp_app_root)
    successful = app.config_manager.snapshot()

    app.config_manager.reload = Mock(return_value=successful)
    app._apply_config = Mock()

    app.reload_config()

    app.config_manager.reload.assert_called_once_with(notify=True)
    app._apply_config.assert_not_called()

    reload_error = ConfigLoadError("invalid replacement")
    fail_closed = ConfigSnapshot(config=successful.config, secrets={})
    app.config_manager.reload = Mock(side_effect=reload_error)
    app.config_manager.snapshot = Mock(return_value=fail_closed)
    app._apply_config.reset_mock()

    with pytest.raises(ConfigLoadError) as raised:
        app.reload_config()

    assert raised.value is reload_error
    app._apply_config.assert_called_once_with(fail_closed)


@pytest.mark.unit
def test_app_apply_config_updates_admins(temp_app_root: Path):
    """Test _apply_config updates admin set"""
    from core.config import ConfigSnapshot

    app = XiaoQingApp(temp_app_root)

    new_snapshot = ConfigSnapshot(
        config  = app.config,
        secrets = {"admin_user_ids": [99999], "onebot_token": "", "inbound_token": ""},
    )

    app._apply_config(new_snapshot)

    assert app.is_admin(99999) is True
    assert app.is_admin(12345) is False


@pytest.mark.unit
def test_stale_ordinary_apply_cannot_restore_admins_after_new_security_generation(
    temp_app_root: Path,
):
    from core.config import ConfigSnapshot

    app          = XiaoQingApp(temp_app_root)
    old_snapshot = ConfigSnapshot(
        config   = app.config,
        secrets  = {"admin_user_ids": [111], "onebot_token": "", "inbound_token": ""},
        revision = 1,
    )
    new_snapshot = ConfigSnapshot(
        config   = app.config,
        secrets  = {"admin_user_ids": [222], "onebot_token": "", "inbound_token": ""},
        revision = 2,
    )
    original_claim = app._claim_config_apply_owner

    def publish_new_security_during_old_apply(snapshot):
        owner = original_claim(snapshot)
        app._apply_security_snapshot(new_snapshot)
        return owner

    app._claim_config_apply_owner = publish_new_security_during_old_apply
    app._apply_config(old_snapshot)

    assert app.is_admin(111) is False
    assert app.is_admin(222) is True


@pytest.mark.unit
def test_app_apply_config_refreshes_prefix_cache(temp_app_root: Path):
    """Test _apply_config refreshes dispatcher prefix cache"""
    from core.config import ConfigSnapshot

    app = XiaoQingApp(temp_app_root)

    # Mock dispatcher
    app.dispatcher.refresh_prefix_cache = Mock()

    new_snapshot = ConfigSnapshot(config=app.config, secrets=app.secrets)
    app._apply_config(new_snapshot)

    app.dispatcher.refresh_prefix_cache.assert_called_once()


@pytest.mark.unit
def test_app_apply_config_resizes_dispatcher_limiter_without_replacing_it(
    temp_app_root: Path,
):
    """热更新只调整同一个 limiter，不能让旧、新容量同时放行。"""
    from core.config import ConfigSnapshot

    app      = XiaoQingApp(temp_app_root)
    original = app.dispatcher.semaphore

    same_config                    = dict(app.config)
    same_config["max_concurrency"] = app._dispatcher_concurrency
    app._apply_config(ConfigSnapshot(config=same_config, secrets=app.secrets, revision=1))

    assert app.dispatcher.semaphore is original

    changed_config                    = dict(same_config)
    changed_config["max_concurrency"] = app._dispatcher_concurrency + 1
    app._apply_config(ConfigSnapshot(config=changed_config, secrets=app.secrets, revision=2))

    assert app.dispatcher.semaphore is original
    assert app.dispatcher.semaphore.capacity == changed_config["max_concurrency"]


@pytest.mark.unit
def test_app_apply_config_validates_scalars_before_publishing_side_effects(
    temp_app_root: Path,
):
    from core.config import ConfigSnapshot

    app                                 = XiaoQingApp(temp_app_root)
    app.dispatcher.refresh_prefix_cache = Mock()
    app._configure_plugin_execution     = Mock()
    app._configure_plugin_watch         = Mock()
    invalid_config                      = dict(app.config)
    invalid_config["session_timeout"]   = "5m"

    with pytest.raises(ValueError, match="session_timeout"):
        app._apply_config(ConfigSnapshot(config=invalid_config, secrets=app.secrets, revision=1))

    app.dispatcher.refresh_prefix_cache.assert_not_called()
    app._configure_plugin_execution.assert_not_called()
    app._configure_plugin_watch.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_dispatcher_limiter_decrease_waits_for_existing_holders_to_drain():
    from core.dispatcher import AdjustableSemaphore

    limiter = AdjustableSemaphore(2)
    await limiter.acquire()
    await limiter.acquire()
    limiter.resize(1)

    waiting = asyncio.create_task(limiter.acquire())
    await asyncio.sleep(0)
    assert waiting.done() is False

    limiter.release()
    await asyncio.sleep(0)
    assert waiting.done() is False

    limiter.release()
    await asyncio.wait_for(waiting, timeout=1)
    assert limiter.in_use == 1
    limiter.release()
