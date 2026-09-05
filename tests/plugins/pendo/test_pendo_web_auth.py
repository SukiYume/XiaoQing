"""Pendo Web 登录码、浏览器会话和 JWT 安全契约回归。"""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import ANY, Mock

import pytest
from fastapi import HTTPException, Request, Response

from plugins.pendo.config import PendoConfig
from tests.helpers.pendo_test_support import reset_pendo_runtime_config

try:
    from plugins.pendo.web import auth as auth_module
    from plugins.pendo.web.api import auth_routes as auth_routes_module
    from plugins.pendo.web.auth import (
        AuthError,
        WebSession,
        consume_login_code,
        create_web_session,
        generate_widget_token,
        get_web_session,
        issue_login_code,
        list_web_sessions,
        revoke_web_session,
        revoke_web_session_device,
        verify_token,
    )
    from plugins.pendo.web.services.demo_space import DemoCapacityError
except ModuleNotFoundError as exc:
    if exc.name != "jwt":
        raise
    pytest.skip("pendo web auth requires PyJWT", allow_module_level=True)


@pytest.fixture(autouse=True)
def _reset_persistent_auth_state(
    monkeypatch: pytest.MonkeyPatch,
    temp_data_dir: Path,
) -> Iterator[None]:
    """让每个用例使用独立的 Pendo 认证表和签名密钥。"""

    from plugins.pendo.services.db import Database

    auth_db = Database(str(temp_data_dir / "pendo.db"))
    monkeypatch.delenv("PENDO_WEB_TOKEN_SECRET", raising=False)
    auth_module.configure_auth_database(auth_db)
    reset_pendo_runtime_config()
    try:
        yield
    finally:
        reset_pendo_runtime_config()
        with auth_module._AUTH_LOCK:
            auth_module._AUTH_DATABASE = None
        auth_db.cleanup()


@pytest.fixture
def isolated_secret_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """让每个密钥持久化用例使用独立文件和空缓存。"""
    secret_file = tmp_path / "web_token_secret.txt"
    monkeypatch.delenv("PENDO_WEB_TOKEN_SECRET", raising=False)
    monkeypatch.setattr(auth_module, "_SECRET_FILE", secret_file)
    monkeypatch.setattr(auth_module, "_SECRET_CACHE", None)
    return secret_file


def test_generated_widget_token_contains_read_only_claims(
    isolated_secret_file: Path,
    temp_db,
) -> None:
    """Widget Token 应绑定规范化主体、固定只读权限和整数期限。"""

    issued_at = int(time.time())
    token = generate_widget_token(" widget-user ", db=temp_db)
    payload = verify_token(token, db=temp_db)

    assert isinstance(token, str) and token
    assert payload["owner_id"] == "widget-user"
    assert payload["sub"] == "widget-user"
    assert payload["iss"] == "pendo-web"
    assert payload["typ"] == "pendo-web"
    assert payload["kind"] == "widget"
    assert payload["scope"] == "widget:read"
    assert isinstance(payload["jti"], str) and payload["jti"]
    assert isinstance(payload["exp"], int)
    lifetime = payload["exp"] - issued_at
    assert 364 * 24 * 60 * 60 <= lifetime <= 366 * 24 * 60 * 60
    assert isolated_secret_file.exists()


def test_authentication_lifetimes_use_seconds_with_distinct_durations() -> None:
    """登录 Code/会话同为 7 天，Widget Token 为 365 天，均使用秒。"""

    day = 24 * 60 * 60
    assert PendoConfig.WEB_LOGIN_CODE_EXPIRE_SECONDS == 7 * day
    assert PendoConfig.WEB_SESSION_EXPIRE_SECONDS == 7 * day
    assert PendoConfig.WEB_WIDGET_TOKEN_EXPIRE_SECONDS == 365 * day


def test_widget_token_revocation_is_persistent(tmp_path: Path) -> None:
    from plugins.pendo.services.db import Database

    db_path = tmp_path / "pendo.db"
    first   = Database(str(db_path))
    token = generate_widget_token("widget-owner", db=first)
    assert verify_token(token, db=first)["owner_id"] == "widget-owner"
    assert first.revoke_widget_tokens("widget-owner") == 1
    with pytest.raises(AuthError, match="revoked"):
        verify_token(token, db=first)
    first.cleanup()

    second = Database(str(db_path))
    try:
        with pytest.raises(AuthError, match="revoked"):
            verify_token(token, db=second)
    finally:
        second.cleanup()


