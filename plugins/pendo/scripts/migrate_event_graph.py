from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..services.db import Database
from ..utils.validators import (
    build_remind_times_from_rules,
    derive_reminder_rules,
    normalize_reminder_rules,
    with_start_time_reminder_rule,
)

ITEM_COLUMNS = [
    "id",
    "type",
    "title",
    "content",
    "tags",
    "category",
    "created_at",
    "updated_at",
    "owner_id",
    "context",
    "visibility",
    "attachments",
    "ai_meta",
    "deleted",
    "deleted_at",
    "start_time",
    "end_time",
    "timezone",
    "location",
    "participants",
    "rrule",
    "remind_policy_id",
    "remind_times",
    "reminder_rules",
    "event_role",
    "event_collection_id",
    "event_collection_kind",
    "event_index",
    "event_node_key",
    "source_item_id",
    "parent_id",
    "milestones",
    "notes",
]

JSON_LIST_DEFAULTS = {
    "tags",
    "attachments",
    "ai_meta",
    "participants",
    "remind_times",
    "reminder_rules",
    "milestones",
}


@dataclass
class MigrationStats:
    single_events_updated: int = 0
    multi_node_collections_created: int = 0
    multi_node_child_events_created: int = 0
    multi_node_source_events_deleted: int = 0
    reminder_logs_moved: int = 0
    recurring_collections_created: int = 0
    recurring_occurrences_updated: int = 0
    skipped: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "single_events_updated": self.single_events_updated,
            "multi_node_collections_created": self.multi_node_collections_created,
            "multi_node_child_events_created": self.multi_node_child_events_created,
            "multi_node_source_events_deleted": self.multi_node_source_events_deleted,
            "reminder_logs_moved": self.reminder_logs_moved,
            "recurring_collections_created": self.recurring_collections_created,
            "recurring_occurrences_updated": self.recurring_occurrences_updated,
            "skipped": self.skipped,
        }


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(conn, table):
        return set()
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _fetch_events(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    columns = _columns(conn, "items")
    select_parts = [
        column if column in columns else f"NULL AS {column}"
        for column in ITEM_COLUMNS
    ]
    rows = conn.execute(
        f"""
        SELECT {", ".join(select_parts)}
        FROM items
        WHERE type = 'event' AND COALESCE(deleted, 0) = 0
        """
    ).fetchall()
    return [dict(row) for row in rows]


def _loads(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, (list, dict)):
        return value
    if not isinstance(value, str):
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _normalize_iso(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        return datetime.fromisoformat(str(value).strip()).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        return None


def _parse_dt(value: Any) -> datetime | None:
    normalized = _normalize_iso(value)
    if not normalized:
        return None
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _list_value(row: dict[str, Any], key: str) -> list[Any]:
    loaded = _loads(row.get(key), [])
    return loaded if isinstance(loaded, list) else []


def _milestones(row: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for milestone in _list_value(row, "milestones"):
        if not isinstance(milestone, dict):
            continue
        name = str(milestone.get("name") or "").strip()
        time_value = _normalize_iso(milestone.get("time"))
        if not name or not time_value:
            continue
        cleaned = dict(milestone)
        cleaned["name"] = name
        cleaned["time"] = time_value
        rows.append(cleaned)
    rows.sort(key=lambda item: item["time"])
    return rows


def _normalize_rules_for_start(
    start_time: str | None,
    reminder_rules: Any,
    remind_times: list[Any],
) -> list[dict[str, int]]:
    loaded_rules = _loads(reminder_rules, [])
    try:
        normalized_rules = normalize_reminder_rules(loaded_rules)
    except ValueError:
        normalized_rules = []
    if normalized_rules:
        return normalized_rules
    if start_time and remind_times:
        try:
            return with_start_time_reminder_rule(derive_reminder_rules(start_time, remind_times))
        except ValueError:
            return [{"offset_seconds": 0}]
    return [{"offset_seconds": 0}] if start_time else []


def _remind_times_for_rules(
    start_time: str | None,
    reminder_rules: list[dict[str, int]],
) -> list[str]:
    if not start_time:
        return []
    try:
        return build_remind_times_from_rules(start_time, reminder_rules)
    except ValueError:
        return []


def _single_updates(row: dict[str, Any]) -> dict[str, Any]:
    start_time = _normalize_iso(row.get("start_time"))
    rules = _normalize_rules_for_start(
        start_time,
        row.get("reminder_rules"),
        _list_value(row, "remind_times"),
    )
    remind_times = _remind_times_for_rules(start_time, rules)
    updates = {
        "event_role": "single",
        "event_collection_id": None,
        "event_collection_kind": None,
        "event_index": None,
        "event_node_key": None,
        "source_item_id": row.get("source_item_id"),
        "reminder_rules": _dumps(rules),
        "remind_times": _dumps(remind_times),
    }
    current = {
        "event_role": row.get("event_role") or None,
        "event_collection_id": row.get("event_collection_id") or None,
        "event_collection_kind": row.get("event_collection_kind") or None,
        "event_index": row.get("event_index"),
        "event_node_key": row.get("event_node_key") or None,
        "source_item_id": row.get("source_item_id"),
        "reminder_rules": _dumps(_loads(row.get("reminder_rules"), [])),
        "remind_times": _dumps(_list_value(row, "remind_times")),
    }
    return {key: value for key, value in updates.items() if current.get(key) != value}


def _collection_exists(conn: sqlite3.Connection, collection_id: str) -> bool:
    if not _table_exists(conn, "event_collections"):
        return False
    row = conn.execute(
        "SELECT 1 FROM event_collections WHERE id = ? AND COALESCE(deleted, 0) = 0",
        (collection_id,),
    ).fetchone()
    return row is not None


def _item_exists(conn: sqlite3.Connection, item_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM items WHERE id = ? AND COALESCE(deleted, 0) = 0",
        (item_id,),
    ).fetchone()
    return row is not None


def _event_collection_payload(
    row: dict[str, Any],
    *,
    kind: str,
    collection_id: str,
    start_time: str | None,
    end_time: str | None,
    rrule: str | None = None,
    reminder_rules: list[dict[str, int]] | None = None,
) -> dict[str, Any]:
    now = datetime.now().isoformat(timespec="seconds")
    return {
        "id": collection_id,
        "owner_id": row.get("owner_id") or "",
        "kind": kind,
        "title": row.get("title") or "无标题日程",
        "content": row.get("content") or "",
        "category": row.get("category") or "未分类",
        "location": row.get("location") or "",
        "tags": row.get("tags") or "[]",
        "notes": row.get("notes") or "",
        "context": row.get("context") or "{}",
        "visibility": row.get("visibility") or "private",
        "timezone": row.get("timezone") or "Asia/Shanghai",
        "rrule": rrule,
        "reminder_rules": _dumps(reminder_rules or []),
        "start_time": start_time,
        "end_time": end_time,
        "source_item_id": row.get("id"),
        "created_at": row.get("created_at") or now,
        "updated_at": now,
        "deleted": 0,
        "deleted_at": None,
    }


def _insert_collection(cursor: sqlite3.Cursor, payload: dict[str, Any]) -> None:
    columns = list(payload.keys())
    placeholders = ", ".join("?" for _ in columns)
    quoted = ", ".join(f'"{column}"' for column in columns)
    cursor.execute(
        f"INSERT INTO event_collections ({quoted}) VALUES ({placeholders})",
        [payload[column] for column in columns],
    )


def _base_child_payload(row: dict[str, Any], child_id: str) -> dict[str, Any]:
    now = datetime.now().isoformat(timespec="seconds")
    payload = {column: row.get(column) for column in ITEM_COLUMNS}
    payload.update(
        {
            "id": child_id,
            "type": "event",
            "created_at": row.get("created_at") or now,
            "updated_at": now,
            "deleted": 0,
            "deleted_at": None,
            "rrule": None,
            "parent_id": None,
            "milestones": "[]",
        }
    )
    return payload


def _insert_item(cursor: sqlite3.Cursor, payload: dict[str, Any]) -> None:
    columns = [column for column in payload.keys() if column in _columns(cursor.connection, "items")]
    placeholders = ", ".join("?" for _ in columns)
    quoted = ", ".join(f'"{column}"' for column in columns)
    cursor.execute(
        f"INSERT INTO items ({quoted}) VALUES ({placeholders})",
        [payload[column] for column in columns],
    )
    if _table_exists(cursor.connection, "items_fts"):
        cursor.execute(
            """
            INSERT OR REPLACE INTO items_fts (id, title, content, tags, category)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                payload.get("id"),
                payload.get("title") or "",
                payload.get("content") or "",
                payload.get("tags") or "[]",
                payload.get("category") or "未分类",
            ),
        )


def _update_item(cursor: sqlite3.Cursor, item_id: str, updates: dict[str, Any]) -> None:
    if not updates:
        return
    updates = dict(updates)
    updates["updated_at"] = datetime.now().isoformat(timespec="seconds")
    columns = [column for column in updates.keys() if column in _columns(cursor.connection, "items")]
    set_clause = ", ".join(f'"{column}" = ?' for column in columns)
    cursor.execute(
        f"UPDATE items SET {set_clause} WHERE id = ?",
        [updates[column] for column in columns] + [item_id],
    )


def _sync_unsent_reminder_logs(
    cursor: sqlite3.Cursor,
    item_id: str,
    remind_times: list[str],
) -> None:
    if not _table_exists(cursor.connection, "reminder_logs"):
        return
    if not remind_times:
        cursor.execute(
            "DELETE FROM reminder_logs WHERE item_id = ? AND sent_at IS NULL",
            (item_id,),
        )
        return
    placeholders = ", ".join("?" for _ in remind_times)
    cursor.execute(
        f"""
        DELETE FROM reminder_logs
        WHERE item_id = ? AND sent_at IS NULL
        AND remind_time NOT IN ({placeholders})
        """,
        [item_id, *remind_times],
    )


def _assignment_index(remind_time: str, milestones: list[dict[str, Any]]) -> int:
    remind_dt = _parse_dt(remind_time)
    if remind_dt is None:
        return 0
    parsed = [_parse_dt(row["time"]) for row in milestones]
    following = [
        (idx, milestone_dt - remind_dt)
        for idx, milestone_dt in enumerate(parsed)
        if milestone_dt is not None and milestone_dt >= remind_dt
    ]
    if following:
        return min(following, key=lambda row: row[1])[0]
    distances = [
        (idx, abs((milestone_dt - remind_dt).total_seconds()))
        for idx, milestone_dt in enumerate(parsed)
        if milestone_dt is not None
    ]
    return min(distances, key=lambda row: row[1])[0] if distances else 0


def _move_reminder_logs(
    cursor: sqlite3.Cursor,
    source_item_id: str,
    milestones: list[dict[str, Any]],
) -> int:
    if not _table_exists(cursor.connection, "reminder_logs"):
        return 0
    rows = cursor.execute(
        """
        SELECT remind_time, sent_at, confirmed_at, user_action, repeat_count, last_sent_at
        FROM reminder_logs
        WHERE item_id = ?
        """,
        (source_item_id,),
    ).fetchall()
    for row in rows:
        target_idx = _assignment_index(row["remind_time"], milestones)
        target_id = f"{source_item_id}_m{target_idx + 1:02d}"
        cursor.execute(
            """
            INSERT INTO reminder_logs
                (item_id, remind_time, sent_at, confirmed_at, user_action, repeat_count, last_sent_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(item_id, remind_time) DO UPDATE SET
                sent_at = COALESCE(excluded.sent_at, reminder_logs.sent_at),
                confirmed_at = COALESCE(excluded.confirmed_at, reminder_logs.confirmed_at),
                user_action = COALESCE(excluded.user_action, reminder_logs.user_action),
                repeat_count = MAX(excluded.repeat_count, reminder_logs.repeat_count),
                last_sent_at = COALESCE(excluded.last_sent_at, reminder_logs.last_sent_at)
            """,
            (
                target_id,
                row["remind_time"],
                row["sent_at"],
                row["confirmed_at"],
                row["user_action"],
                row["repeat_count"] or 1,
                row["last_sent_at"],
            ),
        )
    cursor.execute("DELETE FROM reminder_logs WHERE item_id = ?", (source_item_id,))
    return len(rows)


def _migrate_multi_node(
    cursor: sqlite3.Cursor,
    row: dict[str, Any],
    milestones: list[dict[str, Any]],
    *,
    dry_run: bool,
    stats: MigrationStats,
) -> None:
    source_id = str(row["id"])
    start_time = milestones[0]["time"]
    end_time = milestones[-1]["time"]
    old_remind_times = [
        normalized
        for value in _list_value(row, "remind_times")
        if (normalized := _normalize_iso(value))
    ]
    assigned: dict[int, list[str]] = {idx: [] for idx in range(len(milestones))}
    for remind_time in old_remind_times:
        assigned[_assignment_index(remind_time, milestones)].append(remind_time)

    if not _collection_exists(cursor.connection, source_id):
        stats.multi_node_collections_created += 1
        if not dry_run:
            _insert_collection(
                cursor,
                _event_collection_payload(
                    row,
                    kind="multi_node",
                    collection_id=source_id,
                    start_time=start_time,
                    end_time=end_time,
                    reminder_rules=[],
                ),
            )

    for idx, milestone in enumerate(milestones):
        child_id = f"{source_id}_m{idx + 1:02d}"
        if _item_exists(cursor.connection, child_id):
            continue
        stats.multi_node_child_events_created += 1
        child_start = milestone["time"]
        child_rules = _normalize_rules_for_start(
            child_start,
            None,
            assigned.get(idx, []),
        )
        child_remind_times = _remind_times_for_rules(child_start, child_rules)
        if not dry_run:
            child = _base_child_payload(row, child_id)
            child.update(
                {
                    "title": milestone.get("name") or "无标题节点",
                    "start_time": child_start,
                    "end_time": _normalize_iso(milestone.get("end_time")),
                    "notes": milestone.get("notes") or "",
                    "reminder_rules": _dumps(child_rules),
                    "remind_times": _dumps(child_remind_times),
                    "event_role": "multi_node_child",
                    "event_collection_id": source_id,
                    "event_collection_kind": "multi_node",
                    "event_index": idx + 1,
                    "event_node_key": f"m{idx + 1:02d}",
                    "source_item_id": source_id,
                }
            )
            _insert_item(cursor, child)
            _sync_unsent_reminder_logs(cursor, child_id, child_remind_times)

    if not dry_run:
        moved = _move_reminder_logs(cursor, source_id, milestones)
        stats.reminder_logs_moved += moved
    elif _table_exists(cursor.connection, "reminder_logs"):
        stats.reminder_logs_moved += cursor.execute(
            "SELECT COUNT(*) FROM reminder_logs WHERE item_id = ?",
            (source_id,),
        ).fetchone()[0]

    if not row.get("deleted"):
        stats.multi_node_source_events_deleted += 1
        if not dry_run:
            now = datetime.now().isoformat(timespec="seconds")
            cursor.execute(
                """
                UPDATE items
                SET deleted = 1,
                    deleted_at = ?,
                    updated_at = ?,
                    event_collection_id = ?,
                    event_collection_kind = 'multi_node'
                WHERE id = ? AND COALESCE(deleted, 0) = 0
                """,
                (now, now, source_id, source_id),
            )
            if _table_exists(cursor.connection, "items_fts"):
                cursor.execute("DELETE FROM items_fts WHERE id = ?", (source_id,))


def _recurring_groups(events: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in events:
        parent_id = str(row.get("parent_id") or "").strip()
        if not parent_id:
            continue
        if row.get("event_collection_id") or row.get("event_role") == "recurring_occurrence":
            continue
        groups.setdefault(parent_id, []).append(row)
    for rows in groups.values():
        rows.sort(key=lambda item: (_normalize_iso(item.get("start_time")) or "", item.get("id") or ""))
    return groups


def _migrate_recurring_group(
    cursor: sqlite3.Cursor,
    collection_id: str,
    rows: list[dict[str, Any]],
    *,
    dry_run: bool,
    stats: MigrationStats,
) -> None:
    if not rows:
        return
    first = rows[0]
    start_time = _normalize_iso(first.get("start_time"))
    end_time = _normalize_iso(rows[-1].get("end_time")) or _normalize_iso(rows[-1].get("start_time"))
    first_rules = _normalize_rules_for_start(
        start_time,
        first.get("reminder_rules"),
        _list_value(first, "remind_times"),
    )
    if not _collection_exists(cursor.connection, collection_id):
        stats.recurring_collections_created += 1
        if not dry_run:
            _insert_collection(
                cursor,
                _event_collection_payload(
                    first,
                    kind="recurring",
                    collection_id=collection_id,
                    start_time=start_time,
                    end_time=end_time,
                    rrule=first.get("rrule"),
                    reminder_rules=first_rules,
                ),
            )

    for idx, row in enumerate(rows, 1):
        start = _normalize_iso(row.get("start_time"))
        rules = _normalize_rules_for_start(
            start,
            row.get("reminder_rules"),
            _list_value(row, "remind_times"),
        )
        remind_times = _remind_times_for_rules(start, rules)
        node_key = None
        parsed_start = _parse_dt(start)
        if parsed_start is not None:
            node_key = parsed_start.strftime("%Y%m%d")
        updates = {
            "event_role": "recurring_occurrence",
            "event_collection_id": collection_id,
            "event_collection_kind": "recurring",
            "event_index": idx,
            "event_node_key": node_key or f"r{idx:02d}",
            "source_item_id": row.get("id"),
            "parent_id": None,
            "rrule": None,
            "reminder_rules": _dumps(rules),
            "remind_times": _dumps(remind_times),
        }
        needs_update = any(str(row.get(key) or "") != str(value or "") for key, value in updates.items())
        if not needs_update:
            continue
        stats.recurring_occurrences_updated += 1
        if not dry_run:
            _update_item(cursor, str(row["id"]), updates)
            _sync_unsent_reminder_logs(cursor, str(row["id"]), remind_times)


def migrate_event_graph(
    db_path: str | Path,
    *,
    apply: bool = False,
    create_backup: bool = True,
    write_report: bool = True,
) -> dict[str, Any]:
    db_path = Path(db_path)
    started_at = datetime.now().isoformat(timespec="seconds")
    backup_path: Path | None = None
    report_path: Path | None = None

    if apply:
        if not db_path.exists():
            raise FileNotFoundError(db_path)
        if create_backup:
            backup_path = db_path.with_name(
                f"{db_path.name}.event-graph-backup-{datetime.now().strftime('%Y%m%d%H%M%S')}"
            )
            shutil.copy2(db_path, backup_path)
        db = Database(str(db_path))
        db.cleanup()

    conn = _connect(db_path)
    stats = MigrationStats()
    try:
        events = _fetch_events(conn)
        legacy_multi = {str(row["id"]): _milestones(row) for row in events}
        legacy_multi = {
            item_id: rows
            for item_id, rows in legacy_multi.items()
            if len(rows) >= 2
        }
        recurring_parent_ids = set(_recurring_groups(events).keys())

        if apply:
            conn.execute("BEGIN IMMEDIATE")
        cursor = conn.cursor()

        for row in events:
            item_id = str(row["id"])
            if item_id in legacy_multi:
                _migrate_multi_node(
                    cursor,
                    row,
                    legacy_multi[item_id],
                    dry_run=not apply,
                    stats=stats,
                )
                continue
            if str(row.get("parent_id") or "").strip() in recurring_parent_ids:
                continue
            if row.get("event_collection_id") or row.get("event_role") in {
                "multi_node_child",
                "recurring_occurrence",
            }:
                continue
            updates = _single_updates(row)
            if updates:
                stats.single_events_updated += 1
                if apply:
                    _update_item(cursor, item_id, updates)
                    _sync_unsent_reminder_logs(
                        cursor,
                        item_id,
                        _loads(updates.get("remind_times"), []),
                    )

        for parent_id, rows in _recurring_groups(events).items():
            _migrate_recurring_group(
                cursor,
                parent_id,
                rows,
                dry_run=not apply,
                stats=stats,
            )

        if apply:
            conn.commit()
    except Exception:
        if apply:
            conn.rollback()
        raise
    finally:
        conn.close()

    report = {
        "mode": "apply" if apply else "dry-run",
        "db": str(db_path),
        "backup": str(backup_path) if backup_path else None,
        "started_at": started_at,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        **stats.to_dict(),
    }
    if apply and write_report:
        report_path = db_path.with_name(
            f"{db_path.name}.event-graph-report-{datetime.now().strftime('%Y%m%d%H%M%S')}.json"
        )
        report["report_path"] = str(report_path)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    default_db = Path(__file__).resolve().parents[1] / "data" / "pendo.db"
    parser = argparse.ArgumentParser(
        description="Migrate legacy Pendo events into event_collections + leaf events."
    )
    parser.add_argument("db", nargs="?", default=str(default_db))
    parser.add_argument("--apply", action="store_true", help="write changes after creating a backup")
    parser.add_argument("--dry-run", action="store_true", help="preview changes; this is the default")
    args = parser.parse_args()

    result = migrate_event_graph(args.db, apply=bool(args.apply))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
