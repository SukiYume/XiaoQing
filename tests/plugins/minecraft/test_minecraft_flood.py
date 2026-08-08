"""Minecraft 日志洪泛、投递确认和公平调度契约。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from core.interfaces import DeliveryTarget
from plugins.minecraft import main as mc_main
from plugins.minecraft.connection import ConnectionManager, McConnection
from plugins.minecraft.log_monitor import LogBatch, LogEvent, LogEventType


def _event(index: int, *, message: str | None = None) -> LogEvent:
    return LogEvent(
        LogEventType.CHAT,
        f"Player{index % 100}",
        message if message is not None else f"message-{index}",
    )


def _batch(
    events: list[LogEvent],
    *,
    cursor: int = 0,
    matched_total: int | None = None,
    dropped_events: int = 0,
    skipped_bytes: int = 0,
    skipped_lines: int | None = 0,
) -> LogBatch:
    return LogBatch(
        events=events,
        matched_total=len(events) if matched_total is None else matched_total,
        dropped_events=dropped_events,
        skipped_bytes=skipped_bytes,
        skipped_lines=skipped_lines,
        cursor_before=cursor,
        cursor_after=cursor + 1,
        file_identity="1:1",
    )


class _StaticMonitor:
    """轮询不移动游标；只有 commit 成功才进入下一批。"""

    def __init__(self, batches: list[LogBatch]) -> None:
        self.batches = batches
        self.index = 0
        self.calls = 0
        self.committed: list[LogBatch] = []
        self.commit_result = True
        self.poll_error: BaseException | None = None
        self.commit_error: BaseException | None = None

    def check_updates(self) -> LogBatch:
        self.calls += 1
        if self.poll_error is not None:
            raise self.poll_error
        if self.index >= len(self.batches):
            return LogBatch(events=[])
        return self.batches[self.index]

    def commit(self, batch: LogBatch) -> bool:
        if self.commit_error is not None:
            raise self.commit_error
        if not self.commit_result or self.index >= len(self.batches):
            return False
        if batch is not self.batches[self.index]:
            return False
        self.committed.append(batch)
        self.index += 1
        return True


def _connection(
    target_id: int,
    monitor: _StaticMonitor,
    *,
    kind: str = "private",
    host: str = "mc.example",
    port: int = 25575,
) -> McConnection:
    target = DeliveryTarget(cast(Any, kind), target_id)
    return McConnection(
        host=host,
        port=port,
        target=target,
        log_monitor=cast(Any, monitor),
    )


def _message_text(action: dict[str, Any]) -> str:
    return "".join(
        segment["data"].get("text", "")
        for segment in action["params"]["message"]
        if segment.get("type") == "text"
    )


@pytest.fixture(autouse=True)
def reset_minecraft_runtime(monkeypatch: pytest.MonkeyPatch) -> ConnectionManager:
    manager = ConnectionManager()
    monkeypatch.setattr(mc_main, "_manager", manager)
    monkeypatch.setattr(mc_main, "_schedule_lock", asyncio.Lock())
    mc_main._event_buckets.clear()  # noqa: SLF001
    mc_main._delivery_cursor = 0  # noqa: SLF001
    yield manager
    mc_main._event_buckets.clear()  # noqa: SLF001


@pytest.mark.asyncio
async def test_thousand_events_produce_one_bounded_action_with_exact_drop_count(
    reset_minecraft_runtime: ConnectionManager,
) -> None:
    events = [_event(index) for index in range(1000)]
    monitor = _StaticMonitor([_batch(events, matched_total=1000)])
    await reset_minecraft_runtime.replace_connection(_connection(1, monitor))
    context = SimpleNamespace(send_action=AsyncMock(return_value=True))

    await mc_main.scheduled(cast(Any, context))

    context.send_action.assert_awaited_once()
    text = _message_text(context.send_action.await_args.args[0])
    assert "另有 988 条 MC 事件" in text
    assert len(text) <= mc_main.MC_MAX_ACTION_CHARS
    assert len(text.encode("utf-8")) <= mc_main.MC_MAX_ACTION_BYTES
    assert monitor.committed


@pytest.mark.asyncio
async def test_small_batch_preserves_event_order(
    reset_minecraft_runtime: ConnectionManager,
) -> None:
    monitor = _StaticMonitor([_batch([_event(1), _event(2), _event(3)])])
    await reset_minecraft_runtime.replace_connection(_connection(1, monitor))
    context = SimpleNamespace(send_action=AsyncMock(return_value=True))

    await mc_main.scheduled(cast(Any, context))

    text = _message_text(context.send_action.await_args.args[0])
    assert text.index("message-1") < text.index("message-2") < text.index("message-3")
    assert "折叠/丢弃" not in text


@pytest.mark.asyncio
async def test_sustained_flood_uses_cross_tick_target_bucket(
    reset_minecraft_runtime: ConnectionManager,
) -> None:
    flood = [_event(index) for index in range(1000)]
    monitor = _StaticMonitor(
        [_batch(flood, cursor=index, matched_total=1000) for index in range(3)]
    )
    await reset_minecraft_runtime.replace_connection(_connection(1, monitor))
    context = SimpleNamespace(send_action=AsyncMock(return_value=True))

    await mc_main.scheduled(cast(Any, context))
    await mc_main.scheduled(cast(Any, context))
    await mc_main.scheduled(cast(Any, context))

    texts = [_message_text(call.args[0]) for call in context.send_action.await_args_list]
    assert "message-0" in texts[0]
    assert "message-0" in texts[1]
    assert "message-0" not in texts[2]
    assert "另有 1000 条 MC 事件" in texts[2]
    assert len(mc_main._event_buckets) == 1  # noqa: SLF001


@pytest.mark.asyncio
async def test_global_limit_rotates_targets_without_cross_target_summary(
    reset_minecraft_runtime: ConnectionManager,
) -> None:
    monitors: list[_StaticMonitor] = []
    for index in range(10):
        monitor = _StaticMonitor([_batch([_event(index)])])
        monitors.append(monitor)
        await reset_minecraft_runtime.replace_connection(
            _connection(100 + index, monitor, port=25575 + index)
        )
    context = SimpleNamespace(send_action=AsyncMock(return_value=True))

    await mc_main.scheduled(cast(Any, context))
    first_targets = {
        call.args[0]["params"]["user_id"] for call in context.send_action.await_args_list
    }
    context.send_action.reset_mock()
    await mc_main.scheduled(cast(Any, context))
    second_targets = {
        call.args[0]["params"]["user_id"] for call in context.send_action.await_args_list
    }

    assert first_targets == set(range(100, 105))
    assert second_targets == set(range(105, 110))
    assert all(len(monitor.committed) == 1 for monitor in monitors)
    assert all(
        "个连接" not in _message_text(call.args[0]) for call in context.send_action.await_args_list
    )


@pytest.mark.asyncio
async def test_send_failures_and_unknown_outcomes_use_distinct_cursor_policies(
    reset_minecraft_runtime: ConnectionManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monitors: dict[int, _StaticMonitor] = {}
    for target in range(1, 5):
        monitor = _StaticMonitor([_batch([_event(target)])])
        monitors[target] = monitor
        await reset_minecraft_runtime.replace_connection(
            _connection(target, monitor, port=25575 + target)
        )
    monkeypatch.setattr(mc_main, "MC_SEND_TIMEOUT_SECONDS", 0.01)
    delivered: list[int] = []

    async def send_action(action: dict[str, Any]) -> bool:
        target = int(action["params"]["user_id"])
        if target == 1:
            raise RuntimeError("OneBot down")
        if target == 2:
            await asyncio.sleep(1)
        if target == 3:
            return False
        delivered.append(target)
        return True

    await mc_main.scheduled(cast(Any, SimpleNamespace(send_action=send_action)))

    assert delivered == [4]
    assert monitors[4].committed
    assert monitors[2].committed
    assert all(not monitors[target].committed for target in (1, 3))
    assert all(
        mc_main._event_buckets[
            mc_main._server_bucket_key(  # noqa: SLF001
                reset_minecraft_runtime.get_connection(DeliveryTarget("private", target))
            )
        ].tokens
        == mc_main.MC_EVENT_BUCKET_CAPACITY
        for target in (1, 3)
    )
    timeout_bucket = mc_main._event_buckets[
        mc_main._server_bucket_key(  # noqa: SLF001
            reset_minecraft_runtime.get_connection(DeliveryTarget("private", 2))
        )
    ]
    assert timeout_bucket.tokens < mc_main.MC_EVENT_BUCKET_CAPACITY


@pytest.mark.asyncio
async def test_long_unicode_events_fit_dual_action_budgets(
    reset_minecraft_runtime: ConnectionManager,
) -> None:
    events = [_event(index, message="🧱" * 1000) for index in range(20)]
    monitor = _StaticMonitor([_batch(events, matched_total=20)])
    await reset_minecraft_runtime.replace_connection(_connection(1, monitor))
    context = SimpleNamespace(send_action=AsyncMock(return_value=True))

    await mc_main.scheduled(cast(Any, context))

    text = _message_text(context.send_action.await_args.args[0])
    assert len(text) <= mc_main.MC_MAX_ACTION_CHARS
    assert len(text.encode("utf-8")) <= mc_main.MC_MAX_ACTION_BYTES
    assert "折叠/丢弃" in text


@pytest.mark.asyncio
async def test_empty_batch_commits_cursor_without_sending(
    reset_minecraft_runtime: ConnectionManager,
) -> None:
    empty = _batch([])
    monitor = _StaticMonitor([empty])
    await reset_minecraft_runtime.replace_connection(_connection(1, monitor))
    context = SimpleNamespace(send_action=AsyncMock(return_value=True))

    await mc_main.scheduled(cast(Any, context))

    context.send_action.assert_not_awaited()
    assert monitor.committed == [empty]


@pytest.mark.asyncio
async def test_commit_exception_does_not_fail_other_delivery(
    reset_minecraft_runtime: ConnectionManager,
) -> None:
    failing = _StaticMonitor([_batch([_event(1)])])
    failing.commit_error = OSError("state disk full")
    healthy = _StaticMonitor([_batch([_event(2)])])
    await reset_minecraft_runtime.replace_connection(_connection(1, failing))
    await reset_minecraft_runtime.replace_connection(_connection(2, healthy, port=25576))
    context = SimpleNamespace(send_action=AsyncMock(return_value=True))

    await mc_main.scheduled(cast(Any, context))

    assert context.send_action.await_count == 2
    assert failing.committed == []
    assert healthy.committed


@pytest.mark.asyncio
async def test_poll_exception_does_not_block_healthy_monitor(
    reset_minecraft_runtime: ConnectionManager,
) -> None:
    failing = _StaticMonitor([_batch([_event(1)])])
    failing.poll_error = OSError("read failed")
    healthy = _StaticMonitor([_batch([_event(2)])])
    await reset_minecraft_runtime.replace_connection(_connection(1, failing))
    await reset_minecraft_runtime.replace_connection(_connection(2, healthy, port=25576))
    context = SimpleNamespace(send_action=AsyncMock(return_value=True))

    await mc_main.scheduled(cast(Any, context))

    context.send_action.assert_awaited_once()
    assert healthy.committed


@pytest.mark.asyncio
async def test_group_target_builds_group_action(
    reset_minecraft_runtime: ConnectionManager,
) -> None:
    monitor = _StaticMonitor([_batch([_event(1)])])
    await reset_minecraft_runtime.replace_connection(_connection(88, monitor, kind="group"))
    context = SimpleNamespace(send_action=AsyncMock(return_value=True))

    await mc_main.scheduled(cast(Any, context))

    action = context.send_action.await_args.args[0]
    assert action["action"] == "send_group_msg"
    assert action["params"]["group_id"] == 88


@pytest.mark.asyncio
async def test_skipped_line_summary_handles_unknown_count(
    reset_minecraft_runtime: ConnectionManager,
) -> None:
    monitor = _StaticMonitor([_batch([], skipped_bytes=9_999_999, skipped_lines=None)])
    await reset_minecraft_runtime.replace_connection(_connection(1, monitor))
    context = SimpleNamespace(send_action=AsyncMock(return_value=True))

    await mc_main.scheduled(cast(Any, context))

    text = _message_text(context.send_action.await_args.args[0])
    assert "9999999 字节" in text
    assert "行数未知" in text


@pytest.mark.asyncio
async def test_overlapping_tick_is_skipped(
    reset_minecraft_runtime: ConnectionManager,
) -> None:
    monitor = _StaticMonitor([_batch([_event(1)])])
    await reset_minecraft_runtime.replace_connection(_connection(1, monitor))
    context = SimpleNamespace(send_action=AsyncMock(return_value=True))

    async with mc_main._schedule_lock:  # noqa: SLF001
        await mc_main.scheduled(cast(Any, context))

    context.send_action.assert_not_awaited()
    assert monitor.calls == 0


@pytest.mark.asyncio
async def test_stale_rate_buckets_are_pruned(
    reset_minecraft_runtime: ConnectionManager,
) -> None:
    monitor = _StaticMonitor([_batch([])])
    conn = _connection(1, monitor)
    await reset_minecraft_runtime.replace_connection(conn)
    stale_key = ("old", 1, "private", 99)
    mc_main._event_buckets[stale_key] = mc_main._EventTokenBucket()  # noqa: SLF001

    await mc_main.scheduled(cast(Any, SimpleNamespace(send_action=AsyncMock(return_value=True))))

    assert stale_key not in mc_main._event_buckets  # noqa: SLF001


@pytest.mark.asyncio
async def test_empty_manager_and_connection_without_monitor_do_nothing(
    reset_minecraft_runtime: ConnectionManager,
) -> None:
    context = SimpleNamespace(send_action=AsyncMock(return_value=True))
    await mc_main.scheduled(cast(Any, context))
    await reset_minecraft_runtime.replace_connection(
        McConnection(
            host="mc.example",
            port=25575,
            target=DeliveryTarget("private", 1),
        )
    )
    await mc_main.scheduled(cast(Any, context))
    context.send_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_rejected_cursor_commit_is_contained(
    reset_minecraft_runtime: ConnectionManager,
) -> None:
    monitor = _StaticMonitor([_batch([_event(1)])])
    monitor.commit_result = False
    await reset_minecraft_runtime.replace_connection(_connection(1, monitor))
    context = SimpleNamespace(send_action=AsyncMock(return_value=True))
    await mc_main.scheduled(cast(Any, context))
    context.send_action.assert_awaited_once()
    assert monitor.committed == []


@pytest.mark.asyncio
async def test_send_and_poll_cancellation_propagate(
    reset_minecraft_runtime: ConnectionManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monitor = _StaticMonitor([_batch([_event(1)])])
    await reset_minecraft_runtime.replace_connection(_connection(1, monitor))

    async def cancel_send(_action: dict[str, Any]) -> bool:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await mc_main.scheduled(cast(Any, SimpleNamespace(send_action=cancel_send)))

    async def cancel_poll(*_args: Any, **_kwargs: Any) -> Any:
        raise asyncio.CancelledError

    monkeypatch.setattr(mc_main, "run_sync", cancel_poll)
    with pytest.raises(asyncio.CancelledError):
        await mc_main.scheduled(
            cast(Any, SimpleNamespace(send_action=AsyncMock(return_value=True)))
        )


def test_token_bucket_refill_take_and_refund_are_bounded() -> None:
    bucket = mc_main._EventTokenBucket(tokens=2.5, updated_at=10.0)
    assert bucket.take(10, now=12.0) == 3
    assert bucket.tokens == pytest.approx(0.5)
    bucket.refund(100)
    assert bucket.tokens == mc_main.MC_EVENT_BUCKET_CAPACITY
    assert bucket.take(-1, now=12.0) == 0


@pytest.mark.parametrize(
    "event, marker",
    [
        (LogEvent(LogEventType.CHAT, "A", "hi"), "[MC] A: hi"),
        (LogEvent(LogEventType.JOIN, "A"), "加入了游戏"),
        (LogEvent(LogEventType.LEAVE, "A"), "离开了游戏"),
        (LogEvent(LogEventType.DEATH, "A", "fell"), "💀 A fell"),
        (LogEvent(LogEventType.ADVANCEMENT, "A", "Stone Age"), "获得成就"),
    ],
)
def test_event_formatting_covers_every_event_type(event: LogEvent, marker: str) -> None:
    assert marker in mc_main._format_event_message(event)