def test_token_remains_valid_after_secret_cache_reset(
    monkeypatch: pytest.MonkeyPatch,
    isolated_secret_file: Path,
    temp_db,
) -> None:
    """清空进程缓存后应复用持久密钥，旧 Token 仍可验证。"""

    token = generate_widget_token("persist-user", db=temp_db)
    first_secret = isolated_secret_file.read_text(encoding="utf-8").strip()

    monkeypatch.setattr(auth_module, "_SECRET_CACHE", None)
    second_secret = auth_module._get_secret_key()

    assert second_secret == first_secret
    assert verify_token(token, db=temp_db)["owner_id"] == "persist-user"


def test_secret_initialization_is_thread_safe(
    monkeypatch: pytest.MonkeyPatch,
    isolated_secret_file: Path,
) -> None:
    """并发首次读取只能生成并落盘一次签名密钥。"""

    generation_count = 0
    count_lock       = threading.Lock()

    def slow_token_hex(_size: int) -> str:
        """放大初始化竞争窗口，并记录实际密钥生成次数。"""

        nonlocal generation_count
        with count_lock:
            generation_count += 1
        time.sleep(0.02)
        return "ab" * 32

    monkeypatch.setattr(secrets, "token_hex", slow_token_hex)
    with ThreadPoolExecutor(max_workers=8) as pool:
        secrets_seen = list(pool.map(lambda _index: auth_module._get_secret_key(), range(16)))

    assert set(secrets_seen) == {"ab" * 32}
    assert generation_count == 1
    assert isolated_secret_file.read_text(encoding="utf-8") == "ab" * 32


@pytest.mark.parametrize("source", ["environment", "file"])
def test_short_signing_secret_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    source: str,
) -> None:
    """环境和文件来源的短密钥都必须在使用前拒绝。"""

    secret_file = tmp_path / "web_token_secret.txt"
    monkeypatch.setattr(auth_module, "_SECRET_FILE", secret_file)
    monkeypatch.setattr(auth_module, "_SECRET_CACHE", None)
    monkeypatch.delenv("PENDO_WEB_TOKEN_SECRET", raising=False)
    if source == "environment":
        monkeypatch.setenv("PENDO_WEB_TOKEN_SECRET", "too-short")
    else:
        secret_file.write_text("too-short", encoding="utf-8")

    with pytest.raises(RuntimeError, match="at least 32 bytes"):
        auth_module._get_secret_key()


def test_environment_secret_takes_precedence_without_writing_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """环境密钥优先时不得额外创建持久密钥文件。"""

    secret_file = tmp_path / "web_token_secret.txt"
    env_secret  = "environment-secret-0123456789abcdef"
    monkeypatch.setattr(auth_module, "_SECRET_FILE", secret_file)
    monkeypatch.setattr(auth_module, "_SECRET_CACHE", None)
    monkeypatch.setenv("PENDO_WEB_TOKEN_SECRET", env_secret)

    assert auth_module._get_secret_key() == env_secret
    assert not secret_file.exists()


def _sign_invalid_widget_token(
    owner_id: str,
    *,
    db,
    expires_at: int,
    scope: str | None    = "widget:read",
    token_id: str | None = "test-widget-token",
) -> str:
    """只为验证拒绝路径签发不进入生产注册表的畸形 Widget JWT。"""

    # 验证端会按数据库目录重新绑定密钥；签发测试样本前使用同一真实绑定路径。
    auth_module.configure_auth_database(db)
    issued_at = int(time.time())
    payload   = {
        "owner_id": owner_id,
        "sub": owner_id,
        "typ": "pendo-web",
        "iss": "pendo-web",
        "exp": expires_at,
        "iat": issued_at,
        "kind": "widget",
    }
    if scope is not None:
        payload["scope"] = scope
    if token_id is not None:
        payload["jti"] = token_id
    return auth_module.jwt.encode(payload, auth_module._get_secret_key(), algorithm="HS256")


