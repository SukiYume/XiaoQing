"""事务、会话、导出和时间解析。"""

from __future__ import annotations

from tests.helpers.pendo_review_test_support import (
    AIParser,
    Any,
    AsyncMock,
    Database,
    DbOpsMixin,
    ItemType,
    Path,
    ReminderService,
    SessionManager,
    SimpleNamespace,
    TaskStatus,
    ZoneInfo,
    _make_temp_db,
    _pendo_session_services,
    _PendoSessionTestContext,
    datetime,
    logging,
    pytest,
    shutil,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["create", "update", "delete"])
async def test_qq_crud_and_operation_log_share_one_transaction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operation: str,
) -> None:
    db = Database(str(tmp_path / "pendo-qq-transaction.db"))
    ops = DbOpsMixin()
    ops.db = db
    owner_id = "qq-transaction-user"
    seed = {
        "id": "qq-transaction-item",
        "owner_id": owner_id,
        "type": "note",
        "title": "before",
        "content": "before",
    }
    if operation != "create":
        db.insert_item(dict(seed))

    def fail_log(*_args, **_kwargs):
        raise RuntimeError("injected QQ audit failure")

    monkeypatch.setattr(db, "_log_operation_with_cursor", fail_log)
    with pytest.raises(RuntimeError, match="injected QQ audit failure"):
        if operation == "create":
            await ops._db_create_with_log(dict(seed), owner_id, action="create_note")
        elif operation == "update":
            await ops._db_update_with_log(
                seed["id"],
                {"title": "after", "type": "note"},
                owner_id,
                action="edit_note",
                expected_version=0,
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

    db = Database(str(tmp_path / "pendo-chat-cas.db"))
    ops = DbOpsMixin()
    ops.db = db
    owner_id = "chat-cas-user"
    item_id = "chat-cas-note"
    db.insert_item(
        {
            "id": item_id,
            "owner_id": owner_id,
            "type": "note",
            "title": "original",
            "content": "original",
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
            action="edit_note",
            expected_version=stale.version,
        )

    current = db.get_item(item_id, owner_id)
    assert current is not None
    assert current.title == "concurrent"
    assert db.get_connection().execute("SELECT COUNT(*) FROM operation_logs").fetchone()[0] == 0
    db.cleanup()


@pytest.mark.parametrize("operation", ["update", "delete"])
def test_event_collection_change_and_operation_log_share_one_transaction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operation: str,
) -> None:
    """集合内容、子日程与审计日志必须在同一事务中提交。"""
    db = Database(str(tmp_path / f"pendo-collection-{operation}.db"))
    owner_id = "collection-transaction-user"
    collection_id = "collection-transaction"
    child_id = "collection-transaction-child"
    db.create_event_collection(
        {
            "id": collection_id,
            "owner_id": owner_id,
            "kind": "multi_node",
            "title": "修改前",
        }
    )
    db.insert_item(
        {
            "id": child_id,
            "owner_id": owner_id,
            "type": "event",
            "title": "子日程",
            "event_collection_id": collection_id,
            "event_collection_kind": "multi_node",
        }
    )

    def fail_log(*_args, **_kwargs):
        raise RuntimeError("注入集合审计失败")

    monkeypatch.setattr(db, "_log_operation_with_cursor", fail_log)
    operation_log = {
        "user_id": owner_id,
        "action": f"{operation}_event_collection",
    }
    with pytest.raises(RuntimeError, match="注入集合审计失败"):
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
                cascade=True,
                operation_log=operation_log,
            )

    assert db.get_event_collection(collection_id, owner_id)["title"] == "修改前"
    assert db.get_item(child_id, owner_id) is not None
    assert db.get_connection().execute("SELECT COUNT(*) FROM operation_logs").fetchone()[0] == 0
    db.cleanup()


