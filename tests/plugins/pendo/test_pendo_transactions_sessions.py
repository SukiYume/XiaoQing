"""事务、会话、导出和时间解析。"""

from __future__ import annotations

import shutil
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from tests.helpers.pendo_review_test_support import (
    AIParser,
    Database,
    DbOpsMixin,
    ItemType,
    PendoSessionTestContext,
    ReminderService,
    SessionManager,
    TaskStatus,
    ZoneInfo,
    make_temp_db,
    pendo_session_services,
)
from tests.helpers.pendo_test_support import freeze_pendo_clock, reject_inserts


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["create", "update", "delete"])
async def test_qq_crud_and_operation_log_share_one_transaction(
    tmp_path: Path,
    operation: str,
) -> None:
    db       = Database(str(tmp_path / "pendo-qq-transaction.db"))
    ops      = DbOpsMixin()
    ops.db   = db
    owner_id = "qq-transaction-user"
    seed     = {
        "id"      : "qq-transaction-item",
        "owner_id": owner_id,
        "type"    : "note",
        "title"   : "before",
        "content" : "before",
    }
    if operation != "create":
        db.insert_item(dict(seed))

    reject_inserts(db, "operation_logs", "injected QQ audit failure")
    with pytest.raises(sqlite3.Error, match="injected QQ audit failure"):
        if operation == "create":
            await ops._db_create_with_log(dict(seed), owner_id, action="create_note")
        elif operation == "update":
            await ops._db_update_with_log(
                seed["id"],
                {"title": "after", "type": "note"},
                owner_id,
                action           = "edit_note",
                expected_version = 0,
            )
        else:
            await ops._db_soft_delete_with_log(seed["id"], owner_id, item_type="note")

    row = (
        db.get_connection()
        .execute(
            "SELECT title, deleted FROM items WHERE id = ?",
            (seed["id"],),
        )
        .fetchone()
    )
    if operation == "create":
        assert row is None
    else:
        assert tuple(row) == ("before", 0)
    assert db.get_connection().execute("SELECT COUNT(*) FROM operation_logs").fetchone()[0] == 0
    db.cleanup()


@pytest.mark.asyncio
async def test_chat_update_rejects_a_stale_item_version(tmp_path: Path) -> None:
    from plugins.pendo.core.exceptions import ItemVersionConflictException

    db       = Database(str(tmp_path / "pendo-chat-cas.db"))
    ops      = DbOpsMixin()
    ops.db   = db
    owner_id = "chat-cas-user"
    item_id  = "chat-cas-note"
    db.insert_item(
        {
            "id"      : item_id,
            "owner_id": owner_id,
            "type"    : "note",
            "title"   : "original",
            "content" : "original",
        }
    )
    stale = db.get_item(item_id, owner_id)
    assert stale is not None
    assert db.update_item(
        item_id,
        {"title": "concurrent"},
        owner_id,
        expected_version=stale.version,
    )

    with pytest.raises(ItemVersionConflictException):
        await ops._db_update_with_log(
            item_id,
            {"title": "stale", "type": "note"},
            owner_id,
            action           = "edit_note",
            expected_version = stale.version,
        )

    current = db.get_item(item_id, owner_id)
    assert current is not None
    assert current.title == "concurrent"
    assert db.get_connection().execute("SELECT COUNT(*) FROM operation_logs").fetchone()[0] == 0
    db.cleanup()


@pytest.mark.parametrize("operation", ["update", "delete"])
def test_event_collection_change_and_operation_log_share_one_transaction(
    tmp_path: Path,
    operation: str,
) -> None:
    """集合内容、子日程与审计日志必须在同一事务中提交。"""
    db            = Database(str(tmp_path / f"pendo-collection-{operation}.db"))
    owner_id      = "collection-transaction-user"
    collection_id = "collection-transaction"
    child_id      = "collection-transaction-child"
    db.create_event_collection(
        {
            "id"      : collection_id,
            "owner_id": owner_id,
            "kind"    : "multi_node",
            "title"   : "修改前",
        },
        [
            (
                child_id,
                {
                    "title": "子日程",
                },
            )
        ],
    )
    baseline_log_count = (
        db.get_connection().execute("SELECT COUNT(*) FROM operation_logs").fetchone()[0]
    )

    reject_inserts(db, "operation_logs", "注入集合审计失败")
    operation_log = {
        "user_id": owner_id,
        "action" : f"{operation}_event_collection",
    }
    with pytest.raises(sqlite3.Error, match="注入集合审计失败"):
        if operation == "update":
            db.update_event_collection(
                collection_id,
                {"title": "修改后"},
                owner_id,
                operation_log=operation_log,
            )
        else:
            db.delete_event_collection(
                collection_id,
                owner_id,
                cascade       = True,
                operation_log = operation_log,
            )

    assert db.get_event_collection(collection_id, owner_id)["title"] == "修改前"
    assert db.get_item(child_id, owner_id) is not None
    assert (
        db.get_connection().execute("SELECT COUNT(*) FROM operation_logs").fetchone()[0]
        == baseline_log_count
    )
    db.cleanup()


