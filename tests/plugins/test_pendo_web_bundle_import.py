"""Pendo Web 传输包入库规范化的边界回归。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from plugins.pendo.web.services import bundle_import
from plugins.pendo.web.services.bundle_import import (
    _localize_source_datetime,
    normalize_import_event_collection,
    normalize_import_payload,
)
from plugins.pendo.web.services.transfer_bundle import ParsedBundle

FIXED_NOW = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)


def test_normalize_import_payload_replaces_explicit_empty_timestamps_and_copies_metadata():
    """显式空时间应统一落到同一导入时钟，容器字段不得保留外部别名。"""

    context = {"source": "external"}
    attachments = [{"name": "proof.txt"}]
    ai_meta = {"model": "external"}
    payload = {
        "type": "task",
        "title": "done",
        "status": "done",
        "created_at": None,
        "updated_at": "",
        "completed_at": None,
        "deleted": "false",
        "version": 99,
        "context": context,
        "attachments": attachments,
        "ai_meta": ai_meta,
        "_bundle_line": 12,
    }

    normalized = normalize_import_payload(payload, now=FIXED_NOW)

    assert normalized["created_at"] == "2030-01-01T12:00:00+00:00"
    assert normalized["updated_at"] == normalized["created_at"]
    assert normalized["completed_at"] == normalized["created_at"]
    assert normalized["deleted"] is False
    assert "version" not in normalized
    assert "_bundle_line" not in normalized
    assert normalized["context"] == context and normalized["context"] is not context
    assert normalized["attachments"] == attachments
    assert normalized["attachments"] is not attachments
    assert normalized["ai_meta"] == ai_meta and normalized["ai_meta"] is not ai_meta


def test_normalize_import_payload_sets_cancelled_timestamp_from_same_clock():
    """取消任务的终态时间应使用本次导入共享的 UTC 时钟。"""

    normalized = normalize_import_payload(
        {"type": "task", "title": "cancelled", "status": "cancelled", "cancelled_at": ""},
        now=FIXED_NOW,
    )

    assert normalized["cancelled_at"] == "2030-01-01T12:00:00+00:00"
    assert normalized["completed_at"] is None


def test_normalize_import_event_uses_source_zone_for_times_and_default_timezone():
    """无时区事件时间应按清单来源时区解释，并补齐事件时区。"""

    normalized = normalize_import_payload(
        {
            "type": "event",
            "title": "New York morning",
            "start_time": "2026-01-15T09:00:00",
            "remind_times": ["2026-01-15T08:30:00", None, ""],
        },
        source_zone=ZoneInfo("America/New_York"),
        now=FIXED_NOW,
    )

    assert normalized["start_time"] == "2026-01-15T14:00:00+00:00"
    assert normalized["remind_times"] == [
        "2026-01-15T13:30:00+00:00",
        "2026-01-15T14:00:00+00:00",
    ]
    assert normalized["timezone"] == "America/New_York"


def test_normalize_import_payload_reports_missing_type_as_validation_error():
    """缺失记录类型时应返回稳定的输入校验错误。"""

    with pytest.raises(ValueError, match="Unsupported record type"):
        normalize_import_payload({"title": "missing type"}, now=FIXED_NOW)


@pytest.mark.parametrize(
    ("normalizer", "payload"),
    [
        (normalize_import_payload, {"type": "task", "title": "task"}),
        (
            normalize_import_event_collection,
            {"kind": "multi_node", "title": "collection"},
        ),
    ],
)
def test_import_normalizers_reject_naive_clock(normalizer: Any, payload: dict[str, Any]):
    """所有导入入口都应拒绝没有时区信息的调用方时钟。"""

    with pytest.raises(ValueError, match="timezone-aware"):
        normalizer(payload, now=datetime(2030, 1, 1, 12, 0))


def test_localize_source_datetime_handles_passthrough_aware_invalid_and_overflow_values():
    """来源时间转换应覆盖空值、带时区值、坏格式与上下界溢出。"""

    source_zone = ZoneInfo("Asia/Shanghai")
    aware = datetime(2026, 1, 1, 1, 0, tzinfo=timezone(timedelta(hours=2)))
    aware_overflow = datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=9)))

    assert _localize_source_datetime(None, "start_time", source_zone) is None
    assert _localize_source_datetime("", "start_time", source_zone) == ""
    assert _localize_source_datetime(aware, "start_time", source_zone) == (
        "2025-12-31T23:00:00+00:00"
    )
    with pytest.raises(ValueError, match="expected ISO datetime"):
        _localize_source_datetime("not-a-time", "start_time", source_zone)
    with pytest.raises(ValueError, match="outside supported datetime range"):
        _localize_source_datetime(
            "0001-01-01T00:00:00",
            "start_time",
            ZoneInfo("Asia/Tokyo"),
        )
    with pytest.raises(ValueError, match="outside supported datetime range"):
        _localize_source_datetime(aware_overflow, "start_time", source_zone)


def test_normalize_import_payload_rejects_out_of_range_clock():
    """无法安全换算为 UTC 的导入时钟应作为校验错误拒绝。"""

    with pytest.raises(ValueError, match="import clock is outside the supported range"):
        normalize_import_payload(
            {"type": "task", "title": "task"},
            now=datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=9))),
        )


def test_normalize_import_payload_rejects_non_list_reminders():
    """提醒时间字段必须保持列表结构，禁止把字符串逐字符处理。"""

    with pytest.raises(ValueError, match="remind_times must be a list"):
        normalize_import_payload(
            {"type": "task", "title": "bad reminders", "remind_times": "tomorrow"},
            now=FIXED_NOW,
        )


def test_normalize_import_event_collection_normalizes_storage_fields():
    """日程集合写库前应完成字段清洗、时间换算和容器复制。"""

    context = {"origin": "external"}
    normalized = normalize_import_event_collection(
        {
            "id": "source-collection",
            "kind": " recurring ",
            "title": "  Weekly sync  ",
            "category": "工作",
            "location": " Room 1 ",
            "tags": ["weekly", "weekly", None, ""],
            "context": context,
            "visibility": "group_scope",
            "reminder_rules": [
                {"offset_seconds": "60"},
                {"offset_seconds": 60},
            ],
            "rrule": " FREQ=WEEKLY ",
            "source_item_id": " source-event ",
            "start_time": "2026-01-15T09:00:00",
            "end_time": "2026-01-15T10:00:00",
            "created_at": None,
            "updated_at": "",
            "deleted": "false",
            "_bundle_line": 8,
        },
        source_zone=ZoneInfo("Asia/Tokyo"),
        now=FIXED_NOW,
    )

    assert normalized["kind"] == "recurring"
    assert normalized["title"] == "Weekly sync"
    assert normalized["location"] == "Room 1"
    assert normalized["tags"] == ["weekly"]
    assert normalized["context"] == context and normalized["context"] is not context
    assert normalized["timezone"] == "Asia/Tokyo"
    assert normalized["reminder_rules"] == [{"offset_seconds": 60}]
    assert normalized["rrule"] == "FREQ=WEEKLY"
    assert normalized["source_item_id"] == "source-event"
    assert normalized["start_time"] == "2026-01-15T00:00:00+00:00"
    assert normalized["end_time"] == "2026-01-15T01:00:00+00:00"
    assert normalized["created_at"] == "2030-01-01T12:00:00+00:00"
    assert normalized["updated_at"] == normalized["created_at"]
    assert normalized["deleted"] is False
    assert "_bundle_line" not in normalized


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"kind": "single"}, "Invalid event collection kind"),
        ({"title": ""}, "title is required"),
        ({"tags": "weekly"}, "tags must be a list"),
        ({"visibility": "public"}, "Invalid event collection visibility"),
        ({"timezone": "Mars/Olympus"}, "Invalid event collection timezone"),
        ({"reminder_rules": "soon"}, "reminder_rules must be a list"),
        (
            {"start_time": "2026-01-15T10:00:00", "end_time": "2026-01-15T09:00:00"},
            "end_time must be after start_time",
        ),
    ],
)
def test_normalize_import_event_collection_rejects_invalid_fields(
    updates: dict[str, Any],
    message: str,
):
    """日程集合的枚举、时区、列表与时间顺序边界应统一拒绝。"""

    payload: dict[str, Any] = {"kind": "multi_node", "title": "collection"}
    payload.update(updates)

    with pytest.raises(ValueError, match=message):
        normalize_import_event_collection(payload, now=FIXED_NOW)


def test_inspect_bundle_bytes_merges_row_errors_and_keeps_only_valid_records(monkeypatch):
    """检查入口应保留原解析错误，并只输出规范化成功的记录。"""

    parsed = ParsedBundle(
        manifest={"source": {"timezone": "UTC"}},
        records_by_type={
            "task": [
                {"type": "task", "title": "valid", "_bundle_line": 3},
                {"type": "task", "title": "", "_bundle_line": 7},
            ]
        },
        event_collections=[
            {"kind": "multi_node", "title": "valid collection", "_bundle_line": 4},
            {"kind": "single", "title": "invalid collection", "_bundle_line": 9},
        ],
        errors=[
            {
                "path": "data/tasks.ndjson",
                "line": 1,
                "type": "task",
                "message": "bad json",
            }
        ],
    )
    monkeypatch.setattr(bundle_import, "read_bundle", lambda _file: parsed)

    inspected, records, errors = bundle_import.inspect_bundle_bytes(b"bundle")

    assert inspected is parsed
    assert [record["title"] for record in records] == ["valid"]
    assert [collection["title"] for collection in inspected.event_collections] == [
        "valid collection"
    ]
    assert [(error["line"], error["type"]) for error in errors] == [
        (1, "task"),
        (9, "event_collection"),
        (7, "task"),
    ]