def test_export_reads_all_pages_and_keeps_offset_date_in_user_calendar(
    tmp_path: Path,
) -> None:
    from plugins.pendo.services.exporter import ExporterService

    event = SimpleNamespace(
        id="offset-event",
        owner_id="u-export",
        type="event",
        title="时区标题\n不应换行",
        content="",
        category="测试",
        tags=[],
        created_at="2030-01-01T00:00:00+14:00",
        updated_at="2030-01-01T00:00:00+14:00",
        start_time="2030-01-01T00:30:00+14:00",
        end_time=None,
        location="",
        remind_times=[],
        notes="",
        event_collection_id=None,
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

        def log_transfer(self, **_kwargs):
            return 1

    database = _Db()
    result = ExporterService(database, tmp_path).export_markdown(
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
        start_time="2030-01-01T09:00:00+08:00",
        remind_times=["2030-01-01T00:00:00+00:00"],
        reminder_rules=[],
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
        start_time="2030-01-01T09:00:00+08:00",
        remind_times=[],
        reminder_rules=[],
    )

    shifted = recalculate_event_reminders(
        event,
        {"start_time": "2030-01-02T09:00:00+08:00"},
    )

    assert shifted == []


def test_event_reminders_parse_json_list_instead_of_string_characters() -> None:
    from plugins.pendo.handlers.event_support import ensure_event_reminders

    reminders = ensure_event_reminders(
        {
            "start_time": "2030-01-01T10:00:00+08:00",
            "remind_times": '["2030-01-01T09:00:00+08:00"]',
        }
    )

    assert reminders == ["2030-01-01T09:00:00+08:00", "2030-01-01T10:00:00+08:00"]


@pytest.mark.asyncio
async def test_pendo_handle_session_uses_transaction_argument_and_atomically_replaces_event_info():
    from plugins.pendo import main as pendo_main
    from plugins.pendo.config import PendoConfig

    manager = SessionManager()
    original = await manager.create(
        1001,
        None,
        "pendo",
        {
            "type": PendoConfig.SESSION_TYPE_EVENT_INFO,
            "data": {"title": "组会"},
        },
    )
    context = _PendoSessionTestContext(manager, _pendo_session_services())

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
        "title": "组会",
        "start_time": "2030-01-01T10:00:00",
        "type": "event",
    }


@pytest.mark.asyncio
async def test_pendo_event_info_replacement_failure_rolls_back_staged_generation():
    from plugins.pendo import main as pendo_main
    from plugins.pendo.config import PendoConfig

    manager = SessionManager()
    original = await manager.create(
        1001,
        None,
        "pendo",
        {
            "type": PendoConfig.SESSION_TYPE_EVENT_INFO,
            "data": {"title": "组会"},
        },
    )

    class _FailAfterStagingContext(_PendoSessionTestContext):
        async def create_session(self, initial_data=None, timeout=300.0):
            await super().create_session(initial_data=initial_data, timeout=timeout)
            raise RuntimeError("failure after staging replacement")

    context = _FailAfterStagingContext(manager, _pendo_session_services())

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


@pytest.mark.asyncio
async def test_pendo_session_service_exception_is_not_swallowed_or_committed(monkeypatch):
    from plugins.pendo import main as pendo_main
    from plugins.pendo.core.exceptions import PendoException

    manager = SessionManager()
    original = await manager.create(
        1001,
        None,
        "pendo",
        {"type": "synthetic", "nested": {"items": ["kept"]}},
    )
    context = _PendoSessionTestContext(manager, _pendo_session_services())

    async def fail_with_service_error(_user_id, _text, session, _context):
        session.set("type", "partial")
        session.get("nested")["items"].append("must-rollback")
        raise PendoException("database unavailable")

    monkeypatch.setattr(pendo_main, "handle_session_message", fail_with_service_error)

    with pytest.raises(PendoException, match="database unavailable"):
        await manager.update(
            1001,
            None,
            lambda session: pendo_main.handle_session(
                "continue",
                {"user_id": 1001},
                context,
                session,
            ),
        )

    current = await manager.peek(1001, None)
    assert current is not None
    assert current.session_id == original.session_id
    assert current.get("type") == "synthetic"
    assert current.get("nested") == {"items": ["kept"]}


