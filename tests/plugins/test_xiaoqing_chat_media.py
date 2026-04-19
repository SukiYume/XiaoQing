import asyncio
import base64
import hashlib
import json
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from plugins.xiaoqing_chat.frequency_control import _score_interest
from plugins.xiaoqing_chat.media.emoji_library import (
    EmojiLibraryEntry,
    collect_emoji_candidate,
    load_emoji_library,
)
from plugins.xiaoqing_chat.media.emoji_reply import plan_emoji_reply
from plugins.xiaoqing_chat.media.qq_face_catalog import load_qq_face_catalog
from plugins.xiaoqing_chat.media.qq_face_reply import plan_qq_face_reply
from plugins.xiaoqing_chat.media.event_media import (
    RenderedMedia,
    _download_url_bytes,
    _looks_like_structured_media_text,
    _media_llm_max_tokens,
    _onebot_api_post,
    _prepare_media_for_llm,
    _resolve_media_llm_secrets,
    build_effective_user_text,
    render_event_media_text,
    render_local_media_file,
)
from plugins.xiaoqing_chat.llm.llm_client import LLMError
from plugins.xiaoqing_chat.memory.memory import StoredMessage


_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO7Z0m8AAAAASUVORK5CYII="
)


def _write_png(path: Path) -> Path:
    path.write_bytes(_PNG_BYTES)
    return path


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