def test_export_reads_all_pages_and_keeps_offset_date_in_user_calendar(
    tmp_path: Path,
) -> None:
    from plugins.pendo.services.exporter import ExporterService

    event = SimpleNamespace(
        id                  = "offset-event",
        owner_id            = "u-export",
        type                = "event",
        title               = "时区标题\n不应换行",
        content             = "",
        category            = "测试",
        tags                = [],
        created_at          = "2030-01-01T00:00:00+14:00",
        updated_at          = "2030-01-01T00:00:00+14:00",
        start_time          = "2030-01-01T00:30:00+14:00",
        end_time            = None,
        location            = "",
        remind_times        = [],
        notes               = "",
        event_collection_id = None,
    )

    class _Repo:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any], int]] = []

        def get_all_items(self, owner_id, filters, *, page_size):
            self.calls.append((owner_id, filters, page_size))
            return [event] if filters == {"type": "event"} else []

        def get_items(self, *_args, **_kwargs):
            raise AssertionError("正式导出不应退回有条数上限的单页查询")

    class _Db:
        def __init__(self) -> None:
            self._repo = _Repo()
            self.calls = self._repo.calls

        def get_all_items(self, owner_id, filters, *, page_size):
            return self._repo.get_all_items(owner_id, filters, page_size=page_size)

        def get_items(self, *args, **kwargs):
            return self._repo.get_items(*args, **kwargs)

        def get_user_settings(self, _owner):
            return {"timezone": "Pacific/Kiritimati"}

        def log_transfer(self, **_kwargs):
            return 1

    database = _Db()
    result   = ExporterService(database, tmp_path).export_markdown(
        "u-export",
        "时区档案 2030-01-01 event",
        {},
    )

    assert result["status"] == "success"
    assert result["record_count"] == 1
    assert database.calls == [("u-export", {"type": "event"}, 500)]
    markdown = (tmp_path / "u-export" / "时区档案.md").read_text(encoding="utf-8")
    assert "### 01. 时区标题 不应换行" in markdown


def test_event_reminder_shift_preserves_absolute_offset_across_iso_zones() -> None:
    from plugins.pendo.handlers.event_support import recalculate_event_reminders

    event = SimpleNamespace(
        start_time     = "2030-01-01T09:00:00+08:00",
        remind_times   = ["2030-01-01T00:00:00+00:00"],
        reminder_rules = [],
    )

    shifted = recalculate_event_reminders(
        event,
        {"start_time": "2030-01-02T09:00:00+08:00"},
    )

    assert shifted == ["2030-01-02T08:00:00+08:00", "2030-01-02T09:00:00+08:00"]


def test_event_reminder_shift_preserves_explicit_empty_reminders() -> None:
    """用户清空全部提醒后，修改日程时间不得重新生成默认提醒。"""
    from plugins.pendo.handlers.event_support import recalculate_event_reminders

    event = SimpleNamespace(
        start_time     = "2030-01-01T09:00:00+08:00",
        remind_times   = [],
        reminder_rules = [],
    )

    shifted = recalculate_event_reminders(
        event,
        {"start_time": "2030-01-02T09:00:00+08:00"},
    )

    assert shifted == []


@pytest.mark.parametrize(
    "extra", [{}, {"remind_times": []}, {"remind_offsets": []}, {"reminder_rules": []}]
)
def test_event_default_reminder_is_only_start_time(extra) -> None:
    from plugins.pendo.handlers.event_support import (
        ensure_event_reminder_rules,
        ensure_event_reminders,
    )

    data      = {"start_time": "2026-09-22T14:00:00+08:00", **extra}
    reminders = ensure_event_reminders(data)
    assert reminders == ["2026-09-22T14:00:00+08:00"]
    assert ensure_event_reminder_rules(data, reminders) == [{"offset_seconds": 0}]


