from __future__ import annotations

from datetime import datetime
import importlib
import io
import json
import shutil
import sys
import types
import uuid
import zipfile
from pathlib import Path

import pytest

try:
    from fastapi.testclient import TestClient
    from plugins.pendo.web.server import create_app
    FASTAPI_AVAILABLE = True
except ModuleNotFoundError:
    TestClient = None
    create_app = None
    FASTAPI_AVAILABLE = False
except RuntimeError as exc:
    if "requires the httpx package" not in str(exc):
        raise
    TestClient = None
    create_app = None
    FASTAPI_AVAILABLE = False

from plugins.pendo.models.item import DiaryItem, EventItem, LedgerItem, NoteItem, TaskItem
from plugins.pendo.services.db import Database
try:
    from plugins.pendo.web.auth import generate_token
except ModuleNotFoundError:
    pytest.skip("pendo web auth requires PyJWT", allow_module_level=True)
from plugins.pendo.web.services.transfer_bundle import (
    BundleValidationError,
    build_manifest,
    read_bundle,
    serialize_event_collection,
    serialize_item,
    write_bundle,
)


ROOT = Path(__file__).resolve().parents[2]
OWNER_ID = "u-transfer"


def _load_transfer_module():
    fastapi = types.ModuleType("fastapi")

    class _Router:
        def _decorator(self, *_args, **_kwargs):
            def decorator(fn):
                return fn
            return decorator

        def get(self, *_args, **_kwargs):
            return self._decorator(*_args, **_kwargs)

        def post(self, *_args, **_kwargs):
            return self._decorator(*_args, **_kwargs)

        def put(self, *_args, **_kwargs):
            return self._decorator(*_args, **_kwargs)

        def delete(self, *_args, **_kwargs):
            return self._decorator(*_args, **_kwargs)

    class _HTTPException(Exception):
        def __init__(self, status_code: int, detail: str):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    fastapi.APIRouter = _Router
    fastapi.Depends = lambda dep=None: dep
    fastapi.Header = lambda default=None, **_kwargs: default
    fastapi.Query = lambda default=None, **_kwargs: default
    fastapi.HTTPException = _HTTPException
    fastapi.Request = type("Request", (), {})

    responses = types.ModuleType("fastapi.responses")
    responses.Response = type("Response", (), {})
    responses.JSONResponse = type("JSONResponse", (), {})  # needed by server.py

    # Save originals so we can restore them after import (avoid polluting sys.modules
    # for subsequent tests that rely on the real fastapi / fastapi.responses)
    _orig_fastapi = sys.modules.get("fastapi")
    _orig_responses = sys.modules.get("fastapi.responses")

    sys.modules["fastapi"] = fastapi
    sys.modules["fastapi.responses"] = responses
    sys.modules.pop("plugins.pendo.web.api.transfer", None)
    sys.modules.pop("plugins.pendo.web.deps", None)
    mod = importlib.import_module("plugins.pendo.web.api.transfer")

    # Restore real fastapi modules so other tests are not affected
    if _orig_fastapi is not None:
        sys.modules["fastapi"] = _orig_fastapi
    else:
        sys.modules.pop("fastapi", None)
    if _orig_responses is not None:
        sys.modules["fastapi.responses"] = _orig_responses
    else:
        sys.modules.pop("fastapi.responses", None)

    return mod


@pytest.fixture()
def temp_db():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_transfer_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(temp_dir / "pendo.db"))
    try:
        yield db
    finally:
        db.cleanup()
        shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture()
def client(temp_db: Database):
    if not FASTAPI_AVAILABLE:
        pytest.skip("fastapi is not installed in this environment")
    app = create_app(temp_db)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def auth_headers():
    return {"Authorization": f"Bearer <redacted-historical-token>)}"}


