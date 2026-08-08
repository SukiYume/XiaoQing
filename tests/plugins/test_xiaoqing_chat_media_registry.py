"""媒体注册表和 QQ 表情目录。"""

from __future__ import annotations

import threading

import tests.helpers.xiaoqing_chat_media_test_support as _fixture_support
from tests.helpers.xiaoqing_chat_media_test_support import (
    _PNG_BYTES,
    AsyncMock,
    MagicMock,
    RenderedMedia,
    ResolvedMedia,
    _download_url_bytes,
    _make_media_runtime,
    _resolve_media_llm_secret_candidates,
    _write_png,
    asyncio,
    build_effective_user_text,
    compact_message_content,
    get_state,
    hashlib,
    json,
    load_qq_face_catalog,
    nullcontext,
    patch,
    pytest,
    render_event_media,
    render_event_media_text,
    render_local_media_file,
    resolve_message_content,
)

mock_context = _fixture_support.mock_context


@pytest.mark.asyncio
async def test_download_url_bytes_rejects_oversized_stream(mock_context):
    from core.safe_http import SafeHttpError

    with patch(
        "plugins.xiaoqing_chat.media.event_media.fetch_public_bytes",
        new=AsyncMock(side_effect=SafeHttpError("media too large")),
    ):
        with pytest.raises(ValueError, match="media too large"):
            await _download_url_bytes(
                "https://example.com/test.png",
                max_bytes=5,
            )


def test_media_candidates_use_registry_without_legacy_vision_selection(mock_context):
    secrets = _resolve_media_llm_secret_candidates(mock_context)[0]

    assert secrets["model"] == "glm-4.6v-flash"
    assert secrets["_vision_enabled"] is True


def test_media_registry_dedup_keeps_latest_better_description(tmp_path):
    state = get_state()
    state.media_store.bind(tmp_path)

    first = state.media_store.upsert_media_items(
        [
            {
                "kind": "emoji",
                "media_hash": "same-hash",
                "description": "一张表情包",
                "marker": "[表情包：一张表情包]",
            }
        ]
    )
    upgraded = state.media_store.upsert_media_items(
        [
            {
                "kind": "emoji",
                "media_hash": "same-hash",
                "description": "猫猫无语摊手",
                "emotion_tags": ["无语", "摊手"],
                "marker": "[表情包：猫猫无语摊手]",
            }
        ]
    )

    assert first[0]["media_key"] == upgraded[0]["media_key"]
    assert upgraded[0]["description"] == "猫猫无语摊手"
    assert upgraded[0]["marker"] == "[表情包：猫猫无语摊手]"


def test_registry_upgrade_preserves_cultural_hint():
    from plugins.xiaoqing_chat.media.event_media import _upgrade_rendered_media_from_registry

    original = RenderedMedia(
        media_hash="same-hash",
        kind="emoji",
        description="一张表情包",
        emotion_tags=(),
        marker="[表情包：一张表情包]",
        cultural_hint="常用于表示无语",
    )
    resolved = {
        "media_hash": "same-hash",
        "kind": "emoji",
        "description": "猫猫摊手",
        "emotion_tags": ["无语"],
        "marker": "[表情包：无语]",
    }

    with patch(
        "plugins.xiaoqing_chat.media.event_media.resolve_registered_media_items",
        return_value=[resolved],
    ):
        upgraded = _upgrade_rendered_media_from_registry([original])

    assert upgraded[0].description == "猫猫摊手"
    assert upgraded[0].cultural_hint == "常用于表示无语"


def test_media_registry_defers_disk_write_but_keeps_current_process_resolution(tmp_path):
    from plugins.xiaoqing_chat.media_registry import MediaRegistryStore

    store = MediaRegistryStore()
    store.bind(tmp_path)
    index_path = tmp_path / "media" / "index.json"

    store.upsert_media_items(
        [
            {
                "kind": "emoji",
                "media_hash": "same-hash",
                "description": "猫猫无语摊手",
                "emotion_tags": ["无语", "摊手"],
                "marker": "[表情包：猫猫无语摊手]",
            }
        ]
    )

    rebuilt = resolve_message_content(
        "[[xc_media_1]]",
        [
            {
                "kind": "emoji",
                "media_hash": "same-hash",
                "marker": "[表情包：一张表情包]",
            }
        ],
        store=store,
    )

    assert store.is_dirty() is True
    assert index_path.exists() is False
    assert "猫猫无语摊手" in rebuilt
    assert "一张表情包" not in rebuilt

    store.flush()

    saved = json.loads(index_path.read_text(encoding="utf-8"))
    assert saved["entries"]["media:same-hash"]["description"] == "猫猫无语摊手"
    assert store.is_dirty() is False


