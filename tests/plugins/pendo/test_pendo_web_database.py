"""Web、撤销、索引和数据库迁移。"""

from __future__ import annotations

import json

from tests.helpers.pendo_review_test_support import (
    ROOT,
    Database,
    EventHandler,
    NoteHandler,
    Path,
    SimpleNamespace,
    TaskHandler,
    ZoneInfo,
    _make_temp_db,
    _seed_event_batch_fixture,
    asyncio,
    datetime,
    pytest,
    shutil,
    sqlite3,
)


def test_operation_logs_schema_supports_marked_undo_and_query_indexes(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "pendo-operation-schema.db"))
    try:
        columns = {
            row["name"] for row in db.get_connection().execute("PRAGMA table_info(operation_logs)")
        }
        indexes = {
            row["name"] for row in db.get_connection().execute("PRAGMA index_list(operation_logs)")
        }

        assert {"undone_at", "undo_log_id"} <= columns
        assert {
            "idx_operation_logs_user_time",
            "idx_operation_logs_created_at",
        } <= indexes
        owner_plan = " ".join(
            str(row["detail"])
            for row in db.get_connection().execute(
                """
                EXPLAIN QUERY PLAN
                SELECT id FROM operation_logs
                WHERE user_id = ? AND undone_at IS NULL AND created_at >= ?
                ORDER BY created_at DESC LIMIT 1
                """,
                ("owner", "2030-01-01"),
            )
        )
        retention_plan = " ".join(
            str(row["detail"])
            for row in db.get_connection().execute(
                "EXPLAIN QUERY PLAN DELETE FROM operation_logs WHERE created_at < ?",
                ("2030-01-01",),
            )
        )
        assert "idx_operation_logs_user_time" in owner_plan
        assert "idx_operation_logs_created_at" in retention_plan
    finally:
        db.cleanup()


