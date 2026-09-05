# 验证按键锁的等待、回收及并发互斥契约。
from __future__ import annotations

import asyncio

import pytest

from core.async_keyed_lock import AsyncKeyedLockPool


@pytest.mark.asyncio
async def test_high_cardinality_keys_are_removed_after_use() -> None:
    pool = AsyncKeyedLockPool(max_keys=500)

    async def use(index: int) -> None:
        async with pool.hold(f"owner:{index}"):
            await asyncio.sleep(0)

    await asyncio.gather(*(use(index) for index in range(400)))
    assert pool.active_key_count == 0


@pytest.mark.asyncio
async def test_same_key_is_strictly_serialized() -> None:
    pool   = AsyncKeyedLockPool()
    active = 0
    peak   = 0

    async def use() -> None:
        nonlocal active, peak
        async with pool.hold("same"):
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.001)
            active -= 1

    await asyncio.gather(*(use() for _ in range(20)))
    assert peak == 1
    assert pool.active_key_count == 0


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_leak_key() -> None:
    pool    = AsyncKeyedLockPool()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def owner() -> None:
        async with pool.hold("key"):
            entered.set()
            await release.wait()

    async def waiter() -> None:
        async with pool.hold("key"):
            pass

    owner_task = asyncio.create_task(owner())
    await entered.wait()
    waiter_task = asyncio.create_task(waiter())
    await asyncio.sleep(0)
    waiter_task.cancel()
    await asyncio.gather(waiter_task, return_exceptions=True)
    release.set()
    await owner_task
    assert pool.active_key_count == 0


@pytest.mark.asyncio
async def test_key_length_and_pool_capacity_are_bounded() -> None:
    pool = AsyncKeyedLockPool(max_keys=1, max_key_length=4)
    with pytest.raises(ValueError):
        async with pool.hold("12345"):
            pass

    async with pool.hold("one"):
        with pytest.raises(RuntimeError, match="capacity"):
            async with pool.hold("two"):
                pass


@pytest.mark.asyncio
async def test_same_task_can_reenter_without_releasing_to_waiter() -> None:
    pool           = AsyncKeyedLockPool()
    inner_entered  = asyncio.Event()
    release_outer  = asyncio.Event()
    waiter_entered = asyncio.Event()

    async def owner() -> None:
        async with pool.hold((1, None)):
            async with pool.hold((1, None)):
                inner_entered.set()
            await release_outer.wait()

    async def waiter() -> None:
        async with pool.hold((1, None)):
            waiter_entered.set()

    owner_task = asyncio.create_task(owner())
    await inner_entered.wait()
    waiter_task = asyncio.create_task(waiter())
    await asyncio.sleep(0)
    assert waiter_entered.is_set() is False

    release_outer.set()
    await asyncio.gather(owner_task, waiter_task)
    assert waiter_entered.is_set()
    assert pool.active_key_count == 0


@pytest.mark.asyncio
async def test_unhashable_key_is_rejected_without_allocating_entry() -> None:
    pool = AsyncKeyedLockPool()

    with pytest.raises(ValueError, match="hashable"):
        async with pool.hold(["not", "hashable"]):  # type: ignore[arg-type]
            pass

    assert pool.active_key_count == 0
