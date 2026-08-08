"""为完整的单 URL 消息生成有限、无凭据的网页预览。"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, TypeVar, cast
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from core.bounded_file_cache import BoundedFileCache, FileCacheLimits
from core.image_validation import (
    ImageValidationError,
    ImageValidationLimits,
    validate_image_bytes,
    validate_image_path,
)
from core.plugin_base import image as _core_image
from core.plugin_base import run_sync as _core_run_sync
from core.plugin_base import segments as _core_segments
from core.public_errors import public_error_message
from core.safe_http import (
    SafeHttpError,
    UnsafeUrlError,
    fetch_public_bytes,
    fetch_public_html,
    validate_public_fetch_target,
)

MessageSegment = dict[str, Any]
MessageSegments = list[MessageSegment]
OneBotEvent = dict[str, Any]
T = TypeVar("T")


class Context(Protocol):
    """本插件实际读取的最小运行时上下文。"""

    data_dir: Path


class _RunSync(Protocol):
    async def __call__(
        self,
        function: Callable[..., T],
        *args: Any,
        **kwargs: Any,
    ) -> T: ...


# ──────────────────── Core 边界与资源上限 ────────────────────


segments = cast(Callable[[object], MessageSegments], _core_segments)
image_segment = cast(Callable[[str], MessageSegment], _core_image)
run_sync = cast(_RunSync, _core_run_sync)

logger = logging.getLogger(__name__)

MAX_INPUT_URL_LENGTH = 2048
MAX_HTML_BYTES = 2 * 1024 * 1024
MAX_TITLE_LENGTH = 200
MAX_DESC_LENGTH = 100
MAX_IMAGE_URL_LENGTH = 4096
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000
MAX_IMAGE_FRAMES = 120
MAX_CONCURRENT_PREVIEWS = 4
REQUEST_TIMEOUT = 10

PREVIEW_CACHE_LIMITS = FileCacheLimits(
    max_entries=128,
    max_bytes=128 * 1024 * 1024,
    ttl_seconds=7 * 24 * 60 * 60,
)

_PREVIEW_EXTENSIONS = (".png", ".webp", ".jpg")
_IMAGE_FORMAT_EXTENSIONS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36"
)
_PREVIEW_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_PREVIEWS)


def _preview_cache(context: Context) -> BoundedFileCache:
    """返回固定在插件数据目录内的有限预览缓存。"""

    return BoundedFileCache(Path(context.data_dir) / "url_previews", PREVIEW_CACHE_LIMITS)


# ──────────────────── HTML 元数据解析 ────────────────────


def _compact_text(value: object, max_chars: int) -> str:
    """折叠网页元数据中的空白，并把最终文本限制在消息预算内。"""

    if not isinstance(value, str):
        return ""
    compact = " ".join(value.split())
    if len(compact) <= max_chars:
        return compact
    return f"{compact[: max_chars - 3].rstrip()}..."


def _meta_content(
    soup: BeautifulSoup,
    selectors: tuple[tuple[str, str], ...],
) -> str:
    """按优先级读取第一个字符串类型的 ``meta content``。"""

    for attribute, value in selectors:
        tag = soup.find("meta", attrs={attribute: value})
        if tag is None:
            continue
        content = tag.get("content")
        if isinstance(content, str):
            return content
    return ""


def _parse_preview_html(html: str) -> tuple[str, str, str]:
    """提取并收窄标题、描述和预览图片引用。"""

    soup = BeautifulSoup(html, "html.parser")
    raw_title = soup.title.get_text(" ", strip=True) if soup.title is not None else ""
    title = _compact_text(raw_title, MAX_TITLE_LENGTH)
    description = _compact_text(
        _meta_content(
            soup,
            (
                ("name", "description"),
                ("property", "og:description"),
                ("name", "twitter:description"),
            ),
        ),
        MAX_DESC_LENGTH,
    )
    image_reference = _meta_content(
        soup,
        (("property", "og:image"), ("name", "twitter:image")),
    ).strip()
    if len(image_reference) > MAX_IMAGE_URL_LENGTH:
        image_reference = ""
    return title, description, image_reference


def _decode_html(body: bytes, charset: str | None) -> str:
    """使用响应字符集解码，未知字符集回退 UTF-8。"""

    encoding = charset.strip() if isinstance(charset, str) and charset.strip() else "utf-8"
    try:
        return body.decode(encoding, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def _detect_image_extension(payload: bytes) -> str:
    """用 core 的逐帧校验返回真实图片格式。"""

    limits = ImageValidationLimits(
        max_bytes=MAX_IMAGE_BYTES,
        max_pixels=MAX_IMAGE_PIXELS,
        max_frames=MAX_IMAGE_FRAMES,
    )
    try:
        return validate_image_bytes(
            payload,
            limits=limits,
            format_extensions=_IMAGE_FORMAT_EXTENSIONS,
        ).extension
    except ImageValidationError as exc:
        if exc.reason == "unsupported_format":
            message = "unsupported preview image format"
        elif exc.reason in {"dimension_limit", "invalid_dimensions", "pixel_limit"}:
            message = "preview image dimensions exceed the configured limit"
        elif exc.reason == "frame_limit":
            message = "preview image frame count exceeds the configured limit"
        else:
            message = "preview response is not a valid image"
        raise ValueError(message) from exc


# ──────────────────── 预览图校验与缓存 ────────────────────


def _validated_cached_preview(
    cache: BoundedFileCache,
    names: tuple[str, ...],
) -> Path | None:
    """只返回内容与扩展名均有效的旧缓存，并清除历史损坏项。"""

    for name in names:
        path = cast(Path | None, cache.get_any((name,)))
        if path is None:
            continue
        try:
            validate_image_path(
                path,
                limits=ImageValidationLimits(
                    max_bytes=MAX_IMAGE_BYTES,
                    max_pixels=MAX_IMAGE_PIXELS,
                    max_frames=MAX_IMAGE_FRAMES,
                ),
                format_extensions=_IMAGE_FORMAT_EXTENSIONS,
            )
            return path
        except (ImageValidationError, OSError, ValueError):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.debug("损坏的 URL 预览缓存暂时无法删除")
    return None


async def _cache_preview_image(
    page_url: str,
    image_reference: str,
    context: Context,
) -> str | None:
    """验证、下载并缓存可选预览图；失败时保留文字预览。"""

    image_url = urljoin(page_url, image_reference)
    if not image_url or len(image_url) > MAX_IMAGE_URL_LENGTH:
        return None

    try:
        # 缓存命中前仍重新验证目标，避免旧缓存绕过当前 URL/DNS 安全策略。
        await validate_public_fetch_target(image_url)
        cache = _preview_cache(context)
        digest = hashlib.sha256(image_url.encode("utf-8")).hexdigest()
        cached = await run_sync(
            _validated_cached_preview,
            cache,
            tuple(f"{digest}{extension}" for extension in _PREVIEW_EXTENSIONS),
        )
        if cached is not None:
            return str(cached)

        fetched = await fetch_public_bytes(
            image_url,
            headers={"User-Agent": _USER_AGENT},
            timeout_seconds=REQUEST_TIMEOUT,
            max_bytes=MAX_IMAGE_BYTES,
            allowed_content_type_prefixes=("image/",),
            allowed_schemes=("http", "https"),
        )
        if fetched is None or not fetched.body or len(fetched.body) > MAX_IMAGE_BYTES:
            return None

        extension = await run_sync(_detect_image_extension, fetched.body)
        image_path = await run_sync(cache.put, f"{digest}{extension}", fetched.body)
        return str(image_path) if image_path is not None else None
    except (SafeHttpError, UnsafeUrlError, ValueError) as exc:
        logger.debug("URL 预览图片被安全拒绝: error_type=%s", type(exc).__name__)
        return None
    except Exception as exc:
        public_error_message(
            context,
            exc,
            logger=logger,
            component="url_parser.preview_image",
        )
        return None


async def _build_preview(url: str, context: Context) -> MessageSegments:
    """获取页面并组装文字与可选图片消息段。"""

    fetched = await fetch_public_html(
        url,
        headers={"User-Agent": _USER_AGENT},
        timeout_seconds=REQUEST_TIMEOUT,
    )
    if fetched is None or len(fetched.body) > MAX_HTML_BYTES:
        return []

    html = _decode_html(fetched.body, fetched.charset)
    title, description, image_reference = await run_sync(_parse_preview_html, html)
    image_path = (
        await _cache_preview_image(fetched.url, image_reference, context)
        if image_reference
        else None
    )
    if not title and not description and image_path is None:
        logger.debug("URL 未提取到预览内容: host=%s", urlsplit(url).hostname or "")
        return []

    response: MessageSegments = []
    if title or description:
        lines = [f"🔗 {title or '网页预览'}"]
        if description:
            lines.append(description)
        lines.extend(("", f"链接: {url}"))
        response.extend(segments("\n".join(lines)))
    if image_path is not None:
        response.append(image_segment(image_path))

    logger.info(
        "URL 解析成功: host=%s title_length=%d",
        urlsplit(url).hostname or "",
        len(title),
    )
    return response


# ──────────────────── Dispatcher 与插件入口 ────────────────────


async def handle_url(url: str, event: OneBotEvent, context: Context) -> MessageSegments:
    """处理 dispatcher 筛选后的完整单 URL 消息。"""

    del event  # URL 预览不读取原始 OneBot 事件。
    if not isinstance(url, str) or not url or len(url) > MAX_INPUT_URL_LENGTH:
        return []
    try:
        async with _PREVIEW_SEMAPHORE:
            return await _build_preview(url, context)
    except (SafeHttpError, UnsafeUrlError) as exc:
        logger.debug("URL 页面请求被安全拒绝: error_type=%s", type(exc).__name__)
        return []
    except Exception as exc:
        public_error_message(
            context,
            exc,
            logger=logger,
            component="url_parser.handle_url",
        )
        return []


async def handle(
    command: str,
    args: str,
    event: OneBotEvent,
    context: Context,
) -> MessageSegments:
    """保留 PluginManager 所需入口；本插件没有显式命令。"""

    del command, args, event, context
    return []
