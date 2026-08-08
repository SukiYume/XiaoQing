"""Pendo Web 演示空间、Cookie 会话和公开入口的直接回归。"""

from __future__ import annotations

import json
import re
from collections import deque
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from plugins.pendo.config import PendoConfig
from plugins.pendo.services.db import Database
from plugins.pendo.web.services.transfer_bundle import ParsedBundle
from tests.helpers.paths import REPOSITORY_ROOT
from tests.helpers.pendo_test_support import reset_pendo_runtime_config

try:
    from plugins.pendo.web.auth import AuthError, create_web_session, issue_login_code
    from plugins.pendo.web.deps import SESSION_COOKIE_NAME
    from plugins.pendo.web.services import demo_space as demo_space_module
except ModuleNotFoundError:
    pytest.skip("pendo web demo requires PyJWT", allow_module_level=True)


ROOT = REPOSITORY_ROOT


@pytest.fixture(autouse=True)
def reset_pendo_config_state() -> Iterator[None]:
    """隔离每个用例修改的 Web 配置和进程内演示限流状态。"""

    reset_pendo_runtime_config()
    demo_space_module._DEMO_REQUESTS.clear()
    yield
    reset_pendo_runtime_config()
    demo_space_module._DEMO_REQUESTS.clear()


@pytest.fixture()
def expired_demo_owner(temp_db: Database) -> str:
    """建立带一条任务的过期演示所有者，供直接和 API 拒绝路径复用。"""

    owner_id = "demo_web_expired01"
    temp_db.update_user_settings(
        owner_id,
        {
            "settings_json": {
                "demo_mode": True,
                "demo_expires_at": "2026-04-08T09:00:00",
                "reminder_enabled": False,
                "daily_briefing_enabled": False,
            },
        },
    )
    temp_db.insert_item(
        {
            "id": "demo_expired_task",
            "owner_id": owner_id,
            "type": "task",
            "title": "过期演示任务",
            "status": "open",
            "priority": 3,
            "created_at": "2026-04-08T08:00:00",
            "updated_at": "2026-04-08T08:00:00",
        }
    )
    return owner_id


def test_demo_auth_endpoint_is_disabled_by_default(client: Any) -> None:
    """未显式启用演示模式时，公开创建入口应保持不可发现。"""

    res = client.post("/api/auth/demo")

    assert res.status_code == 404
    assert "disabled" in res.json()["message"]


def test_login_code_exchange_is_single_use_and_creates_httponly_session(
    client: Any,
) -> None:
    """私聊登录码只能兑换一次，浏览器只得到 HttpOnly 会话 Cookie。"""

    code = issue_login_code("private-owner", expires_seconds=60)

    exchange = client.post("/api/auth/exchange", json={"code": code})

    assert exchange.status_code == 200
    assert "token" not in exchange.json()["data"]
    assert exchange.json()["data"]["csrf_token"]
    cookie = exchange.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=strict" in cookie
    max_age = re.search(r"(?:^|;)\s*max-age=(\d+)", cookie)
    assert max_age is not None
    assert 7 * 24 * 60 * 60 - 2 <= int(max_age.group(1)) <= 7 * 24 * 60 * 60
    assert client.get("/api/auth/session").json()["data"]["owner_id"] == "private-owner"

    reused = client.post("/api/auth/exchange", json={"code": code})
    assert reused.status_code == 401


def test_production_session_cookie_is_marked_secure_when_configured(client: Any) -> None:
    """启用安全 Cookie 配置后，登录交换响应必须带 Secure 属性。"""

    PendoConfig.configure({"web_session_cookie_secure": True})

    response = client.post("/api/auth/exchange", json={"code": issue_login_code("secure-owner")})

    assert response.status_code == 200
    assert "secure" in response.headers["set-cookie"].lower()


def test_server_rejects_public_binding_without_secure_session_cookie(
    temp_db: Database,
) -> None:
    """公网监听不得与非安全会话 Cookie 组合启动。"""

    from plugins.pendo.web import server as server_module

    PendoConfig.configure(
        {
            "web_host": "0.0.0.0",
            "web_session_cookie_secure": False,
        }
    )

    assert server_module.start(temp_db) is False
    assert "Secure session cookie" in server_module.get_last_error()