def test_existing_operation_logs_are_migrated_without_losing_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "pendo-operation-migration.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE operation_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            action TEXT NOT NULL,
            item_type TEXT,
            item_id TEXT,
            details TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO operation_logs
            (user_id, action, item_type, item_id, details, created_at)
        VALUES ('legacy-owner', 'edit_note', 'note', 'legacy-note', '{}', '2030-01-01')
        """
    )
    conn.commit()
    conn.close()

    db = Database(str(db_path))
    try:
        row = (
            db.get_connection()
            .execute("SELECT user_id, action, undone_at, undo_log_id FROM operation_logs")
            .fetchone()
        )
        assert tuple(row) == ("legacy-owner", "edit_note", None, None)
    finally:
        db.cleanup()


def test_undo_edit_marks_source_and_appends_audit_record(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "pendo-undo-edit-audit.db"))
    owner_id = "u-undo-edit-audit"
    item_id = "note-undo-edit-audit"
    try:
        db.insert_item(
            {
                "id": item_id,
                "owner_id": owner_id,
                "type": "note",
                "title": "旧标题",
                "content": "正文",
            }
        )
        assert db.update_item(
            item_id,
            {"title": "新标题"},
            owner_id,
            item_type="note",
            operation_log={
                "user_id": owner_id,
                "action": "edit_note",
                "item_type": "note",
                "item_id": item_id,
                "details": {"old_values": {"title": "旧标题"}},
            },
        )

        result = db.undo_edit(owner_id)

        assert result["status"] == "success"
        assert db.get_item(item_id, owner_id).title == "旧标题"
        rows = (
            db.get_connection()
            .execute(
                """
            SELECT id, action, undone_at, undo_log_id, details
            FROM operation_logs WHERE user_id = ? ORDER BY id
            """,
                (owner_id,),
            )
            .fetchall()
        )
        assert [row["action"] for row in rows] == ["edit_note", "undo_edit_note"]
        assert rows[0]["undone_at"] is not None
        assert rows[0]["undo_log_id"] == rows[1]["id"]
        undo_details = json.loads(rows[1]["details"])
        assert undo_details["source_log_ids"] == [rows[0]["id"]]
        assert undo_details["restored_item_ids"] == [item_id]
        assert db.undo_edit(owner_id)["status"] == "error"
    finally:
        db.cleanup()


def test_latest_undoable_operation_uses_log_id_for_same_second(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plugins.pendo.services import db as db_module

    monkeypatch.setattr(db_module, "utc_now_iso", lambda: "2030-01-01T00:00:00+00:00")
    db = Database(str(tmp_path / "pendo-undo-same-second.db"))
    try:
        db.log_operation("edit-last", "delete", "event", "event-a")
        db.log_operation("edit-last", "edit_note", "note", "note-a")
        db.log_operation("delete-last", "edit_note", "note", "note-b")
        db.log_operation("delete-last", "delete", "event", "event-b")

        assert db.get_latest_undoable_operation("edit-last")["type"] == "edit"
        assert db.get_latest_undoable_operation("delete-last")["type"] == "delete"
    finally:
        db.cleanup()


def test_web_item_create_uses_user_clock_and_update_uses_database_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """创建时间取用户墙钟；后续存储更新由数据库入口统一盖章。"""
    from plugins.pendo.services import db as db_module
    from plugins.pendo.web.api import items as items_api

    temp_dir, db = _make_temp_db("pendo_review_web_item_clock")
    owner_id = "u-web-item-clock"

    monkeypatch.setattr(
        items_api,
        "now_in_timezone",
        lambda _owner_id, _db: datetime(2030, 1, 2, 9, 10, 11, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    monkeypatch.setattr(db_module, "utc_now_iso", lambda: "2040-02-03T04:05:06+00:00")

    try:
        created = items_api.create_item(
            items_api.ItemCreate(type="note", title="按用户时间创建"),
            owner_id=owner_id,
            db=db,
        )
        item_id = str(created["data"]["id"])
        initial = db.get_item(item_id, owner_id=owner_id)

        assert initial is not None
        assert initial.created_at == "2030-01-02T01:10:11+00:00"
        assert initial.updated_at == "2030-01-02T01:10:11+00:00"

        items_api.update_item(
            item_id,
            items_api.ItemUpdate(title="数据库时间更新"),
            owner_id=owner_id,
            db=db,
        )
        updated = db.get_item(item_id, owner_id=owner_id)

        assert updated is not None
        assert updated.created_at == "2030-01-02T01:10:11+00:00"
        assert updated.updated_at == "2040-02-03T04:05:06+00:00"
    finally:
        db.cleanup()
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_web_redesign_pages_escape_user_controlled_list_fields():
    tasks_src = (
        ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "pages" / "tasks.js"
    ).read_text(encoding="utf-8")
    dashboard_src = (
        ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "pages" / "dashboard.js"
    ).read_text(encoding="utf-8")

    assert "escapeHtml(task.title || '(无标题)')" in tasks_src
    assert "escapeHtml(task.content)" in tasks_src
    assert "escapeHtml(textCategory)" in tasks_src
    assert "escapeHtml(heading)" in dashboard_src
    assert "escapeHtml(task.title || '(无标题)')" in dashboard_src
    assert "escapeHtml(item.title || '(无摘要)')" in dashboard_src


def test_rrule_generation_stops_iterating_at_the_configured_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """重复规则必须在物化前停止迭代，不能先耗尽一个无界生成器。"""
    from dateutil import rrule as rrule_module

    from plugins.pendo.config import PendoConfig

    start = datetime(2030, 1, 2, 9, 0)

    class _GuardedRule:
        def __init__(self) -> None:
            self.index = 0

        def __iter__(self):
            return self

        def __next__(self) -> datetime:
            if self.index >= PendoConfig.EVENT_MAX_RRULE_COUNT:
                raise AssertionError("重复规则被读取超过配置上限")
            self.index += 1
            return start

    guarded = _GuardedRule()
    monkeypatch.setattr(rrule_module, "rrulestr", lambda *_args, **_kwargs: guarded)

    instances, exhausted = EventHandler._expand_recurring_instances(
        "FREQ=DAILY",
        start,
        datetime(2030, 1, 1, 9, 0),
    )

    assert exhausted is False
    assert len(instances) == PendoConfig.EVENT_MAX_RRULE_COUNT
    assert guarded.index == PendoConfig.EVENT_MAX_RRULE_COUNT


def test_web_list_and_search_http_endpoints_reject_out_of_range_pagination() -> None:
    """分页上限必须由真实 FastAPI 请求层在进入业务处理前拒绝。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from plugins.pendo.web.api import items as items_api
    from plugins.pendo.web.api import search as search_api

    temp_dir, db = _make_temp_db("pendo_review_web_pagination")
    app = FastAPI()
    app.include_router(items_api.router)
    app.include_router(search_api.router)
    app.dependency_overrides[items_api.get_current_user] = lambda: "u-pagination"
    app.dependency_overrides[items_api.get_db] = lambda: db

    try:
        with TestClient(app) as client:
            responses = [
                client.get("/items", params={"page": 0}),
                client.get("/items", params={"page_size": 101}),
                client.get("/search", params={"q": "测试", "page": 0}),
                client.get("/search", params={"q": "测试", "page_size": 101}),
            ]

        assert [response.status_code for response in responses] == [422, 422, 422, 422]
    finally:
        db.cleanup()
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_undo_delete_restores_logged_task_and_note_batches():
    temp_dir, db = _make_temp_db("pendo_review_undo_batch")
    owner_id = "u-undo-batch"

    try:
        for item_id, title in (("task-a", "A"), ("task-b", "B")):
            db.insert_item(
                {
                    "id": item_id,
                    "owner_id": owner_id,
                    "type": "task",
                    "title": title,
                    "category": "工作",
                    "status": "open",
                    "priority": 3,
                    "created_at": "2026-04-30T09:00:00",
                    "updated_at": "2026-04-30T09:00:00",
                }
            )

        deleted = asyncio.run(TaskHandler(db)._delete_category_tasks(owner_id, "工作"))
        assert deleted["status"] == "success"
        assert db.get_item("task-a", owner_id) is None
        assert db.get_item("task-b", owner_id) is None

        restored = db.undo_delete(owner_id)
        assert restored["status"] == "success"
        assert restored["affected"] == 2
        assert db.get_item("task-a", owner_id).title == "A"
        assert db.get_item("task-b", owner_id).title == "B"
        task_logs = (
            db.get_connection()
            .execute(
                """
            SELECT id, action, undone_at, undo_log_id
            FROM operation_logs WHERE user_id = ? ORDER BY id
            """,
                (owner_id,),
            )
            .fetchall()
        )
        assert [row["action"] for row in task_logs] == [
            "delete_task",
            "delete_task",
            "undo_delete_task",
        ]
        assert all(row["undone_at"] is not None for row in task_logs[:2])
        assert {row["undo_log_id"] for row in task_logs[:2]} == {task_logs[2]["id"]}
        assert db.undo_delete(owner_id)["status"] == "error"

        for item_id, title in (("note-a", "NA"), ("note-b", "NB")):
            db.insert_item(
                {
                    "id": item_id,
                    "owner_id": owner_id,
                    "type": "note",
                    "title": title,
                    "content": "body",
                    "category": "知识",
                    "created_at": "2026-04-30T10:00:00",
                    "updated_at": "2026-04-30T10:00:00",
                }
            )

        deleted_notes = asyncio.run(
            NoteHandler(db)._delete_category_notes(owner_id, "知识", SimpleNamespace())
        )
        assert deleted_notes["status"] == "success"
        assert db.get_item("note-a", owner_id) is None
        assert db.get_item("note-b", owner_id) is None

        restored_notes = db.undo_delete(owner_id)
        assert restored_notes["status"] == "success"
        assert restored_notes["affected"] == 2
        assert db.get_item("note-a", owner_id).title == "NA"
        assert db.get_item("note-b", owner_id).title == "NB"
    finally:
        db.cleanup()
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_undo_delete_restores_event_collection_and_children_from_log():
    temp_dir, db = _make_temp_db("pendo_review_undo_collection")
    owner_id = "u-undo-collection"

    try:
        db.create_event_collection(
            {
                "id": "coll-undo",
                "owner_id": owner_id,
                "kind": "multi_node",
                "title": "项目发布",
                "category": "项目",
                "start_time": "2030-05-01T10:00:00",
                "end_time": "2030-05-02T18:00:00",
                "created_at": "2026-04-30T09:00:00",
                "updated_at": "2026-04-30T09:00:00",
            }
        )
        for item_id, index, title, start_time in (
            ("coll-undo_m01", 1, "提审", "2030-05-01T10:00:00"),
            ("coll-undo_m02", 2, "上线", "2030-05-02T18:00:00"),
        ):
            db.insert_item(
                {
                    "id": item_id,
                    "owner_id": owner_id,
                    "type": "event",
                    "title": title,
                    "category": "项目",
                    "start_time": start_time,
                    "event_role": "multi_node_child",
                    "event_collection_id": "coll-undo",
                    "event_collection_kind": "multi_node",
                    "event_index": index,
                    "created_at": "2026-04-30T09:00:00",
                    "updated_at": "2026-04-30T09:00:00",
                }
            )

        assert db.delete_event_collection("coll-undo", owner_id, cascade=True) is True
        db.log_operation(
            owner_id, "delete_event_collection", item_type="event", item_id="coll-undo"
        )
        assert db.get_event_collection("coll-undo", owner_id) is None
        assert db.get_item("coll-undo_m01", owner_id) is None

        restored = db.undo_delete(owner_id)
        assert restored["status"] == "success"
        assert restored["collection_id"] == "coll-undo"
        assert db.get_event_collection("coll-undo", owner_id)["title"] == "项目发布"
        assert db.get_item("coll-undo_m01", owner_id).title == "提审"
        assert db.get_item("coll-undo_m02", owner_id).title == "上线"
    finally:
        db.cleanup()
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_rebuild_fts_index_repairs_missing_and_stale_rows():
    temp_dir, db = _make_temp_db("pendo_review_fts_rebuild")
    owner_id = "u-fts-rebuild"

    try:
        db.insert_item(
            {
                "id": "note-live",
                "owner_id": owner_id,
                "type": "note",
                "title": "全文索引修复",
                "content": "可搜索正文",
                "category": "研究",
                "created_at": "2026-04-30T09:00:00",
                "updated_at": "2026-04-30T09:00:00",
            }
        )
        db.insert_item(
            {
                "id": "note-deleted",
                "owner_id": owner_id,
                "type": "note",
                "title": "应删除索引",
                "content": "旧正文",
                "category": "研究",
                "created_at": "2026-04-30T09:00:00",
                "updated_at": "2026-04-30T09:00:00",
            }
        )
        db.delete_item("note-deleted", soft=True, owner_id=owner_id)
        conn = db.get_connection()
        conn.execute("DELETE FROM items_fts WHERE id = ?", ("note-live",))
        conn.execute(
            "INSERT INTO items_fts (id, title, content, tags, category) VALUES (?, ?, ?, ?, ?)",
            ("note-deleted", "应删除索引", "旧正文", "", "研究"),
        )
        conn.commit()

        result = db.rebuild_fts_index(owner_id)

        assert result["indexed"] == 1
        assert (
            conn.execute("SELECT 1 FROM items_fts WHERE id = ?", ("note-live",)).fetchone()
            is not None
        )
        assert (
            conn.execute("SELECT 1 FROM items_fts WHERE id = ?", ("note-deleted",)).fetchone()
            is None
        )
    finally:
        db.cleanup()
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_schema_add_column_migration_only_suppresses_its_exact_duplicate():
    from plugins.pendo.services.db_schema import _execute_add_column_migration

    conn = sqlite3.connect(":memory:")
    try:
        cursor = conn.cursor()
        cursor.execute("CREATE TABLE sample (existing TEXT)")
        _execute_add_column_migration(
            cursor,
            "ALTER TABLE sample ADD COLUMN existing TEXT",
        )

        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            _execute_add_column_migration(
                cursor,
                "ALTER TABLE missing_table ADD COLUMN value TEXT",
            )
        with pytest.raises(sqlite3.OperationalError):
            _execute_add_column_migration(cursor, "ALTER TABLE sample INVALID SQL")

        class LockedCursor:
            def execute(self, _sql):
                raise sqlite3.OperationalError("database is locked")

        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            _execute_add_column_migration(
                LockedCursor(),
                "ALTER TABLE sample ADD COLUMN another TEXT",
            )
    finally:
        conn.close()


