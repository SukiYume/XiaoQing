from __future__ import annotations

from tests.helpers.git import run_git
from tests.helpers.paths import REPOSITORY_ROOT

ROOT = REPOSITORY_ROOT


def test_no_deprecated_plugin_workspace_is_tracked() -> None:
    deprecated_paths = tuple(
        f"plugins/{name}" for name in ("ads_deprecated", "covid_deprecated", "memo_deprecated")
    )
    tracked = run_git(ROOT, "ls-files", "--", *deprecated_paths).splitlines()

    assert not tracked, f"unsupported historical plugin code is tracked: {tracked}"


def test_no_retired_backup_source_is_tracked() -> None:
    """备份后缀会绕过正常审查和测试，不应进入发布源码。"""

    tracked = run_git(ROOT, "ls-files", "--", "*.old").splitlines()

    assert not tracked, f"retired backup source is tracked: {tracked}"


def test_local_archive_is_ignored_and_docker_denied() -> None:
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    assert ".local_archive/" in gitignore
    assert "!.local_archive" not in dockerignore
