"""
地震快讯插件

从微博中国地震台网获取地震快讯。
仅推送 4 级及以上地震。
"""

import asyncio
import hashlib
import logging
import re
import uuid
import warnings
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

import requests
from PIL import Image, UnidentifiedImageError

from core.args import parse
from core.bounded_http import (
    JSON_MIME_POLICY,
    NO_REDIRECTS,
    BodyLimits,
    JsonLimits,
    MimePolicy,
    ResponseFormatError,
    ResponseLimitError,
    parse_bounded_json,
    requests_request_bounded,
)
from core.plugin_base import (
    atomic_write_bytes,
    build_action,
    image,
    load_json,
    run_sync,
    segments,
    text,
    write_json,
)
from core.public_errors import public_error_message, public_error_response
from core.safe_http import SafeHttpError, fetch_public_bytes

logger = logging.getLogger(__name__)

# ============================================================
# 常量
# ============================================================

WEIBO_UID = "1904228041"
CONTAINER_ID = "1076031904228041"
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_DIMENSION = 16_384
MAX_IMAGE_PIXELS = 32_000_000
MAX_DECODED_IMAGE_BYTES = 128 * 1024 * 1024
_MAX_WEIBO_CARDS = 200
_MAX_SINCE_ID_DIGITS = 32
_IMAGE_HOSTS = frozenset(
    {
        "wx1.sinaimg.cn",
        "wx2.sinaimg.cn",
        "wx3.sinaimg.cn",
        "wx4.sinaimg.cn",
    }
)
_IMAGE_MIME_FORMATS = {
    "image/jpeg": ("JPEG", ".jpg"),
    "image/png": ("PNG", ".png"),
    "image/webp": ("WEBP", ".webp"),
}
_IMAGE_FORMAT_MODES = {
    "JPEG": frozenset({"L", "RGB", "CMYK", "YCbCr"}),
    "PNG": frozenset({"1", "L", "LA", "P", "RGB", "RGBA"}),
    "WEBP": frozenset({"RGB", "RGBA"}),
}
_MODE_BYTES_PER_PIXEL = {
    "1": 1,
    "L": 1,
    "P": 1,
    "LA": 2,
    "RGB": 3,
    "YCbCr": 3,
    "RGBA": 4,
    "CMYK": 4,
}
_VISITOR_MIME_POLICY = MimePolicy(
    exact=frozenset(
        {
            "application/javascript",
            "application/x-javascript",
            "text/javascript",
        }
    )
)
_VISITOR_BODY_LIMITS = BodyLimits(
    max_wire_bytes=256 * 1024,
    max_decoded_bytes=512 * 1024,
    max_decompression_ratio=20,
    ratio_grace_bytes=16 * 1024,
    chunk_bytes=32 * 1024,
)
_CONFIG_BODY_LIMITS = BodyLimits(
    max_wire_bytes=256 * 1024,
    max_decoded_bytes=512 * 1024,
    max_decompression_ratio=20,
    ratio_grace_bytes=16 * 1024,
    chunk_bytes=32 * 1024,
)
_CONFIG_JSON_LIMITS = JsonLimits(
    max_bytes=_CONFIG_BODY_LIMITS.max_decoded_bytes,
    max_depth=12,
    max_nodes=5_000,
    max_string_chars=128 * 1024,
    max_number_chars=128,
)
_INDEX_BODY_LIMITS = BodyLimits(
    max_wire_bytes=1024 * 1024,
    max_decoded_bytes=2 * 1024 * 1024,
    max_decompression_ratio=20,
    ratio_grace_bytes=32 * 1024,
    chunk_bytes=64 * 1024,
)
_INDEX_JSON_LIMITS = JsonLimits(
    max_bytes=_INDEX_BODY_LIMITS.max_decoded_bytes,
    max_depth=24,
    max_nodes=30_000,
    max_string_chars=512 * 1024,
    max_number_chars=128,
)
_WEIBO_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
_IMAGE_HEADERS = {
    "User-Agent": _WEIBO_USER_AGENT,
    "Referer": "https://m.weibo.cn/",
    "Accept": "image/*",
    "Accept-Encoding": "identity",
}


