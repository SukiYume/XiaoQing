import asyncio
import base64
import hashlib
import json
from contextlib import nullcontext
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from core.interfaces import PluginCapabilities
from plugins.xiaoqing_chat.llm.llm_client import LLMError
from plugins.xiaoqing_chat.media.emoji_library import collect_emoji_candidate, load_emoji_library
from plugins.xiaoqing_chat.media.event_media import (
    RenderedMedia,
    ResolvedMedia,
    _download_url_bytes,
    _looks_like_structured_media_text,
    _media_llm_max_tokens,
    _prepare_media_for_llm,
    _resolve_media_llm_secrets,
    build_effective_user_text,
    render_event_media,
    render_event_media_text,
    render_local_media_file,
)
from plugins.xiaoqing_chat.media.marker_resolver import (
    extract_choice_json,
    extract_inbound_marker_labels,
    marker_media_part,
    parse_marker,
    resolve_marker,
    strip_outbound_marker_residue,
    text_without_outbound_marker,
)
from plugins.xiaoqing_chat.media.qq_face_catalog import load_qq_face_catalog
from plugins.xiaoqing_chat.media_registry import compact_message_content, resolve_message_content
from plugins.xiaoqing_chat.message_parts import build_text_message_parts, message_parts_to_legacy
from plugins.xiaoqing_chat.runtime_state import get_state

_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO7Z0m8AAAAASUVORK5CYII="
)


def _write_png(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_PNG_BYTES)
    return path


def _reply_draft(text: str) -> SimpleNamespace:
    return SimpleNamespace(text=text, parts=build_text_message_parts(text))


def test_extract_inbound_marker_labels_ignores_rendered_content_suffix() -> None:
    labels = extract_inbound_marker_labels(
        "[表情包：疑惑，调侃；内容：河禽在河里，说‘没毛病啊’]",
        "emoji",
    )

    assert labels == ["疑惑，调侃"]


def _reply_draft_with_parts(
    text: str,
    parts,
    *,
    media_marker=None,
) -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        parts=tuple(parts),
        media_marker=media_marker,
    )


def _gif_bytes() -> bytes:
    from PIL import Image

    image = Image.new("RGBA", (2, 2), (255, 192, 203, 255))
    buffer = BytesIO()
    image.save(buffer, format="GIF")
    return buffer.getvalue()


def _animated_gif_bytes() -> bytes:
    from PIL import Image

    frames = [
        Image.new("RGBA", (24, 24), (255, 255, 255, 0)),
        Image.new("RGBA", (24, 24), (255, 120, 120, 255)),
        Image.new("RGBA", (24, 24), (120, 255, 120, 255)),
    ]
    buffer = BytesIO()
    frames[0].save(
        buffer,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=80,
        loop=0,
        disposal=2,
    )
    return buffer.getvalue()


def _raw_media_response(
    content: str,
    *,
    used_path: str = "/chat/completions",
    finish_reason: str = "stop",
) -> tuple[dict[str, object], str]:
    return (
        {
            "choices": [
                {
                    "message": {"content": content},
                    "finish_reason": finish_reason,
                }
            ]
        },
        used_path,
    )


def _make_media_runtime(**media_overrides):
    media_cfg = SimpleNamespace(
        enable_inbound_media_context=True,
        enable_auto_collect_inbound_emoji=True,
        emoji_auto_collect_requires_approval=False,
        emoji_auto_collect_max_entries=200,
        emoji_auto_collect_similarity_threshold=4,
        max_media_per_message=3,
        max_analyze_bytes=1024 * 1024,
        enable_emoji_refine_background=False,
        emoji_refine_timeout_seconds=2.0,
        vision_provider="",
        vision_timeout_seconds=5.0,
        vision_max_retry=0,
        vision_retry_interval_seconds=0.1,
    )
    for key, value in media_overrides.items():
        setattr(media_cfg, key, value)

    return SimpleNamespace(
        cfg=SimpleNamespace(
            media=media_cfg,
            endpoint_path="/v1/chat/completions",
            temperature=0.7,
            top_p=0.9,
            max_tokens=256,
            timeout_seconds=15.0,
            max_retry=1,
            retry_interval_seconds=0.2,
            foreground_timeout_seconds=12.0,
            foreground_max_retry=0,
            foreground_retry_interval_seconds=0.2,
            background_timeout_seconds=15.0,
            background_max_retry=1,
            background_retry_interval_seconds=0.2,
            max_context_size=30,
            enable_smalltalk=True,
            goal=SimpleNamespace(enable_goal=False),
            reflection=SimpleNamespace(
                enable_expression_reflection=False,
                enable_review_sessions=False,
            ),
            brain_chat=SimpleNamespace(
                enable_private_brain_chat=False,
                show_mode_indicator=False,
                brain_mode_indicator="[brain]",
            ),
            planner=SimpleNamespace(resolve_think_level=lambda history_len=0: 0),
            personality=SimpleNamespace(states=[], state_probability=0.0),
            debug=SimpleNamespace(log_latency=False),
        )
    )


@pytest.fixture
def mock_context(tmp_path: Path):
    context = MagicMock()
    context.config = {"bot_name": "小青"}
    context.secrets = {
        "plugins": {
            "xiaoqing_chat": {
                "default": "test",
                "providers": {
                    "test": {
                        "api_base": "http://test",
                        "api_key": "key",
                        "model": "model",
                        "endpoint_path": "/v1/chat/completions",
                    }
                },
                "vision": {
                    "providers": {
                        "glm-4v": {
                            "api_base": "https://open.bigmodel.cn/api/paas/v4",
                            "api_key": "vision-key",
                            "model": "glm-4v",
                            "endpoint_path": "/chat/completions",
                        }
                    }
                },
            }
        }
    }
    context.plugin_dir = tmp_path / "plugins" / "xiaoqing_chat"
    context.data_dir = tmp_path / "data" / "xiaoqing_chat"
    context.plugin_dir.mkdir(parents=True, exist_ok=True)
    context.data_dir.mkdir(parents=True, exist_ok=True)
    context.http_session = AsyncMock()
    context.capabilities = PluginCapabilities()
    context.send_action = AsyncMock()
    context.logger = MagicMock()
    return context


@pytest.mark.asyncio
async def test_render_local_media_file_uses_render_cache(mock_context):
    runtime = _make_media_runtime()
    image_path = _write_png(mock_context.data_dir / "trip_photo.png")

    async def _fake_analyze(resolved, *, context, runtime, prefer_emoji):
        return RenderedMedia(
            media_hash=resolved.media_hash,
            kind="image",
            description="海边落日",
            emotion_tags=(),
            marker="[图片：海边落日]",
            cached_path=resolved.cached_path,
        )

    with patch(
        "plugins.xiaoqing_chat.media.event_media._analyze_media_with_llm",
        new=AsyncMock(side_effect=_fake_analyze),
    ) as mock_analyze:
        first = await render_local_media_file(image_path, context=mock_context, runtime=runtime)
        second = await render_local_media_file(image_path, context=mock_context, runtime=runtime)

    assert first is not None
    assert second is not None
    assert first.marker == "[图片：海边落日]"
    assert second.marker == "[图片：海边落日]"
    mock_analyze.assert_awaited_once()


