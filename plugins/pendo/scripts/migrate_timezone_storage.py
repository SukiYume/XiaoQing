"""探测并把 Pendo 业务时间戳迁移为统一的 UTC-aware ISO 文本。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, TypeAlias, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..config import PendoConfig
from ..utils.time_utils import normalize_datetime_for_storage
from .migration_utils import (
    backup_sqlite_database,
    connect_sqlite_database,
    load_json_field,
    table_columns,
    table_exists,
)

_JsonObject: TypeAlias = dict[str, Any]
_UpdatePlan: TypeAlias = list[tuple[str, str, object, dict[str, object]]]

_ITEM_COMMON_FIELDS: Final = ("created_at", "updated_at", "deleted_at")
_ITEM_TYPE_FIELDS: Final = {
    "event": ("start_time", "end_time"),
    "task": ("deadline_at", "completed_at", "cancelled_at"),
    "note": ("last_viewed",),
    "diary": ("entry_time",),
}
_REMINDER_OPERATION_FIELDS: Final = (
    "sent_at",
    "confirmed_at",
    "last_sent_at",
    "claim_expires_at",
    "next_attempt_at",
)
_FORM_NAMES: Final = ("naive", "aware_utc", "aware_offset", "invalid")


class TimezoneMigrationBlocked(RuntimeError):
    """存量值无法无损解释，禁止进入应用阶段。"""

    def __init__(self, report: _JsonObject) -> None:
        self.report = report
        super().__init__(
            "时区迁移被阻止："
            f"unresolved={report['unresolved_count']}, invalid={report['invalid_count']}"
        )


@dataclass(slots=True)
class _ScanStats:
    forms: Counter[str] = field(default_factory=Counter)
    field_forms: dict[str, Counter[str]] = field(default_factory=lambda: defaultdict(Counter))
    scanned_rows: Counter[str] = field(default_factory=Counter)
    changed_rows: Counter[str] = field(default_factory=Counter)
    changed_values: int = 0
    unresolved_count: int = 0
    invalid_count: int = 0
    issues: list[dict[str, str]] = field(default_factory=list)
    max_issues: int = 200

    def record_form(self, field_name: str, form: str) -> None:
        self.forms[form] += 1
        self.field_forms[field_name][form] += 1

    def issue(self, key: str, kind: str, reason: str) -> None:
        if kind == "invalid":
            self.invalid_count += 1
        else:
            self.unresolved_count += 1
        if len(self.issues) < self.max_issues:
            self.issues.append({"key": key, "kind": kind, "reason": reason})


def _zone(value: object, label: str) -> ZoneInfo:
    name = str(value or "").strip()
    if not name:
        raise ValueError(f"{label} 不能为空")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"{label} 不是有效 IANA 时区: {name}") from exc


def _datetime_form(value: object) -> tuple[str, datetime | None]:
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return "invalid", None
    offset = parsed.utcoffset()
    if offset is None:
        return "naive", parsed
    return ("aware_utc" if offset.total_seconds() == 0 else "aware_offset"), parsed


def _load_resolutions(path: str | Path | None) -> dict[str, str]:
    if path is None:
        return {}
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in payload.items()
    ):
        raise ValueError("resolution 文件必须是 {字段键: IANA时区|owner|server} 的 JSON 对象")
    return {key.strip(): value.strip() for key, value in payload.items()}


class _ZoneResolver:
    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        legacy_server_timezone: str | None,
        resolutions: dict[str, str],
        stats: _ScanStats,
    ) -> None:
        self.connection = connection
        self.server_zone = (
            _zone(legacy_server_timezone, "legacy server timezone")
            if legacy_server_timezone
            else None
        )
        self.resolutions = resolutions
        self.stats = stats
        self._owners: dict[str, ZoneInfo] = {}
        if table_exists(connection, "user_settings"):
            columns = table_columns(connection, "user_settings")
            if {"user_id", "timezone"} <= columns:
                for row in connection.execute("SELECT user_id, timezone FROM user_settings"):
                    owner_id = str(row["user_id"] or "")
                    try:
                        self._owners[owner_id] = _zone(row["timezone"], "用户时区")
                    except ValueError:
                        # 精确字段在真正用到该 owner 时再形成可定位的阻塞项。
                        continue

    def owner_zone(self, owner_id: object, key: str) -> ZoneInfo | None:
        owner = str(owner_id or "")
        if owner in self._owners:
            return self._owners[owner]
        try:
            return _zone(PendoConfig.DEFAULT_TIMEZONE, "Pendo 默认时区")
        except ValueError as exc:
            self.stats.issue(key, "unresolved", str(exc))
            return None

    def explicit_zone(
        self,
        key: str,
        *,
        fallback_key: str | None,
        owner_zone: ZoneInfo,
    ) -> ZoneInfo | None:
        value = self.resolutions.get(key)
        if value is None and fallback_key:
            value = self.resolutions.get(fallback_key)
        if value is None:
            return None
        if value == "owner":
            return owner_zone
        if value == "server":
            if self.server_zone is None:
                self.stats.issue(key, "unresolved", "resolution 使用 server，但未提供旧服务器时区")
                return None
            return self.server_zone
        try:
            return _zone(value, f"resolution {key}")
        except ValueError as exc:
            self.stats.issue(key, "unresolved", str(exc))
            return None

    def server(
        self, key: str, owner_zone: ZoneInfo, fallback_key: str | None = None
    ) -> ZoneInfo | None:
        explicit = self.explicit_zone(
            key,
            fallback_key=fallback_key,
            owner_zone=owner_zone,
        )
        if explicit is not None:
            return explicit
        if self.server_zone is None:
            self.stats.issue(
                key, "unresolved", "需要 --legacy-server-timezone 才能解释旧服务器墙钟"
            )
            return None
        return self.server_zone

    def event_source(
        self,
        key: str,
        *,
        fallback_key: str,
        owner_zone: ZoneInfo,
        event_timezone: object,
    ) -> ZoneInfo | None:
        explicit = self.explicit_zone(
            key,
            fallback_key=fallback_key,
            owner_zone=owner_zone,
        )
        if explicit is not None:
            return explicit
        try:
            event_zone = _zone(event_timezone or owner_zone.key, "事件时区")
        except ValueError as exc:
            self.stats.issue(key, "unresolved", str(exc))
            return None
        if self.server_zone is None:
            self.stats.issue(
                key,
                "unresolved",
                "旧事件可能来自用户墙钟或服务器墙钟；请提供旧服务器时区或 resolution",
            )
            return None
        candidates = {owner_zone.key, event_zone.key, self.server_zone.key}
        if len(candidates) == 1:
            return owner_zone
        self.stats.issue(
            key,
            "unresolved",
            "旧事件的用户、事件与服务器时区不一致；请在 resolution 中指定来源时区",
        )
        return None


def _canonical_value(
    value: object,
    *,
    key: str,
    field_label: str,
    source_zone: ZoneInfo | None,
    stats: _ScanStats,
) -> str | None:
    if value in (None, ""):
        return None
    form, parsed = _datetime_form(value)
    stats.record_form(field_label, form)
    if form == "invalid" or parsed is None:
        stats.issue(key, "invalid", "不是有效 ISO datetime")
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")
    if source_zone is None:
        return None
    try:
        return cast(str, normalize_datetime_for_storage(parsed, key, source_zone))
    except ValueError as exc:
        stats.issue(key, "unresolved", str(exc))
        return None


def _item_source_zone(
    resolver: _ZoneResolver,
    row: sqlite3.Row,
    field_name: str,
    key: str,
) -> ZoneInfo | None:
    owner_zone = resolver.owner_zone(row["owner_id"], key)
    if owner_zone is None:
        return None
    item_id = str(row["id"])
    item_type = str(row["type"] or "")
    event_key = f"items:{item_id}:event_source"
    if field_name == "created_at":
        if item_type == "event":
            return resolver.event_source(
                key,
                fallback_key=event_key,
                owner_zone=owner_zone,
                event_timezone=row["timezone"],
            )
        return owner_zone
    if field_name == "updated_at":
        if row["updated_at"] == row["created_at"]:
            return _item_source_zone(resolver, row, "created_at", key)
        return resolver.server(key, owner_zone)
    if field_name == "deleted_at":
        return resolver.server(key, owner_zone)
    if item_type == "event":
        return resolver.event_source(
            key,
            fallback_key=event_key,
            owner_zone=owner_zone,
            event_timezone=row["timezone"],
        )
    if field_name in {"completed_at", "cancelled_at"}:
        return resolver.server(key, owner_zone)
    return owner_zone


def _plan_item_updates(
    connection: sqlite3.Connection,
    resolver: _ZoneResolver,
    stats: _ScanStats,
) -> tuple[_UpdatePlan, dict[str, sqlite3.Row]]:
    plans: _UpdatePlan = []
    item_rows: dict[str, sqlite3.Row] = {}
    if not table_exists(connection, "items"):
        return plans, item_rows
    columns = table_columns(connection, "items")
    required = {"id", "type", "owner_id", "timezone", "remind_times"}
    if not required <= columns:
        raise ValueError(f"items 表缺少迁移所需列: {', '.join(sorted(required - columns))}")
    for row in connection.execute("SELECT * FROM items ORDER BY id"):
        item_id = str(row["id"])
        item_rows[item_id] = row
        stats.scanned_rows["items"] += 1
        updates: dict[str, object] = {}
        item_type = str(row["type"] or "")
        fields = (*_ITEM_COMMON_FIELDS, *_ITEM_TYPE_FIELDS.get(item_type, ()))
        for field_name in fields:
            if field_name not in columns or row[field_name] in (None, ""):
                continue
            key = f"items:{item_id}:{field_name}"
            form, _parsed = _datetime_form(row[field_name])
            source_zone = None
            if form == "naive":
                source_zone = _item_source_zone(resolver, row, field_name, key)
            canonical = _canonical_value(
                row[field_name],
                key=key,
                field_label=f"items.{field_name}",
                source_zone=source_zone,
                stats=stats,
            )
            if canonical is not None and canonical != row[field_name]:
                updates[field_name] = canonical
                stats.changed_values += 1

        raw_reminders = row["remind_times"]
        if raw_reminders not in (None, ""):
            values = load_json_field(raw_reminders, None)
            if not isinstance(values, list):
                stats.record_form("items.remind_times", "invalid")
                stats.issue(f"items:{item_id}:remind_times", "invalid", "不是 JSON 数组")
            else:
                normalized_values: list[str] = []
                complete = True
                for index, value in enumerate(values):
                    key = f"items:{item_id}:remind_times[{index}]"
                    form, _parsed = _datetime_form(value)
                    source_zone = None
                    if form == "naive":
                        source_zone = _item_source_zone(resolver, row, "remind_times", key)
                    canonical = _canonical_value(
                        value,
                        key=key,
                        field_label="items.remind_times",
                        source_zone=source_zone,
                        stats=stats,
                    )
                    if canonical is None:
                        complete = False
                    else:
                        normalized_values.append(canonical)
                encoded = json.dumps(normalized_values, ensure_ascii=False)
                if complete and encoded != raw_reminders:
                    updates["remind_times"] = encoded
                    stats.changed_values += len(values)
        if updates:
            plans.append(("items", "id", item_id, updates))
            stats.changed_rows["items"] += 1
    return plans, item_rows


def _plan_collection_updates(
    connection: sqlite3.Connection,
    resolver: _ZoneResolver,
    stats: _ScanStats,
) -> _UpdatePlan:
    plans: _UpdatePlan = []
    if not table_exists(connection, "event_collections"):
        return plans
    columns = table_columns(connection, "event_collections")
    for row in connection.execute("SELECT * FROM event_collections ORDER BY id"):
        collection_id = str(row["id"])
        stats.scanned_rows["event_collections"] += 1
        updates: dict[str, object] = {}
        owner_zone = resolver.owner_zone(row["owner_id"], f"event_collections:{collection_id}")
        if owner_zone is None:
            continue
        event_key = f"event_collections:{collection_id}:event_source"
        for field_name in ("created_at", "updated_at", "deleted_at", "start_time", "end_time"):
            if field_name not in columns or row[field_name] in (None, ""):
                continue
            key = f"event_collections:{collection_id}:{field_name}"
            form, _parsed = _datetime_form(row[field_name])
            source_zone: ZoneInfo | None = None
            if form == "naive":
                if field_name == "deleted_at" or (
                    field_name == "updated_at" and row["updated_at"] != row["created_at"]
                ):
                    source_zone = resolver.server(key, owner_zone)
                else:
                    source_zone = resolver.event_source(
                        key,
                        fallback_key=event_key,
                        owner_zone=owner_zone,
                        event_timezone=row["timezone"],
                    )
            canonical = _canonical_value(
                row[field_name],
                key=key,
                field_label=f"event_collections.{field_name}",
                source_zone=source_zone,
                stats=stats,
            )
            if canonical is not None and canonical != row[field_name]:
                updates[field_name] = canonical
                stats.changed_values += 1
        if updates:
            plans.append(("event_collections", "id", collection_id, updates))
            stats.changed_rows["event_collections"] += 1
    return plans


def _plan_reminder_log_updates(
    connection: sqlite3.Connection,
    resolver: _ZoneResolver,
    stats: _ScanStats,
    item_rows: dict[str, sqlite3.Row],
) -> _UpdatePlan:
    plans: _UpdatePlan = []
    if not table_exists(connection, "reminder_logs"):
        return plans
    columns = table_columns(connection, "reminder_logs")
    for row in connection.execute("SELECT * FROM reminder_logs ORDER BY id"):
        log_id = int(row["id"])
        stats.scanned_rows["reminder_logs"] += 1
        updates: dict[str, object] = {}
        item = item_rows.get(str(row["item_id"] or ""))
        for field_name in ("remind_time", *_REMINDER_OPERATION_FIELDS, "fire_at_utc"):
            if field_name not in columns or row[field_name] in (None, ""):
                continue
            key = f"reminder_logs:{log_id}:{field_name}"
            form, _parsed = _datetime_form(row[field_name])
            source_zone: ZoneInfo | None = None
            if form == "naive":
                if field_name == "fire_at_utc":
                    source_zone = ZoneInfo("UTC")
                elif field_name == "remind_time" and item is not None:
                    source_zone = _item_source_zone(resolver, item, "remind_times", key)
                elif field_name == "remind_time":
                    stats.issue(key, "unresolved", "提醒日志找不到所属条目，无法确定墙钟来源")
                else:
                    owner_zone = (
                        resolver.owner_zone(item["owner_id"], key)
                        if item is not None
                        else ZoneInfo(PendoConfig.DEFAULT_TIMEZONE)
                    )
                    if owner_zone is not None:
                        source_zone = resolver.server(key, owner_zone)
            canonical = _canonical_value(
                row[field_name],
                key=key,
                field_label=f"reminder_logs.{field_name}",
                source_zone=source_zone,
                stats=stats,
            )
            if canonical is not None and canonical != row[field_name]:
                updates[field_name] = canonical
                stats.changed_values += 1
        if updates:
            plans.append(("reminder_logs", "id", log_id, updates))
            stats.changed_rows["reminder_logs"] += 1
    return plans


def _scan(
    connection: sqlite3.Connection,
    *,
    legacy_server_timezone: str | None,
    resolutions: dict[str, str],
    max_issues: int,
) -> tuple[_UpdatePlan, _ScanStats]:
    stats = _ScanStats(max_issues=max_issues)
    resolver = _ZoneResolver(
        connection,
        legacy_server_timezone=legacy_server_timezone,
        resolutions=resolutions,
        stats=stats,
    )
    item_plans, item_rows = _plan_item_updates(connection, resolver, stats)
    plans = [
        *item_plans,
        *_plan_collection_updates(connection, resolver, stats),
        *_plan_reminder_log_updates(connection, resolver, stats, item_rows),
    ]
    return plans, stats


def _stats_report(stats: _ScanStats) -> _JsonObject:
    forms = {name: int(stats.forms[name]) for name in _FORM_NAMES}
    field_forms = {
        field_name: {name: int(counter[name]) for name in _FORM_NAMES}
        for field_name, counter in sorted(stats.field_forms.items())
    }
    return {
        "forms": forms,
        "field_forms": field_forms,
        "scanned_rows": dict(sorted(stats.scanned_rows.items())),
        "would_change_rows": dict(sorted(stats.changed_rows.items())),
        "would_change_values": stats.changed_values,
        "unresolved_count": stats.unresolved_count,
        "invalid_count": stats.invalid_count,
        "issues_truncated": stats.unresolved_count + stats.invalid_count > len(stats.issues),
        "issues": stats.issues,
        "blocked": bool(stats.unresolved_count or stats.invalid_count),
    }


def _apply_plans(connection: sqlite3.Connection, plans: _UpdatePlan) -> None:
    for table, primary_key, identity, updates in plans:
        assignments = ", ".join(f'"{column}" = ?' for column in updates)
        connection.execute(
            f'UPDATE "{table}" SET {assignments} WHERE "{primary_key}" = ?',
            [*updates.values(), identity],
        )


def _artifact_path(db_path: Path, kind: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return db_path.with_name(f"{db_path.name}.timezone-{kind}-{stamp}")


def _validate_database(path: Path) -> None:
    connection = connect_sqlite_database(path)
    try:
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0]).lower()
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        connection.close()
    if integrity != "ok" or foreign_keys:
        raise RuntimeError(
            f"数据库校验失败: path={path}, integrity={integrity}, foreign_keys={len(foreign_keys)}"
        )


def migrate_timezone_storage(
    db_path: str | Path,
    *,
    apply: bool = False,
    legacy_server_timezone: str | None = None,
    resolution_file: str | Path | None = None,
    create_backup: bool = True,
    max_issues: int = 200,
) -> _JsonObject:
    """探测或迁移 Pendo 业务时间戳；应用模式严格拒绝任何未决值。"""

    path = Path(db_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    if max_issues < 0:
        raise ValueError("max_issues 不能为负数")
    resolutions = _load_resolutions(resolution_file)

    preflight = connect_sqlite_database(path)
    try:
        _plans, stats = _scan(
            preflight,
            legacy_server_timezone=legacy_server_timezone,
            resolutions=resolutions,
            max_issues=max_issues,
        )
    finally:
        preflight.close()
    report: _JsonObject = {
        "mode": "apply" if apply else "dry-run",
        "db": str(path),
        "legacy_server_timezone": legacy_server_timezone,
        "resolution_file": str(Path(resolution_file).resolve()) if resolution_file else None,
        "backup": None,
        **_stats_report(stats),
    }
    if not apply:
        return report
    if report["blocked"]:
        raise TimezoneMigrationBlocked(report)

    backup_path: Path | None = None
    if create_backup:
        backup_path = _artifact_path(path, "backup")
        backup_sqlite_database(path, backup_path)
        _validate_database(backup_path)
        report["backup"] = str(backup_path)

    connection = connect_sqlite_database(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        locked_plans, locked_stats = _scan(
            connection,
            legacy_server_timezone=legacy_server_timezone,
            resolutions=resolutions,
            max_issues=max_issues,
        )
        locked_report = _stats_report(locked_stats)
        if locked_report["blocked"]:
            connection.rollback()
            report.update(locked_report)
            raise TimezoneMigrationBlocked(report)
        _apply_plans(connection, locked_plans)
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0]).lower()
        if integrity != "ok" or foreign_keys:
            raise RuntimeError(
                f"迁移后数据库校验失败: integrity={integrity}, foreign_keys={len(foreign_keys)}"
            )
        connection.commit()
        report.update(locked_report)
        report["applied_rows"] = sum(locked_stats.changed_rows.values())
        report["applied_values"] = locked_stats.changed_values
        report["integrity_check"] = integrity
        report["foreign_key_violations"] = len(foreign_keys)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return report


def _default_database_path() -> Path:
    """Return the same project data path used by the running Pendo plugin."""

    return Path(__file__).resolve().parents[3] / "data" / "pendo" / "pendo.db"


def main() -> int:
    default_db = _default_database_path()
    parser = argparse.ArgumentParser(
        description=(
            "探测 Pendo 时间戳的 naive/UTC/偏移/损坏四种形式，并在无歧义时统一为 UTC。"
            "应用前必须停止 Pendo 写入。"
        )
    )
    parser.add_argument("db", nargs="?", default=str(default_db))
    parser.add_argument(
        "--legacy-server-timezone",
        help="解释历史服务器墙钟的 IANA 时区；不提供时相关字段保持阻塞",
    )
    parser.add_argument(
        "--resolution-file",
        help="歧义字段到 IANA时区、owner 或 server 的 JSON 映射",
    )
    parser.add_argument("--max-issues", type=int, default=200)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", dest="apply", action="store_true", help="备份后事务应用")
    mode.add_argument("--dry-run", dest="apply", action="store_false", help="只探测（默认）")
    parser.set_defaults(apply=False)
    args = parser.parse_args()
    try:
        result = migrate_timezone_storage(
            args.db,
            apply=args.apply,
            legacy_server_timezone=args.legacy_server_timezone,
            resolution_file=args.resolution_file,
            max_issues=args.max_issues,
        )
    except TimezoneMigrationBlocked as exc:
        print(json.dumps(exc.report, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