@pytest.mark.asyncio
async def test_pendo_session_plain_value_error_is_not_misclassified_as_input(monkeypatch):
    from plugins.pendo import main as pendo_main

    manager = SessionManager()
    original = await manager.create(
        1001,
        None,
        "pendo",
        {"type": "synthetic", "nested": {"items": ["kept"]}},
    )
    context = _PendoSessionTestContext(manager, _pendo_session_services())

    async def fail_with_plain_value_error(_user_id, _text, session, _context):
        session.set("type", "partial")
        session.get("nested")["items"].append("must-rollback")
        raise ValueError("internal parser invariant failed")

    monkeypatch.setattr(pendo_main, "handle_session_message", fail_with_plain_value_error)

    with pytest.raises(ValueError, match="internal parser invariant"):
        await manager.update(
            1001,
            None,
            lambda session: pendo_main.handle_session(
                "continue",
                {"user_id": 1001},
                context,
                session,
            ),
        )

    current = await manager.peek(1001, None)
    assert current is not None
    assert current.session_id == original.session_id
    assert current.get("type") == "synthetic"
    assert current.get("nested") == {"items": ["kept"]}


@pytest.mark.asyncio
async def test_pendo_session_converts_only_whitelisted_input_exception(monkeypatch):
    from plugins.pendo import main as pendo_main
    from plugins.pendo.core.exceptions import MissingRequiredFieldException

    manager = SessionManager()
    await manager.create(1001, None, "pendo", {"type": "synthetic"})
    context = _PendoSessionTestContext(manager, _pendo_session_services())

    async def reject_input(_user_id, _text, _session, _context):
        raise MissingRequiredFieldException("start_time", "开始时间")

    monkeypatch.setattr(pendo_main, "handle_session_message", reject_input)

    result = await manager.update(
        1001,
        None,
        lambda session: pendo_main.handle_session(
            "continue",
            {"user_id": 1001},
            context,
            session,
        ),
    )

    assert result == [{"type": "text", "data": {"text": "❓ 请提供开始时间"}}]


def test_bare_week_month_year_are_current_calendar_ranges(monkeypatch):
    from plugins.pendo.utils import time_utils

    class _FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            current = cls(2026, 5, 3, 16, 30, 0)
            return current if tz is None else current.replace(tzinfo=tz)

    monkeypatch.setattr(time_utils, "datetime", _FrozenDateTime)

    week_start, week_end = time_utils._parse_time_range_core("week", strict=True)
    month_start, month_end = time_utils._parse_time_range_core("month", strict=True)
    year_start, year_end = time_utils._parse_time_range_core("year", strict=True)

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


def test_metadata_tokens_require_a_token_boundary():
    from plugins.pendo.utils.formatters import extract_metadata

    untouched = "scat:工作 reportp:2 notype=event"
    assert extract_metadata(untouched, with_priority=True) == {
        "category": None,
        "tags": [],
        "priority": None,
        "text": untouched,
    }
    inline = "链接 https://example.test/page#fragment 和 inline#tag 不是标签"
    assert extract_metadata(inline, with_priority=True) == {
        "category": None,
        "tags": [],
        "priority": None,
        "text": inline,
    }
    parsed = extract_metadata("写周报 cat:工作 p:2 #汇报", with_priority=True)
    assert parsed == {
        "category": "工作",
        "tags": ["汇报"],
        "priority": 2,
        "text": "写周报",
    }


def test_rate_limiter_removes_expired_user_buckets(monkeypatch: pytest.MonkeyPatch) -> None:
    from plugins.pendo.services import ai_parser as ai_parser_module
    from plugins.pendo.services.ai_parser import RateLimiter

    limiter = RateLimiter(max_calls=2, time_window=60)
    limiter.call_history = {"expired": [1.0], "active": [80.0]}
    monkeypatch.setattr(ai_parser_module.time, "time", lambda: 100.0)

    assert limiter.check_rate_limit("new-user") == (True, 0)
    assert limiter.call_history == {"active": [80.0], "new-user": [100.0]}