@pytest.mark.asyncio
async def test_render_local_media_file_refreshes_old_fallback_cache_with_explicit_vision(mock_context):
    runtime = _make_media_runtime(vision_provider="glm-4v")
    image_path = _write_png(mock_context.data_dir / "camera_roll_refresh_probe_longname.png")
    media_hash = hashlib.sha256(_PNG_BYTES).hexdigest()
    cached_description = image_path.stem.replace("_", " ")
    cache_path = mock_context.data_dir / "media" / "render_cache.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(
            {
                "items": {
                    media_hash: {
                        "kind": "image",
                        "description": cached_description,
                        "emotion_tags": [],
                        "marker": f"[图片：{cached_description}]",
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    async def _fake_analyze(resolved, *, context, runtime, prefer_emoji):
        return RenderedMedia(
            media_hash=resolved.media_hash,
            kind="image",
            description="一只猫趴在窗边",
            emotion_tags=("慵懒",),
            marker="[图片：一只猫趴在窗边]",
            cached_path=resolved.cached_path,
        )

    with patch(
        "plugins.xiaoqing_chat.media.event_media._analyze_media_with_llm",
        new=AsyncMock(side_effect=_fake_analyze),
    ) as mock_analyze:
        rendered = await render_local_media_file(image_path, context=mock_context, runtime=runtime)

    assert rendered is not None
    assert rendered.marker == "[图片：一只猫趴在窗边]"
    mock_analyze.assert_awaited_once()
    saved = json.loads(cache_path.read_text(encoding="utf-8"))
    assert saved["items"][media_hash]["analysis_source"] == "llm"
    assert saved["items"][media_hash]["description"] == "一只猫趴在窗边"


@pytest.mark.asyncio
async def test_render_local_media_file_keeps_old_fallback_cache_without_explicit_vision(mock_context):
    runtime = _make_media_runtime()
    image_path = _write_png(mock_context.data_dir / "camera_roll_refresh_probe_longname.png")
    media_hash = hashlib.sha256(_PNG_BYTES).hexdigest()
    cached_description = image_path.stem.replace("_", " ")
    cache_path = mock_context.data_dir / "media" / "render_cache.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(
            {
                "items": {
                    media_hash: {
                        "kind": "image",
                        "description": cached_description,
                        "emotion_tags": [],
                        "marker": f"[图片：{cached_description}]",
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with patch(
        "plugins.xiaoqing_chat.media.event_media._analyze_media_with_llm",
        new=AsyncMock(side_effect=AssertionError("should reuse cache")),
    ) as mock_analyze:
        rendered = await render_local_media_file(image_path, context=mock_context, runtime=runtime)

    assert rendered is not None
    assert rendered.marker == f"[图片：{cached_description}]"
    mock_analyze.assert_not_awaited()


@pytest.mark.asyncio
async def test_render_local_media_file_refreshes_generic_llm_cache_after_prompt_upgrade(mock_context):
    runtime = _make_media_runtime(vision_provider="glm-4v")
    image_path = _write_png(mock_context.data_dir / "sticker_refresh_probe.jpg")
    media_hash = hashlib.sha256(_PNG_BYTES).hexdigest()
    cache_path = mock_context.data_dir / "media" / "render_cache.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(
            {
                "items": {
                    media_hash: {
                        "kind": "emoji",
                        "description": "动画表情",
                        "emotion_tags": [],
                        "marker": "[表情包：动画表情]",
                        "analysis_source": "llm",
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    async def _fake_analyze(resolved, *, context, runtime, prefer_emoji):
        return RenderedMedia(
            media_hash=resolved.media_hash,
            kind="emoji",
            description="一只猫皱着脸，配字是苦鲁西",
            emotion_tags=("委屈", "难受"),
            marker="[表情包：委屈，难受]",
            cached_path=resolved.cached_path,
        )

    with patch(
        "plugins.xiaoqing_chat.media.event_media._analyze_media_with_llm",
        new=AsyncMock(side_effect=_fake_analyze),
    ) as mock_analyze:
        rendered = await render_local_media_file(image_path, context=mock_context, runtime=runtime, prefer_emoji=True)

    assert rendered is not None
    assert rendered.marker == "[表情包：委屈，难受]"
    mock_analyze.assert_awaited_once()
    saved = json.loads(cache_path.read_text(encoding="utf-8"))
    assert saved["items"][media_hash]["analysis_source"] == "llm"
    assert saved["items"][media_hash]["analysis_quality"] == "detailed"
    assert saved["items"][media_hash]["analysis_prompt_version"] >= 3


def test_extract_choice_json_uses_balanced_json_parser():
    payload = extract_choice_json(
        'selector said {"mode":"none","reason":"看到 {猫} 但不必发"} trailing {"mode":"emoji_only"}'
    )

    assert payload == {}


def test_parse_marker_accepts_first_valid_marker_and_cleans_residue() -> None:
    parsed = parse_marker("哈哈 [想发表情:笑哭] [想发QQ表情:狗头]")

    assert parsed is not None
    assert parsed.kind == "emoji"
    assert parsed.hint == "笑哭"
    assert text_without_outbound_marker("哈哈 [想发表情:笑哭]") == "哈哈"
    assert strip_outbound_marker_residue("行吧 [想发表情:笑哭") == "行吧"
    assert parse_marker("[想发表情:这个描述真的太长了吧超过十二字]") is None


def test_parse_marker_accepts_rendered_qq_face_marker_as_outbound_fallback() -> None:
    parsed = parse_marker("有点绷不住[QQ表情：捂脸]")

    assert parsed is not None
    assert parsed.kind == "qq_face"
    assert parsed.hint == "捂脸"
    assert text_without_outbound_marker("有点绷不住[QQ表情：捂脸]") == "有点绷不住"


@pytest.mark.asyncio
async def test_resolve_marker_matches_emoji_hint_and_builds_media_part(mock_context):
    runtime = _make_media_runtime()
    image_path = _write_png(mock_context.data_dir / "media" / "library" / "wuyu.png")
    media_hash = hashlib.sha256(image_path.read_bytes()).hexdigest()
    index_path = image_path.parent / "index.json"
    index_path.write_text(
        json.dumps(
            {
                "entries": {
                    media_hash: {
                        "file_path": "media/library/wuyu.png",
                        "description": "猫猫无语摊手",
                        "emotion_tags": ["无语", "摊手"],
                        "usage_count": 0,
                        "last_used_ts": 0.0,
                        "marker": "[表情包：猫猫无语摊手]",
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    parsed = parse_marker("笑死 [想发表情:无语]")
    assert parsed is not None
    resolved = await resolve_marker(parsed, context=mock_context, runtime=runtime)

    assert resolved is not None
    assert resolved.kind == "emoji"
    assert resolved.marker == "[表情包：猫猫无语摊手]"
    part = marker_media_part(mock_context, resolved)
    assert part is not None
    assert part["kind"] == "emoji"
    assert part["media_hash"] == media_hash


@pytest.mark.asyncio
async def test_resolve_marker_matches_qq_face_and_image_hints(mock_context):
    runtime = _make_media_runtime()
    image_path = _write_png(mock_context.data_dir / "media" / "reply_images" / "猫举手.png")

    face = await resolve_marker(
        parse_marker("来个 [想发QQ表情:狗头]"),
        context=mock_context,
        runtime=runtime,
    )
    image = await resolve_marker(
        parse_marker("看这个 [想发图片:猫举手]"),
        context=mock_context,
        runtime=runtime,
        history=[],
    )

    assert face is not None
    assert face.kind == "qq_face"
    assert face.marker == "[QQ表情：狗头]"
    rendered_face = await resolve_marker(
        parse_marker("行吧[QQ表情：捂脸]"),
        context=mock_context,
        runtime=runtime,
    )
    assert rendered_face is not None
    assert rendered_face.kind == "qq_face"
    assert rendered_face.entry.face_id == "264"
    assert image is not None
    assert image.kind == "image"
    assert image.entry.file_path == str(image_path)
    assert marker_media_part(mock_context, image)["kind"] == "image"


@pytest.mark.asyncio
async def test_resolve_marker_returns_none_when_candidate_missing(mock_context):
    runtime = _make_media_runtime()
    parsed = parse_marker("哈哈 [想发表情:不存在]")

    assert parsed is not None
    assert await resolve_marker(parsed, context=mock_context, runtime=runtime) is None


@pytest.mark.asyncio
async def test_load_emoji_library_uses_mtime_cache(mock_context):
    runtime = _make_media_runtime()
    image_path = _write_png(mock_context.data_dir / "media" / "library" / "cached.png")
    media_hash = hashlib.sha256(image_path.read_bytes()).hexdigest()
    (image_path.parent / "index.json").write_text(
        json.dumps(
            {
                "entries": {
                    media_hash: {
                        "file_path": "media/library/cached.png",
                        "description": "缓存猫猫",
                        "emotion_tags": ["缓存"],
                        "usage_count": 0,
                        "last_used_ts": 0.0,
                        "marker": "[表情包：缓存猫猫]",
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    first = await load_emoji_library(mock_context, runtime)
    with patch(
        "plugins.xiaoqing_chat.media.emoji_library._iter_library_files",
        side_effect=AssertionError("cache hit should not walk files"),
    ):
        second = await load_emoji_library(mock_context, runtime)

    assert [item.media_hash for item in first] == [media_hash]
    assert [item.media_hash for item in second] == [media_hash]


@pytest.mark.asyncio
async def test_load_qq_face_catalog_uses_mtime_cache(mock_context):
    runtime = _make_media_runtime()

    first = await load_qq_face_catalog(mock_context, runtime)
    with patch(
        "plugins.xiaoqing_chat.media.qq_face_catalog._load_payload",
        side_effect=AssertionError("cache hit should not read payload"),
    ):
        second = await load_qq_face_catalog(mock_context, runtime)

    assert first
    assert [item.face_id for item in second] == [item.face_id for item in first]


@pytest.mark.asyncio
async def test_render_local_media_file_retries_generic_llm_cache_on_next_send(mock_context):
    runtime = _make_media_runtime(vision_provider="glm-4v")
    image_path = _write_png(mock_context.data_dir / "sticker_retry_probe.jpg")

    generic_render = RenderedMedia(
        media_hash="",
        kind="emoji",
        description="动画表情",
        emotion_tags=(),
        marker="[表情包：动画表情]",
        cached_path=image_path,
    )
    detailed_render = RenderedMedia(
        media_hash="",
        kind="emoji",
        description="一只猫皱着脸，配字是苦鲁西",
        emotion_tags=("委屈", "难受"),
        marker="[表情包：委屈，难受]",
        cached_path=image_path,
    )

    async def _fake_analyze(resolved, *, context, runtime, prefer_emoji):
        if not generic_render.media_hash:
            object.__setattr__(generic_render, "media_hash", resolved.media_hash)
            object.__setattr__(detailed_render, "media_hash", resolved.media_hash)
            object.__setattr__(generic_render, "cached_path", resolved.cached_path)
            object.__setattr__(detailed_render, "cached_path", resolved.cached_path)
        if _fake_analyze.calls == 0:
            _fake_analyze.calls += 1
            return generic_render
        return detailed_render

    _fake_analyze.calls = 0

    with patch(
        "plugins.xiaoqing_chat.media.event_media._analyze_media_with_llm",
        new=AsyncMock(side_effect=_fake_analyze),
    ) as mock_analyze:
        first = await render_local_media_file(
            image_path,
            context=mock_context,
            runtime=runtime,
            prefer_emoji=True,
        )
        second = await render_local_media_file(
            image_path,
            context=mock_context,
            runtime=runtime,
            prefer_emoji=True,
        )

    assert first is not None
    assert first.marker == "[表情包：动画表情]"
    assert second is not None
    assert second.marker == "[表情包：委屈，难受]"
    assert mock_analyze.await_count == 2


@pytest.mark.asyncio
async def test_render_event_media_text_falls_back_in_configured_provider_order(mock_context):
    runtime = _make_media_runtime()
    vision = mock_context.secrets["plugins"]["xiaoqing_chat"]["vision"]
    vision["default"] = "glm-4.6v-flash"
    vision["fallbacks"] = ["glm-4.6v", "glm-4v-flash", "glm-4.1v-thinking-flash"]
    vision["providers"] = {
        "glm-4.6v-flash": {
            "api_base": "https://open.bigmodel.cn/api/paas/v4",
            "api_key": "vision-key",
            "model": "glm-4.6v-flash",
            "endpoint_path": "/chat/completions",
        },
        "glm-4.6v": {
            "api_base": "https://open.bigmodel.cn/api/paas/v4",
            "api_key": "vision-key",
            "model": "glm-4.6v",
            "endpoint_path": "/chat/completions",
        },
        "glm-4v-flash": {
            "api_base": "https://open.bigmodel.cn/api/paas/v4",
            "api_key": "vision-key",
            "model": "glm-4v-flash",
            "endpoint_path": "/chat/completions",
        },
        "glm-4.1v-thinking-flash": {
            "api_base": "https://open.bigmodel.cn/api/paas/v4",
            "api_key": "vision-key",
            "model": "glm-4.1v-thinking-flash",
            "endpoint_path": "/chat/completions",
        },
    }
    event = {
        "message": [
            {
                "type": "image",
                "data": {
                    "url": "https://example.com/store_emoji.jpg",
                    "emoji_package_id": 1001,
                    "summary": "[动画表情]",
                },
            }
        ]
    }
    used_models: list[str] = []

    async def _fake_chat_raw(*, model, messages, **kwargs):
        used_models.append(model)
        if model in {"glm-4.6v-flash", "glm-4.6v"}:
            raise LLMError('retryable_http_429:{"error":{"code":"1305","message":"busy"}}')
        content = messages[1]["content"]
        if isinstance(content, list):
            return _raw_media_response(
                json.dumps(
                    {
                        "kind": "emoji",
                        "detailed_description": "一只猫皱着脸，配字是苦鲁西",
                        "visible_text": "苦鲁西",
                        "emotion_tags": ["委屈", "难受"],
                    },
                    ensure_ascii=False,
                )
            )
        return _raw_media_response(
            json.dumps({"description": "委屈猫猫苦鲁西", "emotion_tags": ["委屈", "难受"]}, ensure_ascii=False)
        )

    with patch(
        "plugins.xiaoqing_chat.media.event_media._download_url_bytes",
        new=AsyncMock(return_value=(_PNG_BYTES, "image/png")),
    ), patch(
        "plugins.xiaoqing_chat.media.event_media_analysis.chat_completions_raw_with_fallback_paths",
        new=AsyncMock(side_effect=_fake_chat_raw),
    ):
        text = await render_event_media_text(event, context=mock_context, runtime=runtime)

    assert text == "[表情包：委屈，难受]"
    assert used_models == [
        "glm-4.6v-flash",
        "glm-4.6v",
        "glm-4v-flash",
    ]
    warning_lines = "\n".join(
        str(call.args[1]) if len(call.args) > 1 else ""
        for call in mock_context.logger.warning.call_args_list
    )
    assert '"step": "media.analyze.provider_fallback"' in warning_lines
    assert '"to_provider": "glm-4v-flash"' in warning_lines


@pytest.mark.asyncio
async def test_render_event_media_text_retries_same_provider_once_on_semantic_failure(mock_context):
    runtime = _make_media_runtime()
    vision = mock_context.secrets["plugins"]["xiaoqing_chat"]["vision"]
    vision["default"] = "glm-4.6v-flash"
    vision["fallbacks"] = ["glm-4v-flash"]
    vision["providers"] = {
        "glm-4.6v-flash": {
            "api_base": "https://open.bigmodel.cn/api/paas/v4",
            "api_key": "vision-key",
            "model": "glm-4.6v-flash",
            "endpoint_path": "/chat/completions",
        },
        "glm-4v-flash": {
            "api_base": "https://open.bigmodel.cn/api/paas/v4",
            "api_key": "vision-key",
            "model": "glm-4v-flash",
            "endpoint_path": "/chat/completions",
        },
    }
    event = {
        "message": [{"type": "image", "data": {"url": "https://example.com/cat_photo.png"}}]
    }
    used_models: list[str] = []
    call_count = 0

    async def _fake_chat_raw(*, model, **kwargs):
        nonlocal call_count
        used_models.append(model)
        call_count += 1
        if model == "glm-4.6v-flash" and call_count == 1:
            return _raw_media_response("```json\n{\"kind\":\"image\",\"description\":\"")
        if model == "glm-4.6v-flash" and call_count == 2:
            return _raw_media_response(
                json.dumps({"kind": "image", "description": "海边落日"}, ensure_ascii=False)
            )
        raise AssertionError(f"unexpected fallback provider call: {model}")

    with patch(
        "plugins.xiaoqing_chat.media.event_media._download_url_bytes",
        new=AsyncMock(return_value=(_PNG_BYTES, "image/png")),
    ), patch(
        "plugins.xiaoqing_chat.media.event_media_analysis.chat_completions_raw_with_fallback_paths",
        new=AsyncMock(side_effect=_fake_chat_raw),
    ):
        text = await render_event_media_text(event, context=mock_context, runtime=runtime)

    assert text == "[图片：海边落日]"
    assert used_models == ["glm-4.6v-flash", "glm-4.6v-flash"]
    warning_lines = "\n".join(
        str(call.args[1]) if len(call.args) > 1 else ""
        for call in mock_context.logger.warning.call_args_list
    )
    assert '"step": "media.analyze.provider_retry"' in warning_lines


@pytest.mark.asyncio
async def test_render_event_media_text_logs_raw_response_metadata(mock_context):
    runtime = _make_media_runtime(vision_provider="glm-4v")
    event = {
        "message": [{"type": "image", "data": {"url": "https://example.com/cat_photo.png"}}]
    }
    raw_content = json.dumps({"kind": "image", "description": "海边落日"}, ensure_ascii=False)

    async def _fake_chat_raw(**kwargs):
        return (
            {
                "choices": [
                    {
                        "message": {"content": raw_content},
                        "finish_reason": "stop",
                    }
                ]
            },
            "/chat/completions",
        )

    with patch(
        "plugins.xiaoqing_chat.media.event_media._download_url_bytes",
        new=AsyncMock(return_value=(_PNG_BYTES, "image/png")),
    ), patch(
        "plugins.xiaoqing_chat.media.event_media_analysis.chat_completions_raw_with_fallback_paths",
        new=AsyncMock(side_effect=_fake_chat_raw),
    ):
        text = await render_event_media_text(event, context=mock_context, runtime=runtime)

    assert text == "[图片：海边落日]"
    log_lines = "\n".join(
        str(call.args[1]) if len(call.args) > 1 else ""
        for call in mock_context.logger.info.call_args_list
    )
    assert '"step": "media.analyze.detail.ok"' in log_lines
    assert '"used_path": "[redacted ' in log_lines
    assert '"finish_reason": "stop"' in log_lines
    assert f'"raw_chars": {len(raw_content)}' in log_lines


@pytest.mark.asyncio
async def test_render_event_media_text_preserves_visible_text_for_text_heavy_screenshot(mock_context):
    runtime = _make_media_runtime(vision_provider="glm-4v")
    event = {
        "message": [{"type": "image", "data": {"url": "https://example.com/news_screenshot.jpg"}}]
    }
    captured: dict[str, object] = {}

    async def _fake_chat_raw(**kwargs):
        messages = kwargs["messages"]
        captured["prompt"] = messages[1]["content"][0]["text"]
        captured["max_tokens"] = kwargs["max_tokens"]
        return _raw_media_response(
            json.dumps(
                {
                    "kind": "image",
                    "description": "新闻截图，福岛陆上自卫队射击训练误燃干草引发山火，灭火后下山又遭熊袭击",
                    "visible_text": "22日福岛县陆上自卫队射击训练误燃干草，花5小时扑灭山火，下山时遭熊袭击导致队员受伤",
                    "emotion_tags": ["离谱", "惊险"],
                },
                ensure_ascii=False,
            )
        )

    with patch(
        "plugins.xiaoqing_chat.media.event_media._download_url_bytes",
        new=AsyncMock(return_value=(_PNG_BYTES, "image/png")),
    ), patch(
        "plugins.xiaoqing_chat.media.event_media_analysis.chat_completions_raw_with_fallback_paths",
        new=AsyncMock(side_effect=_fake_chat_raw),
    ):
        text = await render_event_media_text(event, context=mock_context, runtime=runtime)

    assert "visible_text" in str(captured["prompt"])
    assert captured["max_tokens"] >= 320
    assert "射击训练误燃干草" in text
    assert "5小时" in text
    assert "熊袭击导致队员受伤" in text


@pytest.mark.asyncio
async def test_render_event_media_text_retries_when_finish_reason_length(mock_context):
    runtime = _make_media_runtime(vision_provider="glm-4v")
    event = {
        "message": [{"type": "image", "data": {"url": "https://example.com/news_screenshot.jpg"}}]
    }
    calls = 0
    max_tokens_seen: list[int] = []

    async def _fake_chat_raw(**kwargs):
        nonlocal calls
        calls += 1
        max_tokens_seen.append(kwargs["max_tokens"])
        if calls == 1:
            return _raw_media_response(
                json.dumps(
                    {
                        "kind": "image",
                        "description": "新闻截图",
                        "visible_text": "陆上自卫队演习引发山火",
                    },
                    ensure_ascii=False,
                ),
                finish_reason="length",
            )
        return _raw_media_response(
            json.dumps(
                {
                    "kind": "image",
                    "description": "新闻截图，福岛陆上自卫队灭火后下山时遭熊袭击，队员受伤",
                    "visible_text": "自卫队训练引发山火，花5小时灭火后遇熊袭击",
                    "emotion_tags": ["意外"],
                },
                ensure_ascii=False,
            )
        )

    with patch(
        "plugins.xiaoqing_chat.media.event_media._download_url_bytes",
        new=AsyncMock(return_value=(_PNG_BYTES, "image/png")),
    ), patch(
        "plugins.xiaoqing_chat.media.event_media_analysis.chat_completions_raw_with_fallback_paths",
        new=AsyncMock(side_effect=_fake_chat_raw),
    ):
        text = await render_event_media_text(event, context=mock_context, runtime=runtime)

    assert calls == 2
    assert max_tokens_seen == [360, 720]
    assert "遇熊袭击" in text
    warning_lines = "\n".join(
        str(call.args[1]) if len(call.args) > 1 else ""
        for call in mock_context.logger.warning.call_args_list
    )
    assert '"reason_code": "length_truncated"' in warning_lines
    assert '"step": "media.analyze.provider_retry"' in warning_lines


@pytest.mark.asyncio
async def test_render_event_media_text_falls_back_after_semantic_retry_exhausted(mock_context):
    runtime = _make_media_runtime()
    vision = mock_context.secrets["plugins"]["xiaoqing_chat"]["vision"]
    vision["default"] = "glm-4.6v-flash"
    vision["fallbacks"] = ["glm-4v-flash"]
    vision["providers"] = {
        "glm-4.6v-flash": {
            "api_base": "https://open.bigmodel.cn/api/paas/v4",
            "api_key": "vision-key",
            "model": "glm-4.6v-flash",
            "endpoint_path": "/chat/completions",
        },
        "glm-4v-flash": {
            "api_base": "https://open.bigmodel.cn/api/paas/v4",
            "api_key": "vision-key",
            "model": "glm-4v-flash",
            "endpoint_path": "/chat/completions",
        },
    }
    event = {
        "message": [{"type": "image", "data": {"url": "https://example.com/cat_photo.png"}}]
    }
    used_models: list[str] = []

    async def _fake_chat_raw(*, model, **kwargs):
        used_models.append(model)
        if model == "glm-4.6v-flash":
            return _raw_media_response("```json\n{\"kind\":\"image\",\"description\":\"")
        return _raw_media_response(json.dumps({"kind": "image", "description": "窗边猫猫"}, ensure_ascii=False))

    with patch(
        "plugins.xiaoqing_chat.media.event_media._download_url_bytes",
        new=AsyncMock(return_value=(_PNG_BYTES, "image/png")),
    ), patch(
        "plugins.xiaoqing_chat.media.event_media_analysis.chat_completions_raw_with_fallback_paths",
        new=AsyncMock(side_effect=_fake_chat_raw),
    ):
        text = await render_event_media_text(event, context=mock_context, runtime=runtime)

    assert text == "[图片：窗边猫猫]"
    assert used_models == ["glm-4.6v-flash", "glm-4.6v-flash", "glm-4v-flash"]
    warning_lines = "\n".join(
        str(call.args[1]) if len(call.args) > 1 else ""
        for call in mock_context.logger.warning.call_args_list
    )
    assert '"step": "media.analyze.provider_retry"' in warning_lines
    assert '"step": "media.analyze.provider_fallback"' in warning_lines
    assert '"to_provider": "glm-4v-flash"' in warning_lines


@pytest.mark.asyncio
async def test_render_event_media_text_falls_back_immediately_on_request_timeout(mock_context):
    runtime = _make_media_runtime()
    vision = mock_context.secrets["plugins"]["xiaoqing_chat"]["vision"]
    vision["default"] = "glm-4.6v-flash"
    vision["fallbacks"] = ["glm-4v-flash"]
    vision["providers"] = {
        "glm-4.6v-flash": {
            "api_base": "https://open.bigmodel.cn/api/paas/v4",
            "api_key": "vision-key",
            "model": "glm-4.6v-flash",
            "endpoint_path": "/chat/completions",
        },
        "glm-4v-flash": {
            "api_base": "https://open.bigmodel.cn/api/paas/v4",
            "api_key": "vision-key",
            "model": "glm-4v-flash",
            "endpoint_path": "/chat/completions",
        },
    }
    event = {
        "message": [{"type": "image", "data": {"url": "https://example.com/cat_photo.png"}}]
    }
    used_models: list[str] = []

    async def _fake_chat_raw(*, model, **kwargs):
        used_models.append(model)
        if model == "glm-4.6v-flash":
            raise asyncio.TimeoutError()
        return _raw_media_response(json.dumps({"kind": "image", "description": "草地小狗"}, ensure_ascii=False))

    with patch(
        "plugins.xiaoqing_chat.media.event_media._download_url_bytes",
        new=AsyncMock(return_value=(_PNG_BYTES, "image/png")),
    ), patch(
        "plugins.xiaoqing_chat.media.event_media_analysis.chat_completions_raw_with_fallback_paths",
        new=AsyncMock(side_effect=_fake_chat_raw),
    ):
        text = await render_event_media_text(event, context=mock_context, runtime=runtime)

    assert text == "[图片：草地小狗]"
    assert used_models == ["glm-4.6v-flash", "glm-4v-flash"]
    warning_lines = "\n".join(
        str(call.args[1]) if len(call.args) > 1 else ""
        for call in mock_context.logger.warning.call_args_list
    )
    assert '"step": "media.analyze.provider_fallback"' in warning_lines
    assert '"to_provider": "glm-4v-flash"' in warning_lines


@pytest.mark.asyncio
async def test_render_event_media_text_uses_detail_when_emoji_refine_is_generic(mock_context):
    runtime = _make_media_runtime(vision_provider="glm-4v")
    event = {
        "message": [
            {
                "type": "image",
                "data": {
                    "url": "https://example.com/store_emoji.jpg",
                    "emoji_package_id": 1001,
                    "summary": "[动画表情]",
                },
            }
        ]
    }

    async def _fake_chat_raw(*, messages, **kwargs):
        content = messages[1]["content"]
        if isinstance(content, list):
            return _raw_media_response(
                json.dumps(
                    {
                        "kind": "emoji",
                        "detailed_description": "一只猫皱着脸，配字是苦鲁西",
                        "visible_text": "苦鲁西",
                        "emotion_tags": ["委屈", "难受"],
                    },
                    ensure_ascii=False,
                )
            )
        return _raw_media_response(json.dumps({"description": "动画表情", "emotion_tags": []}, ensure_ascii=False))

    with patch(
        "plugins.xiaoqing_chat.media.event_media._download_url_bytes",
        new=AsyncMock(return_value=(_PNG_BYTES, "image/png")),
    ), patch(
        "plugins.xiaoqing_chat.media.event_media_analysis.chat_completions_raw_with_fallback_paths",
        new=AsyncMock(side_effect=_fake_chat_raw),
    ):
        text = await render_event_media_text(event, context=mock_context, runtime=runtime)

    assert text == "[表情包：委屈，难受]"


@pytest.mark.asyncio
async def test_render_event_media_text_defers_slow_emoji_refine_and_uses_detail(mock_context):
    runtime = _make_media_runtime(
        vision_provider="glm-4v",
        enable_emoji_refine_background=True,
        emoji_refine_timeout_seconds=0.01,
    )
    event = {
        "message": [
            {
                "type": "image",
                "data": {
                    "url": "https://example.com/store_emoji.jpg",
                    "emoji_package_id": 1001,
                    "summary": "[动画表情]",
                },
            }
        ]
    }

    async def _fake_chat_raw(*, messages, **kwargs):
        content = messages[1]["content"]
        if isinstance(content, list):
            return _raw_media_response(
                json.dumps(
                    {
                        "kind": "emoji",
                        "description": "一只黑色猫咪，眼睛瞪得很大",
                        "emotion_tags": ["惊讶", "警觉"],
                    },
                    ensure_ascii=False,
                )
            )
        await asyncio.sleep(60)

    with patch(
        "plugins.xiaoqing_chat.media.event_media._download_url_bytes",
        new=AsyncMock(return_value=(_PNG_BYTES, "image/png")),
    ), patch(
        "plugins.xiaoqing_chat.media.event_media_analysis.chat_completions_raw_with_fallback_paths",
        new=AsyncMock(side_effect=_fake_chat_raw),
    ):
        text = await render_event_media_text(event, context=mock_context, runtime=runtime)
        await asyncio.sleep(0.05)

    assert text == "[表情包：惊讶，警觉]"
    warning_lines = "\n".join(
        str(call.args[1]) if len(call.args) > 1 else ""
        for call in mock_context.logger.warning.call_args_list
    )
    assert '"step": "media.analyze.refine.background.timeout"' in warning_lines


@pytest.mark.asyncio
async def test_render_event_media_background_refine_updates_cache(mock_context):
    runtime = _make_media_runtime(
        vision_provider="glm-4v",
        enable_emoji_refine_background=True,
        emoji_refine_timeout_seconds=1.0,
    )
    event = {
        "message": [
            {
                "type": "image",
                "data": {
                    "url": "https://example.com/store_emoji.jpg",
                    "emoji_package_id": 1001,
                    "summary": "[动画表情]",
                },
            }
        ]
    }

    async def _fake_chat_raw(*, messages, **kwargs):
        content = messages[1]["content"]
        if isinstance(content, list):
            return _raw_media_response(
                json.dumps(
                    {
                        "kind": "emoji",
                        "description": "一只黑色猫咪，眼睛瞪得很大",
                        "emotion_tags": ["惊讶", "警觉"],
                    },
                    ensure_ascii=False,
                )
            )
        return _raw_media_response(
            json.dumps(
                {"description": "黑猫瞪大双眼", "emotion_tags": ["惊讶", "警觉"]},
                ensure_ascii=False,
            )
        )

    with patch(
        "plugins.xiaoqing_chat.media.event_media._download_url_bytes",
        new=AsyncMock(return_value=(_PNG_BYTES, "image/png")),
    ), patch(
        "plugins.xiaoqing_chat.media.event_media_analysis.chat_completions_raw_with_fallback_paths",
        new=AsyncMock(side_effect=_fake_chat_raw),
    ):
        text = await render_event_media_text(event, context=mock_context, runtime=runtime)
        await asyncio.sleep(0.05)

    assert text == "[表情包：惊讶，警觉]"
    cache_path = mock_context.data_dir / "media" / "render_cache.json"
    item = {}
    for _ in range(20):
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        item = next(iter(cache["items"].values()))
        if item.get("description") == "黑猫瞪大双眼":
            break
        await asyncio.sleep(0.01)
    assert item["description"] == "黑猫瞪大双眼"
    assert item["marker"] == "[表情包：惊讶，警觉]"


@pytest.mark.asyncio
async def test_render_event_media_text_retries_and_then_falls_back_when_detail_empty(mock_context):
    runtime = _make_media_runtime(vision_provider="glm-4v")
    event = {
        "message": [
            {
                "type": "image",
                "data": {
                    "url": "https://example.com/store_emoji.jpg",
                    "emoji_package_id": 1001,
                    "summary": "[动画表情]",
                },
            }
        ]
    }

    async def _fake_chat_raw(*, messages, **kwargs):
        content = messages[1]["content"]
        if isinstance(content, list):
            return _raw_media_response(json.dumps({"kind": "emoji", "description": "", "emotion_tags": []}, ensure_ascii=False))
        return _raw_media_response(json.dumps({"description": "", "emotion_tags": []}, ensure_ascii=False))

    with patch(
        "plugins.xiaoqing_chat.media.event_media._download_url_bytes",
        new=AsyncMock(return_value=(_PNG_BYTES, "image/png")),
    ), patch(
        "plugins.xiaoqing_chat.media.event_media_analysis.chat_completions_raw_with_fallback_paths",
        new=AsyncMock(side_effect=_fake_chat_raw),
    ):
        text = await render_event_media_text(event, context=mock_context, runtime=runtime)

    assert text == "[表情包：动画表情]"
    log_lines = "\n".join(
        str(call.args[1]) if len(call.args) > 1 else ""
        for call in [*mock_context.logger.info.call_args_list, *mock_context.logger.warning.call_args_list]
    )
    assert '"step": "media.analyze.provider_retry"' in log_lines
    assert '"step": "media.analyze.fail"' in log_lines
    assert "semantic_validation_failed" in log_lines
    assert '"step": "media.render.fallback"' in log_lines


@pytest.mark.asyncio
async def test_render_event_media_text_does_not_leak_download_query_into_fallback_label(mock_context):
    runtime = _make_media_runtime(vision_provider="glm-4v")
    event = {
        "message": [
            {
                "type": "image",
                "data": {
                    "url": "https://example.com/download?appid=1407&fileid=EhR8SaLBMCs4n",
                },
            }
        ]
    }

    async def _fake_chat_raw(*, messages, **kwargs):
        content = messages[1]["content"]
        if isinstance(content, list):
            return _raw_media_response(json.dumps({"kind": "image", "description": "", "emotion_tags": []}, ensure_ascii=False))
        return _raw_media_response(json.dumps({"description": "", "emotion_tags": []}, ensure_ascii=False))

    with patch(
        "plugins.xiaoqing_chat.media.event_media._download_url_bytes",
        new=AsyncMock(return_value=(_PNG_BYTES, "image/png")),
    ), patch(
        "plugins.xiaoqing_chat.media.event_media_analysis.chat_completions_raw_with_fallback_paths",
        new=AsyncMock(side_effect=_fake_chat_raw),
    ):
        text = await render_event_media_text(event, context=mock_context, runtime=runtime)

    assert text == "[图片：一张图片]"


@pytest.mark.asyncio
async def test_render_event_media_text_resolves_remote_url_into_cache(mock_context):
    runtime = _make_media_runtime()
    event = {
        "message": [{"type": "image", "data": {"url": "https://example.com/cat_photo.png"}}]
    }

    with patch(
        "plugins.xiaoqing_chat.media.event_media._download_url_bytes",
        new=AsyncMock(return_value=(_PNG_BYTES, "image/png")),
    ), patch(
        "plugins.xiaoqing_chat.media.event_media._analyze_media_with_llm",
        new=AsyncMock(return_value=None),
    ):
        text = await render_event_media_text(event, context=mock_context, runtime=runtime)

    cached_files = list((mock_context.data_dir / "media" / "inbox").glob("*"))
    assert text.startswith("[图片：")
    assert cached_files


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "file_value",
    [
        "base64://" + base64.b64encode(_PNG_BYTES).decode("ascii"),
        "data:image/png;base64," + base64.b64encode(_PNG_BYTES).decode("ascii"),
    ],
)
async def test_render_event_media_text_supports_inline_image_sources(mock_context, file_value):
    runtime = _make_media_runtime()
    event = {
        "message": [{"type": "image", "data": {"file": file_value, "name": "inline_image.png"}}]
    }

    with patch(
        "plugins.xiaoqing_chat.media.event_media._analyze_media_with_llm",
        new=AsyncMock(return_value=None),
    ):
        text = await render_event_media_text(event, context=mock_context, runtime=runtime)

    cached_files = list((mock_context.data_dir / "media" / "inbox").glob("*"))
    assert text.startswith("[图片：")
    assert cached_files


@pytest.mark.asyncio
async def test_render_event_media_blocks_private_image_url_without_download(mock_context):
    runtime = _make_media_runtime()
    event = {
        "message": [
            {
                "type": "image",
                "data": {"url": "http://127.0.0.1:5700/private.png"},
            }
        ]
    }
    mock_context.http_session.get = Mock(side_effect=AssertionError("unsafe URL fetched"))

    rendered = await render_event_media(event, context=mock_context, runtime=runtime)

    mock_context.http_session.get.assert_not_called()
    assert len(rendered) == 1
    assert rendered[0].marker.startswith("[图片：")


@pytest.mark.asyncio
async def test_render_event_media_blocks_file_uri_outside_plugin_data_dir(mock_context, tmp_path):
    runtime = _make_media_runtime()
    outside = _write_png(tmp_path / "outside.png")
    event = {
        "message": [
            {
                "type": "image",
                "data": {"file": outside.as_uri()},
            }
        ]
    }

    with patch(
        "plugins.xiaoqing_chat.media.event_media._analyze_media_with_llm",
        new=AsyncMock(side_effect=AssertionError("outside file should not be analyzed")),
    ):
        rendered = await render_event_media(event, context=mock_context, runtime=runtime)

    cached_files = list((mock_context.data_dir / "media" / "inbox").glob("*"))
    assert len(rendered) == 1
    assert rendered[0].marker.startswith("[图片：")
    assert cached_files == []


@pytest.mark.asyncio
async def test_render_event_media_text_marks_napcat_store_emoji_image(mock_context):
    runtime = _make_media_runtime()
    event = {
        "message": [
            {
                "type": "image",
                "data": {
                    "url": "https://example.com/store_emoji.bin",
                    "emoji_package_id": 1001,
                    "emoji_id": "emo-1",
                    "key": "pkg_123",
                },
            }
        ]
    }

    with patch(
        "plugins.xiaoqing_chat.media.event_media._download_url_bytes",
        new=AsyncMock(return_value=(_PNG_BYTES, "image/png")),
    ), patch(
        "plugins.xiaoqing_chat.media.event_media._analyze_media_with_llm",
        new=AsyncMock(return_value=None),
    ):
        text = await render_event_media_text(event, context=mock_context, runtime=runtime)

    assert text.startswith("[表情包：")
    assert event["_xc_new_emoji_count"] == 1
    assert list((mock_context.data_dir / "media" / "library").glob("*"))


@pytest.mark.asyncio
async def test_render_event_media_text_transcodes_octet_stream_sticker_for_vision(mock_context):
    runtime = _make_media_runtime(vision_provider="glm-4v")
    event = {
        "message": [
            {
                "type": "image",
                "data": {
                    "url": "https://example.com/store_emoji.bin",
                    "emoji_package_id": 1001,
                    "emoji_id": "emo-1",
                    "summary": "[动画表情]",
                },
            }
        ]
    }
    captured_urls: list[str] = []
    captured_prompt = ""

    async def _fake_chat_raw(*, messages, **kwargs):
        nonlocal captured_prompt
        content = messages[1]["content"]
        captured_prompt = content[0]["text"]
        image_url = content[1]["image_url"]["url"]
        captured_urls.append(image_url)
        return _raw_media_response(
            '{"kind":"emoji","description":"一只猫皱着脸，配字是苦鲁西","emotion_tags":["委屈","难受"]}'
        )

    with patch(
        "plugins.xiaoqing_chat.media.event_media._download_url_bytes",
        new=AsyncMock(return_value=(_animated_gif_bytes(), "application/octet-stream")),
    ), patch(
        "plugins.xiaoqing_chat.media.event_media_analysis.chat_completions_raw_with_fallback_paths",
        new=AsyncMock(side_effect=_fake_chat_raw),
    ):
        text = await render_event_media_text(event, context=mock_context, runtime=runtime)

    assert text == "[表情包：委屈，难受]"
    assert captured_urls and captured_urls[0].startswith("data:image/png;base64,")
    assert "同一个动画表情里抽取的" in captured_prompt
    assert "不是多个人物" in captured_prompt
    assert event["_xc_new_emoji_count"] == 1


def test_prepare_media_for_llm_uses_contact_sheet_for_animated_gif(mock_context):
    from PIL import Image

    gif_path = mock_context.data_dir / "animated_probe.gif"
    payload = _animated_gif_bytes()
    gif_path.write_bytes(payload)
    resolved = SimpleNamespace(
        mime_type="image/gif",
        cached_path=gif_path,
        is_animated=True,
    )

    prepared = _prepare_media_for_llm(resolved)

    assert prepared.mime_type == "image/png"
    assert prepared.transcoded is True
    assert prepared.frame_strategy == "animation_contact_sheet"
    assert prepared.frame_count == 3
    with Image.open(BytesIO(prepared.payload)) as image:
        assert image.width > image.height


@pytest.mark.asyncio
async def test_render_event_media_text_supports_face_segment(mock_context):
    runtime = _make_media_runtime()
    event = {
        "message": [
            {
                "type": "face",
                "data": {
                    "id": "14",
                    "raw": {"text": "微笑"},
                },
            }
        ]
    }

    text = await render_event_media_text(event, context=mock_context, runtime=runtime)

    assert text == "[QQ表情：微笑]"
    assert event["_xc_new_emoji_count"] == 0


@pytest.mark.asyncio
async def test_event_media_items_for_memory_preserves_qq_face_identity(mock_context):
    from plugins.xiaoqing_chat.handlers import _event_media_items_for_memory

    runtime = _make_media_runtime()
    event = {
        "message": [
            {
                "type": "face",
                "data": {
                    "id": "14",
                    "raw": {"text": "微笑"},
                },
            }
        ]
    }

    items = await _event_media_items_for_memory(event, context=mock_context, runtime=runtime)

    assert items == [
        {
            "kind": "qq_face",
            "media_hash": "qq_face:14",
            "face_id": "14",
            "marker": "[QQ表情：微笑]",
            "description": "微笑",
            "label": "微笑",
            "emotion_tags": ["微笑"],
        }
    ]


@pytest.mark.asyncio
async def test_build_effective_user_text_preserves_mixed_media_order(mock_context):
    runtime = _make_media_runtime()
    event = {
        "message": [
            {"type": "text", "data": {"text": "看这个"}},
            {"type": "image", "data": {"url": "https://example.com/cat.png"}},
            {"type": "text", "data": {"text": "笑死"}},
        ],
        "_xc_rendered_media_items": [
            RenderedMedia(
                media_hash="hash-1",
                kind="image",
                description="一只猫歪着头",
                emotion_tags=(),
                marker="[图片：一只猫歪着头]",
            )
        ],
    }

    text = await build_effective_user_text(
        "看这个笑死",
        event,
        context=mock_context,
        runtime=runtime,
    )

    assert text == "看这个\n[图片：一只猫歪着头]\n笑死"


@pytest.mark.asyncio
async def test_build_effective_user_text_keeps_media_position_after_prefix_strip(mock_context):
    runtime = _make_media_runtime()
    event = {
        "message": [
            {"type": "text", "data": {"text": "小青你看"}},
            {"type": "image", "data": {"url": "https://example.com/cat.png"}},
            {"type": "text", "data": {"text": "这个"}},
        ],
        "_xc_rendered_media_items": [
            RenderedMedia(
                media_hash="hash-2",
                kind="image",
                description="一只猫歪着头",
                emotion_tags=(),
                marker="[图片：一只猫歪着头]",
            )
        ],
    }

    text = await build_effective_user_text(
        "你看这个",
        event,
        context=mock_context,
        runtime=runtime,
    )

    assert text == "你看\n[图片：一只猫歪着头]\n这个"


@pytest.mark.asyncio
async def test_build_effective_user_text_includes_emoji_detail_context(mock_context):
    runtime = _make_media_runtime()
    event = {
        "message": [
            {"type": "text", "data": {"text": "你这"}},
            {"type": "image", "data": {"url": "https://example.com/bird.jpg", "summary": "[动画表情]"}},
        ],
        "_xc_rendered_media_items": [
            RenderedMedia(
                media_hash="hash-emoji",
                kind="emoji",
                description='卡通小鸟倒地，上方对话框写"不愧是你"，下方文字写"我佩服得鹉体投地"',
                emotion_tags=("佩服", "调侃", "开心"),
                marker="[表情包：佩服，调侃]",
            )
        ],
    }

    text = await build_effective_user_text(
        "你这",
        event,
        context=mock_context,
        runtime=runtime,
    )

    assert text == '你这\n[表情包：佩服，调侃；内容：卡通小鸟倒地，上方对话框写"不愧是你"，下方文字写"我佩服得鹉体投地"]'


@pytest.mark.asyncio
async def test_build_effective_user_text_surfaces_emoji_visible_text_as_quoted_speech(mock_context):
    runtime = _make_media_runtime()
    event = {
        "message": [
            {"type": "image", "data": {"url": "https://example.com/cat.jpg", "summary": "[动画表情]"}},
        ],
        "_xc_rendered_media_items": [
            RenderedMedia(
                media_hash="hash-emoji",
                kind="emoji",
                description="一个猫耳动漫女孩露出惊讶表情，配文字表达疑惑或调侃。，文字内容是“那咋整啊”",
                emotion_tags=("疑惑", "调侃"),
                marker="[表情包：疑惑，调侃]",
            )
        ],
    }

    text = await build_effective_user_text(
        "",
        event,
        context=mock_context,
        runtime=runtime,
    )

    assert text == "[表情包：疑惑，调侃；写着“那咋整啊”]"


@pytest.mark.asyncio
async def test_render_event_media_text_strips_face_brackets(mock_context):
    runtime = _make_media_runtime()
    event = {
        "message": [
            {
                "type": "face",
                "data": {
                    "id": "1",
                    "raw": {"text": "[狂笑]"},
                },
            }
        ]
    }

    text = await render_event_media_text(event, context=mock_context, runtime=runtime)

    assert text == "[QQ表情：狂笑]"


@pytest.mark.asyncio
async def test_render_event_media_text_fetches_mface_image_via_onebot_apis(mock_context):
    runtime = _make_media_runtime()
    event = {
        "message_id": 42,
        "message": [
            {
                "type": "mface",
                "data": {
                    "emoji_package_id": 1001,
                    "emoji_id": "emo-1",
                    "key": "pkg_123",
                    "summary": "无语",
                },
            }
        ],
    }

    media = SimpleNamespace(
        get_message=AsyncMock(
            return_value={
                "message": [
                    {
                        "type": "image",
                        "data": {
                            "file_id": "file-1",
                            "summary": "无语",
                        },
                    }
                ]
            }
        ),
        get_image=AsyncMock(
            return_value={
                "base64": base64.b64encode(_PNG_BYTES).decode("ascii"),
                "file_name": "sticker.png",
            }
        ),
    )
    mock_context.capabilities = PluginCapabilities(onebot_media=media)

    with patch(
        "plugins.xiaoqing_chat.media.event_media._analyze_media_with_llm",
        new=AsyncMock(return_value=None),
    ):
        text = await render_event_media_text(event, context=mock_context, runtime=runtime)

    assert text.startswith("[表情包：")
    assert event["_xc_new_emoji_count"] == 1
    media.get_message.assert_awaited_once_with(42)
    media.get_image.assert_awaited_once_with(file_id="file-1", file=None)


@pytest.mark.asyncio
async def test_render_event_media_text_falls_back_to_store_emoji_summary_when_download_fails(mock_context):
    runtime = _make_media_runtime()
    event = {
        "message": [
            {
                "type": "image",
                "data": {
                    "url": "https://example.com/store_emoji.bin",
                    "emoji_package_id": 1001,
                    "emoji_id": "emo-1",
                    "key": "pkg_123",
                },
            }
        ]
    }

    with patch(
        "plugins.xiaoqing_chat.media.event_media._download_url_bytes",
        new=AsyncMock(side_effect=FileNotFoundError("expired")),
    ):
        text = await render_event_media_text(event, context=mock_context, runtime=runtime)

    assert text.startswith("[表情包：")


@pytest.mark.asyncio
async def test_load_emoji_library_reuses_existing_metadata(mock_context):
    library_dir = mock_context.data_dir / "media" / "library"
    library_dir.mkdir(parents=True, exist_ok=True)
    _write_png(library_dir / "无语猫猫.png")
    _write_png(library_dir / "开心狗狗.png")
    runtime = _make_media_runtime()

    async def _fake_render(file_path, *, context, runtime, prefer_emoji):
        file_path = Path(file_path)
        media_hash = hashlib.sha256(file_path.read_bytes()).hexdigest()
        stem = file_path.stem
        return RenderedMedia(
            media_hash=media_hash,
            kind="emoji",
            description=stem,
            emotion_tags=(stem,),
            marker=f"[表情包：{stem}]",
            cached_path=file_path,
        )

    with patch(
        "plugins.xiaoqing_chat.media.event_media.render_local_media_file",
        new=AsyncMock(side_effect=_fake_render),
    ) as mock_render:
        first_entries = await load_emoji_library(mock_context, runtime)
        second_entries = await load_emoji_library(mock_context, runtime)

    assert {entry.file_path for entry in first_entries} == {
        "media/library/无语猫猫.png",
        "media/library/开心狗狗.png",
    }
    assert len(second_entries) == 2
    assert mock_render.await_count == 2


@pytest.mark.asyncio
async def test_load_emoji_library_rebuilds_bad_existing_metadata(mock_context):
    library_dir = mock_context.data_dir / "media" / "library"
    library_dir.mkdir(parents=True, exist_ok=True)
    image_path = _write_png(library_dir / "坏条目.png")
    media_hash = hashlib.sha256(image_path.read_bytes()).hexdigest()
    runtime = _make_media_runtime()
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
            media_hash=media_hash,
            kind="emoji",
            description="一只黑色小鸟站着，眼神疑惑",
            emotion_tags=("疑惑", "震惊"),
            marker="[表情包：疑惑，震惊]",
            cached_path=file_path,
        )

    with patch(
        "plugins.xiaoqing_chat.media.event_media.render_local_media_file",
        new=AsyncMock(side_effect=_fake_render),
    ) as mock_render:
        entries = await load_emoji_library(mock_context, runtime)

    assert len(entries) == 1
    assert entries[0].description == "一只黑色小鸟站着，眼神疑惑"
    assert entries[0].marker == "[表情包：疑惑，震惊]"
    mock_render.assert_awaited_once()
    saved = json.loads((library_dir / "index.json").read_text(encoding="utf-8"))
    assert saved["entries"][media_hash]["marker"] == "[表情包：疑惑，震惊]"


@pytest.mark.asyncio
async def test_load_emoji_library_schedules_background_repair_without_blocking(mock_context):
    library_dir = mock_context.data_dir / "media" / "library"
    library_dir.mkdir(parents=True, exist_ok=True)
    image_path = _write_png(library_dir / "坏条目.png")
    media_hash = hashlib.sha256(image_path.read_bytes()).hexdigest()
    runtime = _make_media_runtime()
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
            repair_invalid=False,
            schedule_background_repair=True,
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
    runtime = _make_media_runtime()
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
    runtime = _make_media_runtime()
    source_path = _write_png(mock_context.data_dir / "bad_emoji.png")
    rendered = RenderedMedia(
        media_hash=hashlib.sha256(source_path.read_bytes()).hexdigest(),
        kind="emoji",
        description='json\n{"kind":"emoji"}',
        emotion_tags=("json", "kind"),
        marker="[表情包：json，kind]",
        cached_path=source_path,
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
    runtime = _make_media_runtime()
    source_path = _write_png(mock_context.data_dir / "emoji_source" / "source.png")
    outside_path = mock_context.plugin_dir.parent / "outside.png"
    index_path = library_dir / "index.json"
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
        media_hash="hash-safe",
        kind="emoji",
        description="猫猫无语摊手",
        emotion_tags=("无语",),
        marker="[表情包：无语]",
        cached_path=source_path,
    )

    collected = collect_emoji_candidate(mock_context, runtime, rendered, source_path=source_path)
    persisted = json.loads(index_path.read_text(encoding="utf-8"))
    stored_rel = persisted["entries"]["hash-safe"]["file_path"]
    stored_path = (mock_context.data_dir / stored_rel).resolve()

    assert collected is not None
    assert stored_path.exists()
    assert stored_path.parent == library_dir.resolve()
    assert not outside_path.exists()


def test_collect_emoji_candidate_prunes_old_auto_entries(mock_context):
    from PIL import Image

    runtime = _make_media_runtime(emoji_auto_collect_max_entries=1)
    first_path = mock_context.data_dir / "first_auto.png"
    second_path = mock_context.data_dir / "second_auto.png"
    first_image = Image.new("RGBA", (24, 24), (255, 255, 255, 255))
    second_image = Image.new("RGBA", (24, 24), (255, 255, 255, 255))
    for index in range(24):
        first_image.putpixel((index, index), (255, 120, 120, 255))
        second_image.putpixel((23 - index, index), (120, 120, 255, 255))
    first_image.save(first_path)
    second_image.save(second_path)

    first_rendered = RenderedMedia(
        media_hash=hashlib.sha256(first_path.read_bytes()).hexdigest(),
        kind="emoji",
        description="红色小鸟翻白眼",
        emotion_tags=("无语",),
        marker="[表情包：无语]",
        cached_path=first_path,
    )
    second_rendered = RenderedMedia(
        media_hash=hashlib.sha256(second_path.read_bytes()).hexdigest(),
        kind="emoji",
        description="蓝色小鸟生气",
        emotion_tags=("生气",),
        marker="[表情包：生气]",
        cached_path=second_path,
    )

    assert collect_emoji_candidate(mock_context, runtime, first_rendered, source_path=first_path) is not None
    assert collect_emoji_candidate(mock_context, runtime, second_rendered, source_path=second_path) is not None

    saved = json.loads((mock_context.data_dir / "media" / "library" / "index.json").read_text(encoding="utf-8"))
    assert list(saved["entries"].keys()) == [second_rendered.media_hash]
    assert not (mock_context.data_dir / "media" / "library" / f"{first_rendered.media_hash}.png").exists()


@pytest.mark.asyncio
async def test_collect_emoji_candidate_requires_approval_keeps_entry_pending(mock_context):
    runtime = _make_media_runtime(emoji_auto_collect_requires_approval=True)
    source_path = _write_png(mock_context.data_dir / "pending_emoji.png")
    rendered = RenderedMedia(
        media_hash=hashlib.sha256(source_path.read_bytes()).hexdigest(),
        kind="emoji",
        description="猫猫翻白眼",
        emotion_tags=("无语",),
        marker="[表情包：无语]",
        cached_path=source_path,
    )

    collected = collect_emoji_candidate(mock_context, runtime, rendered, source_path=source_path)
    entries = await load_emoji_library(mock_context, runtime, repair_invalid=False)
    saved = json.loads((mock_context.data_dir / "media" / "library" / "index.json").read_text(encoding="utf-8"))

    assert collected is not None
    assert entries == []
    assert saved["entries"][rendered.media_hash]["status"] == "pending"
    assert "/pending/" in saved["entries"][rendered.media_hash]["file_path"]


def test_collect_emoji_candidate_dedups_visually_identical_auto_entries(mock_context):
    from PIL import Image, PngImagePlugin

    runtime = _make_media_runtime(emoji_auto_collect_similarity_threshold=0)
    first_path = mock_context.data_dir / "dup1.png"
    second_path = mock_context.data_dir / "dup2.png"

    image = Image.new("RGBA", (24, 24), (255, 255, 255, 255))
    image.putpixel((2, 2), (0, 0, 0, 255))
    image.save(first_path)
    pnginfo = PngImagePlugin.PngInfo()
    pnginfo.add_text("note", "same-pixels-different-bytes")
    image.save(second_path, pnginfo=pnginfo)

    first_rendered = RenderedMedia(
        media_hash=hashlib.sha256(first_path.read_bytes()).hexdigest(),
        kind="emoji",
        description="猫猫无语",
        emotion_tags=("无语",),
        marker="[表情包：无语]",
        cached_path=first_path,
    )
    second_rendered = RenderedMedia(
        media_hash=hashlib.sha256(second_path.read_bytes()).hexdigest(),
        kind="emoji",
        description="猫猫无语",
        emotion_tags=("无语",),
        marker="[表情包：无语]",
        cached_path=second_path,
    )

    first = collect_emoji_candidate(mock_context, runtime, first_rendered, source_path=first_path)
    second = collect_emoji_candidate(mock_context, runtime, second_rendered, source_path=second_path)
    saved = json.loads((mock_context.data_dir / "media" / "library" / "index.json").read_text(encoding="utf-8"))

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


def test_resolve_media_llm_secrets_uses_default_vision_provider_from_secrets(mock_context):
    runtime = _make_media_runtime()
    mock_context.secrets["plugins"]["xiaoqing_chat"]["vision"]["default"] = "glm-4v"

    secrets = _resolve_media_llm_secrets(mock_context, runtime)

    assert secrets["api_base"] == "https://open.bigmodel.cn/api/paas/v4"
    assert secrets["api_key"] == "vision-key"
    assert secrets["model"] == "glm-4v"
    assert secrets["endpoint_path"] == "/chat/completions"
    assert secrets["_vision_enabled"] is True
    assert secrets["_provider_scope"] == "vision_default"


def test_resolve_media_llm_secrets_preserves_vision_extra_payload(mock_context):
    runtime = _make_media_runtime(vision_provider="glm-4v")
    provider = mock_context.secrets["plugins"]["xiaoqing_chat"]["vision"]["providers"]["glm-4v"]
    provider["thinking"] = {"type": "enabled"}
    provider["extra_payload"] = {"response_format": {"type": "json_object"}}
    provider["fallbacks"] = ["glm-4v-flash"]

    secrets = _resolve_media_llm_secrets(mock_context, runtime)

    assert secrets["_extra_payload"] == {
        "thinking": {"type": "enabled"},
        "response_format": {"type": "json_object"},
    }
    assert "fallbacks" not in secrets["_extra_payload"]


@pytest.mark.asyncio
async def test_render_event_media_text_passes_vision_extra_payload(mock_context):
    runtime = _make_media_runtime(vision_provider="glm-4v")
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
    with patch(
        "plugins.xiaoqing_chat.media.event_media._download_url_bytes",
        new=AsyncMock(return_value=(_PNG_BYTES, "image/png")),
    ), patch(
        "plugins.xiaoqing_chat.media.event_media_analysis.chat_completions_raw_with_fallback_paths",
        new=AsyncMock(side_effect=_fake_chat_raw),
    ):
        text = await render_event_media_text(event, context=mock_context, runtime=runtime)

    assert text == "[图片：海边落日]"
    assert captured["extra_payload"] == {
        "thinking": {"type": "enabled"},
        "response_format": {"type": "json_object"},
    }


@pytest.mark.asyncio
async def test_download_url_bytes_streams_and_enforces_timeout(mock_context):
    response = SimpleNamespace(body=b"1234", headers={"Content-Type": "image/png"})
    with patch(
        "plugins.xiaoqing_chat.media.event_media.fetch_public_bytes",
        new=AsyncMock(return_value=response),
    ) as fetch:
        payload, content_type = await _download_url_bytes(
            "https://example.com/test.png",
            context=mock_context,
            max_bytes=8,
        )

    assert payload == b"1234"
    assert content_type == "image/png"
    assert fetch.await_args.kwargs["timeout_seconds"] == 20


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
                context=mock_context,
                max_bytes=5,
            )


def test_resolve_media_llm_secrets_uses_dedicated_vision_provider(mock_context):
    runtime = _make_media_runtime(vision_provider="glm-4v")

    secrets = _resolve_media_llm_secrets(mock_context, runtime)

    assert secrets["api_base"] == "https://open.bigmodel.cn/api/paas/v4"
    assert secrets["api_key"] == "vision-key"
    assert secrets["model"] == "glm-4v"
    assert secrets["endpoint_path"] == "/chat/completions"
    assert secrets["_vision_enabled"] is True
    assert secrets["_provider_scope"] == "vision"


def test_resolve_media_llm_secrets_does_not_reuse_chat_provider_without_explicit_vision(mock_context):
    runtime = _make_media_runtime()

    secrets = _resolve_media_llm_secrets(mock_context, runtime)

    assert secrets["api_base"] == ""
    assert secrets["api_key"] == ""
    assert secrets["model"] == ""
    assert secrets["_vision_enabled"] is False


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
async def test_render_event_media_falls_back_to_summary_marker_when_image_resolve_fails(mock_context):
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
        patch("plugins.xiaoqing_chat.media.event_media._render_cache_lock", return_value=nullcontext()),
        patch(
            "plugins.xiaoqing_chat.media.event_media._load_render_cache",
            return_value=first_cache,
        ) as mock_load,
        patch("plugins.xiaoqing_chat.media.event_media_common._render_cache_lock", return_value=nullcontext()),
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
    entries = await load_qq_face_catalog(mock_context, runtime)

    assert text == "[QQ表情：狂笑]"
    labels = {entry.label for entry in entries}
    assert "微笑" in labels
    assert "狂笑" in labels


@pytest.mark.asyncio
async def test_load_qq_face_catalog_loads_bundled_qface_labels(mock_context):
    runtime = _make_media_runtime()

    entries = await load_qq_face_catalog(mock_context, runtime)
    target = next(entry for entry in entries if entry.face_id == "300")

    assert target.label == "胖三斤"
    assert "胖三斤" in target.aliases


@pytest.mark.asyncio
async def test_load_qq_face_catalog_keeps_placeholder_for_unlabeled_face(mock_context):
    runtime = _make_media_runtime()
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

    entries = await load_qq_face_catalog(mock_context, runtime)
    placeholder = next(entry for entry in entries if entry.face_id == "999")
    persisted = json.loads(catalog_path.read_text(encoding="utf-8"))

    assert placeholder.label == "系统表情#999"
    assert "系统表情#999" in persisted["entries"]["999"]["labels"]


@pytest.mark.asyncio
async def test_render_local_media_file_merges_latest_cache_before_save(mock_context):
    runtime = _make_media_runtime(vision_provider="glm-4v")
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


@pytest.mark.asyncio
async def test_smalltalk_emoji_reply_returns_mixed_text_and_image_and_persists_marker(mock_context):
    from plugins.xiaoqing_chat.handlers import _maybe_reply_smalltalk

    runtime = _make_media_runtime(
        enable_inbound_media_context=False,
    )
    image_path = _write_png(mock_context.data_dir / "emoji_reply.png")

    state = MagicMock()
    state.get_mood_state.return_value = ""
    state.memory_store.get.return_value = []
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.memory_store.get_recent_async = AsyncMock(return_value=[])
    state.memory_store.append = Mock()
    state.heartflow.on_user_message_async = AsyncMock()
    state.heartflow.on_bot_reply_async = AsyncMock()
    state.heartflow.on_no_reply_async = AsyncMock()
    state.inc_stats = Mock()
    state.action_history.append = Mock()

    event = {
        "post_type": "message",
        "message_type": "group",
        "user_id": 12345,
        "group_id": 67890,
        "message_id": 1,
        "message": [{"type": "text", "data": {"text": "你好"}}],
        "_xc_command_forced": True,
    }
    hctx = SimpleNamespace(
        runtime=runtime,
        state=state,
        chat_id="g67890",
        bot_name="小青",
        secrets={"api_base": "http://test", "api_key": "key", "model": "model"},
        data_dir=mock_context.data_dir,
    )
    emoji_marker = SimpleNamespace(
        kind="emoji",
        entry=SimpleNamespace(file_path=str(image_path), media_hash="hash-1"),
        marker="[表情包：无语]",
        reasoning="emoji_tag:无语",
    )
    reply_draft = _reply_draft_with_parts(
        "懂了",
        (
            {"kind": "text", "text": "懂了"},
            {
                "kind": "emoji",
                "file_path": str(image_path),
                "media_hash": "hash-1",
                "marker": "[表情包：无语]",
            },
        ),
        media_marker=emoji_marker,
    )

    with (
        patch("plugins.xiaoqing_chat.handlers.HandlerContext.from_event", return_value=hctx),
        patch("plugins.xiaoqing_chat.handlers._get_lock", return_value=asyncio.Lock()),
        patch("plugins.xiaoqing_chat.handlers.build_effective_user_text", new=AsyncMock(return_value="你好")),
        patch("plugins.xiaoqing_chat.handlers._should_ignore_text", return_value=False),
        patch(
            "plugins.xiaoqing_chat.handlers._ensure_user_message_recorded",
            new=AsyncMock(return_value="u1"),
        ),
        patch("plugins.xiaoqing_chat.handlers.is_brain_chat_active", return_value=False),
        patch(
            "plugins.xiaoqing_chat.handlers._generate_reply_draft",
            new=AsyncMock(return_value=reply_draft),
        ),
        patch("plugins.xiaoqing_chat.smalltalk_media_helpers.mark_emoji_used"),
        patch("plugins.xiaoqing_chat.handlers._most_recent_user_local_id", return_value="u1"),
        patch("plugins.xiaoqing_chat.handlers._spawn_post_reply_bg_tasks", new=AsyncMock()),
        patch("plugins.xiaoqing_chat.handlers._schedule_memory_persist"),
        patch("plugins.xiaoqing_chat.handlers._schedule_action_history_flush"),
        patch("plugins.xiaoqing_chat.handlers._freq_record"),
        patch("plugins.xiaoqing_chat.handlers._log_step"),
    ):
        result = await _maybe_reply_smalltalk("你好", event, mock_context)

    assert mock_context.send_action.await_count == 0
    assert [segment["type"] for segment in result] == ["text", "emoji"]
    assert result[0]["data"]["text"] == "懂了"
    assistant_append = state.memory_store.append.call_args_list[-1]
    content, media_items = message_parts_to_legacy(assistant_append.kwargs["parts"])
    assert content == "懂了[[xc_media_1]]"
    assert media_items[0]["kind"] == "emoji"
    assert media_items[0]["media_hash"] == "hash-1"
    assert media_items[0]["marker"] == "[表情包：无语]"


@pytest.mark.asyncio
async def test_smalltalk_emoji_only_reply_returns_single_image_and_marker_memory(mock_context):
    from plugins.xiaoqing_chat.handlers import _maybe_reply_smalltalk

    runtime = _make_media_runtime(
        enable_inbound_media_context=False,
    )
    image_path = mock_context.data_dir / "media" / "library" / "emoji_only.png"
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image_path = _write_png(image_path)

    state = MagicMock()
    state.get_mood_state.return_value = ""
    state.memory_store.get.return_value = []
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.memory_store.get_recent_async = AsyncMock(return_value=[])
    state.memory_store.append = Mock()
    state.heartflow.on_user_message_async = AsyncMock()
    state.heartflow.on_bot_reply_async = AsyncMock()
    state.heartflow.on_no_reply_async = AsyncMock()
    state.inc_stats = Mock()
    state.action_history.append = Mock()

    event = {
        "post_type": "message",
        "message_type": "group",
        "user_id": 12345,
        "group_id": 67890,
        "message_id": 1,
        "message": [{"type": "image", "data": {"url": "https://example.com/sticker.png"}}],
        "_xc_command_forced": True,
    }
    hctx = SimpleNamespace(
        runtime=runtime,
        state=state,
        chat_id="g67890",
        bot_name="小青",
        secrets={"api_base": "http://test", "api_key": "key", "model": "model"},
        data_dir=mock_context.data_dir,
    )
    emoji_marker = SimpleNamespace(
        kind="emoji",
        entry=SimpleNamespace(file_path=str(image_path), media_hash="hash-1"),
        marker="[表情包：无语]",
        reasoning="emoji_mode:emoji_only;emoji_tag:无语",
        mode="emoji_only",
    )
    reply_draft = _reply_draft_with_parts(
        "笑死",
        (
            {
                "kind": "emoji",
                "file_path": str(image_path),
                "media_hash": "hash-1",
                "marker": "[表情包：无语]",
                "mode": "emoji_only",
            },
        ),
        media_marker=emoji_marker,
    )

    with (
        patch("plugins.xiaoqing_chat.handlers.HandlerContext.from_event", return_value=hctx),
        patch("plugins.xiaoqing_chat.handlers._get_lock", return_value=asyncio.Lock()),
        patch(
            "plugins.xiaoqing_chat.handlers.build_effective_user_text",
            new=AsyncMock(return_value="[表情包：无语]"),
        ),
        patch("plugins.xiaoqing_chat.handlers._should_ignore_text", return_value=False),
        patch(
            "plugins.xiaoqing_chat.handlers._ensure_user_message_recorded",
            new=AsyncMock(return_value="u1"),
        ),
        patch("plugins.xiaoqing_chat.handlers.is_brain_chat_active", return_value=False),
        patch(
            "plugins.xiaoqing_chat.handlers._generate_reply_draft",
            new=AsyncMock(return_value=reply_draft),
        ),
        patch("plugins.xiaoqing_chat.smalltalk_media_helpers.mark_emoji_used"),
        patch("plugins.xiaoqing_chat.handlers._most_recent_user_local_id", return_value="u1"),
        patch("plugins.xiaoqing_chat.handlers._spawn_post_reply_bg_tasks", new=AsyncMock()),
        patch("plugins.xiaoqing_chat.handlers._schedule_memory_persist"),
        patch("plugins.xiaoqing_chat.handlers._schedule_action_history_flush"),
        patch("plugins.xiaoqing_chat.handlers._freq_record"),
        patch("plugins.xiaoqing_chat.handlers._log_step"),
    ):
        result = await _maybe_reply_smalltalk("", event, mock_context)

    assert mock_context.send_action.await_count == 0
    assert result[0]["type"] == "emoji"
    assistant_append = state.memory_store.append.call_args_list[-1]
    content, media_items = message_parts_to_legacy(assistant_append.kwargs["parts"])
    assert content == "[[xc_media_1]]"
    assert media_items[0]["mode"] == "emoji_only"


@pytest.mark.asyncio
async def test_smalltalk_face_reply_returns_mixed_text_and_face_and_persists_marker(mock_context):
    from plugins.xiaoqing_chat.handlers import _maybe_reply_smalltalk

    runtime = _make_media_runtime(
        enable_inbound_media_context=False,
    )

    state = MagicMock()
    state.get_mood_state.return_value = ""
    state.memory_store.get.return_value = []
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.memory_store.get_recent_async = AsyncMock(return_value=[])
    state.memory_store.append = Mock()
    state.heartflow.on_user_message_async = AsyncMock()
    state.heartflow.on_bot_reply_async = AsyncMock()
    state.heartflow.on_no_reply_async = AsyncMock()
    state.inc_stats = Mock()
    state.action_history.append = Mock()

    event = {
        "post_type": "message",
        "message_type": "group",
        "user_id": 12345,
        "group_id": 67890,
        "message_id": 1,
        "message": [{"type": "text", "data": {"text": "你好"}}],
        "_xc_command_forced": True,
    }
    hctx = SimpleNamespace(
        runtime=runtime,
        state=state,
        chat_id="g67890",
        bot_name="小青",
        secrets={"api_base": "http://test", "api_key": "key", "model": "model"},
        data_dir=mock_context.data_dir,
    )
    face_marker = SimpleNamespace(
        kind="qq_face",
        entry=SimpleNamespace(face_id="277"),
        marker="[QQ表情：狗头]",
        reasoning="face_mode:text_with_face;face_label:狗头",
        mode="text_with_face",
    )
    reply_draft = _reply_draft_with_parts(
        "懂了",
        (
            {"kind": "text", "text": "懂了"},
            {
                "kind": "qq_face",
                "face_id": "277",
                "marker": "[QQ表情：狗头]",
                "mode": "text_with_face",
            },
        ),
        media_marker=face_marker,
    )

    with (
        patch("plugins.xiaoqing_chat.handlers.HandlerContext.from_event", return_value=hctx),
        patch("plugins.xiaoqing_chat.handlers._get_lock", return_value=asyncio.Lock()),
        patch("plugins.xiaoqing_chat.handlers.build_effective_user_text", new=AsyncMock(return_value="你好")),
        patch("plugins.xiaoqing_chat.handlers._should_ignore_text", return_value=False),
        patch(
            "plugins.xiaoqing_chat.handlers._ensure_user_message_recorded",
            new=AsyncMock(return_value="u1"),
        ),
        patch("plugins.xiaoqing_chat.handlers.is_brain_chat_active", return_value=False),
        patch(
            "plugins.xiaoqing_chat.handlers._generate_reply_draft",
            new=AsyncMock(return_value=reply_draft),
        ),
        patch("plugins.xiaoqing_chat.smalltalk_media_helpers.mark_qq_face_used"),
        patch("plugins.xiaoqing_chat.handlers._most_recent_user_local_id", return_value="u1"),
        patch("plugins.xiaoqing_chat.handlers._spawn_post_reply_bg_tasks", new=AsyncMock()),
        patch("plugins.xiaoqing_chat.handlers._schedule_memory_persist"),
        patch("plugins.xiaoqing_chat.handlers._schedule_action_history_flush"),
        patch("plugins.xiaoqing_chat.handlers._freq_record"),
        patch("plugins.xiaoqing_chat.handlers._log_step"),
    ):
        result = await _maybe_reply_smalltalk("你好", event, mock_context)

    assert mock_context.send_action.await_count == 0
    assert [segment["type"] for segment in result] == ["text", "face"]
    assert result[0]["data"]["text"] == "懂了"
    assert result[1]["data"]["id"] == "277"
    assistant_append = state.memory_store.append.call_args_list[-1]
    content, media_items = message_parts_to_legacy(assistant_append.kwargs["parts"])
    assert content == "懂了[[xc_media_1]]"
    assert media_items[0]["kind"] == "qq_face"
    assert media_items[0]["face_id"] == "277"


@pytest.mark.asyncio
async def test_smalltalk_reply_applies_only_one_outbound_media_plan(mock_context):
    from plugins.xiaoqing_chat.handlers import _maybe_reply_smalltalk

    runtime = _make_media_runtime(
        enable_inbound_media_context=False,
    )
    image_path = mock_context.data_dir / "media" / "library" / "mixed_reply.png"
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image_path = _write_png(image_path)

    state = MagicMock()
    state.get_mood_state.return_value = ""
    state.memory_store.get.return_value = []
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.memory_store.get_recent_async = AsyncMock(return_value=[])
    state.memory_store.append = Mock()
    state.heartflow.on_user_message_async = AsyncMock()
    state.heartflow.on_bot_reply_async = AsyncMock()
    state.heartflow.on_no_reply_async = AsyncMock()
    state.inc_stats = Mock()
    state.action_history.append = Mock()

    event = {
        "post_type": "message",
        "message_type": "group",
        "user_id": 12345,
        "group_id": 67890,
        "message_id": 1,
        "message": [{"type": "text", "data": {"text": "你好"}}],
        "_xc_command_forced": True,
    }
    hctx = SimpleNamespace(
        runtime=runtime,
        state=state,
        chat_id="g67890",
        bot_name="小青",
        secrets={"api_base": "http://test", "api_key": "key", "model": "model"},
        data_dir=mock_context.data_dir,
    )
    emoji_marker = SimpleNamespace(
        kind="emoji",
        entry=SimpleNamespace(file_path=str(image_path), media_hash="hash-1", description="无语", emotion_tags=("无语",)),
        marker="[表情包：无语]",
        reasoning="emoji_mode:text_with_emoji;emoji_tag:无语",
        mode="text_with_emoji",
    )
    reply_draft = _reply_draft_with_parts(
        "懂了\n你看这个\n再说",
        (
            {"kind": "text", "text": "懂了"},
            {
                "kind": "emoji",
                "file_path": str(image_path),
                "media_hash": "hash-1",
                "marker": "[表情包：无语]",
                "description": "无语",
                "emotion_tags": ["无语"],
                "mode": "text_with_emoji",
            },
            {"kind": "text", "text": "\n你看这个\n再说"},
        ),
        media_marker=emoji_marker,
    )

    with (
        patch("plugins.xiaoqing_chat.handlers.HandlerContext.from_event", return_value=hctx),
        patch("plugins.xiaoqing_chat.handlers._get_lock", return_value=asyncio.Lock()),
        patch("plugins.xiaoqing_chat.handlers.build_effective_user_text", new=AsyncMock(return_value="你好")),
        patch("plugins.xiaoqing_chat.handlers._should_ignore_text", return_value=False),
        patch(
            "plugins.xiaoqing_chat.handlers._ensure_user_message_recorded",
            new=AsyncMock(return_value="u1"),
        ),
        patch("plugins.xiaoqing_chat.handlers.is_brain_chat_active", return_value=False),
        patch(
            "plugins.xiaoqing_chat.handlers._generate_reply_draft",
            new=AsyncMock(return_value=reply_draft),
        ),
        patch("plugins.xiaoqing_chat.smalltalk_media_helpers.mark_emoji_used"),
        patch("plugins.xiaoqing_chat.smalltalk_media_helpers.mark_qq_face_used"),
        patch("plugins.xiaoqing_chat.handlers._most_recent_user_local_id", return_value="u1"),
        patch("plugins.xiaoqing_chat.handlers._spawn_post_reply_bg_tasks", new=AsyncMock()),
        patch("plugins.xiaoqing_chat.handlers._schedule_memory_persist"),
        patch("plugins.xiaoqing_chat.handlers._schedule_action_history_flush"),
        patch("plugins.xiaoqing_chat.handlers._freq_record"),
        patch("plugins.xiaoqing_chat.handlers._log_step"),
    ):
        result = await _maybe_reply_smalltalk("你好", event, mock_context)

    assert mock_context.send_action.await_count == 0
    assert [segment["type"] for segment in result] == ["text", "emoji", "text"]
    assert result[0]["data"]["text"] == "懂了"
    assert result[2]["data"]["text"] == "你看这个\n再说"
    assistant_append = state.memory_store.append.call_args_list[-1]
    content, media_items = message_parts_to_legacy(assistant_append.kwargs["parts"])
    assert content == "懂了[[xc_media_1]]\n你看这个\n再说"
    assert [item["kind"] for item in media_items] == ["emoji"]
    assert [part["kind"] for part in assistant_append.kwargs["parts"]] == ["text", "emoji", "text"]
    assert media_items[0]["media_hash"] == "hash-1"


@pytest.mark.asyncio
async def test_smalltalk_face_only_reply_returns_single_face_and_marker_memory(mock_context):
    from plugins.xiaoqing_chat.handlers import _maybe_reply_smalltalk

    runtime = _make_media_runtime(
        enable_inbound_media_context=False,
    )

    state = MagicMock()
    state.get_mood_state.return_value = ""
    state.memory_store.get.return_value = []
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.memory_store.get_recent_async = AsyncMock(return_value=[])
    state.memory_store.append = Mock()
    state.heartflow.on_user_message_async = AsyncMock()
    state.heartflow.on_bot_reply_async = AsyncMock()
    state.heartflow.on_no_reply_async = AsyncMock()
    state.inc_stats = Mock()
    state.action_history.append = Mock()

    event = {
        "post_type": "message",
        "message_type": "private",
        "user_id": 12345,
        "message_id": 1,
        "message": [{"type": "text", "data": {"text": "你好"}}],
        "_xc_command_forced": True,
    }
    hctx = SimpleNamespace(
        runtime=runtime,
        state=state,
        chat_id="u12345",
        bot_name="小青",
        secrets={"api_base": "http://test", "api_key": "key", "model": "model"},
        data_dir=mock_context.data_dir,
    )
    face_marker = SimpleNamespace(
        kind="qq_face",
        entry=SimpleNamespace(face_id="14"),
        marker="[QQ表情：微笑]",
        reasoning="face_mode:face_only;face_label:微笑",
        mode="face_only",
    )
    reply_draft = _reply_draft_with_parts(
        "懂了",
        (
            {
                "kind": "qq_face",
                "face_id": "14",
                "marker": "[QQ表情：微笑]",
                "mode": "face_only",
            },
        ),
        media_marker=face_marker,
    )

    with (
        patch("plugins.xiaoqing_chat.handlers.HandlerContext.from_event", return_value=hctx),
        patch("plugins.xiaoqing_chat.handlers._get_lock", return_value=asyncio.Lock()),
        patch("plugins.xiaoqing_chat.handlers.build_effective_user_text", new=AsyncMock(return_value="你好")),
        patch("plugins.xiaoqing_chat.handlers._should_ignore_text", return_value=False),
        patch(
            "plugins.xiaoqing_chat.handlers._ensure_user_message_recorded",
            new=AsyncMock(return_value="u1"),
        ),
        patch("plugins.xiaoqing_chat.handlers.is_brain_chat_active", return_value=False),
        patch(
            "plugins.xiaoqing_chat.handlers._generate_reply_draft",
            new=AsyncMock(return_value=reply_draft),
        ),
        patch("plugins.xiaoqing_chat.smalltalk_media_helpers.mark_qq_face_used"),
        patch("plugins.xiaoqing_chat.handlers._most_recent_user_local_id", return_value="u1"),
        patch("plugins.xiaoqing_chat.handlers._spawn_post_reply_bg_tasks", new=AsyncMock()),
        patch("plugins.xiaoqing_chat.handlers._schedule_memory_persist"),
        patch("plugins.xiaoqing_chat.handlers._schedule_action_history_flush"),
        patch("plugins.xiaoqing_chat.handlers._freq_record"),
        patch("plugins.xiaoqing_chat.handlers._log_step"),
    ):
        result = await _maybe_reply_smalltalk("你好", event, mock_context)

    assert mock_context.send_action.await_count == 0
    assert result[0]["type"] == "face"
    assert result[0]["data"]["id"] == "14"
    assistant_append = state.memory_store.append.call_args_list[-1]
    content, media_items = message_parts_to_legacy(assistant_append.kwargs["parts"])
    assert content == "[[xc_media_1]]"
    assert media_items[0]["mode"] == "face_only"


@pytest.mark.asyncio
async def test_smalltalk_does_not_force_reply_when_new_emoji_collected(mock_context):
    from plugins.xiaoqing_chat.handlers import _maybe_reply_smalltalk

    runtime = _make_media_runtime(
        enable_inbound_media_context=True,
    )
    state = MagicMock()
    state.get_mood_state.return_value = ""
    state.memory_store.get.return_value = []
    state.memory_store.get_async = AsyncMock(return_value=[])
    state.memory_store.get_recent_async = AsyncMock(return_value=[])
    state.memory_store.append = Mock()
    state.heartflow.on_user_message_async = AsyncMock()
    state.heartflow.on_bot_reply_async = AsyncMock()
    state.heartflow.on_no_reply_async = AsyncMock()
    state.inc_stats = Mock()
    state.action_history.append = Mock()

    event = {
        "post_type": "message",
        "message_type": "group",
        "user_id": 12345,
        "group_id": 67890,
        "message_id": 1,
        "message": [{"type": "image", "data": {"url": "https://example.com/sticker.png"}}],
        "_xc_new_emoji_count": 1,
    }
    hctx = SimpleNamespace(
        runtime=runtime,
        state=state,
        chat_id="g67890",
        bot_name="小青",
        secrets={"api_base": "http://test", "api_key": "key", "model": "model"},
        data_dir=mock_context.data_dir,
    )

    with (
        patch("plugins.xiaoqing_chat.handlers.HandlerContext.from_event", return_value=hctx),
        patch("plugins.xiaoqing_chat.handlers._get_lock", return_value=asyncio.Lock()),
        patch(
            "plugins.xiaoqing_chat.handlers.build_effective_user_text",
            new=AsyncMock(return_value="[表情包：无语]"),
        ),
        patch("plugins.xiaoqing_chat.handlers._should_ignore_text", return_value=False),
        patch("plugins.xiaoqing_chat.handlers._should_reply", new=AsyncMock(return_value=False)),
        patch(
            "plugins.xiaoqing_chat.handlers._ensure_user_message_recorded",
            new=AsyncMock(return_value="u1"),
        ),
        patch("plugins.xiaoqing_chat.handlers.is_brain_chat_active", return_value=False),
        patch(
            "plugins.xiaoqing_chat.handlers._generate_reply_draft",
            new=AsyncMock(return_value=_reply_draft("好图，收了")),
        ),
        patch("plugins.xiaoqing_chat.handlers._most_recent_user_local_id", return_value="u1"),
        patch("plugins.xiaoqing_chat.handlers._spawn_post_reply_bg_tasks", new=AsyncMock()),
        patch("plugins.xiaoqing_chat.handlers._schedule_memory_persist"),
        patch("plugins.xiaoqing_chat.handlers._schedule_action_history_flush"),
        patch("plugins.xiaoqing_chat.handlers._freq_record"),
        patch("plugins.xiaoqing_chat.handlers._log_step"),
    ):
        result = await _maybe_reply_smalltalk("", event, mock_context)

    assert result == []
    assert state.heartflow.on_no_reply_async.await_count == 1
