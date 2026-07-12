"""Tests for manifest-backed plugin execution gates."""

from __future__ import annotations

import asyncio
import threading

import pytest

from core.plugin_execution import (
    PluginExecutionClosed,
    PluginExecutionGate,
    PluginExecutionPolicy,
    PluginExecutionTimeout,
    PluginExecutionUnavailable,
    call_plugin_callback,
)


@pytest.mark.asyncio
async def test_sequential_gate_allows_only_one_active_operation():
    gate = PluginExecutionGate("sequential")
    release = asyncio.Event()
    first_started = asyncio.Event()
    active = 0
    max_active = 0

    async def slow_operation() -> None:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        first_started.set()
        await release.wait()
        active -= 1

    first = asyncio.create_task(gate.run(slow_operation))
    await first_started.wait()
    second = asyncio.create_task(gate.run(slow_operation))
    await asyncio.sleep(0)

    assert max_active == 1
    release.set()
    await asyncio.gather(first, second)
    assert max_active == 1


@pytest.mark.asyncio
async def test_parallel_gate_preserves_parallel_execution():
    gate = PluginExecutionGate("parallel")
    release = asyncio.Event()
    both_started = asyncio.Event()
    active = 0
    max_active = 0

    async def slow_operation() -> None:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        if active == 2:
            both_started.set()
        await release.wait()
        active -= 1

    first = asyncio.create_task(gate.run(slow_operation))
    second = asyncio.create_task(gate.run(slow_operation))
    await both_started.wait()
    assert max_active == 2
    release.set()
    await asyncio.gather(first, second)


@pytest.mark.asyncio
async def test_close_cancels_active_and_queued_work_then_rejects_new_work():
    gate = PluginExecutionGate("sequential")
    entered = asyncio.Event()

    async def never_finishes() -> None:
        entered.set()
        await asyncio.Event().wait()

    active = asyncio.create_task(gate.run(never_finishes))
    await entered.wait()
    queued = asyncio.create_task(gate.run(never_finishes))
    await asyncio.sleep(0)

    await gate.close()

    assert active.cancelled()
    assert queued.cancelled()
    with pytest.raises(PluginExecutionClosed):
        await gate.run(lambda: asyncio.sleep(0))


@pytest.mark.asyncio
async def test_timeout_cancels_callback_and_opens_circuit():
    gate = PluginExecutionGate(
        "parallel",
        policy=PluginExecutionPolicy(timeout_seconds=0.01, cooldown_seconds=0.1),
    )
    cancelled = asyncio.Event()

    async def slow_operation() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    with pytest.raises(PluginExecutionTimeout):
        await gate.run(slow_operation)
    await cancelled.wait()
    with pytest.raises(PluginExecutionUnavailable):
        await gate.run(lambda: asyncio.sleep(0))


@pytest.mark.asyncio
async def test_failure_threshold_opens_then_recovers_circuit():
    gate = PluginExecutionGate(
        "parallel",
        policy=PluginExecutionPolicy(
            timeout_seconds=None,
            failure_threshold=2,
            cooldown_seconds=0.01,
        ),
    )

    async def fail() -> None:
        raise ValueError("boom")

    with pytest.raises(ValueError):
        await gate.run(fail)
    with pytest.raises(ValueError):
        await gate.run(fail)
    with pytest.raises(PluginExecutionUnavailable):
        await gate.run(lambda: asyncio.sleep(0))

    await asyncio.sleep(0.02)
    await gate.run(lambda: asyncio.sleep(0))


