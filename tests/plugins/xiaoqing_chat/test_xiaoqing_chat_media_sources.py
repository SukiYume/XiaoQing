"""媒体来源、消息段和有效文本。"""

from __future__ import annotations

import base64
import hashlib
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import tests.helpers.xiaoqing_chat_media_test_support as _fixture_support
from tests.helpers.xiaoqing_chat_media_test_support import (
    _PNG_BYTES,
    PluginCapabilities,
    RenderedMedia,
    _animated_gif_bytes,
    _make_media_runtime,
    _prepare_media_for_llm,
    _raw_media_response,
    _write_png,
    build_effective_user_text,
    load_emoji_library,
    render_event_media,
    render_event_media_text,
)

mock_context = _fixture_support.mock_context


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url,enabled",
    [
        ("https://multimedia.nt.qq.com.cn/download?fileid=test", True),
        ("https://MULTIMEDIA.NT.QQ.COM.CN/download", True),
        ("http://multimedia.nt.qq.com.cn/download", False),
        ("https://multimedia.nt.qq.com.cn.attacker.example/image", False),
        ("https://example.com/image", False),
        ("https://198.18.1.211/image", False),
    ],
)
async def test_qq_media_fake_dns_is_scoped_to_https_host(url, enabled):
    from plugins.xiaoqing_chat.media.sources import _download_url_bytes

    response = SimpleNamespace(body=_PNG_BYTES, headers={"Content-Type": "image/png"})
    with patch(
        "plugins.xiaoqing_chat.media.sources.fetch_public_bytes",
        new=AsyncMock(return_value=response),
    ) as fetch:
        payload, mime = await _download_url_bytes(url, max_bytes=1024)
    assert payload == _PNG_BYTES and mime == "image/png"
    kwargs = fetch.await_args.kwargs
    assert kwargs["allow_transparent_proxy_fake_dns"] is enabled
    assert kwargs["allowed_hosts"] == (frozenset({"multimedia.nt.qq.com.cn"}) if enabled else None)
    assert kwargs["allowed_schemes"] == ({"https"} if enabled else None)


