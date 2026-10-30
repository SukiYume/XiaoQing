"""集合编辑的事务、版本校验、共享分类和撤销快照。"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from ..core.exceptions import ItemNotFoundException, ItemVersionConflictException
from ..utils.time_utils import (
    TimezoneHelper,
    normalize_event_collection_datetimes_for_storage,
    normalize_item_datetimes_for_storage,
    require_canonical_utc_storage,
    utc_now_iso,
)

if TYPE_CHECKING:
    from .db import Database


def update_collection(
    db: Database,
    collection_id: str,
    owner_id: str,
    updates: dict[str, Any],
    *,
    child_updates: dict[str, tuple[list[str], list[dict[str, int]]]] | None = None,
    expected_version: int | None                                            = None,
    expected_child_versions: dict[str, int] | None                          = None,
    operation_log: dict[str, Any] | None                                    = None,
) -> int:
    """在持有写锁时校验整个集合，并原子提交元信息和叶子更新。"""
    clean = {
        key: value for key, value in updates.items() if key in db._EVENT_COLLECTION_UPDATE_FIELDS
    }
    pending_children = child_updates or {}
    if not clean and not pending_children:
        return 0
    changed_children: dict[str, dict[str, Any]] = {}
    user_timezone                               = TimezoneHelper.get_user_timezone(owner_id, db)
    now                                         = utc_now_iso()
    with db.transaction(immediate=True) as conn:
        cursor  = conn.cursor()
        current = db._row_to_event_collection(
            cursor.execute(
                "SELECT * FROM event_collections WHERE id = ? AND owner_id = ? AND deleted = 0",
                (collection_id, owner_id),
            ).fetchone()
        )
        if current is None:
            raise ItemNotFoundException(collection_id)
        before_version = int(current["version"])
        if expected_version is not None and before_version != expected_version:
            raise ItemVersionConflictException(collection_id)
        normalized = normalize_event_collection_datetimes_for_storage(
            {"timezone": current["timezone"], **clean, "updated_at": now},
            user_timezone,
        )
        clean      = {key: normalized[key] for key in clean}
        child_rows = cursor.execute(
            "SELECT * FROM items WHERE event_collection_id = ? AND owner_id = ? AND deleted = 0",
            (collection_id, owner_id),
        ).fetchall()
        children = {str(row["id"]): db._row_to_item(row) for row in child_rows}
        if expected_child_versions is not None:
            if children.keys() != expected_child_versions.keys() or any(
                child is None or child.version != expected_child_versions[child_id]
                for child_id, child in children.items()
            ):
                raise ItemVersionConflictException(collection_id)
        missing = pending_children.keys() - children.keys()
        if missing:
            raise ItemNotFoundException(min(missing))
        for child_id, child in children.items():
            if child is None:
                raise ItemNotFoundException(child_id)
            child_changes: dict[str, Any] = {
                key: clean[key] for key in ("category", "timezone") if key in clean
            }
            if child_id in pending_children:
                times, rules = pending_children[child_id]
                child_changes.update(remind_times=times, reminder_rules=rules)
            if not child_changes:
                continue
            old_child        = child.to_dict()
            normalized_child = normalize_item_datetimes_for_storage(
                {"type": "event", "timezone": normalized["timezone"], **child_changes},
                user_timezone,
            )
            require_canonical_utc_storage(normalized_child)
            child_changes              = {key: normalized_child[key] for key in child_changes}
            changed_children[child_id] = {
                "old_values"    : {key: old_child.get(key) for key in child_changes},
                "before_version": child.version,
                "after_version" : child.version + 1,
            }
            data        = db._prepare_data({**child_changes, "updated_at": now})
            assignments = ", ".join(f"{db._quote_col(key)} = ?" for key in data)
            cursor.execute(
                f"UPDATE items SET {assignments}, version = version + 1 WHERE id = ? AND owner_id = ?",
                [*data.values(), child_id, owner_id],
            )
            if "remind_times" in child_changes:
                db._sync_reminder_logs(cursor, child_id, child_changes["remind_times"])
            if db._FTS_FIELDS & child_changes.keys():
                db._refresh_fts(child_id, conn)
        data        = db._prepare_event_collection_data({**clean, "updated_at": now})
        assignments = ", ".join(f"{db._quote_col(key)} = ?" for key in data)
        cursor.execute(
            f"UPDATE event_collections SET {assignments}, version = version + 1 WHERE id = ? AND owner_id = ?",
            [*data.values(), collection_id, owner_id],
        )
        after_version = int(
            cursor.execute(
                "SELECT version FROM event_collections WHERE id = ?",
                (collection_id,),
            ).fetchone()[0]
        )
        log = dict(operation_log or {"user_id": owner_id, "action": "edit_event_collection"})
        log["details"] = {
            **(log.get("details") or {}),
            "target_kind"   : "event_collection",
            "updates"       : clean,
            "old_values"    : {key: current.get(key) for key in clean},
            "before_version": before_version,
            "after_version" : after_version,
            "children"      : changed_children,
        }
        db._log_operation_from_spec(
            cursor,
            log,
            default_item_id   = collection_id,
            default_item_type = "event",
        )
    for child_id in changed_children:
        db.cache_invalidate(child_id)
    db.cache_invalidate(f"items|{owner_id}")
    db.cache_invalidate(collection_id)
    return len(pending_children)


def restore_collection_edit(
    db: Database,
    owner_id: str,
    collection_id: str,
    details: dict[str, Any],
    log_id: int,
    source_action: str,
) -> dict[str, Any]:
    """校验集合及受影响叶子的版本后恢复同一事务中的编辑快照。"""
    snapshots = details.get("children") or {}
    now       = utc_now_iso()
    with db.transaction(immediate=True) as conn:
        cursor = conn.cursor()
        row    = cursor.execute(
            "SELECT version FROM event_collections WHERE id = ? AND owner_id = ? AND deleted = 0",
            (collection_id, owner_id),
        ).fetchone()
        if row is None:
            return {"status": "error", "message": "该集合已不存在"}
        if int(row["version"]) != details.get("after_version"):
            return {"status": "error", "message": "集合已有后续修改，请先处理最新操作"}
        for child_id, snapshot in snapshots.items():
            child = cursor.execute(
                "SELECT version FROM items WHERE id = ? AND owner_id = ? AND deleted = 0",
                (child_id, owner_id),
            ).fetchone()
            if child is None or int(child["version"]) != snapshot["after_version"]:
                return {"status": "error", "message": "集合节点已有后续修改，请先处理最新操作"}
        for child_id, snapshot in snapshots.items():
            data        = db._prepare_data({**snapshot["old_values"], "updated_at": now})
            assignments = ", ".join(f"{db._quote_col(key)} = ?" for key in data)
            cursor.execute(
                f"UPDATE items SET {assignments}, version = version + 1 WHERE id = ? AND owner_id = ?",
                [*data.values(), child_id, owner_id],
            )
            if "remind_times" in snapshot["old_values"]:
                db._sync_reminder_logs(cursor, child_id, snapshot["old_values"]["remind_times"])
            db._refresh_fts(child_id, conn)
        data = db._prepare_event_collection_data(
            {**details.get("old_values", {}), "updated_at": now}
        )
        assignments = ", ".join(f"{db._quote_col(key)} = ?" for key in data)
        cursor.execute(
            f"UPDATE event_collections SET {assignments}, version = version + 1 WHERE id = ? AND owner_id = ?",
            [*data.values(), collection_id, owner_id],
        )
        new_version = int(
            cursor.execute(
                "SELECT version FROM event_collections WHERE id = ?",
                (collection_id,),
            ).fetchone()[0]
        )
        # 连续撤销沿已验证的前驱版本衔接；独立写入形成版本断点。
        previous = cursor.execute(
            "SELECT id, details FROM operation_logs WHERE user_id = ? AND item_id = ? "
            "AND id < ? AND undone_at IS NULL AND (action LIKE 'edit_%' OR action = 'update_event_collection') "
            "ORDER BY id DESC LIMIT 1",
            (owner_id, collection_id, log_id),
        ).fetchone()
        if previous:
            previous_details = db._parse_operation_details(previous["details"])
            if previous_details.get("after_version") == details.get("before_version"):
                previous_details["after_version"] = new_version
                for child_id, snapshot in previous_details.get("children", {}).items():
                    restored = snapshots.get(child_id)
                    if restored and snapshot["after_version"] == restored["before_version"]:
                        snapshot["after_version"] = restored["after_version"] + 1
                cursor.execute(
                    "UPDATE operation_logs SET details = ? WHERE id = ?",
                    (json.dumps(previous_details, ensure_ascii=False), previous["id"]),
                )
        db._mark_operation_logs_undone(
            cursor,
            owner_id          = owner_id,
            log_ids           = [log_id],
            source_action     = source_action,
            item_type         = "event",
            item_id           = collection_id,
            restored_item_ids = list(snapshots),
        )
    for child_id in snapshots:
        db.cache_invalidate(child_id)
    db.cache_invalidate(collection_id)
    db.cache_invalidate(f"items|{owner_id}")
    return {
        "status"        : "success",
        "item_id"       : collection_id,
        "action"        : source_action,
        "affected"      : 1 + len(snapshots),
        "instance_count": len(snapshots),
    }
