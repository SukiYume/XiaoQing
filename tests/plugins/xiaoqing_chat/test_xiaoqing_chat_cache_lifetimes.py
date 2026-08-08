import asyncio
import gc
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from plugins.xiaoqing_chat.media.event_media_common import RenderedMedia, ResolvedMedia
from plugins.xiaoqing_chat.runtime_state import ChatRuntimeState


def test_provider_overrides_have_a_hard_lru_capacity(monkeypatch):
    monkeypatch.setattr(ChatRuntimeState, "_MAX_PROVIDER_OVERRIDES", 2, raising=False)
    state = ChatRuntimeState()

    state.set_chat_provider("g1", "one")
    state.set_chat_provider("g2", "two")
    assert state.get_chat_provider("g1") == "one"
    state.set_chat_provider("g3", "three")

    assert state.get_chat_provider("g1") == "one"
    assert state.get_chat_provider("g2") is None
    assert state.get_chat_provider("g3") == "three"
    assert len(state._active_provider_by_chat) == 2


def test_stale_chat_cleanup_removes_provider_override(monkeypatch):
    monkeypatch.setattr(ChatRuntimeState, "_MAX_TRACKED_CHATS", 1)
    state = ChatRuntimeState()
    state.set_chat_provider("old", "one")
    state.set_chat_provider("recent", "two")
    state.set_last_observe_ts("old", 1.0)
    state.set_last_observe_ts("recent", 2.0)

    state.cleanup_stale_chats()

    assert state.get_chat_provider("old") is None
    assert state.get_chat_provider("recent") == "two"


def test_runtime_config_cache_has_a_hard_lru_capacity(monkeypatch):
    monkeypatch.setattr(ChatRuntimeState, "_MAX_RUNTIME_CACHES", 2, raising=False)
    state = ChatRuntimeState()

    state.set_runtime("one", SimpleNamespace(), 1, 10)
    state.set_runtime("two", SimpleNamespace(), 2, 20)
    assert state.get_runtime("one") is not None
    state.set_runtime("three", SimpleNamespace(), 3, 30)

    assert state.get_runtime("one") is not None
    assert state.get_runtime("two") is None
    assert state.get_runtime_mtime("two") is None
    assert state.get_runtime_revision("two") is None
    assert state.get_runtime("three") is not None


def test_emoji_library_cache_has_a_hard_lru_capacity(monkeypatch):
    from plugins.xiaoqing_chat.media import emoji_library

    monkeypatch.setattr(emoji_library, "_LIBRARY_CACHE_MAX_ENTRIES", 2)
    emoji_library._LIBRARY_CACHE.clear()
    try:
        emoji_library._store_library_cache("one", (1.0, 1.0), [])
        emoji_library._store_library_cache("two", (2.0, 2.0), [])
        assert emoji_library._get_library_cache("one", (1.0, 1.0)) == []
        emoji_library._store_library_cache("three", (3.0, 3.0), [])

        assert list(emoji_library._LIBRARY_CACHE) == ["one", "three"]
    finally:
        emoji_library._LIBRARY_CACHE.clear()


@pytest.mark.asyncio
async def test_media_render_singleflight_lock_is_released_after_use(tmp_path: Path):
    from plugins.xiaoqing_chat.media import event_media as event_media_module

    resolved = ResolvedMedia(
        media_hash="cache-lifetime",
        segment_type="image",
        source_name="fixture.png",
        mime_type="image/png",
        cached_path=tmp_path / "fixture.png",
    )
    rendered = RenderedMedia(
        media_hash=resolved.media_hash,
        kind="image",
        description="fixture",
        emotion_tags=(),
        marker="[图片：fixture]",
        cached_path=resolved.cached_path,
    )
    context = SimpleNamespace(data_dir=tmp_path)
    loop = asyncio.get_running_loop()
    lock_key = f"{tmp_path.resolve()}::{resolved.media_hash}"

    with patch.object(
        event_media_module,
        "_render_resolved_media_locked",
        new=AsyncMock(return_value=rendered),
    ):
        result = await event_media_module._render_resolved_media(
            resolved,
            context=context,
            runtime=SimpleNamespace(),
            prefer_emoji=False,
        )

    gc.collect()
    locks = event_media_module._MEDIA_RENDER_LOCKS_BY_LOOP.get(loop, {})
    assert result is rendered
    assert lock_key not in locks
