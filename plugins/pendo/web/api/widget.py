"""Compact widget-oriented API for Scriptable and similar clients."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from ...services.db import Database
from ..analytics.event_schedule import build_event_schedule, ensure_datetime
from ..analytics.ledger_insights import build_ledger_insights
from ..analytics.notes_overview import build_notes_overview
from ..analytics.task_overview import build_task_overview
from ..deps import get_current_user, get_db

router = APIRouter()

_WEEKDAY_LABELS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
_SECTION_ORDER = ["tasks", "ledger", "notes"]
_LINKS = {
    "dashboard": "#/dashboard",
    "events": "#/events",
    "tasks": "#/tasks",
    "ledger": "#/ledger",
    "notes": "#/notes",
}


def _parse_now(value: str | None) -> datetime:
    if not value:
        return datetime.now().replace(microsecond=0)
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is not None:
        parsed = parsed.replace(tzinfo=None)
    return parsed.replace(microsecond=0)


def _section_for(value: str, now: datetime) -> str:
    normalized = str(value or "auto").strip().lower()
    if normalized == "all":
        return "all"
    if normalized in _SECTION_ORDER:
        return normalized
    if normalized != "auto":
        raise HTTPException(status_code=400, detail="section must be one of tasks, ledger, notes, all, auto")
    return _SECTION_ORDER[now.hour % len(_SECTION_ORDER)]


def _title_text(value: str | None, fallback: str = "无标题", limit: int = 22) -> str:
    text = str(value or "").strip() or fallback
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


def _preview_text(value: str | None, limit: int = 26) -> str:
    text = " ".join(str(value or "").strip().split())
    if len(text) <= limit:
        return text
    return f"{text[: limit - 1]}…"


def _format_amount(value: float, *, signed: bool = False) -> str:
    amount = float(value or 0)
    prefix = ""
    if signed:
        prefix = "+" if amount >= 0 else "-"
        amount = abs(amount)
    return f"{prefix}¥{amount:.0f}"


def _format_event_meta(entry: dict[str, Any]) -> str:
    parts: list[str] = []
    start_time = str(entry.get("start_time") or "")
    end_time = str(entry.get("end_time") or "")
    if start_time:
        label = start_time[11:16]
        if end_time:
            label = f"{label}-{end_time[11:16]}"
        parts.append(label)
    if entry.get("location"):
        parts.append(str(entry["location"]))
    return " · ".join(parts)


def _format_task_meta(task: dict[str, Any], today_key: str) -> str:
    status_map = {
        "open": "待办",
        "done": "已完成",
        "cancelled": "已取消",
    }
    status = status_map.get(str(task.get("status") or ""), "待办")
    plan_date = str(task.get("plan_date") or "")
    deadline_at = str(task.get("deadline_at") or "")
    due_key = plan_date or deadline_at[:10]
    if due_key:
        if due_key == today_key:
            due_label = "今天"
        else:
            due_label = due_key[5:].replace("-", "/")
        return f"{status} · {due_label}"
    return status


def _merge_unique_tasks(*task_groups: list[dict[str, Any]], limit: int = 3) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in task_groups:
        for task in group:
            task_id = str(task.get("id") or "")
            dedupe_key = task_id or (
                f"{task.get('title')}|{task.get('plan_date')}|"
                f"{task.get('deadline_at')}|{task.get('created_at')}"
            )
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            merged.append(task)
            if len(merged) >= limit:
                return merged
    return merged


def _flatten_event_entries(
    db: Database,
    owner_id: str,
    events: list[Any],
    range_start: datetime,
    range_end: datetime,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    start_day = range_start.date()
    end_day = range_end.date()
    collection_cache: dict[str, dict[str, Any] | None] = {}
    for event in events:
        collection = None
        collection_id = getattr(event, "event_collection_id", None)
        if collection_id:
            if collection_id not in collection_cache:
                collection_cache[collection_id] = db.get_event_collection(collection_id, owner_id)
            collection = collection_cache[collection_id]
        schedule = build_event_schedule(event, start_day, end_day)
        for day in schedule["display_days"]:
            for row in schedule["day_entries"].get(day, []):
                row_start = ensure_datetime(row.get("start_time")) or datetime.fromisoformat(f"{day}T00:00:00")
                entry_title = row.get("title") or getattr(event, "title", None) or "无标题"
                if collection and collection.get("kind") == "multi_node":
                    entry_title = f"{collection.get('title') or '多节点日程'} · {entry_title}"
                rows.append(
                    {
                        "day": day,
                        "title": entry_title,
                        "subtitle": row.get("subtitle") or "",
                        "start_time": row.get("start_time") or "",
                        "end_time": row.get("end_time") or "",
                        "location": row.get("location") or (collection or {}).get("location") or "",
                        "category": row.get("category") or (collection or {}).get("category") or "",
                        "entry_kind": row.get("kind") or "",
                        "sort_time": row_start,
                    }
                )
    rows.sort(key=lambda item: item["sort_time"])
    return rows


def _build_agenda(db: Database, owner_id: str, now: datetime) -> dict[str, Any]:
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow_start = today_start + timedelta(days=1)
    range_end = (today_start + timedelta(days=30)).replace(hour=23, minute=59, second=59, microsecond=0)
    raw_events = db.get_events_for_range(
        owner_id,
        today_start.strftime("%Y-%m-%dT%H:%M:%S"),
        range_end.strftime("%Y-%m-%dT%H:%M:%S"),
    )
    rows = _flatten_event_entries(db, owner_id, raw_events, today_start, range_end)
    upcoming = [row for row in rows if row["sort_time"] >= now][:5]
    today_key = today_start.strftime("%Y-%m-%d")
    tomorrow_key = tomorrow_start.strftime("%Y-%m-%d")

    return {
        "date": {
            "weekday": _WEEKDAY_LABELS[now.weekday()],
            "month": now.month,
            "day": now.day,
        },
        "today_count": sum(1 for row in rows if row["day"] == today_key),
        "tomorrow_count": sum(1 for row in rows if row["day"] == tomorrow_key),
        "items": [
            {
                "title": str(item["title"] or "无标题").strip() or "无标题",
                "subtitle": item["subtitle"],
                "meta": _format_event_meta(item),
                "day": item["day"],
                "start_time": str(item.get("start_time") or ""),
                "end_time": str(item.get("end_time") or ""),
                "location": str(item.get("location") or ""),
                "path": _LINKS["events"],
            }
            for item in upcoming
        ],
        "empty_text": "最近没有安排",
    }


def _build_task_panel(db: Database, owner_id: str, now: datetime) -> dict[str, Any]:
    today_key = now.strftime("%Y-%m-%d")
    overview = build_task_overview(db=db, owner_id=owner_id, today=today_key)
    items = _merge_unique_tasks(
        overview["focus_tasks"],
        overview["up_next_tasks"],
        overview["backlog_tasks"],
        overview["later_tasks"],
        limit=5,
    )
    summary = overview["summary"]
    secondary_bits = [f"{summary['focus_count']} 项今日聚焦"]
    if summary["overdue_count"]:
        secondary_bits.append(f"{summary['overdue_count']} 项逾期")

    return {
        "section": "tasks",
        "title": "待办",
        "path": _LINKS["tasks"],
        "summary": {
            "primary": f"{summary['active_count']} 项待办",
            "secondary": " · ".join(secondary_bits),
        },
        "items": [
            {
                "title": _title_text(task.get("title"), limit=18),
                "meta": _format_task_meta(task, today_key),
            }
            for task in items
        ],
        "empty_text": "当前没有待办",
    }


def _build_ledger_panel(db: Database, owner_id: str, now: datetime) -> dict[str, Any]:
    today_key = now.strftime("%Y-%m-%d")
    month_start = now.replace(day=1).strftime("%Y-%m-%d")
    insights = build_ledger_insights(
        db=db,
        owner_id=owner_id,
        start_date=month_start,
        end_date=today_key,
    )
    recent_ledger = db.get_items(
        owner_id,
        filters={
            "type": "ledger",
            "date_field": "ledger_date",
            "start_date": month_start,
            "end_date": today_key,
            "sort_field": "ledger_date",
            "sort_order": "DESC",
        },
        limit=5,
    )
    recent_ledger = sorted(
        recent_ledger,
        key=lambda item: (
            getattr(item, "ledger_date", "") or "",
            getattr(item, "created_at", "") or "",
        ),
        reverse=True,
    )
    recent_ledger = sorted(
        recent_ledger,
        key=lambda item: getattr(item, "transaction_type", "expense") != "expense",
    )
    summary = insights["summary"]
    balance = summary["income_total"] - summary["expense_total"]

    return {
        "section": "ledger",
        "title": "财务",
        "path": _LINKS["ledger"],
        "summary": {
            "primary": f"支出 {_format_amount(summary['expense_total'])}",
            "secondary": f"收入 {_format_amount(summary['income_total'])} · 结余 {_format_amount(balance, signed=True)}",
        },
        "items": [
            {
                "title": _title_text(getattr(item, "title", None), fallback="未命名账目", limit=18),
                "meta": " · ".join(
                    part for part in [getattr(item, "ledger_category", None) or "未分类", getattr(item, "ledger_date", None) or ""] if part
                ),
                "amount_text": (
                    f"↔ {_format_amount(float(getattr(item, 'amount', 0) or 0))}"
                    if getattr(item, "transaction_type", "expense") == "transfer"
                    else (
                        _format_amount(float(getattr(item, "amount", 0) or 0), signed=True)
                        if getattr(item, "transaction_type", "expense") == "income"
                        else f"-{_format_amount(float(getattr(item, 'amount', 0) or 0))}"
                    )
                ),
            }
            for item in recent_ledger
        ],
        "empty_text": "本月还没有账目",
    }


def _build_note_panel(db: Database, owner_id: str, now: datetime) -> dict[str, Any]:
    today_key = now.strftime("%Y-%m-%d")
    overview = build_notes_overview(db=db, owner_id=owner_id, today=today_key)
    summary = overview["summary"]
    notes = overview["recent_notes"][:5]

    return {
        "section": "notes",
        "title": "笔记",
        "path": _LINKS["notes"],
        "summary": {
            "primary": f"{summary['total_count']} 条笔记",
            "secondary": f"近 7 天新增 {summary['week_new_count']} 条",
        },
        "items": [
            {
                "title": _title_text(note.get("title"), limit=18),
                "meta": note.get("category") or "未分类",
                "preview": _preview_text(note.get("content"), limit=22),
            }
            for note in notes
        ],
        "empty_text": "最近没有笔记更新",
    }


def build_widget_summary(db: Database, owner_id: str, section: str = "auto", now: str | None = None) -> dict[str, Any]:
    current = _parse_now(now)
    resolved_section = _section_for(section, current)
    panel_builders = {
        "tasks": _build_task_panel,
        "ledger": _build_ledger_panel,
        "notes": _build_note_panel,
    }
    base: dict[str, Any] = {
        "generated_at": current.isoformat(),
        "section_requested": str(section or "auto").strip().lower() or "auto",
        "section": resolved_section,
        "agenda": _build_agenda(db=db, owner_id=owner_id, now=current),
        "links": dict(_LINKS),
    }
    if resolved_section == "all":
        base["panels"] = {
            key: builder(db=db, owner_id=owner_id, now=current)
            for key, builder in panel_builders.items()
        }
    else:
        base["panel"] = panel_builders[resolved_section](db=db, owner_id=owner_id, now=current)
    return base


@router.get("/widget/summary")
def get_widget_summary(
    section: str = "auto",
    now: str | None = None,
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    return {
        "ok": True,
        "data": build_widget_summary(db=db, owner_id=owner_id, section=section, now=now),
        "message": "",
    }
