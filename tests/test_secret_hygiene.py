from __future__ import annotations

from pathlib import Path

from tests.helpers.git import run_git

ROOT = Path(__file__).resolve().parents[1]


def test_no_deprecated_plugin_workspace_is_tracked() -> None:
    deprecated_paths = tuple(
        f"plugins/{name}" for name in ("ads_deprecated", "covid_deprecated", "memo_deprecated")
    )
    tracked = run_git(ROOT, "ls-files", "--", *deprecated_paths).splitlines()

    assert not tracked, f"unsupported historical plugin code is tracked: {tracked}"


def test_local_archive_is_ignored_and_docker_denied() -> None:
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    assert ".local_archive/" in gitignore
    assert "!.local_archive" not in dockerignore
