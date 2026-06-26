"""
PluginManager unit tests.
"""

import asyncio
import os
import time
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, Mock
import textwrap

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


def test_is_plugin_dir_skips_deprecated_dirs(tmp_path: Path):
    manager = _build_manager(tmp_path)
    deprecated_dir = manager.plugins_dir / "memo_deprecated"
    deprecated_dir.mkdir()

    assert manager._is_plugin_dir(deprecated_dir) is False


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


@pytest.mark.asyncio
async def test_wait_inits_logs_timeout_from_async_init(tmp_path: Path, caplog):
    manager = _build_manager(tmp_path)

    async def timeout():
        raise asyncio.TimeoutError()

    task = asyncio.create_task(timeout())
    manager._init_tasks.append(task)
    manager._init_task_plugins[task] = "demo"
    manager.unload_plugin = AsyncMock()

    with caplog.at_level("WARNING"):
        await manager.wait_inits()

    assert "Plugin demo init timed out" in caplog.text


@pytest.mark.asyncio
async def test_load_plugin_registers_after_async_init_completes(tmp_path: Path):
    manager = _build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "demo"
    plugin_dir.mkdir()
    (plugin_dir / "__init__.py").write_text("", encoding="utf-8")
    (plugin_dir / "plugin.json").write_text(
        textwrap.dedent(
            """
            {
              "name": "demo",
              "version": "1.0.0",
              "entry": "main.py",
              "commands": [{"name": "demo", "triggers": ["demo"], "help": "demo"}],
              "schedule": [],
              "concurrency": "shared",
              "enabled": true
            }
            """
        ).strip(),
        encoding="utf-8",
    )
    (plugin_dir / "main.py").write_text(
        textwrap.dedent(
            """
            import asyncio

            READY = False

            async def init(context=None):
                global READY
                await asyncio.sleep(0)
                READY = True

            async def handle(command, args, event, context):
                return [{"type": "text", "data": {"text": "ok"}}]
            """
        ).strip(),
        encoding="utf-8",
    )

    manager.load_plugin(plugin_dir)
    assert "demo" not in manager._plugins

    await manager.wait_inits()

    assert "demo" in manager._plugins
    assert manager.router.resolve("demo") is not None


def test_get_mtime_tracks_submodule_changes(tmp_path: Path):
    manager = _build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "demo"
    plugin_dir.mkdir()
    helper = plugin_dir / "helper.py"
    helper.write_text("value = 1\n", encoding="utf-8")
    definition = _build_definition()
    (plugin_dir / "plugin.json").write_text(
        '{"name":"demo","version":"1.0.0","entry":"main.py","commands":[],"schedule":[],"concurrency":"shared","enabled":true}',
        encoding="utf-8",
    )
    (plugin_dir / "main.py").write_text("from .helper import value\n", encoding="utf-8")

    before = manager._get_mtime(plugin_dir, definition)
    helper.write_text("value = 2\n", encoding="utf-8")
    os.utime(helper, None)
    after = manager._get_mtime(plugin_dir, definition)

    assert after != before


def test_get_mtime_distinguishes_offsetting_file_mtimes(tmp_path: Path):
    manager = _build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "demo"
    plugin_dir.mkdir()
    definition = _build_definition()
    main = plugin_dir / "main.py"
    helper = plugin_dir / "helper.py"
    (plugin_dir / "plugin.json").write_text(
        '{"name":"demo","version":"1.0.0","entry":"main.py","commands":[],"schedule":[],"concurrency":"shared","enabled":true}',
        encoding="utf-8",
    )
    main.write_text("VALUE = 1\n", encoding="utf-8")
    helper.write_text("HELPER = 1\n", encoding="utf-8")

    base = time.time_ns()
    os.utime(main, ns=(base + 1_000, base + 1_000))
    os.utime(helper, ns=(base + 3_000, base + 3_000))
    before = manager._get_mtime(plugin_dir, definition)

    os.utime(main, ns=(base + 2_000, base + 2_000))
    os.utime(helper, ns=(base + 2_000, base + 2_000))
    after = manager._get_mtime(plugin_dir, definition)

    assert after != before


def test_iter_watch_files_ignores_runtime_data_dir(tmp_path: Path):
    manager = _build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "demo"
    plugin_dir.mkdir()
    data_dir = plugin_dir / "data"
    data_dir.mkdir()
    definition = _build_definition()
    (plugin_dir / "plugin.json").write_text(
        '{"name":"demo","version":"1.0.0","entry":"main.py","commands":[],"schedule":[],"concurrency":"shared","enabled":true}',
        encoding="utf-8",
    )
    (plugin_dir / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    runtime_state = data_dir / "state.json"
    runtime_state.write_text('{"count": 1}\n', encoding="utf-8")

    files = manager._iter_watch_files(plugin_dir, definition)

    assert runtime_state not in files


def test_build_context_ensures_data_dir_once(tmp_path: Path, monkeypatch):
    manager = _build_manager(tmp_path)
    calls: list[Path] = []

    def fake_ensure_dir(path: Path) -> None:
        calls.append(path)
        path.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr("core.plugin_manager.ensure_dir", fake_ensure_dir)

    manager.build_context("demo")
    manager.build_context("demo")

    assert calls == [manager.plugins_dir / "demo" / "data"]


@pytest.mark.asyncio
async def test_unload_plugin_clears_pending_plugin_state(tmp_path: Path):
    manager = _build_manager(tmp_path)
    definition = _build_definition()
    task = asyncio.create_task(asyncio.sleep(0))
    manager._init_tasks.append(task)
    manager._init_task_plugins[task] = "demo"
    manager._pending_plugins[task] = (definition, ModuleType("demo.main"), 0.0)
    manager._plugin_states["demo"] = {"value": 1}

    await manager.unload_plugin("demo")

    assert "demo" not in manager._plugin_states
    assert not manager._pending_plugins