def test_rule_parser_prefers_longest_week_keyword_and_tolerates_invalid_clock():
    from plugins.pendo.services.rule_parser import RuleParser

    parser = RuleParser()
    now = datetime(2026, 5, 1, 8, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    parsed = parser.parse("下下周10点开会", "u-rule", now=now)
    assert parsed["start_time"] == "2026-05-15T10:00:00+08:00"

    invalid_clock = parser.parse("明天25点开会", "u-rule", now=now)
    assert invalid_clock["start_time"] == "2026-05-02T09:00:00+08:00"


def test_search_items_applies_date_range_filters():
    temp_dir, db = _make_temp_db("pendo_review_search_range")
    owner_id = "u-search-range"

    try:
        db.insert_item(
            {
                "id": "note_april",
                "owner_id": owner_id,
                "type": "note",
                "title": "会议纪要",
                "content": "四月版本",
                "created_at": "2026-04-10T09:00:00",
                "updated_at": "2026-04-10T09:00:00",
            }
        )
        db.insert_item(
            {
                "id": "note_january",
                "owner_id": owner_id,
                "type": "note",
                "title": "会议纪要",
                "content": "一月版本",
                "created_at": "2026-01-10T09:00:00",
                "updated_at": "2026-01-10T09:00:00",
            }
        )

        results = db.search_items(
            owner_id,
            "会议",
            filters={
                "type": "note",
                "date_field": "created_at",
                "start_date": "2026-04-01T00:00:00",
                "end_date": "2026-04-30T23:59:59",
            },
            limit=10,
        )

        assert [item.id for item in results] == ["note_april"]
    finally:
        db.cleanup()
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_ai_parser_build_remind_times_uses_user_timezone(monkeypatch):
    from plugins.pendo.services import ai_parser as ai_parser_module

    la_tz = ZoneInfo("America/Los_Angeles")

    class _ServerDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            base = cls(2030, 1, 2, 0, 30, 0)
            return base if tz is None else base.replace(tzinfo=tz)

    monkeypatch.setattr(ai_parser_module, "datetime", _ServerDatetime)
    monkeypatch.setattr(
        ai_parser_module,
        "now_in_timezone",
        lambda user_id=None, db=None: datetime(2030, 1, 1, 8, 30, 0, tzinfo=la_tz),
    )
    monkeypatch.setattr(
        ai_parser_module,
        "parse_and_localize",
        lambda dt_str, user_id=None, db=None: datetime.fromisoformat(dt_str).replace(tzinfo=la_tz),
    )

    parser = AIParser()

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


@pytest.mark.asyncio
async def test_ai_parser_rule_fallback_preserves_absolute_reminder_times(monkeypatch):
    parser = AIParser()
    monkeypatch.setattr(parser, "_call_llm", AsyncMock(return_value=None))
    monkeypatch.setattr(
        parser.rule_parser,
        "parse",
        lambda _text, user_id: {
            "type": "event",
            "owner_id": user_id,
            "title": "规则日程",
            "start_time": "2030-01-02T10:00:00",
            "remind_times": ["2030-01-02T09:00:00"],
        },
    )

    result = await parser.parse_event_with_ai("规则日程", "u-rule-reminder")

    assert result["remind_times"] == ["2030-01-02T09:00:00"]


@pytest.mark.asyncio
async def test_ai_parser_uses_core_route_and_forwards_task_temperature():
    captured: dict[str, object] = {}

    class AI:
        async def complete(self, route, messages, **kwargs):
            captured.update(route=route, messages=messages, **kwargs)
            return SimpleNamespace(content="ok")

    context = SimpleNamespace(capabilities=SimpleNamespace(ai=AI()))

    result = await AIParser(context=context)._call_llm(
        [{"role": "user", "content": "hello"}],
        temperature=0.25,
    )

    assert result == "ok"
    assert captured == {
        "route": "parse",
        "messages": [{"role": "user", "content": "hello"}],
        "temperature": 0.25,
    }


@pytest.mark.asyncio
async def test_cached_pendo_ai_parser_uses_live_core_capability(monkeypatch):
    from plugins.pendo import main as pendo_main

    temp_dir, db = _make_temp_db("pendo_live_secret_rotation")
    outcomes: list[object] = ["old", "new", RuntimeError("route removed")]
    routes: list[str] = []

    class AI:
        async def complete(self, route, _messages, **_kwargs):
            routes.append(route)
            outcome = outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return SimpleNamespace(content=outcome)

    context = SimpleNamespace(
        state={},
        data_dir=temp_dir,
        capabilities=SimpleNamespace(ai=AI()),
        logger=logging.getLogger("test.pendo.live_ai"),
        request_id=None,
    )

    monkeypatch.setattr(pendo_main, "_get_database", lambda _context: db)

    try:
        services = pendo_main._get_services(context)
        parser = services["ai_parser"]
        assert pendo_main._get_services(context)["ai_parser"] is parser

        assert await parser._call_llm([{"role": "user", "content": "before"}]) == "old"
        assert await parser._call_llm([{"role": "user", "content": "after"}]) == "new"
        assert await parser._call_llm([{"role": "user", "content": "deleted"}]) is None
        assert routes == ["parse", "parse", "parse"]
    finally:
        db.cleanup()
        shutil.rmtree(temp_dir, ignore_errors=True)


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


def test_reminder_dispatch_uses_owner_timezone(monkeypatch):
    from plugins.pendo.services import reminder as reminder_module

    la_tz = ZoneInfo("America/Los_Angeles")
    shanghai_tz = ZoneInfo("Asia/Shanghai")

    monkeypatch.setattr(
        reminder_module,
        "now_in_timezone",
        lambda user_id=None, db=None: (
            datetime(2030, 1, 1, 9, 0, 0, tzinfo=la_tz)
            if user_id == "u-la"
            else datetime(2030, 1, 2, 1, 0, 0, tzinfo=shanghai_tz)
        ),
    )
    monkeypatch.setattr(
        reminder_module,
        "parse_and_localize",
        lambda dt_str, user_id=None, db=None: datetime.fromisoformat(dt_str).replace(
            tzinfo=la_tz if user_id == "u-la" else shanghai_tz
        ),
    )

    item = SimpleNamespace(
        id="evt-la",
        owner_id="u-la",
        title="Morning sync",
        start_time="2030-01-01T10:00:00",
        end_time="2030-01-01T11:00:00",
        remind_times=["2030-01-01T09:00:00"],
        context={},
        location="Room 1",
        notes="",
        tags=[],
    )

    class _FakeDb:
        def get_due_reminder_items(self, *, now):
            return [item]

        def prune_reminder_logs(self, *, before):
            return 0

        def get_user_settings(self, user_id):
            assert user_id == "u-la"
            return {
                "timezone": "America/Los_Angeles",
                "quiet_hours_start": "23:00",
                "quiet_hours_end": "07:00",
                "settings_json": {"reminder_enabled": True},
            }

        def get_reminder_logs(self, item_id):
            assert item_id == "evt-la"
            return []

        def claim_reminder(self, item_id, remind_time, *, now, lease_seconds):
            assert item_id == "evt-la"
            assert remind_time == "2030-01-01T09:00:00"
            return "test-claim-token"

        def release_reminder_claim(self, item_id, remind_time, claim_token, *, retry_at):
            raise AssertionError("the test reminder is outside quiet hours")

        def get_unconfirmed_sent_reminders(self):
            return []

        def get_item(self, item_id):
            return item

    result = ReminderService(db=_FakeDb()).check_and_send_reminders()

    assert result["sent"] == 1
    assert result["messages"][0]["user_id"] == "u-la"
    assert result["messages"][0]["item_id"] == "evt-la"


def test_reminder_conflict_compares_absolute_time_across_offsets():
    existing = SimpleNamespace(
        id="evt-shanghai",
        title="跨时区会议",
        start_time="2030-01-02T10:00:00+08:00",
        end_time="2030-01-02T11:00:00+08:00",
    )

    class _FakeDb:
        settings = SimpleNamespace(get_user_settings=lambda _user_id: {"timezone": "Asia/Shanghai"})

        def get_all_items(self, user_id, filters=None, *, page_size=200):
            assert user_id == "u-shanghai"
            assert filters == {"type": "event"}
            assert page_size == 200
            return [existing]

    service = ReminderService(db=_FakeDb())

    conflicts = service.detect_conflict(
        "u-shanghai",
        "2030-01-02T02:30:00+00:00",
        "2030-01-02T03:00:00+00:00",
    )
    adjacent = service.detect_conflict(
        "u-shanghai",
        "2030-01-02T03:00:00+00:00",
        "2030-01-02T03:30:00+00:00",
    )

    assert conflicts == [
        {
            "id": "evt-shanghai",
            "title": "跨时区会议",
            "start_time": "2030-01-02T10:00:00+08:00",
            "end_time": "2030-01-02T11:00:00+08:00",
        }
    ]
    assert adjacent == []


def test_reminder_accepts_open_task_status_enum():
    task = SimpleNamespace(type=ItemType.TASK, status=TaskStatus.OPEN)

    assert ReminderService._is_active_reminder_item(task)


def test_reminder_service_skips_closed_tasks():
    done_task = SimpleNamespace(
        id="task-done",
        owner_id="u-task",
        type="task",
        status="done",
        title="已完成任务",
        remind_times=["2030-01-01T09:00:00"],
        context={},
    )

    class _FakeDb:
        def get_due_reminder_items(self, *, now):
            return [done_task]

        def prune_reminder_logs(self, *, before):
            return 0

        def get_user_settings(self, user_id):
            raise AssertionError("closed task should not load reminder settings")

        def get_unconfirmed_sent_reminders(self):
            return []

    result = ReminderService(db=_FakeDb()).check_and_send_reminders()

    assert result["sent"] == 0
    assert result["messages"] == []


def test_reminder_repeats_skip_closed_tasks():
    cancelled_task = SimpleNamespace(
        id="task-cancelled",
        owner_id="u-task",
        type="task",
        status="cancelled",
        title="已取消任务",
        remind_times=["2030-01-01T09:00:00"],
        context={},
    )

    class _FakeDb:
        def get_unconfirmed_sent_reminders(self):
            return [
                {
                    "item_id": "task-cancelled",
                    "remind_time": "2030-01-01T09:00:00",
                    "repeat_count": 1,
                    "last_sent_at": "2030-01-01T09:00:00",
                }
            ]

        def get_item(self, item_id):
            return cancelled_task

    messages = ReminderService(db=_FakeDb())._check_unconfirmed_repeats(
        current_time=datetime(2030, 1, 1, 9, 10, 0),
    )

    assert messages == []


def test_reminder_repeats_respect_disabled_setting():
    item = SimpleNamespace(
        id="evt-disabled",
        owner_id="u-disabled",
        type="event",
        title="已关闭提醒的日程",
    )

    class _FakeDb:
        def get_unconfirmed_sent_reminders(self):
            return [
                {
                    "item_id": item.id,
                    "remind_time": "2030-01-01T09:00:00+00:00",
                    "repeat_count": 1,
                    "last_sent_at": "2030-01-01T09:00:00+00:00",
                }
            ]

        def get_item(self, item_id):
            assert item_id == item.id
            return item

        def get_user_settings(self, user_id):
            assert user_id == item.owner_id
            return {"settings_json": {"reminder_enabled": False}}

        def claim_reminder_repeat(self, *_args, **_kwargs):
            raise AssertionError("disabled reminders must not acquire a repeat lease")

    messages = ReminderService(db=_FakeDb())._check_unconfirmed_repeats(
        current_time=datetime.fromisoformat("2030-01-01T09:10:00+00:00")
    )

    assert messages == []
