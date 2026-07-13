from __future__ import annotations

import asyncio
import base64
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.xiaoqing_chat.generation_limiter import (
    GenerationLimiter,
    GenerationLimitExceeded,
)
from plugins.xiaoqing_chat.media.emoji_library import (
    approve_emoji_global,
    collect_emoji_candidate,
    delete_emoji_entry,
    load_emoji_library,
)
from plugins.xiaoqing_chat.media.event_media import (
    RenderedMedia,
    _prune_media_inbox,
    _validate_image_resource_limits,
    render_event_media,
)

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _runtime(**overrides):
    values = {
        "enable_inbound_media_context": True,
        "enable_auto_collect_inbound_emoji": True,
        "emoji_auto_collect_requires_approval": False,
        "emoji_auto_collect_max_entries": 10,
        "emoji_auto_collect_similarity_threshold": 0,
        "max_media_per_message": 1,
        "max_analyze_bytes": 1024 * 1024,
        "max_image_pixels": 1_000_000,
        "max_animation_frames": 10,
        "inbox_disk_quota_bytes": 1024 * 1024,
        "inbox_ttl_seconds": 3600.0,
    }
    values.update(overrides)
    return SimpleNamespace(cfg=SimpleNamespace(media=SimpleNamespace(**values)))


@pytest.mark.asyncio
async def test_auto_emoji_is_chat_scoped_until_admin_approval(tmp_path: Path):
    context = SimpleNamespace(data_dir=tmp_path)
    runtime = _runtime()
    source = tmp_path / "source.png"
    source.write_bytes(_PNG)
    rendered = RenderedMedia(
        media_hash=hashlib.sha256(_PNG).hexdigest(),
        kind="emoji",
        description="猫猫疑惑",
        emotion_tags=("疑惑",),
        marker="[表情包：猫猫疑惑]",
        cached_path=source,
    )

    result = collect_emoji_candidate(
        context,
        runtime,
        rendered,
        source_path=source,
        source_chat_id="g100",
        source_user_id="u1",
    )
    assert result is not None
    assert len(await load_emoji_library(context, runtime, chat_id="g100")) == 1
    assert await load_emoji_library(context, runtime, chat_id="g200") == []

    stored = (await load_emoji_library(context, runtime, chat_id="g100"))[0]
    assert approve_emoji_global(context, runtime, stored.media_hash, approved_by="admin")
    assert len(await load_emoji_library(context, runtime, chat_id="g200")) == 1
    assert delete_emoji_entry(context, runtime, stored.media_hash)
    assert await load_emoji_library(context, runtime, chat_id="g100") == []


@pytest.mark.asyncio
async def test_generation_limiter_applies_to_every_admitted_request():
    limiter = GenerationLimiter()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def first():
        async with limiter.admit(
            chat_id="g1",
            user_id="u1",
            max_global=1,
            max_per_chat=1,
            max_per_user=1,
            max_calls_per_user_per_day=2,
        ):
            entered.set()
            await release.wait()

    task = asyncio.create_task(first())
    await entered.wait()
    with pytest.raises(GenerationLimitExceeded, match="global_inflight"):
        async with limiter.admit(
            chat_id="g2",
            user_id="u2",
            max_global=1,
            max_per_chat=1,
            max_per_user=1,
            max_calls_per_user_per_day=2,
        ):
            pass
    release.set()
    await task


@pytest.mark.asyncio
async def test_media_count_and_resource_limits_are_hard(tmp_path: Path):
    runtime = _runtime(max_media_per_message=2)
    context = SimpleNamespace(data_dir=tmp_path, logger=SimpleNamespace(info=lambda *_a, **_k: None))
    event = {
        "message": [
            {"type": "face", "data": {"id": str(index), "raw": {"text": f"[f{index}]"}}}
            for index in range(5)
        ]
    }
    rendered = await render_event_media(event, context=context, runtime=runtime)
    assert len(rendered) == 2

    with pytest.raises(ValueError, match="pixel limit"):
        _validate_image_resource_limits(
            _PNG,
            width=2000,
            height=2000,
            max_pixels=1_000_000,
            max_frames=10,
        )

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "old.bin").write_bytes(b"x" * 8)
    _prune_media_inbox(inbox, quota_bytes=10, ttl_seconds=3600, incoming_bytes=8)
    assert not (inbox / "old.bin").exists()
