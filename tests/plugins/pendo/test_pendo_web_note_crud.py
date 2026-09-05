"""Pendo Web 笔记迁移和 CRUD。"""

from __future__ import annotations

from tests.helpers.pendo_web_items_test_support import (
    ROOT,
    Database,
    datetime,
    items_api,
    json,
    normalize_diary_fields,
    normalize_item_fields,
    normalize_note_fields,
    normalize_task_fields,
    pytest,
    shutil,
    sqlite3,
    uuid,
)


def test_database_migration_adds_note_reference_columns_to_old_items_table(tmp_path):
    db_path = tmp_path / "old_note_schema.db"
    conn    = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE items (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                title TEXT NOT NULL,
                content TEXT,
                tags TEXT,
                category TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                context TEXT,
                visibility TEXT DEFAULT 'private',
                attachments TEXT,
                ai_meta TEXT,
                deleted INTEGER DEFAULT 0,
                deleted_at TEXT,
                start_time TEXT,
                end_time TEXT,
                timezone TEXT,
                location TEXT,
                participants TEXT,
                remind_times TEXT,
                reminder_rules TEXT,
                event_role TEXT,
                event_collection_id TEXT,
                event_collection_kind TEXT,
                event_index INTEGER,
            event_node_key TEXT,
            source_item_id TEXT,
            plan_date TEXT,
            deadline_at TEXT,
            priority INTEGER,
            status TEXT,
            completed_at TEXT,
            cancelled_at TEXT,
            repeat_rule TEXT,
                mood TEXT,
                mood_score INTEGER,
                weather TEXT,
                template_id TEXT,
                diary_date TEXT,
                notes TEXT,
                amount REAL,
                ledger_category TEXT,
                ledger_date TEXT,
                remark TEXT
            )
            """
        )
        conn.commit()
    finally:
        conn.close()

    db = Database(str(db_path))
    try:
        item_id = db.insert_item(
            normalize_note_fields(
                {
                    "id": "note_ref_migrated",
                    "owner_id": "u-note-migration",
                    "type": "note",
                    "title": "迁移后引用",
                    "content": "正文",
                    "references": [{"kind": "item", "id": "task_1"}],
                    "related_items": ["task_1"],
                    "created_at": "2026-04-01T10:00:00",
                    "updated_at": "2026-04-01T10:00:00",
                },
                partial=False,
            )
        )

        item = db.get_item(item_id, owner_id="u-note-migration")
        assert item.references == [{"kind": "item", "id": "task_1"}]
        assert item.related_items == ["task_1"]
    finally:
        db.cleanup()


def test_item_update_note_logs_edit_details_for_web_undo():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_note_web_edit_log_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db           = Database(str(temp_dir / "pendo.db"))
    owner_id     = "u-note-web-edit-log"
    items_module = items_api

    try:
        db.insert_item(
            normalize_note_fields(
                {
                    "id": "note_edit",
                    "owner_id": owner_id,
                    "type": "note",
                    "title": "旧标题",
                    "content": "旧正文",
                    "category": "工作",
                    "tags": ["旧"],
                    "created_at": "2026-04-01T10:00:00",
                    "updated_at": "2026-04-01T10:00:00",
                },
                partial=False,
            )
        )
        db.insert_item(
            normalize_task_fields(
                {
                    "id": "task_1",
                    "owner_id": owner_id,
                    "type": "task",
                    "title": "关联待办",
                    "content": "",
                    "status": "open",
                    "priority": 3,
                    "created_at": "2026-04-01T11:00:00",
                    "updated_at": "2026-04-01T11:00:00",
                },
                partial=False,
            )
        )

        result = items_module.update_item(
            "note_edit",
            body=items_module.ItemUpdate(
                content       = "新正文",
                references    = [{"kind": "item", "id": "task_1"}],
                related_items = ["task_1"],
            ),
            owner_id = owner_id,
            db       = db,
        )

        assert result["ok"] is True
        updated = db.get_item("note_edit", owner_id=owner_id)
        assert updated.content == "新正文"
        assert updated.related_items == ["task_1"]
        assert updated.references == [
            {"kind": "item", "id": "task_1", "type": "task", "title": "关联待办"}
        ]

        row = (
            db.get_connection()
            .execute(
                "SELECT action, details FROM operation_logs WHERE item_id = ? ORDER BY id DESC LIMIT 1",
                ("note_edit",),
            )
            .fetchone()
        )
        details = json.loads(row[1])
        assert row[0] == "edit_note"
        assert details["old_values"]["content"] == "旧正文"
        assert details["updates"]["content"] == "新正文"
    finally:
        db.cleanup()
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_item_update_note_preserves_unchanged_missing_reference_for_web_edit():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_note_web_missing_ref_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db           = Database(str(temp_dir / "pendo.db"))
    owner_id     = "u-note-web-missing-ref"
    items_module = items_api

    try:
        db.insert_item(
            normalize_note_fields(
                {
                    "id": "note_dangling_ref",
                    "owner_id": owner_id,
                    "type": "note",
                    "title": "旧标题",
                    "content": "旧正文",
                    "category": "工作",
                    "references": [
                        {
                            "kind": "item",
                            "id": "deleted_task",
                            "type": "task",
                            "title": "已删除待办",
                        }
                    ],
                    "related_items": ["deleted_task"],
                    "created_at": "2026-04-01T10:00:00",
                    "updated_at": "2026-04-01T10:00:00",
                },
                partial=False,
            )
        )

        result = items_module.update_item(
            "note_dangling_ref",
            body=items_module.ItemUpdate(
                content       = "新正文",
                references    = [{"kind": "item", "id": "deleted_task"}],
                related_items = ["deleted_task"],
            ),
            owner_id = owner_id,
            db       = db,
        )

        assert result["ok"] is True
        updated = db.get_item("note_dangling_ref", owner_id=owner_id)
        assert updated.content == "新正文"
        assert updated.references == [
            {"kind": "item", "id": "deleted_task", "type": "task", "title": "已删除待办"}
        ]
        assert updated.related_items == ["deleted_task"]
    finally:
        db.cleanup()
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_item_update_note_rejects_new_missing_reference():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_note_web_new_missing_ref_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db           = Database(str(temp_dir / "pendo.db"))
    owner_id     = "u-note-web-new-missing-ref"
    items_module = items_api

    try:
        db.insert_item(
            normalize_note_fields(
                {
                    "id": "note_without_ref",
                    "owner_id": owner_id,
                    "type": "note",
                    "title": "旧标题",
                    "content": "旧正文",
                    "category": "工作",
                    "created_at": "2026-04-01T10:00:00",
                    "updated_at": "2026-04-01T10:00:00",
                },
                partial=False,
            )
        )

        with pytest.raises(items_module.HTTPException) as exc_info:
            items_module.update_item(
                "note_without_ref",
                body=items_module.ItemUpdate(
                    references    = [{"kind": "item", "id": "missing_task"}],
                    related_items = ["missing_task"],
                ),
                owner_id = owner_id,
                db       = db,
            )

        assert exc_info.value.status_code == 422
        assert "Referenced item not found: missing_task" in exc_info.value.detail
    finally:
        db.cleanup()
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_normalize_diary_fields_requires_content_and_clears_optional_values():
    diary = normalize_diary_fields(
        {
            "diary_date": "2026-03-26",
            "title": "  夜晚散步  ",
            "content": "  今天散步很舒服。  ",
            "location": "  江边  ",
            "mood": "😊",
            "weather": "☀️ 晴",
            "mood_score": "8",
            "template_id": "",
            "entry_time": "2026-03-26T21:30:00",
            "template_answers": [{"prompt": "今天做了什么", "answer": "散步"}],
            "is_favorite": "true",
        },
        partial=False,
    )

    assert diary["diary_date"] == "2026-03-26"
    assert diary["title"] == "夜晚散步"
    assert diary["content"] == "今天散步很舒服。"
    assert diary["location"] == "江边"
    assert diary["mood"] == "happy"
    assert diary["weather"] == "☀️ 晴"
    assert diary["mood_score"] == 8
    assert diary["template_id"] is None
    assert diary["entry_time"] == "2026-03-26T21:30:00"
    assert diary["template_answers"] == [{"prompt": "今天做了什么", "answer": "散步"}]
    assert diary["is_favorite"] is True

    cleared = normalize_diary_fields(
        {
            **diary,
            "title": "",
            "location": None,
            "weather": "",
            "mood_score": "",
            "template_answers": "",
            "is_favorite": "",
        },
        partial=False,
    )

    assert cleared["title"] == ""
    assert cleared["location"] == ""
    assert cleared["weather"] == ""
    assert cleared["mood_score"] is None
    assert cleared["template_answers"] == []
    assert cleared["is_favorite"] is False
    assert (
        normalize_diary_fields(
            {
                "diary_date": "2026-03-26",
                "content": "正文",
                "is_favorite": "false",
            },
            partial=False,
        )["is_favorite"]
        is False
    )

    with pytest.raises(ValueError, match="Diary content cannot be empty"):
        normalize_diary_fields({"diary_date": "2026-03-26", "content": ""}, partial=False)

    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        normalize_diary_fields({"diary_date": "2026/03/26", "content": "正文"}, partial=False)

    with pytest.raises(ValueError, match="between 1 and 10"):
        normalize_diary_fields(
            {"diary_date": "2026-03-26", "content": "正文", "mood_score": 11}, partial=False
        )


def test_normalize_item_fields_rejects_legacy_event_and_task_fields():
    with pytest.raises(ValueError, match="Unsupported event field: milestones"):
        normalize_item_fields(
            {
                "type": "event",
                "title": "旧事件",
                "start_time": "2026-03-26T10:00:00",
                "milestones": [],
            },
            partial=False,
        )

    with pytest.raises(ValueError, match="Unsupported task field: due_time"):
        normalize_item_fields(
            {
                "type": "task",
                "title": "旧待办",
                "due_time": "2026-03-26T18:00:00",
            },
            partial=False,
        )


def test_items_api_defaults_diary_entry_time_and_allows_multiple_entries_per_date(
    monkeypatch,
    tmp_path,
    request,
):
    mod = items_api
    db  = Database(str(tmp_path / "pendo_diary_api.db"))
    request.addfinalizer(db.cleanup)
    owner_id = "u-diary-api"
    monkeypatch.setattr(
        mod,
        "now_in_timezone",
        lambda _owner_id, _db: datetime(2026, 4, 29, 21, 30, 45),
    )
    body = mod.ItemCreate(
        type       = "diary",
        title      = "",
        content    = "补写当天记录",
        diary_date = "2026-01-31",
    )

    response = mod.create_item(body, owner_id=owner_id, db=db)
    second_response = mod.create_item(
        mod.ItemCreate(
            type       = "diary",
            content    = "同一天的第二篇",
            diary_date = "2026-01-31",
        ),
        owner_id = owner_id,
        db       = db,
    )
    saved = db.get_item(response["data"]["id"], owner_id=owner_id)
    second = db.get_item(second_response["data"]["id"], owner_id=owner_id)

    assert saved.entry_time == "2026-01-31T13:30:45+00:00"
    assert saved.title == "2026-01-31 21:30 日记"
    assert second is not None and second.diary_date == "2026-01-31"
    assert db.count_items(owner_id, {"type": "diary"}) == 2


def test_web_item_crud_rolls_back_when_operation_log_write_fails(monkeypatch, tmp_path, request):
    items_module = items_api
    db           = Database(str(tmp_path / "pendo_crud_audit_atomic.db"))
    request.addfinalizer(db.cleanup)
    owner_id = "u-crud-audit-atomic"
    db.insert_item(
        {
            "id": "existing-note",
            "owner_id": owner_id,
            "type": "note",
            "title": "原始标题",
            "content": "原始正文",
            "category": "工作",
        }
    )

    def fail_log(*_args, **_kwargs):
        raise RuntimeError("injected operation log failure")

    monkeypatch.setattr(db, "_log_operation_with_cursor", fail_log)

    with pytest.raises(RuntimeError, match="injected operation log failure"):
        items_module.create_item(
            items_module.ItemCreate(
                type     = "note",
                title    = "不得留下的新条目",
                content  = "正文",
                category = "工作",
            ),
            owner_id = owner_id,
            db       = db,
        )
    assert (
        db.get_connection()
        .execute(
            "SELECT COUNT(*) FROM items WHERE owner_id = ? AND title = ?",
            (owner_id, "不得留下的新条目"),
        )
        .fetchone()[0]
        == 0
    )

    with pytest.raises(RuntimeError, match="injected operation log failure"):
        items_module.update_item(
            "existing-note",
            items_module.ItemUpdate(title="不得提交的新标题", version=0),
            owner_id = owner_id,
            db       = db,
        )
    row = (
        db.get_connection()
        .execute(
            "SELECT title, version, deleted FROM items WHERE id = ?",
            ("existing-note",),
        )
        .fetchone()
    )
    assert (row["title"], row["version"], row["deleted"]) == ("原始标题", 0, 0)

    with pytest.raises(RuntimeError, match="injected operation log failure"):
        items_module.delete_item("existing-note", owner_id=owner_id, db=db)
    row = (
        db.get_connection()
        .execute(
            "SELECT version, deleted FROM items WHERE id = ?",
            ("existing-note",),
        )
        .fetchone()
    )
    assert (row["version"], row["deleted"]) == (0, 0)
    assert db.get_connection().execute("SELECT COUNT(*) FROM operation_logs").fetchone()[0] == 0


def test_note_reference_limits_return_422_before_database_lookup(tmp_path, request):
    items_module = items_api
    db           = Database(str(tmp_path / "pendo_note_reference_limits.db"))
    request.addfinalizer(db.cleanup)

    over_count = [{"kind": "item", "id": f"task-{index}"} for index in range(101)]
    with pytest.raises(items_module.HTTPException) as count_error:
        items_module.create_item(
            items_module.ItemCreate(
                type       = "note",
                title      = "too many",
                content    = "body",
                references = over_count,
            ),
            owner_id = "u-note-limits",
            db       = db,
        )
    assert count_error.value.status_code == 422
    assert "cannot exceed 100" in count_error.value.detail

    with pytest.raises(items_module.HTTPException) as byte_error:
        items_module.create_item(
            items_module.ItemCreate(
                type       = "note",
                title      = "too large",
                content    = "body",
                references = [{"kind": "item", "id": "界" * 22000}],
            ),
            owner_id = "u-note-limits",
            db       = db,
        )
    assert byte_error.value.status_code == 422
    assert "UTF-8 bytes" in byte_error.value.detail


def test_note_reference_resolution_uses_one_owner_scoped_query(tmp_path, request):
    items_module = items_api
    db           = Database(str(tmp_path / "pendo_note_reference_batch.db"))
    request.addfinalizer(db.cleanup)
    owner_id = "u-note-reference-batch"
    ids      = [f"task-{index:03d}" for index in range(40)]
    for item_id in ids:
        db.insert_item(
            {
                "id": item_id,
                "owner_id": owner_id,
                "type": "task",
                "title": item_id,
                "status": "open",
            }
        )
    db.insert_item(
        {
            "id": ids[0] + "-other-owner",
            "owner_id": "other-owner",
            "type": "task",
            "title": "not visible",
            "status": "open",
        }
    )

    statements = []
    db.get_connection().set_trace_callback(statements.append)
    resolved = items_module._resolve_note_reference_payload(
        db,
        owner_id,
        {"related_items": ids},
    )
    db.get_connection().set_trace_callback(None)

    item_selects = [sql for sql in statements if "SELECT * FROM items" in sql and "id IN" in sql]
    assert len(item_selects) == 1
    assert resolved["related_items"] == ids
    assert [reference["id"] for reference in resolved["references"]] == ids
