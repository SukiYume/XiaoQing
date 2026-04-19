"""
PluginManager unit tests.
"""

import asyncio
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, Mock

import pytest

from core.plugin_manager import LoadedPlugin, PluginDefinition, PluginManager
from core.router import CommandRouter


def _build_manager(tmp_path: Path) -> PluginManager:
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    return PluginManager(
        plugins_dir=plugins_dir,
        router=CommandRouter(),
        context_factory=lambda *args, **kwargs: Mock(),
    )


def _build_definition(name: str = "demo") -> PluginDefinition:
    return PluginDefinition(
        name=name,
        version="1.0.0",
        entry="main.py",
        commands=[],
        schedule=[],
        concurrency="shared",
        enabled=True,
    )


@pytest.mark.asyncio
async def test_reload_plugin_waits_for_async_inits(tmp_path: Path):
    manager = _build_manager(tmp_path)
    definition = _build_definition()
    module = ModuleType("demo.main")
    manager._plugins["demo"] = LoadedPlugin(definition=definition, module=module, mtime=0.0)
    manager.unload_plugin = AsyncMock()
    manager.load_plugin = Mock()
    manager.wait_inits = AsyncMock()

    await manager.reload_plugin("demo")

    manager.unload_plugin.assert_awaited_once_with("demo")
    manager.load_plugin.assert_called_once_with(manager.plugins_dir / "demo")
    manager.wait_inits.assert_awaited_once()


@pytest.mark.asyncio
async def test_wait_inits_unloads_plugin_when_async_init_fails(tmp_path: Path):
    manager = _build_manager(tmp_path)
    definition = _build_definition()
    module = ModuleType("demo.main")
    manager._plugins["demo"] = LoadedPlugin(definition=definition, module=module, mtime=0.0)

    async def fail():
        raise RuntimeError("boom")

    task = asyncio.create_task(fail())
    manager._init_tasks.append(task)
    manager._init_task_plugins[task] = "demo"
    manager.unload_plugin = AsyncMock(side_effect=lambda name: manager._plugins.pop(name, None))

    await manager.wait_inits()

    manager.unload_plugin.assert_awaited_once_with("demo")
    assert "demo" not in manager._plugins
