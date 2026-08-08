"""Keep user-facing command aliases synchronized with executable manifests."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

import pytest

from tests.helpers.paths import REPOSITORY_ROOT

ROOT = REPOSITORY_ROOT
AUDITED_PLUGINS = (
    "apod",
    "chat",
    "chime",
    "choice",
    "codex",
    "color",
    "dict",
    "earthquake",
    "echo",
    "github",
    "guess_number",
    "jupyter",
    "minecraft",
    "qingssh",
)
SECTION_START = "<!-- manifest-command-aliases:start -->"
SECTION_END = "<!-- manifest-command-aliases:end -->"
SLASH_CODE_SPAN_RE = re.compile(r"`/([^`\s]+)`")


def _manifest_trigger_sets(plugin_dir: Path) -> list[frozenset[str]]:
    manifest = json.loads((plugin_dir / "plugin.json").read_text(encoding="utf-8"))
    return [
        frozenset(str(trigger) for trigger in command.get("triggers", []))
        for command in manifest.get("commands", [])
    ]


def _documented_trigger_sets(plugin_dir: Path) -> list[frozenset[str]]:
    readme = (plugin_dir / "README.md").read_text(encoding="utf-8")
    assert readme.count(SECTION_START) == 1
    assert readme.count(SECTION_END) == 1
    section = readme.split(SECTION_START, 1)[1].split(SECTION_END, 1)[0]

    rows: list[frozenset[str]] = []
    for line in section.splitlines():
        triggers = SLASH_CODE_SPAN_RE.findall(line)
        if not triggers:
            continue
        assert len(triggers) == len(set(triggers)), f"duplicate alias in README row: {line}"
        rows.append(frozenset(triggers))
    return rows


@pytest.mark.parametrize("plugin_name", AUDITED_PLUGINS)
def test_readme_command_alias_rows_match_manifest(plugin_name: str) -> None:
    """Each visible table row must describe exactly one manifest command."""

    plugin_dir = ROOT / "plugins" / plugin_name
    expected = Counter(_manifest_trigger_sets(plugin_dir))
    documented = Counter(_documented_trigger_sets(plugin_dir))
    assert documented == expected