@pytest.mark.asyncio
async def test_parallel_limit_bounds_parallel_plugin_callbacks():
    gate = PluginExecutionGate(
        "parallel",
        policy=PluginExecutionPolicy(timeout_seconds=None, parallel_limit=1),
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    active = 0
    max_active = 0

    async def slow_operation() -> None:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        entered.set()
        await release.wait()
        active -= 1

    first = asyncio.create_task(gate.run(slow_operation))
    await entered.wait()
    second = asyncio.create_task(gate.run(slow_operation))
    await asyncio.sleep(0)
    assert max_active == 1
    release.set()
    await asyncio.gather(first, second)


@pytest.mark.asyncio
async def test_timeout_quarantines_callback_that_ignores_cancellation():
    gate = PluginExecutionGate(
        "parallel",
        policy=PluginExecutionPolicy(timeout_seconds=0.01, cooldown_seconds=0.01),
    )
    ignored_cancel = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    async def ignores_cancel() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            ignored_cancel.set()
            await release.wait()
        finally:
            finished.set()

    with pytest.raises(PluginExecutionTimeout):
        await gate.run(ignores_cancel)
    await ignored_cancel.wait()
    await asyncio.sleep(0.02)
    with pytest.raises(PluginExecutionUnavailable):
        await gate.run(lambda: asyncio.sleep(0))

    release.set()
    await finished.wait()
    await asyncio.sleep(0)
    await gate.run(lambda: asyncio.sleep(0))


@pytest.mark.asyncio
async def test_sync_callbacks_use_bounded_shared_executor():
    started = threading.Event()
    release = threading.Event()
    active = 0
    max_active = 0
    lock = threading.Lock()

    def blocking_callback() -> None:
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
            if active == 4:
                started.set()
        release.wait(timeout=1)
        with lock:
            active -= 1

    tasks = [asyncio.create_task(call_plugin_callback(blocking_callback)) for _ in range(5)]
    await asyncio.to_thread(started.wait, 1)
    assert max_active == 4
    release.set()
    await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_sync_timeout_tracks_real_future_and_close_reports_undrained_work():
    gate = PluginExecutionGate(
        "sequential",
        plugin_name="blocking-sync",
        policy=PluginExecutionPolicy(
            timeout_seconds=0.01,
            cooldown_seconds=0.01,
            drain_timeout_seconds=0.01,
        ),
    )
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    side_effects: list[str] = []

    def blocking_callback() -> None:
        started.set()
        release.wait(timeout=2)
        side_effects.append("finished")
        finished.set()

    async def operation() -> None:
        await call_plugin_callback(blocking_callback)

    with pytest.raises(PluginExecutionTimeout):
        await gate.run(operation)
    assert started.is_set()
    assert gate.pending_sync_callbacks == 1
    await asyncio.sleep(0.02)
    with pytest.raises(PluginExecutionUnavailable):
        await gate.run(lambda: asyncio.sleep(0))

    first_close = await gate.close()
    assert first_close.drained is False
    assert first_close.pending_sync_callbacks == 1
    assert side_effects == []

    release.set()
    assert await asyncio.to_thread(finished.wait, 1)
    await asyncio.sleep(0)
    second_close = await gate.close()

    assert second_close.drained is True
    assert second_close.pending_sync_callbacks == 0
    assert side_effects == ["finished"]


@pytest.mark.asyncio
async def test_cancelled_sync_callback_poison_blocks_sequential_overlap_until_thread_ends():
    gate = PluginExecutionGate(
        "sequential",
        policy=PluginExecutionPolicy(
            timeout_seconds=None,
            drain_timeout_seconds=0.01,
        ),
    )
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def blocking_callback() -> None:
        started.set()
        release.wait(timeout=2)
        finished.set()

    running = asyncio.create_task(
        gate.run(lambda: call_plugin_callback(blocking_callback))
    )
    assert await asyncio.to_thread(started.wait, 1)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    with pytest.raises(PluginExecutionUnavailable):
        await gate.run(lambda: asyncio.sleep(0))
    assert gate.pending_sync_callbacks == 1

    release.set()
    assert await asyncio.to_thread(finished.wait, 1)
    await asyncio.sleep(0)
    assert gate.pending_sync_callbacks == 0
    assert await gate.run(lambda: asyncio.sleep(0, result="safe")) == "safe"
