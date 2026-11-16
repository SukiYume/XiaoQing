"""入站监听候选切换、回滚和取消收敛。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
import pytest_asyncio
from aiohttp import ClientConnectorError

import tests.helpers.app_test_support as _fixture_support
from tests.helpers.app_test_support import (
    ApplicationLifecycleFatalError,
    ClientSession,
    InboundManager,
    InboundReconcileError,
    XiaoQingApp,
)

temp_app_root = _fixture_support.temp_app_root

SECRETS = {"inbound_token": "token"}


def _listener_config(
    port: int,
    *,
    ws_port: int | None     = None,
    trusted_tls_proxy: bool = False,
) -> dict[str, Any]:
    """一份真实的入站监听配置：HTTP 在 ``port``，可选再加一个独立端口的 WebSocket。"""

    return {
        "enable_inbound_server"    : True,
        "inbound_http_base"        : f"http://127.0.0.1:{port}",
        "inbound_ws_uri"           : "" if ws_port is None else f"ws://127.0.0.1:{ws_port}/ws",
        "inbound_trusted_tls_proxy": trusted_tls_proxy,
    }


def _write_config(root: Path, updates: Mapping[str, Any]) -> None:
    _update_json(root / "config" / "config.json", updates)


def _write_secrets(root: Path, updates: Mapping[str, Any]) -> None:
    _update_json(root / "config" / "secrets.json", updates)


def _update_json(path: Path, updates: Mapping[str, Any]) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(updates)
    path.write_text(json.dumps(data), encoding="utf-8")


async def _wait_for_config_apply(app: XiaoQingApp) -> None:
    """等 /reload 发布的配置在事件循环上被应用完；应用失败时把异常抛出来。"""

    for _ in range(100):
        if app._config_apply_tasks:
            break
        await asyncio.sleep(0)
    await asyncio.gather(*app._config_apply_tasks)


def _install_published_listener(app: XiaoQingApp, manager: InboundManager) -> None:
    """把 ``manager`` 装成正在运行、且已发布配置声明的就是它的监听器（凭据对它有效）。"""

    with app._runtime_auth_lock:
        app.inbound_manager        = manager
        app._runtime_inbound_key   = manager.config_key
        app._runtime_inbound_token = SECRETS["inbound_token"]


async def _health_status(port: int, token: str = "token") -> int:
    async with (
        ClientSession() as session,
        session.get(
            f"http://127.0.0.1:{port}/health",
            headers={"Authorization": f"Bearer {token}"},
        ) as response,
    ):
        return int(response.status)


@pytest_asyncio.fixture
async def start_app(temp_app_root: Path):
    """按已发布的配置真实启动应用（入站监听随配置启动）；用例结束时停掉所有应用。"""

    apps: list[XiaoQingApp] = []

    async def start(inbound_config: Mapping[str, Any] | None = None) -> XiaoQingApp:
        _write_config(temp_app_root, inbound_config or {})
        _write_secrets(temp_app_root, SECRETS)
        app = XiaoQingApp(temp_app_root)
        apps.append(app)
        await app.start()
        return app

    yield start

    for app in apps:
        await app.stop()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_reconcile_inbound_restarts_when_proxy_security_declaration_changes(
    start_app: Callable[..., Awaitable[XiaoQingApp]],
    unused_tcp_port_factory,
):
    port    = unused_tcp_port_factory()
    app     = await start_app(_listener_config(port))
    current = app.inbound_manager
    assert current is not None
    assert await _health_status(port) == 200

    await app._reconcile_inbound_manager(_listener_config(port, trusted_tls_proxy=True), SECRETS)

    replacement = app.inbound_manager
    assert replacement is not None
    assert replacement is not current
    assert replacement.config_key[-1] is True
    # 同一个端口只能由一个监听器占用：新监听器能响应，说明旧的已经停掉并放开了端口。
    assert current.http_server is None
    assert await _health_status(port) == 200


@pytest.mark.asyncio
@pytest.mark.integration
async def test_invalid_inbound_config_never_stops_current(
    start_app: Callable[..., Awaitable[XiaoQingApp]],
    unused_tcp_port_factory,
):
    port    = unused_tcp_port_factory()
    app     = await start_app(_listener_config(port))
    current = app.inbound_manager
    assert current is not None

    invalid = {**_listener_config(port), "inbound_trusted_tls_proxy": "yes"}
    with pytest.raises(ValueError):
        await app._reconcile_inbound_manager(invalid, SECRETS)

    assert app.inbound_manager is current
    # 旧监听器没有被停：端口仍在响应，只是配置无效时它的凭据已被收回。
    assert await _health_status(port) == 401


@pytest.mark.asyncio
async def test_failed_candidate_cleanup_remains_owned_until_terminal_cleanup(
    temp_app_root: Path,
):
    app     = XiaoQingApp(temp_app_root)
    current = InboundManager(
        inbound_http_base = "http://127.0.0.1:12000",
        inbound_ws_uri    = "",
        token             = "token",
        handler           = app._handle_inbound_event,
    )
    desired = InboundManager(
        inbound_http_base = "http://127.0.0.1:12001",
        inbound_ws_uri    = "",
        token             = "token",
        handler           = app._handle_inbound_event,
    )
    current.stop = AsyncMock()
    desired.start = AsyncMock(side_effect=OSError("bind failed"))
    desired.stop = AsyncMock(side_effect=[RuntimeError("cleanup failed"), None])
    _install_published_listener(app, current)

    with (
        patch("core.app_ingress.InboundManager.from_config", return_value=desired),
        pytest.raises(OSError, match="bind failed"),
    ):
        await app._reconcile_inbound_manager({}, {"inbound_token": "token"})

    assert app.inbound_manager is current
    assert app._inbound_cleanup_pending == [desired]

    await app.stop()

    assert app._inbound_cleanup_pending == []
    assert desired.stop.await_count == 2
    current.stop.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_disjoint_occupied_port_switch_keeps_old_health_endpoint_live(
    start_app: Callable[..., Awaitable[XiaoQingApp]],
    unused_tcp_port_factory,
):
    old_port      = unused_tcp_port_factory()
    occupied_port = unused_tcp_port_factory()
    app           = await start_app(_listener_config(old_port))
    current       = app.inbound_manager
    assert current is not None
    occupied = await asyncio.start_server(
        lambda _reader, writer: writer.close(), "127.0.0.1", occupied_port
    )

    try:
        with pytest.raises(OSError):
            await app._reconcile_inbound_manager(_listener_config(occupied_port), SECRETS)

        # 新端口绑不上：旧监听器一直没停，带着当前的凭据继续收消息。
        assert app.inbound_manager is current
        assert app._inbound_cleanup_pending == []
        assert await _health_status(old_port) == 200
    finally:
        occupied.close()
        await occupied.wait_closed()


@pytest.mark.asyncio
async def test_disjoint_inbound_switch_starts_candidate_before_stopping_current(
    temp_app_root: Path,
):
    app     = XiaoQingApp(temp_app_root)
    current = InboundManager(
        inbound_http_base = "http://127.0.0.1:12000",
        inbound_ws_uri    = "",
        token             = "token",
        handler           = app._handle_inbound_event,
    )
    desired = InboundManager(
        inbound_http_base = "http://127.0.0.1:12001",
        inbound_ws_uri    = "",
        token             = "token",
        handler           = app._handle_inbound_event,
    )
    events: list[str] = []
    desired.start     = AsyncMock(
        side_effect=lambda *, accept_events=True: events.append(f"new.start:{accept_events}")
    )
    desired.commit_admission = Mock(side_effect=lambda: events.append("new.commit"))
    current.stop = AsyncMock(side_effect=lambda: events.append("old.stop"))
    _install_published_listener(app, current)

    with patch("core.app_ingress.InboundManager.from_config", return_value=desired):
        await app._reconcile_inbound_manager({}, {"inbound_token": "token"})

    assert events == ["new.start:False", "old.stop", "new.commit"]
    assert app.inbound_manager is desired
    app.scheduler.shutdown()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_overlapping_candidate_failure_restores_current(
    start_app: Callable[..., Awaitable[XiaoQingApp]],
    unused_tcp_port_factory,
):
    port    = unused_tcp_port_factory()
    ws_port = unused_tcp_port_factory()
    app     = await start_app(_listener_config(port))
    current = app.inbound_manager
    assert current is not None
    occupied = await asyncio.start_server(
        lambda _reader, writer: writer.close(), "127.0.0.1", ws_port
    )

    try:
        # 候选沿用 HTTP 端口再加一个被占用的 WebSocket 端口：旧监听器必须先让出 HTTP 端口，
        # 候选绑完 HTTP 后在 WebSocket 端口上失败，旧监听器随后被重新拉起。
        with pytest.raises(OSError):
            await app._reconcile_inbound_manager(
                _listener_config(port, ws_port=ws_port),
                SECRETS,
            )

        assert app.inbound_manager is current
        assert app._inbound_cleanup_pending == []
        assert await _health_status(port) == 200
    finally:
        occupied.close()
        await occupied.wait_closed()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_reload_whose_new_listener_cannot_bind_keeps_the_old_one_until_a_later_reload(
    start_app: Callable[..., Awaitable[XiaoQingApp]],
    temp_app_root: Path,
    unused_tcp_port_factory,
):
    port    = unused_tcp_port_factory()
    ws_port = unused_tcp_port_factory()
    app     = await start_app(_listener_config(port))
    current = app.inbound_manager
    assert current is not None
    occupied = await asyncio.start_server(
        lambda _reader, writer: writer.close(), "127.0.0.1", ws_port
    )

    try:
        # 新配置沿用 HTTP 端口、再加一个被占用的 WebSocket 端口：旧监听器先让出 HTTP 端口，候选在
        # WebSocket 端口上失败；报出来的是绑定失败本身，旧监听器在原地址重新启动、继续收消息。
        _write_config(temp_app_root, _listener_config(port, ws_port=ws_port))
        app.reload_config()
        with pytest.raises(OSError):
            await _wait_for_config_apply(app)

        assert app.inbound_manager is current
        assert app._inbound_cleanup_pending == []
        assert await _health_status(port) == 200
    finally:
        occupied.close()
        await occupied.wait_closed()

    # 端口空出来后再 /reload 一次（配置没改），这次换成新声明的监听器。
    app.reload_config()
    await _wait_for_config_apply(app)

    assert app.inbound_manager is not current
    assert await _health_status(port) == 200
    _reader, writer = await asyncio.open_connection("127.0.0.1", ws_port)
    writer.close()
    await writer.wait_closed()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_listener_kept_after_a_failed_switch_accepts_only_the_current_token(
    start_app: Callable[..., Awaitable[XiaoQingApp]],
    temp_app_root: Path,
    unused_tcp_port_factory,
):
    old_port = unused_tcp_port_factory()
    new_port = unused_tcp_port_factory()
    app      = await start_app(_listener_config(old_port))
    occupied = await asyncio.start_server(
        lambda _reader, writer: writer.close(), "127.0.0.1", new_port
    )

    try:
        # 同一次 /reload 换了端口也换了凭据：顶替的旧监听器只认新凭据，旧凭据不会回来。
        _write_config(temp_app_root, _listener_config(new_port))
        _write_secrets(temp_app_root, {"inbound_token": "rotated"})
        app.reload_config()
        with pytest.raises(OSError):
            await _wait_for_config_apply(app)

        assert await _health_status(old_port, token="rotated") == 200
        assert await _health_status(old_port, token="token") == 401
    finally:
        occupied.close()
        await occupied.wait_closed()

    app.reload_config()
    await _wait_for_config_apply(app)

    assert await _health_status(new_port, token="rotated") == 200
    with pytest.raises(ClientConnectorError):
        await _health_status(old_port, token="rotated")


@pytest.mark.asyncio
async def test_restore_fatal_and_cleanup_failure_remain_owned_until_stop_retry(
    temp_app_root: Path,
):
    class RestoreFatal(BaseException):
        pass

    app     = XiaoQingApp(temp_app_root)
    current = InboundManager(
        inbound_http_base = "http://127.0.0.1:12000",
        inbound_ws_uri    = "",
        token             = "token",
        handler           = app._handle_inbound_event,
    )
    desired = InboundManager(
        inbound_http_base = "http://127.0.0.1:12000",
        inbound_ws_uri    = "",
        token             = "token",
        handler           = app._handle_inbound_event,
        trusted_tls_proxy = True,
    )
    fatal = RestoreFatal("restore fatal")
    current.stop = AsyncMock(side_effect=[None, RuntimeError("old cleanup failed"), None])
    current.start = AsyncMock(side_effect=fatal)
    desired.start = AsyncMock(side_effect=OSError("candidate failed"))
    desired.stop = AsyncMock()
    _install_published_listener(app, current)

    with patch("core.app_ingress.InboundManager.from_config", return_value=desired):
        task = asyncio.create_task(app._reconcile_inbound_manager({}, {"inbound_token": "token"}))
        with pytest.raises(InboundReconcileError) as exc_info:
            await task

    assert exc_info.value.restore_error is fatal
    assert app.inbound_manager is None
    assert app._inbound_cleanup_pending == [current]

    await app.stop()
    assert app._lifecycle_state.value == "failed"
    assert app._inbound_cleanup_pending == [current]

    await app.stop()
    assert app._lifecycle_state.value == "stopped"
    assert app._inbound_cleanup_pending == []
    assert current.stop.await_count == 3


@pytest.mark.asyncio
async def test_inbound_candidate_fatal_is_task_safe_and_keeps_current(temp_app_root: Path):
    class CandidateFatal(BaseException):
        pass

    app     = XiaoQingApp(temp_app_root)
    current = InboundManager(
        inbound_http_base = "http://127.0.0.1:12000",
        inbound_ws_uri    = "",
        token             = "token",
        handler           = app._handle_inbound_event,
    )
    desired = InboundManager(
        inbound_http_base = "http://127.0.0.1:12001",
        inbound_ws_uri    = "",
        token             = "token",
        handler           = app._handle_inbound_event,
    )
    fatal        = CandidateFatal("candidate fatal")
    current.stop = AsyncMock()
    desired.start = AsyncMock(side_effect=fatal)
    desired.stop = AsyncMock()
    _install_published_listener(app, current)

    with patch("core.app_ingress.InboundManager.from_config", return_value=desired):
        task = asyncio.create_task(app._reconcile_inbound_manager({}, {"inbound_token": "token"}))
        with pytest.raises(ApplicationLifecycleFatalError) as exc_info:
            await task

    assert exc_info.value.original is fatal
    desired.stop.assert_awaited_once()
    current.stop.assert_not_awaited()
    assert app.inbound_manager is current
    app.scheduler.shutdown()


@pytest.mark.asyncio
async def test_inbound_reconcile_cancellation_finishes_rollback(temp_app_root: Path):
    app     = XiaoQingApp(temp_app_root)
    current = InboundManager(
        inbound_http_base = "http://127.0.0.1:12000",
        inbound_ws_uri    = "",
        token             = "token",
        handler           = app._handle_inbound_event,
    )
    desired = InboundManager(
        inbound_http_base = "http://127.0.0.1:12000",
        inbound_ws_uri    = "",
        token             = "token",
        handler           = app._handle_inbound_event,
        trusted_tls_proxy = True,
    )
    entered       = asyncio.Event()
    blocker       = asyncio.Event()
    current.stop  = AsyncMock()
    current.start = AsyncMock()
    desired.stop  = AsyncMock()

    async def block_candidate() -> None:
        entered.set()
        await blocker.wait()

    desired.start = AsyncMock(side_effect=block_candidate)
    _install_published_listener(app, current)

    with patch("core.app_ingress.InboundManager.from_config", return_value=desired):
        task = asyncio.create_task(app._reconcile_inbound_manager({}, {"inbound_token": "token"}))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    desired.stop.assert_awaited_once()
    current.start.assert_awaited_once()
    assert app.inbound_manager is current
    app.scheduler.shutdown()


@pytest.mark.asyncio
async def test_repeated_cancellation_during_restore_wins_after_rollback(temp_app_root: Path):
    app     = XiaoQingApp(temp_app_root)
    current = InboundManager(
        inbound_http_base = "http://127.0.0.1:12000",
        inbound_ws_uri    = "",
        token             = "token",
        handler           = app._handle_inbound_event,
    )
    desired = InboundManager(
        inbound_http_base = "http://127.0.0.1:12000",
        inbound_ws_uri    = "",
        token             = "token",
        handler           = app._handle_inbound_event,
        trusted_tls_proxy = True,
    )
    restore_entered = asyncio.Event()
    restore_release = asyncio.Event()
    current.stop    = AsyncMock()

    async def restore_current() -> None:
        restore_entered.set()
        await restore_release.wait()

    current.start = AsyncMock(side_effect=restore_current)
    desired.start = AsyncMock(side_effect=OSError("candidate failed"))
    desired.stop = AsyncMock()
    _install_published_listener(app, current)

    with patch("core.app_ingress.InboundManager.from_config", return_value=desired):
        task = asyncio.create_task(app._reconcile_inbound_manager({}, {"inbound_token": "token"}))
        await restore_entered.wait()
        task.cancel()
        task.cancel()
        await asyncio.sleep(0)
        restore_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert task.cancelling() >= 2
    assert app.inbound_manager is current
    desired.stop.assert_awaited_once()
    current.start.assert_awaited_once()
    app.scheduler.shutdown()