def test_media_registry_flush_keeps_dirty_when_save_fails(tmp_path):
    from plugins.xiaoqing_chat.media_registry import MediaRegistryStore

    store = MediaRegistryStore()
    store.bind(tmp_path)
    store.upsert_media_items(
        [
            {
                "kind": "image",
                "media_hash": "hash-fail",
                "description": "测试图片",
                "marker": "[图片：测试图片]",
            }
        ]
    )

    with patch.object(store, "_save_json", return_value=False):
        store.flush()

    assert store.is_dirty() is True


def test_media_registry_load_is_explicit_and_rehydrates_persisted_entries(tmp_path):
    from plugins.xiaoqing_chat.media_registry import MediaRegistryStore

    first = MediaRegistryStore()
    first.bind(tmp_path)
    first.upsert_media_items(
        [
            {
                "kind": "image",
                "media_hash": "persisted-hash",
                "description": "持久化图片",
                "marker": "[图片：持久化图片]",
            }
        ]
    )
    first.flush()
    reloaded = MediaRegistryStore()
    reloaded.bind(tmp_path)
    compact = [{"kind": "image", "media_hash": "persisted-hash"}]

    assert "description" not in reloaded.resolve_media_items(compact)[0]
    reloaded.load()

    assert reloaded.resolve_media_items(compact)[0]["description"] == "持久化图片"


def test_media_registry_flush_does_not_hold_memory_lock_during_io(tmp_path):
    from plugins.xiaoqing_chat.media_registry import MediaRegistryStore

    store = MediaRegistryStore()
    store.bind(tmp_path)
    store.upsert_media_items([{"kind": "image", "media_hash": "hash-1", "description": "first"}])
    resolved = threading.Event()

    def save_while_resolving(_path, _payload):
        worker = threading.Thread(
            target=lambda: (
                store.resolve_media_items([{"kind": "image", "media_hash": "hash-1"}]),
                resolved.set(),
            )
        )
        worker.start()
        assert resolved.wait(timeout=1.0)
        worker.join(timeout=1.0)
        return True

    with patch.object(store, "_save_json", side_effect=save_while_resolving):
        store.flush()

    assert store.is_dirty() is False


def test_media_registry_keeps_updates_arriving_during_flush(tmp_path):
    from plugins.xiaoqing_chat.media_registry import MediaRegistryStore

    store = MediaRegistryStore()
    store.bind(tmp_path)
    store.upsert_media_items([{"kind": "image", "media_hash": "hash-1", "description": "first"}])
    entered_save = threading.Event()
    release_save = threading.Event()
    real_save = store._save_json
    first_call = True

    def blocking_save(path, payload):
        nonlocal first_call
        if first_call:
            first_call = False
            entered_save.set()
            assert release_save.wait(timeout=2.0)
        return real_save(path, payload)

    with patch.object(store, "_save_json", side_effect=blocking_save):
        flush_thread = threading.Thread(target=store.flush)
        flush_thread.start()
        assert entered_save.wait(timeout=2.0)
        store.upsert_media_items(
            [{"kind": "image", "media_hash": "hash-2", "description": "second"}]
        )
        release_save.set()
        flush_thread.join(timeout=2.0)
        assert not flush_thread.is_alive()
        assert store.is_dirty() is True
        store.flush()

    saved = json.loads((tmp_path / "media" / "index.json").read_text(encoding="utf-8"))
    assert set(saved["entries"]) == {"media:hash-1", "media:hash-2"}
    assert store.is_dirty() is False


@pytest.mark.asyncio
async def test_plugin_init_loads_media_registry_off_event_loop(mock_context):
    from plugins.xiaoqing_chat import main as xiaoqing_main

    state = get_state()
    event_loop_thread = threading.get_ident()
    load_threads = []

    with patch.object(
        state.media_store,
        "load",
        side_effect=lambda: load_threads.append(threading.get_ident()),
    ):
        await xiaoqing_main.init(mock_context)

    assert len(load_threads) == 1
    assert load_threads[0] != event_loop_thread


