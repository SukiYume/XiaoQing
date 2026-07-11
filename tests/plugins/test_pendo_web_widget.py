from __future__ import annotations

import asyncio
import importlib
import shutil
import sys
import types
import uuid
from pathlib import Path

import pytest

from plugins.pendo.services.db import Database

try:
    from plugins.pendo.web.auth import generate_token, generate_widget_token
except ModuleNotFoundError:
    pytest.skip("pendo web widget requires PyJWT", allow_module_level=True)

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


ROOT = Path(__file__).resolve().parents[2]


def _load_widget_module():
    fastapi = types.ModuleType("fastapi")

    class _Router:
        def get(self, *_args, **_kwargs):
            def decorator(fn):
                return fn
            return decorator

        def post(self, *_args, **_kwargs):
            return self.get(*_args, **_kwargs)

        def put(self, *_args, **_kwargs):
            return self.get(*_args, **_kwargs)

        def delete(self, *_args, **_kwargs):
            return self.get(*_args, **_kwargs)

        def include_router(self, *_args, **_kwargs):
            return None

    class _HTTPException(Exception):
        def __init__(self, status_code: int, detail: str):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    fastapi.APIRouter = _Router
    fastapi.Depends = lambda dep=None: dep
    fastapi.Header = lambda default=None, **_kwargs: default
    fastapi.Query = lambda default=None, **_kwargs: default
    fastapi.Request = type("Request", (), {})
    fastapi.HTTPException = _HTTPException

    responses = types.ModuleType("fastapi.responses")
    responses.JSONResponse = type("JSONResponse", (), {})
    responses.Response = type("Response", (), {})

    _orig_fastapi = sys.modules.get("fastapi")
    _orig_responses = sys.modules.get("fastapi.responses")

    sys.modules["fastapi"] = fastapi
    sys.modules["fastapi.responses"] = responses
    sys.modules.pop("plugins.pendo.web.deps", None)
    sys.modules.pop("plugins.pendo.web.api.widget", None)
    mod = importlib.import_module("plugins.pendo.web.api.widget")

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
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_widget_{uuid.uuid4().hex}"
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


