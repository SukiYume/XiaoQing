"""自定义断言函数"""

from typing import Any


def assert_message_segment(actual: dict, expected_type: str, expected_text: str | None = None):
    """断言消息段类型和内容"""
    assert actual["type"] == expected_type, f"Expected type {expected_type}, got {actual['type']}"
    if expected_text is not None:
        assert actual["data"]["text"] == expected_text


def assert_text_message(actual: list, expected_text: str):
    """断言纯文本消息"""
    assert len(actual) == 1
    assert_message_segment(actual[0], "text", expected_text)


def assert_success_response(response: dict | str | list):
    """断言响应不为空/错误"""
    if isinstance(response, dict):
        assert "error" not in response or not response["error"]
    elif isinstance(response, str):
        assert len(response) > 0
    elif isinstance(response, list):
        assert len(response) > 0
