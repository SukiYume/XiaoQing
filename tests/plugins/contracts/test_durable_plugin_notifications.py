from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.durable_fanout import default_group_targets, load_pending
from core.interfaces import DeliveryTarget
from plugins.chime import main as chime
from plugins.earthquake import main as earthquake
from plugins.minecraft import main as minecraft
from plugins.minecraft.log_monitor import LogBatch, LogMonitor, PlayerEvent, PlayerEventType
from tests.helpers.earthquake_test_support import serve_weibo, weibo_card


def _chime_context(tmp_path: Path, groups: list[int]) -> SimpleNamespace:
    return SimpleNamespace(
        data_dir       = tmp_path,
        default_groups = lambda: groups,
        send_action    = AsyncMock(),
    )


def _chime_payload() -> dict:
    return {
        "FRB20260714A": {
            "260714": {
                "timestamp": {"value": "2026-07-14T00:00:00"},
                "dm"       : {"value": "123.4"},
                "snr"      : {"value": "18.0"},
            },
            "ra" : {"value": "12:34:56"},
            "dec": {"value": "+01:02:03"},
        }
    }


def test_default_group_targets_normalizes_invalid_and_duplicate_values() -> None:
    context = SimpleNamespace(
        default_groups=lambda: [
            "101",
            202,
            "bad",
            -1,
            101,
            None,
            0,
            True,
            1.5,
            " 303 ",
            "+404",
            "１２",
        ],
    )

    assert [target.key for target in default_group_targets(context)] == [
        "group:101",
        "group:202",
    ]
    assert default_group_targets(SimpleNamespace()) == ()


@pytest.mark.asyncio
async def test_chime_partial_fanout_retries_only_unacknowledged_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _chime_context(tmp_path, [101, 202])
    fetch = AsyncMock(return_value=_chime_payload())
    monkeypatch.setattr(chime, "fetch_chime_repeaters", fetch)
    context.send_action = AsyncMock(side_effect=[True, False])

    await chime.scheduled_check(context)

    pending = load_pending(tmp_path / "chime_delivery.json")
    assert pending is not None
    assert pending.delivered == {"group:101"}
    assert chime.load_history(context) == {}

    context.send_action = AsyncMock(return_value=True)
    await chime.scheduled_check(context)

    assert fetch.await_count == 1
    assert context.send_action.await_count == 1
    assert context.send_action.await_args.args[0]["params"]["group_id"] == 202
    assert chime.load_history(context) == {"FRB20260714A": "2026-07-14T00:00:00"}
    assert load_pending(tmp_path / "chime_delivery.json") is None


@pytest.mark.asyncio
async def test_corrupt_chime_fanout_fails_closed_without_resending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _chime_context(tmp_path, [101, 202])
    fetch = AsyncMock(return_value=_chime_payload())
    monkeypatch.setattr(chime, "fetch_chime_repeaters", fetch)
    context.send_action = AsyncMock(side_effect=[True, False])
    await chime.scheduled_check(context)
    (tmp_path / "chime_delivery.json").write_text("{broken", encoding="utf-8")

    context.send_action = AsyncMock(return_value=True)
    await chime.scheduled_check(context)

    context.send_action.assert_not_awaited()
    assert fetch.await_count == 1
    assert chime.load_history(context) == {}


@pytest.mark.asyncio
async def test_chime_commit_before_outbox_clear_does_not_resend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _chime_context(tmp_path, [101])
    fetch = AsyncMock(return_value=_chime_payload())
    monkeypatch.setattr(chime, "fetch_chime_repeaters", fetch)
    real_replace = os.replace
    crashes      = 0

    def replace_that_crashes_once_history_is_committed(src, dst, *args, **kwargs) -> None:
        # 在 OS 层模拟崩溃：历史基线已落盘后，第一次改写发送队列文件失败。
        nonlocal crashes
        if Path(dst).name == "chime_delivery.json" and chime.load_history(context) and not crashes:
            crashes += 1
            raise OSError("crash before outbox clear")
        real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace_that_crashes_once_history_is_committed)
    context.send_action = AsyncMock(return_value=True)

    await chime.scheduled_check(context)
    assert crashes == 1
    assert context.send_action.await_count == 1
    assert chime.load_history(context) == {"FRB20260714A": "2026-07-14T00:00:00"}

    context.send_action = AsyncMock(return_value=True)
    await chime.scheduled_check(context)

    context.send_action.assert_not_awaited()
    assert fetch.await_count == 1
    assert load_pending(tmp_path / "chime_delivery.json") is None


