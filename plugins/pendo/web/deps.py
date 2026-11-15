"""Pendo Web 的数据库、浏览器会话、CSRF 与 Widget 权限依赖。"""

from __future__ import annotations

import secrets
from typing import Annotated, Final

from fastapi import Header, HTTPException, Request

from ..services.db import Database
from ..utils.db_ops import set_database_singleton
from .auth import AuthError, WebSession, configure_auth_database, get_web_session, verify_token

SESSION_COOKIE_NAME   : Final = "pendo_web_session"
CSRF_HEADER_NAME      : Final = "X-CSRF-Token"
_SAFE_METHODS         : Final = frozenset({"GET", "HEAD", "OPTIONS"})
_REQUEST_SESSION_STATE: Final = "_pendo_web_session"

# ``server.create_app`` 在开始接收请求前设置此引用。
_db_instance: Database | None = None


def set_db(db: Database) -> None:
    """设置 Web 依赖和遗留同步工具共用的数据库实例。"""
    global _db_instance
    _db_instance = db
    set_database_singleton(db)
    configure_auth_database(db)


def get_db() -> Database:
    """取得共享数据库；服务尚未初始化时返回 503。"""
    if _db_instance is None:
        raise HTTPException(status_code=503, detail="Database not initialized")
    return _db_instance


def get_current_user(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> str:
    """认证 Widget Bearer 或浏览器 Cookie，并对写请求执行 CSRF 校验。"""
    try:
        if authorization:
            scheme, _, token = authorization.partition(" ")
            if scheme.casefold() != "bearer" or not token.strip():
                raise HTTPException(status_code=401, detail="Invalid authorization header")
            # verify_token 已确认这是登记在册、未吊销的只读 Widget 令牌，且 owner_id 是非空字符串。
            payload = verify_token(token.strip(), db=get_db())
            path   = request.url.path
            method = request.method
            if method != "GET" or not path.startswith("/api/widget/"):
                raise HTTPException(
                    status_code = 403,
                    detail      = "Widget token is limited to /api/widget/* read-only requests",
                )
            return str(payload["owner_id"])

        session = get_current_session(request)
        if request.method not in _SAFE_METHODS:
            csrf = request.headers.get(CSRF_HEADER_NAME, "")
            if not csrf or not secrets.compare_digest(csrf, session.csrf_token):
                raise HTTPException(status_code=403, detail="Missing or invalid CSRF token")
        return session.owner_id
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=exc.message) from exc


def get_current_session(request: Request) -> WebSession:
    """只从 Cookie 读取浏览器会话，并确认 demo 工作区仍可访问。"""

    cached = getattr(request.state, _REQUEST_SESSION_STATE, None)
    if isinstance(cached, WebSession):
        return cached
    try:
        session = get_web_session(request.cookies.get(SESSION_COOKIE_NAME))
        if session.demo:
            from .services.demo_space import ensure_demo_access

            ensure_demo_access(get_db(), session.owner_id)
        setattr(request.state, _REQUEST_SESSION_STATE, session)
        return session
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=exc.message) from exc
