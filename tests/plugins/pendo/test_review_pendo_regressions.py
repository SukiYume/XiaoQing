"""完整审查的 Pendo 数据一致性、时间与浏览器恢复回归。"""

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from plugins.pendo.handlers.event import EventHandler
from plugins.pendo.handlers.task import TaskHandler
from plugins.pendo.services.ai_parser import AIParser
from plugins.pendo.services.exporter import ExporterService
from plugins.pendo.services.rule_parser import RuleParser
from plugins.pendo.web.api.items import ItemCreate, ItemUpdate, create_item, update_item
from plugins.pendo.web.api.transfer import ExportSelection, _build_bundle_bytes
from plugins.pendo.web.api.widget import build_widget_calendar
from plugins.pendo.web.services.bundle_import import inspect_bundle_bytes


@pytest.mark.parametrize("kind", ["task", "event"])
@pytest.mark.parametrize("times", [[], ["2030-01-01T09:30:00+00:00"]])
def test_explicit_reminders_replace_rules(db, kind, times):
    clock = "deadline_at" if kind == "task" else "start_time"
    iid   = db.insert_item(
        {
            "owner_id": "u",
            "type": kind,
            "title": "reminder",
            clock: "2030-01-01T10:00:00+00:00",
            "reminder_rules": [{"offset_seconds": 3600}],
        }
    )
    update_item(iid, ItemUpdate(remind_times=times), "u", db)
    item = db.get_item(iid, "u")
    assert all(t in item.remind_times for t in times)
    assert "2030-01-01T09:00:00+00:00" not in item.remind_times
    if not times:
        assert item.remind_times == item.reminder_rules == []


def test_event_create_uses_owner_timezone(db):
    db.update_user_settings("u", {"timezone": "America/Los_Angeles"})
    create_item(
        ItemCreate(type="event", title="morning", start_time="2030-01-01T10:00:00"), "u", db
    )
    event = db.get_items("u")[0]
    assert event.timezone == "America/Los_Angeles"
    assert event.start_time == "2030-01-01T18:00:00+00:00"


def test_undo_restores_queue_without_replaying_sent_history(db):
    now = datetime.now(UTC).replace(microsecond=0)
    times = [(now + timedelta(minutes=m)).isoformat() for m in [1, 5]]
    iid = db.insert_item(
        {"owner_id": "u", "type": "task", "title": "restore", "remind_times": times}
    )
    db.get_connection().execute(
        "UPDATE reminder_logs SET sent_at=?, confirmed_at=? WHERE item_id=? AND remind_time=?",
        (now.isoformat(), now.isoformat(), iid, times[0]),
    )
    db.get_connection().commit()
    db.delete_item(iid, owner_id="u")
    assert db.undo_delete("u")["status"] == "success"
    rows = (
        db.get_connection()
        .execute("SELECT * FROM reminder_logs WHERE item_id=? ORDER BY remind_time", (iid,))
        .fetchall()
    )
    assert len(rows) == 2
    assert rows[0]["sent_at"] is not None
    assert rows[1]["state"] == "pending"


