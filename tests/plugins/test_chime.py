"""CHIME/FRB 插件的运行时契约与状态回归测试。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from core.durable_fanout import load_pending
from plugins.chime import main as chime

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def chime_context(tmp_path: Path) -> SimpleNamespace:
    data_dir = tmp_path / "chime-data"
    data_dir.mkdir()
    return SimpleNamespace(
        data_dir=data_dir,
        http_session=MagicMock(),
        logger=MagicMock(),
        request_id="test-chime",
    )


def _payload(
    name: str = "FRB20260714A",
    timestamp: str = "2026-07-14T00:00:00",
    *,
    pulse_key: str = "260714",
) -> dict[str, dict]:
    return {
        name: {
            pulse_key: {
                "timestamp": {"value": timestamp},
                "dm": {"value": "123.4"},
                "snr": {"value": "18.0"},
            },
            "ra": {"value": "12:34:56"},
            "dec": {"value": "+01:02:03"},
        }
    }


def _frb(
    name: str = "FRB20260714A",
    timestamp: str = "2026-07-14T00:00:00",
) -> chime.FRBData:
    data = _payload(name, timestamp)
    return chime.FRBData(name, data[name])


class TestChimeRuntimeContract:
    def test_entrypoints_help_and_constants(self) -> None:
        assert callable(chime.handle)
        assert callable(chime.scheduled_check)
        assert "CHIME" in chime.HELP_TEXT
        assert chime.MAX_DISPLAY_FRBS == 5
        assert chime.PULSE_DATE_PATTERN == r"\d{6}"

    def test_data_model_fails_closed_for_empty_payload(self) -> None:
        frb = chime.FRBData("FRB-test", {})
        assert frb.name == "FRB-TEST"
        assert frb.is_valid() is False

    def test_pulse_key_requires_a_complete_six_digit_match(self) -> None:
        info = {
            "260713-extra": {"timestamp": {"value": "2099-01-01T00:00:00"}},
            "991332": {"timestamp": {"value": "2099-01-01T00:00:00"}},
            "991231": {"timestamp": {"value": "1999-12-31T00:00:00"}},
            "000101": {"timestamp": {"value": "2000-01-01T00:00:00"}},
            "260714": {"timestamp": {"value": "2026-07-14T00:00:00"}},
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
        info = _payload()["FRB20260714A"]
        info["260714"]["dm"] = {"value": "line\nbreak"}
        info["260714"]["snr"] = {"value": float("inf")}
        info["ra"] = {"value": [1, 2]}
        info["dec"] = {"value": "x" * 129}

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

    def test_manifest_identity_and_entrypoints(self, manifest: dict) -> None:
        assert manifest["name"] == "chime"
        assert manifest["entry"] == "main.py"
        assert manifest["dependencies"] == []

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
        chime_context: SimpleNamespace,
    ) -> None:
        malicious_name = "FRB-BAD\nSECRET"
        data = _payload()
        data[malicious_name] = _payload("FRB-BAD")["FRB-BAD"]

        parsed = chime.parse_frb_data(data, chime_context)

        assert [item.name for item in parsed] == ["FRB20260714A"]
        logged = "\n".join(str(call) for call in chime_context.logger.mock_calls)
        assert malicious_name not in logged

    def test_parse_rejects_names_that_collide_after_normalization(
        self,
        chime_context: SimpleNamespace,
    ) -> None:
        data = _payload("FRB20260714A")
        data.update(_payload("frb20260714a"))

        assert chime.parse_frb_data(data, chime_context) == []

    def test_parse_rejects_an_oversized_catalog(
        self,
        chime_context: SimpleNamespace,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(chime, "_MAX_FRB_RECORDS", 0)
        assert chime.parse_frb_data(_payload(), chime_context) == []

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

    def test_history_record_limit_is_enforced(
        self,
        chime_context: SimpleNamespace,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(chime, "_MAX_HISTORY_RECORDS", 0)
        with pytest.raises(chime.ChimeHistoryError):
            chime._validate_history({"FRB-A": "2026-07-14T00:00:00"})

    def test_find_updates_uses_chronology_and_merge_never_regresses(
        self,
        chime_context: SimpleNamespace,
    ) -> None:
        old = {
            "FRB-A": "2026-07-14T00:00:00",
            "FRB-B": "2026-07-14T00:00:00",
            "FRB-MISSING": "2026-07-10T00:00:00",
        }
        current = [
            _frb("FRB-A", "2026-07-15T00:00:00"),
            _frb("FRB-B", "2026-07-13T00:00:00"),
            _frb("FRB-NEW", "2026-07-14T12:00:00"),
        ]

        new_repeaters, new_pulses = chime.find_updates(current, old, chime_context)
        merged = chime.merge_history(old, current)

        assert [item.name for item in new_repeaters] == ["FRB-NEW"]
        assert [item.name for item in new_pulses] == ["FRB-A"]
        assert merged == {
            "FRB-A": "2026-07-15T00:00:00",
            "FRB-B": "2026-07-14T00:00:00",
            "FRB-MISSING": "2026-07-10T00:00:00",
            "FRB-NEW": "2026-07-14T12:00:00",
        }

    def test_equivalent_timestamp_forms_do_not_trigger_an_update(
        self,
        chime_context: SimpleNamespace,
    ) -> None:
        old = {"FRB-A": "2026-07-14 00:00:00"}
        current = [_frb("FRB-A", "2026-07-14T00:00:00Z")]

        assert chime.find_updates(current, old, chime_context) == ([], [])
        assert chime.merge_history(old, current) == old

    def test_merge_rejects_an_invalid_model(self) -> None:
        with pytest.raises(ValueError, match="invalid FRB"):
            chime.merge_history({}, [chime.FRBData("FRB-A", {})])

    def test_update_message_limits_each_section_to_five_records(self) -> None:
        new_repeaters = [_frb(f"FRB-N{index}") for index in range(6)]
        new_pulses = [_frb(f"FRB-P{index}") for index in range(6)]

        message = chime.format_update_message(
            new_repeaters,
            new_pulses,
            is_scheduled=True,
        )

        assert message.startswith("🔔")
        assert "FRB-N5" not in message
        assert "FRB-P5" not in message
        assert message.count("... 还有 1 个") == 2


@pytest.mark.asyncio
async def test_fetch_rejects_non_object_json(
    monkeypatch: pytest.MonkeyPatch,
    chime_context: SimpleNamespace,
) -> None:
    monkeypatch.setattr(chime, "aiohttp_request_bounded", AsyncMock(return_value=object()))
    monkeypatch.setattr(chime, "parse_bounded_json", Mock(return_value=[]))

    assert await chime.fetch_chime_repeaters(chime_context) is None


@pytest.mark.asyncio
async def test_fetch_timeout_is_a_normal_unavailable_result(
    monkeypatch: pytest.MonkeyPatch,
    chime_context: SimpleNamespace,
) -> None:
    monkeypatch.setattr(
        chime,
        "aiohttp_request_bounded",
        AsyncMock(side_effect=asyncio.TimeoutError),
    )

    assert await chime.fetch_chime_repeaters(chime_context) is None


@pytest.mark.asyncio
async def test_list_command_sorts_latest_frbs_without_reading_history(
    monkeypatch: pytest.MonkeyPatch,
    chime_context: SimpleNamespace,
) -> None:
    data = {}
    data.update(_payload("FRB-A", "2026-07-13T00:00:00"))
    data.update(_payload("FRB-B", "2026-07-15T00:00:00"))
    data.update(_payload("FRB-C", "2026-07-14T00:00:00"))
    data.update(_payload("FRB-D", "2026-07-12T00:00:00"))
    data.update(_payload("FRB-E", "2026-07-11T00:00:00"))
    data.update(_payload("FRB-F", "2026-07-10T00:00:00"))
    monkeypatch.setattr(chime, "fetch_chime_repeaters", AsyncMock(return_value=data))
    monkeypatch.setattr(chime, "load_history", Mock(side_effect=AssertionError("not needed")))

    result = await chime.handle("chime", "list", {}, chime_context)

    text = str(result)
    assert text.index("FRB-B") < text.index("FRB-A")
    assert "FRB-F" not in text
    assert "共 6 个" in text


@pytest.mark.asyncio
async def test_catalog_cache_avoids_repeated_manual_downloads(
    monkeypatch: pytest.MonkeyPatch,
    chime_context: SimpleNamespace,
) -> None:
    chime_context.state = {}
    request = AsyncMock(return_value=object())
    data = _payload()
    monkeypatch.setattr(chime, "aiohttp_request_bounded", request)
    monkeypatch.setattr(chime, "parse_bounded_json", Mock(return_value=data))

    first = await chime.fetch_chime_repeaters(chime_context)
    second = await chime.fetch_chime_repeaters(chime_context)

    assert first == second == data
    request.assert_awaited_once()

    await chime.fetch_chime_repeaters(chime_context, force_refresh=True)
    assert request.await_count == 2


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

    assert "基本用法" in str(await chime.handle("chime", "帮助", {}, chime_context))
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
    monkeypatch: pytest.MonkeyPatch,
    chime_context: SimpleNamespace,
) -> None:
    parser = Mock(side_effect=AssertionError("must not parse"))
    monkeypatch.setattr(chime, "parse", parser)

    result = await chime.handle("chime", "x" * 129, {}, chime_context)

    assert "参数格式错误" in str(result)
    parser.assert_not_called()


@pytest.mark.asyncio
async def test_manual_preview_parses_once_and_never_saves_history(
    monkeypatch: pytest.MonkeyPatch,
    chime_context: SimpleNamespace,
) -> None:
    data = _payload(timestamp="2026-07-15T00:00:00")
    parser = Mock(wraps=chime.parse_frb_data)
    save = Mock(return_value=True)
    monkeypatch.setattr(chime, "fetch_chime_repeaters", AsyncMock(return_value=data))
    monkeypatch.setattr(
        chime,
        "load_history",
        Mock(return_value={"FRB20260714A": "2026-07-14T00:00:00"}),
    )
    monkeypatch.setattr(chime, "parse_frb_data", parser)
    monkeypatch.setattr(chime, "save_history", save)

    result = await chime.handle("chime", "", {}, chime_context)

    assert "检测到新脉冲" in str(result)
    parser.assert_called_once()
    save.assert_not_called()


@pytest.mark.asyncio
async def test_manual_preview_reports_no_updates(
    monkeypatch: pytest.MonkeyPatch,
    chime_context: SimpleNamespace,
) -> None:
    data = _payload()
    monkeypatch.setattr(chime, "fetch_chime_repeaters", AsyncMock(return_value=data))
    monkeypatch.setattr(
        chime,
        "load_history",
        Mock(return_value={"FRB20260714A": "2026-07-14 00:00:00"}),
    )

    result = await chime.handle("chime", "", {}, chime_context)

    assert "没有新的重复暴观测" in str(result)


@pytest.mark.asyncio
async def test_scheduled_chime_commits_history_only_after_delivery(
    monkeypatch: pytest.MonkeyPatch,
    chime_context: SimpleNamespace,
) -> None:
    chime_context.default_groups = lambda: [123]
    chime_context.send_action = AsyncMock(return_value=False)
    save = Mock(return_value=True)
    monkeypatch.setattr(chime, "fetch_chime_repeaters", AsyncMock(return_value=_payload()))
    monkeypatch.setattr(chime, "load_history", Mock(return_value={}))
    monkeypatch.setattr(chime, "save_history", save)

    await chime.scheduled_check(chime_context)
    save.assert_not_called()

    chime_context.send_action = AsyncMock(return_value=True)
    await chime.scheduled_check(chime_context)

    save.assert_called_once_with(
        chime_context,
        {"FRB20260714A": "2026-07-14T00:00:00"},
    )
    assert load_pending(chime_context.data_dir / "chime_delivery.json") is None


@pytest.mark.asyncio
async def test_scheduled_chime_uses_at_most_once_policy_for_unknown_delivery(
    monkeypatch: pytest.MonkeyPatch,
    chime_context: SimpleNamespace,
) -> None:
    chime_context.default_groups = lambda: [123]
    chime_context.send_action = AsyncMock(return_value=None)
    save = Mock(return_value=True)
    monkeypatch.setattr(chime, "fetch_chime_repeaters", AsyncMock(return_value=_payload()))
    monkeypatch.setattr(chime, "load_history", Mock(return_value={}))
    monkeypatch.setattr(chime, "save_history", save)

    await chime.scheduled_check(chime_context)

    save.assert_called_once()
    assert load_pending(chime_context.data_dir / "chime_delivery.json") is None
    assert any("at-most-once" in str(call) for call in chime_context.logger.warning.call_args_list)


@pytest.mark.asyncio
async def test_scheduled_no_update_does_not_rewrite_identical_history(
    monkeypatch: pytest.MonkeyPatch,
    chime_context: SimpleNamespace,
) -> None:
    old = {"FRB20260714A": "2026-07-14 00:00:00"}
    save = Mock(return_value=True)
    monkeypatch.setattr(chime, "fetch_chime_repeaters", AsyncMock(return_value=_payload()))
    monkeypatch.setattr(chime, "load_history", Mock(return_value=old))
    monkeypatch.setattr(chime, "save_history", save)

    await chime.scheduled_check(chime_context)

    save.assert_not_called()


@pytest.mark.asyncio
async def test_scheduled_update_without_target_keeps_history(
    monkeypatch: pytest.MonkeyPatch,
    chime_context: SimpleNamespace,
) -> None:
    chime_context.default_groups = lambda: []
    save = Mock(return_value=True)
    monkeypatch.setattr(chime, "fetch_chime_repeaters", AsyncMock(return_value=_payload()))
    monkeypatch.setattr(chime, "load_history", Mock(return_value={}))
    monkeypatch.setattr(chime, "save_history", save)

    await chime.scheduled_check(chime_context)

    save.assert_not_called()
    assert load_pending(chime_context.data_dir / "chime_delivery.json") is None


@pytest.mark.asyncio
async def test_scheduled_corrupt_history_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    chime_context: SimpleNamespace,
) -> None:
    send = AsyncMock(return_value=True)
    chime_context.default_groups = lambda: [123]
    chime_context.send_action = send
    monkeypatch.setattr(chime, "fetch_chime_repeaters", AsyncMock(return_value=_payload()))
    monkeypatch.setattr(
        chime,
        "load_history",
        Mock(side_effect=chime.ChimeHistoryError("broken")),
    )

    assert await chime.scheduled_check(chime_context) == []
    send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [None, {"bad": {}}])
async def test_scheduled_unavailable_or_invalid_catalog_stops_before_history(
    payload: object,
    monkeypatch: pytest.MonkeyPatch,
    chime_context: SimpleNamespace,
) -> None:
    history = Mock(side_effect=AssertionError("must not read history"))
    monkeypatch.setattr(chime, "fetch_chime_repeaters", AsyncMock(return_value=payload))
    monkeypatch.setattr(chime, "load_history", history)

    assert await chime.scheduled_check(chime_context) == []
    history.assert_not_called()


@pytest.mark.asyncio
async def test_delivery_ack_persistence_failure_stops_fanout(
    monkeypatch: pytest.MonkeyPatch,
    chime_context: SimpleNamespace,
) -> None:
    chime_context.default_groups = lambda: [101, 202]
    chime_context.send_action = AsyncMock(return_value=True)
    path = chime_context.data_dir / "chime_delivery.json"
    pending = chime.create_pending(
        path,
        event_id="chime:test-ack",
        payload=list(chime.segments("message")),
        targets=chime.default_group_targets(chime_context),
        commit={"history": {"FRB-A": "2026-07-14T00:00:00"}},
    )
    monkeypatch.setattr(chime, "mark_delivered", Mock(side_effect=OSError("disk failed")))

    assert await chime._deliver_pending(chime_context, pending) is False
    assert chime_context.send_action.await_count == 1
    persisted = load_pending(path)
    assert persisted is not None
    assert persisted.delivered == set()


@pytest.mark.asyncio
async def test_delivery_exception_retains_target_without_logging_details(
    chime_context: SimpleNamespace,
) -> None:
    secret = "must-not-appear"
    chime_context.default_groups = lambda: [101]
    chime_context.send_action = AsyncMock(side_effect=RuntimeError(secret))
    path = chime_context.data_dir / "chime_delivery.json"
    pending = chime.create_pending(
        path,
        event_id="chime:test-send-error",
        payload=list(chime.segments("message")),
        targets=chime.default_group_targets(chime_context),
        commit={"history": {"FRB-A": "2026-07-14T00:00:00"}},
    )

    assert await chime._deliver_pending(chime_context, pending) is False
    persisted = load_pending(path)
    assert persisted is not None
    assert persisted.delivered == set()
    assert secret not in "\n".join(str(call) for call in chime_context.logger.mock_calls)


@pytest.mark.asyncio
async def test_completed_delivery_retains_outbox_when_history_commit_fails(
    monkeypatch: pytest.MonkeyPatch,
    chime_context: SimpleNamespace,
) -> None:
    chime_context.default_groups = lambda: [101]
    chime_context.send_action = AsyncMock(return_value=True)
    path = chime_context.data_dir / "chime_delivery.json"
    pending = chime.create_pending(
        path,
        event_id="chime:test-commit",
        payload=list(chime.segments("message")),
        targets=chime.default_group_targets(chime_context),
        commit={"history": {"FRB-A": "2026-07-14T00:00:00"}},
    )
    monkeypatch.setattr(chime, "save_history", Mock(return_value=False))

    assert await chime._deliver_pending(chime_context, pending) is False
    persisted = load_pending(path)
    assert persisted is not None
    assert persisted.complete is True