def test_logout_requires_csrf_and_revokes_session(client: Any) -> None:
    """登出写操作必须校验 CSRF，成功后当前会话立即失效。"""

    exchange = client.post("/api/auth/exchange", json={"code": issue_login_code("logout-owner")})
    csrf = exchange.json()["data"]["csrf_token"]

    denied = client.post("/api/auth/logout")
    assert denied.status_code == 403
    logged_out = client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})
    assert logged_out.status_code == 200
    assert client.get("/api/auth/session").status_code == 401


def test_session_device_list_and_revoke_route_require_cookie_and_csrf(client: Any) -> None:
    """设备会话列表依赖 Cookie，撤销操作还必须携带正确 CSRF。"""

    exchange = client.post("/api/auth/exchange", json={"code": issue_login_code("device-owner")})
    csrf = exchange.json()["data"]["csrf_token"]
    sessions = client.get("/api/auth/sessions")

    assert sessions.status_code == 200
    device_id = sessions.json()["data"]["sessions"][0]["device_id"]
    assert sessions.json()["data"]["sessions"][0]["current"] is True
    assert client.delete(f"/api/auth/sessions/{device_id}").status_code == 403
    assert (
        client.delete(
            f"/api/auth/sessions/{device_id}",
            headers={"X-CSRF-Token": csrf},
        ).status_code
        == 200
    )
    assert client.get("/api/auth/session").status_code == 401


def test_demo_auth_endpoint_creates_seeded_demo_space(
    client: Any,
    temp_db: Database,
) -> None:
    """演示入口应建立隔离会话、写入各类样例并关闭主动提醒。"""

    PendoConfig.configure({"web_demo_enabled": True})
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


def test_global_config_can_enable_demo_endpoint() -> None:
    """插件全局配置应能显式打开演示入口。"""

    PendoConfig.configure({"web_demo_enabled": True})

    assert PendoConfig.runtime().web_demo_enabled is True


def test_environment_variable_is_not_a_second_demo_config_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runtime settings come from one reviewed config namespace only."""

    monkeypatch.setenv("PENDO_WEB_DEMO_ENABLED", "false")

    PendoConfig.configure({"web_demo_enabled": True})

    assert PendoConfig.runtime().web_demo_enabled is True


def test_create_demo_session_seeds_items_without_fastapi(temp_db: Database) -> None:
    """服务层不依赖 FastAPI 也能创建完整样例空间并平移模板日期。"""

    payload = demo_space_module.create_demo_session(temp_db, now=datetime(2026, 4, 8, 10, 0, 0))

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


def test_demo_creation_enforces_per_client_rate_limit(
    temp_db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一客户端在滚动一小时窗口内不得超过配置的创建次数。"""

    monkeypatch.setattr(PendoConfig, "WEB_DEMO_REQUESTS_PER_HOUR", 1)
    now = datetime(2030, 1, 1, 12, 0, 0)

    demo_space_module.create_demo_session(temp_db, now=now, client_key="test-client")

    with pytest.raises(demo_space_module.DemoCapacityError, match="rate limit"):
        demo_space_module.create_demo_session(
            temp_db, now=now + timedelta(minutes=1), client_key="test-client"
        )


