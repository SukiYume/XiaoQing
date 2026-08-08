"""账本、每日迁移和任务解析。"""

from __future__ import annotations

from tests.helpers.pendo_review_test_support import (
    AsyncMock,
    Database,
    Path,
    SimpleNamespace,
    TaskHandler,
    ThreadPoolExecutor,
    ZoneInfo,
    _make_temp_db,
    asyncio,
    datetime,
    json,
    pytest,
    shutil,
    threading,
)


def test_ledger_cli_edit_recomputes_amount_cents_when_amount_changes():
    from plugins.pendo.handlers.ledger import LedgerHandler

    temp_dir, db = _make_temp_db("pendo_review_ledger_amount_edit")
    owner_id = "u-ledger-cli"

    try:
        db.insert_item(
            {
                "id": "ledger_amount",
                "owner_id": owner_id,
                "type": "ledger",
                "title": "午饭",
                "amount": 12.34,
                "amount_cents": 1234,
                "transaction_type": "expense",
                "currency": "CNY",
                "ledger_category": "餐饮",
                "ledger_date": "2026-04-29",
                "account_name": "现金",
                "created_at": "2026-04-29T12:00:00",
                "updated_at": "2026-04-29T12:00:00",
            }
        )

        result = asyncio.run(
            LedgerHandler(db=db).edit_ledger(
                owner_id,
                "ledger_amount amount:56.78",
                SimpleNamespace(),
            )
        )
        item = db.get_item("ledger_amount", owner_id=owner_id)

        assert result["status"] == "success"
        assert item.amount == 56.78
        assert item.amount_cents == 5678
    finally:
        db.cleanup()
        shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.mark.parametrize("raw_amount", ["NaN", "Infinity", "-Infinity"])
def test_ledger_rejects_non_finite_amounts(raw_amount: str) -> None:
    from plugins.pendo.handlers.ledger import LedgerHandler
    from plugins.pendo.utils.validators import normalize_ledger_fields

    parsed, error = LedgerHandler._parse_quick_ledger_data(f"{raw_amount} 异常金额")

    assert parsed == {}
    assert error and "无法识别金额" in error
    with pytest.raises(ValueError, match="finite"):
        normalize_ledger_fields({"amount": raw_amount}, partial=False)


def test_ledger_list_filters_use_cents_and_validate_boundaries() -> None:
    from plugins.pendo.handlers.ledger import LedgerHandler

    filters = LedgerHandler._parse_list_filters(
        'month TYPE:income account:"招商 银行" amount:10.01..20.02 PAGE:2 ex'
    )

    assert filters.range_text == "month"
    assert filters.transaction_type == "income"
    assert filters.account == "招商 银行"
    assert filters.amount_min_cents == 1001
    assert filters.amount_max_cents == 2002
    assert filters.page == 2
    assert filters.show_extra is True

    for invalid in (
        "amount:20..10",
        "amount:NaN",
        "page:0",
        "type:unknown",
        "cat:餐饮 cat:交通",
    ):
        with pytest.raises(ValueError):
            LedgerHandler._parse_list_filters(invalid)

    parsed, error = LedgerHandler._parse_quick_ledger_data(
        "12 购买资料 https://example.com type:expense"
    )
    assert error is None
    assert parsed["title"] == "购买资料 https://example.com"


