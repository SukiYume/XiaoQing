"""配置安全重载。"""

from __future__ import annotations

import tests.helpers.app_test_support as _fixture_support
from tests.helpers.app_test_support import (
    Any,
    AsyncMock,
    InboundManager,
    MagicMock,
    Mock,
    Path,
    SimpleNamespace,
    XiaoQingApp,
    _onebot_credentials,
    asyncio,
    json,
    patch,
    pytest,
)
from tests.helpers.config_test_support import _last_notified_revision

mock_dependencies = _fixture_support.mock_dependencies
temp_app_root = _fixture_support.temp_app_root


@pytest.mark.asyncio
@pytest.mark.unit
async def test_failed_reload_revokes_auth_and_admin_before_blocked_callbacks(
    temp_app_root: Path,
):
    from core.exceptions import ConfigLoadError
    from core.onebot import OneBotHttpSender, OneBotWsClient

    secrets_path = temp_app_root / "config" / "secrets.json"
    secrets_path.write_text(
        json.dumps(
            {
                "admin_user_ids": [12345],
                "onebot_token": "old-onebot",
                "inbound_token": "old-inbound",
            }
        ),
        encoding="utf-8",
    )
    app = XiaoQingApp(temp_app_root)
    app.http_sender = OneBotHttpSender("http://127.0.0.1:5700", "old-onebot", MagicMock())
    app.ws_client = OneBotWsClient("ws://127.0.0.1:6700", "old-onebot")
    inbound = InboundManager(
        inbound_http_base="http://127.0.0.1:12000",
        inbound_ws_uri="",
        token="old-inbound",
        handler=app._handle_inbound_event,
    )
    app.inbound_manager = inbound
    callback_entered = asyncio.Event()
    callback_release = asyncio.Event()

    async def block_ordinary_callbacks(_snapshot) -> None:
        callback_entered.set()
        await callback_release.wait()

    app.config_manager.on_reload(block_ordinary_callbacks)
    first_config = app.config_manager.snapshot().mutable_config()
    first_config["bot_name"] = "first revision"
    app.config_manager.config_path.write_text(json.dumps(first_config), encoding="utf-8")
    app.config_manager.reload(notify=True)
    await callback_entered.wait()

    app.config_manager.config_path.write_text("{invalid", encoding="utf-8")
    secrets_path.write_text(json.dumps({"admin_user_ids": []}), encoding="utf-8")
    with pytest.raises(ConfigLoadError):
        app.reload_config()

    # No event-loop turn is needed: the trusted publication hook has already
    # revoked every live holder even though the ordinary queue is still stuck.
    assert app.http_sender.auth_token == ""
    assert app.ws_client.auth_token == ""
    assert app.http_sender.credentials_trusted is False
    assert app.ws_client.credentials_trusted is False
    assert inbound._token == ""
    assert app.is_admin(12345) is False

    with patch("core.onebot.aiohttp_request_bounded", new_callable=AsyncMock) as request:
        assert await app.http_sender.request_action({"action": "get_status", "params": {}}) is None
    request.assert_not_awaited()
    with patch("websockets.connect") as connect:
        with pytest.raises(RuntimeError, match="credential source is unavailable"):
            await app.ws_client._connect_once(AsyncMock())
    connect.assert_not_called()

    callback_release.set()
    for _ in range(20):
        if _last_notified_revision(app.config_manager) == app.config_manager.revision:
            break
        await asyncio.sleep(0)
    await asyncio.gather(*tuple(app._config_apply_tasks), return_exceptions=True)
    app.scheduler.shutdown()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_real_failed_reload_stops_old_ws_and_valid_recovery_restarts_trusted_client(
    temp_app_root: Path,
):
    from core.exceptions import ConfigLoadError
    from core.onebot import OneBotWsClient

    config_path = temp_app_root / "config" / "config.json"
    secrets_path = temp_app_root / "config" / "secrets.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config.update(
        {
            "onebot_http_base": "http://127.0.0.1:5700",
            "enable_ws_client": True,
            "onebot_ws_uri": "ws://127.0.0.1:6700",
        }
    )
    config_path.write_text(json.dumps(config), encoding="utf-8")
    secrets_path.write_text(json.dumps({"onebot_token": "old"}), encoding="utf-8")
    app = XiaoQingApp(temp_app_root)
    app.http_session = MagicMock()
    old_client = OneBotWsClient("ws://127.0.0.1:6700", "old")
    old_listener = asyncio.create_task(asyncio.sleep(3600))
    app.ws_client = old_client
    app._ws_client_task = old_listener

    async def drain_config_publications() -> None:
        for _ in range(50):
            if (
                _last_notified_revision(app.config_manager) == app.config_manager.revision
                and not app._config_apply_tasks
            ):
                return
            await asyncio.sleep(0)
        tasks = tuple(app._config_apply_tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    config_path.write_text("{broken", encoding="utf-8")
    secrets_path.write_text("{broken", encoding="utf-8")
    with pytest.raises(ConfigLoadError):
        app.reload_config()
    await drain_config_publications()

    assert app.ws_client is None
    assert old_client.credentials_trusted is False
    assert old_listener.done()
    assert app._security_conflict_revision is None

    config_path.write_text(json.dumps(config), encoding="utf-8")
    secrets_path.write_text(json.dumps({"onebot_token": "new"}), encoding="utf-8")
    release_listener = asyncio.Event()

    async def listen(_handler) -> None:
        await release_listener.wait()

    candidate = MagicMock(
        ws_uri="ws://127.0.0.1:6700",
        auth_token="new",
        credentials_trusted=True,
        _queue_size=app._parse_ws_queue_size(config),
        set_on_connect=Mock(),
        stop=AsyncMock(),
        connect_and_listen=AsyncMock(side_effect=listen),
    )
    with patch("core.app_ingress.OneBotWsClient", return_value=candidate) as client_factory:
        app.reload_config()
        await drain_config_publications()

    client_factory.assert_called_once_with(
        "ws://127.0.0.1:6700",
        "new",
        queue_size=app._parse_ws_queue_size(config),
        credentials_trusted=True,
    )
    assert app.ws_client is candidate
    assert app._runtime_onebot_credentials_trusted is True
    release_listener.set()
    if app._ws_client_task is not None:
        await asyncio.gather(app._ws_client_task, return_exceptions=True)
    await app._stop_ws_client()
    await app._cancel_task("_ws_client_task")
    app.scheduler.shutdown()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_ws_reconcile_does_not_create_client_until_credentials_are_trusted(
    temp_app_root: Path,
):
    app = XiaoQingApp(temp_app_root)
    release = asyncio.Event()

    async def listen(_handler) -> None:
        await release.wait()

    client = MagicMock(
        ws_uri="ws://new/ws",
        auth_token="",
        credentials_trusted=True,
        _queue_size=20,
        stop=AsyncMock(),
        set_on_connect=Mock(),
        connect_and_listen=AsyncMock(side_effect=listen),
    )

    with patch("core.app_ingress.OneBotWsClient", return_value=client) as client_cls:
        await app._reconcile_ws_client(
            enable_ws=True,
            ws_uri="ws://new/ws",
            token="",
            queue_size=20,
            credentials_trusted=False,
        )
        client_cls.assert_not_called()
        assert app.ws_client is None

        await app._reconcile_ws_client(
            enable_ws=True,
            ws_uri="ws://new/ws",
            token="",
            queue_size=20,
            credentials_trusted=True,
        )

    client_cls.assert_called_once_with(
        "ws://new/ws",
        "",
        queue_size=20,
        credentials_trusted=True,
    )
    assert app.ws_client is client
    release.set()
    await asyncio.gather(app._ws_client_task, return_exceptions=True)
    await app._stop_ws_client()
    await app._cancel_task("_ws_client_task")
    app.scheduler.shutdown()


@pytest.mark.unit
@pytest.mark.parametrize("status_name", ["MISSING", "INVALID", "UNAVAILABLE", "INCONSISTENT"])
def test_nonvalid_secret_source_revokes_even_an_explicitly_empty_onebot_token(
    temp_app_root: Path,
    status_name: str,
):
    from core.config import ConfigSnapshot, ConfigSourceStatus
    from core.onebot import OneBotHttpSender, OneBotWsClient

    app = XiaoQingApp(temp_app_root)
    config = app.config_manager.snapshot().mutable_config()
    config.update(
        {
            "onebot_http_base": "http://127.0.0.1:5700",
            "enable_ws_client": True,
            "onebot_ws_uri": "ws://127.0.0.1:6700",
        }
    )
    app.http_sender = OneBotHttpSender("http://127.0.0.1:5700", "", MagicMock())
    app.ws_client = OneBotWsClient("ws://127.0.0.1:6700", "")
    before_generation = app._onebot_auth_generation
    snapshot = ConfigSnapshot(
        config=config,
        secrets={"onebot_token": "must-not-authorize"},
        revision=1,
        secrets_status=getattr(ConfigSourceStatus, status_name),
    )

    app._apply_security_snapshot(snapshot)

    assert app._onebot_auth_generation == before_generation + 1
    assert app.http_sender.auth_token == ""
    assert app.ws_client.auth_token == ""
    assert app.http_sender.credentials_trusted is False
    assert app.ws_client.credentials_trusted is False
    app.scheduler.shutdown()


@pytest.mark.unit
@pytest.mark.parametrize("invalid_token", [False, 0, None, ["token"], {"token": "value"}])
def test_valid_secret_source_with_nonstring_onebot_token_is_revoked(
    temp_app_root: Path,
    invalid_token: Any,
):
    from core.config import ConfigSnapshot

    app = XiaoQingApp(temp_app_root)
    snapshot = ConfigSnapshot(
        config=app.config,
        secrets={"onebot_token": invalid_token},
        revision=1,
    )

    assert _onebot_credentials(snapshot) == ("", False)
    app._apply_security_snapshot(snapshot)
    assert app._runtime_onebot_credentials_trusted is False
    app.scheduler.shutdown()


@pytest.mark.unit
def test_string_subclass_cannot_change_empty_token_header_semantics(temp_app_root: Path):
    from core.config import ConfigSnapshot

    class FalseString(str):
        def __bool__(self) -> bool:
            return False

    app = XiaoQingApp(temp_app_root)
    snapshot = ConfigSnapshot(
        config=app.config,
        secrets={"onebot_token": FalseString("must-not-be-silently-anonymous")},
        revision=1,
    )

    assert _onebot_credentials(snapshot) == ("", False)
    app.scheduler.shutdown()


@pytest.mark.unit
def test_nonvalid_secret_sources_revoke_inbound_and_admin_holders_even_with_values(
    temp_app_root: Path,
):
    from core.config import ConfigSnapshot, ConfigSourceStatus

    app = XiaoQingApp(temp_app_root)
    config = app.config_manager.snapshot().mutable_config()
    config.update(
        {
            "enable_inbound_server": True,
            "inbound_http_base": "http://127.0.0.1:12000",
            "inbound_ws_uri": "",
        }
    )
    current = InboundManager(
        inbound_http_base="http://127.0.0.1:12000",
        inbound_ws_uri="",
        token="old",
        handler=app._handle_inbound_event,
    )
    candidate = InboundManager(
        inbound_http_base="http://127.0.0.1:12000",
        inbound_ws_uri="",
        token="old",
        handler=app._handle_inbound_event,
    )
    app.inbound_manager = current
    with app._inbound_candidates_lock:
        app._inbound_candidates_active.add(candidate)

    statuses = (
        ConfigSourceStatus.MISSING,
        ConfigSourceStatus.INVALID,
        ConfigSourceStatus.UNAVAILABLE,
        ConfigSourceStatus.INCONSISTENT,
    )
    for revision, status in enumerate(statuses, start=1):
        current.update_token("old")
        candidate.update_token("old")
        app._apply_security_snapshot(
            ConfigSnapshot(
                config=config,
                secrets={"inbound_token": "must-not-authorize", "admin_user_ids": [12345]},
                revision=revision,
                secrets_status=status,
            )
        )
        assert app._runtime_inbound_token == ""
        assert current._token == ""
        assert candidate._token == ""
        assert app.is_admin(12345) is False

    with app._inbound_candidates_lock:
        app._inbound_candidates_active.clear()
    app.scheduler.shutdown()


@pytest.mark.unit
def test_valid_secret_source_rejects_non_exact_inbound_token_types(temp_app_root: Path):
    from core.config import ConfigSnapshot

    class TokenSubclass(str):
        pass

    app = XiaoQingApp(temp_app_root)
    config = app.config_manager.snapshot().mutable_config()
    config.update(
        {
            "enable_inbound_server": True,
            "inbound_http_base": "http://127.0.0.1:12000",
            "inbound_ws_uri": "",
        }
    )
    current = InboundManager(
        inbound_http_base="http://127.0.0.1:12000",
        inbound_ws_uri="",
        token="old",
        handler=app._handle_inbound_event,
    )
    app.inbound_manager = current
    invalid_values = (False, 0, None, ["token"], {"token": "value"}, TokenSubclass("token"))

    for revision, invalid_token in enumerate(invalid_values, start=1):
        current.update_token("old")
        app._apply_security_snapshot(
            ConfigSnapshot(
                config=config,
                secrets={"inbound_token": invalid_token},
                revision=revision,
            )
        )
        assert app._runtime_inbound_token == ""
        assert current._token == ""

    app.scheduler.shutdown()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_holder_update_failure_quarantines_every_onebot_network_path(
    temp_app_root: Path,
):
    from core.config import ConfigSnapshot, ConfigSourceStatus

    app = XiaoQingApp(temp_app_root)
    config = app.config_manager.snapshot().mutable_config()
    config.update(
        {
            "onebot_http_base": "http://127.0.0.1:5700",
            "enable_ws_client": True,
            "onebot_ws_uri": "ws://127.0.0.1:6700",
        }
    )
    http_request = AsyncMock(return_value={"status": "ok"})
    http_send = AsyncMock(return_value=True)
    ws_request = AsyncMock(return_value={"status": "ok"})
    ws_send = AsyncMock(return_value=True)

    class BrokenHttpHolder:
        http_base = "http://127.0.0.1:5700"
        credentials_trusted = True

        def update(self, _base: str, _token: str) -> None:
            raise AssertionError("legacy two-argument holder")

        request_action = http_request
        send_action = http_send

    class BrokenWsHolder:
        ws_uri = "ws://127.0.0.1:6700"
        credentials_trusted = True

        def update(self, _uri: str, _token: str) -> None:
            raise AssertionError("legacy two-argument holder")

        @staticmethod
        def connected() -> bool:
            return True

        request_action = ws_request
        send_action = ws_send

    http_holder = BrokenHttpHolder()
    ws_holder = BrokenWsHolder()
    listener_release = asyncio.Event()
    listener_task = asyncio.create_task(listener_release.wait())
    app.http_sender = http_holder  # type: ignore[assignment]
    app.ws_client = ws_holder  # type: ignore[assignment]
    app._ws_client_task = listener_task
    app.dispatcher.handle_event = AsyncMock(return_value=[])

    app._apply_security_snapshot(
        ConfigSnapshot(
            config=config,
            secrets={"onebot_token": "ignored"},
            revision=1,
            secrets_status=ConfigSourceStatus.INVALID,
        )
    )
    await asyncio.sleep(0)
    await asyncio.gather(listener_task, return_exceptions=True)

    assert app._runtime_onebot_credentials_trusted is False
    assert app.http_sender is None
    assert app._ws_client_auth_quarantine is ws_holder
    assert listener_task.cancelled()
    assert app._http_transport_is_trusted(app.http_sender) is False
    assert await app._request_onebot_action("get_status", {}) is None
    assert await app._send_single_action({"action": "get_status", "params": {}}) is False
    await app._handle_upstream_event({}, source_client=ws_holder)  # type: ignore[arg-type]

    http_request.assert_not_awaited()
    http_send.assert_not_awaited()
    ws_request.assert_not_awaited()
    ws_send.assert_not_awaited()
    app.dispatcher.handle_event.assert_not_awaited()
    app._ws_client_task = None
    app.ws_client = None
    app._ws_client_auth_quarantine = None
    app.scheduler.shutdown()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_noop_holder_update_fails_postcondition_and_cannot_keep_old_authority(
    temp_app_root: Path,
):
    from core.config import ConfigSnapshot

    app = XiaoQingApp(temp_app_root)
    http_request = AsyncMock(return_value={"status": "ok"})
    ws_request = AsyncMock(return_value={"status": "ok"})
    ws_send = AsyncMock(return_value=True)

    class NoopHttpHolder:
        http_base = "http://old.example"
        auth_token = "old-token"
        credentials_trusted = True
        request_action = http_request

        @staticmethod
        def update(*_args, **_kwargs) -> None:
            pass

    class NoopWsHolder:
        ws_uri = "ws://old.example"
        auth_token = "old-token"
        credentials_trusted = True
        request_action = ws_request
        send_action = ws_send

        @staticmethod
        def update(*_args, **_kwargs) -> None:
            pass

        @staticmethod
        def connected() -> bool:
            return True

    http_holder = NoopHttpHolder()
    ws_holder = NoopWsHolder()
    app.http_sender = http_holder  # type: ignore[assignment]
    app.ws_client = ws_holder  # type: ignore[assignment]
    app.dispatcher.handle_event = AsyncMock(return_value=[])
    config = app.config_manager.snapshot().mutable_config()
    config.update(
        {
            "onebot_http_base": "http://new.example",
            "enable_ws_client": True,
            "onebot_ws_uri": "ws://new.example",
        }
    )

    app._apply_security_snapshot(
        ConfigSnapshot(
            config=config,
            secrets={"onebot_token": "new-token"},
            revision=1,
        )
    )

    assert app._runtime_onebot_credentials_trusted is True
    assert app.http_sender is None
    assert app._ws_client_auth_quarantine is ws_holder
    assert await app._request_onebot_action("get_status", {}) is None
    assert await app._send_single_action({"action": "get_status", "params": {}}) is False
    await app._handle_upstream_event({}, source_client=ws_holder)  # type: ignore[arg-type]
    http_request.assert_not_awaited()
    ws_request.assert_not_awaited()
    ws_send.assert_not_awaited()
    app.dispatcher.handle_event.assert_not_awaited()
    app.ws_client = None
    app._ws_client_auth_quarantine = None
    app.scheduler.shutdown()


@pytest.mark.unit
def test_closed_loop_listener_cancellation_failure_cannot_skip_inbound_revocation(
    temp_app_root: Path,
):
    from core.config import ConfigSnapshot, ConfigSourceStatus

    app = XiaoQingApp(temp_app_root)

    class BrokenWsHolder:
        ws_uri = "ws://127.0.0.1:6700"
        auth_token = "old"
        credentials_trusted = True

        @staticmethod
        def update(*_args, **_kwargs) -> None:
            raise RuntimeError("cannot rotate")

    async def never_started() -> None:
        await asyncio.Event().wait()

    foreign_loop = asyncio.new_event_loop()
    foreign_task = foreign_loop.create_task(never_started())
    foreign_loop.close()
    inbound = MagicMock()
    app.ws_client = BrokenWsHolder()  # type: ignore[assignment]
    app._ws_client_task = foreign_task
    app.inbound_manager = inbound

    app._apply_security_snapshot(
        ConfigSnapshot(
            config=app.config,
            secrets={"onebot_token": "old", "inbound_token": "old"},
            revision=1,
            secrets_status=ConfigSourceStatus.INVALID,
        )
    )

    assert app._runtime_onebot_credentials_trusted is False
    assert app._ws_client_auth_quarantine is app.ws_client
    inbound.update_token.assert_called_once_with("")

    app._ws_client_task = None
    app.ws_client = None
    app._ws_client_auth_quarantine = None
    foreign_task._log_destroy_pending = False
    foreign_task.get_coro().close()
    app.scheduler.shutdown()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_legacy_holder_without_explicit_trust_is_never_used(temp_app_root: Path):
    app = XiaoQingApp(temp_app_root)
    ws_request = AsyncMock(return_value={"status": "ok"})
    ws_send = AsyncMock(return_value=True)
    http_request = AsyncMock(return_value={"status": "ok"})
    http_send = AsyncMock(return_value=True)
    app.ws_client = SimpleNamespace(
        connected=lambda: True,
        request_action=ws_request,
        send_action=ws_send,
    )
    app.http_sender = SimpleNamespace(
        http_base="http://onebot",
        request_action=http_request,
        send_action=http_send,
    )

    assert await app._request_onebot_action("get_status", {}) is None
    assert await app._send_single_action({"action": "get_status", "params": {}}) is False
    ws_request.assert_not_awaited()
    ws_send.assert_not_awaited()
    http_request.assert_not_awaited()
    http_send.assert_not_awaited()
    app.scheduler.shutdown()


@pytest.mark.unit
def test_equal_security_revision_is_idempotent_but_conflict_stays_fail_closed(
    temp_app_root: Path,
):
    from core.config import ConfigSnapshot, ConfigSourceStatus

    app = XiaoQingApp(temp_app_root)
    first = ConfigSnapshot(
        config=app.config,
        secrets={"onebot_token": "first"},
        revision=1,
    )
    equal_copy = ConfigSnapshot(
        config=first.mutable_config(),
        secrets=first.mutable_secrets(),
        revision=1,
    )
    conflicting = ConfigSnapshot(
        config=app.config,
        secrets={"onebot_token": "conflicting"},
        revision=1,
    )

    app._apply_security_snapshot(first)
    generation = app._security_generation
    app._apply_security_snapshot(equal_copy)
    assert app._security_generation == generation
    assert app._runtime_onebot_token == "first"
    assert app._runtime_onebot_credentials_trusted is True

    app._apply_security_snapshot(conflicting)
    assert app._security_generation == generation + 1
    assert app._security_conflict_revision == 1
    assert app._security_snapshot is not None
    assert app._security_snapshot.secrets_status is ConfigSourceStatus.INCONSISTENT
    assert app._runtime_onebot_token == ""
    assert app._runtime_onebot_credentials_trusted is False

    app._apply_security_snapshot(first)
    assert app._runtime_onebot_credentials_trusted is False
    app._apply_security_snapshot(
        ConfigSnapshot(config=app.config, secrets={"onebot_token": "recovered"}, revision=2)
    )
    assert app._security_conflict_revision is None
    assert app._runtime_onebot_token == "recovered"
    assert app._runtime_onebot_credentials_trusted is True
    app.scheduler.shutdown()


@pytest.mark.unit
@pytest.mark.parametrize("secrets", [{}, {"onebot_token": ""}])
def test_valid_missing_or_empty_onebot_token_remains_explicit_anonymous_mode(
    temp_app_root: Path,
    secrets: dict[str, Any],
):
    from core.config import ConfigSnapshot

    app = XiaoQingApp(temp_app_root)
    snapshot = ConfigSnapshot(config=app.config, secrets=secrets, revision=1)

    assert _onebot_credentials(snapshot) == ("", True)
    app.scheduler.shutdown()


@pytest.mark.unit
def test_valid_secret_recovery_reenables_empty_or_bearer_credentials(temp_app_root: Path):
    from core.config import ConfigSnapshot, ConfigSourceStatus
    from core.onebot import OneBotHttpSender, OneBotWsClient

    app = XiaoQingApp(temp_app_root)
    config = app.config_manager.snapshot().mutable_config()
    config.update(
        {
            "onebot_http_base": "http://127.0.0.1:5700",
            "enable_ws_client": True,
            "onebot_ws_uri": "ws://127.0.0.1:6700",
        }
    )
    app.http_sender = OneBotHttpSender("http://127.0.0.1:5700", "old", MagicMock())
    app.ws_client = OneBotWsClient("ws://127.0.0.1:6700", "old")
    revoked = ConfigSnapshot(
        config=config,
        secrets={},
        revision=1,
        secrets_status=ConfigSourceStatus.MISSING,
    )
    anonymous = ConfigSnapshot(config=config, secrets={"onebot_token": ""}, revision=2)
    bearer = ConfigSnapshot(config=config, secrets={"onebot_token": "new-token"}, revision=3)

    app._apply_security_snapshot(revoked)
    assert app.ws_client.credentials_trusted is False
    app._apply_security_snapshot(anonymous)
    assert (app.ws_client.auth_token, app.ws_client.credentials_trusted) == ("", True)
    app._apply_security_snapshot(bearer)
    assert (app.ws_client.auth_token, app.ws_client.credentials_trusted) == (
        "new-token",
        True,
    )
    app.scheduler.shutdown()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_structural_inbound_failure_never_restores_revoked_token(temp_app_root: Path):
    app = XiaoQingApp(temp_app_root)
    current = InboundManager(
        inbound_http_base="http://127.0.0.1:12000",
        inbound_ws_uri="",
        token="old-inbound",
        handler=app._handle_inbound_event,
    )
    desired = InboundManager(
        inbound_http_base="http://127.0.0.1:12000",
        inbound_ws_uri="",
        token="",
        handler=app._handle_inbound_event,
        trusted_tls_proxy=True,
    )
    observed_tokens: list[str] = []

    async def stop_current() -> None:
        observed_tokens.append(current._token)

    current.stop = AsyncMock(side_effect=stop_current)
    current.start = AsyncMock(side_effect=lambda: observed_tokens.append(current._token))
    desired.start = AsyncMock(side_effect=OSError("candidate failed"))
    desired.stop = AsyncMock()
    app.inbound_manager = current

    with (
        patch("core.app_ingress.InboundManager.from_config", return_value=desired),
        pytest.raises(OSError, match="candidate failed"),
    ):
        await app._reconcile_inbound_manager({}, {"inbound_token": ""})

    assert observed_tokens == ["", ""]
    assert current._token == ""
    assert app.inbound_manager is current
    app.scheduler.shutdown()


@pytest.mark.unit
def test_endpoint_rotation_revokes_old_holders_without_pairing_them_with_new_tokens(
    temp_app_root: Path,
):
    from core.config import ConfigSnapshot
    from core.onebot import OneBotHttpSender, OneBotWsClient

    app = XiaoQingApp(temp_app_root)
    old_http = OneBotHttpSender("http://127.0.0.1:5700", "old-onebot", MagicMock())
    old_ws = OneBotWsClient("ws://127.0.0.1:6700", "old-onebot")
    old_inbound = InboundManager(
        inbound_http_base="http://127.0.0.1:12000",
        inbound_ws_uri="",
        token="old-inbound",
        handler=app._handle_inbound_event,
    )
    app.http_sender = old_http
    app.ws_client = old_ws
    app.inbound_manager = old_inbound
    config = app.config_manager.snapshot().mutable_config()
    config.update(
        {
            "onebot_http_base": "http://127.0.0.1:5701",
            "enable_ws_client": True,
            "onebot_ws_uri": "ws://127.0.0.1:6701",
            "enable_inbound_server": True,
            "inbound_http_base": "http://127.0.0.1:12001",
            "inbound_ws_uri": "",
        }
    )
    snapshot = ConfigSnapshot(
        config=config,
        secrets={"onebot_token": "new-onebot", "inbound_token": "new-inbound"},
        revision=1,
    )

    with patch("core.app_ingress.InboundManager.from_config") as inbound_factory:
        app._apply_config(snapshot)

    inbound_factory.assert_not_called()

    assert (old_http.http_base, old_http.auth_token) == ("http://127.0.0.1:5700", "")
    assert (old_ws.ws_uri, old_ws.auth_token) == ("ws://127.0.0.1:6700", "")
    assert old_http.credentials_trusted is False
    assert old_ws.credentials_trusted is False
    assert old_inbound._token == ""
    assert (old_http.http_base, old_http.auth_token) != (
        "http://127.0.0.1:5700",
        "new-onebot",
    )
    assert (old_ws.ws_uri, old_ws.auth_token) != (
        "ws://127.0.0.1:6700",
        "new-onebot",
    )
    app.scheduler.shutdown()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_new_revision_revokes_cancellation_resistant_provisional_inbound_candidate(
    temp_app_root: Path,
):
    from core.config import ConfigSnapshot

    app = XiaoQingApp(temp_app_root)
    app.http_session = MagicMock()
    candidate = InboundManager(
        inbound_http_base="http://127.0.0.1:12000",
        inbound_ws_uri="",
        token="stale-rev1",
        handler=app._handle_inbound_event,
    )
    start_entered = asyncio.Event()
    cancel_swallowed = asyncio.Event()
    start_release = asyncio.Event()

    async def resistant_start() -> None:
        start_entered.set()
        while not start_release.is_set():
            try:
                await start_release.wait()
            except asyncio.CancelledError:
                cancel_swallowed.set()

    candidate.start = AsyncMock(side_effect=resistant_start)
    candidate.stop = AsyncMock()
    config = app.config_manager.snapshot().mutable_config()
    config.update(
        {
            "enable_inbound_server": True,
            "inbound_http_base": "http://127.0.0.1:12000",
            "inbound_ws_uri": "",
        }
    )
    first_snapshot = ConfigSnapshot(
        config=config,
        secrets={"inbound_token": "stale-rev1"},
        revision=1,
    )
    disabled_config = dict(config)
    disabled_config["enable_inbound_server"] = False
    second_snapshot = ConfigSnapshot(
        config=disabled_config,
        secrets={"inbound_token": "rev2"},
        revision=2,
    )

    with patch("core.app_ingress.InboundManager.from_config", side_effect=[candidate, None]):
        app._apply_config(first_snapshot)
        first_task = app._config_apply_task
        assert first_task is not None
        await start_entered.wait()
        assert candidate in app._active_inbound_candidates()

        app._apply_config(second_snapshot)
        second_task = app._config_apply_task
        assert second_task is not None and second_task is not first_task
        # The synchronous security hook sees provisional candidates even while
        # their cancellation-resistant start coroutine still owns the lock.
        assert candidate._token == ""
        await cancel_swallowed.wait()
        assert not first_task.done()

        start_release.set()
        await asyncio.gather(first_task, second_task)

    assert app._active_inbound_candidates() == ()
    candidate.stop.assert_awaited_once()
    app.scheduler.shutdown()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_stale_config_task_that_swallows_cancel_cannot_reauthorize_old_secrets(
    temp_app_root: Path,
):
    from core.config import ConfigSnapshot

    app = XiaoQingApp(temp_app_root)
    app.http_session = MagicMock()
    auth_events: list[tuple[str, str]] = []
    http_sender = MagicMock(http_base="http://127.0.0.1:5700", auth_token="initial")
    ws_client = MagicMock(
        ws_uri="ws://127.0.0.1:6700",
        auth_token="initial",
        _queue_size=100,
    )
    inbound = MagicMock()

    def update_http(base: str, token: str, *, credentials_trusted: bool = True) -> None:
        http_sender.http_base = base
        http_sender.auth_token = token
        http_sender.credentials_trusted = credentials_trusted
        auth_events.append(("http", token))

    def update_ws(uri: str, token: str, *, credentials_trusted: bool = True) -> None:
        ws_client.ws_uri = uri
        ws_client.auth_token = token
        ws_client.credentials_trusted = credentials_trusted
        auth_events.append(("ws", token))

    def update_inbound(token: str) -> None:
        auth_events.append(("inbound", token))

    http_sender.update.side_effect = update_http
    ws_client.update.side_effect = update_ws
    inbound.update_token.side_effect = update_inbound
    app.http_sender = http_sender
    app.ws_client = ws_client
    app.inbound_manager = inbound
    app._reconcile_ws_client = AsyncMock()
    app._reconcile_inbound_manager = AsyncMock()
    first_entered = asyncio.Event()
    first_cancel_swallowed = asyncio.Event()
    first_release = asyncio.Event()

    async def reset_timezone(timezone: str) -> None:
        assert timezone == "UTC"
        first_entered.set()
        try:
            await first_release.wait()
        except asyncio.CancelledError:
            first_cancel_swallowed.set()
            await first_release.wait()

    app.scheduler.reset_async = AsyncMock(side_effect=reset_timezone)
    base_config = app.config_manager.snapshot().mutable_config()
    base_config.update(
        {
            "onebot_http_base": "http://127.0.0.1:5700",
            "enable_ws_client": True,
            "onebot_ws_uri": "ws://127.0.0.1:6700",
        }
    )
    old_snapshot = ConfigSnapshot(
        config={**base_config, "timezone": "UTC"},
        secrets={"onebot_token": "old-onebot", "inbound_token": "old-inbound"},
        revision=1,
    )
    new_snapshot = ConfigSnapshot(
        config={**base_config, "timezone": "Asia/Shanghai"},
        secrets={"onebot_token": "new-onebot", "inbound_token": "new-inbound"},
        revision=2,
    )

    app._apply_config(old_snapshot)
    first_task = app._config_apply_task
    assert first_task is not None
    await first_entered.wait()

    app._apply_config(new_snapshot)
    second_task = app._config_apply_task
    assert second_task is not None and second_task is not first_task
    await first_cancel_swallowed.wait()
    await second_task

    first_release.set()
    await first_task
    first_new_event = next(
        index for index, (_holder, token) in enumerate(auth_events) if token == "new-onebot"
    )
    assert all(
        token not in {"old-onebot", "old-inbound"}
        for _holder, token in auth_events[first_new_event:]
    )
    assert http_sender.auth_token == "new-onebot"
    assert ws_client.auth_token == "new-onebot"
    assert http_sender.credentials_trusted is True
    assert ws_client.credentials_trusted is True

    # Even an explicit delayed callback cannot move the accepted revision back.
    app._apply_config(old_snapshot)
    assert http_sender.auth_token == "new-onebot"
    assert ws_client.auth_token == "new-onebot"
    assert http_sender.credentials_trusted is True
    assert ws_client.credentials_trusted is True
    app.scheduler.shutdown()