def test_widget_token_without_read_scope_is_rejected(temp_db) -> None:
    """仅伪装 Widget 种类而缺少只读 scope 的 Token 必须失败。"""

    token = _sign_invalid_widget_token(
        "widget-user",
        db         = temp_db,
        expires_at = int(time.time()) + 60,
        scope      = None,
    )

    with pytest.raises(AuthError, match="invalid scope"):
        verify_token(token, db=temp_db)


def test_legacy_widget_token_without_jti_is_rejected(temp_db) -> None:
    auth_module.configure_auth_storage(Path(temp_db.db_path).parent)
    token = _sign_invalid_widget_token(
        "widget-user",
        db         = temp_db,
        expires_at = int(time.time()) + 60,
        token_id   = None,
    )

    with pytest.raises(AuthError, match="missing jti"):
        verify_token(token, db=temp_db)


def test_expired_invalid_and_tampered_tokens_are_rejected(temp_db) -> None:
    """过期、畸形和签名被篡改的 Token 应映射为受控认证错误。"""

    expired = _sign_invalid_widget_token(
        "expired-user",
        db         = temp_db,
        expires_at = int(time.time()) - 1,
    )
    with pytest.raises(AuthError, match="expired"):
        verify_token(expired, db=temp_db)
    with pytest.raises(AuthError, match="Invalid token"):
        verify_token("invalid.token.string", db=temp_db)

    token = generate_widget_token("tampered-user", db=temp_db)
    with pytest.raises(AuthError, match="Invalid token"):
        verify_token(f"{token[:-5]}XXXXX", db=temp_db)


@pytest.mark.parametrize(
    "owner_id",
    ["", "   ", "owner\nspoof", "x" * 257],
)
def test_authentication_subject_rejects_invalid_owner_ids(owner_id: str) -> None:
    """所有认证入口应统一拒绝空白、控制字符和超长主体。"""

    with pytest.raises(ValueError):
        issue_login_code(owner_id)
    with pytest.raises(ValueError):
        create_web_session(owner_id)
    with pytest.raises(ValueError):
        generate_widget_token(owner_id, db=auth_module._resolve_auth_database())


@pytest.mark.parametrize("expires_seconds", [0, -1, True, 1.5])
def test_login_and_session_lifetimes_require_positive_integers(expires_seconds: object) -> None:
    """登录码与会话寿命都只接受正整数，布尔和浮点不得冒充。"""

    with pytest.raises(ValueError, match="positive integer"):
        issue_login_code("owner-a", expires_seconds=expires_seconds)
    with pytest.raises(ValueError, match="positive integer"):
        create_web_session("owner-a", expires_seconds=expires_seconds)


def test_demo_flag_requires_boolean() -> None:
    """会话演示标记只接受真实布尔值。"""

    with pytest.raises(ValueError, match="boolean"):
        create_web_session("owner-a", demo="false")


def test_login_code_is_owner_bound_single_use_and_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """登录码应绑定规范化主体、只可消费一次并按确定时钟过期。"""

    now = 1_000.0
    monkeypatch.setattr(time, "time", lambda: now)
    code = issue_login_code(" owner-a ", expires_seconds=60)

    assert consume_login_code(code) == "owner-a"
    with pytest.raises(AuthError, match="already used"):
        consume_login_code(code)

    expiring_code = issue_login_code("owner-a", expires_seconds=1)
    now = 1_001.0
    with pytest.raises(AuthError, match="expired"):
        consume_login_code(expiring_code)


