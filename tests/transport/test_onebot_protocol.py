"""OneBot 回执判定与鉴权。"""

from __future__ import annotations

import pytest

import tests.helpers.onebot_test_support as _fixture_support
from core.onebot import _onebot_action_succeeded
from tests.helpers.onebot_test_support import verify_bearer_token

bounded_transport_adapter = _fixture_support.bounded_transport_adapter


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ({"status": "ok", "retcode": 0}, True),
        ({"status": "ok", "retcode": False}, False),
        ({"status": "ok", "retcode": 0.0}, False),
        ({"status": "failed", "retcode": 0}, False),
        (None, False),
    ],
)
def test_onebot_action_success_requires_exact_integer_retcode(response, expected):
    assert _onebot_action_succeeded(response) is expected


@pytest.mark.parametrize(
    ("header", "configured", "expected"),
    [
        pytest.param("Bearer my_token", "my_token", True, id="valid"),
        pytest.param("Bearer anything", "", False, id="no-token-configured-empty"),
        pytest.param("Bearer anything", None, False, id="no-token-configured-none"),
        pytest.param("Bearer wrong_token", "my_token", False, id="wrong-token"),
        pytest.param("my_token", "my_token", False, id="missing-bearer-prefix"),
        pytest.param("short", "much_longer_token", False, id="length-mismatch"),
    ],
)
def test_verify_bearer_token(header, configured, expected):
    """未配置 token 时默认拒绝，避免鉴权失败开放。"""
    assert verify_bearer_token(header, configured) is expected