def test_media_registry_compacts_and_rehydrates_message_content() -> None:
    media_items = [
        {
            "kind": "emoji",
            "media_hash": "hash-1",
            "marker": "[表情包：一张表情包]",
            "description": "猫猫翻白眼",
            "emotion_tags": ["无语"],
        },
        {
            "kind": "qq_face",
            "face_id": "14",
            "marker": "[QQ表情：微笑]",
            "label": "微笑",
        },
    ]

    compacted = compact_message_content("懂了\n[表情包：一张表情包]\n[QQ表情：微笑]", media_items)
    rebuilt = resolve_message_content(compacted, media_items, store=None)

    assert compacted == "懂了\n[[xc_media_1]]\n[[xc_media_2]]"
    assert rebuilt == "懂了\n[表情包：无语；内容：猫猫翻白眼]\n[QQ表情：微笑]"


def test_media_registry_emoji_marker_surfaces_visible_text_in_quotes() -> None:
    media_items = [
        {
            "kind": "emoji",
            "media_hash": "hash-vt",
            "marker": "[表情包：疑惑，调侃]",
            "description": "一个猫耳动漫女孩露出惊讶表情，配文字表达疑惑或调侃。，文字内容是“那咋整啊”",
            "emotion_tags": ["疑惑", "调侃"],
        }
    ]

    compacted = compact_message_content("[表情包：疑惑，调侃]", media_items)
    rebuilt = resolve_message_content(compacted, media_items, store=None)

    assert rebuilt == "[表情包：疑惑，调侃；写着“那咋整啊”]"


@pytest.mark.asyncio
async def test_render_event_media_falls_back_to_summary_marker_when_image_resolve_fails(
    mock_context,
):
    runtime = _make_media_runtime()
    event = {
        "message": [
            {
                "type": "image",
                "data": {"summary": "猫猫在发呆"},
            }
        ]
    }

    with patch(
        "plugins.xiaoqing_chat.media.event_media._resolve_segment_media",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    ):
        rendered = await render_event_media(event, context=mock_context, runtime=runtime)

    assert len(rendered) == 1
    assert rendered[0].marker in {"[图片：猫猫在发呆]", "[表情包：猫猫在发呆]"}


@pytest.mark.asyncio
async def test_render_event_media_does_not_treat_failed_image_filename_as_visual_summary(
    mock_context,
):
    runtime = _make_media_runtime()
    event = {
        "message": [
            {
                "type": "image",
                "data": {
                    "url": "https://example.com/unavailable.png",
                    "name": "test-placeholder.png",
                },
            }
        ]
    }

    with patch(
        "plugins.xiaoqing_chat.media.event_media._resolve_segment_media",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    ):
        rendered = await render_event_media(event, context=mock_context, runtime=runtime)

    assert len(rendered) == 1
    assert rendered[0].marker == "[图片：图片内容读取失败]"


@pytest.mark.asyncio
async def test_render_event_media_keeps_all_items_for_current_turn_context(mock_context):
    runtime = _make_media_runtime(max_media_per_message=3)
    event = {
        "message": [
            {"type": "face", "data": {"id": "1", "raw": {"text": "[微笑]"}}},
            {"type": "face", "data": {"id": "2", "raw": {"text": "[大哭]"}}},
            {"type": "face", "data": {"id": "3", "raw": {"text": "[狗头]"}}},
            {"type": "face", "data": {"id": "4", "raw": {"text": "[调皮]"}}},
        ]
    }

    rendered = await render_event_media(event, context=mock_context, runtime=runtime)
    text = await build_effective_user_text("收到", event, context=mock_context, runtime=runtime)

    assert len(rendered) == 3
    assert [item.marker for item in rendered] == [
        "[QQ表情：微笑]",
        "[QQ表情：大哭]",
        "[QQ表情：狗头]",
    ]
    assert text.count("[QQ表情：") == 3