def _seed_widget_data(db: Database, owner_id: str):
    for item in [
        {
            "id": "event_today",
            "owner_id": owner_id,
            "type": "event",
            "title": "今天会议",
            "start_time": "2026-03-25T10:00:00",
            "end_time": "2026-03-25T11:00:00",
            "location": "A1",
        },
        {
            "id": "event_tomorrow",
            "owner_id": owner_id,
            "type": "event",
            "title": "明天复盘",
            "start_time": "2026-03-26T14:00:00",
            "end_time": "2026-03-26T15:00:00",
            "location": "线上",
        },
        {
            "id": "event_later",
            "owner_id": owner_id,
            "type": "event",
            "title": "周末活动",
            "start_time": "2026-03-28T09:00:00",
            "end_time": "2026-03-28T10:30:00",
        },
        {
            "id": "task_focus",
            "owner_id": owner_id,
            "type": "task",
            "title": "处理周报",
            "status": "open",
            "priority": 1,
            "plan_date": "2026-03-25",
            "deadline_at": "2026-03-25T18:00:00",
            "created_at": "2026-03-24T08:00:00",
            "updated_at": "2026-03-25T08:30:00",
        },
        {
            "id": "task_next",
            "owner_id": owner_id,
            "type": "task",
            "title": "整理收据",
            "status": "open",
            "priority": 2,
            "plan_date": "2026-03-27",
            "deadline_at": "2026-03-27T18:00:00",
            "created_at": "2026-03-24T08:00:00",
            "updated_at": "2026-03-24T08:00:00",
        },
        {
            "id": "ledger_expense",
            "owner_id": owner_id,
            "type": "ledger",
            "title": "午饭",
            "amount": 35.5,
            "transaction_type": "expense",
            "ledger_category": "餐饮",
            "ledger_date": "2026-03-20",
        },
        {
            "id": "ledger_income",
            "owner_id": owner_id,
            "type": "ledger",
            "title": "工资",
            "amount": 5000,
            "transaction_type": "income",
            "ledger_category": "工资",
            "ledger_date": "2026-03-21",
        },
        {
            "id": "note_recent",
            "owner_id": owner_id,
            "type": "note",
            "title": "Radcliffe Wave 线索",
            "content": "整理近几天的观测想法和参考资料。",
            "category": "科研",
            "created_at": "2026-03-24T21:00:00",
            "updated_at": "2026-03-25T08:00:00",
            "tags": ["paper"],
        },
        {
            "id": "note_older",
            "owner_id": owner_id,
            "type": "note",
            "title": "SED 拆解",
            "content": "把滤光片组合和拟合过程记一下。",
            "category": "科研",
            "created_at": "2026-03-20T21:00:00",
            "updated_at": "2026-03-21T08:00:00",
        },
    ]:
        db.insert_item(item)


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_widget_summary_returns_agenda_and_task_panel(client: TestClient, temp_db: Database):
    owner_id = "u-widget"
    _seed_widget_data(temp_db, owner_id)
    token = generate_widget_token(owner_id)

    res = client.get(
        "/api/widget/summary",
        params={"section": "tasks", "now": "2026-03-25T09:30:00"},
        headers=_headers(token),
    )

    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True

    data = body["data"]
    assert data["section"] == "tasks"
    assert data["section_requested"] == "tasks"
    assert data["agenda"]["today_count"] == 1
    assert data["agenda"]["tomorrow_count"] == 1
    assert [item["title"] for item in data["agenda"]["items"]] == ["今天会议", "明天复盘", "周末活动"]
    assert data["panel"]["title"] == "待办"
    assert data["panel"]["summary"]["primary"] == "2 项待办"
    assert "今日聚焦" in data["panel"]["summary"]["secondary"]
    assert [item["title"] for item in data["panel"]["items"][:2]] == ["处理周报", "整理收据"]
    assert data["links"]["events"] == "#/events"
    assert data["links"]["tasks"] == "#/tasks"


def test_build_widget_summary_returns_expected_panels_without_fastapi(temp_db: Database):
    widget_module = _load_widget_module()
    owner_id = "u-widget-direct"
    _seed_widget_data(temp_db, owner_id)

    tasks_data = widget_module.build_widget_summary(temp_db, owner_id, section="tasks", now="2026-03-25T09:30:00")
    ledger_data = widget_module.build_widget_summary(temp_db, owner_id, section="ledger", now="2026-03-25T09:30:00")

    assert tasks_data["section"] == "tasks"
    assert tasks_data["agenda"]["today_count"] == 1
    assert tasks_data["panel"]["items"][0]["title"] == "处理周报"
    assert ledger_data["section"] == "ledger"
    assert ledger_data["panel"]["items"][0]["amount_text"] == "-¥36"


def test_build_widget_summary_uses_event_collection_titles_for_agenda(temp_db: Database):
    widget_module = _load_widget_module()
    owner_id = "u-widget-event-collection"
    temp_db.create_event_collection({
        "id": "widget-conf",
        "owner_id": owner_id,
        "kind": "multi_node",
        "title": "FRB2026会议",
        "category": "学术",
        "start_time": "2026-03-25T10:00:00",
        "end_time": "2026-03-26T10:00:00",
    })
    temp_db.insert_item({
        "id": "widget-conf_m01",
        "owner_id": owner_id,
        "type": "event",
        "title": "摘要截止",
        "category": "学术",
        "start_time": "2026-03-25T10:00:00",
        "event_role": "multi_node_child",
        "event_collection_id": "widget-conf",
        "event_collection_kind": "multi_node",
        "event_index": 1,
        "event_node_key": "m01",
    })

    data = widget_module.build_widget_summary(temp_db, owner_id, section="tasks", now="2026-03-25T09:30:00")

    assert data["agenda"]["items"][0]["title"] == "FRB2026会议 · 摘要截止"


