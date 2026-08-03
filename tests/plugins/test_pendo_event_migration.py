from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

from plugins.pendo.models.item import EventItem
from plugins.pendo.scripts import migrate_event_graph as event_graph_module
from plugins.pendo.scripts.migrate_event_graph import migrate_event_graph
from plugins.pendo.scripts.migrate_pendo_redesign import migrate_pendo_redesign
from plugins.pendo.services.db import Database


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def _insert_raw_item(conn: sqlite3.Connection, data: dict) -> None:
    data = dict(data)
    data.setdefault("created_at", "2030-01-01T00:00:00")
    data.setdefault("updated_at", data["created_at"])
    encoded = {}
    for key, value in data.items():
        if isinstance(value, (dict, list)):
            encoded[key] = json.dumps(value, ensure_ascii=False)
        else:
            encoded[key] = value
    # SQLite 的 references 等关键字必须作为标识符引用，才能还原旧库原始数据。
    columns = ", ".join('"' + key.replace('"', '""') + '"' for key in encoded)
    placeholders = ", ".join("?" for _ in encoded)
    conn.execute(
        f"INSERT INTO items ({columns}) VALUES ({placeholders})",
        list(encoded.values()),
    )


def _seed_legacy_events(path: Path) -> None:
    db = Database(str(path))
    try:
        conn = db.get_connection()
        conn.execute("ALTER TABLE items ADD COLUMN rrule TEXT")
        conn.execute("ALTER TABLE items ADD COLUMN parent_id TEXT")
        conn.execute("ALTER TABLE items ADD COLUMN remind_policy_id TEXT")
        conn.execute("ALTER TABLE items ADD COLUMN milestones TEXT")
        conn.execute("CREATE INDEX idx_parent_id ON items(parent_id) WHERE parent_id IS NOT NULL")
        _insert_raw_item(
            conn,
            EventItem(
                id="single1",
                owner_id="u1",
                title="单次会议",
                start_time="2030-01-02T09:00:00",
                remind_times=["2030-01-01T09:00:00"],
            ).to_dict(),
        )
        _insert_raw_item(
            conn,
            {
                "id": "multi1",
                "owner_id": "u1",
                "type": "event",
                "title": "大会",
                "content": "整体说明",
                "category": "学术",
                "start_time": "2030-02-01T09:00:00",
                "milestones": json.dumps(
                    [
                        {
                            "name": "摘要截止",
                            "time": "2030-02-01T09:00:00",
                            "notes": "只提醒摘要",
                        },
                        {
                            "name": "正式报告",
                            "time": "2030-02-02T09:00:00",
                        },
                    ],
                    ensure_ascii=False,
                ),
                "remind_times": [
                    "2030-02-01T09:00:00",
                    "2030-02-02T08:00:00",
                ],
            },
        )
        _insert_raw_item(
            conn,
            {
                "id": "recur1_20300301",
                "owner_id": "u1",
                "type": "event",
                "title": "周会",
                "start_time": "2030-03-01T10:00:00",
                "end_time": "2030-03-01T11:00:00",
                "rrule": "FREQ=WEEKLY;COUNT=2",
                "parent_id": "recur1",
                "remind_times": ["2030-03-01T09:30:00"],
            },
        )
        _insert_raw_item(
            conn,
            {
                "id": "recur1_20300308",
                "owner_id": "u1",
                "type": "event",
                "title": "周会",
                "start_time": "2030-03-08T10:00:00",
                "end_time": "2030-03-08T11:00:00",
                "rrule": "FREQ=WEEKLY;COUNT=2",
                "parent_id": "recur1",
                "remind_times": ["2030-03-08T09:30:00"],
            },
        )
        conn.execute(
            """
            UPDATE items
            SET event_role = NULL,
                event_collection_id = NULL,
                event_collection_kind = NULL,
                event_index = NULL,
                event_node_key = NULL,
                source_item_id = NULL,
                reminder_rules = NULL
            WHERE id IN ('single1', 'multi1', 'recur1_20300301', 'recur1_20300308')
            """
        )
        conn.executemany(
            """
            INSERT INTO reminder_logs
                (item_id, remind_time, sent_at, confirmed_at, user_action, repeat_count, last_sent_at)
            VALUES (?, ?, ?, ?, ?, 1, ?)
            """,
            [
                (
                    "multi1",
                    "2030-02-01T09:00:00",
                    "2030-02-01T09:00:10",
                    None,
                    None,
                    "2030-02-01T09:00:10",
                ),
                (
                    "multi1",
                    "2030-02-02T08:00:00",
                    None,
                    None,
                    None,
                    None,
                ),
            ],
        )
        conn.commit()
    finally:
        db.cleanup()