def _make_media_runtime(**media_overrides):
    media_cfg = SimpleNamespace(
        enable_inbound_media_context=True,
        enable_outbound_emoji_reply=True,
        enable_outbound_face_reply=True,
        emoji_library_dir="figures/library",
        emoji_reply_probability=1.0,
        emoji_candidate_count=4,
        emoji_cooldown_turns=3,
        face_reply_probability=1.0,
        face_candidate_count=6,
        face_cooldown_turns=2,
        max_media_per_message=3,
        max_analyze_bytes=1024 * 1024,
        vision_provider="",
        vision_api_base="",
        vision_api_key="",
        vision_model="",
        vision_endpoint_path="",
        vision_proxy="",
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
            emotion_tags=tuple(),
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


@pytest.mark.asyncio
async def test_render_local_media_file_retries_generic_llm_cache_on_next_send(mock_context):
    runtime = _make_media_runtime(vision_provider="glm-4v")
    image_path = _write_png(mock_context.data_dir / "sticker_retry_probe.jpg")

    generic_render = RenderedMedia(
        media_hash="",
        kind="emoji",
        description="动画表情",
        emotion_tags=tuple(),
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

    async def _fake_chat_completions(*, model, messages, **kwargs):
        used_models.append(model)
        if model in {"glm-4.6v-flash", "glm-4.6v"}:
            raise LLMError('retryable_http_429:{"error":{"code":"1305","message":"busy"}}')
        content = messages[1]["content"]
        if isinstance(content, list):
            return json.dumps(
                {
                    "kind": "emoji",
                    "detailed_description": "一只猫皱着脸，配字是苦鲁西",
                    "visible_text": "苦鲁西",
                    "emotion_tags": ["委屈", "难受"],
                },
                ensure_ascii=False,
            )
        return json.dumps({"description": "委屈猫猫苦鲁西", "emotion_tags": ["委屈", "难受"]}, ensure_ascii=False)

    with patch(
        "plugins.xiaoqing_chat.media.event_media._download_url_bytes",
        new=AsyncMock(return_value=(_PNG_BYTES, "image/png")),
    ), patch(
        "plugins.xiaoqing_chat.media.event_media.chat_completions",
        new=AsyncMock(side_effect=_fake_chat_completions),
    ):
        text = await render_event_media_text(event, context=mock_context, runtime=runtime)

    assert text == "[表情包：委屈，难受]"
    assert used_models == [
        "glm-4.6v-flash",
        "glm-4.6v",
        "glm-4v-flash",
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

    async def _fake_chat_completions(*, messages, **kwargs):
        content = messages[1]["content"]
        if isinstance(content, list):
            return json.dumps(
                {
                    "kind": "emoji",
                    "detailed_description": "一只猫皱着脸，配字是苦鲁西",
                    "visible_text": "苦鲁西",
                    "emotion_tags": ["委屈", "难受"],
                },
                ensure_ascii=False,
            )
        return json.dumps({"description": "动画表情", "emotion_tags": []}, ensure_ascii=False)

    with patch(
        "plugins.xiaoqing_chat.media.event_media._download_url_bytes",
        new=AsyncMock(return_value=(_PNG_BYTES, "image/png")),
    ), patch(
        "plugins.xiaoqing_chat.media.event_media.chat_completions",
        new=AsyncMock(side_effect=_fake_chat_completions),
    ):
        text = await render_event_media_text(event, context=mock_context, runtime=runtime)

    assert text == "[表情包：委屈，难受]"


@pytest.mark.asyncio
async def test_render_event_media_text_logs_summary_fallback_when_detail_empty(mock_context):
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

    async def _fake_chat_completions(*, messages, **kwargs):
        content = messages[1]["content"]
        if isinstance(content, list):
            return json.dumps({"kind": "emoji", "description": "", "emotion_tags": []}, ensure_ascii=False)
        return json.dumps({"description": "", "emotion_tags": []}, ensure_ascii=False)

    with patch(
        "plugins.xiaoqing_chat.media.event_media._download_url_bytes",
        new=AsyncMock(return_value=(_PNG_BYTES, "image/png")),
    ), patch(
        "plugins.xiaoqing_chat.media.event_media.chat_completions",
        new=AsyncMock(side_effect=_fake_chat_completions),
    ):
        text = await render_event_media_text(event, context=mock_context, runtime=runtime)

    assert text == "[表情包：动画表情]"
    log_lines = "\n".join(
        str(call.args[1]) if len(call.args) > 1 else ""
        for call in mock_context.logger.info.call_args_list
    )
    assert '"step": "media.analyze.ok"' in log_lines
    assert '"used_summary_fallback": "True"' in log_lines


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

    cached_files = list((mock_context.plugin_dir / "figures" / "inbox").glob("*"))
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

    cached_files = list((mock_context.plugin_dir / "figures" / "inbox").glob("*"))
    assert text.startswith("[图片：")
    assert cached_files


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
    assert list((mock_context.plugin_dir / "figures" / "library").glob("*"))


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

    async def _fake_chat_completions(*, messages, **kwargs):
        nonlocal captured_prompt
        content = messages[1]["content"]
        captured_prompt = content[0]["text"]
        image_url = content[1]["image_url"]["url"]
        captured_urls.append(image_url)
        return '{"kind":"emoji","description":"一只猫皱着脸，配字是苦鲁西","emotion_tags":["委屈","难受"]}'

    with patch(
        "plugins.xiaoqing_chat.media.event_media._download_url_bytes",
        new=AsyncMock(return_value=(_animated_gif_bytes(), "application/octet-stream")),
    ), patch(
        "plugins.xiaoqing_chat.media.event_media.chat_completions",
        new=AsyncMock(side_effect=_fake_chat_completions),
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
                emotion_tags=tuple(),
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
                emotion_tags=tuple(),
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

    async def _fake_onebot(context, action, payload):
        if action == "get_msg":
            return {
                "data": {
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
            }
        if action == "get_image":
            return {
                "data": {
                    "base64": base64.b64encode(_PNG_BYTES).decode("ascii"),
                    "file_name": "sticker.png",
                }
            }
        raise AssertionError(f"unexpected action: {action}")

    with patch(
        "plugins.xiaoqing_chat.media.event_media._onebot_api_post",
        new=AsyncMock(side_effect=_fake_onebot),
    ), patch(
        "plugins.xiaoqing_chat.media.event_media._analyze_media_with_llm",
        new=AsyncMock(return_value=None),
    ):
        text = await render_event_media_text(event, context=mock_context, runtime=runtime)

    assert text.startswith("[表情包：")
    assert event["_xc_new_emoji_count"] == 1


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
    library_dir = mock_context.plugin_dir / "emoji_library"
    library_dir.mkdir(parents=True, exist_ok=True)
    first_path = _write_png(library_dir / "无语猫猫.png")
    second_path = _write_png(library_dir / "开心狗狗.png")
    runtime = _make_media_runtime(emoji_library_dir=str(library_dir))

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
        "emoji_library/无语猫猫.png",
        "emoji_library/开心狗狗.png",
    }
    assert len(second_entries) == 2
    assert mock_render.await_count == 2


@pytest.mark.asyncio
async def test_load_emoji_library_rebuilds_bad_existing_metadata(mock_context):
    library_dir = mock_context.plugin_dir / "emoji_library_bad"
    library_dir.mkdir(parents=True, exist_ok=True)
    image_path = _write_png(library_dir / "坏条目.png")
    media_hash = hashlib.sha256(image_path.read_bytes()).hexdigest()
    runtime = _make_media_runtime(emoji_library_dir=str(library_dir))
    (library_dir / "index.json").write_text(
        json.dumps(
            {
                "entries": {
                    media_hash: {
                        "media_hash": media_hash,
                        "file_path": "emoji_library_bad/坏条目.png",
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
    assert not (mock_context.plugin_dir / "figures" / "library" / f"{rendered.media_hash}.png").exists()


def test_structured_media_text_detector_flags_think_and_json_noise():
    assert _looks_like_structured_media_text("<think>先分析一下</think>")
    assert _looks_like_structured_media_text('json\n{"kind":"emoji"}')
    assert not _looks_like_structured_media_text("一只黑色小鸟站着，眼神疑惑")


def test_media_llm_max_tokens_expands_for_thinking_models():
    assert _media_llm_max_tokens({"model": "glm-4.6v-flash"}, 200) == 200
    assert _media_llm_max_tokens({"model": "glm-4.1v-thinking-flash"}, 200) == 800
    assert _media_llm_max_tokens({"model": "glm-4.1v-thinking-flash"}, 120) == 480


def test_score_interest_treats_media_markers_as_meaningful():
    assert _score_interest("[图片：一只猫躺在桌上]") == "neutral"
    assert _score_interest("[表情包：无语]") == "high"
    assert _score_interest("[QQ表情：微笑]") == "neutral"


def test_resolve_media_llm_secrets_keeps_blank_key_for_direct_vision_config(mock_context):
    runtime = _make_media_runtime(
        vision_api_base="https://open.bigmodel.cn/api/paas/v4",
        vision_api_key="",
        vision_model="glm-4v",
        vision_endpoint_path="/chat/completions",
    )

    secrets = _resolve_media_llm_secrets(mock_context, runtime)

    assert secrets["api_base"] == "https://open.bigmodel.cn/api/paas/v4"


@pytest.mark.asyncio
async def test_download_url_bytes_streams_and_enforces_timeout(mock_context):
    captured = {}

    class _Stream:
        async def iter_chunked(self, _size):
            yield b"12"
            yield b"34"

    class _Response:
        headers = {"Content-Type": "image/png"}
        content = _Stream()

        def raise_for_status(self):
            return None

    class _ContextManager:
        async def __aenter__(self):
            return _Response()

        async def __aexit__(self, *args):
            return None

    class _Session:
        def get(self, *args, **kwargs):
            captured.update(kwargs)
            return _ContextManager()

    mock_context.http_session = _Session()

    payload, content_type = await _download_url_bytes(
        "https://example.com/test.png",
        context=mock_context,
        max_bytes=8,
    )

    assert payload == b"1234"
    assert content_type == "image/png"
    assert captured["timeout"].total == 20


@pytest.mark.asyncio
async def test_download_url_bytes_rejects_oversized_stream(mock_context):
    class _Stream:
        async def iter_chunked(self, _size):
            yield b"123"
            yield b"456"

    class _Response:
        headers = {"Content-Type": "image/png"}
        content = _Stream()

        def raise_for_status(self):
            return None

    class _ContextManager:
        async def __aenter__(self):
            return _Response()

        async def __aexit__(self, *args):
            return None

    class _Session:
        def get(self, *args, **kwargs):
            return _ContextManager()

    mock_context.http_session = _Session()

    with pytest.raises(ValueError, match="media too large"):
        await _download_url_bytes(
            "https://example.com/test.png",
            context=mock_context,
            max_bytes=5,
        )


@pytest.mark.asyncio
async def test_onebot_api_post_passes_timeout(mock_context):
    captured = {}

    class _Response:
        def raise_for_status(self):
            return None

        async def json(self, content_type=None):
            return {"data": {"ok": True}}

    class _ContextManager:
        async def __aenter__(self):
            return _Response()

        async def __aexit__(self, *args):
            return None

    class _Session:
        def post(self, *args, **kwargs):
            captured.update(kwargs)
            return _ContextManager()

    mock_context.http_session = _Session()
    mock_context.config["onebot_http_base"] = "http://localhost:5700"
    mock_context.secrets["onebot_token"] = "token"

    payload = await _onebot_api_post(mock_context, "get_msg", {"message_id": 1})

    assert payload == {"data": {"ok": True}}
    assert captured["timeout"].total == 15


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


@pytest.mark.asyncio
async def test_plan_emoji_reply_can_choose_emoji_only_mode(mock_context):
    runtime = _make_media_runtime()
    entry = EmojiLibraryEntry(
        media_hash="hash-1",
        file_path="figures/library/emoji.png",
        description="无语猫猫",
        emotion_tags=("无语",),
        usage_count=0,
        last_used_ts=0.0,
        marker="[表情包：无语]",
    )

    with (
        patch("plugins.xiaoqing_chat.media.emoji_reply.random.random", return_value=0.0),
        patch(
            "plugins.xiaoqing_chat.media.emoji_reply.load_emoji_library",
            new=AsyncMock(return_value=[entry]),
        ) as mock_load,
        patch(
            "plugins.xiaoqing_chat.media.emoji_reply.chat_completions",
            new=AsyncMock(return_value='{"mode":"emoji_only","tag":"无语","reason":"只发图更自然"}'),
        ),
    ):
        plan = await plan_emoji_reply(
            context=mock_context,
            runtime=runtime,
            history=[],
            user_text="[表情包：无语]",
            reply_text="笑死",
            secrets={"api_base": "http://test", "api_key": "key", "model": "model"},
        )

    assert plan is not None
    assert plan.mode == "emoji_only"
    assert plan.selected_tag == "无语"
    assert mock_load.await_args.kwargs["repair_invalid"] is False


@pytest.mark.asyncio
async def test_plan_emoji_reply_skips_when_library_empty(mock_context):
    runtime = _make_media_runtime()

    with (
        patch("plugins.xiaoqing_chat.media.emoji_reply.random.random", return_value=0.0),
        patch(
            "plugins.xiaoqing_chat.media.emoji_reply.load_emoji_library",
            new=AsyncMock(return_value=[]),
        ),
    ):
        plan = await plan_emoji_reply(
            context=mock_context,
            runtime=runtime,
            history=[],
            user_text="你看这个",
            reply_text="哈哈",
            secrets={"api_base": "http://test", "api_key": "key", "model": "model"},
        )

    assert plan is None


@pytest.mark.asyncio
async def test_plan_emoji_reply_skips_during_cooldown(mock_context):
    runtime = _make_media_runtime(emoji_cooldown_turns=3)
    history = [
        StoredMessage(role="assistant", name="小青", content="懂了\n[表情包：无语]", ts=1.0),
    ]

    with (
        patch("plugins.xiaoqing_chat.media.emoji_reply.random.random", return_value=0.0),
        patch(
            "plugins.xiaoqing_chat.media.emoji_reply.load_emoji_library",
            new=AsyncMock(side_effect=AssertionError("library should not be loaded during cooldown")),
        ),
    ):
        plan = await plan_emoji_reply(
            context=mock_context,
            runtime=runtime,
            history=history,
            user_text="你看这个",
            reply_text="哈哈",
            secrets={"api_base": "http://test", "api_key": "key", "model": "model"},
        )

    assert plan is None


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
async def test_plan_qq_face_reply_can_choose_text_with_face(mock_context):
    runtime = _make_media_runtime()

    with (
        patch("plugins.xiaoqing_chat.media.qq_face_reply.random.random", return_value=0.0),
        patch(
            "plugins.xiaoqing_chat.media.qq_face_reply.load_qq_face_catalog",
            new=AsyncMock(
                return_value=[
                    SimpleNamespace(
                        face_id="277",
                        label="狗头",
                        aliases=("狗头",),
                        usage_count=0,
                        last_used_ts=0.0,
                        marker="[QQ表情：狗头]",
                    )
                ]
            ),
        ),
        patch(
            "plugins.xiaoqing_chat.media.qq_face_reply.chat_completions",
            new=AsyncMock(return_value='{"mode":"text_with_face","face":"狗头","reason":"补个 face 更自然"}'),
        ),
    ):
        plan = await plan_qq_face_reply(
            context=mock_context,
            runtime=runtime,
            history=[],
            user_text="热死了",
            reply_text="又热了",
            secrets={"api_base": "http://test", "api_key": "key", "model": "model"},
        )

    assert plan is not None
    assert plan.mode == "text_with_face"
    assert plan.entry.face_id == "277"
    assert plan.marker == "[QQ表情：狗头]"


@pytest.mark.asyncio
async def test_plan_qq_face_reply_skips_during_cooldown(mock_context):
    runtime = _make_media_runtime(face_cooldown_turns=2)
    history = [
        StoredMessage(role="assistant", name="小青", content="懂了\n[QQ表情：微笑]", ts=1.0),
    ]

    with (
        patch("plugins.xiaoqing_chat.media.qq_face_reply.random.random", return_value=0.0),
        patch(
            "plugins.xiaoqing_chat.media.qq_face_reply.load_qq_face_catalog",
            new=AsyncMock(side_effect=AssertionError("catalog should not be loaded during cooldown")),
        ),
    ):
        plan = await plan_qq_face_reply(
            context=mock_context,
            runtime=runtime,
            history=history,
            user_text="你看这个",
            reply_text="哈哈",
            secrets={"api_base": "http://test", "api_key": "key", "model": "model"},
        )

    assert plan is None


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
            emotion_tags=tuple(),
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
async def test_smalltalk_emoji_reply_sends_text_then_image_and_persists_marker(mock_context):
    from plugins.xiaoqing_chat.handlers import _maybe_reply_smalltalk

    runtime = _make_media_runtime(enable_inbound_media_context=False, enable_outbound_emoji_reply=True)
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
    emoji_plan = SimpleNamespace(
        entry=SimpleNamespace(file_path=str(image_path), media_hash="hash-1"),
        marker="[表情包：无语]",
        reasoning="emoji_tag:无语",
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
        patch("plugins.xiaoqing_chat.handlers._generate_reply", new=AsyncMock(return_value="懂了")),
        patch("plugins.xiaoqing_chat.handlers.plan_emoji_reply", new=AsyncMock(return_value=emoji_plan)),
        patch("plugins.xiaoqing_chat.handlers.mark_emoji_used"),
        patch("plugins.xiaoqing_chat.handlers._most_recent_user_local_id", return_value="u1"),
        patch("plugins.xiaoqing_chat.handlers._spawn_post_reply_bg_tasks", new=AsyncMock()),
        patch("plugins.xiaoqing_chat.handlers._schedule_memory_persist"),
        patch("plugins.xiaoqing_chat.handlers._schedule_action_history_flush"),
        patch("plugins.xiaoqing_chat.handlers._freq_record"),
        patch("plugins.xiaoqing_chat.handlers._log_step"),
    ):
        result = await _maybe_reply_smalltalk("你好", event, mock_context)

    assert result == []
    assert mock_context.send_action.await_count == 2
    first_action = mock_context.send_action.await_args_list[0].args[0]
    second_action = mock_context.send_action.await_args_list[1].args[0]
    assert first_action["params"]["message"][0]["type"] == "text"
    assert first_action["params"]["message"][0]["data"]["text"] == "懂了"
    assert second_action["params"]["message"][0]["type"] == "image"
    assistant_append = state.memory_store.append.call_args_list[-1]
    assert assistant_append.kwargs["content"] == "懂了\n[表情包：无语]"


@pytest.mark.asyncio
async def test_smalltalk_emoji_only_reply_returns_single_image_and_marker_memory(mock_context):
    from plugins.xiaoqing_chat.handlers import _maybe_reply_smalltalk

    runtime = _make_media_runtime(enable_inbound_media_context=False, enable_outbound_emoji_reply=True)
    image_path = mock_context.plugin_dir / "figures" / "library" / "emoji_only.png"
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
    emoji_plan = SimpleNamespace(
        entry=SimpleNamespace(file_path=str(image_path), media_hash="hash-1"),
        marker="[表情包：无语]",
        reasoning="emoji_mode:emoji_only;emoji_tag:无语",
        mode="emoji_only",
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
        patch("plugins.xiaoqing_chat.handlers._generate_reply", new=AsyncMock(return_value="笑死")),
        patch("plugins.xiaoqing_chat.handlers.plan_emoji_reply", new=AsyncMock(return_value=emoji_plan)),
        patch("plugins.xiaoqing_chat.handlers.mark_emoji_used"),
        patch("plugins.xiaoqing_chat.handlers._most_recent_user_local_id", return_value="u1"),
        patch("plugins.xiaoqing_chat.handlers._spawn_post_reply_bg_tasks", new=AsyncMock()),
        patch("plugins.xiaoqing_chat.handlers._schedule_memory_persist"),
        patch("plugins.xiaoqing_chat.handlers._schedule_action_history_flush"),
        patch("plugins.xiaoqing_chat.handlers._freq_record"),
        patch("plugins.xiaoqing_chat.handlers._log_step"),
    ):
        result = await _maybe_reply_smalltalk("", event, mock_context)

    assert mock_context.send_action.await_count == 0
    assert result[0]["type"] == "image"
    assistant_append = state.memory_store.append.call_args_list[-1]
    assert assistant_append.kwargs["content"] == "[表情包：无语]"


@pytest.mark.asyncio
async def test_smalltalk_face_reply_sends_text_then_face_and_persists_marker(mock_context):
    from plugins.xiaoqing_chat.handlers import _maybe_reply_smalltalk

    runtime = _make_media_runtime(
        enable_inbound_media_context=False,
        enable_outbound_emoji_reply=False,
        enable_outbound_face_reply=True,
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
    face_plan = SimpleNamespace(
        entry=SimpleNamespace(face_id="277"),
        marker="[QQ表情：狗头]",
        reasoning="face_mode:text_with_face;face_label:狗头",
        mode="text_with_face",
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
        patch("plugins.xiaoqing_chat.handlers._generate_reply", new=AsyncMock(return_value="懂了")),
        patch("plugins.xiaoqing_chat.handlers.plan_emoji_reply", new=AsyncMock(return_value=None)),
        patch("plugins.xiaoqing_chat.handlers.plan_qq_face_reply", new=AsyncMock(return_value=face_plan)),
        patch("plugins.xiaoqing_chat.handlers.mark_qq_face_used"),
        patch("plugins.xiaoqing_chat.handlers._most_recent_user_local_id", return_value="u1"),
        patch("plugins.xiaoqing_chat.handlers._spawn_post_reply_bg_tasks", new=AsyncMock()),
        patch("plugins.xiaoqing_chat.handlers._schedule_memory_persist"),
        patch("plugins.xiaoqing_chat.handlers._schedule_action_history_flush"),
        patch("plugins.xiaoqing_chat.handlers._freq_record"),
        patch("plugins.xiaoqing_chat.handlers._log_step"),
    ):
        result = await _maybe_reply_smalltalk("你好", event, mock_context)

    assert result == []
    assert mock_context.send_action.await_count == 2
    first_action = mock_context.send_action.await_args_list[0].args[0]
    second_action = mock_context.send_action.await_args_list[1].args[0]
    assert first_action["params"]["message"][0]["type"] == "text"
    assert first_action["params"]["message"][0]["data"]["text"] == "懂了"
    assert second_action["params"]["message"][0]["type"] == "face"
    assert second_action["params"]["message"][0]["data"]["id"] == "277"
    assistant_append = state.memory_store.append.call_args_list[-1]
    assert assistant_append.kwargs["content"] == "懂了\n[QQ表情：狗头]"


@pytest.mark.asyncio
async def test_smalltalk_face_only_reply_returns_single_face_and_marker_memory(mock_context):
    from plugins.xiaoqing_chat.handlers import _maybe_reply_smalltalk

    runtime = _make_media_runtime(
        enable_inbound_media_context=False,
        enable_outbound_emoji_reply=False,
        enable_outbound_face_reply=True,
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
    face_plan = SimpleNamespace(
        entry=SimpleNamespace(face_id="14"),
        marker="[QQ表情：微笑]",
        reasoning="face_mode:face_only;face_label:微笑",
        mode="face_only",
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
        patch("plugins.xiaoqing_chat.handlers._generate_reply", new=AsyncMock(return_value="懂了")),
        patch("plugins.xiaoqing_chat.handlers.plan_emoji_reply", new=AsyncMock(return_value=None)),
        patch("plugins.xiaoqing_chat.handlers.plan_qq_face_reply", new=AsyncMock(return_value=face_plan)),
        patch("plugins.xiaoqing_chat.handlers.mark_qq_face_used"),
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
    assert assistant_append.kwargs["content"] == "[QQ表情：微笑]"


@pytest.mark.asyncio
async def test_smalltalk_forces_reply_when_new_emoji_collected(mock_context):
    from plugins.xiaoqing_chat.handlers import _maybe_reply_smalltalk

    runtime = _make_media_runtime(
        enable_inbound_media_context=True,
        enable_outbound_emoji_reply=False,
        enable_outbound_face_reply=False,
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
        patch(
            "plugins.xiaoqing_chat.handlers._ensure_user_message_recorded",
            new=AsyncMock(return_value="u1"),
        ),
        patch("plugins.xiaoqing_chat.handlers.is_brain_chat_active", return_value=False),
        patch("plugins.xiaoqing_chat.handlers._generate_reply", new=AsyncMock(return_value="好图，收了")),
        patch("plugins.xiaoqing_chat.handlers._most_recent_user_local_id", return_value="u1"),
        patch("plugins.xiaoqing_chat.handlers._spawn_post_reply_bg_tasks", new=AsyncMock()),
        patch("plugins.xiaoqing_chat.handlers._schedule_memory_persist"),
        patch("plugins.xiaoqing_chat.handlers._schedule_action_history_flush"),
        patch("plugins.xiaoqing_chat.handlers._freq_record"),
        patch("plugins.xiaoqing_chat.handlers._log_step"),
    ):
        result = await _maybe_reply_smalltalk("", event, mock_context)

    assert result[0]["type"] == "text"
    assert result[0]["data"]["text"] == "好图，收了"
