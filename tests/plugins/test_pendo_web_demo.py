from __future__ import annotations

import shutil
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from plugins.pendo.config import PendoConfig
from plugins.pendo.services.db import Database

try:
    from plugins.pendo.web import auth as auth_module
    from plugins.pendo.web.auth import create_web_session, issue_login_code
    from plugins.pendo.web.deps import SESSION_COOKIE_NAME
    from plugins.pendo.web.services import demo_space as demo_space_module
except ModuleNotFoundError:
    pytest.skip("pendo web demo requires PyJWT", allow_module_level=True)

try:
    from fastapi.testclient import TestClient

    from plugins.pendo.web import deps as deps_module
    from plugins.pendo.web.server import create_app
    FASTAPI_AVAILABLE = True
except ModuleNotFoundError:
    TestClient = None
    create_app = None
    deps_module = None
    FASTAPI_AVAILABLE = False
except RuntimeError as exc:
    if "requires the httpx package" not in str(exc):
        raise
    TestClient = None
    create_app = None
    deps_module = None
    FASTAPI_AVAILABLE = False


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def reset_pendo_config_state(monkeypatch):
    monkeypatch.delenv("PENDO_WEB_DEMO_ENABLED", raising=False)
    monkeypatch.delenv("PENDO_WEB_SESSION_COOKIE_SECURE", raising=False)
    PendoConfig.reset_runtime_config()
    yield
    monkeypatch.delenv("PENDO_WEB_DEMO_ENABLED", raising=False)
    monkeypatch.delenv("PENDO_WEB_SESSION_COOKIE_SECURE", raising=False)
    PendoConfig.reset_runtime_config()


@pytest.fixture()
def temp_db():
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_demo_{uuid.uuid4().hex}"
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


def test_demo_auth_endpoint_is_disabled_by_default(client: TestClient):
    res = client.post("/api/auth/demo")

    assert res.status_code == 404
    assert "disabled" in res.json()["message"]


def test_login_code_exchange_is_single_use_and_creates_httponly_session(client: TestClient):
    code = issue_login_code("private-owner", expires_seconds=60)

    exchange = client.post("/api/auth/exchange", json={"code": code})

    assert exchange.status_code == 200
    assert "token" not in exchange.json()["data"]
    assert exchange.json()["data"]["csrf_token"]
    cookie = exchange.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=strict" in cookie
    assert client.get("/api/auth/session").json()["data"]["owner_id"] == "private-owner"

    reused = client.post("/api/auth/exchange", json={"code": code})
    assert reused.status_code == 401


def test_production_session_cookie_is_marked_secure_when_configured(client: TestClient):
    PendoConfig.WEB_SESSION_COOKIE_SECURE = True

    response = client.post("/api/auth/exchange", json={"code": issue_login_code("secure-owner")})

    assert response.status_code == 200
    assert "secure" in response.headers["set-cookie"].lower()


def test_server_rejects_public_binding_without_secure_session_cookie(temp_db: Database):
    from plugins.pendo.web import server as server_module

    PendoConfig.WEB_HOST = "0.0.0.0"
    PendoConfig.WEB_SESSION_COOKIE_SECURE = False

    assert server_module.start(temp_db) is False
    assert "Secure session cookie" in server_module.get_last_error()


def test_logout_requires_csrf_and_revokes_session(client: TestClient):
    exchange = client.post("/api/auth/exchange", json={"code": issue_login_code("logout-owner")})
    csrf = exchange.json()["data"]["csrf_token"]

    denied = client.post("/api/auth/logout")
    assert denied.status_code == 403
    logged_out = client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})
    assert logged_out.status_code == 200
    assert client.get("/api/auth/session").status_code == 401


def test_session_device_list_and_revoke_route_require_cookie_and_csrf(client: TestClient):
    exchange = client.post("/api/auth/exchange", json={"code": issue_login_code("device-owner")})
    csrf = exchange.json()["data"]["csrf_token"]
    sessions = client.get("/api/auth/sessions")

    assert sessions.status_code == 200
    device_id = sessions.json()["data"]["sessions"][0]["device_id"]
    assert sessions.json()["data"]["sessions"][0]["current"] is True
    assert client.delete(f"/api/auth/sessions/{device_id}").status_code == 403
    assert client.delete(
        f"/api/auth/sessions/{device_id}",
        headers={"X-CSRF-Token": csrf},
    ).status_code == 200
    assert client.get("/api/auth/session").status_code == 401


