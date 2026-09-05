"""Pendo Web 依赖注入、Cookie/CSRF 与 Widget 授权边界回归。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import HTTPException, Request

from plugins.pendo.services.db import Database
from plugins.pendo.web import deps
from plugins.pendo.web.auth import AuthError, create_web_session


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


def test_database_dependency_reports_uninitialized_and_returns_configured_instance(
    monkeypatch: pytest.MonkeyPatch,
    pendo_db: Database,
) -> None:
    synchronized: list[Database | None] = []

    def record_singleton(db: Database | None) -> None:
        synchronized.append(db)

    monkeypatch.setattr(deps, "_db_instance", None)
    monkeypatch.setattr(deps, "set_database_singleton", record_singleton)
    with pytest.raises(HTTPException) as exc_info:
        deps.get_db()
    assert exc_info.value.status_code == 503

    deps.set_db(pendo_db)

    assert deps.get_db() is pendo_db
    assert synchronized == [pendo_db]


@pytest.mark.parametrize("authorization", ["Basic token", "Bearer ", "bearer"])
def test_malformed_authorization_header_fails_closed(authorization: str) -> None:
    with pytest.raises(HTTPException) as exc_info:
        deps.get_current_user(_request(path="/api/widget/summary"), authorization)

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Invalid authorization header"


@pytest.mark.parametrize(
    ("payload", "method", "path", "expected_status"),
    [
        ({"owner_id": "owner", "kind": "browser"}, "GET", "/api/widget/summary", 401),
        (
            {"owner_id": "owner", "kind": "widget", "scope": "widget:write"},
            "GET",
            "/api/widget/summary",
            403,
        ),
        (
            {"owner_id": "owner", "kind": "widget", "scope": "widget:read"},
            "POST",
            "/api/widget/summary",
            403,
        ),
        (
            {"owner_id": "owner", "kind": "widget", "scope": "widget:read"},
            "GET",
            "/api/dashboard",
            403,
        ),
        (
            {"owner_id": "owner", "kind": "widget", "scope": "widget:read"},
            "GET",
            "/api/widget/summary",
            None,
        ),
    ],
)
def test_widget_bearer_requires_kind_scope_read_method_and_route(
    monkeypatch: pytest.MonkeyPatch,
    pendo_db: Database,
    payload: dict[str, Any],
    method: str,
    path: str,
    expected_status: int | None,
) -> None:
    def fake_verify(_token: str, **_kwargs: Any) -> dict[str, Any]:
        return payload

    monkeypatch.setattr(deps, "_db_instance", pendo_db)
    monkeypatch.setattr(deps, "verify_token", fake_verify)
    request = _request(method=method, path=path)
    if expected_status is None:
        assert deps.get_current_user(request, "Bearer token") == "owner"
        return

    with pytest.raises(HTTPException) as exc_info:
        deps.get_current_user(request, "Bearer token")
    assert exc_info.value.status_code == expected_status


def test_widget_bearer_rejects_invalid_subject_and_auth_errors(
    monkeypatch: pytest.MonkeyPatch,
    pendo_db: Database,
) -> None:
    request = _request(path="/api/widget/summary")
    monkeypatch.setattr(deps, "_db_instance", pendo_db)

    monkeypatch.setattr(
        deps,
        "verify_token",
        lambda _token, **_kwargs: {
            "owner_id": "",
            "kind": "widget",
            "scope": "widget:read",
        },
    )
    with pytest.raises(HTTPException) as invalid_owner:
        deps.get_current_user(request, "Bearer token")
    assert invalid_owner.value.status_code == 401

    def fail_verify(_token: str, **_kwargs: Any) -> dict[str, Any]:
        raise AuthError("expired test token")

    monkeypatch.setattr(deps, "verify_token", fail_verify)
    with pytest.raises(HTTPException) as auth_error:
        deps.get_current_user(request, "Bearer token")
    assert auth_error.value.status_code == 401
    assert auth_error.value.detail == "expired test token"


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


def test_demo_access_is_checked_once_per_user_resolution(
    monkeypatch: pytest.MonkeyPatch,
    pendo_db: Database,
) -> None:
    from plugins.pendo.web.services import demo_space

    checked: list[tuple[Database, str]] = []

    def record_access(db: Database, owner_id: str) -> None:
        checked.append((db, owner_id))

    monkeypatch.setattr(deps, "get_db", lambda: pendo_db)
    monkeypatch.setattr(demo_space, "ensure_demo_access", record_access)
    session = create_web_session("demo-owner", demo=True)
    request = _request(cookies={deps.SESSION_COOKIE_NAME: session.session_id})

    assert deps.get_current_session(request) == session
    assert deps.get_current_user(request, None) == "demo-owner"
    assert checked == [(pendo_db, "demo-owner")]