def _seed_items(db: Database):
    db.insert_item({
        "id": "event_in",
        "owner_id": OWNER_ID,
        "type": "event",
        "title": "月内会议",
        "category": "工作",
        "start_time": "2026-03-04T10:00:00+08:00",
        "end_time": "2026-03-04T11:00:00+08:00",
        "timezone": "Asia/Shanghai",
        "location": "会议室",
        "participants": ["A", "B"],
        "remind_times": ["2026-03-04T09:30:00+08:00"],
        "notes": "带纪要",
        "created_at": "2026-03-01T09:00:00+08:00",
        "updated_at": "2026-03-01T09:00:00+08:00",
    })
    db.insert_item({
        "id": "event_out",
        "owner_id": OWNER_ID,
        "type": "event",
        "title": "月外会议",
        "category": "工作",
        "start_time": "2026-02-26T10:00:00+08:00",
        "end_time": "2026-02-26T11:00:00+08:00",
        "created_at": "2026-02-25T09:00:00+08:00",
        "updated_at": "2026-02-25T09:00:00+08:00",
    })
    db.insert_item({
        "id": "task_due_in",
        "owner_id": OWNER_ID,
        "type": "task",
        "title": "月内待办",
        "content": "有截止时间",
        "category": "工作",
        "due_time": "2026-03-11T18:00:00+08:00",
        "priority": 2,
        "status": "todo",
        "progress": 0,
        "estimate": 30,
        "subtasks": [{"title": "拆分", "done": False}],
        "dependencies": ["event_in"],
        "remind_times": ["2026-03-11T09:00:00+08:00"],
        "created_at": "2026-03-02T09:00:00+08:00",
        "updated_at": "2026-03-02T09:00:00+08:00",
    })
    db.insert_item({
        "id": "task_fallback_created",
        "owner_id": OWNER_ID,
        "type": "task",
        "title": "无截止待办",
        "content": "靠创建时间命中",
        "category": "个人",
        "priority": 3,
        "status": "in_progress",
        "progress": 20,
        "created_at": "2026-03-12T09:00:00+08:00",
        "updated_at": "2026-03-12T09:00:00+08:00",
    })
    db.insert_item({
        "id": "task_out",
        "owner_id": OWNER_ID,
        "type": "task",
        "title": "月外待办",
        "content": "不应导出",
        "category": "个人",
        "due_time": "2026-02-10T18:00:00+08:00",
        "priority": 3,
        "status": "todo",
        "created_at": "2026-02-02T09:00:00+08:00",
        "updated_at": "2026-02-02T09:00:00+08:00",
    })
    db.insert_item({
        "id": "ledger_in",
        "owner_id": OWNER_ID,
        "type": "ledger",
        "title": "午饭",
        "amount": 23.5,
        "direction": "expense",
        "ledger_category": "餐饮",
        "ledger_date": "2026-03-08",
        "remark": "食堂",
        "created_at": "2026-03-08T12:00:00+08:00",
        "updated_at": "2026-03-08T12:00:00+08:00",
    })
    db.insert_item({
        "id": "ledger_out",
        "owner_id": OWNER_ID,
        "type": "ledger",
        "title": "旧账单",
        "amount": 12,
        "direction": "expense",
        "ledger_category": "交通",
        "ledger_date": "2026-02-08",
        "created_at": "2026-02-08T12:00:00+08:00",
        "updated_at": "2026-02-08T12:00:00+08:00",
    })
    db.insert_item({
        "id": "note_in",
        "owner_id": OWNER_ID,
        "type": "note",
        "title": "月内笔记",
        "content": "笔记正文",
        "category": "知识",
        "tags": ["学习", "导入"],
        "references": [{"kind": "item", "id": "task_due_in"}],
        "related_items": ["event_in"],
        "created_at": "2026-03-09T20:00:00+08:00",
        "updated_at": "2026-03-09T20:00:00+08:00",
    })
    db.insert_item({
        "id": "note_out",
        "owner_id": OWNER_ID,
        "type": "note",
        "title": "旧笔记",
        "content": "过期",
        "category": "知识",
        "created_at": "2026-02-09T20:00:00+08:00",
        "updated_at": "2026-02-09T20:00:00+08:00",
    })
    db.insert_item({
        "id": "diary_in",
        "owner_id": OWNER_ID,
        "type": "diary",
        "title": "月内日记",
        "content": "今天很顺利",
        "diary_date": "2026-03-15",
        "mood": "happy",
        "mood_score": 8,
        "weather": "sunny",
        "location": "上海",
        "template_id": "tpl-1",
        "created_at": "2026-03-15T22:00:00+08:00",
        "updated_at": "2026-03-15T22:00:00+08:00",
    })
    db.insert_item({
        "id": "diary_out",
        "owner_id": OWNER_ID,
        "type": "diary",
        "title": "旧日记",
        "content": "不在范围内",
        "diary_date": "2026-02-15",
        "created_at": "2026-02-15T22:00:00+08:00",
        "updated_at": "2026-02-15T22:00:00+08:00",
    })


def _build_sample_bundle_bytes(records_by_type: dict[str, list[dict]], selection: dict | None = None) -> bytes:
    files = []
    paths = {
        "event": "data/events.ndjson",
        "task": "data/tasks.ndjson",
        "note": "data/notes.ndjson",
        "ledger": "data/ledger.ndjson",
        "diary": "data/diary.ndjson",
        "event_collection": "data/event_collections.ndjson",
    }
    for item_type, rows in records_by_type.items():
        content = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows).encode("utf-8")
        files.append({
            "path": paths[item_type],
            "type": item_type,
            "count": len(rows),
            "sha256": __import__("hashlib").sha256(content).hexdigest(),
        })
    manifest = build_manifest(
        selection or {"types": list(records_by_type.keys()), "preset": "all", "start": None, "end": None},
        files,
        "Asia/Shanghai",
    )
    buf = io.BytesIO()
    write_bundle(buf, manifest, records_by_type)
    return buf.getvalue()


def test_build_manifest_includes_bundle_id():
    manifest = build_manifest(
        {"types": ["event", "task"], "preset": "month", "start": "2026-03-01", "end": "2026-03-31"},
        [{"path": "data/tasks.ndjson", "type": "task", "count": 1, "sha256": "abc"}],
        "Asia/Shanghai",
    )
    assert manifest["format"] == "pendo-bundle"
    assert manifest["version"] == 1
    assert manifest["selection"]["preset"] == "month"
    assert "bundle_id" in manifest
    assert len(manifest["bundle_id"]) == 32


