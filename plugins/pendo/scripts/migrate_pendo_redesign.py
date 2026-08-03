"""在工作副本中把旧 Pendo 数据库迁移到当前完整数据模型。"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Generic, Protocol, TypeAlias, TypeVar, cast

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from plugins.pendo.scripts import migration_utils as _migration_utils
    from plugins.pendo.scripts.migrate_event_graph import migrate_event_graph
    from plugins.pendo.services.db import Database
    from plugins.pendo.utils.validators import (
        normalize_bool_flag,
        normalize_diary_mood,
        normalize_ledger_fields,
        normalize_template_answers,
    )
else:
    from ..services.db import Database
    from ..utils.validators import (
        normalize_bool_flag,
        normalize_diary_mood,
        normalize_ledger_fields,
        normalize_template_answers,
    )
    from . import migration_utils as _migration_utils
    from .migrate_event_graph import migrate_event_graph

backup_sqlite_database = _migration_utils.backup_sqlite_database
_connect = _migration_utils.connect_sqlite_database
_dumps = _migration_utils.dump_json_field
_loads = _migration_utils.load_json_field
_columns = _migration_utils.table_columns
_table_exists = _migration_utils.table_exists
_normalize_iso_or_none = _migration_utils.normalize_iso_seconds

_JsonObject: TypeAlias = dict[str, Any]


class _NoteableException(Protocol):
    """Python 3.11+ 异常备注接口。"""

    def add_note(self, note: str) -> None: ...


def _add_exception_note(error: BaseException, note: str) -> None:
    """在保留原异常类型的前提下附加清理或日志失败说明。"""

    cast(_NoteableException, error).add_note(note)


def _acquire_migration_lock(db_path: Path) -> Path:
    """原子创建迁移锁，并在锁内容写入失败时撤销残留文件。"""

    lock_path = db_path.with_name(f"{db_path.name}.pendo-redesign.lock")
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise RuntimeError(f"已有 Pendo 迁移正在进行: {lock_path}") from exc

    handle = None
    try:
        handle = os.fdopen(fd, "w", encoding="utf-8")
        with handle:
            json.dump(
                {
                    "pid": os.getpid(),
                    "db": str(db_path),
                    "started_at": datetime.now().isoformat(timespec="seconds"),
                },
                handle,
            )
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        if handle is None:
            os.close(fd)
        lock_path.unlink(missing_ok=True)
        raise
    return lock_path


def _validate_database_file(db_path: Path) -> None:
    """确认迁移产物通过 SQLite 完整性和外键检查。"""

    if not db_path.is_file():
        raise FileNotFoundError(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0]).lower()
        foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        conn.close()
    if integrity != "ok" or foreign_keys:
        raise RuntimeError(
            f"迁移数据库校验失败: integrity={integrity}, foreign_keys={len(foreign_keys)}"
        )


def _write_json_file(path: Path, payload: _JsonObject, *, indent: int | None = None) -> None:
    """同目录写入并原子替换 JSON，避免中断留下半份迁移状态。"""

    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=indent)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _artifact_path(db_path: Path, kind: str, suffix: str = "") -> Path:
    """生成不会被同秒重复迁移覆盖的备份、工作副本或报告路径。"""

    timestamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
    return db_path.with_name(f"{db_path.name}.pendo-redesign-{kind}-{timestamp}{suffix}")


@dataclass(slots=True)
class _RedesignWorkspace:
    """管理完整迁移的锁、备份、工作副本和持久化状态。"""

    db_path: Path
    apply: bool
    backup_path: Path | None = None
    work_path: Path | None = None
    lock_path: Path | None = None
    backup_validated: bool = False
    database_replaced: bool = False
    journal_warning: str | None = None

    @property
    def journal_path(self) -> Path:
        return self.db_path.with_name(f"{self.db_path.name}.pendo-redesign-journal.json")

    def prepare(self) -> Path:
        """建立应用模式工作副本；预览模式直接返回原库。"""

        if not self.apply:
            return self.db_path
        self.lock_path = _acquire_migration_lock(self.db_path)
        self.backup_path = _artifact_path(self.db_path, "backup")
        backup_sqlite_database(self.db_path, self.backup_path)
        _validate_database_file(self.backup_path)
        self.backup_validated = True
        self.work_path = _artifact_path(self.db_path, "working")
        shutil.copy2(self.backup_path, self.work_path)
        _write_json_file(
            self.journal_path,
            {
                "state": "running",
                "backup": str(self.backup_path),
                "working_copy": str(self.work_path),
            },
        )
        return self.work_path

    def commit(self) -> None:
        """校验工作副本并原子替换原库，再记录完成状态。"""

        if not self.apply:
            return
        if self.work_path is None:
            raise RuntimeError("迁移工作副本缺失，拒绝替换原数据库")
        _validate_database_file(self.work_path)
        os.replace(self.work_path, self.db_path)
        self.work_path = None
        self.database_replaced = True
        try:
            _write_json_file(
                self.journal_path,
                {"state": "completed", "backup": str(self.backup_path)},
            )
        except OSError as exc:
            self.journal_warning = f"数据库已迁移，但完成日志写入失败: {exc}"

    def record_failure(self, error: Exception) -> None:
        """原库尚未替换时记录失败，同时保留最初异常。"""

        if not self.apply or self.lock_path is None or self.database_replaced:
            return
        try:
            _write_json_file(
                self.journal_path,
                {
                    "state": "failed",
                    "backup": str(self.backup_path) if self.backup_validated else None,
                    "error": str(error),
                },
            )
        except OSError as journal_error:
            _add_exception_note(error, f"迁移失败日志也无法写入: {journal_error}")

    def cleanup(self, active_error: BaseException | None) -> None:
        """始终尝试清理工作副本和锁，且不掩盖原始迁移异常。"""

        cleanup_errors: list[OSError] = []
        invalid_backup = self.backup_path if not self.backup_validated else None
        for path in (self.work_path, invalid_backup, self.lock_path):
            if path is None:
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError as cleanup_error:
                cleanup_errors.append(cleanup_error)
        if not cleanup_errors:
            return
        details = "; ".join(str(error) for error in cleanup_errors)
        if active_error is not None:
            _add_exception_note(active_error, f"迁移临时文件清理失败: {details}")
            return
        raise RuntimeError(f"迁移已结束，但临时文件清理失败: {details}")


_LEGACY_EVENT_ITEM_COLUMNS: Final = ("rrule", "parent_id", "remind_policy_id", "milestones")
_LEGACY_TASK_ITEM_COLUMNS: Final = (
    "due_time",
    "estimate",
    "subtasks",
    "dependencies",
    "progress",
)
_LEGACY_LEDGER_ITEM_COLUMNS: Final = ("direction", "payment_method")
_NOTE_COLUMNS: Final = ("references", "related_items", "last_viewed")
_TASK_COLUMNS: Final = ("plan_date", "deadline_at", "repeat_rule", "cancelled_at")
_DIARY_COLUMNS: Final = ("entry_time", "template_answers", "is_favorite")
_LEDGER_COLUMNS: Final = (
    "amount_cents",
    "currency",
    "transaction_type",
    "account_name",
    "counter_account_name",
    "merchant",
)


@dataclass(slots=True)
class _ItemMigrationStats:
    """四类条目字段迁移共享的计数与 schema 缺口。"""

    seen: int = 0
    updated: int = 0
    missing_schema_columns: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _NoteMigrationStats(_ItemMigrationStats):
    references_enriched: int = 0
    references_added_from_related_items: int = 0


@dataclass(slots=True)
class _TaskMigrationStats(_ItemMigrationStats):
    statuses_normalized: int = 0
    plan_dates_from_category: int = 0
    plan_dates_from_due_time: int = 0
    deadlines_from_due_time: int = 0
    cancelled_timestamps_moved: int = 0
    completed_timestamps_backfilled: int = 0


@dataclass(slots=True)
class _DiaryMigrationStats(_ItemMigrationStats):
    entry_times_backfilled: int = 0
    moods_normalized: int = 0
    moods_cleared: int = 0
    template_answers_normalized: int = 0
    favorites_normalized: int = 0


@dataclass(slots=True)
class _LedgerMigrationStats(_ItemMigrationStats):
    amount_cents_backfilled: int = 0
    transaction_types_backfilled: int = 0
    accounts_backfilled: int = 0
    currencies_backfilled: int = 0


_StatsT = TypeVar("_StatsT", bound=_ItemMigrationStats)


@dataclass(frozen=True, slots=True)
class _FieldMigrationSpec(Generic[_StatsT]):
    """一类条目字段迁移的读取、规范化与报告配置。"""

    item_type: str
    columns: tuple[str, ...]
    migration_columns: tuple[str, ...]
    report_prefix: str
    stats_factory: Callable[[], _StatsT]
    normalizer: Callable[[sqlite3.Connection, _JsonObject, _StatsT], _JsonObject]
    json_columns: frozenset[str] = frozenset()
    touch_updated_at: bool = True


def _quote_identifier(identifier: str) -> str:
    """按 SQLite 双引号规则引用受信任或探测到的标识符。"""

    return '"' + identifier.replace('"', '""') + '"'


def _indexes_referencing_columns(
    conn: sqlite3.Connection,
    table: str,
    columns: tuple[str, ...],
) -> list[str]:
    """查找直接或表达式形式引用目标列的用户索引。"""

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
    )
    for row in rows:
        sql = (row["sql"] or "").lower()
        indexed_columns = {
            info["name"].lower()
            for info in conn.execute(f"PRAGMA index_info({_quote_identifier(row['name'])})")
            if info["name"]
        }
        expression_match = any(
            re.search(rf"(?<![\w]){re.escape(column)}(?![\w])", sql) is not None
            for column in needle_columns
        )
        if indexed_columns & needle_columns or expression_match:
            indexes.append(row["name"])
    return indexes


def _lookup_target(conn: sqlite3.Connection, owner_id: str, item_id: str) -> dict[str, str] | None:
    """在同一所有者范围内读取引用目标的最小展示信息。"""

    row = conn.execute(
        """
        SELECT type, title
        FROM items
        WHERE id = ? AND owner_id = ? AND COALESCE(deleted, 0) = 0
        """,
        (item_id, owner_id),
    ).fetchone()
    if row is None:
        return None
    return {
        "type": str(row["type"] or "item"),
        "title": str(row["title"] or "无标题"),
    }


def _clean_text(value: Any, max_length: int = 200) -> str:
    """把旧自由文本清洗并限制为目标字段允许的长度。"""

    text = str(value or "").strip()
    return text[:max_length]


def _text_or_none(value: Any) -> str | None:
    """清洗可空文本，同时保留数据库中的空值语义。"""

    text = str(value or "").strip()
    return text or None


def _stored_text(value: Any) -> str | None:
    """返回数据库实际文本，供判断是否需要写回规范化值。"""

    return None if value is None else str(value)


def _load_list(value: Any) -> list[Any]:
    """只接受旧 JSON 数组，损坏值和其他 JSON 类型统一视为空。"""

    loaded = _loads(value, [])
    return loaded if isinstance(loaded, list) else []


def _normalize_note_reference(
    conn: sqlite3.Connection,
    owner_id: str,
    raw_ref: Any,
) -> tuple[dict[str, str] | None, bool]:
    """清洗一条引用，并尽量从同所有者目标补齐类型和标题。"""

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
    if not ref_id:
        return None, False

    target = _lookup_target(conn, owner_id, ref_id)
    enriched = False
    if target is not None:
        if not ref_type:
            ref_type = target["type"]
            enriched = True
        if not ref_title:
            ref_title = target["title"]
            enriched = True

    normalized = {"kind": kind, "id": ref_id}
    if ref_type:
        normalized["type"] = ref_type
    if ref_title:
        normalized["title"] = ref_title
    return normalized, enriched


def _collect_note_references(
    conn: sqlite3.Connection,
    row: _JsonObject,
    stats: _NoteMigrationStats,
) -> list[dict[str, str]]:
    """合并 references 与 related_items，按 ID 去重并统计补全来源。"""

    sources = [(raw_ref, False) for raw_ref in _load_list(row.get("references"))]
    sources.extend((related_id, True) for related_id in _load_list(row.get("related_items")))
    owner_id = str(row.get("owner_id") or "")
    clean_refs: list[dict[str, str]] = []
    references_by_id: dict[str, dict[str, str]] = {}
    for raw_ref, from_related in sources:
        clean_ref, enriched = _normalize_note_reference(conn, owner_id, raw_ref)
        if clean_ref is None:
            continue
        existing = references_by_id.get(clean_ref["id"])
        if existing is not None:
            for key in ("type", "title"):
                if not existing.get(key) and clean_ref.get(key):
                    existing[key] = clean_ref[key]
            if existing["kind"] == "item" and clean_ref["kind"] != "item":
                existing["kind"] = clean_ref["kind"]
            continue
        references_by_id[clean_ref["id"]] = clean_ref
        clean_refs.append(clean_ref)
        stats.references_enriched += int(enriched)
        stats.references_added_from_related_items += int(from_related)
    return clean_refs


def _normalize_note_payload(
    conn: sqlite3.Connection,
    row: _JsonObject,
    stats: _NoteMigrationStats,
) -> _JsonObject:
    """计算笔记引用、关联 ID 和最后查看时间的最小更新集。"""

    clean_refs = _collect_note_references(conn, row, stats)
    related_items = [ref["id"] for ref in clean_refs]
    last_viewed = _normalize_iso_or_none(row.get("last_viewed"))
    updates: _JsonObject = {}
    if _loads(row.get("references"), None) != clean_refs:
        updates["references"] = clean_refs
    if _loads(row.get("related_items"), None) != related_items:
        updates["related_items"] = related_items
    if _text_or_none(row.get("last_viewed")) != last_viewed:
        updates["last_viewed"] = last_viewed
    return updates


def _is_date_text(value: Any) -> bool:
    """判断旧文本是否为严格的 YYYY-MM-DD 自然日。"""

    text = str(value or "").strip()
    if len(text) != 10:
        return False
    try:
        datetime.strptime(text, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def _has_meaningful_time(value: str | None) -> bool:
    """排除旧日期字段使用的午夜与 23:59 占位时间。"""

    if not value:
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return (parsed.hour, parsed.minute, parsed.second) not in {(0, 0, 0), (23, 59, 0)}


def _normalize_task_schedule(
    row: _JsonObject,
    updates: _JsonObject,
    stats: _TaskMigrationStats,
) -> None:
    """迁移任务计划日、旧日期分类和有具体时刻的截止时间。"""

    category = str(row.get("category") or "").strip()
    raw_plan = _text_or_none(row.get("plan_date"))
    current_plan = raw_plan if _is_date_text(raw_plan) else None
    due_time = _normalize_iso_or_none(row.get("due_time"))
    due_date = due_time[:10] if due_time else None

    plan_date = current_plan
    if plan_date is None and _is_date_text(category):
        plan_date = category
        stats.plan_dates_from_category += 1
    if plan_date is None and due_date:
        plan_date = due_date
        stats.plan_dates_from_due_time += 1
    if _stored_text(row.get("plan_date")) != plan_date:
        updates["plan_date"] = plan_date

    new_category = category if category and not _is_date_text(category) else "未分类"
    if _stored_text(row.get("category")) != new_category:
        updates["category"] = new_category

    current_deadline = _normalize_iso_or_none(row.get("deadline_at"))
    deadline_at = current_deadline
    if deadline_at is None and due_time and _has_meaningful_time(due_time):
        deadline_at = due_time
        stats.deadlines_from_due_time += 1
    if _stored_text(row.get("deadline_at")) != deadline_at:
        updates["deadline_at"] = deadline_at


def _normalize_task_lifecycle(
    row: _JsonObject,
    status: str,
    updates: _JsonObject,
    stats: _TaskMigrationStats,
) -> None:
    """让完成、取消和开放状态只保留各自合法的时间戳。"""

    completed_at = _normalize_iso_or_none(row.get("completed_at"))
    cancelled_at = _normalize_iso_or_none(row.get("cancelled_at"))
    fallback_close_time = (
        completed_at
        or _normalize_iso_or_none(row.get("updated_at"))
        or _normalize_iso_or_none(row.get("created_at"))
    )

    desired_completed: str | None = None
    desired_cancelled: str | None = None
    if status == "done":
        desired_completed = completed_at or fallback_close_time
        stats.completed_timestamps_backfilled += int(
            completed_at is None and desired_completed is not None
        )
    elif status == "cancelled":
        desired_cancelled = cancelled_at or completed_at or fallback_close_time
        stats.cancelled_timestamps_moved += int(
            cancelled_at is None and desired_cancelled is not None
        )

    if _stored_text(row.get("completed_at")) != desired_completed:
        updates["completed_at"] = desired_completed
    if _stored_text(row.get("cancelled_at")) != desired_cancelled:
        updates["cancelled_at"] = desired_cancelled


def _normalize_task_payload(
    _conn: sqlite3.Connection,
    row: _JsonObject,
    stats: _TaskMigrationStats,
) -> _JsonObject:
    """计算任务状态、计划和生命周期字段的最小更新集。"""

    updates: _JsonObject = {}
    current_status = str(row.get("status") or "").strip().lower()
    if current_status in {"done", "cancelled"}:
        status = current_status
    else:
        status = "open"
    if _stored_text(row.get("status")) != status:
        updates["status"] = status
        stats.statuses_normalized += 1

    _normalize_task_schedule(row, updates, stats)
    _normalize_task_lifecycle(row, status, updates, stats)
    return updates


def _entry_time_fallback(row: _JsonObject) -> str | None:
    """优先复用同日日记时间，否则回退到旧版默认晚间时刻。"""

    diary_date = str(row.get("diary_date") or "").strip()
    if not _is_date_text(diary_date):
        return None
    for key in ("created_at", "updated_at"):
        normalized = _normalize_iso_or_none(row.get(key))
        if isinstance(normalized, str) and normalized[:10] == diary_date:
            return normalized
    return f"{diary_date}T21:00:00"


def _normalize_diary_payload(
    _conn: sqlite3.Connection,
    row: _JsonObject,
    stats: _DiaryMigrationStats,
) -> _JsonObject:
    """计算日记时间、情绪、模板答案与收藏标记的最小更新集。"""

    updates: _JsonObject = {}

    current_entry_time = _normalize_iso_or_none(row.get("entry_time"))
    entry_time = current_entry_time or _entry_time_fallback(row)
    if _stored_text(row.get("entry_time")) != entry_time:
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

    current_favorite = normalize_bool_flag(row.get("is_favorite"))
    raw_favorite = row.get("is_favorite")
    normalized_favorite = 1 if current_favorite else 0
    if raw_favorite not in (normalized_favorite, bool(normalized_favorite)):
        updates["is_favorite"] = normalized_favorite
        stats.favorites_normalized += 1

    return updates


def _normalize_ledger_payload(
    _conn: sqlite3.Connection,
    row: _JsonObject,
    stats: _LedgerMigrationStats,
) -> _JsonObject:
    """通过统一账目校验器计算旧金额、方向和账户字段的更新集。"""

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

    updates: _JsonObject = {}
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


_NOTE_MIGRATION: Final = _FieldMigrationSpec(
    item_type="note",
    columns=("id", "owner_id", "references", "related_items", "last_viewed"),
    migration_columns=_NOTE_COLUMNS,
    report_prefix="notes",
    stats_factory=_NoteMigrationStats,
    normalizer=_normalize_note_payload,
    json_columns=frozenset({"references", "related_items"}),
    touch_updated_at=False,
)

_TASK_MIGRATION: Final = _FieldMigrationSpec(
    item_type="task",
    columns=(
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
    ),
    migration_columns=_TASK_COLUMNS,
    report_prefix="tasks",
    stats_factory=_TaskMigrationStats,
    normalizer=_normalize_task_payload,
)

_DIARY_MIGRATION: Final = _FieldMigrationSpec(
    item_type="diary",
    columns=(
        "id",
        "owner_id",
        "diary_date",
        "entry_time",
        "mood",
        "template_answers",
        "is_favorite",
        "created_at",
        "updated_at",
    ),
    migration_columns=_DIARY_COLUMNS,
    report_prefix="diaries",
    stats_factory=_DiaryMigrationStats,
    normalizer=_normalize_diary_payload,
)

_LEDGER_MIGRATION: Final = _FieldMigrationSpec(
    item_type="ledger",
    columns=(
        "id",
        "owner_id",
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
    ),
    migration_columns=_LEDGER_COLUMNS,
    report_prefix="ledgers",
    stats_factory=_LedgerMigrationStats,
    normalizer=_normalize_ledger_payload,
)


def _fetch_migration_items(
    conn: sqlite3.Connection,
    item_columns: set[str],
    spec: _FieldMigrationSpec[_StatsT],
) -> list[_JsonObject]:
    """按当前 schema 为缺失旧列补 NULL，并读取一类有效条目。"""

    missing_base = {"id", "owner_id", "type"} - item_columns
    if missing_base:
        missing_text = ", ".join(sorted(missing_base))
        raise ValueError(f"items 表缺少迁移必需列: {missing_text}")
    select_parts = [
        _quote_identifier(column)
        if column in item_columns
        else f"NULL AS {_quote_identifier(column)}"
        for column in spec.columns
    ]
    active_filter = "COALESCE(deleted, 0) = 0" if "deleted" in item_columns else "1 = 1"
    rows = conn.execute(
        f"""
        SELECT {", ".join(select_parts)}
        FROM items
        WHERE type = ? AND {active_filter}
        """,
        (spec.item_type,),
    )
    return [dict(row) for row in rows]


def _stats_report(stats: _ItemMigrationStats, prefix: str) -> _JsonObject:
    """把共享 seen/updated 字段映射回既有公开报告键。"""

    report = asdict(stats)
    seen = report.pop("seen")
    updated = report.pop("updated")
    return {
        f"{prefix}_seen": seen,
        f"{prefix}_updated": updated,
        **report,
    }


def _migrate_item_fields(
    db_path: Path,
    *,
    apply: bool,
    spec: _FieldMigrationSpec[_StatsT],
) -> _JsonObject:
    """在一致事务中预览或应用一类条目的字段迁移。"""

    conn = _connect(db_path)
    stats = spec.stats_factory()
    try:
        conn.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
        item_columns = _columns(conn, "items")
        stats.missing_schema_columns = [
            column for column in spec.migration_columns if column not in item_columns
        ]
        rows = _fetch_migration_items(conn, item_columns, spec)
        stats.seen = len(rows)
        updated_at = datetime.now().isoformat(timespec="seconds")
        for row in rows:
            updates = spec.normalizer(conn, row, stats)
            if not updates:
                continue
            stats.updated += 1
            if not apply:
                continue
            if spec.touch_updated_at:
                updates["updated_at"] = updated_at
            missing_updates = set(updates) - item_columns
            if missing_updates:
                missing_text = ", ".join(sorted(missing_updates))
                raise ValueError(f"items 表缺少待写入列: {missing_text}")
            set_clause = ", ".join(f"{_quote_identifier(column)} = ?" for column in updates)
            values = [
                _dumps(value) if column in spec.json_columns else value
                for column, value in updates.items()
            ]
            conn.execute(
                f"UPDATE items SET {set_clause} WHERE id = ? AND owner_id = ?",
                [*values, row["id"], row["owner_id"]],
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return _stats_report(stats, spec.report_prefix)


def _cleanup_legacy_item_columns(
    db_path: Path,
    columns: tuple[str, ...],
    label: str,
    *,
    apply: bool,
) -> dict[str, Any]:
    """删除一组旧 items 列，并在失败时保持索引和列的事务一致性。"""

    conn = _connect(db_path)
    report: _JsonObject = {
        "dropped": [],
        "would_drop": [],
        "dropped_indexes": [],
        "would_drop_indexes": [],
    }
    try:
        conn.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
        item_columns = _columns(conn, "items")
        candidates = [column for column in columns if column in item_columns]
        indexes = _indexes_referencing_columns(conn, "items", tuple(candidates))
        if not apply:
            report["would_drop"] = candidates
            report["would_drop_indexes"] = indexes
            conn.commit()
            return report
        for index in indexes:
            conn.execute(f"DROP INDEX IF EXISTS {_quote_identifier(index)}")
            report["dropped_indexes"].append(index)
        for column in candidates:
            try:
                conn.execute(f"ALTER TABLE items DROP COLUMN {_quote_identifier(column)}")
            except sqlite3.OperationalError as exc:
                raise RuntimeError(f"删除旧 {label} 列失败: {column}: {exc}") from exc
            report["dropped"].append(column)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return report


def _rebuild_fts_index(db_path: Path, *, apply: bool) -> _JsonObject:
    """统计全文索引差异，并在应用模式调用数据库统一重建入口。"""

    conn = _connect(db_path)
    try:
        if not _table_exists(conn, "items") or not _table_exists(conn, "items_fts"):
            return {"available": False, "rebuilt": False}
        active_count = int(
            conn.execute("SELECT COUNT(*) FROM items WHERE COALESCE(deleted, 0) = 0").fetchone()[0]
        )
        indexed_active_count = int(
            conn.execute(
                """
                SELECT COUNT(DISTINCT f.id)
                FROM items_fts f
                JOIN items i ON i.id = f.id
                WHERE COALESCE(i.deleted, 0) = 0
                """
            ).fetchone()[0]
        )
        stale_count = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM items_fts f
                LEFT JOIN items i ON i.id = f.id
                WHERE i.id IS NULL OR COALESCE(i.deleted, 0) != 0
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


def _run_redesign_steps(target_path: Path, *, apply: bool) -> _JsonObject:
    """按依赖顺序运行事件、四类字段、旧列和全文索引迁移。"""

    return {
        "event_graph": migrate_event_graph(
            target_path,
            apply=apply,
            create_backup=False,
            write_report=False,
        ),
        "notes": _migrate_item_fields(target_path, apply=apply, spec=_NOTE_MIGRATION),
        "tasks": _migrate_item_fields(target_path, apply=apply, spec=_TASK_MIGRATION),
        "diaries": _migrate_item_fields(target_path, apply=apply, spec=_DIARY_MIGRATION),
        "ledgers": _migrate_item_fields(target_path, apply=apply, spec=_LEDGER_MIGRATION),
        "legacy_event_item_columns": _cleanup_legacy_item_columns(
            target_path,
            _LEGACY_EVENT_ITEM_COLUMNS,
            "event",
            apply=apply,
        ),
        "legacy_task_item_columns": _cleanup_legacy_item_columns(
            target_path,
            _LEGACY_TASK_ITEM_COLUMNS,
            "task",
            apply=apply,
        ),
        "legacy_ledger_item_columns": _cleanup_legacy_item_columns(
            target_path,
            _LEGACY_LEDGER_ITEM_COLUMNS,
            "ledger",
            apply=apply,
        ),
        "fts": _rebuild_fts_index(target_path, apply=apply),
    }


def migrate_pendo_redesign(db_path: str | Path, *, apply: bool = False) -> _JsonObject:
    """在隔离工作副本中预览或原子应用完整 Pendo 重构迁移。"""

    db_path = Path(db_path)
    if not db_path.is_file():
        raise FileNotFoundError(db_path)

    started_at = datetime.now().isoformat(timespec="seconds")
    workspace = _RedesignWorkspace(db_path, apply)
    step_reports: _JsonObject

    try:
        target_path = workspace.prepare()
        step_reports = _run_redesign_steps(target_path, apply=apply)
        workspace.commit()
    except Exception as exc:
        workspace.record_failure(exc)
        raise
    finally:
        workspace.cleanup(sys.exc_info()[1])

    report: _JsonObject = {
        "mode": "apply" if apply else "dry-run",
        "db": str(db_path),
        "backup": str(workspace.backup_path) if workspace.backup_path else None,
        "started_at": started_at,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        **step_reports,
    }
    if workspace.journal_warning is not None:
        report["journal_warning"] = workspace.journal_warning
    if apply:
        report_path = _artifact_path(db_path, "report", ".json")
        report["report_path"] = str(report_path)
        _write_json_file(report_path, report, indent=2)
    return report


def main() -> int:
    """解析命令行模式、执行完整迁移并输出 JSON 报告。"""

    default_db = Path(__file__).resolve().parents[3] / "data" / "pendo" / "pendo.db"
    parser = argparse.ArgumentParser(
        description="把 Pendo 数据库迁移到当前事件、笔记、任务、日记和账目模型。"
    )
    parser.add_argument("db", nargs="?", default=str(default_db))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--apply",
        dest="apply",
        action="store_true",
        help="在一致备份和工作副本中应用迁移",
    )
    mode.add_argument(
        "--dry-run",
        dest="apply",
        action="store_false",
        help="只预览变更（默认模式）",
    )
    parser.set_defaults(apply=False)
    args = parser.parse_args()

    result = migrate_pendo_redesign(args.db, apply=args.apply)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
