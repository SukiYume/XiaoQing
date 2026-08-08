"""安全抓取并展示 NASA Astronomy Picture of the Day 页面。"""

import hashlib
import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from core.args import parse
from core.bounded_file_cache import BoundedFileCache, FileCacheLimits
from core.image_validation import ImageValidationLimits, validate_image_bytes
from core.plugin_base import (
    PluginContextProtocol,
    Segments,
    bounded_external_text,
    image,
    run_sync,
    segments,
    text,
)
from core.public_errors import public_error_message, public_error_response
from core.safe_http import SafeHttpError, UnsafeUrlError, fetch_public_bytes, fetch_public_html

logger = logging.getLogger(__name__)

# ============================================================
# 常量配置
# ============================================================

HEADERS = {
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9",
}

DEFAULT_APOD_URL = "https://apod.nasa.gov/apod/astropix.html"
HTML_TIMEOUT_SECONDS = 15
IMAGE_TIMEOUT_SECONDS = 20
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_IMAGE_FRAMES = 120
IMAGE_CACHE_LIMITS = FileCacheLimits(
    max_entries=90,
    max_bytes=128 * 1024 * 1024,
    ttl_seconds=120 * 24 * 60 * 60,
)
DEFAULT_FALLBACK_TITLE = "Today's Astronomy Picture of the Day"
NO_EXPLANATION_TEXT = "No explanation found."
EXPLANATION_UNAVAILABLE = "Explanation unavailable."


# ============================================================
# 配置获取
# ============================================================


def _get_config(context: PluginContextProtocol) -> Mapping[str, Any]:
    """从当前原子设置代读取插件公开配置。"""

    return context.get_settings_snapshot().plugin_config("apod")


# ============================================================
# 辅助函数
# ============================================================


