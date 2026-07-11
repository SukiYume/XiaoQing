from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from plugins.minecraft import main as mc_main
from plugins.minecraft.log_monitor import LogBatch, LogEvent, LogEventType, LogMonitor


def _event(index: int, *, message: str | None = None) -> LogEvent:
    return LogEvent(
        event_type=LogEventType.CHAT,
        player=f"Player{index}",
        message=message if message is not None else f"message-{index}",
        raw_line=f"[12:00:00] [Server thread/INFO]: <Player{index}> message-{index}",
        timestamp="12:00:00",
    )


class StaticMonitor:
    def __init__(self, batches: list[LogBatch | list[LogEvent]]) -> None:
        self.batches = list(batches)
        self.calls = 0

    async def check_updates_async(self):
        self.calls += 1
        if self.batches:
            return self.batches.pop(0)
        return LogBatch(events=[])


def _connection(
    target_id: int,
    monitor: Any,
    *,
    host: str = "mc.example",
    port: int = 25575,
) -> mc_main.McConnection:
    return mc_main.McConnection(
        host=host,
        port=port,
        password="secret",
        log_file="latest.log",
        target_type="private",
        target_id=target_id,
        log_monitor=monitor,
    )


def _message_text(action: dict[str, Any]) -> str:
    return "".join(
        segment["data"].get("text", "")
        for segment in action["params"]["message"]
        if segment.get("type") == "text"
    )


@pytest.fixture(autouse=True)
def reset_minecraft_runtime(monkeypatch: pytest.MonkeyPatch):
    manager = mc_main.ConnectionManager()
    monkeypatch.setattr(mc_main, "_manager", manager)
    mc_main._event_buckets.clear()  # noqa: SLF001 - isolate global rate state.
    yield manager
    mc_main._event_buckets.clear()  # noqa: SLF001


def test_log_batch_counts_all_matches_and_retains_bounded_latest_events(tmp_path: Path):
    log_path = tmp_path / "latest.log"
    lines = [
        f"[12:00:00] [Server thread/INFO]: <Player{index}> message-{index}\n"
        for index in range(1500)
    ]
    log_path.write_text("".join(lines), encoding="utf-8")
    monitor = LogMonitor(str(log_path))
    monitor._initialized = True  # noqa: SLF001
    monitor._last_position = 0  # noqa: SLF001
    monitor.MAX_READ_BYTES = log_path.stat().st_size + 1

    batch = monitor.check_updates()

    assert batch.matched_total == 1500
    assert batch.dropped_events == 500
    assert len(batch) == 1000
    assert batch[0].player == "Player500"
    assert batch[-1].player == "Player1499"


def test_log_batch_reports_exact_small_tail_skip_metrics(tmp_path: Path):
    log_path = tmp_path / "latest.log"
    payload = (b"ignored line\n" * 80) + (b"[12:00:00] [Server thread/INFO]: <Steve> bounded\n")
    log_path.write_bytes(payload)
    monitor = LogMonitor(str(log_path))
    monitor._initialized = True  # noqa: SLF001
    monitor._last_position = 0  # noqa: SLF001
    monitor.MAX_READ_BYTES = 128
    read_start = len(payload) - monitor.MAX_READ_BYTES
    boundary_partial = read_start > 0 and payload[read_start - 1 : read_start] != b"\n"
    partial_end = payload.find(b"\n", read_start) + 1 if boundary_partial else read_start
    expected_bytes = partial_end if boundary_partial else read_start
    expected_lines = payload[:read_start].count(b"\n") + int(boundary_partial)

    batch = monitor.check_updates()

    assert batch.skipped_bytes == expected_bytes
    assert batch.skipped_lines == expected_lines
    assert len(batch) == 1
    assert batch[0].message == "bounded"