def _earthquake_context(tmp_path: Path, groups: list[int]) -> SimpleNamespace:
    return SimpleNamespace(
        data_dir       = tmp_path,
        state          = {},
        secrets        = {},
        request_id     = "earthquake-test",
        default_groups = lambda: groups,
        send_action    = AsyncMock(),
    )


@pytest.mark.asyncio
async def test_earthquake_partial_fanout_retries_only_unacknowledged_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _earthquake_context(tmp_path, [303, 404])
    earthquake._save_since(context, "100")
    weibo = serve_weibo(monkeypatch, weibo_card("200", magnitude="5.0"))
    context.send_action = AsyncMock(side_effect=[True, False])

    await earthquake.scheduled(context)

    pending = load_pending(tmp_path / "earthquake_delivery.json")
    assert pending is not None
    assert pending.delivered == {"group:303"}
    assert earthquake._load_since(context) == "100"

    context.send_action = AsyncMock(return_value=True)
    await earthquake.scheduled(context)
    await earthquake.shutdown(context)

    assert [call[1] for call in weibo.index_calls] == ["https://m.weibo.cn/api/container/getIndex"]
    assert context.send_action.await_count == 1
    assert context.send_action.await_args.args[0]["params"]["group_id"] == 404
    assert earthquake._load_since(context) == "200"
    assert load_pending(tmp_path / "earthquake_delivery.json") is None


def _mc_event(index: int = 1) -> PlayerEvent:
    return PlayerEvent(
        event_type = PlayerEventType.CHAT,
        player     = f"Player{index}",
        message    = f"message-{index}",
    )


def test_earthquake_corrupt_cursor_without_checkpoint_fails_closed(tmp_path: Path) -> None:
    context = _earthquake_context(tmp_path, [])
    (tmp_path / "earthquake.json").write_text("{broken", encoding="utf-8")

    with pytest.raises(earthquake.EarthquakeStateCorruptionError):
        earthquake._load_since(context)

    assert len(list(tmp_path.glob("earthquake.json.corrupt-*"))) == 1


@pytest.mark.asyncio
async def test_minecraft_failed_send_replays_same_uncommitted_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_path = tmp_path / "latest.log"
    log_path.write_text(
        "[12:00:00] [Server thread/INFO]: <Player1> once\n",
        encoding="utf-8",
    )
    monitor                = LogMonitor(str(log_path))
    monitor._initialized   = True
    monitor._last_position = 0
    manager                = minecraft.ConnectionManager()
    await manager.replace_connection(
        minecraft.McConnection(
            host        = "mc.example",
            port        = 25575,
            target      = DeliveryTarget("private", 505),
            log_monitor = monitor,
        )
    )
    monkeypatch.setattr(minecraft, "_manager", manager)
    minecraft._event_buckets.clear()
    context = SimpleNamespace(send_action=AsyncMock(return_value=False))

    await minecraft.scheduled(context)
    assert monitor._last_position == 0
    first_message = context.send_action.await_args.args[0]["params"]["message"]

    context.send_action = AsyncMock(return_value=True)
    await minecraft.scheduled(context)

    assert context.send_action.await_args.args[0]["params"]["message"] == first_message
    assert monitor._last_position == log_path.stat().st_size


class _TransactionalMonitor:
    def __init__(self, index: int) -> None:
        self.batch = LogBatch(
            events        = [_mc_event(index)],
            matched_total = 1,
            cursor_before = 0,
            cursor_after  = 1,
            file_identity = f"test:{index}",
        )
        self.commits = 0

    def check_updates(self) -> LogBatch:
        return LogBatch(events=[]) if self.commits else self.batch

    def commit(self, batch: LogBatch) -> bool:
        assert batch is self.batch
        self.commits += 1
        return True


@pytest.mark.asyncio
async def test_minecraft_tick_overflow_keeps_unselected_batches_uncommitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager                               = minecraft.ConnectionManager()
    monitors: list[_TransactionalMonitor] = []
    for index in range(10):
        monitor = _TransactionalMonitor(index)
        monitors.append(monitor)
        await manager.replace_connection(
            minecraft.McConnection(
                host        = "mc.example",
                port        = 25575 + index,
                target      = DeliveryTarget("private", 600 + index),
                log_monitor = monitor,
            )
        )
    monkeypatch.setattr(minecraft, "_manager", manager)
    minecraft._event_buckets.clear()
    context = SimpleNamespace(send_action=AsyncMock(return_value=True))

    await minecraft.scheduled(context)

    assert [monitor.commits for monitor in monitors] == [1, 1, 1, 1, 1, 0, 0, 0, 0, 0]
    assert context.send_action.await_count == minecraft.MC_MAX_ACTIONS_PER_TICK
