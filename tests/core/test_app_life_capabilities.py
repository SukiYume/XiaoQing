"""网上生活桥：契约只允许 xiaoqing_life 导出、xiaoqing_chat 调用，能力只在声明时出现。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

import tests.helpers.app_test_support as _fixture_support
from core.models import PluginManifest
from tests.helpers.app_test_support import (
    PROBE,
    XiaoQingApp,
    load_shipped_plugin,
    load_test_plugin,
    plugin_context_for,
    service_declaration,
    user_principal,
)

temp_app_root                               = _fixture_support.temp_app_root
_isolate_process_global_plugin_import_state = (
    _fixture_support._isolate_process_global_plugin_import_state
)

SERVICES = ("life.turn_context", "life.export_memes")

LIFE_SOURCE = """
from tests.helpers.plugin_probe import PROBE

async def turn_context(payload, context):
    PROBE.log("turn_context").append((payload, context.plugin_name))
    return PROBE.next_result("turn_context")

async def export_memes(payload, _context):
    return {"memes": [], "revoked": [], "next_since": payload["since"]}
"""


def _service(name: str, callers: list[str] | None = None) -> dict:
    return {"name": name, "callback": name.split(".")[1], "callers": callers or ["xiaoqing_chat"]}


async def _install_life(app: XiaoQingApp, *services: str) -> None:
    await load_test_plugin(
        app,
        "xiaoqing_life",
        LIFE_SOURCE,
        services=[
            service_declaration(name, name.split(".")[1], ("xiaoqing_chat",)) for name in services
        ],
    )


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
    app        = XiaoQingApp(temp_app_root)
    undeclared = plugin_context_for(
        app, "xiaoqing_chat", user_id=1, principal=user_principal(app, 1)
    )
    assert undeclared.capabilities.life_context is None
    declared = plugin_context_for(
        app,
        "xiaoqing_chat",
        user_id       = 1,
        principal     = user_principal(app, 1),
        uses_services = frozenset({"life.turn_context"}),
    )
    assert declared.capabilities.life_context is not None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_life_turn_context_forwards_payload_and_validates_results(
    temp_app_root: Path,
) -> None:
    app = XiaoQingApp(temp_app_root)
    PROBE.script("turn_context", {"life_lines": "x"}, None, "bad")
    await _install_life(app, "life.turn_context")
    capability = plugin_context_for(
        app,
        "xiaoqing_chat",
        user_id       = 1,
        principal     = user_principal(app, 1),
        uses_services = frozenset({"life.turn_context"}),
    ).capabilities.life_context
    assert await capability.turn_context({"text": "hi"}) == {"life_lines": "x"}
    assert PROBE.log("turn_context")[-1] == ({"text": "hi"}, "xiaoqing_life")
    assert await capability.turn_context({"text": "hi"}) is None
    with pytest.raises(TypeError):
        await capability.turn_context({"text": "hi"})
    with pytest.raises(TypeError):
        await capability.turn_context(["nope"])  # type: ignore[arg-type]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_life_export_memes_requires_declaration_and_forwards(temp_app_root: Path) -> None:
    app = XiaoQingApp(temp_app_root)
    await _install_life(app, *SERVICES)
    only_turn = plugin_context_for(
        app,
        "xiaoqing_chat",
        user_id       = 1,
        principal     = user_principal(app, 1),
        uses_services = frozenset({"life.turn_context"}),
    ).capabilities.life_context
    with pytest.raises(RuntimeError):
        await only_turn.export_memes({"since": 0.0})

    both = plugin_context_for(
        app,
        "xiaoqing_chat",
        user_id       = 1,
        principal     = user_principal(app, 1),
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
    capability = plugin_context_for(
        app,
        "xiaoqing_chat",
        user_id       = 1,
        principal     = user_principal(app, 1),
        uses_services = frozenset({"life.turn_context"}),
    ).capabilities.life_context
    with pytest.raises(RuntimeError, match="unavailable"):
        await capability.turn_context({"text": "hi"})


@pytest.mark.asyncio
async def test_scheduled_life_travel_uses_codex_queue_and_publishes_once(
    temp_app_root: Path, monkeypatch
):
    from PIL import Image

    from tests.helpers.codex_test_support import FakeRunner

    config                               = temp_app_root / "config/config.json"
    settings                             = json.loads(config.read_bytes())
    settings["plugins"]["xiaoqing_life"] = {"enabled": True}
    settings["plugins"]["codex"]         = {"travel_model": "postcard-test-model"}
    config.write_text(json.dumps(settings), encoding="utf8")
    app   = XiaoQingApp(temp_app_root)
    codex = (await load_shipped_plugin(app, "codex")).module
    life  = (await load_shipped_plugin(app, "xiaoqing_life")).module
    # The plugin loader creates a fresh module generation with its own result type.
    from plugins.codex.runner import CodexRunResult

    monkeypatch.setattr("tests.helpers.codex_test_support.CodexRunResult", CodexRunResult)
    principal = app.identity_service.issue(kind="scheduled_system", schedule_delivery="silent")
    ctx = app.plugin_manager.build_context("xiaoqing_life", None, None, "travel-test", principal)
    runner_context = app.plugin_manager.build_context("codex", None, None, "travel-test", principal)
    assert ctx.capabilities.travel_postcards and ctx.capabilities.codex_agent is None
    user = plugin_context_for(
        app,
        "xiaoqing_life",
        user_id       = 1,
        principal     = user_principal(app, 1),
        uses_services = frozenset({"codex.travel_postcards"}),
    )
    assert user.capabilities.travel_postcards is None
    manager     = await codex.get_manager(runner_context)
    real_runner = manager.runner
    draw        = FakeRunner(
        result_text   = '{"country":"日本","city":"松本","captions":["松本的傍晚"]}',
        artifact_name = "2026-11-18-1.png",
    )
    manager.runner = draw
    runtime        = life.get_runtime(ctx)
    runtime.clock = lambda _: datetime(2026, 11, 19, 3, tzinfo=UTC)
    try:
        # A failed generation leaves the date available to the next scheduled attempt.
        draw.exit_code = 1
        await life.scheduled_travel(ctx)
        await runtime.travel_task
        folder = ctx.data_dir / "travel/2026-11-18"
        assert not folder.exists()
        draw.exit_code = 0
        await life.scheduled_travel(ctx)
        task = runtime.travel_task
        await life.scheduled_travel(ctx)
        assert runtime.travel_task is task
        await task
        assert (
            json.loads((folder / "entry.json").read_bytes())["photos"][0]["caption"] == "松本的傍晚"
        )
        with Image.open(folder / "1.webp") as photo:
            assert photo.format == "WEBP"
        await life.scheduled_travel(ctx)
        await runtime.travel_task
        assert len(draw.calls) == 2
        label, prompt, _ = draw.calls[-1]
        assert label == "life-travel" and "灰紫短发" in prompt and "里斯本" in prompt
        history = manager.data_dir / "session/life-travel/conversation.jsonl"
        queued  = next(
            row
            for line in history.read_bytes().splitlines()
            if (row := json.loads(line)).get("status") == "queued"
        )
        references = queued["metadata"]["input_images"]
        assert len(references) == 3 and all(Path(p).is_file() for p in references)
        assert queued["metadata"]["source"] == "xiaoqing_life"
        assert queued["metadata"]["model"] == "postcard-test-model"
        for thread in (None, "resumed-thread"):
            args = real_runner._build_args(
                temp_app_root,
                thread,
                temp_app_root / "out",
                references,
                folder,
                queued["metadata"]["model"],
            )
            assert args[args.index("--model") + 1] == "postcard-test-model"
            assert [args[n + 1] for n, arg in enumerate(args) if arg == "--image"] == references
            assert args[args.index("--add-dir") + 1] == str(folder)
    finally:
        await life.shutdown(ctx)
        await codex.shutdown(runner_context)
