"""跨测试模块复用的领域无关断言。"""

import logging
import re
from collections.abc import Callable
from typing import Any

import pytest

from core.log import LogEvent
from core.public_errors import PUBLIC_ERROR_PREFIX

# 公开错误回复：前缀、原因，括号里是异常类型、可选的出错位置和 request id。
_PUBLIC_ERROR_REPLY = re.compile(
    rf"{re.escape(PUBLIC_ERROR_PREFIX)}(?P<reason>[^（\n]+)"
    r"（(?P<type_name>[A-Za-z_][\w.]*)(?: @ (?P<location>[^；]+))?；request_id：(?P<request_id>[^）\s]+)）"
)


def text_segments_text(
    segments: list[dict],
    *,
    separator: str = "",
) -> str:
    """按顺序拼接 OneBot 文本消息段，并允许调用方保留段间分隔。"""
    return separator.join(
        str(segment.get("data", {}).get("text", ""))
        for segment in segments
        if segment.get("type") == "text"
    )


def assert_http_error(status_code: int, call: Callable[[], Any]) -> Any:
    """执行调用并返回指定状态码的 FastAPI HTTP 异常。"""

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as captured:
        call()
    assert captured.value.status_code == status_code
    return captured.value


def logged_records(
    caplog: pytest.LogCaptureFixture, name: str | None = None
) -> list[logging.LogRecord]:
    """``caplog`` 里消息是 ``LogEvent`` 的记录，按记录顺序；给 ``name`` 时只取这个事件名。

    要看级别、logger 名或 request id 时用它；只看事件字段时用 :func:`logged_events`。
    """

    return [
        record
        for record in caplog.records
        if isinstance(record.msg, LogEvent) and (name is None or record.msg.name == name)
    ]


def logged_events(caplog: pytest.LogCaptureFixture, name: str | None = None) -> list[LogEvent]:
    """``caplog`` 里经 ``LogEvent()`` 记下的事件，按记录顺序；给 ``name`` 时只取这个事件名。"""

    return [record.msg for record in logged_records(caplog, name)]


def public_error_reply(text: str) -> re.Match[str]:
    """``text`` 里恰好一条公开错误回复，按格式拆成 ``reason``、``type_name``、``location``、
    ``request_id`` 几组；没有位置时 ``location`` 为 None。"""

    (match,) = _PUBLIC_ERROR_REPLY.finditer(text)
    return match


def redacted_field(value: str) -> object:
    """``value`` 在本进程里作为正文字段被日志脱敏后的样子（长度加指纹），用来核对日志记的是哪段原文。"""

    return LogEvent("probe", payload=value).fields["payload"]