def test_event_reminders_parse_json_list_instead_of_string_characters() -> None:
    from plugins.pendo.handlers.event_support import ensure_event_reminders

    reminders = ensure_event_reminders(
        {
            "start_time"  : "2030-01-01T10:00:00+08:00",
            "remind_times": '["2030-01-01T09:00:00+08:00"]',
        }
    )

    assert reminders == ["2030-01-01T09:00:00+08:00", "2030-01-01T10:00:00+08:00"]


@pytest.mark.asyncio
async def test_pendo_handle_session_uses_transaction_argument_and_atomically_replaces_event_info():
    from plugins.pendo import main as pendo_main
    from plugins.pendo.config import PendoConfig

    manager  = SessionManager()
    original = await manager.create(
        1001,
        None,
        "pendo",
        {
            "type": PendoConfig.SESSION_TYPE_EVENT_INFO,
            "data": {"title": "组会"},
        },
    )
    context = PendoSessionTestContext(manager, pendo_session_services())

    result = await manager.update(
        1001,
        None,
        lambda session: pendo_main.handle_session(
            "明天十点",
            {"user_id": 1001},
            context,
            session,
        ),
    )

    current = await manager.peek(1001, None)
    assert result == [{"type": "text", "data": {"text": "需要确认冲突"}}]
    assert context.get_session_calls == 0
    assert current is not None
    assert current.session_id != original.session_id
    assert current.get("type") == PendoConfig.SESSION_TYPE_EVENT_CONFLICT
    assert current.get("data") == {
        "title"     : "组会",
        "start_time": "2030-01-01T10:00:00",
        "type"      : "event",
    }


@pytest.mark.asyncio
async def test_event_info_keeps_middle_turn_location_until_time_is_provided(db):
    from plugins.pendo import main as pendo_main
    from plugins.pendo.config import PendoConfig
    from plugins.pendo.handlers.event import EventHandler

    manager = SessionManager()
    await manager.create(
        1001,
        None,
        "pendo",
        {
            "type": PendoConfig.SESSION_TYPE_EVENT_INFO,
            "data": {"title": "三轮会议"},
        },
    )
    parser = AIParser(db=db)
    parser.parse_event_with_ai = AsyncMock(
        side_effect=[
            {"location": "A 会议室"},
            {"start_time": "2030-01-02T09:00:00"},
        ]
    )
    services                  = pendo_session_services()
    services["ai_parser"]     = parser
    services["event_handler"] = EventHandler(db, parser, ReminderService(db))
    context                   = PendoSessionTestContext(manager, services)
    for text in ("地点是 A 会议室", "时间是 2030-01-02 09:00"):
        await manager.update(
            1001,
            None,
            lambda session, text=text: pendo_main.handle_session(
                text,
                {"user_id": 1001},
                context,
                session,
            ),
        )
    events = db.get_all_items("1001", {"type": "event"})
    assert len(events) == 1
    assert events[0].location == "A 会议室"
    assert await manager.peek(1001, None) is None


@pytest.mark.asyncio
async def test_pendo_event_info_replacement_failure_rolls_back_staged_generation():
    from plugins.pendo import main as pendo_main
    from plugins.pendo.config import PendoConfig

    manager  = SessionManager()
    original = await manager.create(
        1001,
        None,
        "pendo",
        {
            "type": PendoConfig.SESSION_TYPE_EVENT_INFO,
            "data": {"title": "组会"},
        },
    )

    class _FailAfterStagingContext(PendoSessionTestContext):
        async def create_session(self, initial_data=None, timeout=300.0):
            await super().create_session(initial_data=initial_data, timeout=timeout)
            raise RuntimeError("failure after staging replacement")

    context = _FailAfterStagingContext(manager, pendo_session_services())

    with pytest.raises(RuntimeError, match="after staging replacement"):
        await manager.update(
            1001,
            None,
            lambda session: pendo_main.handle_session(
                "明天十点",
                {"user_id": 1001},
                context,
                session,
            ),
        )

    current = await manager.peek(1001, None)
    assert current is not None
    assert current.session_id == original.session_id
    assert current.get("type") == PendoConfig.SESSION_TYPE_EVENT_INFO
    assert current.get("data") == {"title": "组会"}


