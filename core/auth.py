"""Shared authentication helpers."""

import hmac
import logging

logger = logging.getLogger(__name__)

_token_warning_shown = False


def verify_bearer_token(auth_header: str, expected_token: str | None) -> bool:
    """Verify a Bearer token without leaking token length through timing."""
    global _token_warning_shown
    if not expected_token:
        if not _token_warning_shown:
            logger.warning("Security: no token configured, all requests accepted")
            _token_warning_shown = True
        return True

    expected = f"Bearer {expected_token}"
    return hmac.compare_digest(auth_header.encode(), expected.encode())
