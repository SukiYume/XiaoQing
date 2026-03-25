"""Tests for pendo web auth module."""
import time
import pytest
from plugins.pendo.web.auth import generate_token, verify_token, AuthError


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
