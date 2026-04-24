from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from plugins.pendo.models.item import EventItem
from plugins.pendo.scripts.migrate_event_graph import migrate_event_graph
from plugins.pendo.services.db import Database


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def _seed_legacy_events(path: Path) -> None:
    db = Database(str(path))
    try:
        conn = db.get_connection()
        conn.execute("ALTER TABLE items ADD COLUMN rrule TEXT")
        conn.execute("ALTER TABLE items ADD COLUMN parent_id TEXT")
        conn.execute("ALTER TABLE items ADD COLUMN milestones TEXT")
        db.insert_item(
            EventItem(
                id="single1",
                owner_id="u1",
                title="单次会议",
                start_time="2030-01-02T09:00:00",
                remind_times=["2030-01-01T09:00:00"],
            ),
            "single1",
        )
        db.insert_item(
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
            "multi1",
        )
        db.insert_item(
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
            "recur1_20300301",
        )
        db.insert_item(
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
            "recur1_20300308",
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
        assert conn.execute("SELECT COUNT(*) FROM items WHERE id LIKE 'multi1_m%'").fetchone()[0] == 0
    finally:
        conn.close()


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

        collection = conn.execute(
            "SELECT * FROM event_collections WHERE id = 'multi1'"
        ).fetchone()
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
    assert second["single_events_updated"] == 0
    assert second["multi_node_collections_created"] == 0
    assert second["multi_node_child_events_created"] == 0
    assert second["recurring_collections_created"] == 0
    assert second["recurring_occurrences_updated"] == 0