def test_login_code_and_browser_session_survive_auth_storage_reopen(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """未消费 Code 与浏览器会话应跨 Bot/Pendo Web 重启继续有效。"""

    from plugins.pendo.services.db import Database

    now = 1_500
    monkeypatch.setattr(time, "time", lambda: float(now))
    db_path = tmp_path / "persistent-pendo.db"
    first   = Database(str(db_path))
    try:
        code = issue_login_code("persistent-owner", db=first)
        session = create_web_session("persistent-owner", db=first)
        code_row = (
            first.get_connection().execute("SELECT code_digest FROM login_code_registry").fetchone()
        )
        session_row = (
            first.get_connection()
            .execute("SELECT session_digest FROM web_session_registry")
            .fetchone()
        )
    finally:
        first.cleanup()
    assert code_row is not None and code not in str(code_row[0])
    assert session_row is not None and session.session_id not in str(session_row[0])

    with auth_module._AUTH_LOCK:
        auth_module._AUTH_DATABASE = None
    second = Database(str(db_path))
    try:
        auth_module.configure_auth_database(second)
        restored = get_web_session(session.session_id, db=second)
        assert restored.owner_id == "persistent-owner"
        assert restored.device_id == session.device_id
        assert restored.expires_at - restored.created_at == 7 * 24 * 60 * 60
        assert consume_login_code(code, db=second) == "persistent-owner"
    finally:
        second.cleanup()
    assert not (tmp_path / "web_auth.db").exists()


def test_web_session_is_revocable_and_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    """浏览器会话应可幂等撤销，过期后也不能按设备再次命中。"""

    now = 2_000.0
    monkeypatch.setattr(time, "time", lambda: now)
    session = create_web_session(" owner-b ", expires_seconds=60)

    assert get_web_session(session.session_id).owner_id == "owner-b"
    revoke_web_session(session.session_id)
    with pytest.raises(AuthError, match="invalid or expired"):
        get_web_session(session.session_id)

    expiring_session = create_web_session("owner-b", expires_seconds=1)
    now = 2_001.0
    assert revoke_web_session_device("owner-b", expiring_session.device_id) is False
    with pytest.raises(AuthError, match="invalid or expired"):
        get_web_session(expiring_session.session_id)


def test_owner_can_list_and_revoke_session_by_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """设备列表应最新优先，且只能撤销同一所有者的会话。"""

    now = 3_000.0
    monkeypatch.setattr(time, "time", lambda: now)
    first = create_web_session("owner-c", expires_seconds=60)
    now = 3_001.0
    second = create_web_session("owner-c", expires_seconds=60)
    now = 3_002.0

    assert [session.device_id for session in list_web_sessions(" owner-c ")] == [
        second.device_id,
        first.device_id,
    ]
    assert revoke_web_session_device("owner-other", first.device_id) is False
    assert revoke_web_session_device("owner-c", first.device_id) is True
    assert [session.device_id for session in list_web_sessions("owner-c")] == [second.device_id]


# 路由层只验证认证状态到 HTTP 契约的映射，底层令牌算法由上面的用例覆盖。
def _demo_request() -> Request:
    """构造只含客户端地址的最小演示创建请求。"""

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/auth/demo",
            "headers": [],
            "client": ("127.0.0.1", 12345),
        }
    )


def test_login_exchange_revokes_session_when_cookie_write_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """登录码已消费但 Cookie 写入失败时，不得遗留不可达的服务端会话。"""

    code    = issue_login_code("cookie-owner")
    now     = time.time()
    session = WebSession(
        session_id = "login-cookie-failure",
        device_id  = "login-cookie-device",
        owner_id   = "cookie-owner",
        csrf_token = "csrf",
        created_at = now,
        expires_at = now + 60,
    )
    revoke_session = Mock()
    monkeypatch.setattr(
        auth_routes_module,
        "create_web_session",
        Mock(return_value=session),
    )
    monkeypatch.setattr(
        auth_routes_module,
        "_set_session_cookie",
        Mock(side_effect=RuntimeError("cookie failed")),
    )
    monkeypatch.setattr(auth_routes_module, "revoke_web_session", revoke_session)

    with pytest.raises(RuntimeError, match="cookie failed"):
        auth_routes_module.exchange_login_code(
            auth_routes_module.LoginExchangeRequest(code=code),
            Response(),
        )

    revoke_session.assert_called_once_with(session.session_id)
    with pytest.raises(AuthError, match="invalid, expired, or already used"):
        consume_login_code(code)