def test_demo_capacity_failure_does_not_retain_empty_client_bucket(
    temp_db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """容量已满时不应为任意客户端留下永不回收的空限流桶。"""

    monkeypatch.setattr(PendoConfig, "WEB_DEMO_MAX_ACTIVE_SESSIONS", 0)

    with pytest.raises(demo_space_module.DemoCapacityError, match="capacity"):
        demo_space_module.create_demo_session(
            temp_db,
            now=datetime(2030, 1, 1, 12, 0),
            client_key="capacity-client",
        )

    assert demo_space_module._DEMO_REQUESTS == {}


def test_recent_demo_requests_prunes_stale_buckets_and_normalizes_client_key() -> None:
    """全局限流表应清掉过期键，并规范化当前客户端但不预先落空桶。"""

    now = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)
    demo_space_module._DEMO_REQUESTS.update(
        {
            "stale": deque([datetime(2029, 12, 31, 0, 0)]),
            "active": deque([now - timedelta(minutes=30)]),
        }
    )

    key, request_times = demo_space_module._recent_demo_requests(
        f"  {'x' * 256}  ",
        now,
    )

    assert "stale" not in demo_space_module._DEMO_REQUESTS
    assert list(demo_space_module._DEMO_REQUESTS["active"]) == [now - timedelta(minutes=30)]
    assert key == "x" * demo_space_module._MAX_CLIENT_KEY_CHARS
    assert request_times == deque()
    assert key not in demo_space_module._DEMO_REQUESTS


