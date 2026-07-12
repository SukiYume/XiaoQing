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
import secrets
from dataclasses import dataclass

_FINGERPRINT_KEY = secrets.token_bytes(32)
_FINGERPRINT_HEX_CHARS = 24


@dataclass(frozen=True, slots=True)
class SensitiveAuditSummary:
    """Metadata safe to interpolate into an ordinary log record."""

    kind: str
    length: int
    byte_length: int
    fingerprint: str


def summarize_sensitive(value: str | bytes | bytearray | memoryview) -> SensitiveAuditSummary:
    """Return bounded metadata without retaining or exposing ``value``.

    ``length`` is the character count for text and the byte count for binary
    input.  Fingerprints intentionally rotate at process restart.  Callers
    must never use this helper as an authorization, persistence, or cache key.
    """

    if isinstance(value, str):
        kind = "text"
        length = len(value)
        payload = value.encode("utf-8", errors="surrogatepass")
    elif isinstance(value, (bytes, bytearray, memoryview)):
        kind = "bytes"
        payload = bytes(value)
        length = len(payload)
    else:
        raise TypeError("sensitive audit values must be text or bytes")

    digest = hmac.new(
        _FINGERPRINT_KEY,
        kind.encode("ascii") + b"\x00" + payload,
        hashlib.sha256,
    ).hexdigest()[:_FINGERPRINT_HEX_CHARS]
    return SensitiveAuditSummary(
        kind=kind,
        length=length,
        byte_length=len(payload),
        fingerprint=f"hmac-sha256:{digest}",
    )


__all__ = ["SensitiveAuditSummary", "summarize_sensitive"]
