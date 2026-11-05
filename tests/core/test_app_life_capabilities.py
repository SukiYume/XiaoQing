"""网上生活桥：契约只允许 xiaoqing_life 导出、xiaoqing_chat 调用，能力只在声明时出现。"""

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

SERVICES = ("life.turn_context", "life.export_memes")


def _service(name: str, callers: list[str] | None = None) -> dict:
    return {"name": name, "callback": name.split(".")[1], "callers": callers or ["xiaoqing_chat"]}


def _principal(app: XiaoQingApp, user_id: int = 1):
    return app.issue_user_principal(
        {"sender": {"user_id": user_id}}, user_id=user_id, group_id=None, is_private=True
    )


def _install_life(app: XiaoQingApp, **callbacks) -> None:
    module   = ModuleType("plugins.xiaoqing_life.main")
    services = []
    for name, callback in callbacks.items():
        setattr(module, callback.__name__, callback)
        services.append(
            PluginServiceDefinition(
                name     = name,
                callback = callback.__name__,
                callers  = frozenset({"xiaoqing_chat"}),
            )
        )
    definition = PluginDefinition(
        name        = "xiaoqing_life",
        version     = "1.0.0",
        entry       = "main.py",
        commands    = [],
        schedule    = [],
        concurrency = "parallel",
        services    = tuple(services),
    )
    _register_test_loaded_plugin(app, definition, module)


@pytest.mark.unit
def test_life_service_contracts_are_provider_and_caller_scoped() -> None:
    manifest = PluginManifest(
        name     = "xiaoqing_life",
        services = [_service(name) for name in SERVICES],
    )
    assert [s.name for s in manifest.services] == list(SERVICES)
    PluginManifest(name="xiaoqing_chat", uses_services=list(SERVICES))

    with pytest.raises(ValidationError):
        PluginManifest(name="other", services=[_service("life.turn_context")])
    with pytest.raises(ValidationError):
        PluginManifest(name="xiaoqing_life", services=[_service("life.turn_context", ["shell"])])
    with pytest.raises(ValidationError):
        PluginManifest(name="xiaoqing_agent", uses_services=["life.turn_context"])


@pytest.mark.unit
def test_life_capability_only_appears_when_declared(temp_app_root: Path) -> None:
    app = XiaoQingApp(temp_app_root)
    undeclared = _plugin_context_for(app, "xiaoqing_chat", user_id=1, principal=_principal(app))
    assert undeclared.capabilities.life_context is None
    declared = _plugin_context_for(
        app,
        "xiaoqing_chat",
        user_id       = 1,
        principal     = _principal(app),
        uses_services = frozenset({"life.turn_context"}),
    )
    assert declared.capabilities.life_context is not None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_life_turn_context_forwards_payload_and_validates_results(
    temp_app_root: Path,
) -> None:
    app           = XiaoQingApp(temp_app_root)
    seen   : list = []
    results: list = [{"life_lines": "x"}, None, "bad"]

    async def turn_context(payload, context):
        seen.append((payload, context.plugin_name))
        return results.pop(0)

    _install_life(app, **{"life.turn_context": turn_context})
    capability = _plugin_context_for(
        app,
        "xiaoqing_chat",
        user_id       = 1,
        principal     = _principal(app),
        uses_services = frozenset({"life.turn_context"}),
    ).capabilities.life_context
    assert await capability.turn_context({"text": "hi"}) == {"life_lines": "x"}
    assert seen[-1] == ({"text": "hi"}, "xiaoqing_life")
    assert await capability.turn_context({"text": "hi"}) is None
    with pytest.raises(TypeError):
        await capability.turn_context({"text": "hi"})
    with pytest.raises(TypeError):
        await capability.turn_context(["nope"])  # type: ignore[arg-type]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_life_export_memes_requires_declaration_and_forwards(temp_app_root: Path) -> None:
    app = XiaoQingApp(temp_app_root)

    async def export_memes(payload, _context):
        return {"memes": [], "revoked": [], "next_since": payload["since"]}

    async def turn_context(_payload, _context):
        return None

    _install_life(app, **{"life.turn_context": turn_context, "life.export_memes": export_memes})
    only_turn = _plugin_context_for(
        app,
        "xiaoqing_chat",
        user_id       = 1,
        principal     = _principal(app),
        uses_services = frozenset({"life.turn_context"}),
    ).capabilities.life_context
    with pytest.raises(RuntimeError):
        await only_turn.export_memes({"since": 0.0})

    both = _plugin_context_for(
        app,
        "xiaoqing_chat",
        user_id       = 1,
        principal     = _principal(app),
        uses_services = frozenset(SERVICES),
    ).capabilities.life_context
    assert await both.export_memes({"since": 3.0}) == {
        "memes"     : [],
        "revoked"   : [],
        "next_since": 3.0,
    }


@pytest.mark.asyncio
@pytest.mark.unit
async def test_life_service_reports_missing_provider_as_unavailable(temp_app_root: Path) -> None:
    app        = XiaoQingApp(temp_app_root)
    capability = _plugin_context_for(
        app,
        "xiaoqing_chat",
        user_id       = 1,
        principal     = _principal(app),
        uses_services = frozenset({"life.turn_context"}),
    ).capabilities.life_context
    with pytest.raises(RuntimeError, match="unavailable"):
        await capability.turn_context({"text": "hi"})
