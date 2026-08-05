"""Pendo Web 一次性登录、浏览器会话与 JWT 签名验证。"""

from __future__ import annotations

import os
import secrets
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import jwt

from core.plugin_base import atomic_write_text

from ..config import PendoConfig

if TYPE_CHECKING:
    from ..services.db import Database

_ALGORITHM: Final = "HS256"
_TOKEN_ISSUER: Final = "pendo-web"
_TOKEN_TYPE: Final = "pendo-web"
_WIDGET_KIND: Final = "widget"
_WIDGET_SCOPE: Final = "widget:read"
_MIN_SECRET_BYTES: Final = 32
_MAX_OWNER_ID_CHARS: Final = 256
_RESERVED_TOKEN_CLAIMS: Final = frozenset({"exp", "iat", "iss", "owner_id", "sub", "typ"})
_SECRET_FILE: Path | None = None
_SECRET_CACHE: str | None = None
_AUTH_LOCK = threading.Lock()
_SECRET_LOCK = threading.Lock()
_LOGIN_CODES: dict[str, LoginCode] = {}
_SESSIONS: dict[str, WebSession] = {}


def configure_auth_storage(data_dir: Path) -> None:
    """Bind signing-key persistence to Core's writable plugin data directory."""

    global _SECRET_CACHE, _SECRET_FILE
    directory = Path(data_dir)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    secret_file = directory / "web_token_secret.txt"
    with _SECRET_LOCK:
        if _SECRET_FILE != secret_file:
            _SECRET_FILE = secret_file
            _SECRET_CACHE = None


class AuthError(Exception):
    """可安全返回给认证调用方的失败。"""

    def __init__(self, message: str) -> None:
        """保存可展示消息，同时初始化标准异常文本。"""
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class LoginCode:
    """绑定用户且只能消费一次的短期交换码。"""

    owner_id: str
    expires_at: float


@dataclass(frozen=True)
class WebSession:
    """保存在服务端内存中的可撤销浏览器会话。"""

    session_id: str
    device_id: str
    owner_id: str
    csrf_token: str
    created_at: float
    expires_at: float
    demo: bool = False