@pytest.mark.asyncio
async def test_ledger_session_uses_scoped_identity_and_rejects_corrupt_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plugins.pendo.handlers.ledger import LedgerHandler

    class _Session(dict):
        def set(self, key, value):
            self[key] = value

    class _Context:
        def __init__(self) -> None:
            self.end_calls = 0

        async def end_session(self):
            self.end_calls += 1
            return True

    handler = LedgerHandler(SimpleNamespace())
    unavailable = await handler.start_add_session("u-scoped", SimpleNamespace())
    assert unavailable == {"status": "error", "message": "❌ 无法创建记账会话，请稍后重试"}

    save_item = AsyncMock(return_value={"status": "success", "message": "saved"})
    monkeypatch.setattr(handler, "_save_ledger_item", save_item)
    context = _Context()
    session = _Session(
        {
            "step": "merchant",
            "group_id": 42,
            "data": {
                "owner_id": "u-spoofed",
                "amount": 12,
                "title": "午饭",
                "transaction_type": "expense",
                "account_name": "现金",
                "ledger_category": "餐饮",
            },
        }
    )

    result = await handler.handle_session_step("u-scoped", "0", session, context)

    assert result == {"status": "success", "message": "saved"}
    assert context.end_calls == 1
    save_item.assert_awaited_once()
    saved_user, saved_data, saved_group = save_item.await_args.args
    assert saved_user == "u-scoped"
    assert "owner_id" not in saved_data
    assert saved_group == 42

    corrupt_context = _Context()
    corrupt = await handler.handle_session_step(
        "u-scoped", "12", _Session({"data": {}}), corrupt_context
    )
    assert corrupt == {"status": "error", "message": "❌ 记账会话状态损坏，请重新开始"}
    assert corrupt_context.end_calls == 1


@pytest.mark.asyncio
async def test_ledger_month_ranges_use_user_clock_and_integer_cents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from plugins.pendo.handlers.ledger import LedgerHandler

    db = Database(str(tmp_path / "pendo-ledger-user-clock.db"))
    owner_id = "u-ledger-clock"
    try:
        for item_id, title, transaction_type, cents, ledger_date in (
            ("dec-expense", "十二月支出", "expense", 9999, "2029-12-31"),
            ("jan-expense", "一月支出", "expense", 105, "2030-01-01"),
            ("jan-income", "一月收入", "income", 205, "2030-01-02"),
            ("jan-transfer", "一月转账", "transfer", 305, "2030-01-03"),
        ):
            db.insert_item(
                {
                    "id": item_id,
                    "owner_id": owner_id,
                    "type": "ledger",
                    "title": title,
                    "amount_cents": cents,
                    "transaction_type": transaction_type,
                    "ledger_category": "转账" if transaction_type == "transfer" else "其他",
                    "ledger_date": ledger_date,
                    "account_name": "现金",
                    "counter_account_name": "银行卡" if transaction_type == "transfer" else "",
                    "created_at": f"{ledger_date}T12:00:00",
                    "updated_at": f"{ledger_date}T12:00:00",
                }
            )

        monkeypatch.setattr(
            "plugins.pendo.utils.time_utils.now_in_timezone",
            lambda _owner_id, _db: datetime(2030, 1, 1, 0, 30, 0),
        )
        handler = LedgerHandler(db)

        listing = await handler.list_ledger(owner_id, "month all", SimpleNamespace())
        summary = await handler.summary(owner_id, "month", SimpleNamespace())
        invalid_summary = await handler.summary(owner_id, "not-a-range", SimpleNamespace())

        assert listing["status"] == "success"
        assert "2030年1月账目" in listing["message"]
        assert "十二月支出" not in listing["message"]
        assert "一月支出" in listing["message"]
        assert "支出 ¥1.05" in listing["message"]
        assert "收入 ¥2.05" in listing["message"]
        assert "转账 ¥3.05" in listing["message"]
        assert summary["status"] == "success"
        assert "2030年1月收支汇总" in summary["message"]
        assert "总支出: ¥1.05" in summary["message"]
        assert "总收入: ¥2.05" in summary["message"]
        assert invalid_summary["status"] == "error"
        assert "无法解析时间范围" in invalid_summary["message"]
    finally:
        db.cleanup()


