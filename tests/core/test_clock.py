# 验证按配置时区取当前时间。
"""
Tests for core/clock.py - configured-timezone wall clock
"""

from core.clock import now_in_configured_timezone


def test_now_in_configured_timezone_uses_settings_snapshot() -> None:
    class Context:
        def get_settings_snapshot(self):
            return type("Settings", (), {"config": {"timezone": "UTC"}})()

    value = now_in_configured_timezone(Context())
    assert value.tzinfo is not None
    assert value.utcoffset().total_seconds() == 0
