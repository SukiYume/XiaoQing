"""CHIME/FRB 插件的运行时契约与状态测试。"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from core.durable_fanout import load_pending
from plugins.chime import main as chime
from tests.helpers.assertions import logged_events
from tests.helpers.filesystem_faults import fail_replace_onto
from tests.helpers.http import AiohttpResponse, QueuedAiohttpSession, aiohttp_json_response
from tests.helpers.paths import REPOSITORY_ROOT

ROOT = REPOSITORY_ROOT


@pytest.fixture
def chime_context(tmp_path: Path) -> SimpleNamespace:
    data_dir = tmp_path / "chime-data"
    data_dir.mkdir()
    return SimpleNamespace(
        data_dir     = data_dir,
        http_session = MagicMock(),
        request_id   = "test-chime",
    )


def _payload(
    name: str      = "FRB20260714A",
    timestamp: str = "2026-07-14T00:00:00",
    *,
    pulse_key: str = "260714",
) -> dict[str, dict]:
    return {
        name: {
            pulse_key: {
                "timestamp": {"value": timestamp},
                "dm"       : {"value": "123.4"},
                "snr"      : {"value": "18.0"},
            },
            "ra" : {"value": "12:34:56"},
            "dec": {"value": "+01:02:03"},
        }
    }


def _frb(
    name: str      = "FRB20260714A",
    timestamp: str = "2026-07-14T00:00:00",
) -> chime.FRBData:
    data = _payload(name, timestamp)
    return chime.FRBData(name, data[name])


def _history_path(context: SimpleNamespace) -> Path:
    return context.data_dir / "chime_history.json"


def _write_history(context: SimpleNamespace, mapping: object) -> None:
    """把历史基线按插件自己的文件名写进真实数据目录（紧凑排版，便于发现被重写）。"""
    _history_path(context).write_text(json.dumps(mapping, separators=(",", ":")), encoding="utf-8")


def _read_history(context: SimpleNamespace) -> dict[str, str]:
    return json.loads(_history_path(context).read_text(encoding="utf-8"))


class _GatedResponse(AiohttpResponse):
    """HTTP 响应在进入时先停在屏障上，用来在网络边界制造并发的冷请求。"""

    def __init__(self, body: bytes, entered: asyncio.Event, release: asyncio.Event) -> None:
        super().__init__(body, media_type="application/json")
        self._entered = entered
        self._release = release

    async def __aenter__(self) -> AiohttpResponse:
        self._entered.set()
        await self._release.wait()
        return self


class TestChimeRuntimeContract:
    def test_data_model_fails_closed_for_empty_payload(self) -> None:
        frb = chime.FRBData("FRB-test", {})
        assert frb.name == "FRB-TEST"
        assert frb.is_valid() is False

    def test_pulse_key_requires_a_complete_six_digit_match(self) -> None:
        info = {
            "260713-extra": {"timestamp": {"value": "2099-01-01T00:00:00"}},
            "991332"      : {"timestamp": {"value": "2099-01-01T00:00:00"}},
            "991231"      : {"timestamp": {"value": "1999-12-31T00:00:00"}},
            "000101"      : {"timestamp": {"value": "2000-01-01T00:00:00"}},
            "260714"      : {"timestamp": {"value": "2026-07-14T00:00:00"}},
        }
        frb = chime.FRBData("frb20260714a", info)

        assert frb.name == "FRB20260714A"
        assert frb.pulses == ("991231", "000101", "260714")
        assert frb.latest_pulse == "260714"
        assert frb.is_valid() is True

    @pytest.mark.parametrize(
        "timestamp",
        ["N/A", "unknown", "null", "-", "new", "2026-02-30T00:00:00", "2026-07-14"],
    )
    def test_invalid_timestamp_is_not_an_event(self, timestamp: str) -> None:
        assert _frb(timestamp=timestamp).is_valid() is False

    @pytest.mark.parametrize(
        "timestamp",
        [
            "2026-07-14 00:00:00.123456",
            "2026-07-14T00:00:00",
            "2026-07-14T00:00:00Z",
            "2026-07-14T02:00:00+02:00",
        ],
    )
    def test_supported_timestamp_forms_are_valid(self, timestamp: str) -> None:
        assert _frb(timestamp=timestamp).is_valid() is True

    def test_external_display_fields_are_bounded_scalars(self) -> None:
        info                  = _payload()["FRB20260714A"]
        info["260714"]["dm"]  = {"value": "line\nbreak"}
        info["260714"]["snr"] = {"value": float("inf")}
        info["ra"]            = {"value": [1, 2]}
        info["dec"]           = {"value": "x" * 129}

        text = chime.FRBData("FRB20260714A", info).format_info()

        assert "DM: N/A" in text
        assert "SNR: N/A" in text
        assert "RA: N/A" in text
        assert "DEC: N/A" in text
        assert "line\nbreak" not in text

    def test_non_mapping_record_is_invalid(self) -> None:
        assert chime.FRBData("FRB20260714A", []).is_valid() is False


class TestChimePluginJson:
    @pytest.fixture
    def manifest(self) -> dict:
        path = ROOT / "plugins" / "chime" / "plugin.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def test_command_triggers(self, manifest: dict) -> None:
        command = next(item for item in manifest["commands"] if item["name"] == "chime")
        assert command["triggers"] == ["chime", "frb"]
        assert command["admin_only"] is False

    def test_schedule_contract(self, manifest: dict) -> None:
        task = next(item for item in manifest["schedule"] if item["id"] == "chime_check")
        assert task["handler"] == "scheduled_check"
        assert task["cron"] == {"hour": "9,21", "minute": 0}


class TestChimeParsingAndHistory:
    def test_parse_skips_invalid_records_without_logging_raw_name(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        malicious_name       = "FRB-BAD\nSECRET"
        data                 = _payload()
        data[malicious_name] = _payload("FRB-BAD")["FRB-BAD"]

        with caplog.at_level(logging.DEBUG):
            parsed = chime.parse_frb_data(data)

        assert [item.name for item in parsed] == ["FRB20260714A"]
        [skipped] = logged_events(caplog, "catalog.entries_invalid")
        assert skipped.fields["invalid_count"] == 1
        assert "SECRET" not in caplog.text

    def test_parse_rejects_names_that_collide_after_normalization(self) -> None:
        data = _payload("FRB20260714A")
        data.update(_payload("frb20260714a"))

        assert chime.parse_frb_data(data) == []

    def test_parse_rejects_an_oversized_catalog(self) -> None:
        catalog: dict[str, dict] = {}
        for index in range(5_000):
            catalog.update(_payload(f"FRB{index:05d}"))
        assert len(chime.parse_frb_data(catalog)) == 5_000

        catalog.update(_payload("FRB05000"))
        assert chime.parse_frb_data(catalog) == []

    def test_missing_history_is_empty_and_valid_history_round_trips(
        self,
        chime_context: SimpleNamespace,
    ) -> None:
        assert chime.load_history(chime_context) == {}
        mapping = {"FRB20260714A": "2026-07-14 00:00:00.123456"}

        assert chime.save_history(chime_context, mapping) is True
        assert chime.load_history(chime_context) == mapping

    @pytest.mark.parametrize(
        "value",
        [
            [],
            {"bad": "2026-07-14T00:00:00"},
            {"FRB20260714A": "not-a-time"},
            {"frb20260714a": "2026-07-14T00:00:00"},
        ],
    )
    def test_invalid_history_shape_is_rejected(
        self,
        chime_context: SimpleNamespace,
        value: object,
    ) -> None:
        path = chime_context.data_dir / "chime_history.json"
        path.write_text(json.dumps(value), encoding="utf-8")

        with pytest.raises(chime.ChimeHistoryError):
            chime.load_history(chime_context)

    def test_malformed_history_json_is_not_treated_as_empty(
        self,
        chime_context: SimpleNamespace,
    ) -> None:
        (chime_context.data_dir / "chime_history.json").write_text("{broken", encoding="utf-8")

        with pytest.raises(json.JSONDecodeError):
            chime.load_history(chime_context)

    def test_save_rejects_invalid_history(self, chime_context: SimpleNamespace) -> None:
        assert chime.save_history(chime_context, {"bad": "value"}) is False
        assert not (chime_context.data_dir / "chime_history.json").exists()

    def test_history_record_limit_is_enforced(self, chime_context: SimpleNamespace) -> None:
        history = {f"FRB{index:05d}": "2026-07-14T00:00:00" for index in range(10_000)}
        _write_history(chime_context, history)
        assert chime.load_history(chime_context) == history

        history["FRB10000"] = "2026-07-14T00:00:00"
        _write_history(chime_context, history)
        with pytest.raises(chime.ChimeHistoryError):
            chime.load_history(chime_context)

    def test_find_updates_uses_chronology_and_merge_never_regresses(self) -> None:
        old = {
            "FRB-A"      : "2026-07-14T00:00:00",
            "FRB-B"      : "2026-07-14T00:00:00",
            "FRB-MISSING": "2026-07-10T00:00:00",
        }
        current = [
            _frb("FRB-A", "2026-07-15T00:00:00"),
            _frb("FRB-B", "2026-07-13T00:00:00"),
            _frb("FRB-NEW", "2026-07-14T12:00:00"),
        ]

        new_repeaters, new_pulses = chime.find_updates(current, old)
        merged = chime.merge_history(old, current)

        assert [item.name for item in new_repeaters] == ["FRB-NEW"]
        assert [item.name for item in new_pulses] == ["FRB-A"]
        assert merged == {
            "FRB-A"      : "2026-07-15T00:00:00",
            "FRB-B"      : "2026-07-14T00:00:00",
            "FRB-MISSING": "2026-07-10T00:00:00",
            "FRB-NEW"    : "2026-07-14T12:00:00",
        }

    def test_equivalent_timestamp_forms_do_not_trigger_an_update(self) -> None:
        old     = {"FRB-A": "2026-07-14 00:00:00"}
        current = [_frb("FRB-A", "2026-07-14T00:00:00Z")]

        assert chime.find_updates(current, old) == ([], [])
        assert chime.merge_history(old, current) == old

    def test_merge_rejects_an_invalid_model(self) -> None:
        with pytest.raises(ValueError, match="invalid FRB"):
            chime.merge_history({}, [chime.FRBData("FRB-A", {})])

    def test_update_message_limits_each_section_to_five_records(self) -> None:
        new_repeaters = [_frb(f"FRB-N{index}") for index in range(6)]
        new_pulses    = [_frb(f"FRB-P{index}") for index in range(6)]

        message = chime.format_update_message(
            new_repeaters,
            new_pulses,
            is_scheduled=True,
        )

        assert message.startswith("🔔")
        assert "**" not in message
        assert "FRB-N5" not in message
        assert "FRB-P5" not in message
        assert message.count("... 还有 1 个") == 2


@pytest.mark.asyncio
async def test_fetch_rejects_non_object_json(chime_context: SimpleNamespace) -> None:
    chime_context.http_session = QueuedAiohttpSession(aiohttp_json_response([]))

    assert await chime.fetch_chime_repeaters(chime_context) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [TimeoutError(), aiohttp.ClientConnectionError("down")],
    ids=["timeout", "network"],
)
async def test_fetch_upstream_failure_is_a_normal_unavailable_result(
    chime_context: SimpleNamespace,
    caplog: pytest.LogCaptureFixture,
    failure: Exception,
) -> None:
    chime_context.http_session = MagicMock(request=MagicMock(side_effect=failure))

    with caplog.at_level(logging.WARNING):
        assert await chime.fetch_chime_repeaters(chime_context) is None
    # 上游超时或网络故障是已处理的降级：只有 WARNING，没有 public_error。
    assert caplog.records
    assert all(record.levelno == logging.WARNING for record in caplog.records)


@pytest.mark.asyncio
async def test_list_command_sorts_latest_frbs_without_reading_history(
    monkeypatch: pytest.MonkeyPatch,
    chime_context: SimpleNamespace,
) -> None:
    _history_path(chime_context).write_text("{broken", encoding="utf-8")
    data = {}
    data.update(_payload("FRB-A", "2026-07-13T00:00:00"))
    data.update(_payload("FRB-B", "2026-07-15T00:00:00"))
    data.update(_payload("FRB-C", "2026-07-14T00:00:00"))
    data.update(_payload("FRB-D", "2026-07-12T00:00:00"))
    data.update(_payload("FRB-E", "2026-07-11T00:00:00"))
    data.update(_payload("FRB-F", "2026-07-10T00:00:00"))
    monkeypatch.setattr(chime, "fetch_chime_repeaters", AsyncMock(return_value=data))

    result = await chime.handle("chime", "list", {}, chime_context)

    text = str(result)
    assert text.index("FRB-B") < text.index("FRB-A")
    assert "FRB-F" not in text
    assert "共 6 个" in text


@pytest.mark.asyncio
async def test_catalog_cache_avoids_repeated_manual_downloads(
    chime_context: SimpleNamespace,
) -> None:
    chime_context.state = {}
    data                = _payload()
    session             = QueuedAiohttpSession(
        aiohttp_json_response(data),
        aiohttp_json_response(data),
    )
    chime_context.http_session = session

    first  = await chime.fetch_chime_repeaters(chime_context)
    second = await chime.fetch_chime_repeaters(chime_context)

    assert first == second == data
    assert len(session.requests) == 1

    await chime.fetch_chime_repeaters(chime_context, force_refresh=True)
    assert len(session.requests) == 2


@pytest.mark.asyncio
async def test_catalog_cache_coalesces_concurrent_cold_requests(
    monkeypatch: pytest.MonkeyPatch,
    chime_context: SimpleNamespace,
) -> None:
    chime_context.state = {}
    monkeypatch.setattr(chime, "_CATALOG_FETCH_LOCK", asyncio.Lock())
    entered = asyncio.Event()
    release = asyncio.Event()
    data    = _payload()
    session = QueuedAiohttpSession(
        _GatedResponse(json.dumps(data).encode("utf-8"), entered, release),
    )
    chime_context.http_session = session

    first = asyncio.create_task(chime.fetch_chime_repeaters(chime_context))
    await entered.wait()
    second = asyncio.create_task(chime.fetch_chime_repeaters(chime_context))
    await asyncio.sleep(0)

    assert len(session.requests) == 1
    release.set()
    assert await asyncio.gather(first, second) == [data, data]
    assert len(session.requests) == 1


@pytest.mark.asyncio
async def test_specific_frb_query_is_case_insensitive(
    monkeypatch: pytest.MonkeyPatch,
    chime_context: SimpleNamespace,
) -> None:
    monkeypatch.setattr(chime, "fetch_chime_repeaters", AsyncMock(return_value=_payload()))

    result = await chime.handle("chime", "frb20260714a", {}, chime_context)

    assert "FRB: FRB20260714A" in str(result)


@pytest.mark.asyncio
async def test_missing_specific_frb_returns_bounded_query_name(
    monkeypatch: pytest.MonkeyPatch,
    chime_context: SimpleNamespace,
) -> None:
    monkeypatch.setattr(chime, "fetch_chime_repeaters", AsyncMock(return_value=_payload()))

    result = await chime.handle("chime", "frb404", {}, chime_context)

    assert "FRB404" in str(result)
    assert "未找到" in str(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("args", ["unknown-subcommand", "list extra", "--help"])
async def test_invalid_command_returns_local_help_without_fetching(
    args: str,
    monkeypatch: pytest.MonkeyPatch,
    chime_context: SimpleNamespace,
) -> None:
    fetch = AsyncMock()
    monkeypatch.setattr(chime, "fetch_chime_repeaters", fetch)

    result = await chime.handle("chime", args, {}, chime_context)

    assert "CHIME" in str(result)
    if args == "unknown-subcommand":
        assert "未知命令" in str(result)
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_help_command_is_local(
    monkeypatch: pytest.MonkeyPatch,
    chime_context: SimpleNamespace,
) -> None:
    fetch = AsyncMock()
    monkeypatch.setattr(chime, "fetch_chime_repeaters", fetch)

    help_response = str(await chime.handle("chime", "帮助", {}, chime_context))
    assert "用法" in help_response
    assert "**" not in help_response
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_unavailable_catalog_returns_stable_message(
    monkeypatch: pytest.MonkeyPatch,
    chime_context: SimpleNamespace,
) -> None:
    monkeypatch.setattr(chime, "fetch_chime_repeaters", AsyncMock(return_value=None))

    result = await chime.handle("chime", "", {}, chime_context)

    assert "无法获取" in str(result)


@pytest.mark.asyncio
async def test_catalog_without_valid_records_returns_stable_message(
    monkeypatch: pytest.MonkeyPatch,
    chime_context: SimpleNamespace,
) -> None:
    monkeypatch.setattr(
        chime,
        "fetch_chime_repeaters",
        AsyncMock(return_value={"bad": {}}),
    )

    result = await chime.handle("chime", "", {}, chime_context)

    assert "未能解析到有效" in str(result)


@pytest.mark.asyncio
async def test_overlong_arguments_are_rejected_before_parsing(
    chime_context: SimpleNamespace,
) -> None:
    result = await chime.handle("chime", "x" * 129, {}, chime_context)

    # 解析后的未知子命令会得到“未知命令”；过长参数在解析前就被拒绝。
    assert "参数格式错误" in str(result)
    assert "未知命令" not in str(result)


@pytest.mark.asyncio
async def test_manual_preview_never_saves_history(
    monkeypatch: pytest.MonkeyPatch,
    chime_context: SimpleNamespace,
) -> None:
    data = _payload(timestamp="2026-07-15T00:00:00")
    _write_history(chime_context, {"FRB20260714A": "2026-07-14T00:00:00"})
    before = _history_path(chime_context).read_bytes()
    monkeypatch.setattr(chime, "fetch_chime_repeaters", AsyncMock(return_value=data))

    result = await chime.handle("chime", "", {}, chime_context)

    assert "检测到新脉冲" in str(result)
    assert _history_path(chime_context).read_bytes() == before
    assert not (chime_context.data_dir / "chime_delivery.json").exists()


@pytest.mark.asyncio
async def test_manual_preview_reports_no_updates(
    monkeypatch: pytest.MonkeyPatch,
    chime_context: SimpleNamespace,
) -> None:
    data = _payload()
    _write_history(chime_context, {"FRB20260714A": "2026-07-14 00:00:00"})
    monkeypatch.setattr(chime, "fetch_chime_repeaters", AsyncMock(return_value=data))

    result = await chime.handle("chime", "", {}, chime_context)

    assert "没有新的重复暴观测" in str(result)


@pytest.mark.asyncio
async def test_scheduled_chime_commits_history_only_after_delivery(
    monkeypatch: pytest.MonkeyPatch,
    chime_context: SimpleNamespace,
) -> None:
    chime_context.default_groups = lambda: [123]
    chime_context.send_action = AsyncMock(return_value=False)
    monkeypatch.setattr(chime, "fetch_chime_repeaters", AsyncMock(return_value=_payload()))

    await chime.scheduled_check(chime_context)
    assert not _history_path(chime_context).exists()
    retained = load_pending(chime_context.data_dir / "chime_delivery.json")
    assert retained is not None
    assert retained.delivered == set()

    chime_context.send_action = AsyncMock(return_value=True)
    await chime.scheduled_check(chime_context)

    assert _read_history(chime_context) == {"FRB20260714A": "2026-07-14T00:00:00"}
    assert load_pending(chime_context.data_dir / "chime_delivery.json") is None


@pytest.mark.asyncio
async def test_scheduled_chime_uses_at_most_once_policy_for_unknown_delivery(
    monkeypatch: pytest.MonkeyPatch,
    chime_context: SimpleNamespace,
    caplog: pytest.LogCaptureFixture,
) -> None:
    chime_context.default_groups = lambda: [123]
    chime_context.send_action = AsyncMock(return_value=None)
    monkeypatch.setattr(chime, "fetch_chime_repeaters", AsyncMock(return_value=_payload()))

    with caplog.at_level(logging.WARNING):
        await chime.scheduled_check(chime_context)

    assert _read_history(chime_context) == {"FRB20260714A": "2026-07-14T00:00:00"}
    assert load_pending(chime_context.data_dir / "chime_delivery.json") is None
    [unknown] = logged_events(caplog, "target.delivery_unknown")
    assert unknown.fields["policy"] == "at-most-once"


@pytest.mark.asyncio
async def test_scheduled_no_update_does_not_rewrite_identical_history(
    monkeypatch: pytest.MonkeyPatch,
    chime_context: SimpleNamespace,
) -> None:
    chime_context.default_groups = lambda: [123]
    chime_context.send_action = AsyncMock(return_value=True)
    _write_history(chime_context, {"FRB20260714A": "2026-07-14 00:00:00"})
    before = _history_path(chime_context).read_bytes()
    monkeypatch.setattr(chime, "fetch_chime_repeaters", AsyncMock(return_value=_payload()))

    await chime.scheduled_check(chime_context)

    chime_context.send_action.assert_not_awaited()
    assert _history_path(chime_context).read_bytes() == before


@pytest.mark.asyncio
async def test_scheduled_update_without_target_keeps_history(
    monkeypatch: pytest.MonkeyPatch,
    chime_context: SimpleNamespace,
) -> None:
    chime_context.default_groups = lambda: []
    monkeypatch.setattr(chime, "fetch_chime_repeaters", AsyncMock(return_value=_payload()))

    await chime.scheduled_check(chime_context)

    assert not _history_path(chime_context).exists()
    assert load_pending(chime_context.data_dir / "chime_delivery.json") is None


@pytest.mark.asyncio
async def test_scheduled_corrupt_history_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    chime_context: SimpleNamespace,
) -> None:
    send = AsyncMock(return_value=True)
    chime_context.default_groups = lambda: [123]
    chime_context.send_action    = send
    monkeypatch.setattr(chime, "fetch_chime_repeaters", AsyncMock(return_value=_payload()))
    _history_path(chime_context).write_text("[]", encoding="utf-8")

    assert await chime.scheduled_check(chime_context) == []
    send.assert_not_awaited()
    assert _history_path(chime_context).read_text(encoding="utf-8") == "[]"
    assert not (chime_context.data_dir / "chime_delivery.json").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [None, {"bad": {}}])
async def test_scheduled_unavailable_or_invalid_catalog_stops_before_history(
    payload: object,
    monkeypatch: pytest.MonkeyPatch,
    chime_context: SimpleNamespace,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # 历史文件是坏的：真去读它会在日志里留下错误记录。
    _history_path(chime_context).write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(chime, "fetch_chime_repeaters", AsyncMock(return_value=payload))

    with caplog.at_level(logging.DEBUG):
        assert await chime.scheduled_check(chime_context) == []
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]


@pytest.mark.asyncio
async def test_delivery_ack_persistence_failure_stops_fanout(
    monkeypatch: pytest.MonkeyPatch,
    chime_context: SimpleNamespace,
) -> None:
    chime_context.default_groups = lambda: [101, 202]
    chime_context.send_action = AsyncMock(return_value=True)
    path    = chime_context.data_dir / "chime_delivery.json"
    pending = chime.create_pending(
        path,
        event_id = "chime:test-ack",
        payload  = list(chime.segments("message")),
        targets  = chime.default_group_targets(chime_context),
        commit   = {"history": {"FRB-A": "2026-07-14T00:00:00"}},
    )
    fail_replace_onto(monkeypatch, "chime_delivery.json", "disk failed")

    assert await chime._deliver_pending(chime_context, pending) is False
    assert chime_context.send_action.await_count == 1
    persisted = load_pending(path)
    assert persisted is not None
    assert persisted.delivered == set()


@pytest.mark.asyncio
async def test_delivery_exception_retains_target_without_logging_details(
    chime_context: SimpleNamespace,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret                       = "must-not-appear"
    chime_context.default_groups = lambda: [101]
    chime_context.send_action = AsyncMock(side_effect=RuntimeError(secret))
    path    = chime_context.data_dir / "chime_delivery.json"
    pending = chime.create_pending(
        path,
        event_id = "chime:test-send-error",
        payload  = list(chime.segments("message")),
        targets  = chime.default_group_targets(chime_context),
        commit   = {"history": {"FRB-A": "2026-07-14T00:00:00"}},
    )

    with caplog.at_level(logging.DEBUG):
        assert await chime._deliver_pending(chime_context, pending) is False
    persisted = load_pending(path)
    assert persisted is not None
    assert persisted.delivered == set()
    assert logged_events(caplog, "target.send_failed")
    assert secret not in caplog.text


@pytest.mark.asyncio
async def test_completed_delivery_retains_outbox_when_history_commit_fails(
    monkeypatch: pytest.MonkeyPatch,
    chime_context: SimpleNamespace,
) -> None:
    chime_context.default_groups = lambda: [101]
    chime_context.send_action = AsyncMock(return_value=True)
    path    = chime_context.data_dir / "chime_delivery.json"
    pending = chime.create_pending(
        path,
        event_id = "chime:test-commit",
        payload  = list(chime.segments("message")),
        targets  = chime.default_group_targets(chime_context),
        commit   = {"history": {"FRB-A": "2026-07-14T00:00:00"}},
    )
    fail_replace_onto(monkeypatch, "chime_history.json", "disk failed")

    assert await chime._deliver_pending(chime_context, pending) is False
    persisted = load_pending(path)
    assert persisted is not None
    assert persisted.complete is True