@dataclass(frozen=True, slots=True)
class _PreparedCard:
    clean_text: str
    magnitude: float | None
    figure_url: str | None


# ============================================================
# 插件初始化
# ============================================================


def init(context=None) -> None:
    """插件初始化"""
    pass


# ============================================================
# 状态管理
# ============================================================


def _since_path(context):
    """获取状态文件路径"""
    return context.data_dir / "earthquake.json"


def _load_since(context) -> str:
    """加载上次处理的微博 ID"""
    path = _since_path(context)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        write_json(path, {"since_id": "0"})
        return "0"
    try:
        data = load_json(path, {}, raise_on_error=True)
        if not isinstance(data, dict):
            raise ValueError("earthquake state must be an object")
        raw_since_id = data.get("since_id", "0")
        if isinstance(raw_since_id, bool):
            raise ValueError("earthquake since_id must be numeric")
        candidate = str(raw_since_id)
        if re.fullmatch(rf"[0-9]{{1,{_MAX_SINCE_ID_DIGITS}}}", candidate) is None:
            raise ValueError("earthquake since_id must be numeric")
        return str(int(candidate))
    except (ValueError, OSError, TypeError) as exc:
        public_error_message(
            context,
            exc,
            logger=logger,
            component="earthquake.load_state",
        )
        write_json(path, {"since_id": "0"})
        return "0"


def _save_since(context, since_id: str) -> None:
    """保存最新处理的微博 ID"""
    path = _since_path(context)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, {"since_id": since_id})


# ============================================================
# 微博 API
# ============================================================


def _create_session(context=None) -> requests.Session:
    """Create an unbootstrapped session; network I/O stays in one worker."""
    del context
    return requests.Session()


def _weibo_headers(*, referer: str = "https://m.weibo.cn/") -> dict[str, str]:
    return {
        "User-Agent": _WEIBO_USER_AGENT,
        "Referer": referer,
    }


def _validate_api_envelope(payload: Any, *, require_cards: bool) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ResponseFormatError("Weibo API response must be an object")
    if require_cards and "ok" not in payload:
        raise ResponseFormatError("Weibo API response has no success marker")
    if "ok" in payload and (type(payload["ok"]) is not int or payload["ok"] != 1):
        raise ResponseFormatError("Weibo API returned an unsuccessful envelope")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ResponseFormatError("Weibo API data must be an object")
    if not require_cards:
        return payload
    cards = data.get("cards")
    if not isinstance(cards, list):
        raise ResponseFormatError("Weibo API cards must be an array")
    if len(cards) > _MAX_WEIBO_CARDS:
        raise ResponseLimitError("Weibo API returned too many cards")
    for card in cards:
        if not isinstance(card, dict):
            raise ResponseFormatError("Weibo API card must be an object")
        mblog = card.get("mblog")
        if mblog is not None and not isinstance(mblog, dict):
            raise ResponseFormatError("Weibo API mblog must be an object")
    return payload


def _report_bootstrap_error(context, exc: Exception) -> None:
    if context is not None:
        public_error_message(
            context,
            exc,
            logger=logger,
            component="earthquake.bootstrap",
        )
        return
    logger.warning(
        "Failed to bootstrap Weibo visitor session: error_type=%s",
        type(exc).__name__,
    )


