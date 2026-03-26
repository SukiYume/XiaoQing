"""Tests for pendo web auth module."""
import shutil
import time
import uuid
from pathlib import Path

import pytest

from plugins.pendo.web import auth as auth_module
from plugins.pendo.web.auth import generate_token, verify_token, AuthError


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
        temp_dir = ROOT / ".pytest_tmp" / f"pendo_web_auth_{uuid.uuid4().hex}"
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
        temp_dir = ROOT / ".pytest_tmp" / f"pendo_web_auth_{uuid.uuid4().hex}"
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
        temp_dir = ROOT / ".pytest_tmp" / f"pendo_web_auth_{uuid.uuid4().hex}"
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
