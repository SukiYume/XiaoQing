"""表情库采集、修复和模型选择。"""

from __future__ import annotations

import tests.helpers.xiaoqing_chat_media_test_support as _fixture_support
from tests.helpers.xiaoqing_chat_media_test_support import (
    _PNG_BYTES,
    AsyncMock,
    Path,
    PluginCapabilities,
    RenderedMedia,
    SimpleNamespace,
    _download_url_bytes,
    _looks_like_structured_media_text,
    _make_media_runtime,
    _media_llm_max_tokens,
    _raw_media_response,
    _resolve_media_llm_secret_candidates,
    _write_png,
    collect_emoji_candidate,
    hashlib,
    json,
    load_emoji_library,
    patch,
    pytest,
    render_event_media_text,
)

mock_context = _fixture_support.mock_context


@pytest.mark.asyncio
async def test_load_emoji_library_rebuilds_bad_existing_metadata(mock_context):
    library_dir = mock_context.data_dir / "media" / "library"
    library_dir.mkdir(parents=True, exist_ok=True)
    image_path = _write_png(library_dir / "坏条目.png")
    media_hash = hashlib.sha256(image_path.read_bytes()).hexdigest()
    runtime    = _make_media_runtime()
    (library_dir / "index.json").write_text(
        json.dumps(
            {
                "entries": {
                    media_hash: {
                        "media_hash": media_hash,
                        "file_path": "media/library/坏条目.png",
                        "description": 'json\n{"kind":"emoji","description":"坏输出"',
                        "emotion_tags": ["json", "kind"],
                        "usage_count": 0,
                        "last_used_ts": 0.0,
                        "marker": "[表情包：json，kind]",
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    async def _fake_render(file_path, *, context, runtime, prefer_emoji):
        file_path = Path(file_path)
        return RenderedMedia(
            media_hash   = media_hash,
            kind         = "emoji",
            description  = "一只黑色小鸟站着，眼神疑惑",
            emotion_tags = ("疑惑", "震惊"),
            marker       = "[表情包：疑惑，震惊]",
            cached_path  = file_path,
        )

    with patch(
        "plugins.xiaoqing_chat.media.event_media.render_local_media_file",
        new=AsyncMock(side_effect=_fake_render),
    ) as mock_render:
        entries = await load_emoji_library(mock_context, runtime)

    assert entries == []
    mock_render.assert_awaited_once()
    saved = json.loads((library_dir / "index.json").read_text(encoding="utf-8"))
    assert saved["entries"][media_hash]["marker"] == "[表情包：疑惑，震惊]"
    assert saved["entries"][media_hash]["status"] == "pending"
    assert saved["entries"][media_hash]["global_approved"] is False


@pytest.mark.asyncio
async def test_load_emoji_library_rejects_invalid_indexed_image(mock_context):
    library_dir = mock_context.data_dir / "media" / "library"
    library_dir.mkdir(parents=True, exist_ok=True)
    image_path = library_dir / "损坏.png"
    image_path.write_bytes(b"not an image")
    media_hash = hashlib.sha256(image_path.read_bytes()).hexdigest()
    (library_dir / "index.json").write_text(
        json.dumps(
            {
                "entries": {
                    media_hash: {
                        "media_hash": media_hash,
                        "file_path": "media/library/损坏.png",
                        "description": "看起来正常的旧索引",
                        "emotion_tags": ["旧"],
                        "usage_count": 0,
                        "last_used_ts": 0.0,
                        "marker": "[表情包：看起来正常的旧索引]",
                        "status": "active",
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    entries = await load_emoji_library(mock_context, _make_media_runtime())

    assert entries == []
    assert json.loads((library_dir / "index.json").read_text(encoding="utf-8"))["entries"] == {}


@pytest.mark.asyncio
async def test_load_emoji_library_schedules_background_repair_without_blocking(mock_context):
    library_dir = mock_context.data_dir / "media" / "library"
    library_dir.mkdir(parents=True, exist_ok=True)
    image_path = _write_png(library_dir / "坏条目.png")
    media_hash = hashlib.sha256(image_path.read_bytes()).hexdigest()
    runtime    = _make_media_runtime()
    (library_dir / "index.json").write_text(
        json.dumps(
            {
                "entries": {
                    media_hash: {
                        "media_hash": media_hash,
                        "file_path": "media/library/坏条目.png",
                        "description": 'json\n{"kind":"emoji","description":"坏输出"',
                        "emotion_tags": ["json", "kind"],
                        "usage_count": 0,
                        "last_used_ts": 0.0,
                        "marker": "[表情包：json，kind]",
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with (
        patch(
            "plugins.xiaoqing_chat.media.event_media.render_local_media_file",
            new=AsyncMock(side_effect=AssertionError("should not repair inline")),
        ) as mock_render,
        patch(
            "plugins.xiaoqing_chat.media.emoji_library.schedule_emoji_library_repair",
            return_value=True,
        ) as mock_schedule,
    ):
        entries = await load_emoji_library(
            mock_context,
            runtime,
            repair_invalid             = False,
            schedule_background_repair = True,
        )

    assert entries == []
    mock_render.assert_not_awaited()
    mock_schedule.assert_called_once_with(mock_context, runtime)
    saved = json.loads((library_dir / "index.json").read_text(encoding="utf-8"))
    assert saved["entries"][media_hash]["marker"] == "[表情包：json，kind]"


@pytest.mark.asyncio
async def test_load_emoji_library_clears_stale_index_when_library_empty(mock_context):
    library_dir = mock_context.data_dir / "media" / "library"
    library_dir.mkdir(parents=True, exist_ok=True)
    runtime    = _make_media_runtime()
    index_path = library_dir / "index.json"
    index_path.write_text(
        json.dumps(
            {
                "entries": {
                    "stale-hash": {
                        "file_path": "media/library/missing.png",
                        "description": "旧条目",
                        "marker": "[表情包：旧条目]",
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    entries = await load_emoji_library(mock_context, runtime, repair_invalid=False)
    persisted = json.loads(index_path.read_text(encoding="utf-8"))

    assert entries == []
    assert persisted["entries"] == {}


def test_collect_emoji_candidate_skips_structured_garbage(mock_context):
    runtime     = _make_media_runtime()
    source_path = _write_png(mock_context.data_dir / "bad_emoji.png")
    rendered    = RenderedMedia(
        media_hash   = hashlib.sha256(source_path.read_bytes()).hexdigest(),
        kind         = "emoji",
        description  = 'json\n{"kind":"emoji"}',
        emotion_tags = ("json", "kind"),
        marker       = "[表情包：json，kind]",
        cached_path  = source_path,
    )

    collected = collect_emoji_candidate(
        mock_context,
        runtime,
        rendered,
        source_path=source_path,
    )

    assert collected is None
    assert not (mock_context.data_dir / "media" / "library" / f"{rendered.media_hash}.png").exists()


def test_collect_emoji_candidate_rehomes_outside_library_target_path(mock_context):
    library_dir = mock_context.data_dir / "media" / "library"
    library_dir.mkdir(parents=True, exist_ok=True)
    runtime      = _make_media_runtime()
    source_path  = _write_png(mock_context.data_dir / "emoji_source" / "source.png")
    outside_path = mock_context.plugin_dir.parent / "outside.png"
    index_path   = library_dir / "index.json"
    index_path.write_text(
        json.dumps(
            {
                "entries": {
                    "hash-safe": {
                        "file_path": "../outside.png",
                        "description": "旧无语猫猫",
                        "emotion_tags": ["无语"],
                        "usage_count": 2,
                        "last_used_ts": 1.0,
                        "marker": "[表情包：无语]",
                        "status": "active",
                        "source": "auto",
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    rendered = RenderedMedia(
        media_hash   = "hash-safe",
        kind         = "emoji",
        description  = "猫猫无语摊手",
        emotion_tags = ("无语",),
        marker       = "[表情包：无语]",
        cached_path  = source_path,
    )

    collected = collect_emoji_candidate(mock_context, runtime, rendered, source_path=source_path)
    persisted = json.loads(index_path.read_text(encoding="utf-8"))
    stored_rel  = persisted["entries"]["hash-safe"]["file_path"]
    stored_path = (mock_context.data_dir / stored_rel).resolve()

    assert collected is not None
    assert stored_path.exists()
    assert stored_path.parent == library_dir.resolve()
    assert not outside_path.exists()


def test_collect_emoji_candidate_prunes_old_auto_entries(mock_context):
    from PIL import Image

    runtime = _make_media_runtime(emoji_auto_collect_max_entries=1)
    first_path   = mock_context.data_dir / "first_auto.png"
    second_path  = mock_context.data_dir / "second_auto.png"
    first_image  = Image.new("RGBA", (24, 24), (255, 255, 255, 255))
    second_image = Image.new("RGBA", (24, 24), (255, 255, 255, 255))
    for index in range(24):
        first_image.putpixel((index, index), (255, 120, 120, 255))
        second_image.putpixel((23 - index, index), (120, 120, 255, 255))
    first_image.save(first_path)
    second_image.save(second_path)

    first_rendered = RenderedMedia(
        media_hash   = hashlib.sha256(first_path.read_bytes()).hexdigest(),
        kind         = "emoji",
        description  = "红色小鸟翻白眼",
        emotion_tags = ("无语",),
        marker       = "[表情包：无语]",
        cached_path  = first_path,
    )
    second_rendered = RenderedMedia(
        media_hash   = hashlib.sha256(second_path.read_bytes()).hexdigest(),
        kind         = "emoji",
        description  = "蓝色小鸟生气",
        emotion_tags = ("生气",),
        marker       = "[表情包：生气]",
        cached_path  = second_path,
    )

    assert (
        collect_emoji_candidate(mock_context, runtime, first_rendered, source_path=first_path)
        is not None
    )
    assert (
        collect_emoji_candidate(mock_context, runtime, second_rendered, source_path=second_path)
        is not None
    )

    saved = json.loads(
        (mock_context.data_dir / "media" / "library" / "index.json").read_text(encoding="utf-8")
    )
    assert list(saved["entries"].keys()) == [second_rendered.media_hash]
    assert not (
        mock_context.data_dir / "media" / "library" / f"{first_rendered.media_hash}.png"
    ).exists()


@pytest.mark.asyncio
async def test_collect_emoji_candidate_requires_approval_keeps_entry_pending(mock_context):
    runtime = _make_media_runtime(emoji_auto_collect_requires_approval=True)
    source_path = _write_png(mock_context.data_dir / "pending_emoji.png")
    rendered    = RenderedMedia(
        media_hash   = hashlib.sha256(source_path.read_bytes()).hexdigest(),
        kind         = "emoji",
        description  = "猫猫翻白眼",
        emotion_tags = ("无语",),
        marker       = "[表情包：无语]",
        cached_path  = source_path,
    )

    collected = collect_emoji_candidate(mock_context, runtime, rendered, source_path=source_path)
    entries = await load_emoji_library(mock_context, runtime, repair_invalid=False)
    saved = json.loads(
        (mock_context.data_dir / "media" / "library" / "index.json").read_text(encoding="utf-8")
    )

    assert collected is not None
    assert entries == []
    assert saved["entries"][rendered.media_hash]["status"] == "pending"
    assert "/pending/" in saved["entries"][rendered.media_hash]["file_path"]


def test_collect_emoji_candidate_dedups_visually_identical_auto_entries(mock_context):
    from PIL import Image, PngImagePlugin

    runtime = _make_media_runtime(emoji_auto_collect_similarity_threshold=0)
    first_path  = mock_context.data_dir / "dup1.png"
    second_path = mock_context.data_dir / "dup2.png"

    image = Image.new("RGBA", (24, 24), (255, 255, 255, 255))
    image.putpixel((2, 2), (0, 0, 0, 255))
    image.save(first_path)
    pnginfo = PngImagePlugin.PngInfo()
    pnginfo.add_text("note", "same-pixels-different-bytes")
    image.save(second_path, pnginfo=pnginfo)

    first_rendered = RenderedMedia(
        media_hash   = hashlib.sha256(first_path.read_bytes()).hexdigest(),
        kind         = "emoji",
        description  = "猫猫无语",
        emotion_tags = ("无语",),
        marker       = "[表情包：无语]",
        cached_path  = first_path,
    )
    second_rendered = RenderedMedia(
        media_hash   = hashlib.sha256(second_path.read_bytes()).hexdigest(),
        kind         = "emoji",
        description  = "猫猫无语",
        emotion_tags = ("无语",),
        marker       = "[表情包：无语]",
        cached_path  = second_path,
    )

    first = collect_emoji_candidate(mock_context, runtime, first_rendered, source_path=first_path)
    second = collect_emoji_candidate(
        mock_context, runtime, second_rendered, source_path=second_path
    )
    saved = json.loads(
        (mock_context.data_dir / "media" / "library" / "index.json").read_text(encoding="utf-8")
    )

    assert first is not None
    assert second is not None
    assert second[1] is False
    assert list(saved["entries"].keys()) == [first_rendered.media_hash]


def test_structured_media_text_detector_flags_think_and_json_noise():
    assert _looks_like_structured_media_text("<think>先分析一下</think>")
    assert _looks_like_structured_media_text('json\n{"kind":"emoji"}')
    assert not _looks_like_structured_media_text("一只黑色小鸟站着，眼神疑惑")


def test_media_llm_max_tokens_expands_for_thinking_models():
    assert _media_llm_max_tokens({"model": "glm-4.6v-flash"}, 200) == 200
    assert _media_llm_max_tokens({"model": "glm-4.1v-thinking-flash"}, 200) == 800
    assert _media_llm_max_tokens({"model": "glm-4.1v-thinking-flash"}, 120) == 480


def test_media_candidates_follow_core_vision_route_order(mock_context):
    candidates = _resolve_media_llm_secret_candidates(mock_context)

    assert [item["_profile"] for item in candidates] == [
        "glm-4.6v-flash",
        "glm-4.6v",
        "glm-4v-flash",
        "glm-4.1v-thinking-flash",
    ]
    assert all(item["_vision_enabled"] for item in candidates)
    assert all("api_key" not in item and "api_base" not in item for item in candidates)


def test_vision_candidates_never_materialize_plugin_credentials(mock_context):
    mock_context.secrets = {
        "plugins": {
            "xiaoqing_chat": {
                "vision": {
                    "providers": {
                        "legacy": {
                            "api_base": "https://legacy.invalid",
                            "api_key": "<LEGACY_API_KEY>",
                            "model": "legacy-model",
                        }
                    }
                }
            }
        }
    }
    candidates = _resolve_media_llm_secret_candidates(mock_context)
    assert candidates[0]["model"] == "glm-4.6v-flash"
    assert all("api_key" not in item and "api_base" not in item for item in candidates)


def test_malformed_legacy_vision_secrets_cannot_change_core_candidates(mock_context):
    mock_context.secrets = {"plugins": {"xiaoqing_chat": {"vision": ["invalid"]}}}
    candidates           = _resolve_media_llm_secret_candidates(mock_context)
    assert candidates[0]["_profile"] == "glm-4.6v-flash"
    assert len(candidates) == 4


def test_vision_candidates_report_unavailable_without_ai_capability(mock_context):
    mock_context.capabilities = PluginCapabilities()
    candidates                = _resolve_media_llm_secret_candidates(mock_context)
    assert len(candidates) == 1
    assert candidates[0]["_vision_enabled"] is False
    assert candidates[0]["_ai"] is None


def test_media_candidates_do_not_forward_legacy_request_payload(mock_context):
    mock_context.secrets["plugins"]["xiaoqing_chat"]["vision"]["extra_payload"] = {
        "response_format": {"type": "json_object"}
    }
    candidate = _resolve_media_llm_secret_candidates(
        mock_context,
    )[0]
    assert "_extra_payload" not in candidate


@pytest.mark.asyncio
async def test_render_event_media_text_ignores_legacy_vision_extra_payload(mock_context):
    runtime = _make_media_runtime()
    provider = mock_context.secrets["plugins"]["xiaoqing_chat"]["vision"]["providers"]["glm-4v"]
    provider["thinking"] = {"type": "enabled"}
    provider["extra_payload"] = {"response_format": {"type": "json_object"}}
    captured: dict[str, object] = {}

    async def _fake_chat_raw(**kwargs):
        captured["extra_payload"] = kwargs.get("extra_payload")
        return _raw_media_response(
            json.dumps({"kind": "image", "description": "海边落日"}, ensure_ascii=False)
        )

    event = {"message": [{"type": "image", "data": {"url": "https://example.com/cat_photo.png"}}]}
    with (
        patch(
            "plugins.xiaoqing_chat.media.event_media._download_url_bytes",
            new=AsyncMock(return_value=(_PNG_BYTES, "image/png")),
        ),
        patch(
            "plugins.xiaoqing_chat.media.event_media_analysis.chat_completions_raw_with_fallback_paths",
            new=AsyncMock(side_effect=_fake_chat_raw),
        ),
    ):
        text = await render_event_media_text(event, context=mock_context, runtime=runtime)

    assert text == "[图片：海边落日]"
    assert captured["extra_payload"] is None


@pytest.mark.asyncio
async def test_download_url_bytes_streams_and_enforces_timeout(mock_context):
    response = SimpleNamespace(body=b"1234", headers={"Content-Type": "image/png"})
    with patch(
        "plugins.xiaoqing_chat.media.event_media.fetch_public_bytes",
        new=AsyncMock(return_value=response),
    ) as fetch:
        payload, content_type = await _download_url_bytes(
            "https://example.com/test.png",
            max_bytes=8,
        )

    assert payload == b"1234"
    assert content_type == "image/png"
    assert fetch.await_args.kwargs["timeout_seconds"] == 20
