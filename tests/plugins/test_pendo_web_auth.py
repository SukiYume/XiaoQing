"""Tests for pendo web auth module."""
import shutil
import time
import uuid
from pathlib import Path

import pytest

try:
    from plugins.pendo.web import auth as auth_module
    from plugins.pendo.web.auth import (
        AuthError,
        consume_login_code,
        create_web_session,
        generate_token,
        generate_widget_token,
        get_web_session,
        issue_login_code,
        list_web_sessions,
        revoke_web_session,
        revoke_web_session_device,
        verify_token,
    )
except ModuleNotFoundError:
    pytest.skip("pendo web auth requires PyJWT", allow_module_level=True)


ROOT = Path(__file__).resolve().parents[2]


class TestTokenGeneration:
    def test_generate_token_returns_string(self):
        token = generate_token("user123")
        assert isinstance(token, str)
        assert len(token) > 0

    def test_generate_token_contains_owner_id(self):
        token = generate_token("user123")
        payload = verify_token(token)
        assert payload["owner_id"] == "user123"

    def test_verify_valid_token(self):
        token = generate_token("user456")
        payload = verify_token(token)
        assert payload["owner_id"] == "user456"
        assert "exp" in payload

    def test_verify_expired_token_raises(self):
        token = generate_token("user123", expires_hours=0)
        # Token with 0 hours = already expired
        time.sleep(0.1)
        with pytest.raises(AuthError, match="expired"):
            verify_token(token)

    def test_verify_invalid_token_raises(self):
        with pytest.raises(AuthError):
            verify_token("invalid.token.string")

    def test_verify_tampered_token_raises(self):
        token = generate_token("user123")
        tampered = token[:-5] + "XXXXX"
        with pytest.raises(AuthError):
            verify_token(tampered)

    def test_token_remains_valid_after_secret_cache_reset(self, monkeypatch):
        temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_web_auth_{uuid.uuid4().hex}"
        secret_file = temp_dir / "web_token_secret.txt"
        temp_dir.mkdir(parents=True, exist_ok=True)
        try:
            monkeypatch.setattr(auth_module, "_SECRET_FILE", secret_file)
            monkeypatch.setattr(auth_module, "_SECRET_CACHE", None)

            token = generate_token("persist-user")

            monkeypatch.setattr(auth_module, "_SECRET_CACHE", None)
            payload = verify_token(token)

            assert payload["owner_id"] == "persist-user"
            assert secret_file.exists()
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_secret_file_is_reused(self, monkeypatch):
        temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_web_auth_{uuid.uuid4().hex}"
        secret_file = temp_dir / "web_token_secret.txt"
        temp_dir.mkdir(parents=True, exist_ok=True)
        try:
            monkeypatch.setattr(auth_module, "_SECRET_FILE", secret_file)
            monkeypatch.setattr(auth_module, "_SECRET_CACHE", None)

            first = auth_module._get_secret_key()
            monkeypatch.setattr(auth_module, "_SECRET_CACHE", None)
            second = auth_module._get_secret_key()

            assert first == second
            assert secret_file.read_text(encoding="utf-8").strip() == first
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_generated_token_contains_expected_web_claims(self, monkeypatch):
        temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_web_auth_{uuid.uuid4().hex}"
        secret_file = temp_dir / "web_token_secret.txt"
        temp_dir.mkdir(parents=True, exist_ok=True)
        try:
            monkeypatch.setattr(auth_module, "_SECRET_FILE", secret_file)
            monkeypatch.setattr(auth_module, "_SECRET_CACHE", None)

            token = generate_token("user-case")
            payload = verify_token(token)

            assert payload["owner_id"] == "user-case"
            assert payload["sub"] == "user-case"
            assert payload["iss"] == "pendo-web"
            assert payload["typ"] == "pendo-web"
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_generated_widget_token_contains_widget_claims(self, monkeypatch):
        temp_dir = ROOT / ".pytest_cache" / "tmp" / f"pendo_web_auth_widget_{uuid.uuid4().hex}"
        secret_file = temp_dir / "web_token_secret.txt"
        temp_dir.mkdir(parents=True, exist_ok=True)
        try:
            monkeypatch.setattr(auth_module, "_SECRET_FILE", secret_file)
            monkeypatch.setattr(auth_module, "_SECRET_CACHE", None)

            token = generate_widget_token("widget-user")
            payload = verify_token(token)

            assert payload["owner_id"] == "widget-user"
            assert payload["kind"] == "widget"
            assert payload["scope"] == "widget:read"
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


class TestBrowserSessionAuth:
    def test_login_code_is_owner_bound_single_use_and_short_lived(self):
        code = issue_login_code("owner-a", expires_seconds=60)

        assert consume_login_code(code) == "owner-a"
        with pytest.raises(AuthError, match="already used"):
            consume_login_code(code)

    def test_web_session_is_revocable(self):
        session = create_web_session("owner-b", expires_seconds=60)

        assert get_web_session(session.session_id).owner_id == "owner-b"
        revoke_web_session(session.session_id)
        with pytest.raises(AuthError, match="invalid or expired"):
            get_web_session(session.session_id)

    def test_owner_can_list_and_revoke_a_session_by_non_secret_device_id(self):
        first = create_web_session("owner-c", expires_seconds=60)
        second = create_web_session("owner-c", expires_seconds=60)

        assert [session.device_id for session in list_web_sessions("owner-c")] == [
            second.device_id,
            first.device_id,
        ]
        assert revoke_web_session_device("owner-c", first.device_id) is True
        assert [session.device_id for session in list_web_sessions("owner-c")] == [second.device_id]