def test_demo_route_maps_capacity_failure_to_429(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """演示容量错误应转换为稳定的 429，而不是泄漏成服务端异常。"""

    PendoConfig.configure({"web_demo_enabled": True})
    monkeypatch.setattr(
        auth_routes_module,
        "create_demo_session",
        Mock(side_effect=DemoCapacityError("demo capacity reached")),
    )

    with pytest.raises(HTTPException) as exc_info:
        auth_routes_module.create_demo_auth(_demo_request(), Response(), Mock())

    assert exc_info.value.status_code == 429
    assert exc_info.value.detail == "demo capacity reached"


def test_revoke_unknown_session_device_returns_404_without_revoking_current() -> None:
    """未知设备标识应返回 404，并保留发起请求的当前会话。"""

    session = create_web_session("route-owner")

    with pytest.raises(HTTPException) as exc_info:
        auth_routes_module.revoke_auth_session(
            "unknown-device",
            session,
            session.owner_id,
        )

    assert exc_info.value.status_code == 404
    assert get_web_session(session.session_id) == session


def test_demo_route_caps_browser_session_at_demo_lifetime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """演示 Cookie 会话不得比数据空间本身活得更久。"""

    now     = time.time()
    session = WebSession(
        session_id = "demo-session",
        device_id  = "demo-device",
        owner_id   = "demo_web_route",
        csrf_token = "csrf",
        created_at = now,
        expires_at = now + 6 * 60 * 60,
        demo       = True,
    )
    create_session = Mock(return_value=session)
    PendoConfig.configure({"web_demo_enabled": True})
    monkeypatch.setattr(PendoConfig, "WEB_SESSION_EXPIRE_SECONDS", 7 * 24 * 60 * 60)
    monkeypatch.setattr(PendoConfig, "WEB_DEMO_EXPIRE_HOURS", 6)
    monkeypatch.setattr(
        auth_routes_module,
        "create_demo_session",
        Mock(return_value={"owner_id": session.owner_id}),
    )
    monkeypatch.setattr(auth_routes_module, "create_web_session", create_session)

    payload = auth_routes_module.create_demo_auth(_demo_request(), Response(), Mock())

    assert payload["ok"] is True
    create_session.assert_called_once_with(
        session.owner_id,
        expires_seconds = 6 * 60 * 60,
        demo            = True,
    )


def test_demo_route_purges_owner_when_browser_session_creation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """数据空间已创建但浏览器会话失败时，必须回收临时所有者。"""

    owner_id    = "demo_web_session_failure"
    purge_owner = Mock()
    PendoConfig.configure({"web_demo_enabled": True})
    monkeypatch.setattr(
        auth_routes_module,
        "create_demo_session",
        Mock(return_value={"owner_id": owner_id}),
    )
    monkeypatch.setattr(
        auth_routes_module,
        "create_web_session",
        Mock(side_effect=RuntimeError("session failed")),
    )
    monkeypatch.setattr(auth_routes_module, "purge_demo_owner", purge_owner)

    with pytest.raises(RuntimeError, match="session failed"):
        auth_routes_module.create_demo_auth(_demo_request(), Response(), Mock())

    purge_owner.assert_called_once_with(ANY, owner_id)


def test_demo_route_revokes_session_and_purges_owner_when_cookie_write_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cookie 写入失败时应同时撤销持久会话并清理演示数据。"""

    now     = time.time()
    session = WebSession(
        session_id = "failed-cookie-session",
        device_id  = "failed-cookie-device",
        owner_id   = "demo_web_cookie_failure",
        csrf_token = "csrf",
        created_at = now,
        expires_at = now + 60,
        demo       = True,
    )
    revoke_session = Mock()
    purge_owner    = Mock()
    PendoConfig.configure({"web_demo_enabled": True})
    monkeypatch.setattr(
        auth_routes_module,
        "create_demo_session",
        Mock(return_value={"owner_id": session.owner_id}),
    )
    monkeypatch.setattr(
        auth_routes_module,
        "create_web_session",
        Mock(return_value=session),
    )
    monkeypatch.setattr(
        auth_routes_module,
        "_set_session_cookie",
        Mock(side_effect=RuntimeError("cookie failed")),
    )
    monkeypatch.setattr(auth_routes_module, "revoke_web_session", revoke_session)
    monkeypatch.setattr(auth_routes_module, "purge_demo_owner", purge_owner)

    with pytest.raises(RuntimeError, match="cookie failed"):
        auth_routes_module.create_demo_auth(_demo_request(), Response(), Mock())

    revoke_session.assert_called_once_with(session.session_id)
    purge_owner.assert_called_once_with(ANY, session.owner_id)