def test_demo_auth_endpoint_creates_seeded_demo_space(
    client: TestClient,
    temp_db: Database,
):
    PendoConfig.configure({"plugins": {"pendo": {"web_demo_enabled": True}}})
    res = client.post("/api/auth/demo")

    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    owner_id = body["data"]["owner_id"]
    assert owner_id.startswith("demo_web_")
    assert body["data"]["expires_at"]
    assert body["data"]["csrf_token"]
    assert "token" not in body["data"]
    set_cookie = res.headers["set-cookie"].lower()
    assert "httponly" in set_cookie
    assert "samesite=strict" in set_cookie
    session = client.get("/api/auth/session")
    assert session.status_code == 200
    assert session.json()["data"]["owner_id"] == owner_id

    tasks = temp_db.get_items(owner_id, filters={"type": "task"}, limit=20)
    notes = temp_db.get_items(owner_id, filters={"type": "note"}, limit=20)
    ledger = temp_db.get_items(owner_id, filters={"type": "ledger"}, limit=20)
    diaries = temp_db.get_items(owner_id, filters={"type": "diary"}, limit=20)
    events = temp_db.get_items(owner_id, filters={"type": "event"}, limit=20)
    settings = temp_db.get_user_settings(owner_id)

    assert len(tasks) >= 3
    assert len(notes) >= 2
    assert len(ledger) >= 5
    assert len(diaries) >= 2
    assert len(events) >= 2
    assert settings["settings_json"]["demo_mode"] is True
    assert settings["settings_json"]["reminder_enabled"] is False
    assert settings["settings_json"]["daily_briefing_enabled"] is False


def test_global_config_can_enable_demo_endpoint():
    PendoConfig.configure({"plugins": {"pendo": {"web_demo_enabled": True}}})

    assert PendoConfig.WEB_DEMO_ENABLED is True


def test_environment_override_wins_over_global_demo_config(monkeypatch):
    monkeypatch.setenv("PENDO_WEB_DEMO_ENABLED", "false")

    PendoConfig.configure({"plugins": {"pendo": {"web_demo_enabled": True}}})

    assert PendoConfig.WEB_DEMO_ENABLED is False


def test_create_demo_session_seeds_items_without_fastapi(temp_db: Database):
    payload = demo_space_module.create_demo_session(temp_db, now=__import__("datetime").datetime(2026, 4, 8, 10, 0, 0))

    owner_id = payload["owner_id"]
    assert owner_id.startswith("demo_web_")
    assert payload["demo"] is True
    assert "token" not in payload
    events = temp_db.get_items(owner_id, filters={"type": "event"}, limit=20)
    tasks = temp_db.get_items(owner_id, filters={"type": "task"}, limit=20)
    ledger = temp_db.get_items(owner_id, filters={"type": "ledger"}, limit=30)
    notes = temp_db.get_items(owner_id, filters={"type": "note"}, limit=20)
    diaries = temp_db.get_items(owner_id, filters={"type": "diary"}, limit=20)

    assert len(events) >= 6
    assert len(tasks) >= 6
    assert len(ledger) >= 12
    assert len(notes) >= 5
    assert len(diaries) >= 4
    ledger_days = sorted({item.ledger_date for item in ledger})
    assert ledger_days[0].startswith("2025-")
    assert ledger_days[-1].startswith("2026-")
    assert any(day.startswith("2026-04-") for day in ledger_days)
    note_years = {str(item.created_at)[:4] for item in notes}
    diary_days = {item.diary_date for item in diaries}
    assert note_years == {"2025", "2026"}
    assert {"2025-12-31", "2026-01-01"} <= diary_days


def test_demo_creation_enforces_per_client_rate_limit(temp_db: Database, monkeypatch):
    monkeypatch.setattr(PendoConfig, "WEB_DEMO_REQUESTS_PER_HOUR", 1)
    demo_space_module._DEMO_REQUESTS.clear()
    now = datetime(2030, 1, 1, 12, 0, 0)

    demo_space_module.create_demo_session(temp_db, now=now, client_key="test-client")

    with pytest.raises(demo_space_module.DemoCapacityError, match="rate limit"):
        demo_space_module.create_demo_session(temp_db, now=now + timedelta(minutes=1), client_key="test-client")


