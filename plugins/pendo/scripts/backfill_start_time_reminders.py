from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import cast


class _Args(argparse.Namespace):
    db: str = ""
    apply: bool = False


def _normalize_iso(value: str | None) -> str | None:
    if not value or not str(value).strip():
        return None
    try:
        return datetime.fromisoformat(str(value).strip()).isoformat()
    except ValueError:
        return None


def _row_value(row: sqlite3.Row, key: str) -> object:
    return cast(object, row[key])


def _load_remind_times(raw_value: object) -> list[str]:
    values: list[object]
    if isinstance(raw_value, list):
        values = cast(list[object], raw_value)
    elif isinstance(raw_value, str) and raw_value.strip():
        try:
            loaded = cast(object, json.loads(raw_value))
        except json.JSONDecodeError:
            return []
        values = cast(list[object], loaded) if isinstance(loaded, list) else []
    else:
        values = []

    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        normalized_value = _normalize_iso(value)
        if normalized_value is None or normalized_value in seen:
            continue
        seen.add(normalized_value)
        normalized.append(normalized_value)
    normalized.sort()
    return normalized


def _load_milestone_times(raw_value: object) -> list[str]:
    if not isinstance(raw_value, str) or not raw_value.strip():
        return []

    try:
        loaded = cast(object, json.loads(raw_value))
    except json.JSONDecodeError:
        return []

    if not isinstance(loaded, list):
        return []
    milestones = cast(list[object], loaded)

    normalized: list[str] = []
    seen: set[str] = set()
    for milestone in milestones:
        if not isinstance(milestone, dict):
            continue
        milestone_time = cast(dict[object, object], milestone).get("time")
        if not isinstance(milestone_time, str):
            continue
        normalized_time = _normalize_iso(milestone_time)
        if normalized_time is None or normalized_time in seen:
            continue
        seen.add(normalized_time)
        normalized.append(normalized_time)
    normalized.sort()
    return normalized


def backfill_missing_start_time_reminders(db_path: str, dry_run: bool = True) -> dict[str, int]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    matched = 0
    updated = 0
    skipped = 0

    try:
        rows_raw = cast(
            list[object],
            conn.execute(
                """
            SELECT id, start_time, remind_times, milestones
            FROM items
            WHERE type = 'event' AND deleted = 0 AND start_time IS NOT NULL AND TRIM(start_time) != ''
            """
            ).fetchall(),
        )
        rows = [cast(sqlite3.Row, row) for row in rows_raw]

        for row in rows:
            start_time_raw = _row_value(row, "start_time")
            if not isinstance(start_time_raw, str):
                skipped += 1
                continue
            start_time = _normalize_iso(start_time_raw)
            if start_time is None:
                skipped += 1
                continue

            remind_times = _load_remind_times(_row_value(row, "remind_times"))
            expected_times = sorted(
                {start_time, *_load_milestone_times(_row_value(row, "milestones"))}
            )
            missing_times = [
                candidate for candidate in expected_times if candidate not in remind_times
            ]
            if not missing_times:
                continue

            matched += 1
            if dry_run:
                continue

            new_remind_times = sorted({*remind_times, *missing_times})
            row_id_raw = _row_value(row, "id")
            if not isinstance(row_id_raw, str):
                skipped += 1
                continue
            now_dt = datetime.now()
            now_iso = now_dt.isoformat()
            
            _updated_cursor = conn.execute(
                "UPDATE items SET remind_times = ?, updated_at = ? WHERE id = ?",
                (
                    json.dumps(new_remind_times, ensure_ascii=False),
                    now_iso,
                    row_id_raw,
                ),
            )
            
            # 将过去的提醒自动插入 reminder_logs 标记为已确认
            # 使得 /pendo event reminders 中显示为已处理（✅），并防止重复发送任务抓取它们
            for mt in missing_times:
                try:
                    if datetime.fromisoformat(mt) < now_dt:
                        conn.execute(
                            """
                            INSERT INTO reminder_logs (item_id, remind_time, sent_at, confirmed_at, user_action)
                            VALUES (?, ?, ?, ?, ?)
                            """,
                            (row_id_raw, mt, now_iso, now_iso, "auto_backfilled")
                        )
                except ValueError:
                    pass
            
            updated += 1

        if not dry_run:
            conn.commit()
    finally:
        conn.close()

    return {
        "matched": matched,
        "updated": updated,
        "skipped": skipped,
        "dry_run": int(dry_run),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    default_db_path = str(Path(__file__).resolve().parents[1] / "data" / "pendo.db")
    _ = parser.add_argument(
        "--db",
        default=default_db_path,
    )
    _ = parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(namespace=_Args(db=default_db_path, apply=False))
    db_path = args.db
    apply = args.apply

    result = backfill_missing_start_time_reminders(db_path, dry_run=not apply)
    mode = "apply" if apply else "dry-run"
    print(
        json.dumps(
            {
                "mode": mode,
                "db": db_path,
                "matched": result["matched"],
                "updated": result["updated"],
                "skipped": result["skipped"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
