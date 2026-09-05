# 验证动态并发额度调整时等待者、持有者和取消的计数一致性。
from __future__ import annotations

import asyncio

import pytest

from core.dispatcher import AdjustableSemaphore


@pytest.mark.asyncio
async def test_adjustable_semaphore_grants_waiters_in_fifo_order_one_slot_at_a_time() -> None:
    limiter = AdjustableSemaphore(1)
    await limiter.acquire()
    order: list[int] = []

    async def acquire(index: int) -> None:
        await limiter.acquire()
        order.append(index)

    tasks = [asyncio.create_task(acquire(index)) for index in range(3)]
    await asyncio.sleep(0)
    queued = tuple(limiter._waiters)
    assert len(queued) == 3

    limiter.release()
    assert [waiter.done() for waiter in queued] == [True, False, False]
    await asyncio.sleep(0)
    assert order == [0]

    limiter.release()
    await asyncio.sleep(0)
    assert order == [0, 1]

    limiter.release()
    await asyncio.gather(*tasks)
    assert order == [0, 1, 2]
    limiter.release()
    assert limiter.in_use == 0


@pytest.mark.asyncio
async def test_adjustable_semaphore_resize_wakes_only_new_fifo_capacity() -> None:
    limiter = AdjustableSemaphore(1)
    await limiter.acquire()
    order: list[int] = []

    async def acquire(index: int) -> None:
        await limiter.acquire()
        order.append(index)

    tasks = [asyncio.create_task(acquire(index)) for index in range(4)]
    await asyncio.sleep(0)
    queued = tuple(limiter._waiters)

    limiter.resize(3)

    assert [waiter.done() for waiter in queued] == [True, True, False, False]
    await asyncio.sleep(0)
    assert order == [0, 1]
    assert limiter.in_use == 3

    limiter.release()
    await asyncio.sleep(0)
    assert order == [0, 1, 2]
    limiter.release()
    await asyncio.gather(*tasks)
    assert order == [0, 1, 2, 3]
    limiter.release()
    limiter.release()
    limiter.release()
    assert limiter.in_use == 0


@pytest.mark.asyncio
async def test_adjustable_semaphore_reports_over_capacity_after_shrink() -> None:
    limiter = AdjustableSemaphore(2)
    await limiter.acquire()
    await limiter.acquire()

    limiter.resize(1)

    assert limiter.over_capacity() is True
    limiter.release()
    assert limiter.over_capacity() is False
    limiter.release()


@pytest.mark.asyncio
async def test_adjustable_semaphore_cancellation_preserves_fifo_capacity() -> None:
    limiter = AdjustableSemaphore(1)
    await limiter.acquire()
    order: list[int] = []

    async def acquire(index: int) -> None:
        await limiter.acquire()
        order.append(index)

    first     = asyncio.create_task(acquire(0))
    cancelled = asyncio.create_task(acquire(1))
    last      = asyncio.create_task(acquire(2))
    await asyncio.sleep(0)

    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled

    limiter.release()
    await first
    assert order == [0]

    limiter.release()
    await last
    assert order == [0, 2]
    limiter.release()
    assert limiter.in_use == 0
