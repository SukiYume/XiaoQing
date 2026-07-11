"""Security policy for XiaoQing's plaintext inbound HTTP/WS listeners."""

from __future__ import annotations

import ipaddress
from typing import Literal
from urllib.parse import SplitResult, urlsplit

InboundTransport = Literal["http", "ws"]


def is_loopback_host(host: str) -> bool:
    """Return whether a listener host is unambiguously local-only."""

    normalized = host.rstrip(".").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def parse_inbound_listener(value: object, transport: InboundTransport) -> SplitResult | None:
    """Parse one supported plaintext listener URL or raise a useful error."""

    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"invalid inbound {transport.upper()} listener URL") from exc

    secure_scheme = "https" if transport == "http" else "wss"
    if parts.scheme.lower() == secure_scheme:
        raise ValueError(
            f"{secure_scheme}:// inbound listeners are not supported because XiaoQing "
            "does not terminate TLS; use a trusted TLS reverse proxy"
        )
    if parts.scheme.lower() != transport or not parts.hostname or port is None:
        raise ValueError(
            f"inbound {transport.upper()} listener must be an absolute "
            f"{transport}:// URL with an explicit port"
        )
    if parts.username is not None or parts.password is not None:
        raise ValueError("inbound listener URLs must not contain user information")
    if parts.query or parts.fragment:
        raise ValueError("inbound listener URLs must not contain a query or fragment")
    if transport == "http" and parts.path not in {"", "/"}:
        raise ValueError("inbound HTTP listener must not contain a path")
    return parts


def validate_inbound_listener(
    value: object,
    transport: InboundTransport,
    *,
    trusted_tls_proxy: bool,
) -> SplitResult | None:
    """Enforce TLS-proxy acknowledgement for any non-loopback bind."""

    parts = parse_inbound_listener(value, transport)
    if parts is None:
        return None
    host = parts.hostname or ""
    if not is_loopback_host(host) and trusted_tls_proxy is not True:
        raise ValueError(
            f"non-loopback inbound {transport.upper()} listener {host!r} is plaintext; "
            "bind to loopback or set inbound_trusted_tls_proxy=true only when a "
            "trusted TLS proxy protects the listener"
        )
    return parts
