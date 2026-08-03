"""Minecraft 普通审计日志使用的严格标量归一化。"""

from __future__ import annotations

import re

from core.sensitive_audit import audit_error_type

_AUDIT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}\Z")


def audit_request_id(context: object) -> str:
    """只接受可安全写入单行日志的有界 request_id。"""

    value = getattr(context, "request_id", "")
    return value if isinstance(value, str) and _AUDIT_ID_RE.fullmatch(value) else "-"


__all__ = ["audit_error_type", "audit_request_id"]
