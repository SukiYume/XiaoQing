"""统一记忆桥：所有者和调用方固定，调用方名字与主体范围由 Core 写入，伪造无效。"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

import tests.helpers.app_test_support as _fixture_support
from core.models import PluginManifest
from tests.helpers.app_test_support import (
    PROBE,
    XiaoQingApp,
    load_test_plugin,
    plugin_context_for,
    service_declaration,
    user_principal,
)

temp_app_root                               = _fixture_support.temp_app_root
_isolate_process_global_plugin_import_state = (
    _fixture_support._isolate_process_global_plugin_import_state
)

CALLERS = {
    "memory.record": ["xiaoqing_agent", "xiaoqing_chat", "xiaoqing_life"],
    "memory.recall": ["xiaoqing_agent", "xiaoqing_chat"],
    "memory.search": ["xiaoqing_chat"],
    "memory.forget": ["xiaoqing_chat"],
}


def _service(name: str, callers: list[str] | None = None) -> dict:
    return {"name": name, "callback": name.split(".")[1], "callers": callers or CALLERS[name]}


MEMORY_SOURCE = """
from tests.helpers.plugin_probe import PROBE

def _answer(method, payload, context):
    PROBE.log("memory").append((method, payload, context.plugin_name))
    return {"version": 1, "method": method}

async def record(payload, context):
    return _answer("record", payload, context)

async def recall(payload, context):
    return _answer("recall", payload, context)

async def search(payload, context):
    return _answer("search", payload, context)

async def forget(payload, context):
    return _answer("forget", payload, context)
"""


async def _install_memory(app: XiaoQingApp) -> list:
    """装入 xiaoqing_memory；返回的列表按顺序收录它收到的 ``(方法, 载荷, 所有者)``。"""

    await load_test_plugin(
        app,
        "xiaoqing_memory",
        MEMORY_SOURCE,
        services=[
            service_declaration(name, name.split(".")[1], tuple(callers))
            for name, callers in CALLERS.items()
        ],
    )
    return PROBE.log("memory")


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
        plugin_context_for(
            app, "xiaoqing_chat", user_id=7, principal=user_principal(app, 7)
        ).capabilities.memory
        is None
    )
    declared = plugin_context_for(
        app,
        "xiaoqing_agent",
        user_id       = 7,
        principal     = user_principal(app, 7),
        uses_services = frozenset({"memory.record"}),
    ).capabilities.memory
    assert declared is not None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_core_injects_caller_and_scope_over_forged_fields(temp_app_root: Path) -> None:
    app    = XiaoQingApp(temp_app_root)
    seen   = await _install_memory(app)
    forged = {"caller": "xiaoqing_life", "principal_scope": "u1", "principal_kind": "system"}

    group = plugin_context_for(
        app,
        "xiaoqing_chat",
        user_id       = 7,
        group_id      = 300,
        principal     = user_principal(app, 7, 300),
        uses_services = frozenset(CALLERS),
    ).capabilities.memory
    assert await group.recall({"scope": "g300", **forged}) == {"version": 1, "method": "recall"}
    method, payload, owner = seen[-1]
    assert (method, owner) == ("recall", "xiaoqing_memory")
    assert payload["caller"] == "xiaoqing_chat"
    assert payload["principal_scope"] == "g300"
    assert payload["principal_kind"] == "user"
    assert payload["scope"] == "g300"

    private = plugin_context_for(
        app,
        "xiaoqing_chat",
        user_id       = 7,
        principal     = user_principal(app, 7),
        uses_services = frozenset(CALLERS),
    ).capabilities.memory
    await private.forget({"scope": "u7", **forged})
    assert seen[-1][1]["principal_scope"] == "u7"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_system_principal_has_empty_scope_and_undeclared_methods_fail(
    temp_app_root: Path,
) -> None:
    app  = XiaoQingApp(temp_app_root)
    seen = await _install_memory(app)
    system = app.identity_service.issue(kind="scheduled_system")
    life = plugin_context_for(
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