def test_widget_summary_supports_ledger_notes_and_auto_sections(client: TestClient, temp_db: Database):
    owner_id = "u-widget-sections"
    _seed_widget_data(temp_db, owner_id)
    token = generate_widget_token(owner_id)

    ledger_res = client.get(
        "/api/widget/summary",
        params={"section": "ledger", "now": "2026-03-25T09:30:00"},
        headers=_headers(token),
    )
    notes_res = client.get(
        "/api/widget/summary",
        params={"section": "notes", "now": "2026-03-25T11:30:00"},
        headers=_headers(token),
    )
    auto_res = client.get(
        "/api/widget/summary",
        params={"section": "auto", "now": "2026-03-25T11:30:00"},
        headers=_headers(token),
    )

    ledger_data = ledger_res.json()["data"]
    notes_data = notes_res.json()["data"]
    auto_data = auto_res.json()["data"]

    assert ledger_data["section"] == "ledger"
    assert ledger_data["panel"]["title"] == "财务"
    assert ledger_data["panel"]["items"][0]["title"] == "午饭"
    assert ledger_data["panel"]["items"][0]["amount_text"] == "-¥36"

    assert notes_data["section"] == "notes"
    assert notes_data["panel"]["title"] == "笔记"
    assert notes_data["panel"]["items"][0]["title"] == "Radcliffe Wave 线索"
    assert "观测想法" in notes_data["panel"]["items"][0]["preview"]

    assert auto_data["section_requested"] == "auto"
    assert auto_data["section"] == "notes"
    assert auto_data["panel"]["title"] == "笔记"


def test_widget_ledger_panel_marks_transfer_transactions(temp_db: Database):
    widget_module = _load_widget_module()
    owner_id = "u-widget-transfer"
    _seed_widget_data(temp_db, owner_id)
    temp_db.insert_item(
        {
            "id": "ledger_transfer",
            "owner_id": owner_id,
            "type": "ledger",
            "title": "转到储蓄卡",
            "amount": 1200,
            "transaction_type": "transfer",
            "ledger_category": "转账",
            "ledger_date": "2026-03-22",
            "account_name": "现金",
            "counter_account_name": "储蓄卡",
        }
    )

    data = widget_module.build_widget_summary(temp_db, owner_id, section="ledger", now="2026-03-25T09:30:00")
    transfer = next(item for item in data["panel"]["items"] if item["title"] == "转到储蓄卡")

    assert transfer["transaction_type"] == "transfer"
    assert transfer["amount_text"] == "↔ ¥1200"


def test_scriptable_widget_reads_current_widget_summary_shape():
    src = (ROOT / "plugins" / "pendo" / "web" / "scriptable" / "pendo_widget.js").read_text(encoding="utf-8")

    assert "/api/widget/summary?section=" in src
    assert "data.panels || {}" in src
    assert "item?.start_time" in src
    assert "function ledgerAmountKind(item)" in src
    assert 'text.startsWith("↔")' in src


def test_build_widget_summary_auto_rotates_by_hour(temp_db: Database):
    widget_module = _load_widget_module()
    owner_id = "u-widget-auto"
    _seed_widget_data(temp_db, owner_id)

    assert widget_module.build_widget_summary(temp_db, owner_id, section="auto", now="2026-03-25T09:30:00")["section"] == "tasks"
    assert widget_module.build_widget_summary(temp_db, owner_id, section="auto", now="2026-03-25T10:30:00")["section"] == "ledger"
    assert widget_module.build_widget_summary(temp_db, owner_id, section="auto", now="2026-03-25T11:30:00")["section"] == "notes"


