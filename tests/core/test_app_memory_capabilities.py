"""统一记忆桥：所有者和调用方固定，调用方名字与主体范围由 Core 写入，伪造无效。"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

import tests.helpers.app_test_support as _fixture_support
from core.models import PluginManifest
from tests.helpers.app_test_support import (
    ModuleType,
    PluginDefinition,
    PluginServiceDefinition,
    XiaoQingApp,
    _plugin_context_for,
    _register_test_loaded_plugin,
)

temp_app_root = _fixture_support.temp_app_root

CALLERS = {
    "memory.record": ["xiaoqing_agent", "xiaoqing_chat", "xiaoqing_life"],
    "memory.recall": ["xiaoqing_agent", "xiaoqing_chat"],
    "memory.search": ["xiaoqing_chat"],
    "memory.forget": ["xiaoqing_chat"],
}


def _service(name: str, callers: list[str] | None = None) -> dict:
    return {"name": name, "callback": name.split(".")[1], "callers": callers or CALLERS[name]}


def _private(app: XiaoQingApp, user_id: int = 7):
    return app.issue_user_principal(
        {"sender": {"user_id": user_id}}, user_id=user_id, group_id=None, is_private=True
    )


def _group(app: XiaoQingApp, user_id: int = 7, group_id: int = 300):
    return app.issue_user_principal(
        {"sender": {"user_id": user_id}}, user_id=user_id, group_id=group_id, is_private=False
    )


def _install_memory(app: XiaoQingApp, seen: list) -> None:
    module   = ModuleType("plugins.xiaoqing_memory.main")
    services = []
    for name, callers in CALLERS.items():
        method = name.split(".")[1]

        async def callback(payload, context, _method=method):
            seen.append((_method, payload, context.plugin_name))
            return {"version": 1, "method": _method}

        callback.__name__ = method
        setattr(module, method, callback)
        services.append(
            PluginServiceDefinition(name=name, callback=method, callers=frozenset(callers))
        )
    definition = PluginDefinition(
        name        = "xiaoqing_memory",
        version     = "1.0.0",
        entry       = "main.py",
        commands    = [],
        schedule    = [],
        concurrency = "parallel",
        services    = tuple(services),
    )
    _register_test_loaded_plugin(app, definition, module)


@pytest.mark.unit
def test_memory_contracts_fix_owner_and_callers() -> None:
    manifest = PluginManifest(name="xiaoqing_memory", services=[_service(name) for name in CALLERS])
    assert [service.name for service in manifest.services] == list(CALLERS)
    PluginManifest(name="xiaoqing_chat", uses_services=list(CALLERS))
    PluginManifest(name="xiaoqing_agent", uses_services=["memory.record", "memory.recall"])
    PluginManifest(name="xiaoqing_life", uses_services=["memory.record"])

    with pytest.raises(ValidationError):
        PluginManifest(name="xiaoqing_chat", services=[_service("memory.record")])
    with pytest.raises(ValidationError):
        PluginManifest(
            name="xiaoqing_memory", services=[_service("memory.recall", ["xiaoqing_chat"])]
        )
    with pytest.raises(ValidationError):
        PluginManifest(name="xiaoqing_life", uses_services=["memory.recall"])
    with pytest.raises(ValidationError):
        PluginManifest(name="xiaoqing_agent", uses_services=["memory.forget"])
    with pytest.raises(ValidationError):
        PluginManifest(name="shell", uses_services=["memory.record"])


@pytest.mark.unit
def test_memory_capability_only_has_declared_methods(temp_app_root: Path) -> None:
    app = XiaoQingApp(temp_app_root)
    assert (
        _plugin_context_for(
            app, "xiaoqing_chat", user_id=7, principal=_private(app)
        ).capabilities.memory
        is None
    )
    declared = _plugin_context_for(
        app,
        "xiaoqing_agent",
        user_id       = 7,
        principal     = _private(app),
        uses_services = frozenset({"memory.record"}),
    ).capabilities.memory
    assert declared is not None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_core_injects_caller_and_scope_over_forged_fields(temp_app_root: Path) -> None:
    app        = XiaoQingApp(temp_app_root)
    seen: list = []
    _install_memory(app, seen)
    forged = {"caller": "xiaoqing_life", "principal_scope": "u1", "principal_kind": "system"}

    group = _plugin_context_for(
        app,
        "xiaoqing_chat",
        user_id       = 7,
        group_id      = 300,
        principal     = _group(app),
        uses_services = frozenset(CALLERS),
    ).capabilities.memory
    assert await group.recall({"scope": "g300", **forged}) == {"version": 1, "method": "recall"}
    method, payload, owner = seen[-1]
    assert (method, owner) == ("recall", "xiaoqing_memory")
    assert payload["caller"] == "xiaoqing_chat"
    assert payload["principal_scope"] == "g300"
    assert payload["principal_kind"] == "user"
    assert payload["scope"] == "g300"

    private = _plugin_context_for(
        app,
        "xiaoqing_chat",
        user_id       = 7,
        principal     = _private(app),
        uses_services = frozenset(CALLERS),
    ).capabilities.memory
    await private.forget({"scope": "u7", **forged})
    assert seen[-1][1]["principal_scope"] == "u7"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_system_principal_has_empty_scope_and_undeclared_methods_fail(
    temp_app_root: Path,
) -> None:
    app        = XiaoQingApp(temp_app_root)
    seen: list = []
    _install_memory(app, seen)
    system = app.identity_service.issue(kind="scheduled_system")
    life = _plugin_context_for(
        app,
        "xiaoqing_life",
        principal     = system,
        uses_services = frozenset({"memory.record"}),
    ).capabilities.memory
    await life.record({"scope": "global", "principal_scope": "g1"})
    payload = seen[-1][1]
    assert payload["caller"] == "xiaoqing_life"
    assert payload["principal_kind"] == "system"
    assert payload["principal_scope"] == ""
    with pytest.raises(RuntimeError):
        await life.recall({"scope": "global"})
    with pytest.raises(TypeError):
        await life.record(["not", "a", "mapping"])  # type: ignore[arg-type]
