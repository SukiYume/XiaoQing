"""Pendo Web 登录码、浏览器会话和 Widget Token 的 SQLite 仓储。"""

from __future__ import annotations

import re
import sqlite3
import time
from contextlib import AbstractContextManager

_SHA256_HEX = re.compile(r"[0-9a-f]{64}\Z")


class WebAuthRepositoryMixin:
    """把 Web 认证持久化职责从主数据库外观中分离出来。"""

    def get_connection(self) -> sqlite3.Connection:
        """由最终的 Database 提供线程本地连接。"""

        raise NotImplementedError

    def transaction(
        self,
        *,
        immediate: bool = False,
    ) -> AbstractContextManager[sqlite3.Connection]:
        """由最终的 Database 提供事务边界。"""

        raise NotImplementedError

    # ==================== Web 登录认证 ====================

    @staticmethod
    def _validate_auth_digest(value: str, label: str) -> str:
        digest = str(value or "").strip()
        if _SHA256_HEX.fullmatch(digest) is None:
            raise ValueError(f"{label} must be a lowercase SHA-256 digest")
        return digest

    @staticmethod
    def _validate_auth_owner(owner_id: str) -> str:
        owner = str(owner_id or "").strip()
        if not owner or len(owner) > 256:
            raise ValueError("web auth owner_id is invalid")
        return owner

    @staticmethod
    def _validate_auth_times(created_at: int, expires_at: int) -> tuple[int, int]:
        if type(created_at) is not int or type(expires_at) is not int:
            raise TypeError("web auth timestamps must be integers")
        if created_at < 0 or expires_at <= created_at:
            raise ValueError("web auth expiry must follow creation")
        return created_at, expires_at

    def register_login_code(
        self,
        code_digest: str,
        owner_id: str,
        *,
        issued_at: int,
        expires_at: int,
    ) -> None:
        """持久化仅可使用一次的浏览器登录码摘要。"""

        digest = self._validate_auth_digest(code_digest, "login code digest")
        owner = self._validate_auth_owner(owner_id)
        issued, expires = self._validate_auth_times(issued_at, expires_at)
        with self.transaction(immediate=True) as conn:
            conn.execute("DELETE FROM login_code_registry WHERE expires_at <= ?", (issued,))
            conn.execute(
                """
                INSERT INTO login_code_registry
                    (code_digest, owner_id, issued_at, expires_at)
                VALUES (?, ?, ?, ?)
                """,
                (digest, owner, issued, expires),
            )

    def consume_login_code(self, code_digest: str, *, now: int) -> str | None:
        """原子消费一枚未过期登录码并返回所属用户。"""

        digest = self._validate_auth_digest(code_digest, "login code digest")
        if type(now) is not int or now < 0:
            raise ValueError("login code verification time is invalid")
        with self.transaction(immediate=True) as conn:
            conn.execute("DELETE FROM login_code_registry WHERE expires_at <= ?", (now,))
            row = conn.execute(
                """
                SELECT owner_id
                FROM login_code_registry
                WHERE code_digest = ? AND expires_at > ?
                """,
                (digest, now),
            ).fetchone()
            if row is None:
                return None
            conn.execute("DELETE FROM login_code_registry WHERE code_digest = ?", (digest,))
            return str(row["owner_id"])

    def register_web_session(
        self,
        session_digest: str,
        device_id: str,
        owner_id: str,
        csrf_token: str,
        *,
        created_at: int,
        expires_at: int,
        demo: bool,
    ) -> None:
        """在返回 Cookie 前持久化浏览器会话摘要。"""

        digest = self._validate_auth_digest(session_digest, "web session digest")
        device = str(device_id or "").strip()
        csrf = str(csrf_token or "").strip()
        owner = self._validate_auth_owner(owner_id)
        created, expires = self._validate_auth_times(created_at, expires_at)
        if not device or len(device) > 256:
            raise ValueError("web session device_id is invalid")
        if not csrf or len(csrf) > 256:
            raise ValueError("web session CSRF token is invalid")
        if type(demo) is not bool:
            raise TypeError("web session demo flag must be a boolean")
        with self.transaction(immediate=True) as conn:
            conn.execute("DELETE FROM web_session_registry WHERE expires_at <= ?", (created,))
            conn.execute(
                """
                INSERT INTO web_session_registry
                    (session_digest, device_id, owner_id, csrf_token,
                     created_at, expires_at, demo)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (digest, device, owner, csrf, created, expires, int(demo)),
            )

    def get_web_session_record(
        self,
        session_digest: str,
        *,
        now: int,
    ) -> dict[str, object] | None:
        """返回一条未过期且不含 Bearer 原文的浏览器会话记录。"""

        digest = self._validate_auth_digest(session_digest, "web session digest")
        if type(now) is not int or now < 0:
            raise ValueError("web session verification time is invalid")
        row = (
            self.get_connection()
            .execute(
                """
            SELECT device_id, owner_id, csrf_token, created_at, expires_at, demo
            FROM web_session_registry
            WHERE session_digest = ? AND expires_at > ?
            """,
                (digest, now),
            )
            .fetchone()
        )
        return dict(row) if row is not None else None

    def list_web_session_records(self, owner_id: str, *, now: int) -> list[dict[str, object]]:
        """按最新优先列出用户仍有效的浏览器会话。"""

        owner = self._validate_auth_owner(owner_id)
        if type(now) is not int or now < 0:
            raise ValueError("web session listing time is invalid")
        with self.transaction(immediate=True) as conn:
            conn.execute("DELETE FROM web_session_registry WHERE expires_at <= ?", (now,))
            rows = conn.execute(
                """
                SELECT device_id, owner_id, created_at, expires_at, demo
                FROM web_session_registry
                WHERE owner_id = ? AND expires_at > ?
                ORDER BY created_at DESC, device_id DESC
                """,
                (owner, now),
            ).fetchall()
        return [dict(row) for row in rows]

    def revoke_web_session(self, session_digest: str) -> None:
        """按持久化摘要幂等撤销一个浏览器会话。"""

        digest = self._validate_auth_digest(session_digest, "web session digest")
        with self.transaction(immediate=True) as conn:
            conn.execute("DELETE FROM web_session_registry WHERE session_digest = ?", (digest,))

    def revoke_web_session_device(
        self,
        owner_id: str,
        device_id: str,
        *,
        now: int,
    ) -> bool:
        """撤销指定用户拥有的一条有效浏览器会话。"""

        owner = self._validate_auth_owner(owner_id)
        device = str(device_id or "").strip()
        if not device:
            return False
        if type(now) is not int or now < 0:
            raise ValueError("web session revocation time is invalid")
        with self.transaction(immediate=True) as conn:
            conn.execute("DELETE FROM web_session_registry WHERE expires_at <= ?", (now,))
            cursor = conn.execute(
                """
                DELETE FROM web_session_registry
                WHERE owner_id = ? AND device_id = ? AND expires_at > ?
                """,
                (owner, device, now),
            )
            return cursor.rowcount > 0

    # ==================== Web Widget 令牌认证 ====================

    @staticmethod
    def _validate_widget_token_record(
        jti: str,
        owner_id: str,
        issued_at: int,
        expires_at: int,
    ) -> tuple[str, str, int, int]:
        token_id = str(jti or "").strip()
        owner = str(owner_id or "").strip()
        if not token_id or len(token_id) > 256:
            raise ValueError("widget token jti is invalid")
        if not owner or len(owner) > 256:
            raise ValueError("widget token owner_id is invalid")
        if type(issued_at) is not int or type(expires_at) is not int:
            raise TypeError("widget token timestamps must be integers")
        if expires_at <= issued_at:
            raise ValueError("widget token expiry must follow issuance")
        return token_id, owner, issued_at, expires_at

    def register_widget_token(
        self,
        jti: str,
        owner_id: str,
        *,
        issued_at: int,
        expires_at: int,
    ) -> None:
        """在令牌交付前持久化新签发的 JTI。"""

        token_id, owner, issued, expires = self._validate_widget_token_record(
            jti,
            owner_id,
            issued_at,
            expires_at,
        )
        with self.transaction(immediate=True) as conn:
            conn.execute("DELETE FROM widget_token_registry WHERE expires_at <= ?", (issued,))
            conn.execute(
                """
                INSERT INTO widget_token_registry
                    (jti, owner_id, issued_at, expires_at, revoked_at)
                VALUES (?, ?, ?, ?, NULL)
                """,
                (token_id, owner, issued, expires),
            )

    def revoke_widget_tokens(self, owner_id: str, *, revoked_at: int | None = None) -> int:
        """撤销指定用户全部未过期的 Widget 凭据。"""

        owner = str(owner_id or "").strip()
        if not owner or len(owner) > 256:
            raise ValueError("widget token owner_id is invalid")
        timestamp = int(time.time()) if revoked_at is None else revoked_at
        if type(timestamp) is not int or timestamp < 0:
            raise ValueError("widget token revocation time is invalid")
        with self.transaction(immediate=True) as conn:
            cursor = conn.execute(
                """
                UPDATE widget_token_registry
                SET revoked_at = ?
                WHERE owner_id = ? AND revoked_at IS NULL AND expires_at > ?
                """,
                (timestamp, owner, timestamp),
            )
            return max(0, cursor.rowcount)

    def is_widget_token_active(
        self,
        jti: str,
        owner_id: str,
        *,
        now: int | None = None,
    ) -> bool:
        """仅当 JTI 已登记、未过期且未撤销时返回真。"""

        token_id = str(jti or "").strip()
        owner = str(owner_id or "").strip()
        if not token_id or not owner:
            return False
        timestamp = int(time.time()) if now is None else now
        if type(timestamp) is not int or timestamp < 0:
            raise ValueError("widget token verification time is invalid")
        row = (
            self.get_connection()
            .execute(
                """
            SELECT 1
            FROM widget_token_registry
            WHERE jti = ? AND owner_id = ? AND revoked_at IS NULL AND expires_at > ?
            """,
                (token_id, owner, timestamp),
            )
            .fetchone()
        )
        return row is not None