def test_build_widget_summary_limits_agenda_to_five_items_within_thirty_days(temp_db: Database):
    widget_module = _load_widget_module()
    owner_id = "u-widget-agenda-30d"
    _seed_widget_data(temp_db, owner_id)

    for index, start in enumerate(
        [
            "2026-03-29T09:00:00",
            "2026-04-03T09:00:00",
            "2026-04-10T09:00:00",
            "2026-04-20T09:00:00",
        ],
        start=1,
    ):
        temp_db.insert_item(
            {
                "id": f"event_extra_{index}",
                "owner_id": owner_id,
                "type": "event",
                "title": f"额外安排 {index}",
                "start_time": start,
                "end_time": start.replace("09:00:00", "10:00:00"),
            }
        )

    temp_db.insert_item(
        {
            "id": "event_outside_30d",
            "owner_id": owner_id,
            "type": "event",
            "title": "三十天外安排",
            "start_time": "2026-04-26T09:00:00",
            "end_time": "2026-04-26T10:00:00",
        }
    )

    data = widget_module.build_widget_summary(temp_db, owner_id, section="tasks", now="2026-03-25T09:30:00")
    titles = [item["title"] for item in data["agenda"]["items"]]

    assert len(titles) == 5
    assert titles == ["今天会议", "明天复盘", "周末活动", "额外安排 1", "额外安排 2"]
    assert "三十天外安排" not in titles


def test_build_widget_summary_limits_notes_panel_to_five_items(temp_db: Database):
    widget_module = _load_widget_module()
    owner_id = "u-widget-notes-5"
    _seed_widget_data(temp_db, owner_id)

    for index, created_at in enumerate(
        [
            "2026-03-25T09:00:00",
            "2026-03-24T20:00:00",
            "2026-03-24T10:00:00",
            "2026-03-23T20:00:00",
        ],
        start=1,
    ):
        temp_db.insert_item(
            {
                "id": f"note_extra_{index}",
                "owner_id": owner_id,
                "type": "note",
                "title": f"额外笔记 {index}",
                "content": f"这是第 {index} 条额外笔记。",
                "category": "测试",
                "created_at": created_at,
                "updated_at": created_at,
            }
        )

    data = widget_module.build_widget_summary(temp_db, owner_id, section="notes", now="2026-03-25T09:30:00")
    titles = [item["title"] for item in data["panel"]["items"]]

    assert len(titles) == 5
    assert titles == ["额外笔记 1", "Radcliffe Wave 线索", "额外笔记 2", "额外笔记 3", "额外笔记 4"]
    assert "SED 拆解" not in titles


def test_widget_token_is_limited_to_widget_endpoint(client: TestClient, temp_db: Database):
    owner_id = "u-widget-locked"
    _seed_widget_data(temp_db, owner_id)
    token = generate_widget_token(owner_id)

    widget_res = client.get(
        "/api/widget/summary",
        params={"section": "tasks", "now": "2026-03-25T09:30:00"},
        headers=_headers(token),
    )
    dashboard_res = client.get("/api/dashboard", headers=_headers(token))

    assert widget_res.status_code == 200
    assert dashboard_res.status_code == 403
    assert "Widget token" in dashboard_res.json()["message"]


def test_widget_endpoint_rejects_missing_auth(client: TestClient):
    res = client.get("/api/widget/summary")

    assert res.status_code == 401
    assert "Missing web session" in res.json()["message"]


def test_web_handler_never_inlines_widget_token_when_private_delivery_is_unavailable(monkeypatch):
    sys.path.insert(0, str(ROOT))
    sys.modules.pop("plugins.pendo.handlers.web", None)
    sys.modules["plugins.pendo.web.server"] = types.SimpleNamespace(
        get_url=lambda: "http://127.0.0.1:8765",
        is_running=lambda: True,
        start=lambda _db: True,
        stop=lambda: True,
    )

    web_module = importlib.import_module("plugins.pendo.handlers.web")

    monkeypatch.setattr(web_module, "generate_widget_token", lambda *_args, **_kwargs: "mock-widget-token")

    handler = web_module.WebHandler(db=None)
    result = asyncio.run(handler.handle("1001", "widget token", context=None))

    assert result["status"] == "error"
    assert "无法通过私聊安全发送凭据" in result["message"]
    assert "mock-widget-token" not in result["message"]


def test_regular_web_bearer_token_cannot_access_browser_api(client: TestClient, temp_db: Database):
    owner_id = "u-widget-normal"
    _seed_widget_data(temp_db, owner_id)
    token = generate_token(owner_id)

    res = client.get("/api/dashboard", headers=_headers(token))

    assert res.status_code == 401
