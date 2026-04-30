from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from plugins.pendo.scripts.migrate_event_graph import migrate_event_graph
    from plugins.pendo.services.db import Database
    from plugins.pendo.utils.validators import (
        normalize_diary_mood,
        normalize_ledger_fields,
        normalize_template_answers,
    )
else:
    from ..services.db import Database
    from ..utils.validators import (
        normalize_diary_mood,
        normalize_ledger_fields,
        normalize_template_answers,
    )
    from .migrate_event_graph import migrate_event_graph


LEGACY_EVENT_ITEM_COLUMNS = ("rrule", "parent_id", "remind_policy_id", "milestones")
LEGACY_TASK_ITEM_COLUMNS = ("due_time", "estimate", "subtasks", "dependencies", "progress")
LEGACY_LEDGER_ITEM_COLUMNS = ("direction", "payment_method")
NOTE_COLUMNS = ("references", "related_items", "last_viewed")
TASK_COLUMNS = ("plan_date", "deadline_at", "repeat_rule", "cancelled_at")
DIARY_COLUMNS = ("entry_time", "template_answers", "is_favorite")
LEDGER_COLUMNS = (
    "amount_cents",
    "currency",
    "transaction_type",
    "account_name",
    "counter_account_name",
    "merchant",
)


@dataclass
class NoteMigrationStats:
    notes_seen: int = 0
    notes_updated: int = 0
    references_enriched: int = 0
    references_added_from_related_items: int = 0
    missing_schema_columns: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "notes_seen": self.notes_seen,
            "notes_updated": self.notes_updated,
            "references_enriched": self.references_enriched,
            "references_added_from_related_items": self.references_added_from_related_items,
            "missing_schema_columns": self.missing_schema_columns,
        }


@dataclass
class TaskMigrationStats:
    tasks_seen: int = 0
    tasks_updated: int = 0
    statuses_normalized: int = 0
    plan_dates_from_category: int = 0
    plan_dates_from_due_time: int = 0
    deadlines_from_due_time: int = 0
    cancelled_timestamps_moved: int = 0
    completed_timestamps_backfilled: int = 0
    missing_schema_columns: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tasks_seen": self.tasks_seen,
            "tasks_updated": self.tasks_updated,
            "statuses_normalized": self.statuses_normalized,
            "plan_dates_from_category": self.plan_dates_from_category,
            "plan_dates_from_due_time": self.plan_dates_from_due_time,
            "deadlines_from_due_time": self.deadlines_from_due_time,
            "cancelled_timestamps_moved": self.cancelled_timestamps_moved,
            "completed_timestamps_backfilled": self.completed_timestamps_backfilled,
            "missing_schema_columns": self.missing_schema_columns,
        }


@dataclass
class DiaryMigrationStats:
    diaries_seen: int = 0
    diaries_updated: int = 0
    entry_times_backfilled: int = 0
    moods_normalized: int = 0
    moods_cleared: int = 0
    template_answers_normalized: int = 0
    favorites_normalized: int = 0
    missing_schema_columns: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "diaries_seen": self.diaries_seen,
            "diaries_updated": self.diaries_updated,
            "entry_times_backfilled": self.entry_times_backfilled,
            "moods_normalized": self.moods_normalized,
            "moods_cleared": self.moods_cleared,
            "template_answers_normalized": self.template_answers_normalized,
            "favorites_normalized": self.favorites_normalized,
            "missing_schema_columns": self.missing_schema_columns,
        }


