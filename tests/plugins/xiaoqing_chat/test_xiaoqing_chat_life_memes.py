"""梗图导入表情库：幂等、全局可见、只能按来源删除、出现在可发素材里并能解析；同步游标与定时任务。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from PIL import Image

import tests.helpers.xiaoqing_chat_media_test_support as _fixture_support
from plugins.xiaoqing_chat import main as chat_main
from plugins.xiaoqing_chat.life.online import sync_life_memes
from plugins.xiaoqing_chat.media import emoji_library
from plugins.xiaoqing_chat.media.emoji_library import (
    import_external_emoji,
    load_emoji_library,
    remove_external_emoji,
)
from plugins.xiaoqing_chat.media.marker_resolver import (
    build_media_candidates_block,
    parse_marker,
    resolve_marker,
)
from plugins.xiaoqing_chat.media.markers import build_marker
from tests.helpers.xiaoqing_chat_media_test_support import _make_media_runtime, _write_png

mock_context = _fixture_support.mock_context


def _png(color: int) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (16, 16), (color, 40, 90)).save(buffer, format="PNG")
    return buffer.getvalue()


def _import(
    context: Any,
    image: bytes,
    label: str = "点赞认可",
    tags       = ("认可", "开心"),
    **kw: Any,
) -> bool:
    sha = hashlib.sha256(image).hexdigest()
    return import_external_emoji(
        context,
        image_bytes  = image,
        suffix       = ".png",
        description  = label,
        emotion_tags = tuple(tags),
        marker       = build_marker("emoji", label, tuple(tags)),
        source       = kw.pop("source", f"life:{sha}"),
    )


def _index(context: Any) -> dict[str, Any]:
    path = context.data_dir / "media" / "library" / "index.json"
    return json.loads(path.read_text(encoding="utf-8"))["entries"]


@pytest.mark.asyncio
async def test_imported_meme_is_global_active_listed_and_resolvable(mock_context) -> None:
    runtime, image = _make_media_runtime(), _png(1)
    sha = hashlib.sha256(image).hexdigest()
    assert _import(mock_context, image) is True
    record = _index(mock_context)[sha]
    assert (
        record["status"],
        record["source"],
        record["visibility"],
        record["global_approved"],
    ) == (
        "active",
        f"life:{sha}",
        "global",
        True,
    )
    assert (mock_context.data_dir / record["file_path"]).read_bytes() == image

    # 任何群都能看到（全局），不在来源会话里也一样
    entries = await load_emoji_library(mock_context, runtime, chat_id="some-other-chat")
    assert [e.media_hash for e in entries] == [sha] and entries[0].visibility == "global"
    block = await build_media_candidates_block(mock_context, runtime, chat_id="another-chat")
    assert "点赞认可" in block
    parsed = parse_marker("太棒了 [想发表情:点赞认可]")
    assert parsed is not None
    resolved = await resolve_marker(
        parsed, context=mock_context, runtime=runtime, chat_id="another-chat"
    )
    assert resolved is not None and resolved.kind == "emoji" and resolved.entry.media_hash == sha
    # 读取并修复索引后来源和全局标记仍然保留
    assert (
        _index(mock_context)[sha]["source"] == f"life:{sha}"
        and _index(mock_context)[sha]["global_approved"] is True
    )


def test_import_is_idempotent_and_never_overrides_other_sources(mock_context) -> None:
    image = _png(2)
    assert _import(mock_context, image) is True
    before = _index(mock_context)
    assert _import(mock_context, image) is False
    assert _index(mock_context) == before
    # 内容相同但已有手动条目：不覆盖
    manual_image = _png(3)
    msha         = hashlib.sha256(manual_image).hexdigest()
    path         = _write_png(mock_context.data_dir / "media" / "library" / "manual.png")
    path.write_bytes(manual_image)
    index_path = path.parent / "index.json"
    entries = json.loads(index_path.read_text(encoding="utf-8"))["entries"]
    entries[msha] = {
        "file_path"      : "media/library/manual.png",
        "description"    : "手动表情",
        "emotion_tags"   : ["手动"],
        "marker"         : "[表情包：手动]",
        "status"         : "active",
        "source"         : "manual",
        "visibility"     : "chat",
        "global_approved": False,
    }
    index_path.write_text(json.dumps({"entries": entries}), encoding="utf-8")
    assert _import(mock_context, manual_image, label="覆盖尝试") is False
    assert (
        _index(mock_context)[msha]["source"] == "manual"
        and _index(mock_context)[msha]["description"] == "手动表情"
    )


def test_import_rejects_mismatched_sources_and_empty_metadata(mock_context) -> None:
    image = _png(4)
    with pytest.raises(ValueError):
        _import(mock_context, image, source="life:" + "0" * 64)
    with pytest.raises(ValueError):
        _import(mock_context, image, source="manual")
    with pytest.raises(ValueError):
        import_external_emoji(
            mock_context,
            image_bytes  = image,
            suffix       = ".png",
            description  = "",
            emotion_tags = (),
            marker       = "",
            source       = f"life:{hashlib.sha256(image).hexdigest()}",
        )
    assert (
        not (mock_context.data_dir / "media" / "library").exists()
        or _index_entries(mock_context) == {}
    )


def _index_entries(context: Any) -> dict[str, Any]:
    path = context.data_dir / "media" / "library" / "index.json"
    return json.loads(path.read_text(encoding="utf-8"))["entries"] if path.exists() else {}


def test_remove_only_deletes_life_sourced_entries(mock_context) -> None:
    image = _png(5)
    sha   = hashlib.sha256(image).hexdigest()
    _import(mock_context, image)
    path = mock_context.data_dir / _index(mock_context)[sha]["file_path"]
    assert remove_external_emoji(mock_context, sha) is True
    assert sha not in _index(mock_context) and not path.exists()
    assert remove_external_emoji(mock_context, sha) is False  # 幂等
    assert remove_external_emoji(mock_context, "not-a-hash") is False

    keep          = _write_png(mock_context.data_dir / "media" / "library" / "own.png")
    ksha          = hashlib.sha256(keep.read_bytes()).hexdigest()
    entries       = _index(mock_context)
    entries[ksha] = {
        "file_path"   : "media/library/own.png",
        "description" : "自己收的",
        "emotion_tags": ["开心"],
        "marker"      : "[表情包：开心]",
        "status"      : "active",
        "source"      : "auto",
    }
    (keep.parent / "index.json").write_text(json.dumps({"entries": entries}), encoding="utf-8")
    assert remove_external_emoji(mock_context, ksha) is False and keep.exists()
    assert remove_external_emoji(mock_context, ksha, source_prefix="auto") is True


def _page(
    images: list[bytes], *, revoked: list[str] | None = None, next_since: float = 0.0
) -> dict[str, Any]:
    return {
        "memes": [
            {
                "sha256"      : hashlib.sha256(img).hexdigest(),
                "label"       : f"名字{n}",
                "description" : "d",
                "visible_text": "",
                "emotion_tags": ["开心"],
                "usage"       : "u",
                "mime"        : "image/png",
                "data_b64"    : base64.b64encode(img).decode(),
                "approved_at" : float(n + 1),
            }
            for n, img in enumerate(images)
        ],
        "revoked"   : revoked or [],
        "next_since": next_since,
    }


class FakeLife:
    def __init__(self, *pages: Any) -> None:
        self.pages, self.requests = list(pages), []

    async def export_memes(self, payload: dict[str, Any]) -> Any:
        self.requests.append(payload)
        page = self.pages.pop(0)
        if isinstance(page, Exception):
            raise page
        return page


def _context_with(mock_context: Any, life: Any) -> Any:
    mock_context.capabilities = SimpleNamespace(life_context=life)
    return mock_context


@pytest.mark.asyncio
async def test_sync_imports_pages_deletes_revoked_and_advances_the_cursor(mock_context) -> None:
    runtime = _make_media_runtime()
    old     = _png(20)
    _import(mock_context, old)
    new = [_png(21), _png(22)]
    life = FakeLife(_page(new, revoked=[hashlib.sha256(old).hexdigest(), "zz"], next_since=2.0))
    context = _context_with(mock_context, life)
    assert await sync_life_memes(context, runtime) == 3  # 导入 2 + 删除 1
    assert life.requests == [{"since": 0.0, "limit": 10}]
    assert set(_index(context)) == {hashlib.sha256(i).hexdigest() for i in new}
    cursor = json.loads(
        (context.data_dir / "life" / "life_memes_cursor.json").read_text(encoding="utf-8")
    )
    assert cursor == {"since": 2.0}

    again = FakeLife(_page([], next_since=2.0))
    assert await sync_life_memes(_context_with(mock_context, again), runtime) == 0
    assert again.requests[0]["since"] == 2.0  # 从游标继续


@pytest.mark.asyncio
async def test_sync_fetches_further_pages_while_full_and_ignores_bad_items(mock_context) -> None:
    runtime = _make_media_runtime()
    full = _page([_png(30 + n) for n in range(10)], next_since=10.0)
    full["memes"].append({"sha256": "x"})  # 非法条目被忽略，不影响整页
    last = _page([_png(60)], next_since=11.0)
    tampered = _page([_png(61)], next_since=11.0)
    tampered["memes"][0]["data_b64"] = base64.b64encode(b"other bytes").decode()
    life                             = FakeLife(full, last)
    context                          = _context_with(mock_context, life)
    assert await sync_life_memes(context, runtime) == 11
    assert [r["since"] for r in life.requests] == [0.0, 10.0]
    bad = _context_with(mock_context, FakeLife(tampered))
    assert await sync_life_memes(bad, runtime) == 0  # sha 对不上的不导入


@pytest.mark.asyncio
async def test_sync_is_silent_when_disabled_missing_or_failing(mock_context) -> None:
    runtime                            = _make_media_runtime()
    runtime.cfg.life.enable_life_memes = False
    life                               = FakeLife(_page([_png(70)]))
    assert (
        await sync_life_memes(_context_with(mock_context, life), runtime) == 0
        and life.requests == []
    )
    runtime.cfg.life.enable_life_memes = True
    assert await sync_life_memes(_context_with(mock_context, None), runtime) == 0
    failing = FakeLife(RuntimeError("SECRET_TEXT"))
    context = _context_with(mock_context, failing)
    assert await sync_life_memes(context, runtime) == 0
    logged = " ".join(str(c) for c in context.logger.info.call_args_list)
    assert "life.memes.sync_error" in logged and "SECRET_TEXT" not in logged
    assert await sync_life_memes(_context_with(mock_context, FakeLife(None)), runtime) == 0


@pytest.mark.asyncio
async def test_scheduled_task_respects_the_switch_and_swallows_errors(
    mock_context, monkeypatch
) -> None:
    runtime          = _make_media_runtime()
    calls: list[Any] = []

    async def fake_sync(context: Any, rt: Any) -> int:
        calls.append(rt)
        return 0

    monkeypatch.setattr(chat_main, "sync_life_memes", fake_sync)
    monkeypatch.setattr(chat_main, "load_runtime", lambda _c: runtime)
    runtime.cfg.life.enable_life_memes = False
    await chat_main.scheduled_life_memes(mock_context)
    assert calls == []
    runtime.cfg.life.enable_life_memes = True
    await chat_main.scheduled_life_memes(mock_context)
    assert calls == [runtime]
    monkeypatch.setattr(chat_main, "sync_life_memes", AsyncMock(side_effect=RuntimeError("x")))
    await asyncio.wait_for(chat_main.scheduled_life_memes(mock_context), timeout=1)


def test_manifest_schedules_the_meme_sync() -> None:
    from core.models import PluginManifest
    from tests.helpers.paths import REPOSITORY_ROOT

    raw = json.loads(
        (REPOSITORY_ROOT / "plugins/xiaoqing_chat/plugin.json").read_text(encoding="utf-8")
    )
    schedule = {
        item["id"]: item for item in PluginManifest.model_validate(raw).model_dump()["schedule"]
    }
    sync = schedule["xiaoqing_chat.life_memes"]
    assert (
        sync["handler"] == "scheduled_life_memes"
        and sync["delivery"] == "silent"
        and sync["cron"] == {"minute": "7,37"}
    )
    assert callable(chat_main.scheduled_life_memes) and emoji_library.import_external_emoji
