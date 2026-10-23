"""核心并发测试共享的异步控制工具。"""

from __future__ import annotations

import asyncio
import functools
import sys
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any


def run_with_subprocess_loop(coroutine_factory: Callable[[], Coroutine[Any, Any, Any]]) -> None:
    """真实子进程需要 Proactor 循环；测试默认的 Selector 循环在 Windows 上不支持子进程。"""

    factory = asyncio.ProactorEventLoop if sys.platform == "win32" else asyncio.new_event_loop
    with asyncio.Runner(loop_factory=factory) as runner:
        runner.run(coroutine_factory())


async def wait_for_cancellation_then_release(
    started: asyncio.Event,
    cancellation_seen: asyncio.Event,
    release: asyncio.Event,
) -> None:
    """保持运行直至收到取消，再等待测试显式允许退出。"""
    started.set()
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        cancellation_seen.set()
        await release.wait()


def cancellation_then_release_callback(
    started: asyncio.Event,
    cancellation_seen: asyncio.Event,
    release: asyncio.Event,
) -> Callable[[], Awaitable[None]]:
    """构造无参数的“收到取消后再释放”协程回调。"""
    return functools.partial(
        wait_for_cancellation_then_release,
        started,
        cancellation_seen,
        release,
    )


async def resist_cancellation_until_released(
    entered: asyncio.Event,
    cancellation_seen: asyncio.Event,
    release: asyncio.Event,
) -> None:
    """记录每次取消并继续等待，直到测试显式释放。"""
    entered.set()
    while not release.is_set():
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancellation_seen.set()


def cancellation_resistant_callback(
    entered: asyncio.Event,
    cancellation_seen: asyncio.Event,
    release: asyncio.Event,
) -> Callable[[], Awaitable[None]]:
    """构造无参数的取消抵抗协程回调。"""
    return functools.partial(
        resist_cancellation_until_released,
        entered,
        cancellation_seen,
        release,
    )


class BlockingConcurrencyProbe:
    """在释放事件前阻塞调用，并记录观察到的最大并发数。"""

    def __init__(self, entered: asyncio.Event, release: asyncio.Event) -> None:
        self.entered        = entered
        self.release        = release
        self.active         = 0
        self.maximum_active = 0

    async def run(self, *_args: Any) -> list[Any]:
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        self.entered.set()
        try:
            await self.release.wait()
        finally:
            self.active -= 1
        return []