@dataclass
class LedgerMigrationStats:
    ledgers_seen: int = 0
    ledgers_updated: int = 0
    amount_cents_backfilled: int = 0
    transaction_types_backfilled: int = 0
    accounts_backfilled: int = 0
    currencies_backfilled: int = 0
    missing_schema_columns: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ledgers_seen": self.ledgers_seen,
            "ledgers_updated": self.ledgers_updated,
            "amount_cents_backfilled": self.amount_cents_backfilled,
            "transaction_types_backfilled": self.transaction_types_backfilled,
            "accounts_backfilled": self.accounts_backfilled,
            "currencies_backfilled": self.currencies_backfilled,
            "missing_schema_columns": self.missing_schema_columns,
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


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _indexes_referencing_columns(
    conn: sqlite3.Connection,
    table: str,
    columns: tuple[str, ...],
) -> list[str]:
    if not columns:
        return []
    needle_columns = {column.lower() for column in columns}
    indexes: list[str] = []
    rows = conn.execute(
        """
        SELECT name, sql
        FROM sqlite_master
        WHERE type = 'index'
          AND tbl_name = ?
          AND sql IS NOT NULL
        ORDER BY name
        """,
        (table,),
    ).fetchall()
    for row in rows:
        sql = (row["sql"] or "").lower()
        indexed_columns = {
            info["name"].lower()
            for info in conn.execute(f"PRAGMA index_info({_quote_identifier(row['name'])})").fetchall()
            if info["name"]
        }
        if indexed_columns & needle_columns or any(column in sql for column in needle_columns):
            indexes.append(row["name"])
    return indexes


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


