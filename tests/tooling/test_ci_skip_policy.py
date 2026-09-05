# 验证 CI 跳过白名单能够拒绝新增或过期的跳过项。
from __future__ import annotations

from types import SimpleNamespace

from tests.helpers.ci_skip_policy import (
    SkipAllowance,
    load_skip_allowances,
    unexpected_skips,
)


def test_repository_ci_skip_allowlist_is_valid(project_root) -> None:
    allowances = load_skip_allowances(project_root / "tests" / "ci_skip_allowlist.toml")

    assert allowances


def test_skip_policy_matches_node_reason_and_platform_together() -> None:
    allowance = SkipAllowance(
        nodeid    = "tests/test_platform.py::test_windows_*",
        reason    = "Windows-only capability",
        platforms = frozenset({"posix"}),
    )
    allowed = SimpleNamespace(
        nodeid   = "tests/test_platform.py::test_windows_tree",
        longrepr = ("test_platform.py", 1, "Skipped: Windows-only capability"),
    )
    wrong_reason = SimpleNamespace(
        nodeid   = allowed.nodeid,
        longrepr = ("test_platform.py", 1, "Skipped: dependency disappeared"),
    )

    assert unexpected_skips([allowed], [allowance], platform="posix") == ()
    assert unexpected_skips([allowed], [allowance], platform="windows") == (
        f"{allowed.nodeid}: Windows-only capability",
    )
    assert unexpected_skips([wrong_reason], [allowance], platform="posix") == (
        f"{allowed.nodeid}: dependency disappeared",
    )