@pytest.mark.asyncio
async def test_build_effective_user_text_reuses_upgraded_registry_marker(mock_context):
    runtime = _make_media_runtime()
    state = get_state()
    state.media_store.bind(mock_context.data_dir)
    state.media_store.upsert_media_items(
        [
            {
                "kind": "emoji",
                "media_hash": "known-hash",
                "description": "猫猫翻白眼",
                "emotion_tags": ["无语"],
                "marker": "[表情包：猫猫翻白眼]",
            }
        ]
    )
    event = {
        "message": [{"type": "image", "data": {"summary": "旧图"}}],
    }

    with (
        patch(
            "plugins.xiaoqing_chat.media.event_media._resolve_segment_media",
            new=AsyncMock(
                return_value=ResolvedMedia(
                    media_hash="known-hash",
                    segment_type="image",
                    source_name="old",
                    mime_type="image/png",
                    cached_path=mock_context.data_dir / "known.png",
                    width=0,
                    height=0,
                    is_animated=False,
                )
            ),
        ),
        patch(
            "plugins.xiaoqing_chat.media.event_media._render_resolved_media",
            new=AsyncMock(
                return_value=RenderedMedia(
                    media_hash="known-hash",
                    kind="emoji",
                    description="一张表情包",
                    emotion_tags=(),
                    marker="[表情包：一张表情包]",
                    cached_path=mock_context.data_dir / "known.png",
                )
            ),
        ),
    ):
        text = await build_effective_user_text("", event, context=mock_context, runtime=runtime)

    assert "猫猫翻白眼" in text
    assert "一张表情包" not in text


@pytest.mark.asyncio
async def test_render_resolved_media_writes_cache_once_on_cache_miss(mock_context):
    from plugins.xiaoqing_chat.media.event_media import _render_resolved_media

    runtime = _make_media_runtime()
    cached_path = _write_png(mock_context.data_dir / "cache-miss.png")
    resolved = ResolvedMedia(
        media_hash="hash-cache-miss",
        segment_type="image",
        source_name="cache-miss",
        mime_type="image/png",
        cached_path=cached_path,
        width=32,
        height=32,
        is_animated=False,
    )
    rendered = RenderedMedia(
        media_hash="hash-cache-miss",
        kind="image",
        description="一只猫在打哈欠",
        emotion_tags=(),
        marker="[图片：一只猫在打哈欠]",
        cached_path=cached_path,
    )
    first_cache = {"items": {}}
    second_cache = {"items": {}}

    with (
        patch(
            "plugins.xiaoqing_chat.media.event_media._render_cache_lock", return_value=nullcontext()
        ),
        patch(
            "plugins.xiaoqing_chat.media.event_media._load_render_cache",
            return_value=first_cache,
        ) as mock_load,
        patch(
            "plugins.xiaoqing_chat.media.event_media_common._render_cache_lock",
            return_value=nullcontext(),
        ),
        patch(
            "plugins.xiaoqing_chat.media.event_media_common._load_render_cache",
            return_value=second_cache,
        ) as mock_write_load,
        patch("plugins.xiaoqing_chat.media.event_media_common._save_render_cache") as mock_save,
        patch(
            "plugins.xiaoqing_chat.media.event_media._analyze_media_with_llm",
            new=AsyncMock(return_value=rendered),
        ),
    ):
        result = await _render_resolved_media(
            resolved,
            context=mock_context,
            runtime=runtime,
            prefer_emoji=False,
            summary_hint="猫",
        )

    assert result == rendered
    assert first_cache["items"] == {}
    assert second_cache["items"]["hash-cache-miss"]["marker"] == "[图片：一只猫在打哈欠]"
    assert mock_load.call_count == 1
    assert mock_write_load.call_count == 1
    assert mock_save.call_count == 1


@pytest.mark.asyncio
async def test_load_qq_face_catalog_merges_builtin_and_observed_labels(mock_context):
    runtime = _make_media_runtime()
    event = {
        "message": [
            {
                "type": "face",
                "data": {
                    "id": "1",
                    "raw": {"text": "狂笑"},
                },
            }
        ]
    }

    text = await render_event_media_text(event, context=mock_context, runtime=runtime)
    entries = await load_qq_face_catalog(mock_context)

    assert text == "[QQ表情：狂笑]"
    labels = {entry.label for entry in entries}
    assert "微笑" in labels
    assert "狂笑" in labels


@pytest.mark.asyncio
async def test_load_qq_face_catalog_loads_bundled_qface_labels(mock_context):
    entries = await load_qq_face_catalog(mock_context)
    target = next(entry for entry in entries if entry.face_id == "300")

    assert target.label == "胖三斤"
    assert "胖三斤" in target.aliases