def test_ledger_type_switch_clears_transfer_fields_and_rejects_ambiguous_edits() -> None:
    from plugins.pendo.handlers.ledger import LedgerHandler

    temp_dir, db = _make_temp_db("pendo_review_ledger_type_switch")
    owner_id = "u-ledger-switch"
    item_id = "ledger-transfer"
    try:
        db.insert_item(
            {
                "id": item_id,
                "owner_id": owner_id,
                "type": "ledger",
                "title": "账户调拨",
                "amount_cents": 5000,
                "transaction_type": "transfer",
                "currency": "CNY",
                "ledger_category": "转账",
                "ledger_date": "2030-01-01",
                "account_name": "微信",
                "counter_account_name": "银行卡",
                "created_at": "2030-01-01T12:00:00",
                "updated_at": "2030-01-01T12:00:00",
            }
        )
        handler = LedgerHandler(db)

        changed = asyncio.run(
            handler.edit_ledger(owner_id, f"{item_id} type:expense", SimpleNamespace())
        )
        item = db.get_item(item_id, owner_id)
        assert changed["status"] == "success"
        assert item.transaction_type == "expense"
        assert item.counter_account_name == ""
        assert item.ledger_category == "其他"

        missing_counter = asyncio.run(
            handler.edit_ledger(owner_id, f"{item_id} type:transfer", SimpleNamespace())
        )
        ambiguous = asyncio.run(
            handler.edit_ledger(owner_id, f"{item_id} amount:60 多余正文", SimpleNamespace())
        )
        empty_date = asyncio.run(
            handler.edit_ledger(owner_id, f'{item_id} date:""', SimpleNamespace())
        )
        extra_delete = asyncio.run(
            handler.delete_ledger(owner_id, f"{item_id} extra", SimpleNamespace())
        )

        assert missing_counter["status"] == "error"
        assert ambiguous["status"] == "error"
        assert "只接受 field:value" in ambiguous["message"]
        assert empty_date["status"] == "error"
        assert "编辑字段不能为空" in empty_date["message"]
        assert extra_delete["status"] == "error"
        assert db.get_item(item_id, owner_id) is not None
    finally:
        db.cleanup()
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_daily_briefing_orders_urgent_tasks_first():
    temp_dir, db = _make_temp_db("pendo_review_briefing_priority")
    owner_id = "u-briefing-priority"

    try:
        db.insert_item(
            {
                "id": "task-urgent",
                "owner_id": owner_id,
                "type": "task",
                "title": "Fix incident",
                "category": "工作",
                "status": "open",
                "priority": 1,
                "plan_date": "2026-04-23",
                "deadline_at": "2026-04-23T18:00:00",
                "created_at": "2026-04-23T09:00:00",
                "updated_at": "2026-04-23T09:00:00",
            }
        )
        db.insert_item(
            {
                "id": "task-later",
                "owner_id": owner_id,
                "type": "task",
                "title": "Tidy backlog",
                "category": "工作",
                "status": "open",
                "priority": 4,
                "plan_date": "2026-04-23",
                "deadline_at": "2026-04-23T18:00:00",
                "created_at": "2026-04-23T09:05:00",
                "updated_at": "2026-04-23T09:05:00",
            }
        )

        _events, tasks, _overdue = db.get_briefing_items(
            owner_id,
            "2026-04-23T00:00:00",
            "2026-04-24T00:00:00",
        )

        assert [task.id for task in tasks] == ["task-urgent", "task-later"]
    finally:
        db.cleanup()
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_daily_briefing_resolves_mixed_iso_values_in_user_timezone():
    temp_dir, db = _make_temp_db("pendo_review_briefing_mixed_timezones")
    owner_id = "u-briefing-mixed-timezones"

    try:
        db.update_user_settings(owner_id, {"timezone": "Asia/Shanghai"})
        db.insert_item(
            {
                "id": "event-after-local-midnight",
                "owner_id": owner_id,
                "type": "event",
                "title": "UTC 前一天、上海今天",
                "start_time": "2026-04-22T16:30:00+00:00",
            }
        )
        db.insert_item(
            {
                "id": "task-after-local-midnight",
                "owner_id": owner_id,
                "type": "task",
                "title": "上海今天截止",
                "status": "open",
                "deadline_at": "2026-04-22T17:00:00+00:00",
            }
        )

        events, tasks, overdue = db.get_briefing_items(
            owner_id,
            "2026-04-23T00:00:00",
            "2026-04-24T00:00:00",
        )

        assert [event.id for event in events] == ["event-after-local-midnight"]
        assert [task.id for task in tasks] == ["task-after-local-midnight"]
        assert overdue == []
    finally:
        db.cleanup()
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_daily_briefing_excludes_cancelled_tasks():
    temp_dir, db = _make_temp_db("pendo_review_briefing_cancelled")
    owner_id = "u-briefing-cancelled"

    try:
        db.insert_item(
            {
                "id": "task-cancelled-today",
                "owner_id": owner_id,
                "type": "task",
                "title": "Cancelled today",
                "category": "工作",
                "status": "cancelled",
                "priority": 1,
                "plan_date": "2026-04-23",
                "deadline_at": "2026-04-22T18:00:00",
                "cancelled_at": "2026-04-22T09:00:00",
                "created_at": "2026-04-22T08:00:00",
                "updated_at": "2026-04-22T09:00:00",
            }
        )
        db.insert_item(
            {
                "id": "task-open-today",
                "owner_id": owner_id,
                "type": "task",
                "title": "Open today",
                "category": "工作",
                "status": "open",
                "priority": 3,
                "plan_date": "2026-04-23",
                "created_at": "2026-04-23T09:00:00",
                "updated_at": "2026-04-23T09:00:00",
            }
        )

        _events, tasks, overdue = db.get_briefing_items(
            owner_id,
            "2026-04-23T00:00:00",
            "2026-04-24T00:00:00",
        )

        assert [task.id for task in tasks] == ["task-open-today"]
        assert overdue == []
    finally:
        db.cleanup()
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_daily_migration_updates_plan_date_and_invalidates_task_cache():
    temp_dir, db = _make_temp_db("pendo_review_task_migration_cache")
    owner_id = "u-task-migration-cache"

    try:
        db.insert_item(
            {
                "id": "task-yesterday",
                "owner_id": owner_id,
                "type": "task",
                "title": "Move me",
                "category": "工作",
                "status": "open",
                "priority": 3,
                "plan_date": "2026-04-28",
                "created_at": "2026-04-28T08:00:00",
                "updated_at": "2026-04-28T08:00:00",
            }
        )
        assert db.get_item("task-yesterday", owner_id).plan_date == "2026-04-28"
        assert (
            db.get_items(owner_id, filters={"type": "task"}, limit=10)[0].plan_date == "2026-04-28"
        )

        migrated = db.migrate_undone_tasks_to_date(
            owner_id,
            "2026-04-28",
            "2026-04-29",
        )

        assert migrated == 1
        assert db.get_item("task-yesterday", owner_id).plan_date == "2026-04-29"
        assert (
            db.get_items(owner_id, filters={"type": "task"}, limit=10)[0].plan_date == "2026-04-29"
        )
        raw = (
            db.get_connection()
            .execute(
                "SELECT category FROM items WHERE id = ?",
                ("task-yesterday",),
            )
            .fetchone()
        )
        assert raw["category"] == "工作"
        assert db.get_item("task-yesterday", owner_id).version == 1
        settings = db.get_user_settings(owner_id)
        assert settings["settings_json"]["last_todo_migrate_date"] == "2026-04-29"
        log = (
            db.get_connection()
            .execute(
                "SELECT action, details FROM operation_logs WHERE user_id = ?",
                (owner_id,),
            )
            .fetchone()
        )
        assert log["action"] == "migrate_todos"
        assert json.loads(log["details"])["item_ids"] == ["task-yesterday"]
    finally:
        db.cleanup()
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_daily_migration_rechecks_business_state_and_rolls_back_marker_with_audit_failure(
    monkeypatch,
):
    temp_dir, db = _make_temp_db("pendo_review_task_migration_atomic")
    owner_id = "u-task-migration-atomic"
    try:
        for item_id, status, plan_date, deleted in (
            ("move", "open", "2026-05-01", False),
            ("completed", "done", "2026-05-01", False),
            ("rescheduled", "open", "2026-05-02", False),
            ("deleted", "open", "2026-05-01", True),
        ):
            db.insert_item(
                {
                    "id": item_id,
                    "owner_id": owner_id,
                    "type": "task",
                    "title": item_id,
                    "status": status,
                    "plan_date": plan_date,
                    "deleted": deleted,
                    "created_at": "2026-05-01T00:00:00",
                    "updated_at": "2026-05-01T00:00:00",
                }
            )

        def fail_log(*_args, **_kwargs):
            raise RuntimeError("injected migration audit failure")

        monkeypatch.setattr(db, "_log_operation_with_cursor", fail_log)
        with pytest.raises(RuntimeError, match="injected migration audit failure"):
            db.migrate_undone_tasks_to_date(owner_id, "2026-05-01", "2026-05-02")

        rows = {
            row["id"]: (row["plan_date"], row["status"], row["deleted"], row["version"])
            for row in db.get_connection()
            .execute(
                "SELECT id, plan_date, status, deleted, version FROM items WHERE owner_id = ?",
                (owner_id,),
            )
            .fetchall()
        }
        assert rows["move"] == ("2026-05-01", "open", 0, 0)
        assert rows["completed"] == ("2026-05-01", "done", 0, 0)
        assert rows["rescheduled"] == ("2026-05-02", "open", 0, 0)
        assert rows["deleted"] == ("2026-05-01", "open", 1, 0)
        assert db.get_user_settings(owner_id)["settings_json"].get("last_todo_migrate_date") is None
        assert db.get_connection().execute("SELECT COUNT(*) FROM operation_logs").fetchone()[0] == 0
    finally:
        db.cleanup()
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_daily_migration_is_atomic_across_database_instances():
    temp_dir, db1 = _make_temp_db("pendo_review_task_migration_instances")
    db2 = Database(db1.db_path)
    owner_id = "u-task-migration-instances"
    try:
        db1.insert_item(
            {
                "id": "move-once",
                "owner_id": owner_id,
                "type": "task",
                "title": "Move once",
                "status": "open",
                "plan_date": "2026-05-03",
            }
        )
        barrier = threading.Barrier(2)

        def migrate(db):
            barrier.wait(timeout=5)
            return db.migrate_undone_tasks_to_date(
                owner_id,
                "2026-05-03",
                "2026-05-04",
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(migrate, (db1, db2)))

        assert sorted(results) == [0, 1]
        row = (
            db1.get_connection()
            .execute("SELECT plan_date, version FROM items WHERE id = 'move-once'")
            .fetchone()
        )
        assert (row["plan_date"], row["version"]) == ("2026-05-04", 1)
        assert (
            db1.get_connection()
            .execute(
                "SELECT COUNT(*) FROM operation_logs WHERE action = 'migrate_todos' AND user_id = ?",
                (owner_id,),
            )
            .fetchone()[0]
            == 1
        )
    finally:
        db2.cleanup()
        db1.cleanup()
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_task_parser_uses_user_timezone_for_default_plan_date(monkeypatch):
    from plugins.pendo.handlers import task as task_module
    from plugins.pendo.utils import validators as validators_module

    class _ServerDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            base = cls(2030, 1, 1, 10, 0, 0)
            return base if tz is None else base.replace(tzinfo=tz)

    monkeypatch.setattr(validators_module, "datetime", _ServerDatetime)
    monkeypatch.setattr(
        task_module,
        "now_in_timezone",
        lambda user_id, db: datetime(2030, 1, 1, 21, 0, 0, tzinfo=ZoneInfo("America/Los_Angeles")),
    )

    handler = TaskHandler(db=SimpleNamespace())

    parsed = handler._parse_task_text("Write weekly recap", "u-la")

    assert parsed["category"] == "未分类"
    assert parsed["plan_date"] == "2030-01-02"


