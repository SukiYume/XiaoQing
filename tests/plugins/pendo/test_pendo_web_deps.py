"""Pendo Web 依赖注入、Cookie/CSRF 与 Widget 授权边界。"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from fastapi import HTTPException, Request

from plugins.pendo.services.db import Database
from plugins.pendo.utils import db_ops
from plugins.pendo.web import deps
from plugins.pendo.web.auth import create_web_session, generate_widget_token
from plugins.pendo.web.services.demo_space import create_demo_session


def _request(
    *,
    method: str                    = "GET",
    path: str                      = "/api/dashboard",
    cookies: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> Request:
    """构造依赖函数所需的最小 Request 视图。"""
    return cast(
        Request,
        SimpleNamespace(
            method=method,
            url=SimpleNamespace(path=path),
            cookies = cookies or {},
            headers = headers or {},
            state   = SimpleNamespace(),
        ),
    )


@pytest.fixture(autouse=True)
def _reset_browser_sessions(db: Database) -> Iterator[None]:
    from plugins.pendo.web import auth

    auth.configure_auth_database(db)
    yield
    with auth._AUTH_LOCK:
        auth._AUTH_DATABASE = None


@pytest.fixture
def pendo_db(tmp_path: Path) -> Iterator[Database]:
    """为依赖层测试提供显式生命周期的独立数据库。"""
    db = Database(str(tmp_path / "pendo-web-deps.db"))
    try:
        yield db
    finally:
        db.cleanup()


@pytest.fixture
def bound_db(pendo_db: Database, monkeypatch: pytest.MonkeyPatch) -> Database:
    """把依赖层绑定到本用例数据库；模块级引用在用例结束时还原。"""
    monkeypatch.setattr(deps, "_db_instance", None)
    monkeypatch.setattr(db_ops, "_db_singleton", None)
    deps.set_db(pendo_db)
    return pendo_db


def test_database_dependency_reports_uninitialized_and_returns_configured_instance(
    monkeypatch: pytest.MonkeyPatch,
    pendo_db: Database,
) -> None:
    monkeypatch.setattr(deps, "_db_instance", None)
    monkeypatch.setattr(db_ops, "_db_singleton", None)
    with pytest.raises(HTTPException) as exc_info:
        deps.get_db()
    assert exc_info.value.status_code == 503

    deps.set_db(pendo_db)

    assert deps.get_db() is pendo_db
    assert db_ops.get_database(None) is pendo_db


@pytest.mark.parametrize("authorization", ["Basic token", "Bearer ", "bearer"])
def test_malformed_authorization_header_fails_closed(authorization: str) -> None:
    with pytest.raises(HTTPException) as exc_info:
        deps.get_current_user(_request(path="/api/widget/summary"), authorization)

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Invalid authorization header"


@pytest.mark.parametrize(
    ("method", "path", "expected_status"),
    [
        ("POST", "/api/widget/summary", 403),
        ("GET", "/api/dashboard", 403),
        ("GET", "/api/widget/summary", None),
    ],
)
def test_widget_bearer_is_limited_to_get_on_widget_routes(
    bound_db: Database,
    method: str,
    path: str,
    expected_status: int | None,
) -> None:
    token = generate_widget_token("owner", db=bound_db)
    request = _request(method=method, path=path)
    if expected_status is None:
        assert deps.get_current_user(request, f"Bearer {token}") == "owner"
        return

    with pytest.raises(HTTPException) as exc_info:
        deps.get_current_user(request, f"Bearer {token}")
    assert exc_info.value.status_code == expected_status


@pytest.mark.parametrize("revoked", [False, True], ids=["malformed-token", "revoked-token"])
def test_widget_bearer_maps_token_verification_errors_to_401(
    bound_db: Database,
    revoked: bool,
) -> None:
    request = _request(path="/api/widget/summary")
    token  = "not-a-jwt"
    detail = "Invalid token"
    if revoked:
        token = generate_widget_token("owner", db=bound_db)
        detail = "Widget token has been revoked"
        assert bound_db.revoke_widget_tokens("owner") == 1

    with pytest.raises(HTTPException) as auth_error:
        deps.get_current_user(request, f"Bearer {token}")
    assert auth_error.value.status_code == 401
    assert auth_error.value.detail == detail


def test_browser_cookie_requires_csrf_only_for_unsafe_methods() -> None:
    session = create_web_session("browser-owner")
    cookies = {deps.SESSION_COOKIE_NAME: session.session_id}

    assert deps.get_current_user(_request(cookies=cookies)) == "browser-owner"

    with pytest.raises(HTTPException) as missing_csrf:
        deps.get_current_user(_request(method="POST", cookies=cookies), None)
    assert missing_csrf.value.status_code == 403

    request = _request(
        method  = "POST",
        cookies = cookies,
        headers = {deps.CSRF_HEADER_NAME: session.csrf_token},
    )
    assert deps.get_current_user(request, None) == "browser-owner"


def test_demo_access_is_checked_once_per_user_resolution(bound_db: Database) -> None:
    owner_id = create_demo_session(bound_db)["owner_id"]
    session = create_web_session(owner_id, demo=True)
    cookies = {deps.SESSION_COOKIE_NAME: session.session_id}
    request = _request(cookies=cookies)

    assert deps.get_current_session(request) == session

    # 同一请求已经确认过演示空间有效；之后即使空间过期，本请求内不会再校验一次。
    expired = (datetime.now() - timedelta(hours=1)).replace(microsecond=0).isoformat()
    bound_db.update_user_settings(
        owner_id, {"settings_json": {"demo_mode": True, "demo_expires_at": expired}}
    )
    assert deps.get_current_user(request, None) == owner_id

    with pytest.raises(HTTPException) as exc_info:
        deps.get_current_session(_request(cookies=cookies))
    assert exc_info.value.status_code == 401