def test_demo_seed_failure_removes_partial_owner_and_does_not_consume_rate_limit(
    temp_db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模板写入失败时应回滚演示所有者，且失败请求不计入限流。"""

    monkeypatch.setattr(
        demo_space_module,
        "_seed_demo_items",
        Mock(side_effect=RuntimeError("seed failed")),
    )

    with pytest.raises(RuntimeError, match="seed failed"):
        demo_space_module.create_demo_session(
            temp_db,
            now=datetime(2030, 1, 1, 12, 0),
            client_key="failed-client",
        )

    remaining = temp_db.get_connection().execute(
        "SELECT COUNT(*) FROM user_settings WHERE user_id LIKE ?",
        (f"{demo_space_module._DEMO_PREFIX}%",),
    )
    assert remaining.fetchone()[0] == 0
    assert demo_space_module._DEMO_REQUESTS == {}


def test_demo_record_transform_shifts_dates_and_rewrites_only_string_references() -> None:
    """模板转换应复制原记录、平移时间并安全重写字符串形式的内部引用。"""

    record = {
        "id": "source-task",
        "type": "task",
        "plan_date": "2026-01-01",
        "start_time": "2026-01-01T09:00:00.123456",
        "remind_times": ["2026-01-01T08:30:00", "bad-time"],
        "related_items": ["source-task", ["unhashable"]],
    }

    transformed = demo_space_module._transform_demo_record(
        record,
        {"source-task": "demo-task"},
        2,
    )

    assert transformed["id"] == "demo-task"
    assert transformed["plan_date"] == "2026-01-03"
    assert transformed["start_time"] == "2026-01-03T09:00:00"
    assert transformed["remind_times"] == ["2026-01-03T08:30:00", "bad-time"]
    assert transformed["related_items"] == ["demo-task", ["unhashable"]]
    assert record["id"] == "source-task"
    assert demo_space_module._transform_demo_record({"id": "unmapped"}, {}, 0)["id"] == ("unmapped")


def test_demo_time_shifters_preserve_empty_invalid_and_overflow_values() -> None:
    """模板防御分支应保留空值、坏格式和越界值，不把装载异常泄到创建入口。"""

    assert demo_space_module._shift_date_text(None, 1) is None
    assert demo_space_module._shift_date_text("not-a-date", 1) == "not-a-date"
    assert demo_space_module._shift_date_text("9999-12-31", 1) == "9999-12-31"
    assert demo_space_module._shift_datetime_text("", 1) == ""
    assert demo_space_module._shift_datetime_text("not-a-time", 1) == "not-a-time"
    assert demo_space_module._shift_datetime_text("9999-12-31T23:59:59", 1) == "9999-12-31T23:59:59"
    assert demo_space_module._coerce_expiry("") is None
    assert demo_space_module._coerce_expiry("not-a-time") is None

    broken_datetime = Mock(spec=datetime)
    broken_datetime.utcoffset.side_effect = ValueError("broken timezone")
    assert (
        demo_space_module._is_expired(
            broken_datetime,
            datetime(2030, 1, 1, tzinfo=timezone.utc),
        )
        is True
    )


def test_demo_template_bundle_exists_and_covers_time_filters() -> None:
    """发行包必须包含覆盖全部页面和跨年筛选窗口的演示模板。"""

    assert demo_space_module._DEMO_TEMPLATE_PATH.exists()

    records = demo_space_module._load_demo_template_records()
    by_type: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        by_type.setdefault(record["type"], []).append(record)

    assert set(by_type) == {"event", "task", "ledger", "note", "diary"}
    ledger_days = sorted(record["ledger_date"] for record in by_type["ledger"])
    assert ledger_days[0] == "2025-01-12"
    assert ledger_days[-1] == "2026-04-08"
    assert "2025-12-31" in ledger_days
    assert "2026-01-01" in ledger_days
    assert any(day.startswith("2026-04-") for day in ledger_days)


def test_demo_template_loader_rejects_missing_asset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """内置演示包缺失时应在创建空会话前给出明确失败。"""

    monkeypatch.setattr(
        demo_space_module,
        "_DEMO_TEMPLATE_PATH",
        tmp_path / "missing-demo.pendo.zip",
    )

    with pytest.raises(RuntimeError, match="Missing demo template bundle"):
        demo_space_module._load_demo_template_records()


@pytest.mark.parametrize(
    ("parsed", "records", "errors", "message"),
    [
        (
            ParsedBundle(manifest={}),
            [],
            [{"message": "bad row"}],
            "Invalid demo template bundle",
        ),
        (
            ParsedBundle(manifest={}, event_collections=[{"id": "collection"}]),
            [],
            [],
            "must not contain event collections",
        ),
        (
            ParsedBundle(manifest={}, file_summaries=[{"type": "task"}]),
            [],
            [],
            "each demo item type once",
        ),
        (
            ParsedBundle(
                manifest={},
                file_summaries=[
                    {"type": item_type} for item_type in demo_space_module._DEMO_ITEM_TYPES
                ],
            ),
            [{"type": "unsupported"}],
            [],
            "Unsupported demo template item type",
        ),
        (
            ParsedBundle(
                manifest={},
                file_summaries=[
                    {"type": item_type} for item_type in demo_space_module._DEMO_ITEM_TYPES
                ],
            ),
            [
                {"type": item_type}
                for item_type in demo_space_module._DEMO_ITEM_TYPES
                if item_type != "diary"
            ],
            [],
            "contains an empty item type",
        ),
    ],
)
def test_demo_template_loader_rejects_invalid_runtime_contracts(
    parsed: ParsedBundle,
    records: list[dict[str, Any]],
    errors: list[dict[str, Any]],
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模板解析错误、集合、类型布局和空类型都不得被静默忽略。"""

    monkeypatch.setattr(
        demo_space_module,
        "inspect_bundle_bytes",
        Mock(return_value=(parsed, records, errors)),
    )

    with pytest.raises(RuntimeError, match=message):
        demo_space_module._load_demo_template_records()


def test_purge_demo_owner_deletes_every_owner_scoped_table(temp_db: Database) -> None:
    """演示回收应覆盖业务、审计、导入和 Web 认证记录。"""

    owner_id = "demo_web_cleanup01"
    regular_owner = "regular-owner"
    for owner in (owner_id, regular_owner):
        temp_db.update_user_settings(
            owner,
            {"settings_json": {"demo_mode": True, "marker": owner}},
        )
        temp_db.insert_item(
            {
                "id": f"{owner}-task",
                "owner_id": owner,
                "type": "task",
                "title": "清理验证",
                "status": "open",
                "priority": 3,
                "created_at": "2030-01-01T12:00:00",
                "updated_at": "2030-01-01T12:00:00",
            }
        )

    temp_db.register_login_code("a" * 64, owner_id, issued_at=1, expires_at=2)
    temp_db.register_web_session(
        "b" * 64,
        "demo-device",
        owner_id,
        "demo-csrf",
        created_at=1,
        expires_at=2,
        demo=True,
    )
    temp_db.register_widget_token("demo-widget", owner_id, issued_at=1, expires_at=2)

    conn = temp_db.get_connection()
    with conn:
        conn.execute(
            "INSERT INTO reminder_logs (item_id, remind_time) VALUES (?, ?)",
            (f"{owner_id}-task", "2030-01-01T13:00:00"),
        )
        conn.execute(
            """
            INSERT INTO event_collections
                (id, owner_id, kind, title, created_at, updated_at)
            VALUES (?, ?, 'multi_node', '演示集合', ?, ?)
            """,
            ("demo-collection", owner_id, "2030-01-01T12:00:00", "2030-01-01T12:00:00"),
        )
        conn.execute(
            """
            INSERT INTO scheduled_delivery_outbox
                (task_name, owner_id, period_key, delivery_key, created_at, updated_at)
            VALUES ('daily', ?, '2030-01-01', 'demo-delivery', ?, ?)
            """,
            (owner_id, "2030-01-01T12:00:00", "2030-01-01T12:00:00"),
        )
        conn.execute(
            "INSERT INTO operation_logs (user_id, action, created_at) VALUES (?, 'test', ?)",
            (owner_id, "2030-01-01T12:00:00"),
        )
        conn.execute(
            "INSERT INTO transfer_logs (owner_id, action, created_at) VALUES (?, 'test', ?)",
            (owner_id, "2030-01-01T12:00:00"),
        )
        conn.execute(
            "INSERT INTO imported_bundles (owner_id, bundle_id, imported_at) VALUES (?, ?, ?)",
            (owner_id, "demo-bundle", "2030-01-01T12:00:00"),
        )

    demo_space_module.purge_demo_owner(temp_db, regular_owner)
    demo_space_module.ensure_demo_access(
        temp_db,
        regular_owner,
        now=datetime(2030, 1, 1, 12, 0),
    )
    assert temp_db.get_user_settings(regular_owner)["settings_json"]["marker"] == regular_owner

    demo_space_module.purge_demo_owner(temp_db, owner_id)

    owner_queries = (
        ("items", "owner_id"),
        ("event_collections", "owner_id"),
        ("scheduled_delivery_outbox", "owner_id"),
        ("user_settings", "user_id"),
        ("operation_logs", "user_id"),
        ("transfer_logs", "owner_id"),
        ("imported_bundles", "owner_id"),
        ("login_code_registry", "owner_id"),
        ("web_session_registry", "owner_id"),
        ("widget_token_registry", "owner_id"),
    )
    for table, column in owner_queries:
        count = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {column} = ?",
            (owner_id,),
        ).fetchone()[0]
        assert count == 0, table
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM reminder_logs WHERE item_id = ?",
            (f"{owner_id}-task",),
        ).fetchone()[0]
        == 0
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM items_fts WHERE id = ?",
            (f"{owner_id}-task",),
        ).fetchone()[0]
        == 0
    )


