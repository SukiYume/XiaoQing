"""核心生命周期任务的取消与致命异常处理。

应用和入站服务器都需要在调用方被取消后继续完成自己拥有的回滚任务。本模块集中维护这组
语义，避免两套实现随时间产生差异。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any


class LazyAsyncLock:
    """在首次异步操作时创建锁，供可在同步阶段构造的长期对象使用。"""

    __slots__ = ("_lock",)

    def __init__(self) -> None:
        self._lock: asyncio.Lock | None = None

    def get(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock


class FatalErrorCarrier(Exception):
    """用普通 ``Exception`` 安全承载子任务抛出的任意 ``BaseException``。"""

    def __init__(self, original: BaseException) -> None:
        super().__init__(f"{type(original).__name__}: {original}")
        self.original = original


class OwnedTaskFatalError(FatalErrorCarrier):
    """把子任务中的 ``BaseException`` 安全带回拥有者任务。

    ``asyncio`` 会把未包装的致命异常视为任务级失败；先放进普通 ``Exception``，可让拥有者
    完成资源收敛后再按原类型处理。
    """


class DeferredCancellation:
    """记录调用方取消请求，等托管任务收敛后再重新抛出。"""

    def __init__(self) -> None:
        self.error: asyncio.CancelledError | None = None

    def capture(self, error: asyncio.CancelledError) -> None:
        if self.error is None:
            self.error = error

    def raise_if_requested(self, *, cause: BaseException | None = None) -> None:
        if self.error is not None:
            raise self.error from cause


async def run_owned_operation(operation_factory: Callable[[], Awaitable[Any]]) -> Any:
    """运行拥有者负责收尾的操作，并安全承载非普通异常。"""

    try:
        return await operation_factory()
    except Exception:
        # 普通异常保持原类型；后一个分支只负责 CancelledError 等 BaseException。
        raise
    except BaseException as exc:
        raise OwnedTaskFatalError(exc) from None


def unwrap_owned_failure(error: BaseException) -> BaseException:
    """从任务安全包装中取回原始异常。"""

    if isinstance(error, OwnedTaskFatalError):
        return error.original
    return error


async def await_owned_task(
    task: asyncio.Task[Any],
    cancellation: DeferredCancellation,
) -> Any:
    """即使调用方反复取消，也等待其拥有的收尾任务完成。"""

    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancellation.capture(exc)
            if task.done():
                break
            continue
    return task.result()
