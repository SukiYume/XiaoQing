"""Tests for manifest-backed plugin execution gates."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable

import pytest

from core.plugin_execution import (
    PluginCallbackFatalError,
    PluginExecutionClosed,
    PluginExecutionGate,
    PluginExecutionOverloaded,
    PluginExecutionPolicy,
    PluginExecutionTimeout,
    PluginExecutionUnavailable,
    PluginSyncBroker,
    call_plugin_callback,
    callback_accepts_positional_context,
)


def test_lifecycle_context_probe_only_accepts_positional_parameters() -> None:
    async def positional(context) -> None:
        return None

    async def keyword_only(*, context=None) -> None:
        return None

    async def keyword_mapping(**kwargs) -> None:
        return None

    assert callback_accepts_positional_context(positional) is True
    assert callback_accepts_positional_context(keyword_only) is False
    assert callback_accepts_positional_context(keyword_mapping) is False


async def _detach_running_callback(
    gate: PluginExecutionGate,
    callback: Callable[[], object],
    started: threading.Event,
) -> None:
    """取消等待方并保留仍在线程池运行的同步回调。"""
    task     = asyncio.create_task(gate.run(lambda: call_plugin_callback(callback)))
    deadline = time.monotonic() + 1
    while not started.is_set() and time.monotonic() < deadline:
        await asyncio.sleep(0.001)
    assert started.is_set()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_policy_mapping_falls_back_for_unbounded_integer() -> None:
    fallback = PluginExecutionPolicy(parallel_limit=7)

    policy = PluginExecutionPolicy.from_mapping(
        {"parallel_limit": float("inf")},
        fallback=fallback,
    )

    assert policy.parallel_limit == 7


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_workers": 1.5},
        {"max_workers": "2"},
        {"global_queue_limit": 1.5},
        {"global_queue_limit": "2"},
    ],
)
def test_broker_rejects_coerced_integer_limits(kwargs) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        PluginSyncBroker(**kwargs)


@pytest.mark.asyncio
async def test_broker_submit_failure_releases_global_and_lane_slots(monkeypatch) -> None:
    broker = PluginSyncBroker(max_workers=1, global_queue_limit=4)
    gate = PluginExecutionGate(
        "parallel",
        plugin_name="submit-failure",
        policy=PluginExecutionPolicy(timeout_seconds=None),
        sync_broker=broker,
    )

    def fail_executor_access():
        raise RuntimeError("executor unavailable")

    monkeypatch.setattr(broker, "_ensure_executor_locked", fail_executor_access)

    with pytest.raises(RuntimeError, match="executor unavailable"):
        await gate.run(lambda: call_plugin_callback(lambda: "not-run"))

    assert broker.running_callbacks == 0
    assert broker._running_by_lane == {}
    assert broker.drained is True
    assert (await gate.close()).drained is True
    assert (await broker.close()).drained is True


@pytest.mark.asyncio
async def test_sequential_gate_allows_only_one_active_operation():
    gate          = PluginExecutionGate("sequential")
    release       = asyncio.Event()
    first_started = asyncio.Event()
    active        = 0
    max_active    = 0

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
    gate         = PluginExecutionGate("parallel")
    release      = asyncio.Event()
    both_started = asyncio.Event()
    active       = 0
    max_active   = 0

    async def slow_operation() -> None:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        if active == 2:
            both_started.set()
        await release.wait()
        active -= 1

    first  = asyncio.create_task(gate.run(slow_operation))
    second = asyncio.create_task(gate.run(slow_operation))
    await both_started.wait()
    assert max_active == 2
    release.set()
    await asyncio.gather(first, second)


@pytest.mark.asyncio
async def test_close_cancels_active_and_queued_work_then_rejects_new_work():
    gate    = PluginExecutionGate("sequential")
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
async def test_callback_completion_does_not_consume_same_turn_caller_cancellation():
    gate   = PluginExecutionGate("sequential")
    caller = None

    async def completes_while_cancelled():
        # 回调完成与调用方取消发生在同一轮，调用方必须观察到取消。
        caller.cancel()
        return "completed"

    caller = asyncio.create_task(gate.run(completes_while_cancelled))
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert caller.cancelled()
    assert (await gate.close()).drained


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
            timeout_seconds   = None,
            failure_threshold = 2,
            cooldown_seconds  = 0.01,
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
    entered    = asyncio.Event()
    release    = asyncio.Event()
    active     = 0
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
        policy=PluginExecutionPolicy(timeout_seconds=0.1, cooldown_seconds=0.01),
    )
    ignored_cancel = asyncio.Event()
    release        = asyncio.Event()
    finished       = asyncio.Event()

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
    started    = threading.Event()
    release    = threading.Event()
    active     = 0
    max_active = 0
    lock       = threading.Lock()

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
        plugin_name = "blocking-sync",
        policy      = PluginExecutionPolicy(
            timeout_seconds       = 0.1,
            cooldown_seconds      = 0.01,
            drain_timeout_seconds = 0.1,
        ),
    )
    started                 = threading.Event()
    release                 = threading.Event()
    finished                = threading.Event()
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
async def test_late_sync_fatal_is_retained_until_gate_drain() -> None:
    gate = PluginExecutionGate(
        "sequential",
        plugin_name = "late-sync-fatal",
        policy      = PluginExecutionPolicy(
            timeout_seconds       = 0.01,
            drain_timeout_seconds = 0.2,
        ),
    )
    started  = threading.Event()
    release  = threading.Event()
    finished = threading.Event()
    expected = SystemExit("late sync fatal")

    def blocking_callback() -> None:
        started.set()
        release.wait(timeout=2)
        finished.set()
        raise expected

    with pytest.raises(PluginExecutionTimeout):
        await gate.run(lambda: call_plugin_callback(blocking_callback))
    assert started.is_set()

    release.set()
    assert await asyncio.to_thread(finished.wait, 1)
    result = await gate.close()

    assert result.drained is True
    assert gate.consume_deferred_fatal_error() is expected
    assert gate.consume_deferred_fatal_error() is None


@pytest.mark.asyncio
async def test_cancelled_sync_callback_poison_blocks_sequential_overlap_until_thread_ends():
    gate = PluginExecutionGate(
        "sequential",
        policy=PluginExecutionPolicy(
            timeout_seconds       = None,
            drain_timeout_seconds = 0.01,
        ),
    )
    started  = threading.Event()
    release  = threading.Event()
    finished = threading.Event()

    def blocking_callback() -> None:
        started.set()
        release.wait(timeout=2)
        finished.set()

    running = asyncio.create_task(gate.run(lambda: call_plugin_callback(blocking_callback)))
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


@pytest.mark.asyncio
async def test_sync_bulkhead_allows_other_plugin_to_progress() -> None:
    broker = PluginSyncBroker(max_workers=2, global_queue_limit=8)
    policy = PluginExecutionPolicy(
        timeout_seconds     = None,
        parallel_limit      = 3,
        sync_parallel_limit = 1,
    )
    gate_a = PluginExecutionGate("parallel", plugin_name="a", policy=policy, sync_broker=broker)
    gate_b = PluginExecutionGate("parallel", plugin_name="b", policy=policy, sync_broker=broker)
    started = threading.Event()
    release = threading.Event()

    def blocking() -> str:
        started.set()
        release.wait(timeout=2)
        return "a"

    first = asyncio.create_task(gate_a.run(lambda: call_plugin_callback(blocking)))
    second: asyncio.Task[str] | None = None
    try:
        assert await asyncio.to_thread(started.wait, 1)
        second = asyncio.create_task(gate_a.run(lambda: call_plugin_callback(blocking)))
        while broker.queued_callbacks < 1:
            await asyncio.sleep(0)

        result = await asyncio.wait_for(
            gate_b.run(lambda: call_plugin_callback(lambda: "b")),
            timeout=0.5,
        )
        assert result == "b"
        assert broker.running_callbacks == 1
    finally:
        release.set()
        tasks = [first, *([second] if second is not None else [])]
        await asyncio.gather(*tasks, return_exceptions=True)
        await gate_a.close()
        await gate_b.close()
        assert (await broker.close(timeout_seconds=1)).drained is True


@pytest.mark.asyncio
async def test_gate_admission_queue_overflow_is_fast_and_bounded() -> None:
    gate = PluginExecutionGate(
        "parallel",
        plugin_name = "bounded",
        policy      = PluginExecutionPolicy(
            timeout_seconds       = None,
            parallel_limit        = 1,
            admission_queue_limit = 1,
        ),
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    calls   = 0

    async def blocking() -> None:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()

    first = asyncio.create_task(gate.run(blocking))
    await entered.wait()
    second = asyncio.create_task(gate.run(blocking))
    while gate.admitted_operations != 2:
        await asyncio.sleep(0)

    with pytest.raises(PluginExecutionOverloaded):
        await gate.run(blocking)
    assert gate.admitted_operations == 2
    assert calls == 1

    release.set()
    await asyncio.gather(first, second)


@pytest.mark.asyncio
async def test_sync_lane_queue_overflow_is_fast_and_does_not_count_as_failure() -> None:
    broker = PluginSyncBroker(max_workers=2, global_queue_limit=8)
    gate = PluginExecutionGate(
        "parallel",
        plugin_name = "lane-full",
        policy      = PluginExecutionPolicy(
            timeout_seconds     = None,
            parallel_limit      = 4,
            sync_parallel_limit = 1,
            sync_queue_limit    = 1,
            failure_threshold   = 1,
        ),
        sync_broker=broker,
    )
    started    = threading.Event()
    release    = threading.Event()
    late_calls = 0

    def blocking() -> None:
        started.set()
        release.wait(timeout=2)

    def must_not_run() -> None:
        nonlocal late_calls
        late_calls += 1

    first = asyncio.create_task(gate.run(lambda: call_plugin_callback(blocking)))
    second: asyncio.Task[None] | None = None
    try:
        assert await asyncio.to_thread(started.wait, 1)
        second = asyncio.create_task(gate.run(lambda: call_plugin_callback(blocking)))
        while broker.queued_callbacks != 1:
            await asyncio.sleep(0)
        with pytest.raises(PluginExecutionOverloaded):
            await gate.run(lambda: call_plugin_callback(must_not_run))
        assert late_calls == 0
        # Overload is capacity feedback, not a plugin callback failure/circuit event.
        assert await gate.run(lambda: asyncio.sleep(0, result="async-ok")) == "async-ok"
    finally:
        release.set()
        await asyncio.gather(first, *([second] if second else []), return_exceptions=True)
        await gate.close()
        await broker.close(timeout_seconds=1)


@pytest.mark.asyncio
async def test_global_sync_queue_limit_bounds_many_plugin_backlog() -> None:
    broker = PluginSyncBroker(max_workers=1, global_queue_limit=1)
    policy = PluginExecutionPolicy(timeout_seconds=None, sync_queue_limit=4)
    gates = [
        PluginExecutionGate("parallel", plugin_name=name, policy=policy, sync_broker=broker)
        for name in ("a", "b", "c")
    ]
    started        = threading.Event()
    release        = threading.Event()
    rejected_calls = 0

    def blocking() -> None:
        started.set()
        release.wait(timeout=2)

    def rejected() -> None:
        nonlocal rejected_calls
        rejected_calls += 1

    running = asyncio.create_task(gates[0].run(lambda: call_plugin_callback(blocking)))
    queued: asyncio.Task[None] | None = None
    try:
        assert await asyncio.to_thread(started.wait, 1)
        queued = asyncio.create_task(gates[1].run(lambda: call_plugin_callback(blocking)))
        while broker.queued_callbacks != 1:
            await asyncio.sleep(0)
        with pytest.raises(PluginExecutionOverloaded):
            await gates[2].run(lambda: call_plugin_callback(rejected))
        assert broker.queued_callbacks == 1
        assert rejected_calls == 0
    finally:
        release.set()
        await asyncio.gather(running, *([queued] if queued else []), return_exceptions=True)
        for gate in gates:
            await gate.close()
        await broker.close(timeout_seconds=1)


@pytest.mark.asyncio
async def test_broker_round_robin_runs_waiting_lane_before_same_lane_backlog() -> None:
    broker = PluginSyncBroker(max_workers=1, global_queue_limit=8)
    policy = PluginExecutionPolicy(
        timeout_seconds  = None,
        parallel_limit   = 4,
        sync_queue_limit = 4,
    )
    gate_a = PluginExecutionGate("parallel", plugin_name="a", policy=policy, sync_broker=broker)
    gate_b = PluginExecutionGate("parallel", plugin_name="b", policy=policy, sync_broker=broker)
    started          = threading.Event()
    release          = threading.Event()
    order: list[str] = []

    def first_a() -> None:
        order.append("a1")
        started.set()
        release.wait(timeout=2)

    def record(label: str) -> None:
        order.append(label)

    tasks = [asyncio.create_task(gate_a.run(lambda: call_plugin_callback(first_a)))]
    try:
        assert await asyncio.to_thread(started.wait, 1)
        tasks.append(asyncio.create_task(gate_a.run(lambda: call_plugin_callback(record, "a2"))))
        tasks.append(asyncio.create_task(gate_a.run(lambda: call_plugin_callback(record, "a3"))))
        while broker.queued_callbacks != 2:
            await asyncio.sleep(0)
        tasks.append(asyncio.create_task(gate_b.run(lambda: call_plugin_callback(record, "b"))))
        while broker.queued_callbacks != 3:
            await asyncio.sleep(0)
        release.set()
        await asyncio.gather(*tasks)
        assert order.index("b") < order.index("a2")
        assert order.index("b") < order.index("a3")
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await gate_a.close()
        await gate_b.close()
        await broker.close(timeout_seconds=1)


@pytest.mark.asyncio
async def test_cancelled_queued_sync_callback_never_runs_and_frees_capacity() -> None:
    broker = PluginSyncBroker(max_workers=1, global_queue_limit=1)
    policy = PluginExecutionPolicy(
        timeout_seconds  = None,
        parallel_limit   = 3,
        sync_queue_limit = 2,
    )
    gate = PluginExecutionGate("parallel", plugin_name="cancel", policy=policy, sync_broker=broker)
    started          = threading.Event()
    release          = threading.Event()
    calls: list[str] = []

    def blocking() -> None:
        started.set()
        release.wait(timeout=2)

    def record(label: str) -> None:
        calls.append(label)

    first = asyncio.create_task(gate.run(lambda: call_plugin_callback(blocking)))
    second: asyncio.Task[None] | None = None
    third: asyncio.Task[None] | None = None
    try:
        assert await asyncio.to_thread(started.wait, 1)
        second = asyncio.create_task(gate.run(lambda: call_plugin_callback(record, "cancelled")))
        while broker.queued_callbacks != 1:
            await asyncio.sleep(0)
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
        while broker.queued_callbacks:
            await asyncio.sleep(0)

        third = asyncio.create_task(gate.run(lambda: call_plugin_callback(record, "third")))
        while broker.queued_callbacks != 1:
            await asyncio.sleep(0)
        release.set()
        await asyncio.gather(first, third)
        assert calls == ["third"]
    finally:
        release.set()
        await asyncio.gather(
            first,
            *([second] if second else []),
            *([third] if third else []),
            return_exceptions=True,
        )
        await gate.close()
        await broker.close(timeout_seconds=1)


@pytest.mark.asyncio
async def test_queued_sync_timeout_never_executes_late_side_effect() -> None:
    broker = PluginSyncBroker(max_workers=1, global_queue_limit=4)
    gate_a = PluginExecutionGate(
        "parallel",
        plugin_name="blocker",
        policy=PluginExecutionPolicy(timeout_seconds=None),
        sync_broker=broker,
    )
    gate_b = PluginExecutionGate(
        "parallel",
        plugin_name="times-out",
        policy=PluginExecutionPolicy(timeout_seconds=0.02),
        sync_broker=broker,
    )
    started    = threading.Event()
    release    = threading.Event()
    late_calls = 0

    def blocking() -> None:
        started.set()
        release.wait(timeout=2)

    def late_side_effect() -> None:
        nonlocal late_calls
        late_calls += 1

    running = asyncio.create_task(gate_a.run(lambda: call_plugin_callback(blocking)))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        with pytest.raises(PluginExecutionTimeout):
            await gate_b.run(lambda: call_plugin_callback(late_side_effect))
        assert broker.queued_callbacks == 0
        release.set()
        await running
        await asyncio.sleep(0.02)
        assert late_calls == 0
    finally:
        release.set()
        await asyncio.gather(running, return_exceptions=True)
        await gate_a.close()
        await gate_b.close()
        await broker.close(timeout_seconds=1)


@pytest.mark.asyncio
async def test_running_sync_timeout_retains_own_slot_without_blocking_other_plugin() -> None:
    broker = PluginSyncBroker(max_workers=2, global_queue_limit=4)
    gate_a = PluginExecutionGate(
        "parallel",
        plugin_name="slow",
        policy=PluginExecutionPolicy(timeout_seconds=0.02, drain_timeout_seconds=0.01),
        sync_broker=broker,
    )
    gate_b = PluginExecutionGate(
        "parallel",
        plugin_name="healthy",
        policy=PluginExecutionPolicy(timeout_seconds=None),
        sync_broker=broker,
    )
    started = threading.Event()
    release = threading.Event()

    def blocking() -> None:
        started.set()
        release.wait(timeout=2)

    try:
        with pytest.raises(PluginExecutionTimeout):
            await gate_a.run(lambda: call_plugin_callback(blocking))
        assert started.is_set()
        assert gate_a.pending_sync_callbacks == 1
        assert broker.running_callbacks == 1
        assert (
            await asyncio.wait_for(
                gate_b.run(lambda: call_plugin_callback(lambda: "healthy")),
                timeout=0.5,
            )
            == "healthy"
        )
    finally:
        release.set()
        while gate_a.pending_sync_callbacks:
            await asyncio.sleep(0)
        await gate_a.close()
        await gate_b.close()
        await broker.close(timeout_seconds=1)


@pytest.mark.asyncio
async def test_closed_gate_allows_sync_lifecycle_callback_but_rejects_detached_scope() -> None:
    broker = PluginSyncBroker(max_workers=2, global_queue_limit=4)
    gate = PluginExecutionGate(
        "parallel",
        plugin_name="lifecycle",
        policy=PluginExecutionPolicy(timeout_seconds=None),
        sync_broker=broker,
    )
    child_release = asyncio.Event()

    async def spawn_detached() -> asyncio.Task[str]:
        async def child() -> str:
            await child_release.wait()
            return await call_plugin_callback(lambda: "too-late")

        return asyncio.create_task(child())

    child = await gate.run(spawn_detached)
    await gate.close()
    assert (
        await gate.run(
            lambda: call_plugin_callback(lambda: "shutdown"),
            allow_closed=True,
        )
        == "shutdown"
    )
    child_release.set()
    with pytest.raises(PluginExecutionClosed):
        await child
    assert (await gate.close()).drained is True
    await broker.close(timeout_seconds=1)


@pytest.mark.asyncio
async def test_hot_parallel_limit_decrease_and_increase_apply_to_waiters() -> None:
    policy = PluginExecutionPolicy(
        timeout_seconds       = None,
        parallel_limit        = 2,
        admission_queue_limit = 4,
    )
    gate = PluginExecutionGate("parallel", policy=policy)
    entered  = [asyncio.Event() for _ in range(3)]
    releases = [asyncio.Event() for _ in range(3)]

    async def operation(index: int) -> None:
        entered[index].set()
        await releases[index].wait()

    tasks = [asyncio.create_task(gate.run(lambda index=i: operation(index))) for i in range(2)]
    await asyncio.gather(entered[0].wait(), entered[1].wait())
    gate.set_policy(
        PluginExecutionPolicy(
            timeout_seconds       = None,
            parallel_limit        = 1,
            admission_queue_limit = 4,
        )
    )
    tasks.append(asyncio.create_task(gate.run(lambda: operation(2))))
    releases[0].set()
    await tasks[0]
    await asyncio.sleep(0)
    assert entered[2].is_set() is False
    releases[1].set()
    await entered[2].wait()
    releases[2].set()
    await asyncio.gather(*tasks)

    increase_gate = PluginExecutionGate(
        "parallel",
        policy=PluginExecutionPolicy(
            timeout_seconds       = None,
            parallel_limit        = 1,
            admission_queue_limit = 2,
        ),
    )
    first_entered  = asyncio.Event()
    second_entered = asyncio.Event()
    release        = asyncio.Event()

    async def first_operation() -> None:
        first_entered.set()
        await release.wait()

    async def second_operation() -> None:
        second_entered.set()
        await release.wait()

    first = asyncio.create_task(increase_gate.run(first_operation))
    await first_entered.wait()
    second = asyncio.create_task(increase_gate.run(second_operation))
    await asyncio.sleep(0)
    increase_gate.set_policy(
        PluginExecutionPolicy(
            timeout_seconds       = None,
            parallel_limit        = 2,
            admission_queue_limit = 2,
        )
    )
    await asyncio.wait_for(second_entered.wait(), timeout=0.5)
    release.set()
    await asyncio.gather(first, second)


@pytest.mark.asyncio
async def test_many_plugin_gates_share_fixed_broker_thread_bound() -> None:
    broker = PluginSyncBroker(max_workers=4, global_queue_limit=32)
    policy = PluginExecutionPolicy(timeout_seconds=None)
    gates = [
        PluginExecutionGate(
            "parallel",
            plugin_name = f"plugin-{index}",
            policy      = policy,
            sync_broker = broker,
        )
        for index in range(20)
    ]
    release      = threading.Event()
    started      = 0
    started_lock = threading.Lock()
    four_started = threading.Event()

    def blocking() -> None:
        nonlocal started
        with started_lock:
            started += 1
            if started == 4:
                four_started.set()
        release.wait(timeout=2)

    tasks = [
        asyncio.create_task(gate.run(lambda: call_plugin_callback(blocking))) for gate in gates
    ]
    try:
        assert await asyncio.to_thread(four_started.wait, 1)
        assert broker.running_callbacks == 4
        assert broker.worker_thread_count <= 4
        assert broker.queued_callbacks <= 32
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        for gate in gates:
            await gate.close()
        result = await broker.close(timeout_seconds=1)
        assert result.drained is True


@pytest.mark.asyncio
async def test_immediate_sync_fatal_propagates_without_duplicate_deferred_outcome() -> None:
    gate = PluginExecutionGate("parallel", policy=PluginExecutionPolicy(timeout_seconds=None))
    expected = SystemExit("immediate fatal")

    def fatal() -> None:
        raise expected

    with pytest.raises(PluginCallbackFatalError) as captured:
        await gate.run(lambda: call_plugin_callback(fatal))
    assert captured.value.original is expected
    await asyncio.sleep(0)
    assert gate.consume_deferred_fatal_error() is None


@pytest.mark.asyncio
async def test_gate_close_waits_for_detached_fatal_delivery_before_reporting_drained() -> None:
    broker = PluginSyncBroker(max_workers=2, global_queue_limit=32)
    try:
        for index in range(25):
            gate = PluginExecutionGate(
                "parallel",
                plugin_name = f"fatal-race-{index}",
                policy      = PluginExecutionPolicy(
                    timeout_seconds       = None,
                    drain_timeout_seconds = 1,
                ),
                sync_broker=broker,
            )
            started  = threading.Event()
            release  = threading.Event()
            expected = SystemExit(f"late-{index}")

            def late_fatal(
                started: threading.Event = started,
                release: threading.Event = release,
                expected: SystemExit     = expected,
            ) -> None:
                started.set()
                release.wait(timeout=2)
                raise expected

            running = asyncio.create_task(gate.run(lambda: call_plugin_callback(late_fatal)))
            assert await asyncio.to_thread(started.wait, 1)
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running
            release.set()

            result = await gate.close(timeout_seconds=1)
            assert result.drained is True
            # No extra event-loop yield is allowed between close and consume:
            # lifecycle code relies on this exact ordering.
            assert gate.consume_deferred_fatal_error() is expected
            assert gate.consume_deferred_fatal_error() is None
    finally:
        assert (await broker.close(timeout_seconds=1)).drained is True


def test_closed_owner_loop_still_cleans_detached_future_and_retains_fatal() -> None:
    broker = PluginSyncBroker(max_workers=1, global_queue_limit=4)
    gate = PluginExecutionGate(
        "parallel",
        plugin_name="dead-loop",
        policy=PluginExecutionPolicy(timeout_seconds=None),
        sync_broker=broker,
    )
    started  = threading.Event()
    release  = threading.Event()
    finished = threading.Event()
    expected = SystemExit("dead loop fatal")

    def late_fatal() -> None:
        started.set()
        release.wait(timeout=2)
        finished.set()
        raise expected

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_detach_running_callback(gate, late_fatal, started))
    finally:
        loop.close()

    release.set()
    assert finished.wait(timeout=1)
    deadline = time.monotonic() + 1
    while not broker.drained and time.monotonic() < deadline:
        time.sleep(0.001)

    assert broker.drained is True
    assert gate.pending_sync_callbacks == 0
    assert gate._poisoned_by_sync_callback is False
    assert gate.consume_deferred_fatal_error() is expected
    assert gate.consume_deferred_fatal_error() is None
    assert asyncio.run(broker.close(timeout_seconds=1)).drained is True


def test_stopped_open_owner_loop_cannot_strand_detached_delivery() -> None:
    broker = PluginSyncBroker(max_workers=1, global_queue_limit=4)
    gate = PluginExecutionGate(
        "parallel",
        plugin_name="stopped-loop",
        policy=PluginExecutionPolicy(timeout_seconds=None),
        sync_broker=broker,
    )
    started  = threading.Event()
    release  = threading.Event()
    finished = threading.Event()
    expected = SystemExit("stopped loop fatal")

    def late_fatal() -> None:
        started.set()
        release.wait(timeout=2)
        finished.set()
        raise expected

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_detach_running_callback(gate, late_fatal, started))
        # The owner loop is open but stopped here.  call_soon_threadsafe would
        # accept a callback that loop.close() can later discard without ever
        # running; detached completion must therefore finalize in the worker.
        release.set()
        assert finished.wait(timeout=1)
        deadline = time.monotonic() + 1
        while not broker.drained and time.monotonic() < deadline:
            time.sleep(0.001)
    finally:
        loop.close()

    assert broker.drained is True
    assert broker._pending_delivery_count == 0
    assert gate.pending_sync_callbacks == 0
    assert gate._poisoned_by_sync_callback is False
    assert gate.consume_deferred_fatal_error() is expected
    assert asyncio.run(broker.close(timeout_seconds=1)).drained is True


@pytest.mark.asyncio
async def test_timed_out_broker_close_auto_shuts_executor_after_late_callback() -> None:
    broker = PluginSyncBroker(max_workers=1, global_queue_limit=4)
    gate = PluginExecutionGate(
        "parallel",
        plugin_name="late-broker-close",
        policy=PluginExecutionPolicy(timeout_seconds=None),
        sync_broker=broker,
    )
    started = threading.Event()
    release = threading.Event()

    def blocking() -> str:
        started.set()
        release.wait(timeout=2)
        return "done"

    task = asyncio.create_task(gate.run(lambda: call_plugin_callback(blocking)))
    assert await asyncio.to_thread(started.wait, 1)
    executor = broker._executor
    assert executor is not None

    first_close = await broker.close(timeout_seconds=0)
    assert first_close.drained is False
    release.set()
    assert await task == "done"

    deadline = time.monotonic() + 1
    while broker._executor is not None and time.monotonic() < deadline:
        await asyncio.sleep(0.001)
    assert broker.drained is True
    assert broker._executor is None
    assert broker._executor_shutdown is True

    worker_deadline = time.monotonic() + 1
    while (
        any(thread.is_alive() for thread in executor._threads)
        and time.monotonic() < worker_deadline
    ):
        await asyncio.sleep(0.001)
    assert all(not thread.is_alive() for thread in executor._threads)
    assert (await broker.close(timeout_seconds=0)).drained is True
    assert (await gate.close()).drained is True


@pytest.mark.asyncio
async def test_gate_close_physically_cancels_queued_sync_job_before_worker_release() -> None:
    broker = PluginSyncBroker(max_workers=1, global_queue_limit=4)
    gate = PluginExecutionGate(
        "parallel",
        plugin_name = "close-queued",
        policy      = PluginExecutionPolicy(
            timeout_seconds       = None,
            parallel_limit        = 3,
            sync_queue_limit      = 2,
            drain_timeout_seconds = 0.01,
        ),
        sync_broker=broker,
    )
    started             = threading.Event()
    release             = threading.Event()
    queued_side_effects = 0

    def blocking() -> None:
        started.set()
        release.wait(timeout=2)

    def queued_callback() -> None:
        nonlocal queued_side_effects
        queued_side_effects += 1

    running = asyncio.create_task(gate.run(lambda: call_plugin_callback(blocking)))
    queued: asyncio.Task[None] | None = None
    try:
        assert await asyncio.to_thread(started.wait, 1)
        queued = asyncio.create_task(gate.run(lambda: call_plugin_callback(queued_callback)))
        while broker.queued_callbacks != 1:
            await asyncio.sleep(0)

        first_close = await gate.close(timeout_seconds=0.01)
        assert first_close.drained is False
        assert broker.queued_callbacks == 0
        assert queued.cancelled()

        release.set()
        await asyncio.gather(running, return_exceptions=True)
        assert (await gate.close(timeout_seconds=1)).drained is True
        assert queued_side_effects == 0
    finally:
        release.set()
        await asyncio.gather(
            running,
            *([queued] if queued is not None else []),
            return_exceptions=True,
        )
        await broker.close(timeout_seconds=1)