@pytest.mark.parametrize("kind", ["item", "items", "settings", "event_collection"])
def test_cache_rejects_concurrent_old_snapshot(db, monkeypatch, kind):
    iid = db.insert_item({"owner_id": "u", "type": "note", "title": "before"})
    if kind == "event_collection":
        cid = db.create_event_collection(
            {"owner_id": "u", "kind": "multi_node", "title": "before"},
            children=[("child", {"title": "child", "start_time": "2030-01-01T10:00:00+00:00"})],
        )

        def read():
            return db.get_event_collection(cid, "u")

        def write():
            return db.update_event_collection(cid, {"title": "after"}, "u")

        def value(row):
            return row["title"]
    elif kind == "settings":
        db.update_user_settings("u", {"timezone": "Asia/Shanghai"})

        def read():
            return db.get_user_settings("u")

        def write():
            return db.update_user_settings("u", {"timezone": "America/Los_Angeles"})

        def value(row):
            return row["timezone"]
    elif kind == "items":

        def read():
            return db.get_items("u")

        def write():
            return db.update_item(iid, {"title": "after"}, "u")

        def value(rows):
            return rows[0].title
    else:

        def read():
            return db.get_item(iid, "u")

        def write():
            return db.update_item(iid, {"title": "after"}, "u")

        def value(row):
            return row.title

    db.cache_clear()
    fetched, release = Event(), Event()
    original = db._cache_set

    def delayed(key, data):
        from threading import current_thread

        if key.startswith(kind + "|") and current_thread().name == "old-reader":
            fetched.set()
            assert release.wait(5)
        original(key, data)

    monkeypatch.setattr(db, "_cache_set", delayed)
    thread = Thread(target=read, name="old-reader")
    thread.start()
    try:
        assert fetched.wait(5)
        write()
    finally:
        release.set()
        thread.join(5)
    assert value(read()) == ("America/Los_Angeles" if kind == "settings" else "after")


def test_collection_search_filters_before_pagination(db):
    db.create_event_collection(
        {"owner_id": "u", "kind": "multi_node", "title": "needle"},
        children=[
            ("child", {"title": "leaf", "start_time": "2030-01-01T10:00:00+00:00", "tags": ["x"]})
        ],
    )
    for filters in [
        {
            "type": "event",
            "date_field": "start_time",
            "start_date": "2031-01-01",
            "end_date": "2031-02-01",
        },
        {"type": "event", "tags": "absent"},
    ]:
        items, total = db.search_items_page("u", "needle", filters=filters)
        assert items == [] and total == 0


@pytest.mark.asyncio
async def test_task_timezone_and_deadline_reschedule(db, monkeypatch):
    db.update_user_settings("u", {"timezone": "Asia/Shanghai"})
    iid = db.insert_item(
        {
            "owner_id": "u",
            "type": "task",
            "title": "future",
            "deadline_at": "2030-01-01T12:00:00+08:00",
            "reminder_rules": [{"offset_seconds": 3600}],
        }
    )
    handler = TaskHandler(db)
    monkeypatch.setattr(handler, "_user_local_now", lambda _uid: datetime(2030, 1, 1, 10))
    assert "future" not in (await handler.list_tasks("u", "overdue", None))["message"]
    result = await handler.edit_task("u", iid + " deadline:2030-01-02T12:00", None)
    assert result["status"] == "success"
    assert db.get_item(iid, "u").remind_times == ["2030-01-02T03:00:00+00:00"]


@pytest.mark.asyncio
async def test_web_lifecycle_requires_admin(db, monkeypatch):
    from plugins.pendo.handlers import web

    calls = []
    server = SimpleNamespace(stop=lambda: calls.append("stop"), is_managed_running=lambda: True)
    monkeypatch.setattr(web, "web_server", server)
    monkeypatch.setattr(web, "issue_login_code", lambda: None)
    monkeypatch.setattr(web, "generate_widget_token", lambda: None)
    for context in [None, SimpleNamespace(is_global_admin=lambda _uid: False)]:
        result = await web.WebHandler(db).handle("123", "stop", context)
        assert result["status"] == "error"
    assert calls == []


def test_recurring_count_remains_anchored():
    instances, _ = EventHandler._expand_recurring_instances(
        "FREQ=DAILY;COUNT=3",
        datetime.fromisoformat("2030-01-01T09:00:00+08:00"),
        datetime.fromisoformat("2030-01-02T12:00:00+08:00"),
    )
    assert [d.day for d in instances] == [3]


