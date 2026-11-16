"""xiaoqing_chat 媒体测试共享 fixture、导入和私有 helper。"""

import base64
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.ai import AIModelInfo
from core.interfaces import PluginCapabilities
from plugins.xiaoqing_chat.config.config import XiaoQingChatConfig
from plugins.xiaoqing_chat.llm.gateway import LLMError
from plugins.xiaoqing_chat.llm.llm_config import LLMCallConfig
from plugins.xiaoqing_chat.media.emoji_library import collect_emoji_candidate, load_emoji_library
from plugins.xiaoqing_chat.media.event_media import (
    RenderedMedia,
    ResolvedMedia,
    build_effective_user_text,
    render_event_media,
    render_local_media_file,
)
from plugins.xiaoqing_chat.media.loading import read_file_bounded, run_media_blocking
from plugins.xiaoqing_chat.media.marker_resolver import (
    find_candidate_by_hint,
    marker_media_part,
    parse_marker,
    resolve_marker,
    strip_outbound_marker_residue,
    text_without_outbound_marker,
)
from plugins.xiaoqing_chat.media.markers import looks_like_structured_media_text
from plugins.xiaoqing_chat.media.models import MediaAnalysisDraft, MediaPayloadTooLarge
from plugins.xiaoqing_chat.media.qq_face_catalog import load_qq_face_catalog
from plugins.xiaoqing_chat.media_registry import compact_message_content, resolve_message_content
from plugins.xiaoqing_chat.message_parts import message_parts_to_legacy
from plugins.xiaoqing_chat.runtime_state import get_state
from tests.helpers.settings_snapshot import with_settings_reader

# 视觉模型调用的边界：识图请求都经这个网关函数发出，测试在这里替换它。
VISION_GATEWAY = (
    "plugins.xiaoqing_chat.media.event_media_analysis.chat_completions_raw_with_profile"
)

PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4////fwAJ+wP9KobjigAAAABJRU5ErkJggg=="
)


async def render_event_media_text(event, *, context, runtime) -> str:
    """把生产渲染结果投影成便于断言的标记文本。"""

    rendered = await render_event_media(event, context=context, runtime=runtime)
    return "\n".join(item.marker for item in rendered if item.marker.strip()).strip()


def write_png(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(PNG_BYTES)
    return path


def animated_gif_bytes() -> bytes:
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


def raw_media_response(
    content: str,
    *,
    used_path: str     = "/chat/completions",
    finish_reason: str = "stop",
) -> tuple[dict[str, object], str]:
    return (
        {
            "choices": [
                {
                    "message"      : {"content": content},
                    "finish_reason": finish_reason,
                }
            ]
        },
        used_path,
    )


def make_media_runtime(**media_overrides):
    cfg                                      = XiaoQingChatConfig()
    media_cfg                                = cfg.media
    media_cfg.max_media_per_message          = 3
    media_cfg.max_analyze_bytes              = 1024 * 1024
    media_cfg.enable_emoji_refine_background = False
    media_cfg.enable_meme_cultural_hint      = False
    for key, value in media_overrides.items():
        setattr(media_cfg, key, value)
    cfg._llm_configs["vision"] = LLMCallConfig(
        timeout_seconds        = 5,
        max_retry              = 0,
        retry_interval_seconds = 0.1,
        temperature            = 0.2,
        top_p                  = 0.9,
        max_tokens             = 800,
    )
    cfg.goal.enable_goal                        = False
    cfg.reflection.enable_expression_reflection = False
    cfg.reflection.enable_review_sessions       = False
    cfg.brain_chat.enable_private_brain_chat    = False
    cfg.brain_chat.show_mode_indicator          = False
    cfg.brain_chat.brain_mode_indicator         = "[brain]"
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
                "default"  : "test",
                "providers": {
                    "test": {
                        "api_base"     : "http://test",
                        "api_key"      : "key",
                        "model"        : "model",
                        "endpoint_path": "/v1/chat/completions",
                    }
                },
                "vision": {
                    "providers": {
                        "glm-4v": {
                            "api_base"     : "https://open.bigmodel.cn/api/paas/v4",
                            "api_key"      : "vision-key",
                            "model"        : "glm-4v",
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
    "LLMError",
    "MediaAnalysisDraft",
    "MediaPayloadTooLarge",
    "PluginCapabilities",
    "RenderedMedia",
    "ResolvedMedia",
    "VISION_GATEWAY",
    "PNG_BYTES",
    "animated_gif_bytes",
    "looks_like_structured_media_text",
    "make_media_runtime",
    "raw_media_response",
    "read_file_bounded",
    "run_media_blocking",
    "write_png",
    "build_effective_user_text",
    "collect_emoji_candidate",
    "compact_message_content",
    "find_candidate_by_hint",
    "get_state",
    "load_emoji_library",
    "load_qq_face_catalog",
    "marker_media_part",
    "message_parts_to_legacy",
    "mock_context",
    "parse_marker",
    "render_event_media",
    "render_event_media_text",
    "render_local_media_file",
    "resolve_marker",
    "resolve_message_content",
    "strip_outbound_marker_residue",
    "text_without_outbound_marker",
)
