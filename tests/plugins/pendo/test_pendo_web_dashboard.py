"""Pendo Web 看板路由的注册和所有者范围转发回归。"""

from __future__ import annotations

from datetime import datetime
from typing import cast
from unittest.mock import Mock

import pytest

from plugins.pendo.services.db import Database
from plugins.pendo.web.analytics.dashboard_overview import build_dashboard_overview
from plugins.pendo.web.api import dashboard as dashboard_module
from plugins.pendo.web.api import widget


def test_dashboard_router_registers_one_read_endpoint() -> None:
    """看板模块只应暴露一个 GET 聚合入口。"""

    assert len(dashboard_module.router.routes) == 1
    route = dashboard_module.router.routes[0]
    assert route.path == "/dashboard"
    assert getattr(route, "methods", set()) == {"GET"}


def test_dashboard_endpoint_forwards_database_and_current_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """端点应原样转发认证主体与数据库，并套用统一响应信封。"""

    overview = {"tasks": {"open": 3}, "events": []}
    build_overview = Mock(return_value=overview)
    monkeypatch.setattr(dashboard_module, "build_dashboard_overview", build_overview)
    db = cast(Database, Mock(spec=Database))

    payload = dashboard_module.get_dashboard(owner_id="owner-a", db=db)

    assert payload == {"ok": True, "data": overview, "message": ""}
    build_overview.assert_called_once_with(db=db, owner_id="owner-a")


@pytest.fixture
def currency_db(tmp_path):
    db = Database(str(tmp_path / "currency.db"))
    for currency, amount in [("CNY", 100), ("USD", 200)]:
        db.insert_item(
            {
                "id": currency,
                "owner_id": "owner",
                "type": "ledger",
                "title": currency,
                "currency": currency,
                "amount": amount,
                "amount_cents": amount * 100,
                "transaction_type": "expense",
                "ledger_category": "餐饮",
                "ledger_date": "2026-03-01",
            }
        )
    yield db
    db.cleanup()


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
