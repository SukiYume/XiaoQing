"""Regression tests for cross-plugin command trigger ownership."""

import json
from itertools import permutations
from pathlib import Path
from typing import Any

from core.router import CommandRouter, CommandSpec

ROOT = Path(__file__).resolve().parent.parent


async def _handler(
    command: str,
    args: str,
    event: dict[str, Any],
    context: Any,
) -> list[dict[str, Any]]:
    return []


def _command_spec(plugin_name: str, command_name: str) -> CommandSpec:
    manifest = json.loads((ROOT / "plugins" / plugin_name / "plugin.json").read_text(encoding="utf-8"))
    command = next(item for item in manifest["commands"] if item["name"] == command_name)
    return CommandSpec(
        plugin=plugin_name,
        name=command["name"],
        triggers=command["triggers"],
        help_text=command["help"],
        admin_only=command["admin_only"],
        handler=_handler,
        priority=command.get("priority", 0),
    )


def test_exec_trigger_has_stable_shell_owner_regardless_of_plugin_load_order() -> None:
    specs = [_command_spec("jupyter", "jupyter"), _command_spec("shell", "shell")]

    for ordered_specs in permutations(specs):
        router = CommandRouter()
        for spec in ordered_specs:
            router.register(spec)

        resolved = router.resolve("exec echo stable")
        assert resolved is not None
        assert resolved[0].plugin == "shell"
        assert resolved[0].admin_only is True


def test_explicit_aliases_keep_both_admin_execution_backends_available() -> None:
    router = CommandRouter()
    router.register(_command_spec("jupyter", "jupyter"))
    router.register(_command_spec("shell", "shell"))

    assert router.resolve("py print(1)")[0].plugin == "jupyter"  # type: ignore[index]
    assert router.resolve("shell echo ok")[0].plugin == "shell"  # type: ignore[index]


def test_all_cross_plugin_duplicate_triggers_have_explicit_distinct_priorities() -> None:
    owners: dict[str, list[tuple[str, int]]] = {}
    for manifest_path in (ROOT / "plugins").glob("*/plugin.json"):
        if manifest_path.parent.name.endswith("_deprecated"):
            continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for command in manifest.get("commands", []):
            for trigger in command.get("triggers", []):
                owners.setdefault(trigger, []).append(
                    (manifest["name"], int(command.get("priority", 0)))
                )

    for trigger, trigger_owners in owners.items():
        plugins = {plugin_name for plugin_name, _priority in trigger_owners}
        if len(plugins) > 1:
            priorities = [priority for _plugin_name, priority in trigger_owners]
            assert len(priorities) == len(set(priorities)), (
                f"ambiguous cross-plugin trigger {trigger!r}: {trigger_owners}"
            )