def _normalize_owner_id(owner_id: str) -> str:
    """规范化认证主体，并拒绝空值、控制字符和异常大标识。"""
    if not isinstance(owner_id, str):
        raise ValueError("owner_id must be a string")
    normalized = owner_id.strip()
    if not normalized:
        raise ValueError("owner_id is required")
    if len(normalized) > _MAX_OWNER_ID_CHARS:
        raise ValueError(f"owner_id must not exceed {_MAX_OWNER_ID_CHARS} characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in normalized):
        raise ValueError("owner_id contains control characters")
    return normalized


def _positive_lifetime_seconds(value: int) -> int:
    """校验登录码和浏览器会话的正整数有效期。"""
    if type(value) is not int or value < 1:
        raise ValueError("expires_seconds must be a positive integer")
    return value


def _validated_secret(secret: str, source: str) -> str:
    """确保 HMAC 密钥达到 HS256 所需的最小强度。"""
    if len(secret.encode("utf-8")) < _MIN_SECRET_BYTES:
        raise RuntimeError(f"{source} must contain at least {_MIN_SECRET_BYTES} bytes")
    return secret


def _prune_expired(now: float) -> None:
    """清理过期登录码和会话；调用方必须持有认证锁。"""
    for code, grant in list(_LOGIN_CODES.items()):
        if grant.expires_at <= now:
            _LOGIN_CODES.pop(code, None)
    for session_id, session in list(_SESSIONS.items()):
        if session.expires_at <= now:
            _SESSIONS.pop(session_id, None)


def issue_login_code(
    owner_id: str,
    expires_seconds: int = PendoConfig.WEB_LOGIN_CODE_EXPIRE_SECONDS,
) -> str:
    """签发绑定用户、限时有效且只能消费一次的登录交换码。"""
    normalized_owner = _normalize_owner_id(owner_id)
    lifetime = _positive_lifetime_seconds(expires_seconds)
    code = secrets.token_urlsafe(32)
    now = time.time()
    with _AUTH_LOCK:
        _prune_expired(now)
        _LOGIN_CODES[code] = LoginCode(owner_id=normalized_owner, expires_at=now + lifetime)
    return code


def consume_login_code(code: str) -> str:
    """原子消费登录码并返回其绑定用户。"""

    if not isinstance(code, str) or not code.strip():
        raise AuthError("Missing login code")
    now = time.time()
    with _AUTH_LOCK:
        _prune_expired(now)
        grant = _LOGIN_CODES.pop(code.strip(), None)
    if grant is None:
        raise AuthError("Login code is invalid, expired, or already used")
    return grant.owner_id


def create_web_session(
    owner_id: str,
    *,
    expires_seconds: int = 8 * 60 * 60,
    demo: bool = False,
) -> WebSession:
    """创建含 CSRF 令牌、可按设备撤销的服务端浏览器会话。"""
    normalized_owner = _normalize_owner_id(owner_id)
    lifetime = _positive_lifetime_seconds(expires_seconds)
    if type(demo) is not bool:
        raise ValueError("demo must be a boolean")
    now = time.time()
    session = WebSession(
        session_id=secrets.token_urlsafe(32),
        device_id=secrets.token_urlsafe(12),
        owner_id=normalized_owner,
        csrf_token=secrets.token_urlsafe(32),
        created_at=now,
        expires_at=now + lifetime,
        demo=demo,
    )
    with _AUTH_LOCK:
        _prune_expired(now)
        _SESSIONS[session.session_id] = session
    return session


def get_web_session(session_id: str | None) -> WebSession:
    """读取未过期会话；缺失、撤销或过期均按认证失败处理。"""
    if not session_id:
        raise AuthError("Missing web session")
    now = time.time()
    with _AUTH_LOCK:
        _prune_expired(now)
        session = _SESSIONS.get(session_id)
    if session is None:
        raise AuthError("Web session is invalid or expired")
    return session


def revoke_web_session(session_id: str | None) -> None:
    """幂等撤销指定浏览器会话。"""
    if not session_id:
        return
    with _AUTH_LOCK:
        _SESSIONS.pop(session_id, None)


def list_web_sessions(owner_id: str) -> list[WebSession]:
    """按最新优先列出用户的内部会话记录，供设备视图脱敏展示。"""
    normalized_owner = _normalize_owner_id(owner_id)
    now = time.time()
    with _AUTH_LOCK:
        _prune_expired(now)
        return sorted(
            (session for session in _SESSIONS.values() if session.owner_id == normalized_owner),
            key=lambda session: (session.created_at, session.session_id),
            reverse=True,
        )


def revoke_web_session_device(owner_id: str, device_id: str) -> bool:
    """按非秘密设备标识撤销用户自己的一个会话。"""
    normalized_owner = _normalize_owner_id(owner_id)
    normalized_device = str(device_id or "").strip()
    if not normalized_device:
        return False
    with _AUTH_LOCK:
        _prune_expired(time.time())
        for session_id, session in list(_SESSIONS.items()):
            if session.owner_id == normalized_owner and session.device_id == normalized_device:
                _SESSIONS.pop(session_id, None)
                return True
    return False


def _get_secret_key() -> str:
    """加载稳定的 JWT 签名密钥，并串行化首次生成。

    优先级：
    1. 环境变量 ``PENDO_WEB_TOKEN_SECRET``；
    2. Core 分配的 Pendo 数据目录下的持久化密钥文件。
    """
    global _SECRET_CACHE
    if _SECRET_CACHE:
        return _SECRET_CACHE

    with _SECRET_LOCK:
        if _SECRET_CACHE:
            return _SECRET_CACHE

        env_secret = os.getenv("PENDO_WEB_TOKEN_SECRET", "").strip()
        if env_secret:
            _SECRET_CACHE = _validated_secret(env_secret, "PENDO_WEB_TOKEN_SECRET")
            return _SECRET_CACHE

        secret_file = _SECRET_FILE
        if secret_file is None:
            raise RuntimeError("Pendo auth storage is not configured")
        secret_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if secret_file.exists():
            saved_secret = secret_file.read_text(encoding="utf-8").strip()
            if saved_secret:
                _SECRET_CACHE = _validated_secret(saved_secret, str(secret_file))
                return _SECRET_CACHE

        generated_secret = secrets.token_hex(32)
        atomic_write_text(secret_file, generated_secret)
        _SECRET_CACHE = generated_secret
        return _SECRET_CACHE


def _encode_token(
    owner_id: str,
    expires_hours: int,
    extra_claims: Mapping[str, Any] | None,
) -> tuple[str, dict[str, Any]]:
    """Validate claims and return both the encoded JWT and its exact payload."""

    normalized_owner = _normalize_owner_id(owner_id)
    if type(expires_hours) is not int or expires_hours < 0:
        raise ValueError("expires_hours must be a non-negative integer")
    if extra_claims is not None and not isinstance(extra_claims, Mapping):
        raise ValueError("extra_claims must be a mapping")
    claims = dict(extra_claims or {})
    if any(not isinstance(key, str) or not key for key in claims):
        raise ValueError("extra_claims keys must be non-empty strings")
    conflicting_claims = sorted(_RESERVED_TOKEN_CLAIMS.intersection(claims))
    if conflicting_claims:
        raise ValueError(f"extra_claims contains reserved fields: {', '.join(conflicting_claims)}")

    now = int(time.time())
    payload: dict[str, Any] = {
        "owner_id": normalized_owner,
        "sub": normalized_owner,
        "typ": _TOKEN_TYPE,
        "iss": _TOKEN_ISSUER,
        "exp": now + expires_hours * 3600,
        "iat": now,
    }
    payload.update(claims)
    encoded = jwt.encode(payload, _get_secret_key(), algorithm=_ALGORITHM)
    if isinstance(encoded, bytes):
        return encoded.decode("ascii"), payload
    if not isinstance(encoded, str):
        raise RuntimeError("JWT encoder returned an unsupported token type")
    return encoded, payload


def generate_token(
    owner_id: str,
    expires_hours: int = 24,
    extra_claims: Mapping[str, Any] | None = None,
) -> str:
    """为用户签发 JWT；扩展声明不得覆盖身份与有效期保留字段。"""
    encoded, _payload = _encode_token(owner_id, expires_hours, extra_claims)
    return encoded


def generate_widget_token(
    owner_id: str,
    expires_hours: int = PendoConfig.WEB_WIDGET_TOKEN_EXPIRE_HOURS,
    *,
    db: Database,
) -> str:
    """Register and sign a revocable, read-only Widget credential."""

    configure_auth_storage(Path(db.db_path).parent)
    token_id = secrets.token_urlsafe(24)
    encoded, payload = _encode_token(
        owner_id,
        expires_hours,
        {
            "kind": _WIDGET_KIND,
            "scope": _WIDGET_SCOPE,
            "jti": token_id,
        },
    )
    db.register_widget_token(
        token_id,
        str(payload["owner_id"]),
        issued_at=int(payload["iat"]),
        expires_at=int(payload["exp"]),
    )
    return encoded


def verify_token(token: str, *, db: Database | None = None) -> dict[str, Any]:
    """验证 JWT 的签名、签发者、期限和主体一致性。"""
    if not isinstance(token, str) or not token.strip():
        raise AuthError("Missing token")
    if db is not None:
        configure_auth_storage(Path(db.db_path).parent)
    try:
        decoded = jwt.decode(
            token.strip(),
            _get_secret_key(),
            algorithms=[_ALGORITHM],
            issuer=_TOKEN_ISSUER,
            options={"require": ["exp", "iat", "iss", "owner_id", "sub", "typ"]},
        )
        if not isinstance(decoded, dict):
            raise AuthError("Token payload must be an object")
        payload: dict[str, Any] = {str(key): value for key, value in decoded.items()}
        owner_id = payload.get("owner_id")
        if not isinstance(owner_id, str) or not owner_id.strip():
            raise AuthError("Token missing owner_id")
        if owner_id != owner_id.strip() or payload.get("sub") != owner_id:
            raise AuthError("Token subject does not match owner_id")
        if payload.get("typ") != _TOKEN_TYPE:
            raise AuthError("Token has invalid type")
        if payload.get("kind") == _WIDGET_KIND and payload.get("scope") != _WIDGET_SCOPE:
            raise AuthError("Widget token has invalid scope")
        if payload.get("kind") == _WIDGET_KIND:
            token_id = payload.get("jti")
            if not isinstance(token_id, str) or not token_id:
                raise AuthError("Widget token is missing jti")
            if db is None:
                raise AuthError("Widget token registry is unavailable")
            if not db.is_widget_token_active(token_id, owner_id, now=int(time.time())):
                raise AuthError("Widget token has been revoked")
        return payload
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("Token has expired") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthError("Invalid token") from exc