_IMAGE_MIME_FORMATS = {
    "image/gif": "GIF",
    "image/jpeg": "JPEG",
    "image/png": "PNG",
    "image/webp": "WEBP",
}
_IMAGE_FORMAT_EXTENSIONS = {"GIF": ".gif", "JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}


def _cache_filename(url: str, extension: str) -> str:
    """Build a Windows-safe cache name from a content-verified extension."""
    return f"{hashlib.sha256(url.encode('utf-8')).hexdigest()}{extension}"


def _allowed_hosts(context: PluginContextProtocol) -> set[str]:
    """合并 NASA 默认域名与管理员显式允许的附加域名。"""

    configured = _get_config(context).get("allowed_hosts", [])
    hosts = {"apod.nasa.gov"}
    if isinstance(configured, Sequence) and not isinstance(configured, (str, bytes, bytearray)):
        hosts.update(
            host.strip().rstrip(".").lower()
            for host in configured
            if isinstance(host, str) and host.strip().rstrip(".")
        )
    return hosts


def _require_allowed_url(
    url: str,
    context: PluginContextProtocol,
    *,
    allowed_hosts: set[str] | None = None,
) -> str:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").rstrip(".").lower()
    hosts = allowed_hosts if allowed_hosts is not None else _allowed_hosts(context)
    if parsed.scheme.lower() != "https" or host not in hosts:
        raise UnsafeUrlError("APOD URL host is not allowed")
    return url


def _require_https_display_url(url: str) -> str:
    """Validate a link that is displayed but never fetched by the bot."""
    parsed = urlsplit(url)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise UnsafeUrlError("media link is not an absolute HTTPS URL")
    return url


async def _safe_download_image(
    url: str,
    images_dir: Path,
    context: PluginContextProtocol,
) -> Path | None:
    allowed_hosts = _allowed_hosts(context)
    _require_allowed_url(url, context, allowed_hosts=allowed_hosts)
    fetched = await fetch_public_bytes(
        url,
        headers={**HEADERS, "accept-encoding": "identity"},
        timeout_seconds=IMAGE_TIMEOUT_SECONDS,
        max_bytes=MAX_IMAGE_BYTES,
        allowed_content_type_prefixes=("image/",),
        allowed_hosts=allowed_hosts,
    )
    if fetched is None:
        return None

    content_type = str(fetched.headers.get("Content-Type", "")).split(";", 1)[0].strip().casefold()
    expected_format = _IMAGE_MIME_FORMATS.get(content_type)
    if expected_format is None:
        raise ValueError("APOD image Content-Type is not supported")
    validated = await run_sync(
        validate_image_bytes,
        fetched.body,
        limits=ImageValidationLimits(
            max_bytes=MAX_IMAGE_BYTES,
            max_pixels=MAX_IMAGE_PIXELS,
            max_frames=MAX_IMAGE_FRAMES,
        ),
        format_extensions=_IMAGE_FORMAT_EXTENSIONS,
        expected_format=expected_format,
    )
    cache = BoundedFileCache(images_dir, IMAGE_CACHE_LIMITS)
    target = await run_sync(
        cache.put,
        _cache_filename(fetched.url, validated.extension),
        fetched.body,
    )
    if target is None:
        context.logger.warning("APOD 图片超过缓存总字节预算")
    return target


def _find_image_url(
    soup: BeautifulSoup,
    base_url: str,
    context: PluginContextProtocol,
    allowed_hosts: set[str],
) -> str | None:
    """Select an APOD image candidate without trusting document order."""

    candidates: list[str] = []
    preferred: list[str] = []
    for element in soup.find_all("img"):
        raw_src = element.attrs.get("src")
        if not isinstance(raw_src, str) or not raw_src.strip():
            continue
        candidate = urljoin(base_url, raw_src.strip())
        try:
            _require_allowed_url(candidate, context, allowed_hosts=allowed_hosts)
        except UnsafeUrlError:
            continue
        candidates.append(candidate)
        path_parts = {part.casefold() for part in urlsplit(candidate).path.split("/") if part}
        if "image" in path_parts:
            preferred.append(candidate)
    if preferred:
        return preferred[0]
    return candidates[0] if len(candidates) == 1 else None


def _extract_title(soup: BeautifulSoup, context: PluginContextProtocol) -> str:
    """按 APOD 页面结构、通用 ``center``、HTML 标题依次提取标题。"""
    try:
        # 策略1: 查找第二个 center 标签
        centers = soup.find_all("center")
        if len(centers) > 1 and centers[1].b:
            title_text = centers[1].b.string
            if title_text:
                return str(title_text).strip()

        # 策略2: 查找任何有内容的 center 标签中的 b 标签
        for center in centers:
            if center.b and center.b.string:
                return str(center.b.string).strip()

        # 策略3: 使用 title 标签
        if soup.title and soup.title.string:
            return str(soup.title.string).strip()

    except Exception as exc:
        context.logger.debug("APOD title extraction fell back to default: %s", exc)

    return DEFAULT_FALLBACK_TITLE


def get_explanation(
    soup: BeautifulSoup | None,
    context: PluginContextProtocol,
) -> str:
    """从页面提取说明，并移除指向次日内容的页脚。"""
    if not soup:
        return NO_EXPLANATION_TEXT

    try:
        paragraphs = soup.find_all("p")
        for paragraph in paragraphs:
            bold = paragraph.find("b")
            if bold and bold.string and bold.string.strip() == "Explanation:":
                explanation = str(paragraph.get_text()).strip()
                # 移除 "Tomorrow's picture:" 之后的内容
                return explanation.split("Tomorrow's picture:", 1)[0].strip()
        return NO_EXPLANATION_TEXT
    except (AttributeError, IndexError) as exc:
        public_error_message(
            context,
            exc,
            logger=context.logger,
            component="apod.parse_explanation",
        )
        return EXPLANATION_UNAVAILABLE


def _tag_source(element: Tag | None) -> str | None:
    """读取媒体标签的字符串 ``src``；列表等畸形属性按缺失处理。"""

    if element is None:
        return None
    raw_source = element.attrs.get("src")
    if not isinstance(raw_source, str) or not raw_source.strip():
        return None
    return raw_source.strip()


def _video_unavailable(title: str, explanation: str, page_url: str) -> Segments:
    return segments(f"[视频无法获取链接]\n\n{title}\n\n{explanation}\n\n原网址: {page_url}")


async def _render_image(
    image_url: str,
    *,
    images_dir: Path,
    title: str,
    explanation: str,
    context: PluginContextProtocol,
) -> Segments:
    """下载并校验图片；图片增强失败时保留标题、说明和原链接。"""

    context.logger.info("发现图片: %s", image_url)
    await run_sync(images_dir.mkdir, parents=True, exist_ok=True)
    try:
        image_path = await _safe_download_image(image_url, images_dir, context)
    except (SafeHttpError, TimeoutError, OSError, ValueError) as exc:
        # APOD 正文已经可用时，图片只是可选增强，不能把整条命令升级为内部错误。
        context.logger.warning("APOD 图片下载或校验失败: %s", exc)
        image_path = None
    if image_path is None:
        return segments(
            f"⚠️ 图片暂时下载失败，可稍后重试或直接查看：{image_url}\n\n{title}\n\n{explanation}"
        )
    return [image(str(image_path)), text(f"{title}\n\n{explanation}")]


def _render_iframe(
    iframe: Tag,
    *,
    base_url: str,
    page_url: str,
    title: str,
    explanation: str,
    context: PluginContextProtocol,
) -> Segments:
    source = _tag_source(iframe)
    if source is None:
        return _video_unavailable(title, explanation, page_url)
    video_url = _require_https_display_url(urljoin(base_url, source))
    context.logger.info("发现 iframe 视频: %s", video_url)
    return segments(f"{video_url}\n\n{title}\n\n{explanation}")


def _render_video(
    video: Tag,
    *,
    base_url: str,
    page_url: str,
    title: str,
    explanation: str,
    context: PluginContextProtocol,
) -> Segments:
    context.logger.info("发现 video 标签视频")
    nested_source = video.find("source")
    source = _tag_source(nested_source if isinstance(nested_source, Tag) else None)
    if source is None:
        source = _tag_source(video)
    if source is None:
        return _video_unavailable(title, explanation, page_url)
    video_url = _require_https_display_url(urljoin(base_url, source))
    return segments(f"{video_url}\n\n{title}\n\n{explanation}")


async def _render_page(
    soup: BeautifulSoup,
    *,
    base_url: str,
    page_url: str,
    images_dir: Path,
    allowed_hosts: set[str],
    context: PluginContextProtocol,
) -> Segments:
    """按图片、iframe、video 的优先级把已验证页面转换为消息段。"""

    title = _extract_title(soup, context)
    explanation = get_explanation(soup, context)

    image_url = _find_image_url(soup, base_url, context, allowed_hosts)
    if image_url is not None:
        return await _render_image(
            image_url,
            images_dir=images_dir,
            title=title,
            explanation=explanation,
            context=context,
        )

    iframe = soup.find("iframe")
    if isinstance(iframe, Tag):
        return _render_iframe(
            iframe,
            base_url=base_url,
            page_url=page_url,
            title=title,
            explanation=explanation,
            context=context,
        )

    video = soup.find("video")
    if isinstance(video, Tag):
        return _render_video(
            video,
            base_url=base_url,
            page_url=page_url,
            title=title,
            explanation=explanation,
            context=context,
        )

    return segments(f"今天的 APOD 内容格式不支持，请直接访问: {page_url}")


# ============================================================
# 主处理函数
# ============================================================


async def handle(
    command: str,
    args: str,
    event: dict[str, Any],
    context: PluginContextProtocol,
) -> Segments:
    """处理手动 APOD 查询；只接受空参数或精确的 help 子命令。"""
    del command, event
    try:
        parsed = parse(args)

        # 解析子命令
        if parsed and parsed.first:
            if len(parsed) == 1 and parsed.first.lower() in {"help", "帮助"}:
                return segments(_show_help())
            visible_command = bounded_external_text(
                parsed.first,
                max_chars=32,
                max_bytes=128,
                default="未知",
            )
            return segments(f"未知命令: {visible_command}\n输入 /apod help 查看帮助")

        logger.info("开始获取 APOD...")

        # 配置只决定入口与额外域名；每次重定向仍由 safe_http 重新校验。
        configured_url = _get_config(context).get("url", DEFAULT_APOD_URL)
        url = configured_url if isinstance(configured_url, str) else DEFAULT_APOD_URL
        allowed_hosts = _allowed_hosts(context)
        page_url = _require_allowed_url(url, context, allowed_hosts=allowed_hosts)
        response = await fetch_public_html(
            page_url,
            headers={**HEADERS, "accept-encoding": "identity"},
            timeout_seconds=HTML_TIMEOUT_SECONDS,
            allowed_hosts=allowed_hosts,
        )
        if response is None or not response.body:
            error_msg = "❌ 获取失败: 网络错误"
            logger.error(error_msg)
            return segments(error_msg)

        soup = await run_sync(BeautifulSoup, response.body, "html.parser")
        return await _render_page(
            soup,
            base_url=response.url,
            page_url=page_url,
            images_dir=context.data_dir / "images",
            allowed_hosts=allowed_hosts,
            context=context,
        )

    except Exception as exc:
        return public_error_response(context, exc, logger=logger, component="apod.handle")


def _show_help() -> str:
    """返回适合聊天窗口直接展示的简明帮助。"""
    return """
🌌 **每日一天文图 (APOD)**

**基本用法:**
• /apod - 获取今日天文图片
• /apod help - 显示帮助信息

**功能特点:**
✨ 自动获取 NASA 每日天文图片
📷 支持图片自动下载与缓存
📝 提供图片说明和描述
🎬 支持视频链接展示
🛡️ HTTPS、主机、响应字节、MIME 与图片像素受限校验

输入 /apod 获取今日天文美图
""".strip()


# ============================================================
# 定时任务
# ============================================================


async def scheduled(context: PluginContextProtocol) -> Segments:
    """
    定时任务入口

    每天 13:30 自动推送 APOD 到配置的群组
    """
    context.logger.info("执行 APOD 定时任务...")

    # handle 的统一契约仍要求 event；实际投递目标来自 Core 签发的 principal
    # 与 manifest，不能由这个空占位事件决定。
    return await handle(command="apod", args="", event={}, context=context)
