"""从仓库里真实的 plugin.json 构建命令目录，供需要完整命令目录的测试使用。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from core.models import PluginManifest
from core.router import (
    CommandCatalogNode,
    CommandInvocation,
    CommandRouter,
    CommandSpec,
    build_command_catalog_node,
)
from tests.helpers.paths import REPOSITORY_ROOT

PLUGIN_MANIFESTS = tuple(sorted((REPOSITORY_ROOT / "plugins").glob("*/plugin.json")))


@dataclass(frozen=True, slots=True)
class CatalogFixture:
    manifests  : tuple[PluginManifest, ...]
    roots      : tuple[CommandCatalogNode, ...]
    router     : CommandRouter
    invocations: list[CommandInvocation]


def load_production_catalog() -> CatalogFixture:
    """从生产 manifest 构建与 PluginManager 相同的不可变目录。"""

    manifests = tuple(
        PluginManifest.model_validate(json.loads(path.read_text(encoding="utf-8")))
        for path in PLUGIN_MANIFESTS
    )
    roots: list[CommandCatalogNode]      = []
    router                               = CommandRouter()
    invocations: list[CommandInvocation] = []

    async def catalog_handler(
        _command: str,
        _args: str,
        _event: dict[str, Any],
        context: Any,
    ) -> list[dict[str, Any]]:
        invocation = context.command_invocation
        invocations.append(invocation)
        return [{"type": "text", "data": {"text": invocation.node.code}}]

    for manifest in manifests:
        for command in manifest.commands:
            root = build_command_catalog_node(
                manifest.name,
                command.model_dump(),
                root=True,
            )
            roots.append(root)
            router.register(
                CommandSpec(
                    plugin   = manifest.name,
                    name     = command.name,
                    triggers = command.triggers,
                    handler  = catalog_handler,
                    catalog  = root,
                    priority = command.priority,
                )
            )
    return CatalogFixture(manifests, tuple(roots), router, invocations)