def _bootstrap_session(session: requests.Session, context=None) -> None:
    """Best-effort cookie bootstrap without weakening response boundaries."""
    request_id = uuid.uuid4().hex
    visitor_url = "https://visitor.passport.weibo.cn/visitor/genvisitor2"
    visitor_data = {
        "cb": "visitor_gray_callback",
        "ver": "20250916",
        "request_id": request_id,
        "tid": "",
        "from": "weibo",
        "webdriver": "false",
        "rid": "01Cn_5z8ew6CZHvNiTdPeyK2Qf740",
        "return_url": f"https://m.weibo.cn/u/{WEIBO_UID}",
    }
    headers = _weibo_headers()
    try:
        requests_request_bounded(
            "POST",
            visitor_url,
            session=session,
            headers=headers,
            limits=_VISITOR_BODY_LIMITS,
            mime_policy=_VISITOR_MIME_POLICY,
            redirect_policy=NO_REDIRECTS,
            request_kwargs={"data": visitor_data, "timeout": (5.0, 15.0)},
            total_timeout_seconds=20.0,
        )
    except Exception as exc:
        _report_bootstrap_error(context, exc)

    try:
        config_response = requests_request_bounded(
            "GET",
            "https://m.weibo.cn/api/config",
            session=session,
            headers=headers,
            limits=_CONFIG_BODY_LIMITS,
            mime_policy=JSON_MIME_POLICY,
            redirect_policy=NO_REDIRECTS,
            request_kwargs={"timeout": (5.0, 15.0)},
            total_timeout_seconds=20.0,
        )
        config = parse_bounded_json(config_response, limits=_CONFIG_JSON_LIMITS)
        _validate_api_envelope(config, require_cards=False)
    except Exception as exc:
        _report_bootstrap_error(context, exc)


def _fetch_weibo(session: requests.Session) -> dict[str, Any]:
    """获取微博列表"""
    headers = _weibo_headers(referer=f"https://m.weibo.cn/u/{WEIBO_UID}")
    headers["X-Requested-With"] = "XMLHttpRequest"
    params = {
        "type": "uid",
        "value": WEIBO_UID,
        "containerid": CONTAINER_ID,
    }
    response = requests_request_bounded(
        "GET",
        "https://m.weibo.cn/api/container/getIndex",
        session=session,
        headers=headers,
        limits=_INDEX_BODY_LIMITS,
        mime_policy=JSON_MIME_POLICY,
        redirect_policy=NO_REDIRECTS,
        request_kwargs={"params": params, "timeout": (5.0, 15.0)},
        total_timeout_seconds=20.0,
    )
    payload = parse_bounded_json(response, limits=_INDEX_JSON_LIMITS)
    return _validate_api_envelope(payload, require_cards=True)


def _extract_magnitude(text: str) -> float | None:
    """从文本中提取震级"""
    match = re.search(r"发生(\d+\.?\d*)级地震", text)
    if match:
        return float(match.group(1))
    return None


def _extract_clean_text(raw_text: str) -> str:
    """提取纯净的地震信息文本"""
    # 清理空白字符
    clean = re.sub(r"\s+\u200b+", "", raw_text)
    # 提取核心内容
    match = re.search(r"</a>(.+)（ <a href=", clean)
    if match:
        return match.group(1)
    return clean


def _image_media_type(headers: Any) -> str:
    raw_value = headers.get("Content-Type") if hasattr(headers, "get") else None
    if not isinstance(raw_value, str) or not raw_value.strip() or "," in raw_value:
        raise ResponseFormatError("earthquake image has no valid Content-Type")
    media_type = raw_value.split(";", 1)[0].strip().casefold()
    if media_type not in _IMAGE_MIME_FORMATS:
        raise ResponseFormatError("earthquake image MIME type is not allowed")
    return media_type


def _validate_image_metadata(candidate: Image.Image, *, expected_format: str) -> None:
    actual_format = str(candidate.format or "").upper()
    if actual_format != expected_format:
        raise ResponseFormatError("earthquake image MIME and format do not match")
    if (
        bool(getattr(candidate, "is_animated", False))
        or int(getattr(candidate, "n_frames", 1)) != 1
    ):
        raise ResponseFormatError("animated earthquake images are not allowed")
    width, height = candidate.size
    if width <= 0 or height <= 0:
        raise ResponseFormatError("earthquake image dimensions are invalid")
    if width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION:
        raise ResponseLimitError("earthquake image dimension limit exceeded")
    pixels = width * height
    if pixels > MAX_IMAGE_PIXELS:
        raise ResponseLimitError("earthquake image pixel limit exceeded")
    if candidate.mode not in _IMAGE_FORMAT_MODES[expected_format]:
        raise ResponseFormatError("earthquake image mode is not allowed")
    decoded_bytes = pixels * _MODE_BYTES_PER_PIXEL[candidate.mode]
    if decoded_bytes > MAX_DECODED_IMAGE_BYTES:
        raise ResponseLimitError("earthquake image decoded-size limit exceeded")


