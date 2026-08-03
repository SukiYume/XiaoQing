"""为 Jupyter 代码执行路径生成不含原始载荷的结构化审计日志。"""

from __future__ import annotations

from core.sensitive_audit import (
    log_sensitive_operation as log_sensitive_audit,
)
from core.sensitive_audit import (
    safe_audit_id,
)


def context_request_id(context: object | None) -> str:
    if context is None:
        return "-"
    try:
        value = getattr(context, "request_id", None)
    except BaseException:
        return "-"
    return safe_audit_id(value)


__all__ = ["context_request_id", "log_sensitive_audit", "safe_audit_id"]
