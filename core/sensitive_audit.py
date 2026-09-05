# 敏感审计：记录进程内可关联的指纹，日志内容保持凭据隔离。
"""Safe, restart-scoped fingerprints for sensitive log metadata.

Administrator tools intentionally accept arbitrary commands and prompts.  The
payload must remain available to the tool, but ordinary logs should contain
only enough metadata to correlate lifecycle events.  A process-random HMAC
key makes the fingerprint deterministic during one process lifetime without
turning low-entropy commands into an offline dictionary target.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
from dataclasses import dataclass

_FINGERPRINT_KEY       = secrets.token_bytes(32)
_FINGERPRINT_HEX_CHARS = 24
_AUDIT_ID_RE           = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}\Z")
_AUDIT_LABEL_RE        = re.compile(r"[a-z][a-z0-9_.:-]{0,95}\Z")
_ERROR_TYPE_RE         = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,95}\Z")


@dataclass(frozen=True, slots=True)
class SensitiveAuditSummary:
    """Metadata safe to interpolate into an ordinary log record."""

    kind: str
    length: int
    byte_length: int
    fingerprint: str


def audit_error_type(exc: BaseException | None) -> str:
    """Return a bounded, log-safe exception type name."""

    if exc is None:
        return "-"
    candidate = type(exc).__name__
    return candidate if _ERROR_TYPE_RE.fullmatch(candidate) else "Exception"


def safe_audit_id(value: object) -> str:
    """Return a bounded identifier safe to include in audit metadata."""

    return value if type(value) is str and _AUDIT_ID_RE.fullmatch(value) else "-"


def safe_audit_label(value: object) -> str:
    """Return a bounded operation/status label safe for structured logs."""

    return value if type(value) is str and _AUDIT_LABEL_RE.fullmatch(value) else "unknown"


def summarize_sensitive(value: str | bytes | bytearray | memoryview) -> SensitiveAuditSummary:
    """Return bounded metadata without retaining or exposing ``value``.

    ``length`` is the character count for text and the byte count for binary
    input.  Fingerprints intentionally rotate at process restart.  Callers
    must never use this helper as an authorization, persistence, or cache key.
    """

    if isinstance(value, str):
        kind   = "text"
        length = len(value)
        payload = value.encode("utf-8", errors="surrogatepass")
    elif isinstance(value, (bytes, bytearray, memoryview)):
        kind    = "bytes"
        payload = bytes(value)
        length  = len(payload)
    else:
        raise TypeError("sensitive audit values must be text or bytes")

    digest = hmac.new(
        _FINGERPRINT_KEY,
        kind.encode("ascii") + b"\x00" + payload,
        hashlib.sha256,
    ).hexdigest()[:_FINGERPRINT_HEX_CHARS]
    return SensitiveAuditSummary(
        kind        = kind,
        length      = length,
        byte_length = len(payload),
        fingerprint = f"hmac-sha256:{digest}",
    )


def log_sensitive_operation(
    target_logger: logging.Logger,
    operation: str,
    *,
    status: str,
    request_id: object                                   = None,
    job_id: object                                       = None,
    payload: str | bytes | bytearray | memoryview | None = None,
    exc: BaseException | None                            = None,
    error_type: str                                      = "-",
    return_code: int | None                              = None,
    level: int                                           = logging.INFO,
) -> None:
    """Log safe metadata for an operation without retaining sensitive text.

    Plugins may add their own stable numeric fields such as ``return_code``;
    arbitrary user-controlled fields do not belong in this shared boundary.
    """

    if payload is None:
        payload_kind        = "none"
        payload_length      = 0
        payload_bytes       = 0
        payload_fingerprint = "-"
    else:
        summary             = summarize_sensitive(payload)
        payload_kind        = summary.kind
        payload_length      = summary.length
        payload_bytes       = summary.byte_length
        payload_fingerprint = summary.fingerprint
    target_logger.log(
        logging.ERROR if exc is not None else level,
        "sensitive_audit operation=%s request_id=%s job_id=%s status=%s "
        "return_code=%s error_type=%s payload_kind=%s payload_length=%d "
        "payload_bytes=%d payload_fingerprint=%s",
        safe_audit_label(operation),
        safe_audit_id(request_id),
        safe_audit_id(job_id),
        safe_audit_label(status),
        return_code if type(return_code) is int else "-",
        audit_error_type(exc) if exc is not None else _safe_error_type(error_type),
        payload_kind,
        payload_length,
        payload_bytes,
        payload_fingerprint,
    )


def _safe_error_type(value: object) -> str:
    return value if type(value) is str and _ERROR_TYPE_RE.fullmatch(value) else "Exception"


__all__ = [
    "SensitiveAuditSummary",
    "audit_error_type",
    "log_sensitive_operation",
    "safe_audit_id",
    "safe_audit_label",
    "summarize_sensitive",
]