def _validate_image_bytes(payload: bytes, *, media_type: str) -> str:
    """Verify a single-frame image before any bytes become a local attachment."""
    expected_format, extension = _IMAGE_MIME_FORMATS[media_type]
    if expected_format == "JPEG" and not payload.endswith(b"\xff\xd9"):
        raise ResponseFormatError("earthquake JPEG has trailing or truncated data")
    if expected_format == "PNG" and not payload.endswith(b"\x00\x00\x00\x00IEND\xaeB\x60\x82"):
        raise ResponseFormatError("earthquake PNG has trailing or truncated data")
    if expected_format == "WEBP" and (
        len(payload) < 12
        or payload[:4] != b"RIFF"
        or payload[8:12] != b"WEBP"
        or int.from_bytes(payload[4:8], "little") + 8 != len(payload)
    ):
        raise ResponseFormatError("earthquake WebP container length is invalid")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(payload)) as candidate:
                _validate_image_metadata(candidate, expected_format=expected_format)
                candidate.verify()
            with Image.open(BytesIO(payload)) as decoded:
                _validate_image_metadata(decoded, expected_format=expected_format)
                decoded.load()
                _validate_image_metadata(decoded, expected_format=expected_format)
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ResponseLimitError("earthquake image decompression-bomb limit exceeded") from exc
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise ResponseFormatError("earthquake image is invalid") from exc
    return extension


def _validate_and_store_figure(context, response) -> Path:
    media_type = _image_media_type(response.headers)
    extension = _validate_image_bytes(response.body, media_type=media_type)
    digest = hashlib.sha256(response.body).hexdigest()
    file_path = context.data_dir / "EarthquakeFigures" / f"{digest}{extension}"
    atomic_write_bytes(file_path, response.body)
    return file_path


async def _download_figure(context, figure_url: str) -> Path:
    response = await fetch_public_bytes(
        figure_url,
        headers=_IMAGE_HEADERS,
        timeout_seconds=20.0,
        max_bytes=MAX_IMAGE_BYTES,
        allowed_content_type_prefixes=tuple(_IMAGE_MIME_FORMATS),
        allowed_content_types=tuple(_IMAGE_MIME_FORMATS),
        allowed_hosts=_IMAGE_HOSTS,
        allowed_schemes=("https",),
    )
    if response is None:
        raise SafeHttpError("earthquake image request failed")
    return await run_sync(_validate_and_store_figure, context, response)


# ============================================================
# 主处理函数
# ============================================================


async def handle(command: str, args: str, event: dict, context) -> list:
    """命令处理入口"""
    try:
        parsed = parse(args)

        # help/帮助 仍然显示帮助；空参数则默认获取最新地震快讯
        if parsed and parsed.first and parsed.first.lower() in ["help", "帮助"]:
            return segments(_show_help())

        subcommand = parsed.first.lower() if parsed and parsed.first else None

        # 命令路由
        if subcommand in {"latest", "最新"}:
            return await _fetch_earthquake_news(context, force=True)

        # 默认行为：获取最新地震快讯
        return await _fetch_earthquake_news(context, force=True)

    except Exception as exc:
        return public_error_response(context, exc, logger=logger, component="earthquake.handle")


def _show_help() -> str:
    """显示帮助信息"""
    return """
🌏 **地震快讯**

从微博中国地震台网获取地震快讯信息。

**命令:**
• /earthquake 或 /地震 - 获取最新地震快讯
• /earthquake latest - 获取最新地震快讯
• /earthquake help - 显示此帮助信息

**说明:**
• 定时任务仅推送 4 级及以上地震
• 手动查询显示最新地震信息（不限震级）
• 数据来源: 中国地震台网官方微博

输入 /earthquake help 查看此帮助
""".strip()