def _normalize_iso_or_none(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        return datetime.fromisoformat(str(value).strip()).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        return None


def _ensure_runtime_schema(db_path: Path) -> None:
    db = Database(str(db_path))
    db.cleanup()


def _require_db_file(db_path: Path) -> None:
    if not db_path.exists():
        raise FileNotFoundError(db_path)


def _fetch_notes(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    columns = _columns(conn, "items")
    select_parts = [
        "id",
        "owner_id",
        "title",
        '"references" AS "references"' if "references" in columns else 'NULL AS "references"',
        "related_items" if "related_items" in columns else "NULL AS related_items",
        "last_viewed" if "last_viewed" in columns else "NULL AS last_viewed",
    ]
    rows = conn.execute(
        f"""
        SELECT {", ".join(select_parts)}
        FROM items
        WHERE type = 'note' AND COALESCE(deleted, 0) = 0
        """
    ).fetchall()
    return [dict(row) for row in rows]


def _lookup_target(conn: sqlite3.Connection, owner_id: str, item_id: str) -> dict[str, str] | None:
    row = conn.execute(
        """
        SELECT type, title
        FROM items
        WHERE id = ? AND owner_id = ? AND COALESCE(deleted, 0) = 0
        """,
        (item_id, owner_id),
    ).fetchone()
    if not row:
        return None
    return {
        "type": str(row["type"] or "item"),
        "title": str(row["title"] or "无标题"),
    }


def _clean_text(value: Any, max_length: int = 200) -> str:
    text = str(value or "").strip()
    return text[:max_length]


def _normalize_note_payload(
    conn: sqlite3.Connection,
    row: dict[str, Any],
    stats: NoteMigrationStats,
) -> dict[str, Any]:
    owner_id = str(row.get("owner_id") or "")
    clean_refs: list[dict[str, str]] = []
    seen: set[str] = set()

    def add_reference(raw_ref: Any, *, from_related: bool = False) -> None:
        if isinstance(raw_ref, dict):
            ref_id = _clean_text(raw_ref.get("id"), 120)
            kind = _clean_text(raw_ref.get("kind") or "item", 40) or "item"
            ref_type = _clean_text(raw_ref.get("type"), 40)
            ref_title = _clean_text(raw_ref.get("title"), 200)
        else:
            ref_id = _clean_text(raw_ref, 120)
            kind = "item"
            ref_type = ""
            ref_title = ""
        if not ref_id or ref_id in seen:
            return
        seen.add(ref_id)

        target = _lookup_target(conn, owner_id, ref_id)
        enriched = False
        if target:
            if not ref_type:
                ref_type = target["type"]
                enriched = True
            if not ref_title:
                ref_title = target["title"]
                enriched = True

        clean_ref = {"kind": kind, "id": ref_id}
        if ref_type:
            clean_ref["type"] = ref_type
        if ref_title:
            clean_ref["title"] = ref_title
        clean_refs.append(clean_ref)
        if enriched:
            stats.references_enriched += 1
        if from_related:
            stats.references_added_from_related_items += 1

    for ref in _loads(row.get("references"), []):
        add_reference(ref)
    for related_id in _loads(row.get("related_items"), []):
        add_reference(related_id, from_related=True)

    return {
        "references": clean_refs,
        "related_items": [ref["id"] for ref in clean_refs],
        "last_viewed": _normalize_iso_or_none(row.get("last_viewed")),
    }


def migrate_note_fields(db_path: str | Path, *, apply: bool = False) -> dict[str, Any]:
    db_path = Path(db_path)
    _require_db_file(db_path)
    if apply:
        _ensure_runtime_schema(db_path)

    conn = _connect(db_path)
    stats = NoteMigrationStats()
    try:
        item_columns = _columns(conn, "items")
        stats.missing_schema_columns = [column for column in NOTE_COLUMNS if column not in item_columns]
        notes = _fetch_notes(conn)
        stats.notes_seen = len(notes)

        if apply:
            conn.execute("BEGIN IMMEDIATE")
        for row in notes:
            normalized = _normalize_note_payload(conn, row, stats)
            current_refs = _loads(row.get("references"), None)
            current_related = _loads(row.get("related_items"), None)
            current_last_viewed = _normalize_iso_or_none(row.get("last_viewed"))
            needs_update = (
                current_refs != normalized["references"]
                or current_related != normalized["related_items"]
                or current_last_viewed != normalized["last_viewed"]
            )
            if not needs_update:
                continue
            stats.notes_updated += 1
            if apply:
                conn.execute(
                    """
                    UPDATE items
                    SET "references" = ?, related_items = ?, last_viewed = ?
                    WHERE id = ? AND owner_id = ?
                    """,
                    (
                        _dumps(normalized["references"]),
                        _dumps(normalized["related_items"]),
                        normalized["last_viewed"],
                        row["id"],
                        row["owner_id"],
                    ),
                )
        if apply:
            conn.commit()
    except Exception:
        if apply:
            conn.rollback()
        raise
    finally:
        conn.close()
    return stats.to_dict()


def _is_date_text(value: Any) -> bool:
    text = str(value or "").strip()
    if len(text) != 10:
        return False
    try:
        datetime.strptime(text, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def _fetch_tasks(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    columns = _columns(conn, "items")
    wanted = [
        "id",
        "owner_id",
        "category",
        "status",
        "due_time",
        "plan_date",
        "deadline_at",
        "completed_at",
        "cancelled_at",
        "created_at",
        "updated_at",
    ]
    select_parts = [column if column in columns else f"NULL AS {column}" for column in wanted]
    rows = conn.execute(
        f"""
        SELECT {", ".join(select_parts)}
        FROM items
        WHERE type = 'task' AND COALESCE(deleted, 0) = 0
        """
    ).fetchall()
    return [dict(row) for row in rows]


def _has_meaningful_time(value: str | None) -> bool:
    if not value:
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return (parsed.hour, parsed.minute, parsed.second) not in {(0, 0, 0), (23, 59, 0)}


def _normalize_task_payload(row: dict[str, Any], stats: TaskMigrationStats) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    current_status = str(row.get("status") or "").strip()
    if current_status in {"done", "cancelled"}:
        status = current_status
    else:
        status = "open"
    if status != current_status:
        updates["status"] = status
        stats.statuses_normalized += 1

    category = str(row.get("category") or "").strip()
    current_plan = str(row.get("plan_date") or "").strip() or None
    due_time = _normalize_iso_or_none(row.get("due_time"))
    due_date = due_time[:10] if due_time else None

    plan_date = current_plan
    if not plan_date and _is_date_text(category):
        plan_date = category
        stats.plan_dates_from_category += 1
    if not plan_date and due_date:
        plan_date = due_date
        stats.plan_dates_from_due_time += 1
    if plan_date != current_plan:
        updates["plan_date"] = plan_date

    new_category = category
    if _is_date_text(category):
        new_category = "未分类"
    elif not new_category:
        new_category = "未分类"
    if new_category != category:
        updates["category"] = new_category

    current_deadline = _normalize_iso_or_none(row.get("deadline_at"))
    deadline_at = current_deadline
    if not deadline_at and due_time and _has_meaningful_time(due_time):
        deadline_at = due_time
        stats.deadlines_from_due_time += 1
    if deadline_at != current_deadline:
        updates["deadline_at"] = deadline_at

    completed_at = _normalize_iso_or_none(row.get("completed_at"))
    cancelled_at = _normalize_iso_or_none(row.get("cancelled_at"))
    fallback_close_time = completed_at or _normalize_iso_or_none(row.get("updated_at")) or _normalize_iso_or_none(row.get("created_at"))

    if status == "done":
        if not completed_at and fallback_close_time:
            updates["completed_at"] = fallback_close_time
            stats.completed_timestamps_backfilled += 1
        if cancelled_at:
            updates["cancelled_at"] = None
    elif status == "cancelled":
        if completed_at:
            updates["completed_at"] = None
            if not cancelled_at:
                updates["cancelled_at"] = completed_at
                stats.cancelled_timestamps_moved += 1
        elif not cancelled_at and fallback_close_time:
            updates["cancelled_at"] = fallback_close_time
            stats.cancelled_timestamps_moved += 1
    else:
        if completed_at:
            updates["completed_at"] = None
        if cancelled_at:
            updates["cancelled_at"] = None

    return updates


def migrate_task_fields(db_path: str | Path, *, apply: bool = False) -> dict[str, Any]:
    db_path = Path(db_path)
    _require_db_file(db_path)
    if apply:
        _ensure_runtime_schema(db_path)

    conn = _connect(db_path)
    stats = TaskMigrationStats()
    try:
        item_columns = _columns(conn, "items")
        stats.missing_schema_columns = [column for column in TASK_COLUMNS if column not in item_columns]
        tasks = _fetch_tasks(conn)
        stats.tasks_seen = len(tasks)

        if apply:
            conn.execute("BEGIN IMMEDIATE")
        now = datetime.now().isoformat(timespec="seconds")
        for row in tasks:
            updates = _normalize_task_payload(row, stats)
            if not updates:
                continue
            stats.tasks_updated += 1
            if apply:
                updates["updated_at"] = now
                set_clause = ", ".join(f'"{column}" = ?' for column in updates)
                conn.execute(
                    f'UPDATE items SET {set_clause} WHERE id = ? AND owner_id = ?',
                    list(updates.values()) + [row["id"], row["owner_id"]],
                )
        if apply:
            conn.commit()
    except Exception:
        if apply:
            conn.rollback()
        raise
    finally:
        conn.close()
    return stats.to_dict()


def _fetch_diaries(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    columns = _columns(conn, "items")
    wanted = [
        "id",
        "owner_id",
        "diary_date",
        "entry_time",
        "mood",
        "template_answers",
        "is_favorite",
        "created_at",
        "updated_at",
    ]
    select_parts = [column if column in columns else f"NULL AS {column}" for column in wanted]
    rows = conn.execute(
        f"""
        SELECT {", ".join(select_parts)}
        FROM items
        WHERE type = 'diary' AND COALESCE(deleted, 0) = 0
        """
    ).fetchall()
    return [dict(row) for row in rows]


def _normalize_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on", "是", "收藏"}


def _entry_time_fallback(row: dict[str, Any]) -> str | None:
    diary_date = str(row.get("diary_date") or "").strip()
    if not _is_date_text(diary_date):
        return None
    for key in ("created_at", "updated_at"):
        normalized = _normalize_iso_or_none(row.get(key))
        if normalized and normalized[:10] == diary_date:
            return normalized
    return f"{diary_date}T21:00:00"


def _normalize_diary_payload(row: dict[str, Any], stats: DiaryMigrationStats) -> dict[str, Any]:
    updates: dict[str, Any] = {}

    current_entry_time = _normalize_iso_or_none(row.get("entry_time"))
    entry_time = current_entry_time or _entry_time_fallback(row)
    if entry_time != current_entry_time:
        updates["entry_time"] = entry_time
        stats.entry_times_backfilled += 1

    current_mood = str(row.get("mood") or "").strip()
    if current_mood:
        try:
            mood = normalize_diary_mood(current_mood)
        except ValueError:
            mood = None
        if mood and mood != current_mood:
            updates["mood"] = mood
            stats.moods_normalized += 1
        elif mood is None:
            updates["mood"] = None
            stats.moods_cleared += 1

    current_answers = _loads(row.get("template_answers"), None)
    try:
        answers = normalize_template_answers(current_answers if current_answers is not None else [])
    except ValueError:
        answers = []
    if current_answers != answers:
        updates["template_answers"] = _dumps(answers)
        stats.template_answers_normalized += 1

    current_favorite = _normalize_bool(row.get("is_favorite"))
    raw_favorite = row.get("is_favorite")
    normalized_favorite = 1 if current_favorite else 0
    if raw_favorite not in (normalized_favorite, bool(normalized_favorite)):
        updates["is_favorite"] = normalized_favorite
        stats.favorites_normalized += 1

    return updates


def migrate_diary_fields(db_path: str | Path, *, apply: bool = False) -> dict[str, Any]:
    db_path = Path(db_path)
    _require_db_file(db_path)
    if apply:
        _ensure_runtime_schema(db_path)

    conn = _connect(db_path)
    stats = DiaryMigrationStats()
    try:
        item_columns = _columns(conn, "items")
        stats.missing_schema_columns = [column for column in DIARY_COLUMNS if column not in item_columns]
        diaries = _fetch_diaries(conn)
        stats.diaries_seen = len(diaries)

        if apply:
            conn.execute("BEGIN IMMEDIATE")
        now = datetime.now().isoformat(timespec="seconds")
        for row in diaries:
            updates = _normalize_diary_payload(row, stats)
            if not updates:
                continue
            stats.diaries_updated += 1
            if apply:
                updates["updated_at"] = now
                set_clause = ", ".join(f'"{column}" = ?' for column in updates)
                conn.execute(
                    f'UPDATE items SET {set_clause} WHERE id = ? AND owner_id = ?',
                    list(updates.values()) + [row["id"], row["owner_id"]],
                )
        if apply:
            conn.commit()
    except Exception:
        if apply:
            conn.rollback()
        raise
    finally:
        conn.close()
    return stats.to_dict()


def _fetch_ledgers(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    columns = _columns(conn, "items")
    wanted = [
        "id",
        "owner_id",
        "title",
        "amount",
        "amount_cents",
        "currency",
        "direction",
        "transaction_type",
        "ledger_category",
        "ledger_date",
        "payment_method",
        "account_name",
        "counter_account_name",
        "merchant",
        "remark",
        "created_at",
        "updated_at",
    ]
    select_parts = [column if column in columns else f"NULL AS {column}" for column in wanted]
    rows = conn.execute(
        f"""
        SELECT {", ".join(select_parts)}
        FROM items
        WHERE type = 'ledger' AND COALESCE(deleted, 0) = 0
        """
    ).fetchall()
    return [dict(row) for row in rows]


def _normalize_ledger_payload(row: dict[str, Any], stats: LedgerMigrationStats) -> dict[str, Any]:
    transaction_type = row.get("transaction_type") or row.get("direction")
    before = {
        "amount": row.get("amount"),
        "amount_cents": row.get("amount_cents"),
        "currency": row.get("currency"),
        "transaction_type": transaction_type,
        "ledger_category": row.get("ledger_category"),
        "ledger_date": row.get("ledger_date"),
        "account_name": row.get("account_name") or row.get("payment_method"),
        "counter_account_name": row.get("counter_account_name"),
        "merchant": row.get("merchant"),
        "remark": row.get("remark"),
    }
    normalized = normalize_ledger_fields(before, partial=False)

    updates: dict[str, Any] = {}
    for key in (
        "amount",
        "amount_cents",
        "currency",
        "transaction_type",
        "ledger_category",
        "ledger_date",
        "account_name",
        "counter_account_name",
        "merchant",
        "remark",
    ):
        if row.get(key) != normalized.get(key):
            updates[key] = normalized.get(key)

    if "amount_cents" in updates:
        stats.amount_cents_backfilled += 1
    if "transaction_type" in updates:
        stats.transaction_types_backfilled += 1
    if "account_name" in updates:
        stats.accounts_backfilled += 1
    if "currency" in updates:
        stats.currencies_backfilled += 1
    return updates


def migrate_ledger_fields(db_path: str | Path, *, apply: bool = False) -> dict[str, Any]:
    db_path = Path(db_path)
    _require_db_file(db_path)
    if apply:
        _ensure_runtime_schema(db_path)

    conn = _connect(db_path)
    stats = LedgerMigrationStats()
    try:
        item_columns = _columns(conn, "items")
        stats.missing_schema_columns = [column for column in LEDGER_COLUMNS if column not in item_columns]
        ledgers = _fetch_ledgers(conn)
        stats.ledgers_seen = len(ledgers)

        if apply:
            conn.execute("BEGIN IMMEDIATE")
        now = datetime.now().isoformat(timespec="seconds")
        for row in ledgers:
            updates = _normalize_ledger_payload(row, stats)
            if not updates:
                continue
            stats.ledgers_updated += 1
            if apply:
                updates["updated_at"] = now
                set_clause = ", ".join(f'"{column}" = ?' for column in updates)
                conn.execute(
                    f'UPDATE items SET {set_clause} WHERE id = ? AND owner_id = ?',
                    list(updates.values()) + [row["id"], row["owner_id"]],
                )
        if apply:
            conn.commit()
    except Exception:
        if apply:
            conn.rollback()
        raise
    finally:
        conn.close()
    return stats.to_dict()


def cleanup_legacy_event_item_columns(db_path: str | Path, *, apply: bool = False) -> dict[str, Any]:
    db_path = Path(db_path)
    _require_db_file(db_path)
    conn = _connect(db_path)
    dropped: list[str] = []
    would_drop: list[str] = []
    dropped_indexes: list[str] = []
    would_drop_indexes: list[str] = []
    skipped: list[dict[str, str]] = []
    try:
        item_columns = _columns(conn, "items")
        candidates = [column for column in LEGACY_EVENT_ITEM_COLUMNS if column in item_columns]
        indexes = _indexes_referencing_columns(conn, "items", tuple(candidates))
        if not apply:
            would_drop = candidates
            would_drop_indexes = indexes
            return {
                "dropped": dropped,
                "would_drop": would_drop,
                "dropped_indexes": dropped_indexes,
                "would_drop_indexes": would_drop_indexes,
                "skipped": skipped,
            }
        if candidates:
            conn.execute("BEGIN IMMEDIATE")
            for index in indexes:
                conn.execute(f"DROP INDEX IF EXISTS {_quote_identifier(index)}")
                dropped_indexes.append(index)
        for column in candidates:
            try:
                conn.execute(f'ALTER TABLE items DROP COLUMN "{column}"')
                dropped.append(column)
            except sqlite3.OperationalError as exc:
                skipped.append({"column": column, "reason": str(exc)})
        if skipped:
            conn.rollback()
            details = "; ".join(f"{row['column']}: {row['reason']}" for row in skipped)
            raise RuntimeError(f"Failed to drop legacy event item columns: {details}")
        conn.commit()
    except Exception:
        if apply:
            conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "dropped": dropped,
        "would_drop": would_drop,
        "dropped_indexes": dropped_indexes,
        "would_drop_indexes": would_drop_indexes,
        "skipped": skipped,
    }


def cleanup_legacy_task_item_columns(db_path: str | Path, *, apply: bool = False) -> dict[str, Any]:
    db_path = Path(db_path)
    _require_db_file(db_path)
    conn = _connect(db_path)
    dropped: list[str] = []
    would_drop: list[str] = []
    dropped_indexes: list[str] = []
    would_drop_indexes: list[str] = []
    skipped: list[dict[str, str]] = []
    try:
        item_columns = _columns(conn, "items")
        candidates = [column for column in LEGACY_TASK_ITEM_COLUMNS if column in item_columns]
        indexes = _indexes_referencing_columns(conn, "items", tuple(candidates))
        if not apply:
            would_drop = candidates
            would_drop_indexes = indexes
            return {
                "dropped": dropped,
                "would_drop": would_drop,
                "dropped_indexes": dropped_indexes,
                "would_drop_indexes": would_drop_indexes,
                "skipped": skipped,
            }
        if candidates:
            conn.execute("BEGIN IMMEDIATE")
            for index in indexes:
                conn.execute(f"DROP INDEX IF EXISTS {_quote_identifier(index)}")
                dropped_indexes.append(index)
        for column in candidates:
            try:
                conn.execute(f'ALTER TABLE items DROP COLUMN "{column}"')
                dropped.append(column)
            except sqlite3.OperationalError as exc:
                skipped.append({"column": column, "reason": str(exc)})
        if skipped:
            conn.rollback()
            details = "; ".join(f"{row['column']}: {row['reason']}" for row in skipped)
            raise RuntimeError(f"Failed to drop legacy task item columns: {details}")
        conn.commit()
    except Exception:
        if apply:
            conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "dropped": dropped,
        "would_drop": would_drop,
        "dropped_indexes": dropped_indexes,
        "would_drop_indexes": would_drop_indexes,
        "skipped": skipped,
    }


def cleanup_legacy_ledger_item_columns(db_path: str | Path, *, apply: bool = False) -> dict[str, Any]:
    db_path = Path(db_path)
    _require_db_file(db_path)
    conn = _connect(db_path)
    dropped: list[str] = []
    would_drop: list[str] = []
    dropped_indexes: list[str] = []
    would_drop_indexes: list[str] = []
    skipped: list[dict[str, str]] = []
    try:
        item_columns = _columns(conn, "items")
        candidates = [column for column in LEGACY_LEDGER_ITEM_COLUMNS if column in item_columns]
        indexes = _indexes_referencing_columns(conn, "items", tuple(candidates))
        if not apply:
            would_drop = candidates
            would_drop_indexes = indexes
            return {
                "dropped": dropped,
                "would_drop": would_drop,
                "dropped_indexes": dropped_indexes,
                "would_drop_indexes": would_drop_indexes,
                "skipped": skipped,
            }
        if candidates:
            conn.execute("BEGIN IMMEDIATE")
            for index in indexes:
                conn.execute(f"DROP INDEX IF EXISTS {_quote_identifier(index)}")
                dropped_indexes.append(index)
        for column in candidates:
            try:
                conn.execute(f'ALTER TABLE items DROP COLUMN "{column}"')
                dropped.append(column)
            except sqlite3.OperationalError as exc:
                skipped.append({"column": column, "reason": str(exc)})
        if skipped:
            conn.rollback()
            details = "; ".join(f"{row['column']}: {row['reason']}" for row in skipped)
            raise RuntimeError(f"Failed to drop legacy ledger item columns: {details}")
        conn.commit()
    except Exception:
        if apply:
            conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "dropped": dropped,
        "would_drop": would_drop,
        "dropped_indexes": dropped_indexes,
        "would_drop_indexes": would_drop_indexes,
        "skipped": skipped,
    }


def rebuild_fts_index(db_path: str | Path, *, apply: bool = False) -> dict[str, Any]:
    db_path = Path(db_path)
    _require_db_file(db_path)
    conn = _connect(db_path)
    try:
        if not _table_exists(conn, "items") or not _table_exists(conn, "items_fts"):
            return {"available": False, "rebuilt": False}
        active_count = int(
            conn.execute("SELECT COUNT(*) FROM items WHERE deleted = 0").fetchone()[0]
        )
        indexed_active_count = int(
            conn.execute(
                """
                SELECT COUNT(DISTINCT f.id)
                FROM items_fts f
                JOIN items i ON i.id = f.id
                WHERE i.deleted = 0
                """
            ).fetchone()[0]
        )
        stale_count = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM items_fts f
                LEFT JOIN items i ON i.id = f.id
                WHERE i.id IS NULL OR i.deleted != 0
                """
            ).fetchone()[0]
        )
    finally:
        conn.close()

    report = {
        "available": True,
        "active_items": active_count,
        "indexed_active_items": indexed_active_count,
        "missing_active_items": max(active_count - indexed_active_count, 0),
        "stale_rows": stale_count,
        "rebuilt": False,
    }
    if not apply:
        return report

    db = Database(str(db_path))
    try:
        rebuild_result = db.rebuild_fts_index()
    finally:
        db.cleanup()
    report.update(rebuild_result)
    report["rebuilt"] = True
    return report


def migrate_pendo_redesign(db_path: str | Path, *, apply: bool = False) -> dict[str, Any]:
    db_path = Path(db_path)
    _require_db_file(db_path)
    started_at = datetime.now().isoformat(timespec="seconds")
    backup_path: Path | None = None
    report_path: Path | None = None

    if apply:
        backup_path = db_path.with_name(
            f"{db_path.name}.pendo-redesign-backup-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        )
        shutil.copy2(db_path, backup_path)

    event_report = migrate_event_graph(
        db_path,
        apply=apply,
        create_backup=False,
        write_report=False,
    )
    note_report = migrate_note_fields(db_path, apply=apply)
    task_report = migrate_task_fields(db_path, apply=apply)
    diary_report = migrate_diary_fields(db_path, apply=apply)
    ledger_report = migrate_ledger_fields(db_path, apply=apply)
    cleanup_report = cleanup_legacy_event_item_columns(db_path, apply=apply)
    task_cleanup_report = cleanup_legacy_task_item_columns(db_path, apply=apply)
    ledger_cleanup_report = cleanup_legacy_ledger_item_columns(db_path, apply=apply)
    fts_report = rebuild_fts_index(db_path, apply=apply)

    report = {
        "mode": "apply" if apply else "dry-run",
        "db": str(db_path),
        "backup": str(backup_path) if backup_path else None,
        "started_at": started_at,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "event_graph": event_report,
        "notes": note_report,
        "tasks": task_report,
        "diaries": diary_report,
        "ledgers": ledger_report,
        "legacy_event_item_columns": cleanup_report,
        "legacy_task_item_columns": task_cleanup_report,
        "legacy_ledger_item_columns": ledger_cleanup_report,
        "fts": fts_report,
    }
    if apply:
        report_path = db_path.with_name(
            f"{db_path.name}.pendo-redesign-report-{datetime.now().strftime('%Y%m%d%H%M%S')}.json"
        )
        report["report_path"] = str(report_path)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    default_db = Path(__file__).resolve().parents[1] / "data" / "pendo.db"
    parser = argparse.ArgumentParser(
        description="Migrate a Pendo database to the event graph + note + task + diary + ledger redesign."
    )
    parser.add_argument("db", nargs="?", default=str(default_db))
    parser.add_argument("--apply", action="store_true", help="write changes after creating a backup")
    parser.add_argument("--dry-run", action="store_true", help="preview changes; this is the default")
    args = parser.parse_args()

    result = migrate_pendo_redesign(args.db, apply=bool(args.apply))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