def test_demo_template_bundle_exists_and_covers_time_filters():
    assert demo_space_module._DEMO_TEMPLATE_PATH.exists()

    records = demo_space_module._load_demo_template_records()
    by_type: dict[str, list[dict]] = {}
    for record in records:
        by_type.setdefault(record["type"], []).append(record)

    assert set(by_type) == {"event", "task", "ledger", "note", "diary"}
    ledger_days = sorted(record["ledger_date"] for record in by_type["ledger"])
    assert ledger_days[0] == "2025-01-12"
    assert ledger_days[-1] == "2026-04-08"
    assert "2025-12-31" in ledger_days
    assert "2026-01-01" in ledger_days
    assert any(day.startswith("2026-04-") for day in ledger_days)


def test_ensure_demo_access_purges_expired_demo_owner(temp_db: Database, monkeypatch):
    owner_id = "demo_web_expired01"
    temp_db.update_user_settings(owner_id, {
        "settings_json": {
            "demo_mode": True,
            "demo_expires_at": "2026-04-08T09:00:00",
            "reminder_enabled": False,
            "daily_briefing_enabled": False,
        },
    })
    temp_db.insert_item({
        "id": "demo_expired_task",
        "owner_id": owner_id,
        "type": "task",
        "title": "过期演示任务",
        "status": "open",
        "priority": 3,
        "created_at": "2026-04-08T08:00:00",
        "updated_at": "2026-04-08T08:00:00",
    })

    class _FrozenDemoDateTime(__import__("datetime").datetime):
        @classmethod
        def now(cls):
            return cls(2026, 4, 8, 10, 0, 0)

    monkeypatch.setattr(demo_space_module, "datetime", _FrozenDemoDateTime)

    with pytest.raises(auth_module.AuthError, match="expired"):
        demo_space_module.ensure_demo_access(temp_db, owner_id)

    assert temp_db.get_items(owner_id, filters={"type": "task"}, limit=10) == []
    assert temp_db.get_user_settings(owner_id)["settings_json"].get("demo_mode") is not True


def test_expired_demo_token_is_rejected_and_demo_data_is_purged(client: TestClient, temp_db: Database, monkeypatch):
    owner_id = "demo_web_expired01"
    temp_db.update_user_settings(owner_id, {
        "settings_json": {
            "demo_mode": True,
            "demo_expires_at": "2026-04-08T09:00:00",
            "reminder_enabled": False,
            "daily_briefing_enabled": False,
        },
    })
    temp_db.insert_item({
        "id": "demo_expired_task",
        "owner_id": owner_id,
        "type": "task",
        "title": "过期演示任务",
        "status": "open",
        "priority": 3,
        "created_at": "2026-04-08T08:00:00",
        "updated_at": "2026-04-08T08:00:00",
    })

    class _FrozenDemoDateTime(__import__("datetime").datetime):
        @classmethod
        def now(cls):
            return cls(2026, 4, 8, 10, 0, 0)

    monkeypatch.setattr(demo_space_module, "datetime", _FrozenDemoDateTime)
    session = create_web_session(owner_id, demo=True)
    client.cookies.set(SESSION_COOKIE_NAME, session.session_id)

    res = client.get("/api/auth/session")

    assert res.status_code == 401
    assert "expired" in res.json()["message"]
    assert temp_db.get_items(owner_id, filters={"type": "task"}, limit=10) == []
    assert temp_db.get_user_settings(owner_id)["settings_json"].get("demo_mode") is not True


def test_login_page_sources_offer_demo_entry():
    app_src = (ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "app.js").read_text(encoding="utf-8")
    api_src = (ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "api.js").read_text(encoding="utf-8")
    html = (ROOT / "plugins" / "pendo" / "web" / "static" / "index.html").read_text(encoding="utf-8")
    auth_src = (ROOT / "plugins" / "pendo" / "web" / "api" / "auth_routes.py").read_text(encoding="utf-8")

    assert "createDemoSession" in app_src
    assert "const demoBtn = document.getElementById('login-demo-btn');" in app_src
    assert "const enterDemo = async () => {" in app_src
    assert "demoBtn.onclick = enterDemo;" in app_src
    assert "export async function createDemoSession()" in api_src
    assert "fetch('api/auth/demo'" in api_src
    assert 'id="login-demo-btn"' in html
    assert ">Demo<" in html
    assert '@router.post("/auth/demo")' in auth_src
    assert "localStorage" not in api_src
    assert "Authorization" not in api_src
    assert "credentials: 'same-origin'" in api_src
    assert "X-CSRF-Token" in api_src
    assert "exchangeLoginCode" in app_src
    assert "history.replaceState" in app_src