def _seed_notes_for_redesign(path: Path) -> None:
    db = Database(str(path))
    try:
        conn = db.get_connection()
        conn.execute("ALTER TABLE items ADD COLUMN due_time TEXT")
        conn.execute("ALTER TABLE items ADD COLUMN estimate INTEGER")
        conn.execute("ALTER TABLE items ADD COLUMN subtasks TEXT")
        conn.execute("ALTER TABLE items ADD COLUMN dependencies TEXT")
        conn.execute("ALTER TABLE items ADD COLUMN progress INTEGER")
        conn.execute("ALTER TABLE items ADD COLUMN direction TEXT")
        conn.execute("ALTER TABLE items ADD COLUMN payment_method TEXT")
        conn.execute("CREATE INDEX idx_due_time ON items(due_time) WHERE type='task'")
        _insert_raw_item(
            conn,
            {
                "id": "task_ref",
                "owner_id": "u1",
                "type": "task",
                "title": "关联待办",
                "content": "",
                "category": "2030-01-02",
                "tags": [],
                "status": "todo",
                "priority": 3,
                "due_time": "2030-01-02T18:30:00",
                "completed_at": None,
            },
        )
        _insert_raw_item(
            conn,
            {
                "id": "note_refs",
                "owner_id": "u1",
                "type": "note",
                "title": "引用笔记",
                "content": "正文",
                "category": "工作",
                "tags": [],
                "references": [{"kind": "item", "id": "task_ref"}],
                "related_items": ["missing_ref"],
                "last_viewed": "2030-01-01T08:00",
            },
        )
        _insert_raw_item(
            conn,
            {
                "id": "note_empty",
                "owner_id": "u1",
                "type": "note",
                "title": "空笔记",
                "content": "",
                "category": "工作",
                "tags": [],
            },
        )
        _insert_raw_item(
            conn,
            {
                "id": "diary_legacy",
                "owner_id": "u1",
                "type": "diary",
                "title": "旧日记",
                "content": "晚上散步，很开心。",
                "category": "日记",
                "tags": [],
                "diary_date": "2030-01-03",
                "mood": "😊",
                "template_answers": "legacy text",
                "is_favorite": "yes",
                "created_at": "2030-01-03T22:15:00",
                "updated_at": "2030-01-03T22:16:00",
            },
        )
        conn.execute(
            """
            INSERT INTO items (
                id, owner_id, type, title, content, amount, direction,
                ledger_category, ledger_date, payment_method, remark,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "ledger_legacy",
                "u1",
                "ledger",
                "午饭",
                "",
                32.5,
                "expense",
                "餐饮",
                "2030-01-04",
                "微信",
                "食堂",
                "2030-01-04T12:00:00",
                "2030-01-04T12:01:00",
            ),
        )
        conn.commit()
    finally:
        db.cleanup()


def test_event_graph_migration_dry_run_counts_without_mutating(tmp_path: Path):
    db_path = tmp_path / "pendo.db"
    _seed_legacy_events(db_path)

    result = migrate_event_graph(db_path, apply=False)

    assert result["mode"] == "dry-run"
    assert result["single_events_updated"] == 1
    assert result["multi_node_collections_created"] == 1
    assert result["multi_node_child_events_created"] == 2
    assert result["multi_node_source_events_deleted"] == 1
    assert result["reminder_logs_moved"] == 2
    assert result["recurring_collections_created"] == 1
    assert result["recurring_occurrences_updated"] == 2

    conn = _connect(db_path)
    try:
        single = conn.execute("SELECT event_role FROM items WHERE id = 'single1'").fetchone()
        assert single["event_role"] is None
        assert conn.execute("SELECT COUNT(*) FROM event_collections").fetchone()[0] == 0
        assert (
            conn.execute("SELECT COUNT(*) FROM items WHERE id LIKE 'multi1_m%'").fetchone()[0] == 0
        )
    finally:
        conn.close()


def test_event_graph_dry_run_rejects_missing_or_invalid_database(tmp_path: Path):
    """预览模式不得创建空库，已有非 Pendo 库也应给出明确错误。"""

    missing_path = tmp_path / "missing.db"
    with pytest.raises(FileNotFoundError):
        migrate_event_graph(missing_path, apply=False)
    assert not missing_path.exists()

    invalid_path = tmp_path / "invalid.db"
    connection = sqlite3.connect(invalid_path)
    try:
        connection.execute("CREATE TABLE unrelated (value TEXT)")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(ValueError, match="缺少 items 表"):
        migrate_event_graph(invalid_path, apply=False)
    verification = sqlite3.connect(invalid_path)
    try:
        tables = {
            row[0]
            for row in verification.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    finally:
        verification.close()
    assert tables == {"unrelated"}

    incomplete_path = tmp_path / "incomplete.db"
    connection = sqlite3.connect(incomplete_path)
    try:
        connection.execute("CREATE TABLE items (value TEXT)")
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(ValueError, match="items 表缺少事件迁移必需列: id, type"):
        migrate_event_graph(incomplete_path, apply=False)


def test_event_graph_migration_handles_mixed_timezone_legacy_reminders(tmp_path: Path):
    db_path = tmp_path / "pendo.db"
    db = Database(str(db_path))
    try:
        conn = db.get_connection()
        _insert_raw_item(
            conn,
            {
                "id": "mixed_tz_event",
                "owner_id": "u1",
                "type": "event",
                "title": "混合时区提醒",
                "content": "",
                "category": "工作",
                "start_time": "2030-01-02T09:00:00",
                "remind_times": ["2030-01-02T08:00:00+08:00"],
                "reminder_rules": None,
            },
        )
        conn.commit()
    finally:
        db.cleanup()

    result = migrate_event_graph(
        db_path,
        apply=True,
        create_backup=False,
        write_report=False,
    )

    assert result["single_events_updated"] == 1
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT reminder_rules, remind_times FROM items WHERE id = 'mixed_tz_event'"
        ).fetchone()
        assert json.loads(row["reminder_rules"]) == [
            {"offset_seconds": 3600},
            {"offset_seconds": 0},
        ]
        assert json.loads(row["remind_times"]) == [
            "2030-01-02T08:00:00",
            "2030-01-02T09:00:00",
        ]
    finally:
        conn.close()


def test_multi_node_migration_assigns_mixed_timezone_reminders_by_wall_clock(tmp_path: Path):
    """带偏移提醒与无偏移节点混用时，应按 Pendo 默认时区分配且不触发类型错误。"""

    db_path = tmp_path / "pendo.db"
    _seed_legacy_events(db_path)
    connection = _connect(db_path)
    try:
        connection.execute(
            "UPDATE items SET remind_times = ? WHERE id = 'multi1'",
            (json.dumps(["2030-02-01T08:00:00+08:00"]),),
        )
        connection.execute("DELETE FROM reminder_logs WHERE item_id = 'multi1'")
        connection.execute(
            """
            INSERT INTO reminder_logs (item_id, remind_time, repeat_count)
            VALUES ('multi1', '2030-02-01T08:00:00+08:00', 1)
            """
        )
        connection.commit()
    finally:
        connection.close()

    migrate_event_graph(db_path, apply=True, create_backup=False, write_report=False)

    connection = _connect(db_path)
    try:
        child = connection.execute(
            "SELECT reminder_rules, remind_times FROM items WHERE id = 'multi1_m01'"
        ).fetchone()
        target = connection.execute(
            "SELECT item_id FROM reminder_logs WHERE remind_time = ?",
            ("2030-02-01T08:00:00+08:00",),
        ).fetchone()
    finally:
        connection.close()
    assert json.loads(child["reminder_rules"]) == [
        {"offset_seconds": 3600},
        {"offset_seconds": 0},
    ]
    assert json.loads(child["remind_times"]) == [
        "2030-02-01T08:00:00",
        "2030-02-01T09:00:00",
    ]
    assert target["item_id"] == "multi1_m01"


def test_multi_node_child_id_collision_rolls_back_event_graph_transaction(tmp_path: Path):
    """既有无关条目占用子 ID 时必须停止迁移，不能串接提醒或删除源日程。"""

    db_path = tmp_path / "pendo.db"
    _seed_legacy_events(db_path)
    connection = _connect(db_path)
    try:
        _insert_raw_item(
            connection,
            {
                "id": "multi1_m01",
                "owner_id": "other-owner",
                "type": "note",
                "title": "不得覆盖",
                "content": "",
                "category": "测试",
            },
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(ValueError, match="子日程 ID 冲突"):
        migrate_event_graph(db_path, apply=True, create_backup=False, write_report=False)

    connection = _connect(db_path)
    try:
        source_deleted = connection.execute(
            "SELECT deleted FROM items WHERE id = 'multi1'"
        ).fetchone()[0]
        collision = connection.execute(
            "SELECT type, owner_id FROM items WHERE id = 'multi1_m01'"
        ).fetchone()
        collection_count = connection.execute(
            "SELECT COUNT(*) FROM event_collections WHERE id = 'multi1'"
        ).fetchone()[0]
        source_logs = connection.execute(
            "SELECT COUNT(*) FROM reminder_logs WHERE item_id = 'multi1'"
        ).fetchone()[0]
    finally:
        connection.close()
    assert source_deleted == 0
    assert tuple(collision) == ("note", "other-owner")
    assert collection_count == 0
    assert source_logs == 2


def test_event_graph_migration_apply_rewrites_legacy_events_and_is_idempotent(tmp_path: Path):
    db_path = tmp_path / "pendo.db"
    _seed_legacy_events(db_path)

    result = migrate_event_graph(db_path, apply=True)

    assert result["mode"] == "apply"
    assert result["single_events_updated"] == 1
    assert result["multi_node_collections_created"] == 1
    assert result["multi_node_child_events_created"] == 2
    assert result["reminder_logs_moved"] == 2
    assert Path(result["backup"]).exists()
    assert Path(result["report_path"]).exists()

    conn = _connect(db_path)
    try:
        single = conn.execute("SELECT * FROM items WHERE id = 'single1'").fetchone()
        assert single["event_role"] == "single"
        assert json.loads(single["reminder_rules"]) == [
            {"offset_seconds": 86400},
            {"offset_seconds": 0},
        ]
        assert json.loads(single["remind_times"]) == [
            "2030-01-01T09:00:00",
            "2030-01-02T09:00:00",
        ]

        collection = conn.execute("SELECT * FROM event_collections WHERE id = 'multi1'").fetchone()
        assert collection["kind"] == "multi_node"
        assert collection["title"] == "大会"
        assert collection["source_item_id"] == "multi1"
        assert conn.execute("SELECT deleted FROM items WHERE id = 'multi1'").fetchone()[0] == 1

        child1 = conn.execute("SELECT * FROM items WHERE id = 'multi1_m01'").fetchone()
        child2 = conn.execute("SELECT * FROM items WHERE id = 'multi1_m02'").fetchone()
        assert child1["event_role"] == "multi_node_child"
        assert child1["event_collection_id"] == "multi1"
        assert child1["title"] == "摘要截止"
        assert child1["notes"] == "只提醒摘要"
        assert child2["event_index"] == 2
        assert json.loads(child2["reminder_rules"]) == [
            {"offset_seconds": 3600},
            {"offset_seconds": 0},
        ]
        assert json.loads(child2["remind_times"]) == [
            "2030-02-02T08:00:00",
            "2030-02-02T09:00:00",
        ]

        log_targets = {
            row["item_id"]
            for row in conn.execute(
                "SELECT item_id FROM reminder_logs WHERE item_id LIKE 'multi1%'"
            ).fetchall()
        }
        assert log_targets == {"multi1_m01", "multi1_m02"}

        recurring_collection = conn.execute(
            "SELECT * FROM event_collections WHERE id = 'recur1'"
        ).fetchone()
        assert recurring_collection["kind"] == "recurring"
        assert recurring_collection["rrule"] == "FREQ=WEEKLY;COUNT=2"
        recurring_rows = conn.execute(
            """
            SELECT event_role, event_collection_id, event_index, parent_id, rrule
            FROM items
            WHERE id IN ('recur1_20300301', 'recur1_20300308')
            ORDER BY event_index
            """
        ).fetchall()
        assert [row["event_index"] for row in recurring_rows] == [1, 2]
        assert all(row["event_role"] == "recurring_occurrence" for row in recurring_rows)
        assert all(row["event_collection_id"] == "recur1" for row in recurring_rows)
        assert all(row["parent_id"] is None for row in recurring_rows)
        assert all(row["rrule"] is None for row in recurring_rows)
    finally:
        conn.close()

    second = migrate_event_graph(db_path, apply=True)
    assert second["backup"] != result["backup"]
    assert Path(second["backup"]).exists()
    assert Path(result["backup"]).exists()
    assert second["single_events_updated"] == 0
    assert second["multi_node_collections_created"] == 0
    assert second["multi_node_child_events_created"] == 0
    assert second["recurring_collections_created"] == 0
    assert second["recurring_occurrences_updated"] == 0


def test_event_graph_cli_rejects_conflicting_modes(monkeypatch: pytest.MonkeyPatch):
    """命令行应用与预览模式必须互斥，不能忽略用户明确指定的 dry-run。"""

    monkeypatch.setattr(
        sys,
        "argv",
        ["migrate_event_graph", "--apply", "--dry-run"],
    )

    with pytest.raises(SystemExit) as captured:
        event_graph_module.main()

    assert captured.value.code == 2


def test_redesign_normalizes_iso_only_and_invalid_lifecycle_fields(tmp_path: Path):
    """即使其他字段已规范，非标准 ISO 与损坏任务时间也必须写回并保持幂等。"""

    db_path = tmp_path / "pendo.db"
    db = Database(str(db_path))
    try:
        connection = db.get_connection()
        _insert_raw_item(
            connection,
            {
                "id": "note_iso_only",
                "owner_id": "u1",
                "type": "note",
                "title": "仅时间待规范",
                "content": "",
                "category": "工作",
                "tags": [],
                "references": [
                    {"kind": "item", "id": "missing"},
                    {"kind": "item", "id": "missing", "title": "保留后项标题"},
                ],
                "related_items": ["missing"],
                "last_viewed": "2030-01-01T08:00",
            },
        )
        _insert_raw_item(
            connection,
            {
                "id": "task_invalid_lifecycle",
                "owner_id": "u1",
                "type": "task",
                "title": "清理损坏时间",
                "content": "",
                "category": "工作",
                "tags": [],
                "status": " Done ",
                "plan_date": "not-a-date",
                "deadline_at": "not-a-time",
                "completed_at": "not-a-time",
                "cancelled_at": "2030-01-02T10:00",
                "created_at": "2030-01-01T09:00:00",
                "updated_at": "2030-01-02T11:00:00",
            },
        )
        _insert_raw_item(
            connection,
            {
                "id": "diary_iso_only",
                "owner_id": "u1",
                "type": "diary",
                "title": "仅日记时间待规范",
                "content": "正文",
                "category": "日记",
                "tags": [],
                "diary_date": "2030-01-03",
                "entry_time": "2030-01-03T22:15",
                "mood": "happy",
                "template_answers": [],
                "is_favorite": 0,
            },
        )
        connection.commit()
    finally:
        db.cleanup()

    result = migrate_pendo_redesign(db_path, apply=True)

    assert result["notes"]["notes_updated"] == 1
    assert result["tasks"]["tasks_updated"] == 1
    assert result["tasks"]["completed_timestamps_backfilled"] == 1
    assert result["diaries"]["diaries_updated"] == 1
    connection = _connect(db_path)
    try:
        note = connection.execute(
            'SELECT "references", related_items, last_viewed '
            "FROM items WHERE id = 'note_iso_only'"
        ).fetchone()
        task = connection.execute(
            """
            SELECT status, plan_date, deadline_at, completed_at, cancelled_at
            FROM items WHERE id = 'task_invalid_lifecycle'
            """
        ).fetchone()
        diary = connection.execute(
            "SELECT entry_time FROM items WHERE id = 'diary_iso_only'"
        ).fetchone()
    finally:
        connection.close()
    assert json.loads(note["references"]) == [
        {"kind": "item", "id": "missing", "title": "保留后项标题"}
    ]
    assert json.loads(note["related_items"]) == ["missing"]
    assert note["last_viewed"] == "2030-01-01T08:00:00"
    assert tuple(task) == (
        "done",
        None,
        None,
        "2030-01-02T11:00:00",
        None,
    )
    assert diary["entry_time"] == "2030-01-03T22:15:00"

    second = migrate_pendo_redesign(db_path, apply=True)
    assert second["notes"]["notes_updated"] == 0
    assert second["tasks"]["tasks_updated"] == 0
    assert second["diaries"]["diaries_updated"] == 0


def test_pendo_redesign_migration_dry_run_reports_event_note_and_cleanup_without_mutating(
    tmp_path: Path,
):
    db_path = tmp_path / "pendo.db"
    _seed_legacy_events(db_path)
    _seed_notes_for_redesign(db_path)

    result = migrate_pendo_redesign(db_path, apply=False)

    assert result["mode"] == "dry-run"
    assert not (tmp_path / "pendo.db.pendo-redesign-journal.json").exists()
    assert result["event_graph"]["single_events_updated"] == 1
    assert result["event_graph"]["multi_node_collections_created"] == 1
    assert result["event_graph"]["recurring_collections_created"] == 1
    assert result["notes"]["notes_seen"] == 2
    assert result["notes"]["notes_updated"] == 2
    assert result["notes"]["references_enriched"] == 1
    assert result["notes"]["references_added_from_related_items"] == 1
    assert result["tasks"]["tasks_seen"] == 1
    assert result["tasks"]["tasks_updated"] == 1
    assert result["tasks"]["statuses_normalized"] == 1
    assert result["tasks"]["plan_dates_from_category"] == 1
    assert result["tasks"]["deadlines_from_due_time"] == 1
    assert result["diaries"]["diaries_seen"] == 1
    assert result["diaries"]["diaries_updated"] == 1
    assert result["diaries"]["entry_times_backfilled"] == 1
    assert result["diaries"]["moods_normalized"] == 1
    assert result["ledgers"]["ledgers_seen"] == 1
    assert result["ledgers"]["ledgers_updated"] == 1
    assert result["ledgers"]["amount_cents_backfilled"] == 1
    assert result["ledgers"]["transaction_types_backfilled"] == 1
    assert result["ledgers"]["accounts_backfilled"] == 1
    assert result["legacy_event_item_columns"]["would_drop"] == [
        "rrule",
        "parent_id",
        "remind_policy_id",
        "milestones",
    ]
    assert result["legacy_task_item_columns"]["would_drop"] == [
        "due_time",
        "estimate",
        "subtasks",
        "dependencies",
        "progress",
    ]
    assert result["legacy_ledger_item_columns"]["would_drop"] == [
        "direction",
        "payment_method",
    ]

    conn = _connect(db_path)
    try:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(items)").fetchall()}
        assert {
            "rrule",
            "parent_id",
            "remind_policy_id",
            "milestones",
            "due_time",
            "direction",
            "payment_method",
        }.issubset(columns)
        assert conn.execute("SELECT COUNT(*) FROM event_collections").fetchone()[0] == 0
        note = conn.execute(
            'SELECT "references", related_items, last_viewed FROM items WHERE id = ?',
            ("note_refs",),
        ).fetchone()
        assert json.loads(note["references"]) == [{"kind": "item", "id": "task_ref"}]
        assert json.loads(note["related_items"]) == ["missing_ref"]
        assert note["last_viewed"] == "2030-01-01T08:00"
        diary = conn.execute(
            "SELECT entry_time, mood, template_answers, is_favorite FROM items WHERE id = ?",
            ("diary_legacy",),
        ).fetchone()
        assert diary["entry_time"] is None
        assert diary["mood"] == "😊"
        assert diary["template_answers"] == "legacy text"
        assert diary["is_favorite"] == "yes"
        ledger = conn.execute(
            "SELECT amount_cents, transaction_type, account_name, currency FROM items WHERE id = ?",
            ("ledger_legacy",),
        ).fetchone()
        assert ledger["amount_cents"] is None
        assert ledger["transaction_type"] is None
        assert ledger["account_name"] is None
        assert ledger["currency"] is None
    finally:
        conn.close()


def test_pendo_redesign_migration_apply_migrates_events_notes_and_drops_legacy_columns(
    tmp_path: Path,
):
    db_path = tmp_path / "pendo.db"
    _seed_legacy_events(db_path)
    _seed_notes_for_redesign(db_path)

    result = migrate_pendo_redesign(db_path, apply=True)

    assert result["mode"] == "apply"
    assert Path(result["backup"]).exists()
    assert Path(result["report_path"]).exists()
    assert result["event_graph"]["backup"] is None
    assert result["notes"]["notes_updated"] == 2
    assert result["tasks"]["tasks_updated"] == 1
    assert result["diaries"]["diaries_updated"] == 1
    assert result["ledgers"]["ledgers_updated"] == 1
    assert result["legacy_event_item_columns"]["dropped"] == [
        "rrule",
        "parent_id",
        "remind_policy_id",
        "milestones",
    ]
    assert result["legacy_task_item_columns"]["dropped"] == [
        "due_time",
        "estimate",
        "subtasks",
        "dependencies",
        "progress",
    ]
    assert result["legacy_ledger_item_columns"]["dropped"] == [
        "direction",
        "payment_method",
    ]

    conn = _connect(db_path)
    try:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(items)").fetchall()}
        assert {
            "rrule",
            "parent_id",
            "remind_policy_id",
            "milestones",
            "due_time",
            "estimate",
            "subtasks",
            "dependencies",
            "progress",
            "direction",
            "payment_method",
        }.isdisjoint(columns)
        task = conn.execute(
            "SELECT category, plan_date, deadline_at, status FROM items WHERE id = ?",
            ("task_ref",),
        ).fetchone()
        assert task["category"] == "未分类"
        assert task["plan_date"] == "2030-01-02"
        assert task["deadline_at"] == "2030-01-02T18:30:00"
        assert task["status"] == "open"

        single = conn.execute("SELECT event_role FROM items WHERE id = 'single1'").fetchone()
        assert single["event_role"] == "single"
        assert (
            conn.execute("SELECT COUNT(*) FROM items WHERE id LIKE 'multi1_m%'").fetchone()[0] == 2
        )
        recurring_collection = conn.execute(
            "SELECT kind, rrule FROM event_collections WHERE id = 'recur1'"
        ).fetchone()
        assert recurring_collection["kind"] == "recurring"
        assert recurring_collection["rrule"] == "FREQ=WEEKLY;COUNT=2"

        note = conn.execute(
            'SELECT "references", related_items, last_viewed FROM items WHERE id = ?',
            ("note_refs",),
        ).fetchone()
        assert json.loads(note["references"]) == [
            {"kind": "item", "id": "task_ref", "type": "task", "title": "关联待办"},
            {"kind": "item", "id": "missing_ref"},
        ]
        assert json.loads(note["related_items"]) == ["task_ref", "missing_ref"]
        assert note["last_viewed"] == "2030-01-01T08:00:00"

        empty = conn.execute(
            'SELECT "references", related_items, last_viewed FROM items WHERE id = ?',
            ("note_empty",),
        ).fetchone()
        assert json.loads(empty["references"]) == []
        assert json.loads(empty["related_items"]) == []
        assert empty["last_viewed"] is None

        diary = conn.execute(
            "SELECT entry_time, mood, template_answers, is_favorite FROM items WHERE id = ?",
            ("diary_legacy",),
        ).fetchone()
        assert diary["entry_time"] == "2030-01-03T22:15:00"
        assert diary["mood"] == "happy"
        assert json.loads(diary["template_answers"]) == []
        assert diary["is_favorite"] == 1

        ledger = conn.execute(
            """
            SELECT amount, amount_cents, currency, transaction_type, account_name,
                   counter_account_name, ledger_category
            FROM items WHERE id = ?
            """,
            ("ledger_legacy",),
        ).fetchone()
        assert ledger["amount"] == 32.5
        assert ledger["amount_cents"] == 3250
        assert ledger["currency"] == "CNY"
        assert ledger["transaction_type"] == "expense"
        assert ledger["account_name"] == "微信"
        assert ledger["counter_account_name"] == ""
        assert ledger["ledger_category"] == "餐饮"
    finally:
        conn.close()

    second = migrate_pendo_redesign(db_path, apply=True)
    assert second["event_graph"]["single_events_updated"] == 0
    assert second["event_graph"]["multi_node_collections_created"] == 0
    assert second["event_graph"]["multi_node_child_events_created"] == 0
    assert second["event_graph"]["recurring_collections_created"] == 0
    assert second["event_graph"]["recurring_occurrences_updated"] == 0
    assert second["notes"]["notes_updated"] == 0
    assert second["tasks"]["tasks_updated"] == 0
    assert second["diaries"]["diaries_updated"] == 0
    assert second["ledgers"]["ledgers_updated"] == 0
    assert second["legacy_event_item_columns"]["dropped"] == []
    assert second["legacy_task_item_columns"]["dropped"] == []
    assert second["legacy_ledger_item_columns"]["dropped"] == []
