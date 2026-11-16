"""Pendo Web 看板路由的注册和所有者范围转发。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import tests.helpers.pendo_web_transfer_test_support as _fixture_support
from plugins.pendo.services.db import Database
from plugins.pendo.web.analytics.dashboard_overview import build_dashboard_overview
from plugins.pendo.web.api import dashboard as dashboard_module
from plugins.pendo.web.api import widget
from tests.helpers.pendo_web_transfer_test_support import OWNER_ID

auth_headers = _fixture_support.auth_headers


def test_dashboard_router_registers_one_read_endpoint() -> None:
    """看板模块只应暴露一个 GET 聚合入口。"""

    assert len(dashboard_module.router.routes) == 1
    route = dashboard_module.router.routes[0]
    assert route.path == "/dashboard"
    assert getattr(route, "methods", set()) == {"GET"}


def test_dashboard_endpoint_forwards_database_and_current_owner(
    client: Any, temp_db: Database, auth_headers: dict
) -> None:
    """端点只汇总认证主体自己的数据，并套用统一响应信封。"""

    for index in range(2):
        temp_db.insert_item(
            {
                "id"      : f"mine-{index}",
                "owner_id": OWNER_ID,
                "type"    : "task",
                "title"   : "我的待办",
                "status"  : "open",
            }
        )
    for index in range(5):
        temp_db.insert_item(
            {
                "id"      : f"theirs-{index}",
                "owner_id": "other-owner",
                "type"    : "task",
                "title"   : "别人的待办",
                "status"  : "open",
            }
        )

    response = client.get("/api/dashboard")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["message"] == ""
    assert payload["data"]["summary"]["tasks_pending"] == 2
    assert {task["title"] for task in payload["data"]["tasks"]["active"]} == {"我的待办"}


def test_dashboard_and_widget_show_currency_specific_totals(currency_db):
    now       = datetime(2026, 3, 2, 10)
    dashboard = build_dashboard_overview(currency_db, "owner", now)
    assert dashboard["month_summary"]["expense"] == 100
    assert dashboard["ledger_by_currency"]["USD"]["expense"] == 200
    panel = widget._build_ledger_panel(currency_db, "owner", now)
    assert panel["summary"]["primary"] == "支出 ¥100"
    assert "USD 支出 200" in panel["summary"]["secondary"]
    usd = next(item for item in panel["items"] if item["title"] == "USD")
    assert usd["amount_text"] == "-USD 200"
