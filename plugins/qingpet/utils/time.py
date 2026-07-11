from __future__ import annotations

from datetime import datetime, timezone


def utc_now() -> datetime:
    """Return a host-timezone-independent naive UTC value for legacy columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def business_date() -> str:
    return utc_now().strftime("%Y-%m-%d")


def business_week() -> str:
    return utc_now().strftime("%G-W%V")
