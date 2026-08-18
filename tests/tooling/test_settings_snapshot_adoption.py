"""Architecture gates for the atomic plugin-settings contract."""

from __future__ import annotations

import ast

from tests.helpers.paths import REPOSITORY_ROOT

ROOT = REPOSITORY_ROOT
PLUGINS = ROOT / "plugins"

REVIEWED_RUNTIME_FILES = {
    "plugins/adnmb/main.py",
    "plugins/ads_paper/main.py",
    "plugins/apod/main.py",
    "plugins/arxiv_filter/main.py",
    "plugins/chat/main.py",
    "plugins/codex/config.py",
    "plugins/codex/manager.py",
    "plugins/flickr/client.py",
    "plugins/github/main.py",
    "plugins/pendo/main.py",
    "plugins/qingssh/output_relay.py",
    "plugins/qingssh/ssh_manager.py",
    "plugins/shell/main.py",
    "plugins/signin/yingshi.py",
    "plugins/smalltalk/main.py",
    "plugins/twitter/main.py",
    "plugins/voice/main.py",
    "plugins/wolframalpha/main.py",
    "plugins/xiaoqing_chat/handlers.py",
    "plugins/xiaoqing_chat/helper_utils.py",
}


def _is_context_reference(node: ast.expr) -> bool:
    if isinstance(node, ast.Name):
        return node.id in {"context", "ctx"} or node.id.endswith("_context")
    return isinstance(node, ast.Attribute) and node.attr == "context"


def test_plugins_do_not_bypass_the_atomic_settings_reader() -> None:
    violations: list[str] = []
    for path in sorted(PLUGINS.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr in {"config", "secrets"}
                and _is_context_reference(node.value)
            ):
                violations.append(f"{path.relative_to(ROOT)}:{node.lineno}:{node.attr}")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and len(node.args) >= 2
                and _is_context_reference(node.args[0])
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value in {"config", "secrets"}
            ):
                violations.append(
                    f"{path.relative_to(ROOT)}:{node.lineno}:getattr-{node.args[1].value}"
                )

    assert violations == []


def test_reviewed_runtime_readers_explicitly_use_settings_snapshots() -> None:
    missing = [
        relative
        for relative in sorted(REVIEWED_RUNTIME_FILES)
        if "get_settings_snapshot(" not in (ROOT / relative).read_text(encoding="utf-8")
    ]

    assert missing == []


def test_pendo_web_runtime_has_one_config_source() -> None:
    runtime_sources = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted((PLUGINS / "pendo").rglob("*.py"))
    )
    obsolete_environment_keys = {
        "PENDO_WEB_HOST",
        "PENDO_WEB_PORT",
        "PENDO_WEB_DEMO_ENABLED",
        "PENDO_WEB_SESSION_COOKIE_SECURE",
    }

    assert all(key not in runtime_sources for key in obsolete_environment_keys)
    assert "os.environ" not in (PLUGINS / "pendo" / "config.py").read_text(encoding="utf-8")