def test_schema_migrations_are_versioned_and_idempotent():
    from plugins.pendo.services import db_schema

    temp_dir, db = _make_temp_db("pendo_review_schema_migrations")
    try:
        rows_before = (
            db.get_connection()
            .execute("SELECT version, name, sql FROM schema_migrations ORDER BY version")
            .fetchall()
        )
        assert rows_before
        add_column_count = len(db_schema._ADD_COLUMN_MIGRATIONS)
        add_column_rows = rows_before[:add_column_count]
        assert [row["version"] for row in add_column_rows] == list(range(1, add_column_count + 1))
        assert all(row["name"].startswith("add-column-") for row in add_column_rows)
        assert all("ADD COLUMN" in row["sql"] for row in add_column_rows)
        assert len(rows_before) == add_column_count

        db._init_database()
        rows_after = (
            db.get_connection()
            .execute("SELECT version, name, sql FROM schema_migrations ORDER BY version")
            .fetchall()
        )
        assert [tuple(row) for row in rows_after] == [tuple(row) for row in rows_before]
    finally:
        db.cleanup()
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_database_reinitialization_does_not_rewrite_reminder_data():
    temp_dir, db = _make_temp_db("pendo_schema_keeps_reminder_data")
    try:
        conn = db.get_connection()
        conn.execute(
            """
            INSERT INTO reminder_logs
                (item_id, remind_time, sent_at, state, repeat_count, failure_count)
            VALUES ('manual-row', '2030-01-01T00:00:00+00:00',
                    '2030-01-01T00:00:01+00:00', 'pending', 1, 0)
            """
        )
        conn.commit()

        db._init_database()

        row = conn.execute(
            "SELECT state, sent_at FROM reminder_logs WHERE item_id = 'manual-row'"
        ).fetchone()
        assert tuple(row) == ("pending", "2030-01-01T00:00:01+00:00")
    finally:
        db.cleanup()
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_event_range_query_clamps_its_sql_prefilter_at_datetime_limits(tmp_path: Path):
    from plugins.pendo.services.db import Database

    db = Database(str(tmp_path / "pendo-range-limits.db"))
    try:
        assert (
            db.get_events_for_range("u-range-limits", "0001-01-01T00:00:00", "0001-01-02T00:00:00")
            == []
        )
        assert (
            db.get_events_for_range("u-range-limits", "9999-12-30T00:00:00", "9999-12-31T23:59:59")
            == []
        )
    finally:
        db.cleanup()


