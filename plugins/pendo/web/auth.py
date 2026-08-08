"""Pendo Web 一次性登录、浏览器会话与 JWT 签名验证。"""

from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, cast

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
_LOGIN_CODE_DIGEST_DOMAIN: Final = b"pendo-login-code\0"
_WEB_SESSION_DIGEST_DOMAIN: Final = b"pendo-web-session\0"
_SECRET_FILE: Path | None = None
_SECRET_CACHE: str | None = None
_AUTH_DATABASE: Database | None = None
_AUTH_LOCK = threading.Lock()
_SECRET_LOCK = threading.Lock()


def configure_auth_storage(data_dir: Path) -> None:
    """把签名密钥绑定到 Core 分配的插件可写数据目录。"""

    global _SECRET_CACHE, _SECRET_FILE
    directory = Path(data_dir)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    secret_file = directory / "web_token_secret.txt"
    with _SECRET_LOCK:
        if _SECRET_FILE != secret_file:
            _SECRET_FILE = secret_file
            _SECRET_CACHE = None


def configure_auth_database(db: Database) -> None:
    """把登录码和浏览器会话绑定到 Pendo 已有数据库。"""

    global _AUTH_DATABASE
    configure_auth_storage(Path(db.db_path).parent)
    with _AUTH_LOCK:
        _AUTH_DATABASE = db


def _resolve_auth_database(db: Database | None = None) -> Database:
    """取得显式传入或应用启动时绑定的认证仓储。"""

    if db is not None:
        configure_auth_database(db)
        return db
    with _AUTH_LOCK:
        configured = _AUTH_DATABASE
    if configured is None:
        raise RuntimeError("Pendo auth database is not configured")
    return configured


class AuthError(Exception):
    """可安全返回给认证调用方的失败。"""

    def __init__(self, message: str) -> None:
        """保存可展示消息，同时初始化标准异常文本。"""
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class WebSession:
    """由服务端持久化、可按设备撤销的浏览器会话。"""

    session_id: str
    device_id: str
    owner_id: str
    csrf_token: str
    created_at: int
    expires_at: int
    demo: bool = False


@dataclass(frozen=True)
class WebSessionInfo:
    """不含浏览器 Bearer 凭据的会话设备记录。"""

    device_id: str
    owner_id: str
    created_at: int
    expires_at: int
    demo: bool = False


def _credential_digest(domain: bytes, credential: str) -> str:
    """持久化高熵凭据前按用途分域计算摘要，不保存原始值。"""

    return hashlib.sha256(domain + credential.encode("utf-8")).hexdigest()


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
    """校验需要保持一段时间的凭据使用正整数秒。"""
    if type(value) is not int or value < 1:
        raise ValueError("expires_seconds must be a positive integer")
    return value


def _expiry_timestamp(issued_at: int, expires_seconds: int) -> int:
    """所有 Pendo Web 凭据统一按正整数秒计算到期时间。"""

    return issued_at + _positive_lifetime_seconds(expires_seconds)


def _validated_secret(secret: str, source: str) -> str:
    """确保 HMAC 密钥达到 HS256 所需的最小强度。"""
    if len(secret.encode("utf-8")) < _MIN_SECRET_BYTES:
        raise RuntimeError(f"{source} must contain at least {_MIN_SECRET_BYTES} bytes")
    return secret


def issue_login_code(
    owner_id: str,
    expires_seconds: int = PendoConfig.WEB_LOGIN_CODE_EXPIRE_SECONDS,
    *,
    db: Database | None = None,
) -> str:
    """签发可跨进程重启、绑定用户且只能消费一次的登录交换码。"""

    registry = _resolve_auth_database(db)
    normalized_owner = _normalize_owner_id(owner_id)
    now = int(time.time())
    expires_at = _expiry_timestamp(now, expires_seconds)
    for _attempt in range(3):
        code = secrets.token_urlsafe(32)
        code_digest = _credential_digest(_LOGIN_CODE_DIGEST_DOMAIN, code)
        try:
            registry.register_login_code(
                code_digest,
                normalized_owner,
                issued_at=now,
                expires_at=expires_at,
            )
            return code
        except sqlite3.IntegrityError:
            continue
    raise RuntimeError("Unable to allocate a unique Pendo login code")


def consume_login_code(code: str, *, db: Database | None = None) -> str:
    """原子消费登录码并返回其绑定用户。"""

    if not isinstance(code, str) or not code.strip():
        raise AuthError("Missing login code")
    code_digest = _credential_digest(_LOGIN_CODE_DIGEST_DOMAIN, code.strip())
    now = int(time.time())
    owner_id = _resolve_auth_database(db).consume_login_code(code_digest, now=now)
    if owner_id is None:
        raise AuthError("Login code is invalid, expired, or already used")
    return str(owner_id)