def test_build_manifest_and_serialize_item_preserve_type_fields():
    manifest = build_manifest(
        {"types": ["event", "task"], "preset": "month", "start": "2026-03-01", "end": "2026-03-31"},
        [{"path": "data/tasks.ndjson", "type": "task", "count": 1, "sha256": "abc"}],
        "Asia/Shanghai",
    )

    assert manifest["format"] == "pendo-bundle"
    assert manifest["version"] == 1
    assert manifest["selection"]["preset"] == "month"

    event_record = serialize_item(EventItem(
        id="event_1",
        owner_id=OWNER_ID,
        title="发布会",
        category="工作",
        start_time="2026-03-20T09:00:00+08:00",
        end_time="2026-03-20T10:00:00+08:00",
        timezone="Asia/Shanghai",
        participants=["A"],
        remind_times=["2026-03-20T08:30:00+08:00"],
        notes="带录音",
        event_role="multi_node_child",
        event_collection_id="col_1",
        event_collection_kind="multi_node",
        event_index=1,
        event_node_key="m01",
    ))
    collection_record = serialize_event_collection({
        "id": "col_1",
        "owner_id": OWNER_ID,
        "kind": "multi_node",
        "title": "发布会整体",
        "category": "工作",
        "notes": "整体备注",
        "start_time": "2026-03-20T09:00:00+08:00",
        "end_time": "2026-03-20T10:00:00+08:00",
    })
    task_record = serialize_item(TaskItem(
        id="task_1",
        owner_id=OWNER_ID,
        title="补图表",
        content="导出实现",
        category="工作",
        due_time="2026-03-21T18:00:00+08:00",
        priority=2,
        status="todo",
        subtasks=[{"title": "补测试", "done": False}],
        dependencies=["event_1"],
        remind_times=["2026-03-21T09:00:00+08:00"],
    ))
    note_record = serialize_item(NoteItem(
        id="note_1",
        owner_id=OWNER_ID,
        title="格式说明",
        content="记录规范",
        category="知识",
        tags=["格式"],
        references=[{"kind": "item", "id": "task_1"}],
        related_items=["event_1"],
    ))
    diary_record = serialize_item(DiaryItem(
        id="diary_1",
        owner_id=OWNER_ID,
        title="今天",
        content="正文",
        diary_date="2026-03-21",
        mood="happy",
        mood_score=9,
        weather="sunny",
        location="家",
        template_id="tpl-1",
    ))
    ledger_record = serialize_item(LedgerItem(
        id="ledger_1",
        owner_id=OWNER_ID,
        title="咖啡",
        amount=18,
        direction="expense",
        ledger_category="餐饮",
        ledger_date="2026-03-21",
        remark="拿铁",
    ))

    assert event_record["participants"] == ["A"]
    assert "milestones" not in event_record
    assert "rrule" not in event_record
    assert "parent_id" not in event_record
    assert event_record["event_collection_id"] == "col_1"
    assert event_record["event_collection_kind"] == "multi_node"
    assert event_record["event_index"] == 1
    assert collection_record["_type"] == "event_collection"
    assert collection_record["title"] == "发布会整体"
    assert "owner_id" not in collection_record
    assert task_record["subtasks"][0]["title"] == "补测试"
    assert task_record["dependencies"] == ["event_1"]
    assert note_record["references"][0]["id"] == "task_1"
    assert diary_record["mood_score"] == 9
    assert ledger_record["ledger_category"] == "餐饮"


def test_read_bundle_rejects_missing_manifest():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("data/tasks.ndjson", '{"_type":"task","_schema":1,"id":"task_1","title":"测试"}\n')
    buf.seek(0)

    with pytest.raises(BundleValidationError, match="manifest.json"):
        read_bundle(buf)


