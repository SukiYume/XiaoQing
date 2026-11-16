"""插件代际重新加载、隔离与运行态协调。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

import tests.helpers.plugin_manager_test_support as _fixture_support
from tests.helpers.plugin_manager_test_support import (
    DEMO_COMMAND,
    HOOKED_PLUGIN_SOURCE,
    PluginExecutionClosed,
    build_manager,
    load_plugin,
    load_plugin_dir,
    textwrap,
    write_plugin,
    write_runtime_manifest,
)
from tests.helpers.plugin_probe import PROBE

_isolate_process_global_plugin_import_state = (
    _fixture_support._isolate_process_global_plugin_import_state
)

# 关闭回调失败的旧代：卸载它会被隔离。
_FAILING_SHUTDOWN_SOURCE = """
from tests.helpers.plugin_probe import PROBE

PROBE.log("import").append(1)

async def shutdown(context=None):
    PROBE.log("shutdown").append(1)
    raise RuntimeError("resource still live")
"""

_ADMIN_ONLY_DEMO_COMMAND = {**DEMO_COMMAND, "admin_only": True}


@pytest.mark.asyncio
async def test_reload_plugin_keeps_old_instance_when_fingerprint_fails(tmp_path: Path):
    manager       = build_manager(tmp_path)
    old_plugin    = await load_plugin(manager)
    old_gate      = old_plugin.execution_gate
    real_snapshot = manager._authorize_plugin_snapshot
    failures      = [OSError("stat failed")]

    def snapshot(plugin_dir: Path, definition):  # type: ignore[no-untyped-def]
        if failures:
            raise failures.pop()
        return real_snapshot(plugin_dir, definition)

    # 文件在读取中途被换掉的竞态无法稳定地用真实输入造出来：只能在取指纹处注入失败。
    manager._authorize_plugin_snapshot = snapshot

    await manager.reload_plugin("demo")

    assert manager._plugins["demo"] is old_plugin
    assert old_gate.closed is False
    assert await old_gate.run(lambda: asyncio.sleep(0, "still live")) == "still live"
    assert PROBE.log("shutdown") == []
    assert len(PROBE.log("import")) == 1


@pytest.mark.asyncio
async def test_reload_retires_old_policy_when_replacement_fingerprint_fails(
    tmp_path: Path,
) -> None:
    manager = build_manager(tmp_path)
    plugin = await load_plugin(manager, commands=[DEMO_COMMAND])
    gate = plugin.execution_gate
    write_plugin(manager, commands=[_ADMIN_ONLY_DEMO_COMMAND])

    def snapshot(_plugin_dir: Path, _definition):  # type: ignore[no-untyped-def]
        raise OSError("stat denied")

    # 文件在读取中途被换掉的竞态无法稳定地用真实输入造出来：只能在取指纹处注入失败。
    manager._authorize_plugin_snapshot = snapshot

    await manager.reload_plugin("demo")

    assert gate.closed is True
    assert len(PROBE.log("shutdown")) == 1
    assert "demo" not in manager.list_runtime_plugins()
    assert manager.router.resolve("demo") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("manifest_state", ["invalid", "disabled"])
async def test_direct_reload_retires_invalid_or_disabled_manifest_fail_closed(
    tmp_path: Path,
    manifest_state: str,
) -> None:
    manager = build_manager(tmp_path)
    plugin = await load_plugin(manager, commands=[DEMO_COMMAND])
    gate = plugin.execution_gate
    assert manager.router.resolve("demo") is not None
    if manifest_state == "invalid":
        (manager.plugins_dir / "demo" / "plugin.json").write_text("{invalid", encoding="utf-8")
    else:
        write_runtime_manifest(
            manager.plugins_dir / "demo",
            "demo",
            commands = [DEMO_COMMAND],
            enabled  = False,
        )

    await manager.reload_plugin("demo")

    assert len(PROBE.log("shutdown")) == 1
    assert gate.closed is True
    assert manager.router.resolve("demo") is None
    assert "demo" not in manager._plugins
    assert "demo" not in manager._execution_gates


@pytest.mark.asyncio
async def test_reconcile_keeps_live_generation_when_required_dependency_is_transiently_missing(
    tmp_path: Path,
) -> None:
    manager = build_manager(tmp_path)
    plugin  = await load_plugin(manager)
    write_runtime_manifest(
        manager.plugins_dir / "demo",
        "demo",
        dependencies=[{"name": "missing_xiaoqing_dependency", "required": True}],
    )

    await manager.reconcile_plugins()

    assert manager._plugins["demo"] is plugin
    assert plugin.execution_gate.closed is False
    assert PROBE.log("shutdown") == []


@pytest.mark.asyncio
async def test_reload_plugin_quarantines_old_instance_when_shutdown_fails(tmp_path: Path):
    manager    = build_manager(tmp_path)
    old_plugin = await load_plugin(
        manager, "demo", _FAILING_SHUTDOWN_SOURCE, commands=[DEMO_COMMAND]
    )
    old_gate  = old_plugin.execution_gate
    old_state = manager._plugin_states["demo"]

    await manager.reload_plugin("demo")

    assert old_gate.closed is True
    assert manager._plugins["demo"] is old_plugin
    assert manager._execution_gates["demo"] is old_gate
    assert manager._plugin_states["demo"] is old_state
    assert "demo" in manager._quarantined_plugins
    assert len(PROBE.log("shutdown")) == 1
    # 旧代没停干净：模块还留着，候选代没有被导入。
    assert "plugins.demo.main" in sys.modules
    assert len(PROBE.log("import")) == 1
    assert manager.router.resolve("demo") is None


@pytest.mark.asyncio
async def test_reload_plugin_closes_old_gate_before_shutdown(tmp_path: Path):
    shutdown_started = asyncio.Event()
    release_shutdown = asyncio.Event()

    async def on_shutdown(_context) -> None:
        shutdown_started.set()
        await release_shutdown.wait()

    PROBE.slots["on_shutdown"] = on_shutdown
    manager                    = build_manager(tmp_path)
    old_plugin                 = await load_plugin(manager)
    old_gate                   = old_plugin.execution_gate
    write_plugin(manager, source=HOOKED_PLUGIN_SOURCE + "\nVERSION = 2\n")

    reload_task = asyncio.create_task(manager.reload_plugin("demo"))
    await asyncio.wait_for(shutdown_started.wait(), timeout=1)

    assert old_gate.closed is True
    ran = False

    async def operation() -> None:
        nonlocal ran
        ran = True

    with pytest.raises(PluginExecutionClosed, match="unloading"):
        await old_gate.run(operation)
    assert ran is False

    release_shutdown.set()
    await asyncio.wait_for(reload_task, timeout=1)
    assert manager._plugins["demo"].module.VERSION == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "manifest_failure",
    ["disabled", "deleted", "invalid", "invalid-with-backup", "missing-entry"],
)
async def test_reconcile_unloads_fail_closed_manifest_and_recovers_once(
    tmp_path: Path,
    manifest_failure: str,
) -> None:
    manager    = build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "demo"
    plugin_dir.mkdir()
    (plugin_dir / "__init__.py").write_text("", encoding="utf-8")
    manifest_path    = plugin_dir / "plugin.json"
    enabled_manifest = textwrap.dedent(
        """
        {
          "name": "demo",
          "version": "1.0.0",
          "entry": "main.py",
          "commands": [{"name": "demo", "triggers": ["demo"], "help": "demo"}],
          "schedule": [],
          "concurrency": "parallel",
          "enabled": true
        }
        """
    ).strip()
    manifest_path.write_text(enabled_manifest, encoding="utf-8")
    (plugin_dir / "main.py").write_text(
        textwrap.dedent(
            """
            SHUTDOWN_CALLS = 0

            async def shutdown(context=None):
                global SHUTDOWN_CALLS
                SHUTDOWN_CALLS += 1

            async def handle(command, args, event, context):
                return [{"type": "text", "data": {"text": "ok"}}]
            """
        ).strip(),
        encoding="utf-8",
    )

    await load_plugin_dir(manager, plugin_dir)
    original      = manager._plugins["demo"]
    original_gate = original.execution_gate
    assert manager.router.resolve("demo") is not None

    if manifest_failure == "disabled":
        manifest_path.write_text(
            enabled_manifest.replace('"enabled": true', '"enabled": false'),
            encoding="utf-8",
        )
    elif manifest_failure == "deleted":
        manifest_path.unlink()
    elif manifest_failure == "invalid":
        manifest_path.write_text("{invalid", encoding="utf-8")
    elif manifest_failure == "invalid-with-backup":
        manifest_path.with_name("plugin.json.bak").write_text(
            enabled_manifest,
            encoding="utf-8",
        )
        manifest_path.write_text("{invalid", encoding="utf-8")
    else:
        manifest_path.write_text(
            enabled_manifest.replace('"entry": "main.py"', '"entry": "missing.py"'),
            encoding="utf-8",
        )

    await manager.reconcile_plugins()

    assert "demo" not in manager._plugins
    assert "demo" not in manager._execution_gates
    assert manager.router.resolve("demo") is None
    assert original_gate is not None and original_gate.closed is True
    assert original.module.SHUTDOWN_CALLS == 1

    manifest_path.write_text(enabled_manifest, encoding="utf-8")
    await manager.reconcile_plugins()

    replacement = manager._plugins["demo"]
    resolved    = manager.router.resolve("demo")
    assert replacement is not original
    assert replacement.execution_gate is not original_gate
    assert resolved is not None
    assert resolved[0].execution_gate is replacement.execution_gate
    assert len([spec for spec in manager.router._commands if spec.plugin == "demo"]) == 1

    await manager.reconcile_plugins()
    assert manager._plugins["demo"] is replacement
    assert len([spec for spec in manager.router._commands if spec.plugin == "demo"]) == 1

    await manager.unload_plugin("demo")


@pytest.mark.asyncio
@pytest.mark.parametrize("manifest_failure", ["disabled", "invalid"])
async def test_reconcile_never_auto_retires_or_replaces_quarantined_plugin(
    tmp_path: Path,
    manifest_failure: str,
) -> None:
    manager     = build_manager(tmp_path)
    quarantined = await load_plugin(
        manager, "demo", _FAILING_SHUTDOWN_SOURCE, commands=[DEMO_COMMAND]
    )
    gate     = quarantined.execution_gate
    manifest = manager.plugins_dir / "demo" / "plugin.json"
    enabled = manifest.read_text(encoding="utf-8")

    if manifest_failure == "disabled":
        manifest.write_text(enabled.replace("true", "false"), encoding="utf-8")
    else:
        manifest.write_text("{invalid", encoding="utf-8")
    await manager.reconcile_plugins()
    manifest.write_text(enabled, encoding="utf-8")
    await manager.reconcile_plugins()

    assert manager._plugins["demo"] is quarantined
    assert manager._execution_gates["demo"] is gate
    assert "demo" in manager._quarantined_plugins
    assert gate.closed is True
    assert manager.router.resolve("demo") is None
    assert len(PROBE.log("shutdown")) == 1
    assert len(PROBE.log("import")) == 1


@pytest.mark.asyncio
async def test_reconcile_calls_are_serialized(tmp_path: Path) -> None:
    active  = 0
    maximum = 0

    async def exclusive_hook(_context) -> None:
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        try:
            await asyncio.sleep(0.02)
        finally:
            active -= 1

    PROBE.slots["on_init"]     = exclusive_hook
    PROBE.slots["on_shutdown"] = exclusive_hook
    manager                    = build_manager(tmp_path)
    await load_plugin(manager)
    write_plugin(manager, source=HOOKED_PLUGIN_SOURCE + "\nVERSION = 2\n")

    await asyncio.gather(manager.reconcile_plugins(), manager.reconcile_plugins())

    # 第二次协调排在第一次之后，看到的已经是新一代：只重载一次，且钩子从没有重叠。
    assert len(PROBE.log("shutdown")) == 1
    assert len(PROBE.log("init")) == 2
    assert manager._plugins["demo"].module.VERSION == 2
    assert maximum == 1


@pytest.mark.asyncio
async def test_reload_all_stops_before_later_plugins_or_reconcile_on_quarantine(
    tmp_path: Path,
) -> None:
    manager = build_manager(tmp_path)
    blocked = await load_plugin(manager, "blocked", _FAILING_SHUTDOWN_SOURCE)
    later   = await load_plugin(manager, "later")
    # 磁盘上多出一个还没装入的插件：真的跑了协调就会把它装进来。
    write_plugin(manager, "fresh")
    seen: list[str] = []

    async def before_reload(name: str) -> None:
        seen.append(name)

    completed = await manager.reload_all_plugins(before_reload=before_reload)

    assert completed is False
    assert seen == ["blocked"]
    assert "blocked" in manager._quarantined_plugins
    assert manager._plugins["blocked"] is blocked
    assert manager._plugins["later"] is later
    assert later.execution_gate.closed is False
    assert len(PROBE.log("shutdown")) == 1
    assert manager.get("fresh") is None


@pytest.mark.asyncio
async def test_reload_all_retires_invalid_plugin_before_later_quarantine(
    tmp_path: Path,
) -> None:
    manager = build_manager(tmp_path)
    invalid = await load_plugin(manager, "a_invalid")
    await load_plugin(manager, "b_blocked", _FAILING_SHUTDOWN_SOURCE)
    (manager.plugins_dir / "a_invalid" / "plugin.json").write_text("{invalid", encoding="utf-8")
    write_plugin(manager, "fresh")

    completed = await manager.reload_all_plugins()

    assert completed is False
    assert "a_invalid" not in manager._plugins
    assert "a_invalid" not in manager._execution_gates
    assert invalid.execution_gate.closed is True
    assert manager._plugins["b_blocked"].module.__name__ == "plugins.b_blocked.main"
    assert manager._execution_gates["b_blocked"].closed is True
    assert "b_blocked" in manager._quarantined_plugins
    assert manager.get("fresh") is None


@pytest.mark.asyncio
async def test_direct_reload_refuses_quarantined_generation(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = build_manager(tmp_path)
    plugin  = await load_plugin(manager, "demo", _FAILING_SHUTDOWN_SOURCE)
    await manager.unload_plugin("demo")
    assert "demo" in manager._quarantined_plugins
    write_plugin(manager, source=HOOKED_PLUGIN_SOURCE)

    await manager.reload_plugin("demo")

    assert manager._plugins["demo"] is plugin
    assert "Refusing to reload quarantined plugin demo" in caplog.text
    assert len(PROBE.log("import")) == 1


@pytest.mark.asyncio
async def test_reconcile_quarantines_old_entry_when_new_entry_cannot_import(
    tmp_path: Path,
) -> None:
    manager    = build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "demo"
    plugin_dir.mkdir()
    (plugin_dir / "__init__.py").write_text("", encoding="utf-8")
    manifest_path = plugin_dir / "plugin.json"
    manifest_path.write_text(
        '{"name":"demo","version":"1.0.0","entry":"main.py",'
        '"commands":[{"name":"demo","triggers":["demo"],"help":"demo"}],'
        '"schedule":[],"concurrency":"parallel","enabled":true}',
        encoding="utf-8",
    )
    (plugin_dir / "main.py").write_text(
        "async def handle(command, args, event, context):\n    return []\n",
        encoding="utf-8",
    )
    await load_plugin_dir(manager, plugin_dir)
    old_plugin = manager._plugins["demo"]
    old_gate   = old_plugin.execution_gate
    (plugin_dir / "broken.py").write_text("def broken(:\n", encoding="utf-8")
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8").replace("main.py", "broken.py"),
        encoding="utf-8",
    )

    await manager.reconcile_plugins()

    quarantined = manager._plugins["demo"]
    assert quarantined is old_plugin
    assert quarantined.definition.entry == "main.py"
    assert quarantined.execution_gate is manager._execution_gates["demo"]
    assert quarantined.execution_gate is not None
    assert quarantined.execution_gate.closed is True
    assert old_gate is not None and old_gate.closed is True
    assert "demo" in manager._quarantined_plugins
    assert manager.router.resolve("demo") is None

    await manager.unload_plugin("demo")
    assert "demo" not in manager.list_runtime_plugins()
    assert "demo" not in manager._restart_required_plugins


@pytest.mark.asyncio
async def test_reconcile_retires_changed_policy_when_fingerprint_fails(
    tmp_path: Path,
) -> None:
    manager = build_manager(tmp_path)
    plugin = await load_plugin(manager, commands=[DEMO_COMMAND])
    gate = plugin.execution_gate
    write_plugin(manager, commands=[_ADMIN_ONLY_DEMO_COMMAND])

    async def capture(_plugin_dir, _definition, **_kwargs):  # type: ignore[no-untyped-def]
        raise OSError("stat denied")

    # 文件在读取中途被换掉的竞态无法稳定地用真实输入造出来：只能在取指纹处注入失败。
    manager._capture_plugin_snapshot_async = capture

    await manager.reconcile_plugins()

    assert gate.closed is True
    assert len(PROBE.log("shutdown")) == 1
    assert "demo" not in manager.list_runtime_plugins()


@pytest.mark.asyncio
async def test_reconcile_reloads_when_only_the_manifest_changes(tmp_path: Path) -> None:
    manager = build_manager(tmp_path)
    old = await load_plugin(manager, commands=[DEMO_COMMAND])
    write_plugin(manager, commands=[_ADMIN_ONLY_DEMO_COMMAND])

    await manager.reconcile_plugins()

    replacement = manager._plugins["demo"]
    assert replacement is not old
    assert replacement.definition.commands[0]["admin_only"] is True
    assert old.execution_gate.closed is True
    assert len(PROBE.log("shutdown")) == 1
    assert len(PROBE.log("init")) == 2


@pytest.mark.asyncio
async def test_reload_cancellation_during_shutdown_quarantines_old(tmp_path: Path):
    shutdown_started = asyncio.Event()
    release_shutdown = asyncio.Event()

    async def on_shutdown(_context) -> None:
        shutdown_started.set()
        await release_shutdown.wait()

    PROBE.slots["on_shutdown"] = on_shutdown
    manager                    = build_manager(tmp_path)
    old_plugin                 = await load_plugin(manager)
    old_gate                   = old_plugin.execution_gate

    reload_task = asyncio.create_task(manager.reload_plugin("demo"))
    await asyncio.wait_for(shutdown_started.wait(), timeout=1)
    reload_task.cancel()
    await asyncio.sleep(0)
    assert reload_task.done() is False
    release_shutdown.set()
    with pytest.raises(asyncio.CancelledError):
        await reload_task

    assert old_gate.closed is True
    assert manager._plugins["demo"] is old_plugin
    assert "demo" in manager._quarantined_plugins