def create_web_session(
    owner_id: str,
    *,
    expires_seconds: int = PendoConfig.WEB_SESSION_EXPIRE_SECONDS,
    demo: bool = False,
    db: Database | None = None,
) -> WebSession:
    """创建含 CSRF 令牌、可跨重启并可按设备撤销的浏览器会话。"""

    normalized_owner = _normalize_owner_id(owner_id)
    registry = _resolve_auth_database(db)
    if type(demo) is not bool:
        raise ValueError("demo must be a boolean")
    now = int(time.time())
    expires_at = _expiry_timestamp(now, expires_seconds)
    for _attempt in range(3):
        session = WebSession(
            session_id=secrets.token_urlsafe(32),
            device_id=secrets.token_urlsafe(12),
            owner_id=normalized_owner,
            csrf_token=secrets.token_urlsafe(32),
            created_at=now,
            expires_at=expires_at,
            demo=demo,
        )
        session_digest = _credential_digest(_WEB_SESSION_DIGEST_DOMAIN, session.session_id)
        try:
            registry.register_web_session(
                session_digest,
                session.device_id,
                session.owner_id,
                session.csrf_token,
                created_at=session.created_at,
                expires_at=session.expires_at,
                demo=session.demo,
            )
            return session
        except sqlite3.IntegrityError:
            continue
    raise RuntimeError("Unable to allocate a unique Pendo web session")


def _session_from_row(session_id: str, row: Mapping[str, object]) -> WebSession:
    """从数据库行重建已认证会话，同时不向上层暴露持久化摘要。"""

    return WebSession(
        session_id=session_id,
        device_id=str(row["device_id"]),
        owner_id=str(row["owner_id"]),
        csrf_token=str(row["csrf_token"]),
        created_at=cast(int, row["created_at"]),
        expires_at=cast(int, row["expires_at"]),
        demo=bool(row["demo"]),
    )


def _session_info_from_row(row: Mapping[str, object]) -> WebSessionInfo:
    """把会话行转换为不含 Bearer 凭据的设备列表项。"""

    return WebSessionInfo(
        device_id=str(row["device_id"]),
        owner_id=str(row["owner_id"]),
        created_at=cast(int, row["created_at"]),
        expires_at=cast(int, row["expires_at"]),
        demo=bool(row["demo"]),
    )


def get_web_session(session_id: str | None, *, db: Database | None = None) -> WebSession:
    """读取未过期会话；缺失、撤销或过期均按认证失败处理。"""

    if not isinstance(session_id, str) or not session_id.strip():
        raise AuthError("Missing web session")
    normalized_session_id = session_id.strip()
    session_digest = _credential_digest(_WEB_SESSION_DIGEST_DOMAIN, normalized_session_id)
    now = int(time.time())
    row = _resolve_auth_database(db).get_web_session_record(session_digest, now=now)
    if row is None:
        raise AuthError("Web session is invalid or expired")
    return _session_from_row(normalized_session_id, row)


def revoke_web_session(session_id: str | None, *, db: Database | None = None) -> None:
    """幂等撤销指定浏览器会话。"""

    if not isinstance(session_id, str) or not session_id.strip():
        return
    session_digest = _credential_digest(_WEB_SESSION_DIGEST_DOMAIN, session_id.strip())
    _resolve_auth_database(db).revoke_web_session(session_digest)


def list_web_sessions(owner_id: str, *, db: Database | None = None) -> list[WebSessionInfo]:
    """按最新优先列出用户的内部会话记录，供设备视图脱敏展示。"""

    normalized_owner = _normalize_owner_id(owner_id)
    now = int(time.time())
    rows = _resolve_auth_database(db).list_web_session_records(normalized_owner, now=now)
    return [_session_info_from_row(row) for row in rows]


def revoke_web_session_device(
    owner_id: str,
    device_id: str,
    *,
    db: Database | None = None,
) -> bool:
    """按非秘密设备标识撤销用户自己的一个会话。"""

    normalized_owner = _normalize_owner_id(owner_id)
    normalized_device = str(device_id or "").strip()
    if not normalized_device:
        return False
    now = int(time.time())
    return bool(
        _resolve_auth_database(db).revoke_web_session_device(
            normalized_owner,
            normalized_device,
            now=now,
        )
    )


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


def _encode_widget_token(
    owner_id: str,
    expires_seconds: int,
    token_id: str,
) -> tuple[str, dict[str, Any]]:
    """签发固定声明的只读 Widget JWT，并返回实际载荷供仓储登记。"""

    normalized_owner = _normalize_owner_id(owner_id)
    now = int(time.time())
    expires_at = _expiry_timestamp(now, expires_seconds)
    payload: dict[str, Any] = {
        "owner_id": normalized_owner,
        "sub": normalized_owner,
        "typ": _TOKEN_TYPE,
        "iss": _TOKEN_ISSUER,
        "exp": expires_at,
        "iat": now,
        "kind": _WIDGET_KIND,
        "scope": _WIDGET_SCOPE,
        "jti": token_id,
    }
    encoded = jwt.encode(payload, _get_secret_key(), algorithm=_ALGORITHM)
    if isinstance(encoded, bytes):
        return encoded.decode("ascii"), payload
    if not isinstance(encoded, str):
        raise RuntimeError("JWT encoder returned an unsupported token type")
    return encoded, payload


def generate_widget_token(
    owner_id: str,
    expires_seconds: int = PendoConfig.WEB_WIDGET_TOKEN_EXPIRE_SECONDS,
    *,
    db: Database,
) -> str:
    """Register and sign a revocable, read-only Widget credential."""

    configure_auth_database(db)
    token_id = secrets.token_urlsafe(24)
    encoded, payload = _encode_widget_token(
        owner_id,
        expires_seconds,
        token_id,
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
        if payload.get("kind") != _WIDGET_KIND:
            raise AuthError("Token has invalid kind")
        if payload.get("scope") != _WIDGET_SCOPE:
            raise AuthError("Widget token has invalid scope")
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
