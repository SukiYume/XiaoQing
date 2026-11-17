import asyncio
import gc
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.interfaces import PluginCapabilities
from plugins.xiaoqing_chat.media import emoji_library
from plugins.xiaoqing_chat.runtime_state import SELF_MOOD_KEY, ChatRuntimeState
from tests.helpers.xiaoqing_chat_media_test_support import make_media_runtime, write_png


def test_provider_overrides_have_a_hard_lru_capacity():
    capacity = ChatRuntimeState._MAX_PROVIDER_OVERRIDES
    state    = ChatRuntimeState()

    for index in range(capacity):
        state.set_chat_provider(f"g{index}", f"p{index}")
    assert state.get_chat_provider("g0") == "p0"
    state.set_chat_provider("extra", "one-more")

    assert state.get_chat_provider("g0") == "p0"
    assert state.get_chat_provider("g1") is None
    assert state.get_chat_provider("extra") == "one-more"
    assert len(state._active_provider_by_chat) == capacity


def test_stale_chat_cleanup_removes_provider_override():
    capacity = ChatRuntimeState._MAX_TRACKED_CHATS
    state    = ChatRuntimeState()
    state.set_chat_provider("old", "one")
    state.set_chat_provider("recent", "two")
    state.set_last_observe_ts("old", 1.0)
    for index in range(capacity):
        state.set_last_observe_ts("recent" if index == 0 else f"busy{index}", 2.0 + index)

    state.set_mood_state(SELF_MOOD_KEY, "现在有点调皮")

    state.cleanup_stale_chats()

    assert state.get_chat_provider("old") is None
    assert state.get_chat_provider("recent") == "two"
    assert state.get_mood_state(SELF_MOOD_KEY) == "现在有点调皮", "她自己的心境不属于任何会话"


def test_runtime_config_cache_has_a_hard_lru_capacity():
    capacity = ChatRuntimeState._MAX_RUNTIME_CACHES
    state    = ChatRuntimeState()

    for index in range(capacity):
        state.set_runtime(f"k{index}", SimpleNamespace(), index, index * 10)
    assert state.get_runtime("k0") is not None
    state.set_runtime("extra", SimpleNamespace(), capacity, capacity * 10)

    assert state.get_runtime("k0") is not None
    assert state.get_runtime("k1") is None
    assert state.get_runtime_mtime("k1") is None
    assert state.get_runtime_revision("k1") is None
    assert state.get_runtime("extra") is not None


def _emoji_library_context(root: Path, name: str) -> SimpleNamespace:
    """数据目录下有一个表情库：一张图加一条可直接使用的已启用索引。"""

    context = SimpleNamespace(data_dir=root / name)
    library = context.data_dir / "media" / "library"
    digest  = hashlib.sha256(write_png(library / "a.png").read_bytes()).hexdigest()
    entry   = {
        "media_hash"  : digest,
        "file_path"   : "media/library/a.png",
        "description" : "一只小鸟",
        "emotion_tags": ["疑惑"],
        "usage_count" : 0,
        "last_used_ts": 0.0,
        "marker"      : "[表情包：疑惑]",
        "status"      : "active",
    }
    (library / "index.json").write_text(
        json.dumps({"entries": {digest: entry}}, ensure_ascii=False), encoding="utf-8"
    )
    return context


@pytest.mark.asyncio
async def test_emoji_library_cache_has_a_hard_lru_capacity(tmp_path: Path):
    capacity  = emoji_library._LIBRARY_CACHE_MAX_ENTRIES
    runtime   = make_media_runtime()
    libraries = [_emoji_library_context(tmp_path, f"lib{index}") for index in range(capacity + 1)]

    emoji_library._LIBRARY_CACHE.clear()
    try:
        for context in libraries[:capacity]:
            assert len(await emoji_library.load_emoji_library(context, runtime)) == 1
        # 再读第一个库：它成了最近使用的，下一个新库挤掉的是第二个。
        await emoji_library.load_emoji_library(libraries[0], runtime)
        await emoji_library.load_emoji_library(libraries[capacity], runtime)

        cached = list(emoji_library._LIBRARY_CACHE)
        assert len(cached) == capacity
        assert emoji_library._library_cache_key(libraries[0]) in cached
        assert emoji_library._library_cache_key(libraries[1]) not in cached
        assert emoji_library._library_cache_key(libraries[capacity]) in cached
    finally:
        emoji_library._LIBRARY_CACHE.clear()


@pytest.mark.asyncio
async def test_media_render_singleflight_lock_is_released_after_use(tmp_path: Path):
    from plugins.xiaoqing_chat.media import event_media as event_media_module

    image   = write_png(tmp_path / "fixture.png")
    context = SimpleNamespace(
        data_dir     = tmp_path / "data",
        capabilities = PluginCapabilities(),
    )
    loop = asyncio.get_running_loop()

    result = await event_media_module.render_local_media_file(
        image, context=context, runtime=make_media_runtime()
    )

    gc.collect()
    assert result is not None
    lock_key = f"{context.data_dir.resolve()}::{result.media_hash}"
    locks    = event_media_module._MEDIA_RENDER_LOCKS_BY_LOOP.get(loop, {})
    assert lock_key not in locks