@pytest.mark.asyncio
async def test_load_qq_face_catalog_keeps_placeholder_for_unlabeled_face(mock_context):
    catalog_path = mock_context.data_dir / "media" / "qq_face_catalog.json"
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    catalog_path.write_text(
        json.dumps(
            {
                "entries": {
                    "999": {
                        "labels": ["id=999"],
                        "usage_count": 0,
                        "last_used_ts": 0.0,
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    entries = await load_qq_face_catalog(mock_context)
    placeholder = next(entry for entry in entries if entry.face_id == "999")
    persisted = json.loads(catalog_path.read_text(encoding="utf-8"))

    assert placeholder.label == "系统表情#999"
    assert "系统表情#999" in persisted["entries"]["999"]["labels"]


@pytest.mark.asyncio
async def test_load_qq_face_catalog_repairs_invalid_usage_metadata(mock_context):
    catalog_path = mock_context.data_dir / "media" / "qq_face_catalog.json"
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    catalog_path.write_text(
        json.dumps(
            {
                "entries": {
                    "999": {
                        "labels": ["自定义表情"],
                        "usage_count": "broken",
                        "last_used_ts": float("inf"),
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    entries = await load_qq_face_catalog(mock_context)
    repaired = next(entry for entry in entries if entry.face_id == "999")

    assert repaired.usage_count == 0
    assert repaired.last_used_ts == 0.0


@pytest.mark.asyncio
async def test_render_local_media_file_merges_latest_cache_before_save(mock_context):
    runtime = _make_media_runtime()
    image_path = _write_png(mock_context.data_dir / "merge_cache_probe.png")
    media_hash = hashlib.sha256(_PNG_BYTES).hexdigest()
    cache_path = mock_context.data_dir / "media" / "render_cache.json"

    other_hash = "other-media-hash"
    other_entry = {
        "kind": "emoji",
        "description": "另一条缓存",
        "emotion_tags": ["无语"],
        "marker": "[表情包：另一条缓存]",
        "analysis_source": "llm",
        "analysis_quality": "detailed",
        "analysis_prompt_version": 2,
    }

    async def _fake_analyze(resolved, *, context, runtime, prefer_emoji):
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps({"items": {other_hash: other_entry}}, ensure_ascii=False),
            encoding="utf-8",
        )
        return RenderedMedia(
            media_hash=resolved.media_hash,
            kind="image",
            description="新的图片描述",
            emotion_tags=(),
            marker="[图片：新的图片描述]",
            cached_path=resolved.cached_path,
        )

    with patch(
        "plugins.xiaoqing_chat.media.event_media._analyze_media_with_llm",
        new=AsyncMock(side_effect=_fake_analyze),
    ):
        rendered = await render_local_media_file(image_path, context=mock_context, runtime=runtime)

    assert rendered is not None
    saved = json.loads(cache_path.read_text(encoding="utf-8"))
    assert other_hash in saved["items"]
    assert saved["items"][other_hash]["marker"] == other_entry["marker"]
    assert media_hash in saved["items"]
    assert saved["items"][media_hash]["marker"] == "[图片：新的图片描述]"


@pytest.mark.asyncio
async def test_observe_message_records_image_only_marker(mock_context):
    from plugins.xiaoqing_chat.handlers import observe_message

    runtime = _make_media_runtime()
    state = MagicMock()
    event = {
        "user_id": 12345,
        "group_id": 67890,
        "message": [{"type": "image", "data": {"file": "file:///tmp/test.png"}}],
    }

    with (
        patch("plugins.xiaoqing_chat.handlers._load_runtime", return_value=runtime),
        patch("plugins.xiaoqing_chat.handlers._get_bound_state", return_value=state),
        patch("plugins.xiaoqing_chat.handlers._get_lock", return_value=asyncio.Lock()),
        patch(
            "plugins.xiaoqing_chat.handlers.build_effective_user_text",
            new=AsyncMock(return_value="[图片：猫猫在发呆]"),
        ),
        patch("plugins.xiaoqing_chat.handlers._should_ignore_text", return_value=False),
        patch(
            "plugins.xiaoqing_chat.handlers._ensure_user_message_recorded",
            new=AsyncMock(return_value="m1"),
        ) as mock_record,
    ):
        result = await observe_message("", event, mock_context)

    assert result == []
    mock_record.assert_awaited_once()
    assert mock_record.await_args.args[0] == "[图片：猫猫在发呆]"
