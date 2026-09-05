"""xiaoqing_chat 媒体测试共享 fixture、导入和私有 helper。"""

import asyncio
import base64
import hashlib
import json
import threading
from contextlib import nullcontext
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from core.ai import AIModelInfo
from core.interfaces import PluginCapabilities
from plugins.xiaoqing_chat.config.config import XiaoQingChatConfig
from plugins.xiaoqing_chat.llm.llm_client import LLMError
from plugins.xiaoqing_chat.media.emoji_library import collect_emoji_candidate, load_emoji_library
from plugins.xiaoqing_chat.media.event_media import (
    RenderedMedia,
    ResolvedMedia,
    _download_url_bytes,
    build_effective_user_text,
    render_event_media,
    render_local_media_file,
)
from plugins.xiaoqing_chat.media.event_media_analysis import (
    _load_and_prepare_media_for_llm,
    _media_llm_max_tokens,
    _prepare_media_for_llm,
    _resolve_media_llm_secret_candidates,
    _semantic_retry_reason,
)
from plugins.xiaoqing_chat.media.event_media_common import (
    MediaAnalysisDraft,
    MediaPayloadTooLarge,
    _looks_like_structured_media_text,
    _read_file_bounded,
    _run_media_blocking,
)
from plugins.xiaoqing_chat.media.marker_resolver import (
    find_candidate_by_hint,
    marker_media_part,
    parse_marker,
    resolve_marker,
    strip_outbound_marker_residue,
    text_without_outbound_marker,
)
from plugins.xiaoqing_chat.media.qq_face_catalog import load_qq_face_catalog
from plugins.xiaoqing_chat.media_registry import compact_message_content, resolve_message_content
from plugins.xiaoqing_chat.message_parts import message_parts_to_legacy
from plugins.xiaoqing_chat.runtime_state import get_state
from tests.helpers.payloads import text_reply_draft as _reply_draft
from tests.helpers.settings_snapshot import with_settings_reader

_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4////fwAJ+wP9KobjigAAAABJRU5ErkJggg=="
)


async def render_event_media_text(event, *, context, runtime) -> str:
    """把生产渲染结果投影成便于断言的标记文本。"""

    rendered = await render_event_media(event, context=context, runtime=runtime)
    return "\n".join(item.marker for item in rendered if item.marker.strip()).strip()


def _write_png(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_PNG_BYTES)
    return path


def _reply_draft_with_parts(
    text: str,
    parts,
    *,
    media_marker=None,
) -> SimpleNamespace:
    return SimpleNamespace(
        text         = text,
        parts        = tuple(parts),
        media_marker = media_marker,
    )


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
        format        = "GIF",
        save_all      = True,
        append_images = frames[1:],
        duration      = 80,
        loop          = 0,
        disposal      = 2,
    )
    return buffer.getvalue()


def _raw_media_response(
    content: str,
    *,
    used_path: str     = "/chat/completions",
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
    cfg                                      = XiaoQingChatConfig()
    media_cfg                                = cfg.media
    media_cfg.max_media_per_message          = 3
    media_cfg.max_analyze_bytes              = 1024 * 1024
    media_cfg.enable_emoji_refine_background = False
    media_cfg.enable_meme_cultural_hint      = False
    media_cfg.vision_timeout_seconds         = 5.0
    media_cfg.vision_max_retry               = 0
    media_cfg.vision_retry_interval_seconds  = 0.1
    for key, value in media_overrides.items():
        setattr(media_cfg, key, value)
    cfg.temperature                             = 0.7
    cfg.top_p                                   = 0.9
    cfg.max_tokens                              = 256
    cfg.max_retry                               = 1
    cfg.retry_interval_seconds                  = 0.2
    cfg.foreground_max_retry                    = 0
    cfg.foreground_retry_interval_seconds       = 0.2
    cfg.background_max_retry                    = 1
    cfg.background_retry_interval_seconds       = 0.2
    cfg.goal.enable_goal                        = False
    cfg.reflection.enable_expression_reflection = False
    cfg.reflection.enable_review_sessions       = False
    cfg.brain_chat.enable_private_brain_chat    = False
    cfg.brain_chat.show_mode_indicator          = False
    cfg.brain_chat.brain_mode_indicator         = "[brain]"
    cfg.personality.states                      = []
    cfg.personality.state_probability           = 0.0
    return SimpleNamespace(cfg=cfg)


def _vision_ai_service() -> SimpleNamespace:
    models = (
        AIModelInfo("glm-4.6v-flash", "zhipu", "glm-4.6v-flash", ("text", "image")),
        AIModelInfo("glm-4.6v", "zhipu", "glm-4.6v", ("text", "image")),
        AIModelInfo("glm-4v-flash", "zhipu", "glm-4v-flash", ("text", "image")),
        AIModelInfo(
            "glm-4.1v-thinking-flash",
            "zhipu",
            "glm-4.1v-thinking-flash",
            ("text", "image"),
        ),
    )
    return SimpleNamespace(
        list_models=lambda route, **kwargs: models,
        complete=AsyncMock(side_effect=LLMError("test route call not stubbed")),
    )


@pytest.fixture
def mock_context(tmp_path: Path):
    context         = MagicMock()
    context.config  = {"bot_name": "小青"}
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
    context.data_dir   = tmp_path / "data" / "xiaoqing_chat"
    context.plugin_dir.mkdir(parents=True, exist_ok=True)
    context.data_dir.mkdir(parents=True, exist_ok=True)
    context.http_session = AsyncMock()
    context.capabilities = PluginCapabilities(ai=_vision_ai_service())
    context.send_action = AsyncMock()
    context.logger      = MagicMock()
    return with_settings_reader(context)


__all__ = (
    "AIModelInfo",
    "AsyncMock",
    "BytesIO",
    "LLMError",
    "MagicMock",
    "MediaAnalysisDraft",
    "MediaPayloadTooLarge",
    "Mock",
    "Path",
    "PluginCapabilities",
    "RenderedMedia",
    "ResolvedMedia",
    "SimpleNamespace",
    "XiaoQingChatConfig",
    "_PNG_BYTES",
    "_animated_gif_bytes",
    "_download_url_bytes",
    "_load_and_prepare_media_for_llm",
    "_looks_like_structured_media_text",
    "_make_media_runtime",
    "_media_llm_max_tokens",
    "_prepare_media_for_llm",
    "_raw_media_response",
    "_read_file_bounded",
    "_reply_draft",
    "_reply_draft_with_parts",
    "_resolve_media_llm_secret_candidates",
    "_run_media_blocking",
    "_semantic_retry_reason",
    "_vision_ai_service",
    "_write_png",
    "asyncio",
    "base64",
    "build_effective_user_text",
    "collect_emoji_candidate",
    "compact_message_content",
    "find_candidate_by_hint",
    "get_state",
    "hashlib",
    "json",
    "load_emoji_library",
    "load_qq_face_catalog",
    "marker_media_part",
    "message_parts_to_legacy",
    "mock_context",
    "nullcontext",
    "parse_marker",
    "patch",
    "pytest",
    "render_event_media",
    "render_event_media_text",
    "render_local_media_file",
    "resolve_marker",
    "resolve_message_content",
    "strip_outbound_marker_residue",
    "text_without_outbound_marker",
    "threading",
)
