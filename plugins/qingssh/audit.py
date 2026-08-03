"""QingSSH 普通审计日志使用的安全标量规范化。"""

from __future__ import annotations

import re

from core.sensitive_audit import audit_error_type

_AUDIT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}\Z")


def audit_id(value: object) -> str:
    """返回可安全写入日志的请求/任务 ID，否则使用固定占位符。"""
    return value if isinstance(value, str) and _AUDIT_ID_RE.fullmatch(value) else "-"


def audit_request_id(context: object) -> str:
    """从插件上下文提取经过约束的请求 ID。"""

    return audit_id(getattr(context, "request_id", ""))


__all__ = ["audit_error_type", "audit_id", "audit_request_id"]
