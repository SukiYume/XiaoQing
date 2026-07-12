"""Strict scalar normalization for Minecraft ordinary audit logs."""

from __future__ import annotations

import re

_AUDIT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}\Z")
_ERROR_TYPE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,95}\Z")


def audit_request_id(context: object) -> str:
    value = getattr(context, "request_id", "")
    return value if isinstance(value, str) and _AUDIT_ID_RE.fullmatch(value) else "-"


def audit_error_type(exc: BaseException | None) -> str:
    if exc is None:
        return "-"
    candidate = type(exc).__name__
    return candidate if _ERROR_TYPE_RE.fullmatch(candidate) else "Exception"


__all__ = ["audit_error_type", "audit_request_id"]
