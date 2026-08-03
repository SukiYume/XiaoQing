"""Pendo Web 看板路由的注册和所有者范围转发回归。"""

from __future__ import annotations

from typing import cast
from unittest.mock import Mock

import pytest

from plugins.pendo.services.db import Database
from plugins.pendo.web.api import dashboard as dashboard_module


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
