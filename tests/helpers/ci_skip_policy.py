"""Strict allowlist support for skips reported by CI test runs."""

from __future__ import annotations

import fnmatch
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

_VALID_PLATFORMS = frozenset({"all", "posix", "windows"})


@dataclass(frozen=True, slots=True)
class SkipAllowance:
    nodeid: str
    reason: str
    platforms: frozenset[str]

    def matches(self, *, nodeid: str, reason: str, platform: str) -> bool:
        return (
            ("all" in self.platforms or platform in self.platforms)
            and fnmatch.fnmatchcase(nodeid, self.nodeid)
            and fnmatch.fnmatchcase(reason, self.reason)
        )


def current_platform() -> str:
    return "windows" if os.name == "nt" else "posix"


def load_skip_allowances(path: Path) -> tuple[SkipAllowance, ...]:
    document = tomllib.loads(path.read_text(encoding="utf-8"))
    if document.get("version") != 1:
        raise ValueError("CI skip allowlist version must be 1")
    raw_entries = document.get("allow")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise ValueError("CI skip allowlist must contain entries")

    allowances: list[SkipAllowance] = []
    identities: set[tuple[str, str, frozenset[str]]] = set()
    for index, entry in enumerate(raw_entries):
        if not isinstance(entry, dict) or set(entry) != {"nodeid", "reason", "platforms"}:
            raise ValueError(f"CI skip allowlist entry {index} has an invalid shape")
        nodeid = entry["nodeid"]
        reason = entry["reason"]
        raw_platforms = entry["platforms"]
        if not isinstance(nodeid, str) or not nodeid:
            raise ValueError(f"CI skip allowlist entry {index} has an invalid nodeid")
        if not isinstance(reason, str) or not reason:
            raise ValueError(f"CI skip allowlist entry {index} has an invalid reason")
        if (
            not isinstance(raw_platforms, list)
            or not raw_platforms
            or any(not isinstance(value, str) for value in raw_platforms)
        ):
            raise ValueError(f"CI skip allowlist entry {index} has invalid platforms")
        platforms = frozenset(raw_platforms)
        if not platforms <= _VALID_PLATFORMS or ("all" in platforms and len(platforms) != 1):
            raise ValueError(f"CI skip allowlist entry {index} has invalid platforms")
        identity = (nodeid, reason, platforms)
        if identity in identities:
            raise ValueError(f"CI skip allowlist entry {index} is duplicated")
        identities.add(identity)
        allowances.append(SkipAllowance(nodeid=nodeid, reason=reason, platforms=platforms))
    return tuple(allowances)


def report_skip_reason(report: object) -> str:
    longrepr = getattr(report, "longrepr", "")
    if isinstance(longrepr, tuple) and len(longrepr) >= 3:
        reason = str(longrepr[2])
    else:
        reason = str(longrepr)
    prefix = "Skipped: "
    return reason[len(prefix) :] if reason.startswith(prefix) else reason


def unexpected_skips(
    reports: Iterable[object],
    allowances: Iterable[SkipAllowance],
    *,
    platform: str,
) -> tuple[str, ...]:
    active_allowances = tuple(allowances)
    unexpected: list[str] = []
    for report in reports:
        nodeid = str(getattr(report, "nodeid", "<unknown>"))
        reason = report_skip_reason(report)
        if any(
            allowance.matches(nodeid=nodeid, reason=reason, platform=platform)
            for allowance in active_allowances
        ):
            continue
        unexpected.append(f"{nodeid}: {reason}")
    return tuple(unexpected)