class _MutatingTaskHandler:
    """先改动事务内的会话副本，再按给定异常失败的待办会话处理器。"""

    def __init__(self, error: BaseException) -> None:
        self.error = error

    async def handle_session_step(self, _user_id, _text, session, _context):
        session.set("data", {"items": ["must-rollback"]})
        raise self.error


async def _continue_task_session_with(error: BaseException):
    """让一条待办补充会话的处理器抛出 ``error``，返回续答调用结果与原会话。"""

    from plugins.pendo import main as pendo_main
    from plugins.pendo.config import PendoConfig

    manager  = SessionManager()
    original = await manager.create(
        1001,
        None,
        "pendo",
        {"type": PendoConfig.SESSION_TYPE_TASK_ADD, "data": {"items": ["kept"]}},
    )
    services = {"task_handler": _MutatingTaskHandler(error)}
    context  = PendoSessionTestContext(manager, services)

    async def continue_session():
        return await manager.update(
            1001,
            None,
            lambda session: pendo_main.handle_session(
                "continue",
                {"user_id": 1001},
                context,
                session,
            ),
        )

    return manager, original, continue_session


@pytest.mark.asyncio
async def test_pendo_session_service_exception_is_not_swallowed_or_committed():
    from plugins.pendo.config import PendoConfig
    from plugins.pendo.core.exceptions import PendoException

    manager, original, continue_session = await _continue_task_session_with(
        PendoException("database unavailable")
    )

    with pytest.raises(PendoException, match="database unavailable"):
        await continue_session()

    current = await manager.peek(1001, None)
    assert current is not None
    assert current.session_id == original.session_id
    assert current.get("type") == PendoConfig.SESSION_TYPE_TASK_ADD
    assert current.get("data") == {"items": ["kept"]}


@pytest.mark.asyncio
async def test_pendo_session_plain_value_error_is_not_misclassified_as_input():
    from plugins.pendo.config import PendoConfig

    manager, original, continue_session = await _continue_task_session_with(
        ValueError("internal parser invariant failed")
    )

    with pytest.raises(ValueError, match="internal parser invariant"):
        await continue_session()

    current = await manager.peek(1001, None)
    assert current is not None
    assert current.session_id == original.session_id
    assert current.get("type") == PendoConfig.SESSION_TYPE_TASK_ADD
    assert current.get("data") == {"items": ["kept"]}


@pytest.mark.asyncio
async def test_pendo_session_converts_only_whitelisted_input_exception():
    from plugins.pendo.core.exceptions import MissingRequiredFieldException

    _manager, _original, continue_session = await _continue_task_session_with(
        MissingRequiredFieldException("start_time", "开始时间")
    )

    result = await continue_session()

    assert result == [{"type": "text", "data": {"text": "❓ 请提供开始时间"}}]


def test_bare_week_month_year_are_current_calendar_ranges():
    from plugins.pendo.utils import time_utils

    now = datetime(2026, 5, 3, 16, 30, 0)

    week_start, week_end = time_utils._parse_time_range_core("week", now=now, strict=True)
    month_start, month_end = time_utils._parse_time_range_core("month", now=now, strict=True)
    year_start, year_end = time_utils._parse_time_range_core("year", now=now, strict=True)

    assert week_start.isoformat() == "2026-04-27T00:00:00"
    assert week_end.isoformat() == "2026-05-03T23:59:59"
    assert month_start.isoformat() == "2026-05-01T00:00:00"
    assert month_end.isoformat() == "2026-05-31T23:59:59"
    assert year_start.isoformat() == "2026-01-01T00:00:00"
    assert year_end.isoformat() == "2026-12-31T23:59:59"


def test_time_range_tokens_must_match_the_complete_input():
    from plugins.pendo.utils import time_utils

    now = datetime(2026, 5, 3, 16, 30, 0)
    start, end = time_utils._parse_time_range_core("last7d", now=now, strict=True)

    assert start == datetime(2026, 4, 26, 16, 30, 0)
    assert end == now
    with pytest.raises(ValueError, match="无法解析时间范围"):
        time_utils._parse_time_range_core("prefix-last7d-suffix", now=now, strict=True)
    assert time_utils.parse_diary_range("prefix-today-suffix", now=now) == (
        "2026-04-03",
        "2026-05-03",
    )