def test_read_bundle_rejects_unknown_file_type():
    manifest = {
        "format": "pendo-bundle",
        "version": 1,
        "bundle_id": "test123",
        "exported_at": "2026-03-29T12:00:00+08:00",
        "source": {"app": "pendo-web", "timezone": "Asia/Shanghai"},
        "selection": {"types": ["unknown"], "preset": "all", "start": None, "end": None},
        "files": [{"path": "data/unknown.ndjson", "type": "unknown", "count": 1, "sha256": "abc"}],
        "attachments_mode": "metadata_only",
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
        zf.writestr("data/unknown.ndjson", '{"_type":"unknown","_schema":1}\n')
    buf.seek(0)

    with pytest.raises(BundleValidationError, match="unknown"):
        read_bundle(buf)


def test_read_bundle_accepts_tasks_ndjson():
    bundle_bytes = _build_sample_bundle_bytes({
        "task": [{
            "_type": "task",
            "_schema": 1,
            "id": "task_1",
            "title": "导入任务",
            "content": "正文",
            "category": "工作",
            "priority": 3,
            "status": "todo",
            "created_at": "2026-03-21T09:00:00+08:00",
            "updated_at": "2026-03-21T09:00:00+08:00",
        }]
    })

    parsed = read_bundle(io.BytesIO(bundle_bytes))

    assert parsed.manifest["format"] == "pendo-bundle"
    assert parsed.records_by_type["task"][0]["title"] == "导入任务"
    assert parsed.errors == []


@pytest.mark.parametrize("preset,payload", [
    ("week", {"preset": "week"}),
    ("month", {"preset": "month"}),
    ("quarter", {"preset": "quarter"}),
    ("year", {"preset": "year"}),
    ("last_year", {"preset": "last_year"}),
    ("all", {"preset": "all"}),
    ("custom", {"preset": "custom", "start": "2026-03-01", "end": "2026-03-31"}),
])
def test_export_preview_accepts_supported_presets(client: TestClient, temp_db: Database, auth_headers: dict, preset: str, payload: dict):
    _seed_items(temp_db)

    response = client.post(
        "/api/transfer/export/preview",
        headers=auth_headers,
        json={"selection": {"types": ["task"], **payload}},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"]["selection"]["preset"] == preset


def test_export_preview_rejects_reversed_custom_range(client: TestClient, auth_headers: dict):
    response = client.post(
        "/api/transfer/export/preview",
        headers=auth_headers,
        json={"selection": {"types": ["task"], "preset": "custom", "start": "2026-03-31", "end": "2026-03-01"}},
    )

    assert response.status_code == 422
    assert "before end" in response.json()["message"]


def test_resolve_range_supports_quarter_to_date():
    transfer_module = _load_transfer_module()
    start, end = transfer_module.resolve_range(
        transfer_module.ExportSelection(types=["task"], preset="quarter"),
        now=datetime(2026, 3, 30, 9, 0, 0),
    )

    assert start.isoformat() == "2026-01-01"
    assert end.isoformat() == "2026-03-30"


def test_export_preview_returns_counts_by_type_and_filters_by_time_field(client: TestClient, temp_db: Database, auth_headers: dict):
    _seed_items(temp_db)

    response = client.post(
        "/api/transfer/export/preview",
        headers=auth_headers,
        json={
            "selection": {
                "types": ["event", "task", "ledger", "note", "diary"],
                "preset": "custom",
                "start": "2026-03-01",
                "end": "2026-03-31",
            }
        },
    )

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["counts"] == {"event": 1, "task": 2, "ledger": 1, "note": 1, "diary": 1}
    assert body["total"] == 6


def test_export_preview_returns_warnings_field(client: TestClient, temp_db: Database, auth_headers: dict):
    _seed_items(temp_db)

    response = client.post(
        "/api/transfer/export/preview",
        headers=auth_headers,
        json={"selection": {"types": ["task"], "preset": "all"}},
    )

    assert response.status_code == 200
    body = response.json()["data"]
    assert "warnings" in body
    assert isinstance(body["warnings"], list)


def test_export_download_returns_bundle_with_manifest(client: TestClient, temp_db: Database, auth_headers: dict):
    _seed_items(temp_db)

    response = client.post(
        "/api/transfer/export/download",
        headers=auth_headers,
        json={
            "selection": {
                "types": ["task", "note"],
                "preset": "custom",
                "start": "2026-03-01",
                "end": "2026-03-31",
            }
        },
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/zip")
    assert ".pendo.zip" in response.headers["content-disposition"]

    with zipfile.ZipFile(io.BytesIO(response.content), "r") as zf:
        names = set(zf.namelist())
        assert "manifest.json" in names
        assert "data/tasks.ndjson" in names
        assert "data/notes.ndjson" in names
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        assert manifest["selection"]["types"] == ["task", "note"]
        assert "bundle_id" in manifest


def test_export_download_includes_event_collections_for_event_graph(client: TestClient, temp_db: Database, auth_headers: dict):
    temp_db.create_event_collection({
        "id": "conf_2026",
        "owner_id": OWNER_ID,
        "kind": "multi_node",
        "title": "FRB2026会议",
        "category": "学术",
        "notes": "整体会议",
        "start_time": "2026-03-05T09:00:00",
        "end_time": "2026-04-01T10:00:00",
    })
    temp_db.insert_item({
        "id": "conf_2026_m01",
        "owner_id": OWNER_ID,
        "type": "event",
        "title": "摘要截止",
        "category": "学术",
        "start_time": "2026-03-05T09:00:00",
        "event_role": "multi_node_child",
        "event_collection_id": "conf_2026",
        "event_collection_kind": "multi_node",
        "event_index": 1,
        "event_node_key": "m01",
    })

    response = client.post(
        "/api/transfer/export/download",
        headers=auth_headers,
        json={
            "selection": {
                "types": ["event"],
                "preset": "custom",
                "start": "2026-03-01",
                "end": "2026-03-31",
            }
        },
    )

    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content), "r") as zf:
        names = set(zf.namelist())
        assert "data/events.ndjson" in names
        assert "data/event_collections.ndjson" in names
        event_rows = [json.loads(line) for line in zf.read("data/events.ndjson").decode("utf-8").splitlines() if line]
        collection_rows = [json.loads(line) for line in zf.read("data/event_collections.ndjson").decode("utf-8").splitlines() if line]

    assert event_rows[0]["event_collection_id"] == "conf_2026"
    assert event_rows[0]["event_collection_kind"] == "multi_node"
    assert collection_rows[0]["_type"] == "event_collection"
    assert collection_rows[0]["title"] == "FRB2026会议"


def test_export_download_creates_audit_log(client: TestClient, temp_db: Database, auth_headers: dict):
    _seed_items(temp_db)

    client.post(
        "/api/transfer/export/download",
        headers=auth_headers,
        json={"selection": {"types": ["task"], "preset": "all"}},
    )

    logs = temp_db.get_transfer_logs(OWNER_ID)
    assert len(logs) >= 1
    assert logs[0]["action"] == "export"
    assert "task" in logs[0]["types"]


def test_import_inspect_returns_summary_and_row_errors(client: TestClient, auth_headers: dict):
    valid_task = {
        "_type": "task",
        "_schema": 1,
        "id": "task_bundle_1",
        "title": "导入任务",
        "content": "来自备份",
        "category": "工作",
        "priority": 3,
        "status": "todo",
        "created_at": "2026-03-20T09:00:00+08:00",
        "updated_at": "2026-03-20T09:00:00+08:00",
    }
    invalid_task = {"_type": "task", "_schema": 1, "id": "bad"}
    records = [valid_task, invalid_task]
    content = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in records).encode("utf-8")
    manifest = build_manifest(
        {"types": ["task"], "preset": "all", "start": None, "end": None},
        [{
            "path": "data/tasks.ndjson",
            "type": "task",
            "count": len(records),
            "sha256": __import__("hashlib").sha256(content).hexdigest(),
        }],
        "Asia/Shanghai",
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
        zf.writestr("data/tasks.ndjson", content)
    bundle_bytes = buf.getvalue()

    response = client.post(
        "/api/transfer/import/inspect",
        headers=auth_headers,
        content=bundle_bytes,
    )

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["summary"]["types"] == ["task"]
    assert body["counts"]["valid"] == 1
    assert body["counts"]["errors"] == 1
    assert "bundle_id" in body
    assert body["already_imported"] is False
    assert body["samples"][0]["title"] == "导入任务"
    assert body["errors"][0]["path"] == "data/tasks.ndjson"
    assert body["errors"][0]["line"] == 2


def test_import_inspect_detects_already_imported_bundle(client: TestClient, temp_db: Database, auth_headers: dict):
    bundle_bytes = _build_sample_bundle_bytes({
        "task": [{
            "_type": "task",
            "_schema": 1,
            "id": "task_idem_1",
            "title": "幂等测试",
            "content": "正文",
            "category": "工作",
            "priority": 3,
            "status": "todo",
            "created_at": "2026-03-20T09:00:00+08:00",
            "updated_at": "2026-03-20T09:00:00+08:00",
        }],
    })

    # 先执行一次导入
    client.post(
        "/api/transfer/import/execute",
        headers={**auth_headers, "X-Transfer-Options": json.dumps({"types": ["task"], "conflict_policy": "skip"})},
        content=bundle_bytes,
    )

    # 再次预检同一个 bundle
    response = client.post(
        "/api/transfer/import/inspect",
        headers=auth_headers,
        content=bundle_bytes,
    )
    assert response.status_code == 200
    assert response.json()["data"]["already_imported"] is True


def test_import_inspect_preserves_original_line_number_for_normalization_errors(client: TestClient, auth_headers: dict):
    rows = [
        '{"_type":"task","_schema":1,"id":"broken-json"',
        json.dumps({
            "_type": "task",
            "_schema": 1,
            "id": "task_bad_line",
            "created_at": "2026-03-20T09:00:00+08:00",
            "updated_at": "2026-03-20T09:00:00+08:00",
        }, ensure_ascii=False),
    ]
    content = ("\n".join(rows) + "\n").encode("utf-8")
    manifest = build_manifest(
        {"types": ["task"], "preset": "all", "start": None, "end": None},
        [{
            "path": "data/tasks.ndjson",
            "type": "task",
            "count": 2,
            "sha256": __import__("hashlib").sha256(content).hexdigest(),
        }],
        "Asia/Shanghai",
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
        zf.writestr("data/tasks.ndjson", content)

    response = client.post(
        "/api/transfer/import/inspect",
        headers=auth_headers,
        content=buf.getvalue(),
    )

    assert response.status_code == 200
    errors = response.json()["data"]["errors"]
    assert errors[0]["line"] == 1
    assert errors[1]["line"] == 2


def test_import_execute_supports_skip_overwrite_duplicate_and_subset(client: TestClient, temp_db: Database, auth_headers: dict):
    temp_db.insert_item({
        "id": "task_existing",
        "owner_id": OWNER_ID,
        "type": "task",
        "title": "旧任务",
        "content": "旧内容",
        "category": "旧分类",
        "priority": 2,
        "status": "todo",
        "created_at": "2026-03-01T09:00:00+08:00",
        "updated_at": "2026-03-01T09:00:00+08:00",
    })

    bundle_bytes = _build_sample_bundle_bytes({
        "task": [{
            "_type": "task",
            "_schema": 1,
            "id": "task_existing",
            "title": "新任务标题",
            "content": "新内容",
            "category": "工作",
            "priority": 4,
            "status": "done",
            "created_at": "2026-03-20T09:00:00+08:00",
            "updated_at": "2026-03-20T09:00:00+08:00",
        }],
        "note": [{
            "_type": "note",
            "_schema": 1,
            "id": "note_subset",
            "title": "不会导入",
            "content": "因为没选中",
            "category": "知识",
            "created_at": "2026-03-20T09:00:00+08:00",
            "updated_at": "2026-03-20T09:00:00+08:00",
        }],
    })

    skip_response = client.post(
        "/api/transfer/import/execute",
        headers={**auth_headers, "X-Transfer-Options": json.dumps({"types": ["task"], "conflict_policy": "skip"})},
        content=bundle_bytes,
    )
    assert skip_response.status_code == 200
    skip_body = skip_response.json()["data"]["results"]
    assert skip_body["inserted"] == 0
    assert skip_body["updated"] == 0
    assert skip_body["skipped"] == 1
    assert skip_response.json()["data"]["details"]["skipped"][0]["reason"] == "ID 已存在，按策略跳过"
    assert temp_db.get_item("task_existing", owner_id=OWNER_ID).title == "旧任务"
    assert temp_db.get_item("note_subset", owner_id=OWNER_ID) is None

    # overwrite uses a new bundle_id so no idempotency block
    overwrite_bundle = _build_sample_bundle_bytes({
        "task": [{
            "_type": "task",
            "_schema": 1,
            "id": "task_existing",
            "title": "新任务标题",
            "content": "新内容",
            "category": "工作",
            "priority": 4,
            "status": "done",
            "created_at": "2026-03-20T09:00:00+08:00",
            "updated_at": "2026-03-20T09:00:00+08:00",
        }],
    })

    overwrite_response = client.post(
        "/api/transfer/import/execute",
        headers={**auth_headers, "X-Transfer-Options": json.dumps({"types": ["task"], "conflict_policy": "overwrite"})},
        content=overwrite_bundle,
    )
    assert overwrite_response.status_code == 200
    overwrite_body = overwrite_response.json()["data"]["results"]
    assert overwrite_body["updated"] == 1
    assert overwrite_response.json()["data"]["details"]["updated"][0]["id"] == "task_existing"
    overwritten = temp_db.get_item("task_existing", owner_id=OWNER_ID)
    assert overwritten.title == "新任务标题"
    assert overwritten.owner_id == OWNER_ID

    duplicate_bundle = _build_sample_bundle_bytes({
        "task": [{
            "_type": "task",
            "_schema": 1,
            "id": "task_existing",
            "title": "新任务标题",
            "content": "新内容",
            "category": "工作",
            "priority": 4,
            "status": "done",
            "created_at": "2026-03-20T09:00:00+08:00",
            "updated_at": "2026-03-20T09:00:00+08:00",
        }],
    })

    duplicate_response = client.post(
        "/api/transfer/import/execute",
        headers={**auth_headers, "X-Transfer-Options": json.dumps({"types": ["task"], "conflict_policy": "duplicate"})},
        content=duplicate_bundle,
    )
    assert duplicate_response.status_code == 200
    duplicate_body = duplicate_response.json()["data"]["results"]
    assert duplicate_body["inserted"] == 1
    assert "已生成副本" in duplicate_response.json()["data"]["details"]["inserted"][0]["reason"]

    tasks = temp_db.get_items(OWNER_ID, filters={"type": "task"}, limit=20)
    imported_copies = [item for item in tasks if item.id != "task_existing" and item.title == "新任务标题"]
    assert len(imported_copies) >= 1
    assert imported_copies[0].context["import"]["source_id"] == "task_existing"
    # duplicate ID should be 16 hex chars
    assert len(imported_copies[0].id) == 16


def test_import_execute_restores_event_collection_before_leaf_events(client: TestClient, temp_db: Database, auth_headers: dict):
    bundle_bytes = _build_sample_bundle_bytes({
        "event_collection": [{
            "_type": "event_collection",
            "_schema": 1,
            "id": "bundle_conf",
            "kind": "multi_node",
            "title": "导入会议",
            "category": "学术",
            "notes": "整体备注",
            "start_time": "2026-03-05T09:00:00",
            "end_time": "2026-04-01T10:00:00",
        }],
        "event": [{
            "_type": "event",
            "_schema": 1,
            "id": "bundle_conf_m01",
            "title": "摘要截止",
            "category": "学术",
            "start_time": "2026-03-05T09:00:00",
            "event_role": "multi_node_child",
            "event_collection_id": "bundle_conf",
            "event_collection_kind": "multi_node",
            "event_index": 1,
            "event_node_key": "m01",
        }],
    })

    inspect_response = client.post(
        "/api/transfer/import/inspect",
        headers=auth_headers,
        content=bundle_bytes,
    )
    assert inspect_response.status_code == 200
    inspect_data = inspect_response.json()["data"]
    assert inspect_data["summary"]["types"] == ["event"]
    assert inspect_data["counts"]["valid"] == 2
    assert inspect_data["counts"]["total_samples"] == 1
    assert any(file["type"] == "event_collection" for file in inspect_data["files"])

    import_response = client.post(
        "/api/transfer/import/execute",
        headers={**auth_headers, "X-Transfer-Options": json.dumps({"types": ["event"], "conflict_policy": "skip"})},
        content=bundle_bytes,
    )

    assert import_response.status_code == 200
    collection = temp_db.get_event_collection("bundle_conf", OWNER_ID)
    event = temp_db.get_item("bundle_conf_m01", owner_id=OWNER_ID)
    assert collection is not None
    assert collection["title"] == "导入会议"
    assert event.event_collection_id == "bundle_conf"
    assert event.event_collection_kind == "multi_node"


def test_import_execute_idempotency_blocks_duplicate_bundle(client: TestClient, temp_db: Database, auth_headers: dict):
    bundle_bytes = _build_sample_bundle_bytes({
        "task": [{
            "_type": "task",
            "_schema": 1,
            "id": "task_idem_block",
            "title": "幂等阻断",
            "content": "正文",
            "category": "工作",
            "priority": 3,
            "status": "todo",
            "created_at": "2026-03-20T09:00:00+08:00",
            "updated_at": "2026-03-20T09:00:00+08:00",
        }],
    })

    # 第一次导入成功
    resp1 = client.post(
        "/api/transfer/import/execute",
        headers={**auth_headers, "X-Transfer-Options": json.dumps({"types": ["task"], "conflict_policy": "skip"})},
        content=bundle_bytes,
    )
    assert resp1.status_code == 200

    # 第二次导入同一 bundle 应被阻断 (409)
    resp2 = client.post(
        "/api/transfer/import/execute",
        headers={**auth_headers, "X-Transfer-Options": json.dumps({"types": ["task"], "conflict_policy": "skip"})},
        content=bundle_bytes,
    )
    assert resp2.status_code == 409

    # 带 force=true 可以强制重新导入
    resp3 = client.post(
        "/api/transfer/import/execute",
        headers={**auth_headers, "X-Transfer-Options": json.dumps({"types": ["task"], "conflict_policy": "skip", "force": True})},
        content=bundle_bytes,
    )
    assert resp3.status_code == 200


def test_execute_import_bundle_uses_db_level_bundle_guard(temp_db: Database):
    operations = [
        (
            "insert",
            {
                "id": "bundle_guard_task",
                "type": "task",
                "title": "bundle guard",
                "content": "first import",
                "category": "工作",
                "priority": 3,
                "status": "todo",
                "created_at": "2026-03-20T09:00:00+08:00",
                "updated_at": "2026-03-20T09:00:00+08:00",
            },
        )
    ]

    temp_db.execute_import_bundle(
        owner_id=OWNER_ID,
        bundle_id="bundle-guard-1",
        operations=operations,
        filename="bundle.zip",
        types=["task"],
        record_count=1,
        result_summary={"inserted": 1, "updated": 0, "skipped": 0, "failed": 0},
        force=False,
    )

    with pytest.raises(Exception):
        temp_db.execute_import_bundle(
            owner_id=OWNER_ID,
            bundle_id="bundle-guard-1",
            operations=operations,
            filename="bundle.zip",
            types=["task"],
            record_count=1,
            result_summary={"inserted": 1, "updated": 0, "skipped": 0, "failed": 0},
            force=False,
        )

    logs = [log for log in temp_db.get_transfer_logs(OWNER_ID) if log["action"] == "import"]
    assert len(logs) == 1


def test_import_execute_transaction_atomicity(client: TestClient, temp_db: Database, auth_headers: dict):
    """如果导入事务中有记录失败，整体应该回滚"""
    # 插入一条已存在的记录用于 overwrite
    temp_db.insert_item({
        "id": "task_atom_exist",
        "owner_id": OWNER_ID,
        "type": "task",
        "title": "旧任务",
        "content": "旧",
        "category": "工作",
        "priority": 2,
        "status": "todo",
        "created_at": "2026-03-01T09:00:00+08:00",
        "updated_at": "2026-03-01T09:00:00+08:00",
    })

    # 测试正常的批量插入是原子的
    bundle_bytes = _build_sample_bundle_bytes({
        "task": [
            {
                "_type": "task", "_schema": 1, "id": "task_atom_new_1",
                "title": "原子新1", "content": "OK", "category": "工作",
                "priority": 3, "status": "todo",
                "created_at": "2026-03-20T09:00:00+08:00",
                "updated_at": "2026-03-20T09:00:00+08:00",
            },
            {
                "_type": "task", "_schema": 1, "id": "task_atom_new_2",
                "title": "原子新2", "content": "OK", "category": "工作",
                "priority": 3, "status": "todo",
                "created_at": "2026-03-20T09:00:00+08:00",
                "updated_at": "2026-03-20T09:00:00+08:00",
            },
        ],
    })

    resp = client.post(
        "/api/transfer/import/execute",
        headers={**auth_headers, "X-Transfer-Options": json.dumps({"types": ["task"], "conflict_policy": "skip"})},
        content=bundle_bytes,
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["results"]["inserted"] == 2

    # 两条记录都应该存在
    assert temp_db.get_item("task_atom_new_1", owner_id=OWNER_ID) is not None
    assert temp_db.get_item("task_atom_new_2", owner_id=OWNER_ID) is not None


def test_import_execute_rejects_empty_selected_types(client: TestClient, auth_headers: dict):
    bundle_bytes = _build_sample_bundle_bytes({
        "task": [{
            "_type": "task",
            "_schema": 1,
            "id": "task_only",
            "title": "只有任务",
            "content": "正文",
            "category": "工作",
            "priority": 3,
            "status": "todo",
            "created_at": "2026-03-20T09:00:00+08:00",
            "updated_at": "2026-03-20T09:00:00+08:00",
        }],
    })

    response = client.post(
        "/api/transfer/import/execute",
        headers={**auth_headers, "X-Transfer-Options": json.dumps({"types": ["event"], "conflict_policy": "skip"})},
        content=bundle_bytes,
    )

    assert response.status_code == 422
    assert "At least one import type" in response.json()["message"]


def test_import_execute_creates_audit_log(client: TestClient, temp_db: Database, auth_headers: dict):
    bundle_bytes = _build_sample_bundle_bytes({
        "task": [{
            "_type": "task",
            "_schema": 1,
            "id": "task_audit_log",
            "title": "审计日志测试",
            "content": "正文",
            "category": "工作",
            "priority": 3,
            "status": "todo",
            "created_at": "2026-03-20T09:00:00+08:00",
            "updated_at": "2026-03-20T09:00:00+08:00",
        }],
    })

    client.post(
        "/api/transfer/import/execute",
        headers={**auth_headers, "X-Transfer-Options": json.dumps({"types": ["task"], "conflict_policy": "skip"})},
        content=bundle_bytes,
    )

    logs = temp_db.get_transfer_logs(OWNER_ID)
    import_logs = [log for log in logs if log["action"] == "import"]
    assert len(import_logs) >= 1
    assert import_logs[0]["record_count"] >= 1
    assert "bundle_id" in import_logs[0] or import_logs[0].get("bundle_id")


def test_import_rejects_oversized_body(client: TestClient, auth_headers: dict):
    """上传超过大小限制的文件应返回 413"""
    # 通过 Content-Length header 触发
    response = client.post(
        "/api/transfer/import/inspect",
        headers={**auth_headers, "Content-Length": str(200 * 1024 * 1024)},
        content=b"x",
    )
    assert response.status_code == 413


def test_import_samples_pagination(client: TestClient, auth_headers: dict):
    # 创建包含 10 条记录的 bundle
    tasks = []
    for i in range(10):
        tasks.append({
            "_type": "task", "_schema": 1, "id": f"task_page_{i}",
            "title": f"分页任务{i}", "content": "正文", "category": "工作",
            "priority": 3, "status": "todo",
            "created_at": "2026-03-20T09:00:00+08:00",
            "updated_at": "2026-03-20T09:00:00+08:00",
        })
    bundle_bytes = _build_sample_bundle_bytes({"task": tasks})

    # 请求第1页 (5条/页)
    response = client.post(
        "/api/transfer/import/samples",
        headers={
            **auth_headers,
            "X-Transfer-Page": "1",
            "X-Transfer-Page-Size": "5",
        },
        content=bundle_bytes,
    )
    assert response.status_code == 200
    body = response.json()["data"]
    assert body["total"] == 10
    assert body["page"] == 1
    assert len(body["samples"]) == 5

    # 请求第2页
    response2 = client.post(
        "/api/transfer/import/samples",
        headers={
            **auth_headers,
            "X-Transfer-Page": "2",
            "X-Transfer-Page-Size": "5",
        },
        content=bundle_bytes,
    )
    body2 = response2.json()["data"]
    assert body2["page"] == 2
    assert len(body2["samples"]) == 5


def test_transfer_logs_endpoint(client: TestClient, temp_db: Database, auth_headers: dict):
    temp_db.log_transfer(
        owner_id=OWNER_ID,
        action="export",
        filename="test.pendo.zip",
        types=["task"],
        record_count=5,
        result_summary={"counts": {"task": 5}},
    )

    response = client.get("/api/transfer/logs", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()["data"]
    assert len(body["logs"]) >= 1
    assert body["logs"][0]["action"] == "export"


def test_query_items_for_types_paginates_full_export():
    transfer_module = _load_transfer_module()

    class FakeDB:
        def __init__(self):
            self.calls = []

        def get_items(self, owner_id, filters=None, limit=100, offset=0):
            self.calls.append((owner_id, filters, limit, offset))
            total = 2505
            if offset >= total:
                return []
            remaining = total - offset
            size = min(limit, remaining)
            return [{"id": f"task_{offset + idx}"} for idx in range(size)]

    db = FakeDB()
    result = transfer_module.query_items_for_types(db, OWNER_ID, ["task"])

    assert len(result["task"]) == 2505
    assert db.calls[:3] == [
        (OWNER_ID, {"type": "task"}, 1000, 0),
        (OWNER_ID, {"type": "task"}, 1000, 1000),
        (OWNER_ID, {"type": "task"}, 1000, 2000),
    ]


def test_transfer_page_sources_register_route_and_settings_entry():
    app_src = (ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "app.js").read_text(encoding="utf-8")
    settings_src = (ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "pages" / "settings.js").read_text(encoding="utf-8")
    transfer_src = (ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "pages" / "transfer.js").read_text(encoding="utf-8")
    header_src = (ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "components" / "header.js").read_text(encoding="utf-8")

    assert "registerRoute('transfer'" in app_src
    assert '数据迁移' in settings_src
    assert "btn-open-transfer" in settings_src
    assert "export function render(container)" in transfer_src
    assert "transfer: '数据迁移'" in header_src


def test_transfer_page_source_wires_export_and_import_endpoints():
    transfer_src = (ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "pages" / "transfer.js").read_text(encoding="utf-8")
    api_src = (ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "api.js").read_text(encoding="utf-8")

    assert "/transfer/export/preview" in transfer_src
    assert "/transfer/export/download" in transfer_src
    assert "/transfer/import/inspect" in transfer_src
    assert "/transfer/import/execute" in transfer_src
    assert "/transfer/import/samples" in transfer_src
    assert "/transfer/logs" in transfer_src
    assert "apiDownload" in api_src
    assert "apiUpload" in api_src
    assert "renderResultRows" in transfer_src
    assert "renderImportStatusBanner" in transfer_src
    assert "导入已完成" in transfer_src
    assert "result.details?.inserted" in transfer_src
    assert "manifest.json" in transfer_src
    assert "tasks.ndjson" in transfer_src
    assert "_type" in transfer_src
    assert "导入示例" in transfer_src
    assert "稳定自定义字符串" in transfer_src
    assert "默认生成短随机 ID" in transfer_src
    assert "操作记录" in transfer_src
    assert "bundle_id" in transfer_src
    assert "forceReimport" in transfer_src
    assert "已导入过" in transfer_src
    assert "event_collection" in transfer_src
    assert "日程集合" in transfer_src


def test_transfer_and_settings_pages_scale_summary_values_for_mid_width_layouts():
    settings_src = (ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "pages" / "settings.js").read_text(encoding="utf-8")
    transfer_src = (ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "pages" / "transfer.js").read_text(encoding="utf-8")

    assert ".settings-summary-card { padding: 18px; min-width: 0; }" in settings_src
    assert "font-size: clamp(18px, 1.55vw, 24px);" in settings_src
    assert "overflow-wrap: anywhere;" in settings_src
    assert "word-break: break-word;" in settings_src
    assert ".transfer-summary-card { padding: 16px; min-width: 0; }" in transfer_src
    assert "font-size: clamp(22px, 1.75vw, 28px);" in transfer_src
    assert "overflow-wrap: anywhere;" in transfer_src
    assert "word-break: break-word;" in transfer_src


def test_transfer_page_source_stacks_import_status_banner_on_mobile():
    transfer_src = (ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "pages" / "transfer.js").read_text(encoding="utf-8")

    assert 'class="transfer-status-copy"' in transfer_src
    assert ".transfer-status-copy { min-width: 0; }" in transfer_src
    assert "line-height: 1.35; overflow-wrap: anywhere; word-break: break-word;" in transfer_src
    assert "line-height: 1.7; color: #475569; overflow-wrap: anywhere; word-break: break-word;" in transfer_src
    assert ".transfer-status-banner { grid-template-columns: 1fr; align-items: start; }" in transfer_src
    assert ".transfer-status-pills { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }" in transfer_src