def test_purge_expired_demo_users_fails_closed_for_malformed_settings(
    temp_db: Database,
) -> None:
    """损坏、伪布尔或缺少过期时间的保留命名空间记录都应被回收。"""

    raw_settings = {
        "demo_web_active": json.dumps(
            {"demo_mode": True, "demo_expires_at": "2030-01-03T00:00:00+00:00"}
        ),
        "demo_web_expired": json.dumps(
            {"demo_mode": True, "demo_expires_at": "2029-12-31T00:00:00"}
        ),
        "demo_web_missing_expiry": json.dumps({"demo_mode": True}),
        "demo_web_fake_mode": json.dumps(
            {"demo_mode": "true", "demo_expires_at": "2031-01-01T00:00:00"}
        ),
        "demo_web_scalar": "[]",
        "demo_web_bad_json": "{",
    }
    conn = temp_db.get_connection()
    with conn:
        conn.executemany(
            "INSERT INTO user_settings (user_id, settings_json) VALUES (?, ?)",
            raw_settings.items(),
        )

    removed = demo_space_module.purge_expired_demo_users(
        temp_db,
        now=datetime(2030, 1, 1, 12, 0),
    )

    assert removed == 5
    remaining = {
        row[0]
        for row in conn.execute(
            "SELECT user_id FROM user_settings WHERE user_id LIKE 'demo_web_%'"
        ).fetchall()
    }
    assert remaining == {"demo_web_active"}