def test_rate_limiter_removes_expired_user_buckets(monkeypatch: pytest.MonkeyPatch) -> None:
    from plugins.pendo.services import ai_parser as ai_parser_module
    from plugins.pendo.services.ai_parser import RateLimiter

    limiter = RateLimiter(max_calls=2, time_window=60)
    limiter.call_history = {"expired": [1.0], "active": [80.0]}
    monkeypatch.setattr(ai_parser_module.time, "time", lambda: 100.0)

    assert limiter.check_rate_limit("new-user") == (True, 0)
    assert limiter.call_history == {"active": [80.0], "new-user": [100.0]}


def test_rule_parser_prefers_longest_week_keyword_and_flags_invalid_clock():
    from plugins.pendo.services.rule_parser import RuleParser

    parser = RuleParser()
    now = datetime(2026, 5, 1, 8, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    parsed = parser.parse("下下周10点开会", "u-rule", now=now)
    assert parsed["start_time"] == "2026-05-15T10:00:00+08:00"

    # 非法钟点不能被换成默认 09:00，而是留空并要求补充时间。
    invalid_clock = parser.parse("明天25点开会", "u-rule", now=now)
    assert "start_time" not in invalid_clock
    assert invalid_clock["invalid_time"] is True
    assert "start_time" in invalid_clock["needs_confirmation"]

    # 2026-05-01 是周五：下周一按自然周落在 05-04。
    weekday = parser.parse("下周一9点开会", "u-rule", now=now)
    assert weekday["start_time"] == "2026-05-04T09:00:00+08:00"


def test_rule_parser_places_this_week_weekday_in_the_natural_week():
    from plugins.pendo.services.rule_parser import RuleParser

    parser = RuleParser()
    friday = datetime(2026, 5, 1, 8, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    saturday = datetime(2026, 5, 2, 8, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    sunday = datetime(2026, 5, 3, 8, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    # 本周X、这周X、这个星期X 写法不同，都落在周一起算的自然周里的那一天。
    for phrase in ("本周五", "这周五", "这个星期五", "本周周五", "本礼拜五", "这个周五"):
        parsed = parser.parse(f"{phrase}下午3点开会", "u-rule", now=friday)
        assert parsed["start_time"] == "2026-05-01T15:00:00+08:00", phrase
        assert parsed["needs_confirmation"] == [], phrase

    # 那一天已经过去也还是本周的那一天（与“这周五”一贯的处理一致），不顺延到下周。
    assert (
        parser.parse("本周五下午3点开会", "u-rule", now=saturday)["start_time"]
        == parser.parse("这周五下午3点开会", "u-rule", now=saturday)["start_time"]
        == "2026-05-01T15:00:00+08:00"
    )
    assert parser.parse("本周日10点开会", "u-rule", now=sunday)["start_time"] == (
        "2026-05-03T10:00:00+08:00"
    )
    assert parser.parse("本周一开会", "u-rule", now=sunday)["start_time"] == (
        "2026-04-27T09:00:00+08:00"
    )
    # 下周的写法不受影响。
    assert parser.parse("下周五下午3点开会", "u-rule", now=saturday)["start_time"] == (
        "2026-05-08T15:00:00+08:00"
    )

    # 没有星期几的“本周”不是具体时间；有星期几才算时间表达（决定类型）。
    note = parser.parse("讨论本周排期", "u-rule", now=friday)
    assert "start_time" not in note
    assert parser.parse("本周三前把文档发我", "u-rule", now=friday)["type"].value == "task"


def test_search_items_applies_date_range_filters():
    temp_dir, db = make_temp_db("pendo_review_search_range")
    owner_id = "u-search-range"

    try:
        db.insert_item(
            {
                "id"        : "note_april",
                "owner_id"  : owner_id,
                "type"      : "note",
                "title"     : "会议纪要",
                "content"   : "四月版本",
                "created_at": "2026-04-10T09:00:00",
                "updated_at": "2026-04-10T09:00:00",
            }
        )
        db.insert_item(
            {
                "id"        : "note_january",
                "owner_id"  : owner_id,
                "type"      : "note",
                "title"     : "会议纪要",
                "content"   : "一月版本",
                "created_at": "2026-01-10T09:00:00",
                "updated_at": "2026-01-10T09:00:00",
            }
        )

        results, total = db.search_items_page(
            owner_id,
            "会议",
            filters={
                "type"      : "note",
                "date_field": "created_at",
                "start_date": "2026-04-01T00:00:00",
                "end_date"  : "2026-04-30T23:59:59",
            },
            limit=10,
        )

        assert total == 1
        assert [item.id for item in results] == ["note_april"]
    finally:
        db.cleanup()
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_ai_parser_build_remind_times_uses_user_timezone(db, monkeypatch):
    from plugins.pendo.utils import time_utils

    # 服务器时钟此刻是 UTC 2030-01-01 16:30，即用户所在的洛杉矶 08:30。
    freeze_pendo_clock(monkeypatch, datetime(2030, 1, 1, 16, 30, tzinfo=UTC), time_utils)
    db.update_user_settings("u-la", {"timezone": "America/Los_Angeles"})

    parser = AIParser(db=db)

    remind_times = parser.build_remind_times_from_offsets(
        "2030-01-01T10:00:00",
        ["提前1小时"],
        user_id="u-la",
    )

    assert remind_times == ["2030-01-01T09:00:00-08:00"]


def test_ai_parser_builds_semantic_reminder_rules_from_description():
    parser = AIParser()

    rules = parser.build_reminder_rules_from_description("提前1天和2小时提醒")

    assert rules == [
        {"offset_seconds": 86400},
        {"offset_seconds": 7200},
        {"offset_seconds": 0},
    ]
    assert parser.build_reminder_rules_from_description("提前一百分钟") == [
        {"offset_seconds": 6000},
        {"offset_seconds": 0},
    ]


@pytest.mark.parametrize(
    "offset,seconds",
    [
        ("提前一小时", 3600),
        ("提前半小时", 1800),
        ("提前一百二十分钟", 7200),
        ("提前一百二分钟", 7200),
        ("1.5h", 5400),
        ("1H", 3600),
        ("提前1小时30分钟", 5400),
        ("PT1H", None),
        ("-1h", None),
        ("1小时后", None),
    ],
)
def test_ai_reminder_offset_requires_complete_duration(offset, seconds):
    delta = AIParser()._parse_offset(offset)
    assert (delta.total_seconds() if delta else None) == seconds


@pytest.mark.parametrize("offsets", [[], ["PT1H"], [60], [{"minutes": 60}], ["提前1天"]])
def test_ai_reminder_original_instruction_recovers_model_output(offsets):
    parser = AIParser()
    result = parser.build_event_result(
        {"start_time": "2030-09-22T16:00:00", "remind_offsets": offsets},
        "后面一个月每周二下午4点，跟华为讨论，提前一小时提醒",
        "reminder-recovery",
        partial=False,
    )
    assert result["reminder_rules"] == [{"offset_seconds": 3600}, {"offset_seconds": 0}]


@pytest.mark.parametrize(
    "text,expected",
    [
        ("明天开会，提前1天和2小时提醒", ["1天", "2小时"]),
        ("明天开会，提前1小时30分钟提醒", ["1小时30分钟"]),
        ("后面一个月每周二下午4点，开会", []),
        ("不要提前1小时提醒", []),
        ("提前1天提交报告", []),
    ],
)
def test_ai_reminder_source_extraction_is_limited_to_reminder_clauses(text, expected):
    assert AIParser._explicit_source_offsets(text) == expected


def test_ai_invalid_offsets_do_not_emit_empty_rules():
    result = AIParser().build_event_result(
        {"start_time": "2030-09-22T16:00:00", "remind_offsets": ["PT1H"]},
        "开会",
        "invalid-reminder",
        partial=False,
    )
    assert "reminder_rules" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("partial", [False, True])
async def test_ai_event_prompt_separates_modes_and_inherits_route_settings(partial):
    captured: dict[str, Any] = {}

    class AI:
        async def complete(self, route, messages, **kwargs):
            captured.update(messages=messages, kwargs=kwargs)
            return SimpleNamespace(content='{"location": "会议室"}')

    parser = AIParser(context=SimpleNamespace(capabilities=SimpleNamespace(ai=AI())))
    result = await parser.parse_event_with_ai(
        "地点改为会议室", f"prompt-mode-{partial}", partial=partial
    )
    prompt = captured["messages"][1]["content"]
    assert "局部模式：省略所有未提及的字段" in prompt if partial else "解析模式: 创建模式" in prompt
    assert "temperature" not in captured["kwargs"]
    assert result["location"] == "会议室"
    assert "start_time" not in result
    assert "reminder_rules" not in result


@pytest.mark.asyncio
async def test_ai_parser_rule_fallback_preserves_absolute_reminder_times():
    # 没有接入模型能力，解析退回规则；规则按开始时间算出的绝对提醒时间必须原样保留。
    parser = AIParser(now_factory=lambda tz: datetime(2030, 1, 1, 8, 0, tzinfo=tz))

    result = await parser.parse_event_with_ai(
        "2030-01-02 10:00 开会，提前1小时提醒", "u-rule-reminder"
    )

    assert result["remind_times"] == ["2030-01-02T09:00:00+08:00"]


@pytest.mark.asyncio
async def test_ai_parser_uses_core_route_without_overriding_parameters():
    captured: dict[str, object] = {}

    class AI:
        async def complete(self, route, messages, **kwargs):
            captured.update(route=route, messages=messages, **kwargs)
            return SimpleNamespace(content="ok")

    context = SimpleNamespace(capabilities=SimpleNamespace(ai=AI()))

    result = await AIParser(context=context)._call_llm(
        [{"role": "user", "content": "hello"}],
    )

    assert result == "ok"
    assert captured == {
        "route"   : "parse",
        "messages": [{"role": "user", "content": "hello"}],
    }


class _LiveAiSessionContext(PendoSessionTestContext):
    """服务由 Pendo 在第一次续答时按真实数据库和当前 AI 能力建立的会话上下文。"""

    def __init__(self, manager: SessionManager, data_dir: Path, ai: object) -> None:
        super().__init__(manager, {})
        self.state    = {}
        self.data_dir = data_dir
        self.capabilities = SimpleNamespace(ai=ai)
        self.request_id = None


@pytest.mark.asyncio
async def test_cached_pendo_ai_parser_uses_live_core_capability(db, tmp_path):
    from plugins.pendo import main as pendo_main
    from plugins.pendo.config import PendoConfig
    from plugins.pendo.utils import db_ops

    routes: list[str] = []

    class AI:
        def __init__(self, outcome: object) -> None:
            self.outcome = outcome

        async def complete(self, route, _messages, **_kwargs):
            routes.append(route)
            if isinstance(self.outcome, BaseException):
                raise self.outcome
            return SimpleNamespace(content=self.outcome)

    manager = SessionManager()
    context = _LiveAiSessionContext(manager, tmp_path, None)
    db_ops.set_database_singleton(db)
    try:
        for index, ai in enumerate(
            (
                AI('{"location": "旧路由会议室"}'),
                AI('{"location": "新路由会议室"}'),
                AI(RuntimeError("route removed")),
            ),
            start=1,
        ):
            context.capabilities.ai = ai
            await manager.create(
                1001,
                None,
                "pendo",
                {
                    "type": PendoConfig.SESSION_TYPE_EVENT_INFO,
                    "data": {"title": f"第{index}次", "start_time": f"2030-01-0{index}T10:00:00"},
                },
            )
            await manager.update(
                1001,
                None,
                lambda session: pendo_main.handle_session(
                    "地点见通知",
                    {"user_id": 1001},
                    context,
                    session,
                ),
            )
            if index == 1:
                parser = context.state["pendo_runtime"]["services"]["ai_parser"]
            assert context.state["pendo_runtime"]["services"]["ai_parser"] is parser

        events = sorted(db.get_all_items("1001", {"type": "event"}), key=lambda e: e.start_time)
        assert [event.location for event in events] == ["旧路由会议室", "新路由会议室", ""]
        assert routes == ["parse", "parse", "parse"]
    finally:
        db_ops.set_database_singleton(None)


@pytest.mark.asyncio
async def test_pendo_ai_parser_never_reads_plugin_llm_secrets():
    class AI:
        async def complete(self, route, _messages, **_kwargs):
            assert route == "parse"
            return SimpleNamespace(content="ok")

    context = SimpleNamespace(
        capabilities=SimpleNamespace(ai=AI()),
        get_settings_snapshot=lambda: (_ for _ in ()).throw(
            AssertionError("AI credentials must remain inside core")
        ),
    )
    parser = AIParser(context=context)

    assert await parser._call_llm([{"role": "user", "content": "hello"}]) == "ok"


def test_reminder_dispatch_uses_owner_timezone(db, monkeypatch):
    from plugins.pendo.services import reminder as reminder_module
    from plugins.pendo.utils import time_utils

    # 服务器时钟此刻是 UTC 2030-01-01 17:00，即提醒所有者所在的洛杉矶 09:00。
    freeze_pendo_clock(
        monkeypatch, datetime(2030, 1, 1, 17, 0, tzinfo=UTC), reminder_module, time_utils
    )
    db.update_user_settings(
        "u-la",
        {
            "timezone"         : "America/Los_Angeles",
            "quiet_hours_start": "23:00",
            "quiet_hours_end"  : "07:00",
            "settings_json"    : {"reminder_enabled": True},
        },
    )
    item_id = db.insert_item(
        {
            "owner_id"    : "u-la",
            "type"        : "event",
            "title"       : "Morning sync",
            "start_time"  : "2030-01-01T10:00:00",
            "end_time"    : "2030-01-01T11:00:00",
            "remind_times": ["2030-01-01T09:00:00"],
            "location"    : "Room 1",
        }
    )

    result = ReminderService(db=db).check_and_send_reminders()

    assert result["sent"] == 1
    assert result["messages"][0]["user_id"] == "u-la"
    assert result["messages"][0]["item_id"] == item_id


def test_reminder_conflict_compares_absolute_time_across_offsets():
    existing = SimpleNamespace(
        id         = "evt-shanghai",
        title      = "跨时区会议",
        start_time = "2030-01-02T10:00:00+08:00",
        end_time   = "2030-01-02T11:00:00+08:00",
    )

    class _FakeDb:
        settings = SimpleNamespace(get_user_settings=lambda _user_id: {"timezone": "Asia/Shanghai"})

        def get_events_for_range(self, user_id, start_date, end_date):
            assert user_id == "u-shanghai"
            assert start_date <= end_date
            return [existing]

    service = ReminderService(db=_FakeDb())

    conflicts = service.detect_conflicts(
        "u-shanghai", [("2030-01-02T02:30:00+00:00", "2030-01-02T03:00:00+00:00")]
    )
    adjacent = service.detect_conflicts(
        "u-shanghai", [("2030-01-02T03:00:00+00:00", "2030-01-02T03:30:00+00:00")]
    )

    assert conflicts == [
        {
            "id"        : "evt-shanghai",
            "title"     : "跨时区会议",
            "start_time": "2030-01-02T10:00:00+08:00",
            "end_time"  : "2030-01-02T11:00:00+08:00",
        }
    ]
    assert adjacent == []


def test_reminder_accepts_open_task_status_enum():
    task = SimpleNamespace(type=ItemType.TASK, status=TaskStatus.OPEN)

    assert ReminderService._is_active_reminder_item(task)


@pytest.mark.parametrize("repeat", [False, True])
@pytest.mark.parametrize(
    ("status", "enabled"), [("open", True), ("done", True), ("cancelled", True), ("open", False)]
)
def test_reminder_filters_task_state_and_settings(db, monkeypatch, repeat, status, enabled):
    from plugins.pendo.services import reminder as reminder_module
    from plugins.pendo.utils import time_utils

    fire  = datetime.fromisoformat("2030-01-01T09:00:00+00:00")
    owner = "1001"
    # 首次提醒在提醒点当刻领取；重复提醒在首次发送十分钟后领取。
    now = fire + timedelta(minutes=10) if repeat else fire
    freeze_pendo_clock(monkeypatch, now, reminder_module, time_utils)
    db.update_user_settings(
        owner, {"timezone": "UTC", "settings_json": {"reminder_enabled": enabled}}
    )
    item_id = db.insert_item(
        {
            "owner_id"    : owner,
            "type"        : "task",
            "title"       : "提醒验证",
            "status"      : status,
            "remind_times": [fire.isoformat()],
        }
    )
    if repeat:
        db.log_reminder(item_id, fire.isoformat())
        with db.get_connection() as conn:
            conn.execute(
                "UPDATE reminder_logs SET last_sent_at = ? WHERE item_id = ?",
                (fire.isoformat(), item_id),
            )
    messages = ReminderService(db).check_and_send_reminders()["messages"]
    expected = int(status == "open" and enabled)
    claimed  = (
        db.get_connection()
        .execute(
            "SELECT COUNT(*) FROM reminder_logs WHERE item_id = ? AND state = 'claimed'",
            (item_id,),
        )
        .fetchone()[0]
    )
    assert len(messages) == expected
    assert claimed == expected
    if expected:
        assert messages[0]["item_id"] == item_id
        assert messages[0]["claim_token"]
