"""从指定 X/Twitter 账号抓取图片，并从本地缓存随机发送。

API 身份信息只从当前原子 settings 快照的 ``plugins.twitter`` 秘密命名空间读取；媒体文件则始终以
不含认证信息的公共请求下载。抓取结果使用内容哈希命名，并受数量、容量和 TTL
共同约束，避免远端文件名、异常响应或长期运行无限占用本地资源。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import urlencode, urlsplit, urlunsplit

from core.args import parse
from core.atomic_store import atomic_write_text
from core.bounded_file_cache import BoundedFileCache, FileCacheLimits
from core.bounded_http import (
    JSON_MIME_POLICY,
    BodyLimits,
    HttpStatusError,
    JsonLimits,
    MimePolicy,
    RedirectPolicy,
    ResponseFormatError,
    aiohttp_request_bounded,
    parse_bounded_json,
)
from core.delivery import DeliveryReceipt, DeliverySegments
from core.image_validation import (
    ImageValidationError,
    ImageValidationLimits,
    validate_image_bytes,
    validate_image_path,
)
from core.interfaces import PluginSettingsSnapshot
from core.plugin_base import build_action as _core_build_action
from core.plugin_base import ensure_dir as _core_ensure_dir
from core.plugin_base import gather_bounded
from core.plugin_base import has_control_characters as _has_control_chars
from core.plugin_base import image as _core_image
from core.plugin_base import segments as _core_segments
from core.public_errors import public_error_message
from core.public_errors import public_error_response as _core_public_error_response
from core.safe_http import fetch_public_bytes

MessageSegment = dict[str, Any]
MessageSegments = list[MessageSegment]
OneBotEvent = dict[str, Any]
TimelineEntry = dict[str, Any]


class Context(Protocol):
    """本插件实际使用的最小运行时上下文。"""

    data_dir: Path
    http_session: Any
    current_user_id: int | None
    current_group_id: int | None

    def get_settings_snapshot(self) -> PluginSettingsSnapshot: ...

    async def send_action(self, action: dict[str, Any]) -> object: ...


class TwitterFetchError(RuntimeError):
    """远端时间线请求失败；只保留可安全展示的 HTTP 状态。"""

    def __init__(self, *, status: int | None = None) -> None:
        self.status = status
        message = (
            f"Twitter API returned HTTP {status}"
            if status is not None
            else "Twitter API request failed"
        )
        super().__init__(message)

    def user_message(self) -> str:
        if self.status is not None:
            return f"❌ Twitter 图片抓取失败：远端接口返回 HTTP {self.status}"
        return "❌ Twitter 图片抓取失败：远端接口暂时不可用，请稍后重试"


class TwitterMediaFetchError(RuntimeError):
    """时间线已读取，但本轮所有媒体下载均失败。"""

    def __init__(self, attempted: int) -> None:
        self.attempted = attempted
        super().__init__("all Twitter media downloads failed")

    def user_message(self) -> str:
        return "❌ Twitter 图片抓取失败：时间线读取成功，但图片下载全部失败，请检查代理或稍后重试"


@dataclass(frozen=True)
class _FetchOutcome:
    """后台抓取结果；只保存可以直接发给用户的脱敏消息。"""

    count: int
    message: str
    succeeded: bool


segments = cast(Callable[[object], MessageSegments], _core_segments)
image = cast(Callable[[str], MessageSegment], _core_image)
build_action = cast(
    Callable[[MessageSegments, int | None, int | None], dict[str, Any] | None],
    _core_build_action,
)
ensure_dir = cast(Callable[[Path], None], _core_ensure_dir)
public_error_response = cast(Callable[..., MessageSegments], _core_public_error_response)

logger = logging.getLogger(__name__)

DEFAULT_USER_ID = "123456789012345678"
MAX_PAGES_WITHOUT_NEW_IMAGES = 2
MAX_PAGES_TO_CHECK = 50
MAX_CONCURRENT_IMAGE_DOWNLOADS = 4
REQUEST_TIMEOUT_SECONDS = 30
MAX_API_BYTES = 5 * 1024 * 1024
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_IMAGE_FRAMES = 120
MAX_IMAGE_CACHE_BYTES = 2 * 1024 * 1024 * 1024
MAX_POSTED_STATE_BYTES = 1024 * 1024
MAX_BACKFILL_STATE_BYTES = 1024
MAX_MEDIA_URL_CHARS = 4096
MAX_USER_ID_CHARS = 128
MAX_CURSOR_CHARS = 4096
BACKFILL_STATE_FILENAME = "backfill_complete.json"
BACKFILL_STATE_VERSION = 1

IMAGE_CACHE_LIMITS = FileCacheLimits(
    max_entries=5_000,
    max_bytes=MAX_IMAGE_CACHE_BYTES,
    ttl_seconds=90 * 24 * 60 * 60,
)

_TIMELINE_URL = "https://x.com/i/api/graphql/mF05yo9gtSsl1tFPPHNEgQ/UserTweets"
_TWITTER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36"
)
_ALLOWED_TWITTER_MEDIA_HOSTS = frozenset({"pbs.twimg.com", "ton.twitter.com", "video.twimg.com"})
_IMAGE_FORMAT_EXTENSIONS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}
_ALLOWED_LOCAL_IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp"})
_HEADER_NAME_PATTERN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
_CACHE_FILENAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_TRANSPORT_MANAGED_HEADERS = frozenset(
    {"connection", "content-length", "host", "proxy-authorization", "transfer-encoding"}
)
_HELP_ALIASES = frozenset({"help", "帮助", "?"})
_TWIMG_HELP = (
    "🎨 推特图片命令\n"
    "• /twimg\n"
    "  只读取本地缓存并随机发送一张图\n"
    "• /twitter、/推特\n"
    "  /twimg 的别名\n"
    "• /twimg help\n"
    "随机发送不会临时联网抓取；缓存为空时请由管理员执行 /tw_fetch，"
    "新图片也会由每日定时任务补充。"
)
_TW_FETCH_HELP = (
    "🔄 抓取推特图片\n"
    "• /tw_fetch\n"
    "  后台抓取配置账号的新图片\n"
    "• /抓取推特\n"
    "  /tw_fetch 的别名\n"
    "• /tw_fetch help\n"
    "此命令仅限管理员，提交后会立即返回，完成时另行通知；"
    "首次运行会回填允许分页内的全部图片，完成后只检查新图，"
    "连续两页无新增即停止；插件也会在每天 03:00 自动后台抓取。"
)

_API_BODY_LIMITS = BodyLimits(
    max_wire_bytes=MAX_API_BYTES,
    max_decoded_bytes=MAX_API_BYTES,
    max_decompression_ratio=20,
)
_API_JSON_LIMITS = JsonLimits(
    max_bytes=MAX_API_BYTES,
    max_depth=48,
    max_nodes=100_000,
    max_string_chars=2 * 1024 * 1024,
)
_MEDIA_BODY_LIMITS = BodyLimits(
    max_wire_bytes=MAX_IMAGE_BYTES,
    max_decoded_bytes=MAX_IMAGE_BYTES,
    max_decompression_ratio=2,
    ratio_grace_bytes=64 * 1024,
    chunk_bytes=64 * 1024,
)
_MEDIA_MIME_POLICY = MimePolicy(type_prefixes=frozenset({"image/"}))
_MEDIA_REDIRECT_POLICY = RedirectPolicy(
    max_hops=3,
    allowed_schemes=frozenset({"https"}),
    allowed_origins=frozenset(f"https://{host}" for host in _ALLOWED_TWITTER_MEDIA_HOSTS),
    same_origin_only=False,
)
_TIMELINE_FEATURES = {
    "responsive_web_enhance_cards_enabled": True,
    "rweb_video_screen_enabled": False,
    "profile_label_improvements_pcf_label_in_post_enabled": False,
    "rweb_tipjar_consumption_enabled": False,
    "verified_phone_label_enabled": False,
    "creator_subscriptions_tweet_preview_api_enabled": False,
    "responsive_web_graphql_timeline_navigation_enabled": False,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "premium_content_api_read_enabled": False,
    "communities_web_enable_tweet_community_results_fetch": False,
    "c9s_tweet_anatomy_moderator_badge_enabled": False,
    "responsive_web_grok_analyze_button_fetch_trends_enabled": False,
    "responsive_web_grok_analyze_post_followups_enabled": False,
    "responsive_web_jetfuel_frame": False,
    "responsive_web_grok_share_attachment_enabled": False,
    "articles_preview_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "tweet_awards_web_tipping_enabled": False,
    "responsive_web_grok_show_grok_translated_post": False,
    "responsive_web_grok_analysis_button_from_backend": False,
    "creator_subscriptions_quote_tweet_preview_enabled": False,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "responsive_web_grok_image_annotation_enabled": False,
}
_TIMELINE_FIELD_TOGGLES = {"withArticlePlainText": False}

# 发送状态与整轮抓取分别串行化；普通随机取图仍可与抓取并行。
_POSTED_LOCK = asyncio.Lock()
_FETCH_LOCK = asyncio.Lock()
# 这些句柄属于当前模块代次，不是用户数据：卸载代次前由 shutdown() 取消；
# 只有普通插件事件需要共享或必须持久化的状态才应放入 context.state。
_FETCH_TASK: asyncio.Task[_FetchOutcome] | None = None
_MANUAL_NOTIFICATION_TASK: asyncio.Task[None] | None = None
_POSTED_RESERVATIONS: dict[str, set[str]] = {}


# ──────────────────── 生命周期与配置 ────────────────────


async def shutdown(context: Context | None = None) -> None:
    """取消插件拥有的后台抓取，避免热重载后遗留旧模块任务。"""

    del context
    global _FETCH_TASK, _MANUAL_NOTIFICATION_TASK

    tasks = {
        task
        for task in (_FETCH_TASK, _MANUAL_NOTIFICATION_TASK)
        if task is not None and not task.done()
    }
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _FETCH_TASK = None
    _MANUAL_NOTIFICATION_TASK = None
    async with _POSTED_LOCK:
        _POSTED_RESERVATIONS.clear()


def _get_config(context: Context) -> Mapping[str, object]:
    """读取当前原子设置代中的插件密钥配置。"""

    return context.get_settings_snapshot().plugin_secrets("twitter")


def _get_headers(context: Context) -> dict[str, str]:
    """构造 API 请求头，并忽略畸形或由传输层管理的自定义字段。"""

    headers = {"Accept": "application/json", "User-Agent": _TWITTER_USER_AGENT}
    custom_headers = _get_config(context).get("headers", {})
    if not isinstance(custom_headers, Mapping):
        return headers

    for key, value in islice(custom_headers.items(), 64):
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        name = key.strip()
        content = value.strip()
        if (
            not name
            or not content
            or len(name) > 128
            or len(content) > 8192
            or _HEADER_NAME_PATTERN.fullmatch(name) is None
            or _has_control_chars(content)
            or name.casefold() in _TRANSPORT_MANAGED_HEADERS
        ):
            continue
        previous_name = next(
            (existing for existing in headers if existing.casefold() == name.casefold()),
            None,
        )
        if previous_name is not None:
            del headers[previous_name]
        headers[name] = content
    return headers


def _get_media_headers() -> dict[str, str]:
    """构造不含 API 凭据和 Cookie 的公共媒体请求头。"""

    return {
        "Accept": "image/*",
        "Accept-Encoding": "identity",
        "User-Agent": _TWITTER_USER_AGENT,
    }


def _get_proxy(context: Context) -> str | None:
    """读取显式配置的 HTTP(S) 代理；非法值不传给客户端。"""

    value = _get_config(context).get("proxy")
    if not isinstance(value, str):
        return None
    if _has_control_chars(value):
        return None
    proxy = value.strip()
    if not proxy or len(proxy) > 2048:
        return None
    try:
        parsed = urlsplit(proxy)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.hostname
        or parsed.query
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65_535)
    ):
        return None
    return proxy


def _get_user_id(context: Context) -> str:
    """读取目标用户 ID，并兼容配置快照中的正整数。"""

    value = _get_config(context).get("user_id")
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return str(value)
    if isinstance(value, str):
        user_id = value.strip()
        if user_id and len(user_id) <= MAX_USER_ID_CHARS and not _has_control_chars(user_id):
            return user_id
    return DEFAULT_USER_ID


def _get_cookies(context: Context) -> dict[str, str]:
    """读取有限的字符串 Cookie 映射，拒绝嵌套值和控制字符。"""

    configured = _get_config(context).get("cookies", {})
    if not isinstance(configured, Mapping):
        return {}

    cookies: dict[str, str] = {}
    for key, value in islice(configured.items(), 100):
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        name = key.strip()
        content = value.strip()
        if (
            name
            and content
            and len(name) <= 256
            and len(content) <= 4096
            and not _has_control_chars(name)
            and not _has_control_chars(content)
        ):
            cookies[name] = content
    return cookies


def _get_max_pages(context: Context) -> int:
    """读取 1 到 50 之间的整数页数；其他类型使用默认值。"""

    value = _get_config(context).get("max_pages", MAX_PAGES_TO_CHECK)
    if not isinstance(value, int) or isinstance(value, bool):
        return MAX_PAGES_TO_CHECK
    return max(1, min(value, MAX_PAGES_TO_CHECK))


def _backfill_is_complete(path: Path, user_id: str) -> bool:
    """仅接纳当前账号的小型全量回填标记；畸形状态会安全触发重新回填。"""

    try:
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_size > MAX_BACKFILL_STATE_BYTES
        ):
            return False
        payload = path.read_bytes()
        if len(payload) > MAX_BACKFILL_STATE_BYTES:
            return False
        state = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return (
        isinstance(state, Mapping)
        and state.get("version") == BACKFILL_STATE_VERSION
        and state.get("user_id") == user_id
    )


def _write_backfill_state(path: Path, user_id: str) -> None:
    """原子记录当前账号已经完成首次全量回填。"""

    payload = json.dumps(
        {"version": BACKFILL_STATE_VERSION, "user_id": user_id},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    atomic_write_text(path, f"{payload}\n")


# ──────────────────── 时间线与媒体下载 ────────────────────


def _is_allowed_media_url(url: str) -> bool:
    """仅允许无用户信息、标准端口的 Twitter HTTPS 媒体地址。"""

    if not isinstance(url, str) or not url or len(url) > MAX_MEDIA_URL_CHARS:
        return False
    if _has_control_chars(url):
        return False
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme.casefold() == "https"
        and parsed.hostname in _ALLOWED_TWITTER_MEDIA_HOSTS
        and parsed.username is None
        and parsed.password is None
        and port in {None, 443}
        and bool(parsed.path)
    )


def _original_media_url(url: str) -> str:
    """把 pbs.twimg.com 的常见带扩展名地址规范化为受控尺寸图请求。"""

    parsed = urlsplit(url)
    path = parsed.path
    suffix = Path(path).suffix.casefold()
    if parsed.hostname == "pbs.twimg.com" and suffix in {".jpg", ".jpeg"}:
        path = path[: -len(suffix)]
        query = urlencode({"format": "jpg", "name": "large"})
    else:
        query = parsed.query
    return urlunsplit(("https", parsed.netloc, path, query, ""))


async def _fetch_timeline(
    context: Context,
    cursor: str | None = None,
) -> tuple[list[TimelineEntry], str | None, bool]:
    """获取一页用户时间线，并从不稳定的 GraphQL 结构中提取推文和游标。"""

    variables: dict[str, object] = {
        "userId": _get_user_id(context),
        "count": 100,
        "includePromotedContent": False,
        "withCommunity": False,
        "withVoice": False,
        "include_entities": True,
        "include_user_entities": True,
        "include_ext_media_availability": True,
        "include_ext_alt_text": True,
        "include_cards": True,
        "tweet_mode": "extended",
    }
    if (
        isinstance(cursor, str)
        and cursor
        and len(cursor) <= MAX_CURSOR_CHARS
        and not _has_control_chars(cursor)
    ):
        variables["cursor"] = cursor

    params = {
        "variables": json.dumps(variables, ensure_ascii=False, separators=(",", ":")),
        "features": json.dumps(_TIMELINE_FEATURES, separators=(",", ":")),
        "fieldToggles": json.dumps(_TIMELINE_FIELD_TOGGLES, separators=(",", ":")),
    }

    try:
        response = await aiohttp_request_bounded(
            context.http_session,
            "GET",
            _TIMELINE_URL,
            limits=_API_BODY_LIMITS,
            mime_policy=JSON_MIME_POLICY,
            headers=_get_headers(context),
            request_kwargs={
                "params": params,
                "cookies": _get_cookies(context),
                "proxy": _get_proxy(context),
                "timeout": REQUEST_TIMEOUT_SECONDS,
            },
        )
        data: object = parse_bounded_json(response, limits=_API_JSON_LIMITS)
        if not isinstance(data, Mapping):
            raise ResponseFormatError("Twitter API response must be a JSON object")

        current: object = data
        # GraphQL 的 timeline 外壳是 timeline.timeline，两层同名字段都是协议结构。
        for key in ("data", "user", "result", "timeline", "timeline"):
            current = current.get(key, {}) if isinstance(current, Mapping) else {}
        instructions = current.get("instructions", []) if isinstance(current, Mapping) else []
        if not isinstance(instructions, list):
            instructions = []

        entries: list[object] = []
        for instruction in instructions:
            if not isinstance(instruction, Mapping):
                continue
            if instruction.get("type") != "TimelineAddEntries":
                continue
            candidate = instruction.get("entries", [])
            entries = candidate if isinstance(candidate, list) else []
            break

        tweets: list[TimelineEntry] = []
        next_cursor: str | None = None
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            entry_id = entry.get("entryId")
            if isinstance(entry_id, str) and entry_id.startswith("tweet-"):
                tweets.append(cast(TimelineEntry, entry))
                continue
            content = entry.get("content")
            if (
                next_cursor is None
                and isinstance(entry_id, str)
                and entry_id.startswith("cursor-bottom-")
                and isinstance(content, Mapping)
            ):
                value = content.get("value")
                if (
                    isinstance(value, str)
                    and value
                    and len(value) <= MAX_CURSOR_CHARS
                    and not _has_control_chars(value)
                ):
                    next_cursor = value

        return tweets, next_cursor, next_cursor is not None
    except HttpStatusError as exc:
        logger.warning("Twitter API 返回 HTTP %s", exc.status)
        raise TwitterFetchError(status=exc.status) from exc
    except Exception as exc:
        raise TwitterFetchError() from exc


def _extract_image_urls(tweet: Mapping[str, Any]) -> list[str]:
    """逐项提取照片 URL；单个畸形媒体项不会丢弃同一推文中的其他图片。"""

    current: object = tweet
    for key in (
        "content",
        "itemContent",
        "tweet_results",
        "result",
        "legacy",
        "extended_entities",
        "media",
    ):
        current = current.get(key, {}) if isinstance(current, Mapping) else {}
    if not isinstance(current, list):
        return []

    urls: list[str] = []
    for item in current:
        if not isinstance(item, Mapping) or item.get("type") != "photo":
            continue
        url = item.get("media_url_https")
        if isinstance(url, str) and url.strip():
            urls.append(url.strip())
    return urls


def _detect_image_extension(payload: bytes) -> str:
    """用 core 的逐帧校验返回由实际内容决定的扩展名。"""

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
            message = "unsupported Twitter image format"
        elif exc.reason in {"dimension_limit", "invalid_dimensions", "pixel_limit"}:
            message = "Twitter image dimensions exceed the configured limit"
        elif exc.reason == "frame_limit":
            message = "Twitter image frame count exceeds the configured limit"
        else:
            message = "Twitter media response is not a valid supported image"
        raise ValueError(message) from exc


def _validate_cached_image(path: Path) -> None:
    validate_image_path(
        path,
        limits=ImageValidationLimits(
            max_bytes=MAX_IMAGE_BYTES,
            max_pixels=MAX_IMAGE_PIXELS,
            max_frames=MAX_IMAGE_FRAMES,
        ),
        format_extensions=_IMAGE_FORMAT_EXTENSIONS,
    )


async def _fetch_media_bytes(url: str, context: Context) -> bytes | None:
    """获取受信媒体字节；显式代理与时间线请求使用同一配置。"""

    if not _is_allowed_media_url(url):
        return None
    proxy = _get_proxy(context)
    if proxy is None:
        fetched = await fetch_public_bytes(
            url,
            headers=_get_media_headers(),
            timeout_seconds=REQUEST_TIMEOUT_SECONDS,
            max_bytes=MAX_IMAGE_BYTES,
            allowed_content_type_prefixes=("image/",),
            allowed_hosts=_ALLOWED_TWITTER_MEDIA_HOSTS,
            allowed_schemes=("https",),
        )
        return None if fetched is None else fetched.body

    # URL 在进入此函数前已经限制为 Twitter 的三个 HTTPS 媒体源。代理路径仍使用
    # 有界读取和显式跳转白名单，同时不携带 GraphQL 的认证头或 Cookie。
    response = await aiohttp_request_bounded(
        context.http_session,
        "GET",
        url,
        limits=_MEDIA_BODY_LIMITS,
        mime_policy=_MEDIA_MIME_POLICY,
        redirect_policy=_MEDIA_REDIRECT_POLICY,
        headers=_get_media_headers(),
        request_kwargs={
            "proxy": proxy,
            "timeout": REQUEST_TIMEOUT_SECONDS,
        },
        accept_encoding="identity",
    )
    return response.body


async def _download_image(url: str, save_dir: Path, context: Context) -> bool | None:
    """下载并原子写入图片；新增、已存在、失败分别返回真、假、空。"""

    if not _is_allowed_media_url(url):
        logger.warning("拒绝下载非 Twitter 媒体地址")
        return None
    original_url = _original_media_url(url)

    try:
        content = await _fetch_media_bytes(original_url, context)
        if content is None:
            logger.warning("Twitter 媒体请求未返回成功响应")
            return None
        if not isinstance(content, bytes) or not content:
            raise ValueError("Twitter image response is empty")
        if len(content) > MAX_IMAGE_BYTES:
            raise ValueError("Twitter image exceeds the configured byte limit")

        extension = await asyncio.to_thread(_detect_image_extension, content)
        filename = f"{hashlib.sha256(content).hexdigest()}{extension}"
        cache = BoundedFileCache(save_dir, IMAGE_CACHE_LIMITS)
        filepath, created = await asyncio.to_thread(cache.put_if_absent, filename, content)
        if filepath is None or not created:
            return False
        logger.info("下载 Twitter 图片: %s", filename)
        return True
    except Exception as exc:
        public_error_message(
            context,
            exc,
            logger=logger,
            component="twitter.download_image",
        )
        return None


async def _fetch_twitter_images(context: Context) -> int:
    """首次完整回填允许分页，之后连续两页无新增即结束增量抓取。"""

    async with _FETCH_LOCK:
        save_dir = context.data_dir / "images"
        ensure_dir(save_dir)
        await asyncio.to_thread(BoundedFileCache(save_dir, IMAGE_CACHE_LIMITS).prune)
        user_id = _get_user_id(context)
        backfill_state = context.data_dir / BACKFILL_STATE_FILENAME
        incremental = await asyncio.to_thread(_backfill_is_complete, backfill_state, user_id)
        logger.info("Twitter: 开始%s抓取", "增量" if incremental else "首次全量")

        cursor: str | None = None
        seen_cursors: set[str] = set()
        seen_urls: set[str] = set()
        total_new = 0
        total_attempted = 0
        total_failed = 0
        consecutive_empty = 0

        for page_number in range(1, _get_max_pages(context) + 1):
            logger.info("Twitter: 检查第 %s 页", page_number)
            tweets, next_cursor, has_next = await _fetch_timeline(context, cursor)
            if not tweets:
                break

            page_urls: list[str] = []
            for tweet in tweets:
                for url in _extract_image_urls(tweet):
                    if url not in seen_urls:
                        seen_urls.add(url)
                        page_urls.append(url)

            outcomes = await gather_bounded(
                (_download_image(url, save_dir, context) for url in page_urls),
                limit=MAX_CONCURRENT_IMAGE_DOWNLOADS,
            )
            total_attempted += len(outcomes)
            failed_count = sum(outcome is None for outcome in outcomes)
            total_failed += failed_count
            new_count = sum(outcome is True for outcome in outcomes)
            total_new += new_count
            consecutive_empty = 0 if new_count else consecutive_empty + 1

            if incremental and consecutive_empty >= MAX_PAGES_WITHOUT_NEW_IMAGES:
                logger.info("连续 %s 页没有新图片，停止抓取", consecutive_empty)
                break
            if not has_next or not next_cursor or next_cursor in seen_cursors:
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor

        if total_attempted and total_failed == total_attempted:
            raise TwitterMediaFetchError(total_attempted)
        if total_failed:
            logger.warning(
                "Twitter: %s/%s 个媒体下载失败",
                total_failed,
                total_attempted,
            )
        if not incremental and total_failed == 0:
            await asyncio.to_thread(_write_backfill_state, backfill_state, user_id)
            logger.info("Twitter: 首次全量回填完成")
        logger.info("Twitter: 共下载 %s 张新图片", total_new)
        return total_new


# ──────────────────── 后台抓取任务 ────────────────────


async def _run_background_fetch(context: Context) -> _FetchOutcome:
    """执行一轮抓取并把异常转换成可安全通知的结果。"""

    try:
        count = await _fetch_twitter_images(context)
    except asyncio.CancelledError:
        raise
    except (TwitterFetchError, TwitterMediaFetchError) as exc:
        component = (
            "twitter.fetch_media"
            if isinstance(exc, TwitterMediaFetchError)
            else "twitter.fetch_timeline"
        )
        public_error_message(
            context,
            exc,
            logger=logger,
            component=component,
        )
        return _FetchOutcome(count=0, message=exc.user_message(), succeeded=False)
    except Exception as exc:
        message = public_error_message(
            context,
            exc,
            logger=logger,
            component="twitter.background_fetch",
        )
        return _FetchOutcome(count=0, message=message, succeeded=False)

    logger.info("Twitter 后台抓取完成: 新下载 %s 张图片", count)
    if count == 0:
        save_dir = context.data_dir / "images"
        ensure_dir(save_dir)
        cached_names = await asyncio.to_thread(_list_cached_image_names, save_dir)
        if not cached_names:
            return _FetchOutcome(
                count=0,
                message=(
                    "⚠️ Twitter 抓取已完成，但本地图片缓存仍为空；请检查目标账号是否有可抓取图片"
                ),
                succeeded=False,
            )
    return _FetchOutcome(
        count=count,
        message=f"✅ Twitter 图片抓取完成，新下载 {count} 张图片",
        succeeded=True,
    )


def _clear_fetch_task(task: asyncio.Task[_FetchOutcome]) -> None:
    """仅清理当前任务引用，避免旧任务回调误删后来启动的新任务。"""

    global _FETCH_TASK
    if _FETCH_TASK is task:
        _FETCH_TASK = None
    if task.cancelled():
        return
    try:
        task.result()
    except Exception as exc:  # 防御：正常异常已在 _run_background_fetch 内转换。
        logger.error("Twitter background fetch escaped error_type=%s", type(exc).__name__)


def _get_or_start_fetch(context: Context) -> tuple[asyncio.Task[_FetchOutcome], bool]:
    """复用正在运行的抓取；同一时刻最多存在一轮真实网络抓取。"""

    global _FETCH_TASK
    if _FETCH_TASK is not None and not _FETCH_TASK.done():
        return _FETCH_TASK, False

    task = asyncio.create_task(
        _run_background_fetch(context),
        name="twitter-background-fetch",
    )
    _FETCH_TASK = task
    task.add_done_callback(_clear_fetch_task)
    return task, True


async def _notify_manual_fetch(
    fetch_task: asyncio.Task[_FetchOutcome],
    *,
    context: Context,
    user_id: int | None,
    group_id: int | None,
) -> None:
    """等待后台抓取完成，再向发起命令的会话发送一次结果。"""

    outcome = await fetch_task
    action = build_action(segments(outcome.message), user_id, group_id)
    if action is None:
        return
    try:
        delivered = await context.send_action(action)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        public_error_message(
            context,
            exc,
            logger=logger,
            component="twitter.manual_fetch_delivery",
        )
        return
    if delivered is False:
        logger.warning("Twitter 后台抓取结果未被 OneBot 确认")
    elif delivered is None:
        logger.warning("Twitter 后台抓取结果已提交，但最终投递回执未知")


def _clear_manual_notification(task: asyncio.Task[None]) -> None:
    global _MANUAL_NOTIFICATION_TASK
    if _MANUAL_NOTIFICATION_TASK is task:
        _MANUAL_NOTIFICATION_TASK = None
    if task.cancelled():
        return
    try:
        task.result()
    except Exception as exc:  # 防御：通知函数已处理普通发送异常。
        logger.error("Twitter manual notification escaped error_type=%s", type(exc).__name__)


def _start_manual_notification(
    fetch_task: asyncio.Task[_FetchOutcome],
    *,
    context: Context,
    user_id: int | None,
    group_id: int | None,
) -> None:
    global _MANUAL_NOTIFICATION_TASK
    task = asyncio.create_task(
        _notify_manual_fetch(
            fetch_task,
            context=context,
            user_id=user_id,
            group_id=group_id,
        ),
        name="twitter-manual-fetch-notification",
    )
    _MANUAL_NOTIFICATION_TASK = task
    task.add_done_callback(_clear_manual_notification)


# ──────────────────── 本地缓存与发送回执 ────────────────────


def _list_cached_image_names(save_dir: Path) -> list[str]:
    """修剪缓存后列出可发送的普通图片文件，忽略符号链接和内部锁文件。"""

    BoundedFileCache(save_dir, IMAGE_CACHE_LIMITS).prune()
    names: list[str] = []
    for path in list(save_dir.iterdir()):
        try:
            if (
                not path.name.startswith(".")
                and len(path.name) <= 128
                and _CACHE_FILENAME_PATTERN.fullmatch(path.name) is not None
                and path.suffix.casefold() in _ALLOWED_LOCAL_IMAGE_SUFFIXES
                and not path.is_symlink()
                and path.is_file()
            ):
                names.append(path.name)
        except OSError:
            continue
    return sorted(names)


def _read_posted_names(posted_file: Path, local_names: set[str]) -> set[str]:
    """有限读取发送状态，只保留仍存在于缓存中的安全文件名。"""

    try:
        if posted_file.is_symlink() or not posted_file.is_file():
            return set()
        if posted_file.stat().st_size > MAX_POSTED_STATE_BYTES:
            return set()
        with posted_file.open("rb") as file:
            payload = file.read(MAX_POSTED_STATE_BYTES + 1)
        if len(payload) > MAX_POSTED_STATE_BYTES:
            return set()
        lines = payload.decode("utf-8").splitlines()
    except (OSError, UnicodeError):
        return set()
    return {line.strip() for line in lines if line.strip() in local_names}


def _discard_posted_reservation(key: str, name: str) -> None:
    reserved = _POSTED_RESERVATIONS.get(key)
    if reserved is None:
        return
    reserved.discard(name)
    if not reserved:
        _POSTED_RESERVATIONS.pop(key, None)


async def _commit_posted_image(
    *,
    reservation_key: str,
    selected_name: str,
    save_dir: Path,
    posted_file: Path,
    reset_round: bool,
) -> None:
    """只在传输确认后推进已发送集合，并始终释放内存预留。"""

    async with _POSTED_LOCK:
        try:
            local_images = await asyncio.to_thread(_list_cached_image_names, save_dir)
            local_names = set(local_images)
            if selected_name not in local_names:
                return
            posted = await asyncio.to_thread(_read_posted_names, posted_file, local_names)
            if reset_round:
                posted.clear()
            posted.add(selected_name)
            state = "".join(f"{name}\n" for name in sorted(posted & local_names))
            await asyncio.to_thread(atomic_write_text, posted_file, state)
        finally:
            _discard_posted_reservation(reservation_key, selected_name)


async def _rollback_posted_image(*, reservation_key: str, selected_name: str) -> None:
    """发送失败时只释放预留，让同一图片仍可被下一次命令选择。"""

    async with _POSTED_LOCK:
        _discard_posted_reservation(reservation_key, selected_name)


async def _get_random_image(context: Context) -> DeliverySegments | None:
    """随机预留一张本轮未发送图片，并把状态提交延迟到 delivery ack。"""

    save_dir = context.data_dir / "images"
    posted_file = context.data_dir / "posted.txt"
    reservation_key = str(posted_file.resolve(strict=False))
    ensure_dir(save_dir)

    async with _POSTED_LOCK:
        local_images = await asyncio.to_thread(_list_cached_image_names, save_dir)
        if not local_images:
            return None

        local_names = set(local_images)
        posted = await asyncio.to_thread(_read_posted_names, posted_file, local_names)
        reserved = _POSTED_RESERVATIONS.setdefault(reservation_key, set())
        reserved.intersection_update(local_names)
        available = [name for name in local_images if name not in posted and name not in reserved]
        reset_round = False
        if not available:
            if reserved:
                return None
            logger.info("所有 Twitter 图片都已发送过，重置发送轮次")
            available = local_images.copy()
            reset_round = True

        cache = BoundedFileCache(save_dir, IMAGE_CACHE_LIMITS)
        selected_path: Path | None = None
        selected_name: str | None = None
        while available:
            selected_name = random.choice(available)
            selected_path = await asyncio.to_thread(cache.get_any, (selected_name,))
            if selected_path is not None:
                try:
                    await asyncio.to_thread(_validate_cached_image, selected_path)
                    break
                except (ImageValidationError, OSError):
                    try:
                        await asyncio.to_thread(selected_path.unlink, missing_ok=True)
                    except OSError:
                        logger.debug("损坏的 Twitter 图片缓存暂时无法删除")
                    selected_path = None
            available.remove(selected_name)
        if selected_path is None or selected_name is None:
            if not reserved:
                _POSTED_RESERVATIONS.pop(reservation_key, None)
            return None

        reserved.add(selected_name)

        async def commit_selection() -> None:
            await _commit_posted_image(
                reservation_key=reservation_key,
                selected_name=selected_name,
                save_dir=save_dir,
                posted_file=posted_file,
                reset_round=reset_round,
            )

        receipt = DeliveryReceipt(
            expected_actions=1,
            commit=commit_selection,
            rollback=lambda: _rollback_posted_image(
                reservation_key=reservation_key,
                selected_name=selected_name,
            ),
            unknown=commit_selection,
        )
        return DeliverySegments([image(str(selected_path))], receipt)


# ──────────────────── 命令与调度入口 ────────────────────


async def handle(
    command: str,
    args: str,
    event: OneBotEvent,
    context: Context,
) -> MessageSegments:
    """处理清单规范化后的 ``twimg`` 与 ``tw_fetch`` 命令。"""

    del event  # 当前命令只使用调度器写入 context 的会话目标。
    try:
        parsed = parse(args)
        asks_for_help = bool(parsed and parsed.first.casefold() in _HELP_ALIASES)
        if parsed and (len(parsed) != 1 or parsed.options or not asks_for_help):
            usage = "/tw_fetch [help]" if command == "tw_fetch" else "/twimg [help]"
            return segments(f"❌ 用法: {usage}")

        if command == "tw_fetch":
            if asks_for_help:
                return segments(_TW_FETCH_HELP)
            user_id = context.current_user_id
            group_id = context.current_group_id
            if user_id is None and group_id is None:
                return segments("❌ 无法确定抓取结果的通知目标")
            if _MANUAL_NOTIFICATION_TASK is not None and not _MANUAL_NOTIFICATION_TASK.done():
                return segments("⏳ Twitter 图片正在后台抓取，请稍后用 /twimg 查看")

            fetch_task, started = _get_or_start_fetch(context)
            _start_manual_notification(
                fetch_task,
                context=context,
                user_id=user_id,
                group_id=group_id,
            )
            if started:
                return segments("🔄 已开始后台抓取 Twitter 图片，完成后会通知你")
            return segments("🔄 已加入正在进行的 Twitter 抓取，完成后会通知你")

        if command == "twimg":
            if asks_for_help:
                return segments(_TWIMG_HELP)
            image_result = await _get_random_image(context)
            if image_result is not None:
                return image_result
            return segments(
                "📭 Twitter 本地图片缓存为空，请由管理员执行 /tw_fetch，等待抓取完成后再试"
            )

        return segments(f"❓ 未知 Twitter 命令: {command}")
    except Exception as exc:
        return public_error_response(
            context,
            exc,
            logger=logger,
            component="twitter.handle",
        )


async def scheduled_fetch(context: Context) -> MessageSegments:
    """启动每日后台抓取；定时回调立即返回，也不主动向群聊发消息。"""

    _task, started = _get_or_start_fetch(context)
    if started:
        logger.info("Twitter 定时后台抓取已启动")
    else:
        logger.info("Twitter 已有抓取任务运行，跳过重复定时启动")
    return []
