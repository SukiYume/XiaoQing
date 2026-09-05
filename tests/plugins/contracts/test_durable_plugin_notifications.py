from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.durable_fanout import default_group_targets, load_pending
from core.interfaces import DeliveryTarget
from plugins.chime import main as chime
from plugins.earthquake import main as earthquake
from plugins.minecraft import main as minecraft
from plugins.minecraft.log_monitor import LogBatch, LogEvent, LogEventType, LogMonitor


def _chime_context(tmp_path: Path, groups: list[int]) -> SimpleNamespace:
    return SimpleNamespace(
        data_dir       = tmp_path,
        logger         = MagicMock(),
        default_groups = lambda: groups,
        send_action    = AsyncMock(),
    )


def _chime_payload(timestamp: str = "2026-07-14T00:00:00") -> dict:
    return {
        "FRB20260714A": {
            "260714": {
                "timestamp": {"value": timestamp},
                "dm": {"value": "123.4"},
                "snr": {"value": "18.0"},
            },
            "ra": {"value": "12:34:56"},
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


def test_chime_placeholder_timestamp_is_not_a_valid_event() -> None:
    for placeholder in ("N/A", "unknown", "null", "-"):
        data = _chime_payload(placeholder)
        frb  = chime.FRBData("FRB20260714A", data["FRB20260714A"])
        assert frb.is_valid() is False


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
    original_clear = chime.clear_pending
    clear_calls    = 0

    def interrupt_first_clear(path: Path) -> None:
        nonlocal clear_calls
        clear_calls += 1
        if clear_calls == 1:
            raise OSError("crash before outbox clear")
        original_clear(path)

    monkeypatch.setattr(chime, "clear_pending", interrupt_first_clear)
    context.send_action = AsyncMock(return_value=True)

    await chime.scheduled_check(context)
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
        request_id     = "cr275-earthquake",
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

    async def fetch(ctx, force=False, advance_cursor=True):
        del force, advance_cursor
        ctx.state["earthquake_pending_since"] = "200"
        return earthquake.segments("M5 event")

    fetch_mock = AsyncMock(side_effect=fetch)
    monkeypatch.setattr(earthquake, "_fetch_earthquake_news", fetch_mock)
    context.send_action = AsyncMock(side_effect=[True, False])

    await earthquake.scheduled(context)

    pending = load_pending(tmp_path / "earthquake_delivery.json")
    assert pending is not None
    assert pending.delivered == {"group:303"}
    assert earthquake._load_since(context) == "100"

    context.send_action = AsyncMock(return_value=True)
    await earthquake.scheduled(context)

    assert fetch_mock.await_count == 1
    assert context.send_action.await_count == 1
    assert context.send_action.await_args.args[0]["params"]["group_id"] == 404
    assert earthquake._load_since(context) == "200"
    assert load_pending(tmp_path / "earthquake_delivery.json") is None


def _mc_event(index: int = 1) -> LogEvent:
    return LogEvent(
        event_type = LogEventType.CHAT,
        player     = f"Player{index}",
        message    = f"message-{index}",
    )


def test_minecraft_log_cursor_requires_commit_and_survives_restart(tmp_path: Path) -> None:
    log_path   = tmp_path / "latest.log"
    state_path = tmp_path / "cursor.json"
    log_path.write_text(
        "[12:00:00] [Server thread/INFO]: <Player1> first\n",
        encoding="utf-8",
    )
    monitor = LogMonitor(str(log_path), state_path=state_path)
    monitor._initialized   = True
    monitor._last_position = 0

    first   = monitor.check_updates()
    retried = monitor.check_updates()

    assert [event.message for event in first.events] == ["first"]
    assert [event.message for event in retried.events] == ["first"]
    assert monitor._last_position == 0
    assert monitor.commit(first) is True

    restarted = LogMonitor(str(log_path), state_path=state_path)
    assert restarted.initialize() is True
    assert restarted._last_position == log_path.stat().st_size
    with log_path.open("a", encoding="utf-8") as stream:
        stream.write("[12:00:01] [Server thread/INFO]: <Player2> second\n")
    next_batch = restarted.check_updates()
    assert [event.message for event in next_batch.events] == ["second"]


def test_minecraft_cursor_waits_for_complete_trailing_line(tmp_path: Path) -> None:
    log_path = tmp_path / "latest.log"
    prefix   = "[12:00:00] [Server thread/INFO]: <Player1> partial"
    log_path.write_text(prefix, encoding="utf-8")
    monitor                = LogMonitor(str(log_path))
    monitor._initialized   = True
    monitor._last_position = 0

    incomplete = monitor.check_updates()
    assert incomplete.events == []
    assert monitor.commit(incomplete) is True
    assert monitor._last_position == 0

    with log_path.open("a", encoding="utf-8") as stream:
        stream.write(" message\n")
    complete = monitor.check_updates()
    assert [event.message for event in complete.events] == ["partial message"]


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


def test_earthquake_grouped_card_and_changed_html_layout_are_supported() -> None:
    mblog = {
        "id": "900",
        "text": (
            '<span>#地震快讯#</span><a href="/official">中国地震台网正式测定</a>'
            "<div>：某地发生5.1级地震</div>"
        ),
    }
    assert earthquake._iter_mblogs([{"card_group": [{"mblog": mblog}]}]) == [mblog]
    cleaned = earthquake._extract_clean_text(mblog["text"])
    assert "发生5.1级地震" in cleaned
    assert "<" not in cleaned
