# 验证 Web 服务启动、停止、认证与连接回收。
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from plugins.pendo.services.db import Database
from plugins.pendo.utils import db_ops
from plugins.pendo.web import auth, deps
from plugins.pendo.web import server as web_server


class _FakeThread:
    def __init__(self, *, stop_on_join: bool) -> None:
        self.alive        = True
        self.stop_on_join = stop_on_join

    def is_alive(self) -> bool:
        return self.alive

    def join(self, timeout: float) -> None:
        del timeout
        if self.stop_on_join:
            self.alive = False


class _NeverStartedServer:
    """替身 uvicorn.Server：不监听端口，``run`` 一直阻塞到被要求退出，且永远不报告已启动。"""

    def __init__(self, _config: object) -> None:
        self.started     = False
        self.should_exit = False
        self.force_exit  = False
        self.running     = threading.Event()
        self.finished    = threading.Event()

    def run(self) -> None:
        self.running.set()
        while not self.should_exit:
            time.sleep(0.001)
        self.finished.set()


def test_start_timeout_stops_thread_before_clearing_owned_state(
    monkeypatch: pytest.MonkeyPatch,
    db: Database,
) -> None:
    servers: list[_NeverStartedServer] = []

    def make_server(config: object) -> _NeverStartedServer:
        servers.append(_NeverStartedServer(config))
        return servers[0]

    # uvicorn 是第三方边界：用不监听端口的服务器替身，其余（应用、线程、状态锁）走真实代码。
    monkeypatch.setattr(web_server.uvicorn, "Server", make_server)
    for name in ("_app", "_server", "_thread", "_last_error"):
        monkeypatch.setattr(web_server, name, None)
    # create_app 会绑定数据库依赖，结束后把这些模块级引用还原。
    monkeypatch.setattr(deps, "_db_instance", None)
    monkeypatch.setattr(db_ops, "_db_singleton", None)
    monkeypatch.setattr(auth, "_AUTH_DATABASE", None)

    assert web_server.start(db) is False

    server = servers[0]
    assert server.running.is_set()
    assert server.should_exit is True
    assert server.finished.is_set()
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


def test_static_images_and_fonts_carry_their_media_types(client) -> None:
    """登录页的夜景图和像素字体带着正确的类型送出，不依赖解释器自带的类型表认不认识它们。"""

    assert client.get("/assets/diary-desk.webp").headers["content-type"] == "image/webp"
    assert client.get("/assets/fusion-pixel.woff2").headers["content-type"] == "font/woff2"
