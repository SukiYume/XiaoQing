"""jupyter 插件测试共用的 jupyter_client 替身。

真实边界是第三方的 ``KernelManager``（启动、打断、关闭内核）和它给出的内核客户端（提交代码、
读取 iopub 消息）；插件的执行、取消、清理与输出整理都走真实代码。这里不在导入时加载任何
``plugins.jupyter`` 模块，调用方按自己的时机导入插件。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from queue import Empty as QueueEmpty
from typing import Any

import pytest


class FakeKernelClient:
    """jupyter_client 内核客户端替身：第一次提交是启动时的 matplotlib 初始化，只回放 idle。

    其后每次提交由 ``responder(code)`` 给出要回放的 iopub 消息（缺省不输出），再补一条 idle；
    ``fail_submit`` 非空时，启动之后的提交直接抛出该文字的异常。
    """

    def __init__(
        self,
        responder: Callable[[str], list[dict[str, Any]]] | None = None,
        *,
        fail_submit: str | None = None,
    ) -> None:
        self.responder                      = responder or (lambda _code: [])
        self.fail_submit                    = fail_submit
        self.executed: list[str]            = []
        self._pending: list[dict[str, Any]] = []

    def start_channels(self) -> None:
        return None

    def wait_for_ready(self, timeout: float) -> None:
        assert timeout == 30

    def stop_channels(self) -> None:
        return None

    def execute(self, code: str) -> str:
        self.executed.append(code)
        msg_id                     = f"msg-{len(self.executed)}"
        body: list[dict[str, Any]] = []
        if len(self.executed) > 1:
            if self.fail_submit:
                raise RuntimeError(self.fail_submit)
            body = self.responder(code)
        idle          = {"msg_type": "status", "content": {"execution_state": "idle"}}
        self._pending = [
            {**message, "parent_header": {"msg_id": msg_id}} for message in (*body, idle)
        ]
        return msg_id

    def get_iopub_msg(self, timeout: float) -> dict[str, Any]:
        del timeout
        if not self._pending:
            raise QueueEmpty
        return self._pending.pop(0)


class FakeKernel:
    """jupyter_client 内核管理器替身：记录启动、打断与关闭，可设置启动、打断、关闭失败。

    ``alive`` 是创建时内核是否已在运行（经 ``start_kernel`` 启动的内核总是存活）；
    ``block_start`` 让启动卡在线程屏障上（内核已起来，启动调用还没返回）；
    ``fail_start`` / ``interrupt_fails`` / ``shutdown_fails`` 是对应调用抛出的异常文字，
    ``shutdown_fails`` 可在测试里随时改写。不给 ``client`` 时每次 ``client()`` 返回新的默认客户端。
    """

    kernel_name = "python3"

    def __init__(
        self,
        client: FakeKernelClient | None = None,
        *,
        alive: bool                                                 = False,
        block_start: tuple[threading.Event, threading.Event] | None = None,
        fail_start: str | None                                      = None,
        interrupt_fails: str | None                                 = None,
        shutdown_fails: str | None                                  = None,
    ) -> None:
        self.client_instance = client
        self.alive           = alive
        self.block_start     = block_start
        self.fail_start      = fail_start
        self.interrupt_fails = interrupt_fails
        self.shutdown_fails  = shutdown_fails
        self.interrupt_calls = 0
        self.shutdown_calls  = 0

    def start_kernel(self) -> None:
        if self.fail_start:
            raise RuntimeError(self.fail_start)
        self.alive = True
        if self.block_start is not None:
            entered, release = self.block_start
            entered.set()
            assert release.wait(timeout=2)

    def client(self) -> FakeKernelClient:
        return self.client_instance or FakeKernelClient()

    def is_alive(self) -> bool:
        return self.alive

    def interrupt_kernel(self) -> None:
        self.interrupt_calls += 1
        if self.interrupt_fails:
            raise RuntimeError(self.interrupt_fails)

    def shutdown_kernel(self, now: bool = False) -> None:
        assert now is True
        self.shutdown_calls += 1
        if self.shutdown_fails:
            raise RuntimeError(self.shutdown_fails)
        self.alive = False

    def cleanup_resources(self, restart: bool = False) -> None:
        assert restart is False


class KernelBackend:
    """替换 ``KernelManager`` 类的工厂：每次启动内核产出一个替身，并记下来供测试查看。

    ``responder`` / ``block_start`` / ``fail_start`` 在测试里改写，之后产出的内核按它们行事。
    """

    def __init__(self) -> None:
        self.kernels: list[FakeKernel]                                   = []
        self.responder: Callable[[str], list[dict[str, Any]]] | None     = None
        self.block_start: tuple[threading.Event, threading.Event] | None = None
        self.fail_start: str | None                                      = None

    def __call__(self, **_kwargs: Any) -> FakeKernel:
        kernel = FakeKernel(
            FakeKernelClient(self.responder),
            block_start = self.block_start,
            fail_start  = self.fail_start,
        )
        self.kernels.append(kernel)
        return kernel


def install_kernel_backend(
    monkeypatch: pytest.MonkeyPatch,
    factory: Callable[..., Any],
) -> None:
    """让插件认为 jupyter_client 可用，并用 ``factory`` 顶替第三方的 ``KernelManager`` 类。

    同时清掉插件缓存的依赖探测结果，使用例按这里装好的替身重新探测。
    """

    from plugins.jupyter import jupyter_manager
    from plugins.jupyter import main as jupyter_main

    monkeypatch.setattr(jupyter_manager, "JUPYTER_AVAILABLE", True)
    monkeypatch.setattr(jupyter_manager, "IMPORT_ERROR", None)
    monkeypatch.setattr(jupyter_manager, "KernelManager", factory)
    monkeypatch.setattr(jupyter_main, "_DEPENDENCY_PROBED", False)