def test_database_initialization_helpers_share_one_rollback_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from plugins.pendo.services import db as db_module
    from plugins.pendo.services import db_schema as db_schema_module

    db_path = tmp_path / "pendo-init-rollback.db"
    db = db_module.Database(str(db_path))
    original_create_event_schema = db_schema_module._create_event_schema
    helper_connection_ids: list[int] = []

    def fail_after_event_schema(cursor: sqlite3.Cursor) -> None:
        helper_connection_ids.append(id(cursor.connection))
        original_create_event_schema(cursor)
        cursor.execute(
            """
            INSERT INTO items (id, type, created_at, updated_at, owner_id)
            VALUES ('must-rollback', 'note', '2030-01-01', '2030-01-01', 'owner')
            """
        )
        raise RuntimeError("injected initialization failure")

    monkeypatch.setattr(db_schema_module, "_create_event_schema", fail_after_event_schema)
    try:
        with pytest.raises(RuntimeError, match="injected initialization failure"):
            db._init_database()

        assert helper_connection_ids == [id(db.get_connection())]
        assert (
            db.get_connection().execute("SELECT 1 FROM items WHERE id = 'must-rollback'").fetchone()
            is None
        )
    finally:
        db.cleanup()


@pytest.mark.asyncio
async def test_event_range_lists_batch_collection_and_reminder_queries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db = Database(str(tmp_path / "pendo-event-batch-lists.db"))
    owner_id = "u-event-batch"
    _seed_event_batch_fixture(db, owner_id)
    handler = EventHandler(db, SimpleNamespace(), SimpleNamespace())
    original_collections = db.get_event_collections_by_ids
    original_logs = db.get_reminder_logs_by_item_ids
    calls = {"collections": 0, "logs": 0}

    def counted_collections(request_owner: str, collection_ids: list[str]):
        calls["collections"] += 1
        return original_collections(request_owner, collection_ids)

    def counted_logs(request_owner: str, item_ids: list[str]):
        calls["logs"] += 1
        return original_logs(request_owner, item_ids)

    def forbid_single_query(*_args, **_kwargs):
        raise AssertionError("range rendering must not issue per-row queries")

    monkeypatch.setattr(db, "get_event_collections_by_ids", counted_collections)
    monkeypatch.setattr(db, "get_reminder_logs_by_item_ids", counted_logs)
    monkeypatch.setattr(db, "get_event_collection", forbid_single_query)
    monkeypatch.setattr(db, "get_reminder_logs", forbid_single_query)

    try:
        event_result = await handler.list_events(
            owner_id,
            "2030-01-01..2030-01-02",
            {},
        )
        assert event_result["status"] == "success"
        assert "项目甲" in event_result["message"]
        assert "项目乙" in event_result["message"]
        assert calls == {"collections": 1, "logs": 0}

        calls.update(collections=0, logs=0)
        reminder_result = await handler.list_reminders(
            owner_id,
            "2030-01-01..2030-01-02",
            {},
        )
        assert reminder_result["status"] == "success"
        assert "项目甲" in reminder_result["message"]
        assert "项目乙" in reminder_result["message"]
        assert calls == {"collections": 1, "logs": 1}
    finally:
        db.cleanup()
