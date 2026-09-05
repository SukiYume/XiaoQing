"""提供短期登录码交换、浏览器会话和公共演示认证路由。"""

import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from ...config import PendoConfig
from ...services.db import Database
from ..auth import (
    AuthError,
    WebSession,
    WebSessionInfo,
    consume_login_code,
    create_web_session,
    list_web_sessions,
    revoke_web_session,
    revoke_web_session_device,
)
from ..deps import SESSION_COOKIE_NAME, get_current_session, get_current_user, get_db
from ..services.demo_space import DemoCapacityError, create_demo_session, purge_demo_owner

router = APIRouter()


# 当前 Pydantic 运行依赖没有向 Mypy 暴露基类类型，模型字段仍由请求验证覆盖。
class LoginExchangeRequest(BaseModel):  # type: ignore[misc]
    """浏览器提交的一次性登录码请求。"""

    code: str


def _session_payload(session: WebSession) -> dict[str, object]:
    """序列化会话启动所需的非秘密字段。"""

    return {
        "owner_id": session.owner_id,
        "expires_at": int(session.expires_at),
        "csrf_token": session.csrf_token,
        "demo": session.demo,
    }


def _device_payload(session: WebSessionInfo, current_session: WebSession) -> dict[str, object]:
    """序列化设备列表项，并标记当前浏览器会话。"""

    return {
        "device_id": session.device_id,
        "created_at": int(session.created_at),
        "expires_at": int(session.expires_at),
        "current": session.device_id == current_session.device_id,
    }


def _set_session_cookie(response: Response, session: WebSession) -> None:
    """写入与服务端会话同寿命、仅同站脚本不可读的 Cookie。"""

    response.set_cookie(
        key      = SESSION_COOKIE_NAME,
        value    = session.session_id,
        max_age  = max(1, int(session.expires_at - time.time())),
        httponly = True,
        secure   = PendoConfig.runtime().web_session_cookie_secure,
        samesite = "strict",
        path     = "/",
    )


def _create_cookie_session(
    response: Response,
    owner_id: str,
    *,
    expires_seconds: int,
    demo: bool = False,
) -> WebSession:
    """原子建立服务端会话和 Cookie；Cookie 写入失败时撤销持久会话。"""

    session = create_web_session(
        owner_id,
        expires_seconds = expires_seconds,
        demo            = demo,
    )
    try:
        _set_session_cookie(response, session)
    except Exception:
        revoke_web_session(session.session_id)
        raise
    return session


@router.post("/auth/exchange")
def exchange_login_code(
    body: LoginExchangeRequest,
    response: Response,
) -> dict[str, object]:
    """原子消费私聊登录码，并交换为服务端浏览器会话。"""

    try:
        owner_id = consume_login_code(body.code)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=exc.message) from exc
    session = _create_cookie_session(
        response,
        owner_id,
        expires_seconds=PendoConfig.WEB_SESSION_EXPIRE_SECONDS,
    )
    return {"ok": True, "data": _session_payload(session), "message": ""}


@router.post("/auth/demo")
def create_demo_auth(
    request: Request,
    response: Response,
    db: Database = Depends(get_db),
) -> dict[str, object]:
    """在演示容量允许时创建临时数据空间及不超期的浏览器会话。"""

    if not PendoConfig.runtime().web_demo_enabled:
        raise HTTPException(status_code=404, detail="Demo mode is disabled")
    client = request.client.host if request.client else "unknown"
    try:
        demo = create_demo_session(db=db, client_key=client)
    except DemoCapacityError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc

    owner_id = demo["owner_id"]
    try:
        session = _create_cookie_session(
            response,
            owner_id,
            expires_seconds=min(
                PendoConfig.WEB_SESSION_EXPIRE_SECONDS,
                PendoConfig.WEB_DEMO_EXPIRE_HOURS * 60 * 60,
            ),
            demo=True,
        )
    except Exception:
        purge_demo_owner(db, owner_id)
        raise
    return {"ok": True, "data": _session_payload(session), "message": ""}


@router.get("/auth/session")
def get_auth_session(
    session: WebSession = Depends(get_current_session),
) -> dict[str, object]:
    """返回应用启动所需的当前浏览器会话元数据。"""

    return {"ok": True, "data": _session_payload(session), "message": ""}


@router.get("/auth/sessions")
def get_auth_sessions(
    session: WebSession = Depends(get_current_session),
) -> dict[str, object]:
    """列出当前所有者的脱敏设备会话，不暴露任何凭据。"""

    sessions = list_web_sessions(session.owner_id)
    return {
        "ok": True,
        "data": {"sessions": [_device_payload(item, session) for item in sessions]},
        "message": "",
    }


@router.delete("/auth/sessions/{device_id}")
def revoke_auth_session(
    device_id: str,
    session: WebSession = Depends(get_current_session),
    owner_id: str       = Depends(get_current_user),
) -> dict[str, object]:
    """在 Cookie 与 CSRF 校验后撤销当前所有者的指定设备会话。"""

    if not revoke_web_session_device(owner_id, device_id):
        raise HTTPException(status_code=404, detail="Session device was not found")
    return {"ok": True, "data": {"current": device_id == session.device_id}, "message": ""}


@router.post("/auth/logout")
def logout(
    response: Response,
    session: WebSession = Depends(get_current_session),
    _owner_id: str      = Depends(get_current_user),
) -> dict[str, object]:
    """撤销当前服务端会话，并使用相同属性删除浏览器 Cookie。"""

    revoke_web_session(session.session_id)
    response.delete_cookie(
        key      = SESSION_COOKIE_NAME,
        httponly = True,
        secure   = PendoConfig.runtime().web_session_cookie_secure,
        samesite = "strict",
        path     = "/",
    )
    return {"ok": True, "data": {}, "message": ""}
