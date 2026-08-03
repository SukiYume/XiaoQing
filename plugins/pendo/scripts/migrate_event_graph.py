"""把旧版 Pendo 日程迁移为集合与叶子日程组成的事件图。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final, TypeAlias, cast

from ..services.db import Database
from ..utils.validators import (
    DEFAULT_EVENT_TIMEZONE,
    build_remind_times_from_rules,
    derive_reminder_rules,
    normalize_reminder_rules,
    with_start_time_reminder_rule,
)
from .migration_utils import (
    backup_sqlite_database,
)
from .migration_utils import (
    connect_sqlite_database as _connect,
)
from .migration_utils import (
    dump_json_field as _dumps,
)
from .migration_utils import (
    load_json_field as _loads,
)
from .migration_utils import (
    normalize_iso_seconds as _normalize_iso,
)
from .migration_utils import (
    table_columns as _columns,
)
from .migration_utils import (
    table_exists as _table_exists,
)

_JsonObject: TypeAlias = dict[str, Any]
_ReminderRule: TypeAlias = dict[str, int]

_ITEM_COLUMNS: Final = (
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
)


@dataclass(slots=True)
class _MigrationStats:
    """记录一次事件图迁移实际会执行或已执行的变更数量。"""

    single_events_updated: int = 0
    multi_node_collections_created: int = 0
    multi_node_child_events_created: int = 0
    multi_node_source_events_deleted: int = 0
    reminder_logs_moved: int = 0
    recurring_collections_created: int = 0
    recurring_occurrences_updated: int = 0


@dataclass(frozen=True, slots=True)
class _Milestone:
    """已清洗且可按统一墙钟时间比较的旧多节点记录。"""

    name: str
    start_time: str
    moment: datetime
    end_time: str | None
    notes: str


@dataclass(frozen=True, slots=True)
class _CollectionSpec:
    """创建事件集合时由迁移分支提供的差异字段。"""

    collection_id: str
    kind: str
    start_time: str | None
    end_time: str | None
    reminder_rules: list[_ReminderRule]
    rrule: str | None = None


@dataclass(slots=True)
class _MigrationContext:
    """缓存一次迁移共享的事务、模式信息与统计对象。"""

    cursor: sqlite3.Cursor
    apply: bool
    stats: _MigrationStats
    item_columns: set[str]
    has_event_collections: bool
    has_items_fts: bool
    has_reminder_logs: bool


def _fetch_events(
    conn: sqlite3.Connection,
    item_columns: set[str],
) -> list[_JsonObject]:
    """按兼容列集读取仍然有效的旧日程。"""

    if not item_columns:
        raise ValueError("数据库缺少 items 表，无法执行事件图迁移")
    missing_required = {"id", "type"} - item_columns
    if missing_required:
        missing_text = ", ".join(sorted(missing_required))
        raise ValueError(f"items 表缺少事件迁移必需列: {missing_text}")

    select_parts = [
        f'"{column}"' if column in item_columns else f'NULL AS "{column}"'
        for column in _ITEM_COLUMNS
    ]
    active_filter = "COALESCE(deleted, 0) = 0" if "deleted" in item_columns else "1 = 1"
    rows = conn.execute(
        f"""
        SELECT {", ".join(select_parts)}
        FROM items
        WHERE type = 'event' AND {active_filter}
        """
    )
    return [dict(row) for row in rows]


def _parse_wall_datetime(value: Any) -> datetime | None:
    """把带偏移和无偏移旧时间统一为默认时区的无偏移墙钟时间。"""

    normalized = _normalize_iso(value)
    if not normalized:
        return None
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(DEFAULT_EVENT_TIMEZONE).replace(tzinfo=None)
    return parsed


def _list_value(row: _JsonObject, key: str) -> list[Any]:
    """读取可能是 JSON 文本或列表的旧字段。"""

    loaded = _loads(row.get(key), [])
    return loaded if isinstance(loaded, list) else []


def _milestones(row: _JsonObject) -> list[_Milestone]:
    """清洗旧里程碑，并按同一墙钟语义排序。"""

    rows: list[_Milestone] = []
    for milestone in _list_value(row, "milestones"):
        if not isinstance(milestone, dict):
            continue
        name = str(milestone.get("name") or "").strip()
        start_time = _normalize_iso(milestone.get("time"))
        moment = _parse_wall_datetime(start_time)
        if not name or not start_time or moment is None:
            continue
        rows.append(
            _Milestone(
                name=name,
                start_time=start_time,
                moment=moment,
                end_time=_normalize_iso(milestone.get("end_time")),
                notes=str(milestone.get("notes") or ""),
            )
        )
    rows.sort(key=lambda item: item.moment)
    return rows


def _normalize_rules_for_start(
    start_time: str | None,
    reminder_rules: Any,
    remind_times: list[Any],
) -> list[_ReminderRule]:
    """优先清洗相对规则，必要时从旧绝对提醒时间反推。"""

    loaded_rules = _loads(reminder_rules, [])
    try:
        normalized_rules = cast(list[_ReminderRule], normalize_reminder_rules(loaded_rules))
    except ValueError:
        normalized_rules = []
    if normalized_rules:
        return normalized_rules
    if start_time and remind_times:
        try:
            return cast(
                list[_ReminderRule],
                with_start_time_reminder_rule(derive_reminder_rules(start_time, remind_times)),
            )
        except ValueError:
            return [{"offset_seconds": 0}]
    return [{"offset_seconds": 0}] if start_time else []


def _remind_times_for_rules(
    start_time: str | None,
    reminder_rules: list[_ReminderRule],
) -> list[str]:
    """根据清洗后的相对规则重建绝对提醒时间。"""

    if not start_time:
        return []
    try:
        return cast(list[str], build_remind_times_from_rules(start_time, reminder_rules))
    except ValueError:
        return []


def _single_updates(row: _JsonObject) -> _JsonObject:
    """计算单次日程需要补齐的事件角色与提醒字段。"""

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
        "reminder_rules": _dumps(rules),
        "remind_times": _dumps(remind_times),
    }
    current = {
        "event_role": row.get("event_role") or None,
        "event_collection_id": row.get("event_collection_id") or None,
        "event_collection_kind": row.get("event_collection_kind") or None,
        "event_index": row.get("event_index"),
        "event_node_key": row.get("event_node_key") or None,
        "reminder_rules": _dumps(_loads(row.get("reminder_rules"), [])),
        "remind_times": _dumps(_list_value(row, "remind_times")),
    }
    return {key: value for key, value in updates.items() if current.get(key) != value}


def _collection_exists(
    context: _MigrationContext,
    source: _JsonObject,
    spec: _CollectionSpec,
) -> bool:
    """确认同 ID 集合确实来自当前旧日程，防止碰撞时串接数据。"""

    if not context.has_event_collections:
        return False
    row = context.cursor.execute(
        """
        SELECT owner_id, kind, source_item_id, deleted
        FROM event_collections
        WHERE id = ?
        """,
        (spec.collection_id,),
    ).fetchone()
    if row is None:
        return False

    expected = (
        str(source.get("owner_id") or ""),
        spec.kind,
        str(source.get("id") or ""),
    )
    actual = (
        str(row["owner_id"] or ""),
        str(row["kind"] or ""),
        str(row["source_item_id"] or ""),
    )
    if row["deleted"] or actual != expected:
        raise ValueError(f"事件集合 ID 冲突，迁移已停止: {spec.collection_id}")
    return True


def _multi_node_child_exists(
    context: _MigrationContext,
    child_id: str,
    source_id: str,
) -> bool:
    """确认已有子 ID 是同一旧日程的迁移产物。"""

    fields = ("type", "event_role", "event_collection_id", "source_item_id", "deleted")
    select_parts = [
        f'"{field}"' if field in context.item_columns else f'NULL AS "{field}"' for field in fields
    ]
    row = context.cursor.execute(
        f"SELECT {', '.join(select_parts)} FROM items WHERE id = ?",
        (child_id,),
    ).fetchone()
    if row is None:
        return False
    expected = ("event", "multi_node_child", source_id, source_id, 0)
    actual = (
        str(row["type"] or ""),
        str(row["event_role"] or ""),
        str(row["event_collection_id"] or ""),
        str(row["source_item_id"] or ""),
        int(row["deleted"] or 0),
    )
    if actual != expected:
        raise ValueError(f"多节点子日程 ID 冲突，迁移已停止: {child_id}")
    return True


def _event_collection_payload(
    row: _JsonObject,
    spec: _CollectionSpec,
) -> _JsonObject:
    """把旧日程公共字段与集合差异字段合成为插入负载。"""

    now = datetime.now().isoformat(timespec="seconds")
    return {
        "id": spec.collection_id,
        "owner_id": row.get("owner_id") or "",
        "kind": spec.kind,
        "title": row.get("title") or "无标题日程",
        "content": row.get("content") or "",
        "category": row.get("category") or "未分类",
        "location": row.get("location") or "",
        "tags": row.get("tags") or "[]",
        "notes": row.get("notes") or "",
        "context": row.get("context") or "{}",
        "visibility": row.get("visibility") or "private",
        "timezone": row.get("timezone") or str(DEFAULT_EVENT_TIMEZONE),
        "rrule": spec.rrule,
        "reminder_rules": _dumps(spec.reminder_rules),
        "start_time": spec.start_time,
        "end_time": spec.end_time,
        "source_item_id": row.get("id"),
        "created_at": row.get("created_at") or now,
        "updated_at": now,
        "deleted": 0,
        "deleted_at": None,
    }


def _insert_collection(cursor: sqlite3.Cursor, payload: _JsonObject) -> None:
    """插入字段集合固定且已由本模块构造的事件集合。"""

    columns = list(payload.keys())
    placeholders = ", ".join("?" for _ in columns)
    quoted = ", ".join(f'"{column}"' for column in columns)
    cursor.execute(
        f"INSERT INTO event_collections ({quoted}) VALUES ({placeholders})",
        [payload[column] for column in columns],
    )


def _base_child_payload(row: _JsonObject, child_id: str) -> _JsonObject:
    """复制旧日程公共字段，并清除不属于叶子日程的旧结构字段。"""

    now = datetime.now().isoformat(timespec="seconds")
    payload = {column: row.get(column) for column in _ITEM_COLUMNS}
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


def _insert_item(context: _MigrationContext, payload: _JsonObject) -> None:
    """按迁移开始时缓存的现有列插入叶子日程。"""

    columns = [column for column in payload if column in context.item_columns]
    placeholders = ", ".join("?" for _ in columns)
    quoted = ", ".join(f'"{column}"' for column in columns)
    context.cursor.execute(
        f"INSERT INTO items ({quoted}) VALUES ({placeholders})",
        [payload[column] for column in columns],
    )
    if context.has_items_fts:
        context.cursor.execute(
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


def _update_item(context: _MigrationContext, item_id: str, updates: _JsonObject) -> None:
    """只更新当前 schema 仍存在的日程列。"""

    if not updates:
        return
    updates = dict(updates)
    updates["updated_at"] = datetime.now().isoformat(timespec="seconds")
    columns = [column for column in updates if column in context.item_columns]
    if not columns:
        return
    set_clause = ", ".join(f'"{column}" = ?' for column in columns)
    context.cursor.execute(
        f"UPDATE items SET {set_clause} WHERE id = ?",
        [updates[column] for column in columns] + [item_id],
    )


def _sync_unsent_reminder_logs(
    context: _MigrationContext,
    item_id: str,
    remind_times: list[str],
) -> None:
    """删除已不属于新规则、且尚未发送的旧提醒日志。"""

    if not context.has_reminder_logs:
        return
    if not remind_times:
        context.cursor.execute(
            "DELETE FROM reminder_logs WHERE item_id = ? AND sent_at IS NULL",
            (item_id,),
        )
        return
    placeholders = ", ".join("?" for _ in remind_times)
    context.cursor.execute(
        f"""
        DELETE FROM reminder_logs
        WHERE item_id = ? AND sent_at IS NULL
        AND remind_time NOT IN ({placeholders})
        """,
        [item_id, *remind_times],
    )


def _assignment_index(remind_time: str, milestones: list[_Milestone]) -> int:
    """把旧提醒分配给其后的最近节点；若已过全部节点则取绝对最近值。"""

    remind_dt = _parse_wall_datetime(remind_time)
    if remind_dt is None:
        return 0
    following = [
        (idx, milestone.moment - remind_dt)
        for idx, milestone in enumerate(milestones)
        if milestone.moment >= remind_dt
    ]
    if following:
        return min(following, key=lambda row: row[1])[0]
    distances = [
        (idx, abs((milestone.moment - remind_dt).total_seconds()))
        for idx, milestone in enumerate(milestones)
    ]
    return min(distances, key=lambda row: row[1])[0]


def _move_reminder_logs(
    context: _MigrationContext,
    source_item_id: str,
    milestones: list[_Milestone],
) -> int:
    """把源日程的全部提醒日志合并到对应叶子日程。"""

    if not context.has_reminder_logs:
        return 0
    rows = context.cursor.execute(
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
        context.cursor.execute(
            """
            INSERT INTO reminder_logs
                (item_id, remind_time, sent_at, confirmed_at, user_action, repeat_count, last_sent_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(item_id, remind_time) DO UPDATE SET
                sent_at = COALESCE(excluded.sent_at, reminder_logs.sent_at),
                confirmed_at = COALESCE(excluded.confirmed_at, reminder_logs.confirmed_at),
                user_action = COALESCE(excluded.user_action, reminder_logs.user_action),
                repeat_count = MAX(
                    excluded.repeat_count,
                    COALESCE(reminder_logs.repeat_count, 0)
                ),
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
    context.cursor.execute("DELETE FROM reminder_logs WHERE item_id = ?", (source_item_id,))
    return len(rows)


def _finish_multi_node_source(
    context: _MigrationContext,
    source_id: str,
    milestones: list[_Milestone],
) -> None:
    """迁移完叶子后转移日志，并软删除旧多节点源日程。"""

    context.stats.multi_node_source_events_deleted += 1
    if not context.apply:
        if context.has_reminder_logs:
            context.stats.reminder_logs_moved += context.cursor.execute(
                "SELECT COUNT(*) FROM reminder_logs WHERE item_id = ?",
                (source_id,),
            ).fetchone()[0]
        return

    context.stats.reminder_logs_moved += _move_reminder_logs(context, source_id, milestones)
    now = datetime.now().isoformat(timespec="seconds")
    context.cursor.execute(
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
    if context.has_items_fts:
        context.cursor.execute("DELETE FROM items_fts WHERE id = ?", (source_id,))


def _migrate_multi_node(
    context: _MigrationContext,
    row: _JsonObject,
    milestones: list[_Milestone],
) -> None:
    """把一个旧多节点日程拆成集合、叶子日程与迁移后的提醒日志。"""

    source_id = str(row["id"])
    collection_spec = _CollectionSpec(
        collection_id=source_id,
        kind="multi_node",
        start_time=milestones[0].start_time,
        end_time=milestones[-1].start_time,
        reminder_rules=[],
    )
    old_remind_times = [
        normalized
        for value in _list_value(row, "remind_times")
        if (normalized := _normalize_iso(value))
    ]
    assigned: dict[int, list[str]] = {idx: [] for idx in range(len(milestones))}
    for remind_time in old_remind_times:
        assigned[_assignment_index(remind_time, milestones)].append(remind_time)

    if not _collection_exists(context, row, collection_spec):
        context.stats.multi_node_collections_created += 1
        if context.apply:
            _insert_collection(
                context.cursor,
                _event_collection_payload(row, collection_spec),
            )

    for idx, milestone in enumerate(milestones):
        child_id = f"{source_id}_m{idx + 1:02d}"
        if _multi_node_child_exists(context, child_id, source_id):
            continue
        context.stats.multi_node_child_events_created += 1
        child_start = milestone.start_time
        child_rules = _normalize_rules_for_start(
            child_start,
            None,
            assigned.get(idx, []),
        )
        child_remind_times = _remind_times_for_rules(child_start, child_rules)
        if not context.apply:
            continue
        child = _base_child_payload(row, child_id)
        child.update(
            {
                "title": milestone.name,
                "start_time": child_start,
                "end_time": milestone.end_time,
                "notes": milestone.notes,
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
        _insert_item(context, child)
        _sync_unsent_reminder_logs(context, child_id, child_remind_times)

    _finish_multi_node_source(context, source_id, milestones)


def _recurring_groups(events: list[_JsonObject]) -> dict[str, list[_JsonObject]]:
    """按旧 parent_id 收集尚未迁移的重复日程实例。"""

    groups: dict[str, list[_JsonObject]] = {}
    for row in events:
        parent_id = str(row.get("parent_id") or "").strip()
        if not parent_id:
            continue
        if row.get("event_collection_id") or row.get("event_role") == "recurring_occurrence":
            continue
        groups.setdefault(parent_id, []).append(row)
    for rows in groups.values():
        rows.sort(
            key=lambda item: (
                _parse_wall_datetime(item.get("start_time")) or datetime.max,
                str(item.get("id") or ""),
            )
        )
    return groups


def _migrate_recurring_group(
    context: _MigrationContext,
    collection_id: str,
    rows: list[_JsonObject],
) -> None:
    """把同一 parent_id 的旧实例归入一个重复日程集合。"""

    if not rows:
        return
    first = rows[0]
    start_time = _normalize_iso(first.get("start_time"))
    end_time = _normalize_iso(rows[-1].get("end_time")) or _normalize_iso(
        rows[-1].get("start_time")
    )
    first_rules = _normalize_rules_for_start(
        start_time,
        first.get("reminder_rules"),
        _list_value(first, "remind_times"),
    )
    collection_spec = _CollectionSpec(
        collection_id=collection_id,
        kind="recurring",
        start_time=start_time,
        end_time=end_time,
        rrule=next(
            (str(row["rrule"]) for row in rows if row.get("rrule")),
            None,
        ),
        reminder_rules=first_rules,
    )
    if not _collection_exists(context, first, collection_spec):
        context.stats.recurring_collections_created += 1
        if context.apply:
            _insert_collection(
                context.cursor,
                _event_collection_payload(first, collection_spec),
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
        parsed_start = _parse_wall_datetime(start)
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
        needs_update = any(
            str(row.get(key) or "") != str(value or "") for key, value in updates.items()
        )
        if not needs_update:
            continue
        context.stats.recurring_occurrences_updated += 1
        if context.apply:
            _update_item(context, str(row["id"]), updates)
            _sync_unsent_reminder_logs(context, str(row["id"]), remind_times)


def _migrate_event_row(
    context: _MigrationContext,
    row: _JsonObject,
    legacy_multi: dict[str, list[_Milestone]],
    recurring_parent_ids: set[str],
) -> None:
    """按旧记录形态分发多节点、重复实例或单次日程迁移。"""

    item_id = str(row["id"])
    milestones = legacy_multi.get(item_id)
    if milestones is not None:
        _migrate_multi_node(context, row, milestones)
        return
    if str(row.get("parent_id") or "").strip() in recurring_parent_ids:
        return
    if row.get("event_collection_id") or row.get("event_role") in {
        "multi_node_child",
        "recurring_occurrence",
    }:
        return

    updates = _single_updates(row)
    if not updates:
        return
    context.stats.single_events_updated += 1
    if not context.apply:
        return

    _update_item(context, item_id, updates)
    remind_times = _loads(updates.get("remind_times"), [])
    _sync_unsent_reminder_logs(
        context,
        item_id,
        cast(list[str], remind_times) if isinstance(remind_times, list) else [],
    )


def _migrate_connection(conn: sqlite3.Connection, *, apply: bool) -> _MigrationStats:
    """在一个连接内统计或事务性执行全部事件图变更。"""

    stats = _MigrationStats()
    conn.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
    try:
        item_columns = _columns(conn, "items")
        events = _fetch_events(conn, item_columns)
        legacy_multi = {str(row["id"]): _milestones(row) for row in events}
        legacy_multi = {item_id: rows for item_id, rows in legacy_multi.items() if len(rows) >= 2}
        recurring_groups = _recurring_groups(events)
        recurring_parent_ids = set(recurring_groups)
        context = _MigrationContext(
            cursor=conn.cursor(),
            apply=apply,
            stats=stats,
            item_columns=item_columns,
            has_event_collections=_table_exists(conn, "event_collections"),
            has_items_fts=_table_exists(conn, "items_fts"),
            has_reminder_logs=_table_exists(conn, "reminder_logs"),
        )

        for row in events:
            _migrate_event_row(context, row, legacy_multi, recurring_parent_ids)

        for parent_id, rows in recurring_groups.items():
            _migrate_recurring_group(context, parent_id, rows)

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return stats


def _artifact_path(db_path: Path, kind: str, suffix: str = "") -> Path:
    """生成不会在同一秒重复执行时互相覆盖的迁移产物路径。"""

    timestamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
    return db_path.with_name(f"{db_path.name}.event-graph-{kind}-{timestamp}{suffix}")


def migrate_event_graph(
    db_path: str | Path,
    *,
    apply: bool = False,
    create_backup: bool = True,
    write_report: bool = True,
) -> _JsonObject:
    """预览或应用事件图迁移；应用模式默认先建立一致性备份。"""

    db_path = Path(db_path)
    if not db_path.is_file():
        raise FileNotFoundError(db_path)

    started_at = datetime.now().isoformat(timespec="seconds")
    backup_path: Path | None = None
    if apply:
        if create_backup:
            backup_path = _artifact_path(db_path, "backup")
            backup_sqlite_database(db_path, backup_path)
        # 初始化当前 schema 后立即关闭服务对象，实际数据迁移使用下方独立事务。
        Database(str(db_path)).cleanup()

    conn = _connect(db_path)
    try:
        stats = _migrate_connection(conn, apply=apply)
    finally:
        conn.close()

    report = {
        "mode": "apply" if apply else "dry-run",
        "db": str(db_path),
        "backup": str(backup_path) if backup_path else None,
        "started_at": started_at,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        **asdict(stats),
    }
    if apply and write_report:
        report_path = _artifact_path(db_path, "report", ".json")
        report["report_path"] = str(report_path)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    """解析命令行参数、运行迁移并输出 JSON 报告。"""

    default_db = Path(__file__).resolve().parents[3] / "data" / "pendo" / "pendo.db"
    parser = argparse.ArgumentParser(description="把旧版 Pendo 日程迁移为事件集合和叶子日程。")
    parser.add_argument("db", nargs="?", default=str(default_db))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--apply",
        dest="apply",
        action="store_true",
        help="建立备份后写入迁移结果",
    )
    mode.add_argument(
        "--dry-run",
        dest="apply",
        action="store_false",
        help="只预览变更（默认模式）",
    )
    parser.set_defaults(apply=False)
    args = parser.parse_args()

    result = migrate_event_graph(args.db, apply=args.apply)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
