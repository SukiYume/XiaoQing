from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_all_deprecated_plugin_workspaces_are_absent() -> None:
    for name in ("ads_deprecated", "covid_deprecated", "memo_deprecated"):
        deprecated_dir = ROOT / "plugins" / name
        assert not deprecated_dir.exists(), (
            f"plugins/{name} is unsupported historical code and must not be restored "
            "to the production workspace"
        )


def test_local_archive_is_ignored_and_docker_denied() -> None:
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    assert ".local_archive/" in gitignore
    assert "!.local_archive" not in dockerignore
