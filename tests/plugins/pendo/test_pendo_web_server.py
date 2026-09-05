# 验证 Web 服务启动、停止、认证与连接回收。
from __future__ import annotations

from types import SimpleNamespace

import pytest

from plugins.pendo.web import server as web_server
from tests.helpers.pendo_test_support import reset_pendo_runtime_config


@pytest.fixture(autouse=True)
def _reset_runtime_config():
    reset_pendo_runtime_config()
    yield
    reset_pendo_runtime_config()


class _FakeThread:
    def __init__(self, *, stop_on_join: bool) -> None:
        self.alive        = True
        self.stop_on_join = stop_on_join
        self.started      = False

    def start(self) -> None:
        self.started = True

    def is_alive(self) -> bool:
        return self.alive

    def join(self, timeout: float) -> None:
        del timeout
        if self.stop_on_join:
            self.alive = False


def test_start_timeout_stops_thread_before_clearing_owned_state(monkeypatch) -> None:
    fake_server = SimpleNamespace(started=False, should_exit=False, force_exit=False)
    fake_thread = _FakeThread(stop_on_join=True)

    monkeypatch.setattr(web_server, "create_app", lambda _db: object())
    monkeypatch.setattr(web_server.uvicorn, "Config", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(web_server.uvicorn, "Server", lambda _config: fake_server)
    monkeypatch.setattr(web_server.threading, "Thread", lambda **_kwargs: fake_thread)
    monkeypatch.setattr(web_server.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(web_server, "_app", None)
    monkeypatch.setattr(web_server, "_server", None)
    monkeypatch.setattr(web_server, "_thread", None)

    assert web_server.start(object()) is False
    assert fake_thread.started is True
    assert fake_server.should_exit is True
    assert web_server._server is None
    assert web_server._thread is None


def test_stop_keeps_owned_state_when_thread_cannot_be_reaped(monkeypatch) -> None:
    fake_server = SimpleNamespace(started=True, should_exit=False, force_exit=False)
    fake_thread = _FakeThread(stop_on_join=False)
    monkeypatch.setattr(web_server, "_server", fake_server)
    monkeypatch.setattr(web_server, "_thread", fake_thread)

    assert web_server.stop(timeout=0.01) is False
    assert fake_server.should_exit is True
    assert fake_server.force_exit is True
    assert web_server._server is fake_server
    assert web_server._thread is fake_thread


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), True])
def test_server_timeouts_must_be_positive_and_finite(timeout) -> None:
    with pytest.raises((TypeError, ValueError)):
        web_server.stop(timeout=timeout)
    with pytest.raises((TypeError, ValueError)):
        web_server.is_reachable(timeout=timeout)


def test_get_url_brackets_ipv6_loopback() -> None:
    web_server.PendoConfig.configure({"web_host": "::1", "web_port": 8765})

    assert web_server.get_url() == "http://[::1]:8765"