def test_task_today_shortcut_uses_user_timezone(monkeypatch):
    from plugins.pendo.handlers import task as task_module

    class _ServerDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            base = cls(2030, 1, 2, 13, 0, 0)
            return base if tz is None else base.replace(tzinfo=tz)

    class _ItemsRepo:
        def __init__(self):
            self.captured_filters = None

        def get_items(self, owner_id, filters, limit):
            self.captured_filters = filters
            return [
                SimpleNamespace(
                    id="la",
                    title="LA today",
                    status="open",
                    priority=3,
                    plan_date="2030-01-01",
                    deadline_at=None,
                    category="工作",
                    created_at="2030-01-01T08:00:00",
                ),
                SimpleNamespace(
                    id="server",
                    title="Server tomorrow",
                    status="open",
                    priority=3,
                    plan_date="2030-01-02",
                    deadline_at=None,
                    category="工作",
                    created_at="2030-01-02T08:00:00",
                ),
            ]

        def get_all_items(self, owner_id, filters):
            return self.get_items(owner_id, filters, limit=None)

    items_repo = _ItemsRepo()
    handler = TaskHandler(db=items_repo)

    monkeypatch.setattr(task_module, "datetime", _ServerDatetime)
    monkeypatch.setattr(
        task_module,
        "now_in_timezone",
        lambda user_id, db: datetime(2030, 1, 1, 23, 30, 0, tzinfo=ZoneInfo("America/Los_Angeles")),
    )

    result = asyncio.run(handler.list_tasks("u-la", "today", SimpleNamespace()))

    assert items_repo.captured_filters is not None
    assert items_repo.captured_filters["status"] == "open"
    assert "LA today" in result["message"]
    assert "Server tomorrow" not in result["message"]