def test_compound_duration_and_explicit_reminder_list():
    parser = AIParser.__new__(AIParser)
    assert parser._parse_offset("2小时30分钟").total_seconds() == 9000
    assert parser.build_reminder_rules_from_description("提前2小时30分钟") == [
        {"offset_seconds": 9000},
        {"offset_seconds": 0},
    ]
    assert parser.build_reminder_rules_from_description("提前2小时、30分钟") == [
        {"offset_seconds": 7200},
        {"offset_seconds": 1800},
        {"offset_seconds": 0},
    ]


@pytest.mark.parametrize(
    ("text", "hour"), [("明天下午3点开会", 15), ("明天晚上8点开会", 20), ("明天上午12点开会", 0)]
)
def test_rule_parser_preserves_time_period(text, hour):
    result = RuleParser()._extract_relative_time(
        text, datetime.fromisoformat("2030-01-01T10:00:00+08:00")
    )
    assert datetime.fromisoformat(result["start_time"]).hour == hour


def test_export_and_widget_preserve_instant(db, tmp_path):
    db.update_user_settings("u", {"timezone": "Asia/Shanghai"})
    db.insert_item(
        {
            "owner_id": "u",
            "type": "event",
            "title": "凌晨",
            "start_time": "2030-01-02T01:00:00+08:00",
        }
    )
    result = ExporterService(db, tmp_path).export_markdown("u", "day 2030-01-02 event", {})
    assert result["record_count"] == 1
    assert "2030-01-02 01:00+08:00" in Path(result["file_path"]).read_text(encoding="utf-8")
    item = build_widget_calendar(db, "u", start_date="2030-01-02", end_date="2030-01-02")["items"][
        0
    ]
    assert (
        datetime.fromisoformat(item["start_time"]).astimezone(UTC).isoformat()
        == "2030-01-01T17:00:00+00:00"
    )


@pytest.mark.parametrize("offset", ["-07:00", "-08:00"])
def test_widget_preserves_each_dst_overlap_instant(db, offset):
    """夏令时回拨当天两次相同墙钟分别同步到原始真实时刻。"""
    db.update_user_settings("u", {"timezone": "America/Los_Angeles"})
    start = f"2026-11-01T01:30:00{offset}"
    end   = f"2026-11-01T01:45:00{offset}"
    db.insert_item(
        {
            "owner_id": "u",
            "type": "event",
            "title": "重复墙钟",
            "timezone": "America/Los_Angeles",
            "start_time": start,
            "end_time": end,
        }
    )
    item = build_widget_calendar(db, "u", start_date="2026-11-01", end_date="2026-11-01")["items"][
        0
    ]
    assert item["start_time"] == start
    assert item["end_time"] == end


def test_bundle_unicode_and_large_export_round_trip():
    records = [
        {
            "_type": "note",
            "_schema": 2,
            "id": str(i),
            "title": "n",
            "content": "a\u2028b\u2029c\u0085d",
        }
        for i in range(20001)
    ]
    payload = _build_bundle_bytes({"note": records}, ExportSelection(types=["note"]), None, None)
    _parsed, imported, errors = inspect_bundle_bytes(payload)
    assert errors == []
    assert len(imported) == 20001
    assert all(record["content"] == "a\u2028b\u2029c\u0085d" for record in imported)


def test_stale_full_editor_requires_version(db):
    iid = db.insert_item({"owner_id": "u", "type": "note", "title": "old", "content": "old"})
    old = db.get_item(iid, "u").version
    update_item(iid, ItemUpdate(title="new", content="new", version=old), "u", db)
    for body, status in [
        (ItemUpdate(title="stale", content="old"), 422),
        (ItemUpdate(title="stale", content="old", version=old), 409),
    ]:
        with pytest.raises(HTTPException) as exc:
            update_item(iid, body, "u", db)
        assert exc.value.status_code == status
    assert db.get_item(iid, "u").content == "new"