@pytest.mark.asyncio
async def test_media_download_error_survives_unsupported_file_id(mock_context):
    from plugins.xiaoqing_chat.media.sources import _resolve_media_bytes

    with patch(
        "plugins.xiaoqing_chat.media.sources._download_url_bytes",
        new=AsyncMock(side_effect=ValueError("DNS blocked")),
    ):
        with pytest.raises(ValueError, match="DNS blocked"):
            await _resolve_media_bytes(
                {"type": "image", "data": {"file": "opaque-id", "url": "https://example.com/img"}},
                context   = mock_context,
                max_bytes = 1024,
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("recovery_source", ["message", "image_api", "public_url"])
async def test_ordinary_image_recovers_adapter_identifier(mock_context, recovery_source):
    from plugins.xiaoqing_chat.media.sources import _resolve_media_bytes

    event      = {"message_id": 42}
    segment    = {"type": "image", "data": {"file": "image-42.image"}}
    encoded    = "base64://" + base64.b64encode(_PNG_BYTES).decode("ascii")
    detail     = {"message": [{"type": "image", "data": {"file": encoded}}]}
    image_data = (
        {"file": "C:/outside/adapter-cache.png", "url": "https://example.com/image.png"}
        if recovery_source == "public_url"
        else {"base64": base64.b64encode(_PNG_BYTES).decode("ascii")}
    )
    media = SimpleNamespace(
        get_message=AsyncMock(return_value=detail if recovery_source == "message" else {}),
        get_image=AsyncMock(return_value=image_data),
    )
    mock_context.capabilities = PluginCapabilities(onebot_media=media)
    with patch(
        "plugins.xiaoqing_chat.media.sources._download_url_bytes",
        new=AsyncMock(return_value=(_PNG_BYTES, "image/png")),
    ) as download:
        payload, _, _ = await _resolve_media_bytes(
            segment, context=mock_context, max_bytes=1024, event=event
        )

    assert payload == _PNG_BYTES
    if recovery_source == "message":
        media.get_image.assert_not_awaited()
    else:
        media.get_image.assert_awaited_once_with(file="image-42.image", file_id=None)
    if recovery_source == "public_url":
        download.assert_awaited_once_with("https://example.com/image.png", max_bytes=1024)


@pytest.mark.asyncio
async def test_adapter_recovery_retains_size_and_local_path_limits(mock_context):
    from plugins.xiaoqing_chat.media.sources import _resolve_media_bytes

    segment = {"type": "image", "data": {"file": "image-42.image"}}
    media   = SimpleNamespace(
        get_message=AsyncMock(return_value={}),
        get_image=AsyncMock(return_value={"base64": base64.b64encode(_PNG_BYTES).decode("ascii")}),
    )
    mock_context.capabilities = PluginCapabilities(onebot_media=media)
    with pytest.raises(FileNotFoundError):
        await _resolve_media_bytes(
            segment, context=mock_context, max_bytes=8, event={"message_id": 42}
        )
    assert media.get_image.await_count == 1
    media.get_image.reset_mock()
    segment["data"]["file"] = "C:/outside/adapter-cache.png"
    with pytest.raises(FileNotFoundError):
        await _resolve_media_bytes(
            segment, context=mock_context, max_bytes=1024, event={"message_id": 42}
        )
    media.get_image.assert_not_awaited()


@pytest.mark.asyncio
async def test_ordinary_image_recovery_keeps_multiple_attachments_distinct(mock_context):
    from plugins.xiaoqing_chat.media.sources import _resolve_media_bytes

    media = SimpleNamespace(
        get_message=AsyncMock(
            return_value={
                "message": [
                    {"type": "image", "data": {"file": "other-a.image"}},
                    {"type": "image", "data": {"file": "other-b.image"}},
                ]
            }
        ),
        get_image=AsyncMock(return_value={}),
    )
    mock_context.capabilities = PluginCapabilities(onebot_media=media)
    with pytest.raises(FileNotFoundError):
        await _resolve_media_bytes(
            {"type": "image", "data": {"file": "wanted.image"}},
            context   = mock_context,
            max_bytes = 1024,
            event     = {"message_id": 42},
        )
    media.get_image.assert_awaited_once_with(file="wanted.image", file_id=None)


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
    event   = {
        "message": [{"type": "image", "data": {"file": file_value, "name": "inline_image.png"}}]
    }

    with patch(
        "plugins.xiaoqing_chat.media.event_media.analyze_media_with_llm",
        new=AsyncMock(return_value=None),
    ):
        text = await render_event_media_text(event, context=mock_context, runtime=runtime)

    cached_files = list((mock_context.data_dir / "media" / "inbox").glob("*"))
    assert text.startswith("[图片：")
    assert cached_files


@pytest.mark.asyncio
async def test_render_event_media_blocks_private_image_url_without_download(mock_context):
    runtime = _make_media_runtime()
    event   = {
        "message": [
            {
                "type": "image",
                "data": {"url": "http://127.0.0.1/private.png"},
            }
        ]
    }
    from core.safe_http import validate_public_url
    from plugins.xiaoqing_chat.media.sources import _download_url_bytes

    with (
        patch("core.safe_http.validate_public_url", wraps=validate_public_url) as validate,
        patch("core.safe_http.aiohttp.ClientSession") as network,
    ):
        with pytest.raises(ValueError, match="non-public"):
            await _download_url_bytes("http://127.0.0.1/private.png", max_bytes=1024)
        rendered = await render_event_media(event, context=mock_context, runtime=runtime)
    assert validate.call_count >= 2
    network.assert_not_called()
    assert len(rendered) == 1
    assert rendered[0].marker.startswith("[图片：")


@pytest.mark.asyncio
async def test_render_event_media_blocks_file_uri_outside_plugin_data_dir(mock_context, tmp_path):
    runtime = _make_media_runtime()
    outside = _write_png(tmp_path / "outside.png")
    event   = {
        "message": [
            {
                "type": "image",
                "data": {"file": outside.as_uri()},
            }
        ]
    }

    with patch(
        "plugins.xiaoqing_chat.media.event_media.analyze_media_with_llm",
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
    event   = {
        "message": [
            {
                "type": "image",
                "data": {
                    "url"             : "https://example.com/store_emoji.bin",
                    "emoji_package_id": 1001,
                    "emoji_id"        : "emo-1",
                    "key"             : "pkg_123",
                },
            }
        ]
    }

    with (
        patch(
            "plugins.xiaoqing_chat.media.sources._download_url_bytes",
            new=AsyncMock(return_value=(_PNG_BYTES, "image/png")),
        ),
        patch(
            "plugins.xiaoqing_chat.media.event_media.analyze_media_with_llm",
            new=AsyncMock(return_value=None),
        ),
    ):
        text = await render_event_media_text(event, context=mock_context, runtime=runtime)

    assert text.startswith("[表情包：")
    assert event["_xc_new_emoji_count"] == 1
    assert list((mock_context.data_dir / "media" / "library").glob("*"))


@pytest.mark.asyncio
async def test_render_event_media_text_transcodes_octet_stream_sticker_for_vision(mock_context):
    runtime = _make_media_runtime()
    event   = {
        "message": [
            {
                "type": "image",
                "data": {
                    "url"             : "https://example.com/store_emoji.bin",
                    "emoji_package_id": 1001,
                    "emoji_id"        : "emo-1",
                    "summary"         : "[动画表情]",
                },
            }
        ]
    }
    captured_urls: list[str] = []
    captured_prompt          = ""

    async def _fake_chat_raw(*, messages, **kwargs):
        nonlocal captured_prompt
        content         = messages[1]["content"]
        captured_prompt = content[0]["text"]
        image_url       = content[1]["image_url"]["url"]
        captured_urls.append(image_url)
        return _raw_media_response(
            '{"kind":"emoji","description":"一只猫皱着脸，配字是苦鲁西","emotion_tags":["委屈","难受"]}'
        )

    with (
        patch(
            "plugins.xiaoqing_chat.media.sources._download_url_bytes",
            new=AsyncMock(return_value=(_animated_gif_bytes(), "application/octet-stream")),
        ),
        patch(
            "plugins.xiaoqing_chat.media.event_media_analysis.chat_completions_raw_with_fallback_paths",
            new=AsyncMock(side_effect=_fake_chat_raw),
        ),
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
    payload  = _animated_gif_bytes()
    gif_path.write_bytes(payload)
    resolved = SimpleNamespace(
        mime_type   = "image/gif",
        cached_path = gif_path,
        is_animated = True,
    )

    prepared = _prepare_media_for_llm(resolved)

    assert prepared.mime_type == "image/png"
    assert prepared.transcoded is True
    assert prepared.frame_strategy == "animation_contact_sheet"
    assert prepared.frame_count == 3
    with Image.open(BytesIO(prepared.payload)) as image:
        assert image.width > image.height


def test_prepare_media_for_llm_never_sends_original_when_decode_fails(mock_context):
    image_path = _write_png(mock_context.data_dir / "decode_failure.png")
    resolved   = SimpleNamespace(
        mime_type   = "image/png",
        cached_path = image_path,
        is_animated = False,
    )

    with patch("PIL.Image.open", side_effect=OSError("decode failed")):
        with pytest.raises(ValueError, match="decode"):
            _prepare_media_for_llm(resolved)


@pytest.mark.asyncio
async def test_render_event_media_text_supports_face_segment(mock_context):
    runtime = _make_media_runtime()
    event   = {
        "message": [
            {
                "type": "face",
                "data": {
                    "id" : "14",
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
    from plugins.xiaoqing_chat.reply_media import event_media_items_for_memory

    runtime = _make_media_runtime()
    event   = {
        "message": [
            {
                "type": "face",
                "data": {
                    "id" : "14",
                    "raw": {"text": "微笑"},
                },
            }
        ]
    }

    items = await event_media_items_for_memory(event, context=mock_context, runtime=runtime)

    assert items == [
        {
            "kind"        : "qq_face",
            "media_hash"  : "qq_face:14",
            "face_id"     : "14",
            "marker"      : "[QQ表情：微笑]",
            "description" : "微笑",
            "label"       : "微笑",
            "emotion_tags": ["微笑"],
        }
    ]


@pytest.mark.asyncio
async def test_build_effective_user_text_preserves_mixed_media_order(mock_context):
    runtime = _make_media_runtime()
    event   = {
        "message": [
            {"type": "text", "data": {"text": "看这个"}},
            {"type": "image", "data": {"url": "https://example.com/cat.png"}},
            {"type": "text", "data": {"text": "笑死"}},
        ],
        "_xc_rendered_media_items": [
            RenderedMedia(
                media_hash   = "hash-1",
                kind         = "image",
                description  = "一只猫歪着头",
                emotion_tags = (),
                marker       = "[图片：一只猫歪着头]",
            )
        ],
    }

    text = await build_effective_user_text(
        "看这个笑死",
        event,
        context = mock_context,
        runtime = runtime,
    )

    assert text == "看这个\n[图片：一只猫歪着头]\n笑死"


@pytest.mark.asyncio
async def test_build_effective_user_text_keeps_media_position_after_prefix_strip(mock_context):
    runtime = _make_media_runtime()
    event   = {
        "message": [
            {"type": "text", "data": {"text": "小青你看"}},
            {"type": "image", "data": {"url": "https://example.com/cat.png"}},
            {"type": "text", "data": {"text": "这个"}},
        ],
        "_xc_rendered_media_items": [
            RenderedMedia(
                media_hash   = "hash-2",
                kind         = "image",
                description  = "一只猫歪着头",
                emotion_tags = (),
                marker       = "[图片：一只猫歪着头]",
            )
        ],
    }

    text = await build_effective_user_text(
        "你看这个",
        event,
        context = mock_context,
        runtime = runtime,
    )

    assert text == "你看\n[图片：一只猫歪着头]\n这个"


@pytest.mark.asyncio
async def test_build_effective_user_text_includes_emoji_detail_context(mock_context):
    runtime = _make_media_runtime()
    event   = {
        "message": [
            {"type": "text", "data": {"text": "你这"}},
            {
                "type": "image",
                "data": {"url": "https://example.com/bird.jpg", "summary": "[动画表情]"},
            },
        ],
        "_xc_rendered_media_items": [
            RenderedMedia(
                media_hash   = "hash-emoji",
                kind         = "emoji",
                description  = '卡通小鸟倒地，上方对话框写"不愧是你"，下方文字写"我佩服得鹉体投地"',
                emotion_tags = ("佩服", "调侃", "开心"),
                marker       = "[表情包：佩服，调侃]",
            )
        ],
    }

    text = await build_effective_user_text(
        "你这",
        event,
        context = mock_context,
        runtime = runtime,
    )

    assert (
        text
        == '你这\n[表情包：佩服，调侃；内容：卡通小鸟倒地，上方对话框写"不愧是你"，下方文字写"我佩服得鹉体投地"]'
    )


@pytest.mark.asyncio
async def test_build_effective_user_text_surfaces_emoji_visible_text_as_quoted_speech(mock_context):
    runtime = _make_media_runtime()
    event   = {
        "message": [
            {
                "type": "image",
                "data": {"url": "https://example.com/cat.jpg", "summary": "[动画表情]"},
            },
        ],
        "_xc_rendered_media_items": [
            RenderedMedia(
                media_hash   = "hash-emoji",
                kind         = "emoji",
                description  = "一个猫耳动漫女孩露出惊讶表情，配文字表达疑惑或调侃。，文字内容是“那咋整啊”",
                emotion_tags = ("疑惑", "调侃"),
                marker       = "[表情包：疑惑，调侃]",
            )
        ],
    }

    text = await build_effective_user_text(
        "",
        event,
        context = mock_context,
        runtime = runtime,
    )

    # 画面内容和配字同时保留，追问画面细节时才有依据。
    assert text == (
        "[表情包：疑惑，调侃；内容：一个猫耳动漫女孩露出惊讶表情，配文字表达疑惑或调侃。；写着“那咋整啊”]"
    )


@pytest.mark.asyncio
async def test_render_event_media_text_strips_face_brackets(mock_context):
    runtime = _make_media_runtime()
    event   = {
        "message": [
            {
                "type": "face",
                "data": {
                    "id" : "1",
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
    event   = {
        "message_id": 42,
        "message"   : [
            {
                "type": "mface",
                "data": {
                    "emoji_package_id": 1001,
                    "emoji_id"        : "emo-1",
                    "key"             : "pkg_123",
                    "summary"         : "无语",
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
                "base64"   : base64.b64encode(_PNG_BYTES).decode("ascii"),
                "file_name": "sticker.png",
            }
        ),
    )
    mock_context.capabilities = PluginCapabilities(onebot_media=media)

    with patch(
        "plugins.xiaoqing_chat.media.event_media.analyze_media_with_llm",
        new=AsyncMock(return_value=None),
    ):
        text = await render_event_media_text(event, context=mock_context, runtime=runtime)

    assert text.startswith("[表情包：")
    assert event["_xc_new_emoji_count"] == 1
    media.get_message.assert_awaited_once_with(42)
    media.get_image.assert_awaited_once_with(file_id="file-1", file=None)


@pytest.mark.asyncio
async def test_render_event_media_text_falls_back_to_store_emoji_summary_when_download_fails(
    mock_context,
):
    runtime = _make_media_runtime()
    event   = {
        "message": [
            {
                "type": "image",
                "data": {
                    "url"             : "https://example.com/store_emoji.bin",
                    "emoji_package_id": 1001,
                    "emoji_id"        : "emo-1",
                    "key"             : "pkg_123",
                },
            }
        ]
    }

    with patch(
        "plugins.xiaoqing_chat.media.sources._download_url_bytes",
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
        file_path  = Path(file_path)
        media_hash = hashlib.sha256(file_path.read_bytes()).hexdigest()
        stem       = file_path.stem
        return RenderedMedia(
            media_hash   = media_hash,
            kind         = "emoji",
            description  = stem,
            emotion_tags = (stem,),
            marker       = f"[表情包：{stem}]",
            cached_path  = file_path,
        )

    with patch(
        "plugins.xiaoqing_chat.media.event_media.render_local_media_file",
        new=AsyncMock(side_effect=_fake_render),
    ) as mock_render:
        first_entries  = await load_emoji_library(mock_context, runtime)
        second_entries = await load_emoji_library(mock_context, runtime)

    assert first_entries == []
    assert second_entries == []
    assert mock_render.await_count == 2