@pytest.mark.asyncio
async def test_thousand_events_produce_one_bounded_action_with_exact_drop_count(
    reset_minecraft_runtime,
):
    events = [_event(index) for index in range(1000)]
    reset_minecraft_runtime.add_connection(
        _connection(1, StaticMonitor([LogBatch(events=events, matched_total=1000)]))
    )
    context = SimpleNamespace(send_action=AsyncMock())

    await mc_main.scheduled(context)

    context.send_action.assert_awaited_once()
    text = _message_text(context.send_action.await_args.args[0])
    assert "另有 988 条 MC 事件" in text
    assert len(text) <= mc_main.MC_MAX_ACTION_CHARS
    assert len(text.encode("utf-8")) <= mc_main.MC_MAX_ACTION_BYTES


@pytest.mark.asyncio
async def test_small_batch_preserves_event_order(reset_minecraft_runtime):
    reset_minecraft_runtime.add_connection(
        _connection(1, StaticMonitor([[_event(1), _event(2), _event(3)]]))
    )
    context = SimpleNamespace(send_action=AsyncMock())

    await mc_main.scheduled(context)

    text = _message_text(context.send_action.await_args.args[0])
    assert text.index("Player1") < text.index("Player2") < text.index("Player3")
    assert "折叠/丢弃" not in text


@pytest.mark.asyncio
async def test_sustained_multi_player_flood_uses_one_per_server_bucket(
    reset_minecraft_runtime,
):
    flood = [_event(index) for index in range(1000)]
    monitor = StaticMonitor([LogBatch(events=flood, matched_total=1000) for _ in range(3)])
    reset_minecraft_runtime.add_connection(_connection(1, monitor))
    context = SimpleNamespace(send_action=AsyncMock())

    await mc_main.scheduled(context)
    await mc_main.scheduled(context)
    await mc_main.scheduled(context)

    texts = [_message_text(call.args[0]) for call in context.send_action.await_args_list]
    assert len(texts) == 3
    assert "Player0" in texts[0]
    assert "Player0" in texts[1]
    assert "Player0" not in texts[2]
    assert "另有 1000 条 MC 事件" in texts[2]
    assert len(mc_main._event_buckets) == 1  # noqa: SLF001


@pytest.mark.asyncio
async def test_global_tick_action_limit_reserves_one_overflow_notice(
    reset_minecraft_runtime,
):
    for index in range(10):
        reset_minecraft_runtime.add_connection(
            _connection(
                100 + index,
                StaticMonitor([[_event(index)]]),
                port=25575 + index,
            )
        )
    context = SimpleNamespace(send_action=AsyncMock())

    await mc_main.scheduled(context)

    assert context.send_action.await_count == mc_main.MC_MAX_ACTIONS_PER_TICK
    last_text = _message_text(context.send_action.await_args_list[-1].args[0])
    assert "另有 6 个连接、6 条日志事件未转发" in last_text


@pytest.mark.asyncio
async def test_send_failure_and_timeout_do_not_block_other_connections(
    reset_minecraft_runtime,
    monkeypatch: pytest.MonkeyPatch,
):
    for index in range(4):
        reset_minecraft_runtime.add_connection(
            _connection(index + 1, StaticMonitor([[_event(index)]]), port=25575 + index)
        )
    monkeypatch.setattr(mc_main, "MC_SEND_TIMEOUT_SECONDS", 0.01)
    delivered: list[int] = []

    async def send_action(action: dict[str, Any]):
        target = int(action["params"]["user_id"])
        if target == 1:
            raise RuntimeError("OneBot down")
        if target == 2:
            await asyncio.sleep(1)
        if target == 3:
            return False
        delivered.append(target)
        return True

    await mc_main.scheduled(SimpleNamespace(send_action=send_action))

    assert delivered == [4]


@pytest.mark.asyncio
async def test_long_unicode_events_still_fit_single_action_budgets(
    reset_minecraft_runtime,
):
    events = [_event(index, message="🧱" * 1000) for index in range(20)]
    reset_minecraft_runtime.add_connection(
        _connection(1, StaticMonitor([LogBatch(events=events, matched_total=20)]))
    )
    context = SimpleNamespace(send_action=AsyncMock())

    await mc_main.scheduled(context)

    text = _message_text(context.send_action.await_args.args[0])
    assert len(text) <= mc_main.MC_MAX_ACTION_CHARS
    assert len(text.encode("utf-8")) <= mc_main.MC_MAX_ACTION_BYTES
    assert "折叠/丢弃" in text
