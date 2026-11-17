"""插件定义、服务和执行契约。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

import tests.helpers.plugin_manager_test_support as _fixture_support
from core.plugin_manager_support import PluginDefinition
from core.plugin_watcher import _ManifestRejection
from tests.helpers.assertions import logged_events
from tests.helpers.paths import REPOSITORY_ROOT
from tests.helpers.plugin_manager_test_support import (
    CommandRouter,
    FatalLifecycleError,
    ModuleType,
    PluginManager,
    build_manager,
    detach_repository_plugin_modules,
    load_plugin,
    load_plugin_dir,
    write_plugin,
    write_runtime_manifest,
)
from tests.helpers.plugin_probe import PROBE

_isolate_process_global_plugin_import_state = (
    _fixture_support._isolate_process_global_plugin_import_state
)

VOICE_SERVICE = {
    "name"    : "voice.synthesize_text",
    "callback": "synthesize",
    "callers" : ["smalltalk"],
}
CODEX_SERVICE = {
    "name"               : "codex.enqueue_arxiv_summary",
    "callback"           : "enqueue",
    "callers"            : ["arxiv_filter"],
    "required_capability": "codex_arxiv_summary",
}


def test_is_plugin_dir_skips_deprecated_dirs(tmp_path: Path):
    manager        = build_manager(tmp_path)
    deprecated_dir = manager.plugins_dir / "memo_deprecated"
    deprecated_dir.mkdir()

    assert manager._is_plugin_dir(deprecated_dir) is False


def test_notify_change_warning_identifies_the_plugin(tmp_path: Path, caplog):
    manager = build_manager(tmp_path)

    def fail_change(_name: str) -> None:
        raise RuntimeError("broken callback")

    manager.on_change(fail_change)
    caplog.set_level("WARNING", logger="core.plugin_manager")

    manager._notify_change("demo")

    (failed,) = logged_events(caplog, "plugin.change_handler.failed")
    assert failed.fields["plugin"] == "demo"
    assert failed.fields["error"] == "RuntimeError"
    assert "broken callback" not in caplog.text


@pytest.mark.asyncio
async def test_plugin_modules_use_only_the_plugins_namespace(tmp_path: Path):
    import sys

    manager = build_manager(tmp_path)
    loaded  = await load_plugin(manager, "canonical_demo", "VALUE = object()")

    assert loaded.module.__name__ == "plugins.canonical_demo.main"
    assert "canonical_demo.main" not in sys.modules
    await manager.unload_plugin("canonical_demo")


@pytest.mark.asyncio
async def test_parent_reexport_imports_entry_once_and_purge_removes_parent_handles(
    tmp_path: Path,
) -> None:
    import sys

    manager    = build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "reexport_demo"
    plugin_dir.mkdir()
    (plugin_dir / "__init__.py").write_text("from .main import handle\n", encoding="utf-8")
    (plugin_dir / "main.py").write_text(
        "import xiaoqing_import_tracker as tracker\n"
        "tracker.executions += 1\n"
        "async def handle(command, args, event, context):\n"
        "    return []\n",
        encoding="utf-8",
    )
    (plugin_dir / "plugin.json").write_text(
        '{"name":"reexport_demo","version":"1.0.0","entry":"main.py",'
        '"commands":[],"schedule":[],"concurrency":"parallel","enabled":true}',
        encoding="utf-8",
    )
    tracker                       = ModuleType("xiaoqing_import_tracker")
    tracker.executions            = 0
    sys.modules[tracker.__name__] = tracker

    try:
        await load_plugin_dir(manager, plugin_dir)

        loaded  = manager._plugins["reexport_demo"]
        package = sys.modules["plugins.reexport_demo"]
        assert tracker.executions == 1
        assert package.handle is loaded.module.handle

        await manager.unload_plugin("reexport_demo")

        plugins_package = sys.modules["plugins"]
        assert "plugins.reexport_demo" not in sys.modules
        assert "plugins.reexport_demo.main" not in sys.modules
        assert not hasattr(plugins_package, "reexport_demo")
    finally:
        manager._purge_plugin_modules("reexport_demo")
        sys.modules.pop(tracker.__name__, None)


@pytest.mark.asyncio
async def test_partial_parent_import_requires_restart_and_cannot_overlap(
    tmp_path: Path,
) -> None:
    import sys

    manager    = build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "partial_parent"
    plugin_dir.mkdir()
    (plugin_dir / "__init__.py").write_text(
        "import xiaoqing_parent_tracker as tracker\n"
        "tracker.resources += 1\n"
        "raise RuntimeError('parent import failed')\n",
        encoding="utf-8",
    )
    (plugin_dir / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    (plugin_dir / "plugin.json").write_text(
        '{"name":"partial_parent","version":"1.0.0","entry":"main.py",'
        '"commands":[],"schedule":[],"concurrency":"parallel","enabled":true}',
        encoding="utf-8",
    )
    tracker                       = ModuleType("xiaoqing_parent_tracker")
    tracker.resources             = 0
    sys.modules[tracker.__name__] = tracker

    try:
        await load_plugin_dir(manager, plugin_dir)

        assert tracker.resources == 1
        assert "partial_parent" in manager._quarantined_plugins
        assert "partial_parent" in manager._restart_required_plugins
        gate = manager._execution_gates["partial_parent"]
        assert gate.closed is True

        await manager.unload_plugin("partial_parent")
        await load_plugin_dir(manager, plugin_dir)

        assert tracker.resources == 1
        assert "partial_parent" in manager.list_runtime_plugins()
    finally:
        manager._purge_plugin_modules("partial_parent")
        manager._restart_required_plugins.discard("partial_parent")
        manager._quarantined_plugins.discard("partial_parent")
        manager._execution_gates.pop("partial_parent", None)
        manager._plugin_states.pop("partial_parent", None)
        sys.modules.pop(tracker.__name__, None)


@pytest.mark.asyncio
async def test_moduleless_parent_fatal_preserves_original_and_restart_ledger(
    tmp_path: Path,
) -> None:
    import sys

    manager    = build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "fatal_parent"
    plugin_dir.mkdir()
    (plugin_dir / "__init__.py").write_text(
        "import xiaoqing_parent_fatal_tracker as tracker\nraise tracker.error\n",
        encoding="utf-8",
    )
    (plugin_dir / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    (plugin_dir / "plugin.json").write_text(
        '{"name":"fatal_parent","version":"1.0.0","entry":"main.py",'
        '"commands":[],"schedule":[],"concurrency":"parallel","enabled":true}',
        encoding="utf-8",
    )
    expected                      = FatalLifecycleError("fatal parent")
    tracker                       = ModuleType("xiaoqing_parent_fatal_tracker")
    tracker.error                 = expected
    sys.modules[tracker.__name__] = tracker

    try:
        with pytest.raises(FatalLifecycleError) as raised:
            await load_plugin_dir(manager, plugin_dir)

        assert raised.value is expected
        assert "fatal_parent" in manager._restart_required_plugins
        assert "fatal_parent" in manager._quarantined_plugins
        assert manager._execution_gates["fatal_parent"].closed is True
    finally:
        manager._purge_plugin_modules("fatal_parent")
        manager._restart_required_plugins.discard("fatal_parent")
        manager._quarantined_plugins.discard("fatal_parent")
        manager._execution_gates.pop("fatal_parent", None)
        manager._plugin_states.pop("fatal_parent", None)
        sys.modules.pop(tracker.__name__, None)


@pytest.mark.asyncio
async def test_external_alias_detection_is_restart_only(tmp_path: Path) -> None:
    import sys

    manager    = build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "alias_owned_elsewhere"
    plugin_dir.mkdir()
    (plugin_dir / "__init__.py").write_text("", encoding="utf-8")
    entry = plugin_dir / "main.py"
    entry.write_text("VALUE = 1\n", encoding="utf-8")
    (plugin_dir / "plugin.json").write_text(
        '{"name":"alias_owned_elsewhere","version":"1.0.0","entry":"main.py",'
        '"commands":[],"schedule":[],"concurrency":"parallel","enabled":true}',
        encoding="utf-8",
    )
    alias                       = ModuleType("alias_owned_elsewhere.main")
    alias.__file__              = str(entry)
    sys.modules[alias.__name__] = alias

    try:
        await load_plugin_dir(manager, plugin_dir)

        assert "alias_owned_elsewhere" in manager._restart_required_plugins
        assert "alias_owned_elsewhere" in manager._quarantined_plugins
        assert manager._execution_gates["alias_owned_elsewhere"].closed is True

        await manager.unload_plugin("alias_owned_elsewhere")
        assert "alias_owned_elsewhere" in manager.list_runtime_plugins()
        assert sys.modules[alias.__name__] is alias
    finally:
        manager._restart_required_plugins.discard("alias_owned_elsewhere")
        manager._quarantined_plugins.discard("alias_owned_elsewhere")
        manager._execution_gates.pop("alias_owned_elsewhere", None)
        manager._plugin_states.pop("alias_owned_elsewhere", None)
        sys.modules.pop(alias.__name__, None)
        manager._purge_plugin_modules("alias_owned_elsewhere")


@pytest.mark.parametrize(
    "manifest_text",
    [
        pytest.param('{"name":"demo","entry":"main.py","concurrency":"shared"}', id="concurrency"),
        pytest.param(
            '{"name":"demo","entry":"main.py","unknown_runtime_option":true}',
            id="unknown_field",
        ),
    ],
)
def test_load_definition_rejects_invalid_manifest(tmp_path: Path, caplog, manifest_text: str):
    manager    = build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "demo"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text(manifest_text, encoding="utf-8")

    rejection = manager._load_definition(plugin_dir)
    assert isinstance(rejection, _ManifestRejection)
    assert rejection.bucket == "invalid"
    (rejected,) = logged_events(caplog, "plugin.manifest.invalid")
    assert rejected.fields["plugin"] == "demo"


@pytest.mark.parametrize(
    ("plugin_name", "declarations", "expected_services", "expected_capabilities"),
    [
        (
            "smalltalk",
            '"uses_services":["chat.reply","voice.synthesize_text"]',
            frozenset({"chat.reply", "voice.synthesize_text"}),
            frozenset(),
        ),
        (
            "bot_core",
            '"capabilities":["secret_admin"]',
            frozenset(),
            frozenset({"secret_admin"}),
        ),
    ],
)
def test_load_definition_preserves_manifest_authorization_declarations(
    tmp_path: Path,
    plugin_name: str,
    declarations: str,
    expected_services: frozenset[str],
    expected_capabilities: frozenset[str],
) -> None:
    manager    = build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / plugin_name
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text(
        f'{{"name":"{plugin_name}","entry":"main.py",{declarations}}}',
        encoding="utf-8",
    )

    definition = manager._load_definition(plugin_dir)

    assert definition is not None
    assert definition.uses_services == expected_services
    assert definition.capabilities == expected_capabilities


@pytest.mark.asyncio
async def test_declared_service_registry_is_immutable_and_caller_scoped(tmp_path: Path):
    manager = build_manager(tmp_path)
    voice   = await load_plugin(
        manager,
        "voice",
        "async def synthesize(text, context):\n    return text, context\n",
        services=[VOICE_SERVICE],
    )

    loaded, service = manager.resolve_service(
        caller_plugin = "smalltalk",
        service_name  = "voice.synthesize_text",
    )
    assert loaded is voice
    assert service.callback is voice.module.synthesize
    assert loaded.services["voice.synthesize_text"] is service
    with pytest.raises(TypeError):
        loaded.services["voice.synthesize_text"] = service  # type: ignore[index]
    with pytest.raises(PermissionError):
        manager.resolve_service(
            caller_plugin = "shell",
            service_name  = "voice.synthesize_text",
        )
    with pytest.raises(RuntimeError):
        manager.resolve_service(
            caller_plugin = "smalltalk",
            service_name  = "voice.shutdown",
        )


@pytest.mark.asyncio
async def test_declared_service_callback_and_required_capability_fail_closed(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    manager = build_manager(tmp_path)
    detach_repository_plugin_modules(manager, "codex")
    await load_plugin_dir(
        manager, write_plugin(manager, "codex", "VALUE = 1", services=[CODEX_SERVICE])
    )
    # 声明的回调不存在：这一代加载失败，什么也没有发布。
    assert "Declared service callback" in caplog.text
    assert manager.get("codex") is None
    with pytest.raises(RuntimeError, match="unavailable"):
        manager.resolve_service(
            caller_plugin        = "arxiv_filter",
            service_name         = "codex.enqueue_arxiv_summary",
            granted_capabilities = frozenset({"codex_arxiv_summary"}),
        )

    await load_plugin(
        manager,
        "codex",
        "async def enqueue(*args):\n    return args\n",
        services=[CODEX_SERVICE],
    )
    with pytest.raises(PermissionError, match="requires capability"):
        manager.resolve_service(
            caller_plugin = "arxiv_filter",
            service_name  = "codex.enqueue_arxiv_summary",
        )
    loaded, _ = manager.resolve_service(
        caller_plugin        = "arxiv_filter",
        service_name         = "codex.enqueue_arxiv_summary",
        granted_capabilities = frozenset({"codex_arxiv_summary"}),
    )
    assert loaded.definition.name == "codex"

    # 只被隔离、执行门仍开着的状态，真实的卸载失败造不出（失败的卸载同时会关门）：直接标记隔离。
    manager._quarantined_plugins.add("codex")
    with pytest.raises(RuntimeError, match="not accepting calls"):
        manager.resolve_service(
            caller_plugin        = "arxiv_filter",
            service_name         = "codex.enqueue_arxiv_summary",
            granted_capabilities = frozenset({"codex_arxiv_summary"}),
        )
    manager._quarantined_plugins.discard("codex")

    loaded.execution_gate.close_admission()
    with pytest.raises(RuntimeError, match="not accepting calls"):
        manager.resolve_service(
            caller_plugin        = "arxiv_filter",
            service_name         = "codex.enqueue_arxiv_summary",
            granted_capabilities = frozenset({"codex_arxiv_summary"}),
        )


@pytest.mark.asyncio
async def test_reconcile_treats_deep_json_recursion_as_invalid_and_retires_old(
    tmp_path: Path,
) -> None:
    manager    = build_manager(tmp_path)
    plugin     = await load_plugin(manager)
    plugin_dir = manager.plugins_dir / "demo"
    (plugin_dir / "plugin.json").write_text(
        "[" * 10_000 + "0" + "]" * 10_000,
        encoding="utf-8",
    )

    await manager.reconcile_plugins()

    assert plugin.execution_gate.closed is True
    assert len(PROBE.log("shutdown")) == 1
    assert "demo" not in manager.list_runtime_plugins()


def test_load_definition_rejects_missing_required_python_dependency(tmp_path: Path, caplog):
    manager    = build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "demo"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text(
        '{"name":"demo","dependencies":[{"name":"missing_xiaoqing_dependency","required":true}]}',
        encoding="utf-8",
    )

    rejection = manager._load_definition(plugin_dir)
    assert isinstance(rejection, _ManifestRejection)
    assert rejection.bucket == "dependency"
    (missing,) = logged_events(caplog, "plugin.manifest.dependency_missing")
    assert missing.fields["dependency"] == "missing_xiaoqing_dependency"


def test_dependency_probe_never_imports_plugin_parent_package(tmp_path: Path) -> None:
    import sys

    manager    = build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "dependency_probe"
    plugin_dir.mkdir()
    (plugin_dir / "__init__.py").write_text(
        "import xiaoqing_dependency_tracker as tracker\ntracker.executed = True\n",
        encoding="utf-8",
    )
    (plugin_dir / "sub.py").write_text("VALUE = 1\n", encoding="utf-8")
    (plugin_dir / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    (plugin_dir / "plugin.json").write_text(
        '{"name":"dependency_probe","entry":"main.py",'
        '"dependencies":[{"name":"plugins.dependency_probe.sub","required":true}],'
        '"enabled":true}',
        encoding="utf-8",
    )
    tracker                       = ModuleType("xiaoqing_dependency_tracker")
    tracker.executed              = False
    sys.modules[tracker.__name__] = tracker

    try:
        rejection = manager._load_definition(plugin_dir)
        assert isinstance(rejection, _ManifestRejection)
        assert rejection.bucket == "dependency"
        assert tracker.executed is False
        assert "plugins.dependency_probe" not in sys.modules
    finally:
        sys.modules.pop(tracker.__name__, None)
        manager._purge_plugin_modules("dependency_probe")


def test_load_definition_allows_missing_optional_python_dependency(tmp_path: Path, caplog):
    manager    = build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "demo"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text(
        '{"name":"demo","dependencies":[{"name":"missing_xiaoqing_dependency","required":false}]}',
        encoding="utf-8",
    )

    with caplog.at_level("INFO"):
        definition = manager._load_definition(plugin_dir)

    assert definition is not None
    assert definition.dependencies == ["missing_xiaoqing_dependency"]
    (missing,) = logged_events(caplog, "plugin.manifest.optional_dependency_missing")
    assert missing.fields["dependency"] == "missing_xiaoqing_dependency"


def test_all_active_plugin_manifests_validate_against_the_strict_schema():
    root    = REPOSITORY_ROOT
    manager = PluginManager(
        plugins_dir     = root / "plugins",
        router          = CommandRouter(),
        context_factory = lambda *args, **kwargs: Mock(),
    )

    definitions = {
        path.name: manager._load_definition(path)
        for path in (root / "plugins").iterdir()
        if manager._is_plugin_dir(path)
    }

    # 拒绝结果同样“不为 None”：必须断言得到的是真正的定义，并报告被拒绝的插件与原因。
    assert definitions
    rejected = {
        name: definition.bucket
        for name, definition in definitions.items()
        if isinstance(definition, _ManifestRejection)
    }
    assert rejected == {}
    assert all(isinstance(definition, PluginDefinition) for definition in definitions.values())


@pytest.mark.asyncio
async def test_configure_execution_applies_per_plugin_timeout_override(tmp_path: Path):
    manager = build_manager(tmp_path)
    manager.configure_execution(
        {
            "timeout_seconds"      : 12,
            "parallel_limit"       : 2,
            "drain_timeout_seconds": 7,
            "overrides"            : {"codex": {"timeout_seconds": 0}},
        }
    )

    ordinary = await load_plugin(manager, "ordinary", "VALUE = 1")
    codex    = await load_plugin(manager, "codex", "VALUE = 1")

    assert ordinary.execution_gate.policy.timeout_seconds == 12
    assert ordinary.execution_gate.policy.parallel_limit == 2
    assert ordinary.execution_gate.policy.drain_timeout_seconds == 7
    assert codex.execution_gate.policy.timeout_seconds is None


@pytest.mark.asyncio
async def test_execution_gates_share_one_configured_sync_broker(tmp_path: Path):
    manager = build_manager(tmp_path)
    manager.configure_execution({"global_sync_queue_limit": 37})

    first  = await load_plugin(manager, "first", "VALUE = 1")
    second = await load_plugin(manager, "second", "VALUE = 1")

    assert manager._global_sync_queue_limit == 37
    assert first.execution_gate._sync_broker is manager._sync_broker
    assert second.execution_gate._sync_broker is manager._sync_broker


@pytest.mark.asyncio
async def test_execution_broker_closes_only_after_runtimes_and_rebuilds_lazily(
    tmp_path: Path,
):
    manager    = build_manager(tmp_path)
    old_broker = manager._sync_broker
    await load_plugin(manager)

    with pytest.raises(RuntimeError, match="runtimes remain: demo"):
        await manager.close_execution_broker(timeout_seconds=0.1)

    await manager.unload_plugin("demo", drain_timeout_seconds=0.1)
    result = await manager.close_execution_broker(timeout_seconds=0.1)

    assert result.drained is True
    assert old_broker.closed is True
    replacement = await load_plugin(manager, "replacement", "VALUE = 1")
    assert manager._sync_broker is not old_broker
    assert replacement.execution_gate._sync_broker is manager._sync_broker
    assert manager._sync_broker.closed is False

    await manager.unload_plugin("replacement", drain_timeout_seconds=0.1)
    await manager.close_execution_broker(timeout_seconds=0.1)


@pytest.mark.asyncio
async def test_unload_reuses_one_deadline_for_both_generation_drains(tmp_path: Path, monkeypatch):
    manager = build_manager(tmp_path)
    plugin  = await load_plugin(manager)
    gate    = plugin.execution_gate
    # 只控制卸载器的时钟，验证剩余预算递减，避免平台计时精度影响 5ms 等待。
    elapsed = 0.0
    clock = Mock(spec=["monotonic"], monotonic=lambda: elapsed)
    monkeypatch.setattr("core.plugin_generation.time", clock)
    monkeypatch.setattr("core.plugin_runtime.time", clock)
    observed_timeouts: list[float] = []
    original_close                 = gate.close

    async def close_gate(*, timeout_seconds: float | None = None):
        nonlocal elapsed
        assert timeout_seconds is not None
        observed_timeouts.append(timeout_seconds)
        elapsed += 0.005
        return await original_close(timeout_seconds=timeout_seconds)

    # 每次关闭记下收到的剩余预算，并让假时钟走 5ms，随后照常调用原函数。
    gate.close = close_gate  # type: ignore[method-assign]

    await manager.unload_plugin("demo", drain_timeout_seconds=0.05)

    assert len(observed_timeouts) == 2
    assert observed_timeouts == pytest.approx([0.05, 0.045])
    assert 0 <= observed_timeouts[1] < observed_timeouts[0] <= 0.05
    await manager.close_execution_broker(timeout_seconds=0.1)


@pytest.mark.asyncio
async def test_manifest_timeout_capability_is_applied_without_name_based_grant(tmp_path: Path):
    manager         = build_manager(tmp_path)
    name_only       = await load_plugin(manager, "qingssh", "VALUE = 1")
    capability_gate = (
        await load_plugin(
            manager,
            "codex",
            "VALUE = 1",
            capabilities=["execution_timeout_exempt"],
        )
    ).execution_gate

    assert name_only.execution_gate.policy.timeout_seconds is not None
    assert capability_gate.policy.timeout_seconds is None


@pytest.mark.asyncio
async def test_reload_plugin_installs_only_the_canonical_candidate(tmp_path: Path):
    import sys

    manager    = build_manager(tmp_path)
    plugin_dir = manager.plugins_dir / "demo"
    plugin_dir.mkdir()
    (plugin_dir / "__init__.py").write_text("", encoding="utf-8")
    (plugin_dir / "main.py").write_text(
        "import xiaoqing_candidate_tracker as tracker\n"
        "VALUE = 'old'\n"
        "async def shutdown(context=None):\n"
        "    tracker.shutdowns += 1\n",
        encoding="utf-8",
    )
    write_runtime_manifest(plugin_dir, "demo")
    tracker                       = ModuleType("xiaoqing_candidate_tracker")
    tracker.manager               = manager
    tracker.shutdowns             = 0
    tracker.published_at_import   = None
    sys.modules[tracker.__name__] = tracker

    try:
        await load_plugin_dir(manager, plugin_dir)
        old_plugin = manager._plugins["demo"]
        old_state  = manager._plugin_states["demo"]
        (plugin_dir / "main.py").write_text(
            "import xiaoqing_candidate_tracker as tracker\n"
            "tracker.published_at_import = tracker.manager._plugins['demo']\n"
            "VALUE = 'new'\n",
            encoding="utf-8",
        )

        await manager.reload_plugin("demo")

        assert tracker.published_at_import is old_plugin
        assert tracker.shutdowns == 1
        assert manager._plugins["demo"] is not old_plugin
        assert manager._plugins["demo"].module.VALUE == "new"
        assert manager._plugins["demo"].module.__name__ == "plugins.demo.main"
        assert "demo.main" not in sys.modules
        assert manager._plugin_states["demo"] is not old_state
    finally:
        await manager.unload_plugin("demo")
        sys.modules.pop(tracker.__name__, None)