async def scheduled(context) -> list:
    """定时任务入口"""
    result = await _fetch_earthquake_news(context, force=False, advance_cursor=False)
    pending_since = getattr(context, "state", {}).pop("earthquake_pending_since", None)
    payload = segments(result)
    if not payload:
        if pending_since:
            await asyncio.to_thread(_save_since, context, pending_since)
        return []
    targets = list(context.default_groups()) if hasattr(context, "default_groups") else []
    if not targets:
        logger.warning("Earthquake notification has no configured target group; cursor retained")
        return []
    delivered = True
    for group_id in targets:
        action = build_action(payload, None, int(group_id))
        delivered = bool(action) and bool(await context.send_action(action)) and delivered
    if delivered and pending_since:
        await asyncio.to_thread(_save_since, context, pending_since)
    return []


async def _fetch_earthquake_news(
    context,
    force: bool = False,
    *,
    advance_cursor: bool = True,
) -> list:
    """
    获取地震快讯

    Args:
        context: 插件上下文
        force: 是否强制返回（手动触发模式）
    """
    since_id = await asyncio.to_thread(_load_since, context)
    since_id_int = int(since_id)

    def _do_fetch() -> list[_PreparedCard]:
        with _create_session() as session:
            _bootstrap_session(session, context)
            data = _fetch_weibo(session)

            found_card = None
            found_cards = []
            newest_seen_id = since_id_int

            for card in data["data"]["cards"]:
                mblog = card.get("mblog") or {}
                raw_text = mblog.get("text", "")
                if not isinstance(raw_text, str):
                    continue

                # 检查是否是地震快讯
                if "#地震快讯#" not in raw_text or "中国地震台网正式测定" not in raw_text:
                    continue

                mid = str(mblog.get("id", ""))
                if not mid:
                    continue
                try:
                    mid_int = int(mid)
                except (TypeError, ValueError):
                    continue

                is_new = mid_int > since_id_int
                if is_new:
                    newest_seen_id = max(newest_seen_id, mid_int)

                # 手动触发：直接返回最新的有效地震信息，不论是否看过或震级大小
                if force:
                    found_card = card
                    break

                # 定时任务：遇到旧消息后停止处理更旧数据
                if not is_new:
                    break

                clean_text = _extract_clean_text(raw_text)
                magnitude = _extract_magnitude(clean_text)

                if magnitude is not None and magnitude >= 4:
                    found_cards.append(card)
                    continue

                logger.info("Earthquake M%.1f < 4, skipping", magnitude or 0)

            if not force and newest_seen_id > since_id_int:
                if advance_cursor or not found_cards:
                    _save_since(context, str(newest_seen_id))
                elif isinstance(getattr(context, "state", None), dict):
                    context.state["earthquake_pending_since"] = str(newest_seen_id)

            cards_to_render = [found_card] if force and found_card else found_cards
            prepared: list[_PreparedCard] = []
            for card in reversed(cards_to_render):
                mblog = card.get("mblog") or {}
                raw_text = mblog.get("text", "")
                clean_text = _extract_clean_text(raw_text)
                figure_url = mblog.get("original_pic")
                prepared.append(
                    _PreparedCard(
                        clean_text=clean_text,
                        magnitude=_extract_magnitude(clean_text),
                        figure_url=figure_url
                        if isinstance(figure_url, str) and figure_url
                        else None,
                    )
                )
            return prepared

    try:
        prepared_cards = await run_sync(_do_fetch)
    except Exception as exc:
        if not force:
            public_error_message(
                context,
                exc,
                logger=logger,
                component="earthquake.fetch_scheduled",
            )
            return []
        return public_error_response(context, exc, logger=logger, component="earthquake.fetch")

    if not prepared_cards:
        if force:
            return segments("未获取到地震快讯数据")
        return []

    output = []
    for prepared in prepared_cards:
        output.append(text(prepared.clean_text))
        if prepared.figure_url:
            try:
                file_path = await _download_figure(context, prepared.figure_url)
                output.append(image(str(file_path)))
            except Exception as exc:
                public_error_message(
                    context,
                    exc,
                    logger=logger,
                    component="earthquake.download_image",
                )
        logger.info(
            "Earthquake notification prepared: magnitude=%.1f text_chars=%d",
            prepared.magnitude or 0,
            len(prepared.clean_text),
        )
    return output