def test_todo_edit_only_updates_explicit_fields_and_accepts_24_hour_deadline():
    temp_dir, db = _make_temp_db("pendo_review_todo_edit_partial")
    owner_id = "u-todo-edit"

    try:
        db.insert_item(
            {
                "id": "todo-edit",
                "owner_id": owner_id,
                "type": "task",
                "title": "写项目周报",
                "category": "工作",
                "plan_date": "2026-05-01",
                "deadline_at": "2026-05-01T18:00:00",
                "priority": 3,
                "status": "open",
                "created_at": "2026-05-01T09:00:00",
                "updated_at": "2026-05-01T09:00:00",
            }
        )

        result = asyncio.run(
            TaskHandler(db).edit_task(
                owner_id,
                "todo-edit deadline:2026-05-01T24:00",
                SimpleNamespace(),
            )
        )

        assert result["status"] == "success"
        task = db.get_item("todo-edit", owner_id)
        assert task.title == "写项目周报"
        assert task.category == "工作"
        assert task.plan_date == "2026-05-01"
        assert task.deadline_at == "2026-05-01T16:00:00+00:00"
    finally:
        db.cleanup()
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_todo_edit_title_phrase_updates_title_only():
    temp_dir, db = _make_temp_db("pendo_review_todo_title_phrase")
    owner_id = "u-todo-title-phrase"

    try:
        db.insert_item(
            {
                "id": "todo-title",
                "owner_id": owner_id,
                "type": "task",
                "title": "旧待办",
                "category": "工作",
                "plan_date": "2026-05-01",
                "deadline_at": "2026-05-01T18:00:00",
                "priority": 2,
                "status": "open",
                "created_at": "2026-05-01T09:00:00",
                "updated_at": "2026-05-01T09:00:00",
            }
        )

        result = asyncio.run(
            TaskHandler(db).edit_task(
                owner_id,
                "todo-title 标题改为新的待办标题",
                SimpleNamespace(),
            )
        )

        assert result["status"] == "success"
        task = db.get_item("todo-title", owner_id)
        assert task.title == "新的待办标题"
        assert task.category == "工作"
        assert task.plan_date == "2026-05-01"
        assert task.deadline_at == "2026-05-01T10:00:00+00:00"
        assert task.priority == 2
    finally:
        db.cleanup()
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_todo_edit_quoted_title_keeps_hash_literal_before_metadata():
    temp_dir, db = _make_temp_db("pendo_review_todo_quoted_title")
    owner_id = "u-todo-quoted-title"

    try:
        db.insert_item(
            {
                "id": "todo-quoted-title",
                "owner_id": owner_id,
                "type": "task",
                "title": "旧标题",
                "category": "原分类",
                "priority": 3,
                "status": "open",
                "created_at": "2026-05-01T09:00:00",
                "updated_at": "2026-05-01T09:00:00",
            }
        )

        result = asyncio.run(
            TaskHandler(db).edit_task(
                owner_id,
                'todo-quoted-title 标题改为 "Release #1" cat:工作',
                SimpleNamespace(),
            )
        )
        task = db.get_item("todo-quoted-title", owner_id)

        assert result["status"] == "success"
        assert task.title == "Release #1"
        assert task.category == "工作"
        assert task.tags == []
    finally:
        db.cleanup()
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_todo_edit_plain_text_and_metadata_update_only_mentioned_fields():
    temp_dir, db = _make_temp_db("pendo_review_todo_edit_semantics")
    owner_id = "u-todo-edit-semantics"

    def insert_task(item_id: str):
        db.insert_item(
            {
                "id": item_id,
                "owner_id": owner_id,
                "type": "task",
                "title": "原标题",
                "category": "原分类",
                "plan_date": "2026-05-01",
                "deadline_at": "2026-05-01T18:00:00",
                "priority": 2,
                "status": "open",
                "created_at": "2026-05-01T09:00:00",
                "updated_at": "2026-05-01T09:00:00",
            }
        )

    try:
        handler = TaskHandler(db)
        cases = [
            ("todosem1", "新标题", "新标题", "原分类"),
            ("todosem2", "cat:新分类", "原标题", "新分类"),
            ("todosem3", "新标题 cat:新分类", "新标题", "新分类"),
        ]

        for item_id, edit_text, expected_title, expected_category in cases:
            insert_task(item_id)
            result = asyncio.run(
                handler.edit_task(owner_id, f"{item_id} {edit_text}", SimpleNamespace())
            )

            assert result["status"] == "success"
            task = db.get_item(item_id, owner_id)
            assert task.title == expected_title
            assert task.category == expected_category
            assert task.plan_date == "2026-05-01"
            assert task.deadline_at == "2026-05-01T10:00:00+00:00"
            assert task.priority == 2
    finally:
        db.cleanup()
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_todo_edit_keeps_title_when_only_reminder_changes():
    temp_dir, db = _make_temp_db("pendo_review_todo_edit_reminder")
    owner_id = "u-todo-reminder"

    try:
        db.insert_item(
            {
                "id": "todo-reminder",
                "owner_id": owner_id,
                "type": "task",
                "title": "提交材料",
                "category": "行政",
                "plan_date": "2026-05-01",
                "deadline_at": "2026-05-02T10:00:00",
                "priority": 3,
                "status": "open",
                "remind_times": [],
                "created_at": "2026-05-01T09:00:00",
                "updated_at": "2026-05-01T09:00:00",
            }
        )

        result = asyncio.run(
            TaskHandler(db).edit_task(
                owner_id,
                "todo-reminder remind:2026-05-01T24:00",
                SimpleNamespace(),
            )
        )

        assert result["status"] == "success"
        task = db.get_item("todo-reminder", owner_id)
        assert task.title == "提交材料"
        assert task.remind_times == ["2026-05-01T16:00:00+00:00"]
    finally:
        db.cleanup()
        shutil.rmtree(temp_dir, ignore_errors=True)