def test_ensure_demo_access_rejects_non_boolean_demo_marker(temp_db: Database) -> None:
    """字符串形式的真值不得把普通或损坏设置提升为有效演示会话。"""

    owner_id = "demo_web_fake_marker"
    temp_db.update_user_settings(
        owner_id,
        {
            "settings_json": {
                "demo_mode": "true",
                "demo_expires_at": "2031-01-01T00:00:00",
            }
        },
    )

    with pytest.raises(AuthError, match="unavailable"):
        demo_space_module.ensure_demo_access(
            temp_db,
            owner_id,
            now=datetime(2030, 1, 1, 12, 0),
        )

    assert (
        temp_db.get_connection()
        .execute("SELECT COUNT(*) FROM user_settings WHERE user_id = ?", (owner_id,))
        .fetchone()[0]
        == 0
    )


def test_ensure_demo_access_purges_expired_demo_owner(
    temp_db: Database,
    expired_demo_owner: str,
) -> None:
    """直接访问过期演示空间时应失败关闭，并同步删除其数据。"""

    with pytest.raises(AuthError, match="expired"):
        demo_space_module.ensure_demo_access(
            temp_db,
            expired_demo_owner,
            now=datetime(2026, 4, 8, 10, 0, 0),
        )

    assert temp_db.get_items(expired_demo_owner, filters={"type": "task"}, limit=10) == []
    assert (
        temp_db.get_user_settings(expired_demo_owner)["settings_json"].get("demo_mode") is not True
    )


def test_expired_demo_token_is_rejected_and_demo_data_is_purged(
    client: Any,
    temp_db: Database,
    expired_demo_owner: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """带过期演示主体的浏览器会话也必须被依赖层拒绝并清理。"""

    class _FrozenDemoDateTime(datetime):
        """让依赖层使用确定性墙钟，不引入真实等待。"""

        @classmethod
        def now(cls, tz: tzinfo | None = None) -> _FrozenDemoDateTime:
            """返回刚好晚于演示过期时间的固定墙钟。"""

            return cls(2026, 4, 8, 10, 0, 0, tzinfo=tz)

    monkeypatch.setattr(demo_space_module, "datetime", _FrozenDemoDateTime)
    session = create_web_session(expired_demo_owner, demo=True)
    client.cookies.set(SESSION_COOKIE_NAME, session.session_id)

    res = client.get("/api/auth/session")

    assert res.status_code == 401
    assert "expired" in res.json()["message"]
    assert temp_db.get_items(expired_demo_owner, filters={"type": "task"}, limit=10) == []
    assert (
        temp_db.get_user_settings(expired_demo_owner)["settings_json"].get("demo_mode") is not True
    )


def test_login_page_sources_offer_demo_entry() -> None:
    """登录页、浏览器 API 和路由源码应共同保留无本地 Token 的演示入口。"""

    app_src = (ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "app.js").read_text(
        encoding="utf-8"
    )
    api_src = (ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "api.js").read_text(
        encoding="utf-8"
    )
    html = (ROOT / "plugins" / "pendo" / "web" / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    auth_src = (ROOT / "plugins" / "pendo" / "web" / "api" / "auth_routes.py").read_text(
        encoding="utf-8"
    )

    assert "createDemoSession" in app_src
    assert "const demoBtn = document.getElementById('login-demo-btn');" in app_src
    assert "const enterDemo = async () => {" in app_src
    assert "demoBtn.onclick = enterDemo;" in app_src
    assert "export function createDemoSession()" in api_src
    assert "'api/auth/demo'" in api_src
    assert 'id="login-demo-btn"' in html
    assert re.search(r'id="login-demo-btn"[^>]*>\s*Demo\s*</button>', html)
    assert '@router.post("/auth/demo")' in auth_src
    assert "localStorage" not in api_src
    assert "Authorization" not in api_src
    assert "credentials: 'same-origin'" in api_src
    assert "X-CSRF-Token" in api_src
    assert "exchangeLoginCode" in app_src
    assert "history.replaceState" in app_src