def test_browser_upload_unicode_and_logout_retry(tmp_path):
    source = Path("plugins/pendo/web/static/js/api.js").read_text(encoding="utf-8")
    script = tmp_path / "api.mjs"
    script.write_text(
        source
        + """
import assert from 'node:assert/strict';
let calls=[];
let offline=true;
globalThis.fetch=async(path, options)=>{
 calls.push(options.headers);
 if(path.endsWith('/session')) return {ok:true,status:200,json:async()=>({ok:true,data:{owner_id:'u',csrf_token:'token'}})};
 if(path.includes('/transfer') || !offline) return {ok:true,status:200,json:async()=>({ok:true})};
 throw new Error('offline');
};
await getSession();
for(const filename of ['备份.pendo.zip','📦.zip','a%20b.zip']) {
 await apiUpload('/transfer/inspect',new Uint8Array(),{'X-Transfer-Filename':filename});
 assert.equal(decodeURIComponent(calls.at(-1).get('X-Transfer-Filename')),filename);
}
await assert.rejects(logout());
await assert.rejects(logout());
assert.equal(calls.at(-1).get('X-CSRF-Token'),'token');
offline=false;
await logout();
await apiUpload('/transfer/inspect',new Uint8Array());
assert.equal(calls.at(-1).get('X-CSRF-Token'),null);
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["node", str(script)],
        capture_output = True,
        text           = True,
        encoding       = "utf-8",
        errors         = "replace",
        timeout        = 15,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
@pytest.mark.parametrize("filename", ["备份.pendo.zip", "📦.zip", "a%20b.zip"])
async def test_transfer_decodes_uploaded_filename_into_audit(db, filename):
    """真实导入事务只解码一次文件名，并把 Unicode 原文保存在审计中。"""
    from urllib.parse import quote

    from starlette.requests import Request

    from plugins.pendo.web.api.transfer import execute_import

    payload = _build_bundle_bytes(
        {"note": [{"_type": "note", "_schema": 2, "id": "uploaded", "title": "audit"}]},
        ExportSelection(types=["note"]),
        None,
        None,
    )

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/transfer/import/execute",
            "headers": [(b"x-transfer-filename", quote(filename, safe="").encode("ascii"))],
        },
        receive,
    )
    result = await execute_import(request, owner_id="u", db=db)
    assert result["ok"] is True
    assert db.get_transfer_logs("u")[0]["filename"] == filename


@pytest.mark.asyncio
async def test_currency_statistics_keep_independent_amounts(db, tmp_path):
    from plugins.pendo.commands.scheduled import (
        _FinancePeriod,
        _format_finance_summary,
        _summarize_finance_items,
    )
    from plugins.pendo.handlers.ledger import LedgerHandler

    for code in ["CNY", "USD"]:
        db.insert_item(
            {
                "owner_id": "u",
                "type": "ledger",
                "title": code,
                "currency": code,
                "amount_cents": 10000,
                "transaction_type": "expense",
                "ledger_date": "2030-01-01",
                "ledger_category": "餐饮",
            }
        )
    items   = db.get_items("u")
    metrics = _summarize_finance_items(items)
    assert metrics.total_expense == 0
    assert {code: value.total_expense for code, value in metrics.by_currency.items()} == {
        "CNY": 100,
        "USD": 100,
    }
    period = _FinancePeriod("2030-01", "2030-01-01", "2030-01-31", "January", "Report")
    report = _format_finance_summary(metrics, period)
    assert "¥100.00" in report and "USD 100.00" in report and "200.00" not in report
    handler = LedgerHandler(db)
    for result in [
        await handler.summary("u", "2030-01", None),
        await handler.list_ledger("u", "2030-01 all", None),
    ]:
        assert result["status"] == "success"
        assert "¥100.00" in result["message"] and "USD 100.00" in result["message"]
        assert "200.00" not in result["message"]
    result = ExporterService(db, tmp_path).export_markdown("u", "ledger 2030-01 ledger", {})
    assert "USD 100.00" in Path(result["file_path"]).read_text(encoding="utf-8")
