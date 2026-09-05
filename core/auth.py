# 认证工具：集中处理 Bearer 凭据解析与恒定时间比较。
"""Shared authentication helpers."""

import hmac


def verify_bearer_token(auth_header: str, expected_token: str | None) -> bool:
    """Verify a Bearer token, failing closed when no token is configured."""
    if not expected_token:
        return False

    expected = f"Bearer {expected_token}"
    return hmac.compare_digest(auth_header.encode(), expected.encode())
