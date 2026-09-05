"""Flickr 公共照片发现、搜索、用户与相册浏览命令。"""

from __future__ import annotations

import asyncio
import calendar
import hashlib
import logging
import re
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from core.args import FLAG_VALUE, ParsedArgs, parse, parse_int
from core.bounded_file_cache import BoundedFileCache, FileCacheLimits
from core.image_validation import ImageValidationLimits, validate_image_bytes
from core.plugin_base import (
    PluginContextProtocol,
    Segments,
    bounded_external_text,
    image,
    run_sync,
    text,
)
from core.public_errors import public_error_response
from core.safe_http import SafeHttpError, UnsafeUrlError, fetch_public_bytes

from .client import (
    FlickrApiError,
    FlickrClient,
    FlickrConfigurationError,
    FlickrError,
    FlickrPage,
    FlickrPhoto,
)

logger = logging.getLogger(__name__)

SESSION_TTL_SECONDS = 15 * 60
MAX_SESSIONS        = 512
MAX_MORE_COUNT      = 5
MAX_IMAGE_BYTES     = 12 * 1024 * 1024
_STATIC_IMAGE_HOSTS = frozenset({"live.staticflickr.com"})
_FLICKR_PAGE_HOSTS  = frozenset({"flickr.com", "www.flickr.com", "m.flickr.com"})
_IMAGE_EXTENSIONS   = (".jpg", ".png", ".webp", ".gif")
_IMAGE_LIMITS       = ImageValidationLimits(
    max_bytes     = MAX_IMAGE_BYTES,
    max_pixels    = 40_000_000,
    max_dimension = 12_000,
    max_frames    = 1,
)
_IMAGE_CACHE_LIMITS = FileCacheLimits(
    max_entries = 256,
    max_bytes   = 256 * 1024 * 1024,
    ttl_seconds = 60 * 60,
)

_SORTS = {
    "relevance": "relevance",
    "interesting": "interestingness-desc",
    "new": "date-posted-desc",
    "old": "date-posted-asc",
}
_LICENSE_FILTERS: dict[str, str | None] = {
    "any": None,
    "cc": "1,2,3,4,5,6,9,10",
    "public-domain": "7,8,9,10",
}
_LICENSES: dict[str, tuple[str, str]] = {
    "0": ("All Rights Reserved", ""),
    "1": ("CC BY-NC-SA 2.0", "https://creativecommons.org/licenses/by-nc-sa/2.0/"),
    "2": ("CC BY-NC 2.0", "https://creativecommons.org/licenses/by-nc/2.0/"),
    "3": ("CC BY-NC-ND 2.0", "https://creativecommons.org/licenses/by-nc-nd/2.0/"),
    "4": ("CC BY 2.0", "https://creativecommons.org/licenses/by/2.0/"),
    "5": ("CC BY-SA 2.0", "https://creativecommons.org/licenses/by-sa/2.0/"),
    "6": ("CC BY-ND 2.0", "https://creativecommons.org/licenses/by-nd/2.0/"),
    "7": ("No known copyright restrictions", "https://www.flickr.com/commons/usage/"),
    "8": ("United States Government Work", "https://www.usa.gov/government-copyright"),
    "9": ("CC0 1.0", "https://creativecommons.org/publicdomain/zero/1.0/"),
    "10": ("Public Domain Mark 1.0", "https://creativecommons.org/publicdomain/mark/1.0/"),
}

_COMMAND_ALIASES = {
    "搜索": "search",
    "典藏": "commons",
    "公共典藏": "commons",
    "作者": "user",
    "用户": "user",
    "相册": "album",
    "更多": "more",
    "下一张": "more",
    "详情": "info",
    "信息": "info",
    "帮助": "help",
    "?": "help",
}

HELP_TEXT = """📷 Flickr 公共摄影

/flickr
  浏览今日精选
/flickr search <关键词> [--tags a,b] [--sort relevance|interesting|new|old]
  [--license any|cc|public-domain] [--date YYYY-MM|YYYY-MM-DD]
/flickr commons <关键词>
  搜索 Flickr Commons
/flickr user <用户名或个人页 URL>
  浏览用户公开照片
/flickr album <相册 URL>
/flickr album <用户> <相册ID>
  浏览公开相册
/flickr more [1-5]
  继续当前结果
/flickr info [照片ID或 URL]
  查看当前照片或指定照片详情

默认 license=any，覆盖全部公开许可类型；回复保留作者、许可和 Flickr 原图页。
结果会话保留 15 分钟。"""


class FlickrUsageError(ValueError):
    """命令参数不满足用户可见契约。"""


@dataclass(slots=True)
class _BrowseSession:
    photos: tuple[FlickrPhoto, ...]
    next_index: int
    last_index: int
    summary: str
    expires_at: float


@dataclass(frozen=True, slots=True)
class _SearchRequest:
    query: str
    tags: str
    sort: str
    license_ids: str | None
    min_taken_date: str | None
    max_taken_date: str | None


def _runtime(context: PluginContextProtocol) -> dict[str, Any]:
    state = getattr(context, "state", None)
    if not isinstance(state, dict):
        state         = {}
        context.state = state
    runtime = state.setdefault("flickr_runtime", {})
    if not isinstance(runtime, dict):
        runtime                 = {}
        state["flickr_runtime"] = runtime
    runtime.setdefault("sessions", {})
    runtime.setdefault("locks", {})
    return runtime


def _event_id(event: dict[str, Any], name: str) -> int | None:
    value = event.get(name)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _session_key(
    event: dict[str, Any],
    context: PluginContextProtocol,
) -> tuple[str, int, int] | tuple[str, int]:
    user_id  = getattr(context, "current_user_id", None) or _event_id(event, "user_id")
    group_id = getattr(context, "current_group_id", None) or _event_id(event, "group_id")
    if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
        raise FlickrUsageError("无法识别当前用户，不能保存 Flickr 翻页状态")
    if isinstance(group_id, int) and not isinstance(group_id, bool) and group_id > 0:
        return ("group", group_id, user_id)
    return ("private", user_id)


def _prune_runtime(runtime: dict[str, Any], *, now: float) -> None:
    sessions = runtime["sessions"]
    locks    = runtime["locks"]
    if not isinstance(sessions, dict) or not isinstance(locks, dict):
        runtime["sessions"] = {}
        runtime["locks"]    = {}
        return

    # 没有会话的空闲锁也需要回收；等待者已排队时保留同一个锁对象。
    def in_use(lock: object) -> bool:
        return isinstance(lock, asyncio.Lock) and (
            lock.locked() or bool(getattr(lock, "_waiters", None))
        )

    for key, lock in tuple(locks.items()):
        if key not in sessions and not in_use(lock):
            locks.pop(key, None)
    for key, session in tuple(sessions.items()):
        if not isinstance(session, _BrowseSession) or session.expires_at <= now:
            lock = locks.get(key)
            if in_use(lock):
                continue
            sessions.pop(key, None)
            locks.pop(key, None)
    if len(sessions) <= MAX_SESSIONS:
        return
    excess = len(sessions) - MAX_SESSIONS
    oldest = sorted(sessions.items(), key=lambda item: item[1].expires_at)
    for key, _session in oldest:
        if excess <= 0:
            break
        if in_use(locks.get(key)):
            continue
        sessions.pop(key, None)
        locks.pop(key, None)
        excess -= 1


def _session_lock(runtime: dict[str, Any], key: object) -> asyncio.Lock:
    locks = runtime["locks"]
    lock  = locks.get(key)
    if not isinstance(lock, asyncio.Lock):
        lock       = asyncio.Lock()
        locks[key] = lock
    return lock


def _normalize_action(value: str) -> str:
    stripped = value.strip()
    return _COMMAND_ALIASES.get(stripped, stripped.casefold())


def _require_option_values(parsed: ParsedArgs, names: set[str]) -> None:
    unknown = set(parsed.options) - names
    if unknown:
        first = sorted(unknown)[0]
        raise FlickrUsageError(f"未知选项 --{first}")
    for name in names & set(parsed.options):
        if parsed.opt(name) == FLAG_VALUE:
            raise FlickrUsageError(f"--{name} 需要一个值")


def _taken_date_range(value: str) -> tuple[str, str]:
    try:
        if re.fullmatch(r"\d{4}-\d{2}", value):
            year, month = (int(part) for part in value.split("-"))
            last_day = calendar.monthrange(year, month)[1]
            return f"{year:04d}-{month:02d}-01 00:00:00", (
                f"{year:04d}-{month:02d}-{last_day:02d} 23:59:59"
            )
        day = date.fromisoformat(value)
    except (ValueError, OverflowError) as exc:
        raise FlickrUsageError("--date 需使用有效的 YYYY-MM 或 YYYY-MM-DD") from exc
    rendered = day.isoformat()
    return f"{rendered} 00:00:00", f"{rendered} 23:59:59"


def _parse_search(parsed: ParsedArgs, *, token_start: int) -> _SearchRequest:
    _require_option_values(parsed, {"tags", "sort", "license", "date"})
    query = bounded_external_text(
        " ".join(parsed.tokens[token_start:]),
        max_chars = 300,
        max_bytes = 1_200,
        truncate  = False,
    )
    raw_tags = parsed.opt("tags")
    tags     = ",".join(
        tag
        for item in raw_tags.split(",")
        if (tag := bounded_external_text(item, max_chars=48, max_bytes=192, truncate=False))
    )
    if not query and not tags:
        raise FlickrUsageError("请提供搜索关键词或 --tags")
    sort_name = parsed.opt("sort", "relevance").casefold()
    if sort_name not in _SORTS:
        raise FlickrUsageError("--sort 可选 relevance、interesting、new、old")
    license_name = parsed.opt("license", "any").casefold()
    if license_name not in _LICENSE_FILTERS:
        raise FlickrUsageError("--license 可选 any、cc、public-domain")
    min_date = max_date = None
    if parsed.has("date"):
        min_date, max_date = _taken_date_range(parsed.opt("date"))
    return _SearchRequest(
        query          = query,
        tags           = tags,
        sort           = _SORTS[sort_name],
        license_ids    = _LICENSE_FILTERS[license_name],
        min_taken_date = min_date,
        max_taken_date = max_date,
    )


def _opaque_id(value: str, *, label: str) -> str:
    candidate = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9@_-]{1,128}", candidate):
        raise FlickrUsageError(f"{label} 格式无效")
    return candidate


def _parse_album_reference(tokens: list[str]) -> tuple[str, str]:
    if len(tokens) == 2:
        return tokens[0], _opaque_id(tokens[1], label="相册 ID")
    if len(tokens) != 1:
        raise FlickrUsageError("用法：/flickr album <相册 URL> 或 <用户> <相册ID>")
    value = tokens[0]
    try:
        parsed = urlsplit(value)
        port   = parsed.port
    except ValueError as exc:
        raise FlickrUsageError("相册 URL 格式无效") from exc
    host = (parsed.hostname or "").rstrip(".").casefold()
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or host not in _FLICKR_PAGE_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 80, 443}
    ):
        raise FlickrUsageError("请提供 Flickr 相册 URL")
    parts = [part for part in parsed.path.split("/") if part]
    if (
        len(parts) < 4
        or parts[0].casefold() != "photos"
        or parts[2].casefold() not in {"albums", "sets"}
    ):
        raise FlickrUsageError("请提供形如 flickr.com/photos/用户/albums/相册ID 的 URL")
    return parts[1], _opaque_id(parts[3], label="相册 ID")


_FLICKR_BASE58 = "123456789abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ"


def _decode_short_photo_id(value: str) -> str:
    result = 0
    for character in value:
        try:
            digit = _FLICKR_BASE58.index(character)
        except ValueError as exc:
            raise FlickrUsageError("Flickr 短链接格式无效") from exc
        result = result * 58 + digit
        if result > 10**30:
            raise FlickrUsageError("Flickr 短链接超出支持范围")
    if result <= 0:
        raise FlickrUsageError("Flickr 短链接格式无效")
    return str(result)


def _parse_photo_reference(value: str) -> str:
    candidate = value.strip()
    if "://" not in candidate:
        return _opaque_id(candidate, label="照片 ID")
    try:
        parsed = urlsplit(candidate)
        port   = parsed.port
    except ValueError as exc:
        raise FlickrUsageError("照片 URL 格式无效") from exc
    host = (parsed.hostname or "").rstrip(".").casefold()
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 80, 443}
    ):
        raise FlickrUsageError("照片 URL 格式无效")
    parts = [part for part in parsed.path.split("/") if part]
    if host == "flic.kr" and len(parts) == 2 and parts[0] == "p":
        return _decode_short_photo_id(parts[1])
    if host in _FLICKR_PAGE_HOSTS and len(parts) >= 3 and parts[0].casefold() == "photos":
        return _opaque_id(parts[2], label="照片 ID")
    raise FlickrUsageError("请提供 Flickr 照片 ID 或照片页 URL")


def _license_line(photo: FlickrPhoto) -> tuple[str, str]:
    name, url = _LICENSES.get(photo.license_id, (f"Flickr License #{photo.license_id}", ""))
    return name, url


def _photo_caption(
    photo: FlickrPhoto,
    *,
    index: int | None,
    total: int | None,
    detailed: bool,
) -> str:
    title_value = bounded_external_text(
        photo.title,
        max_chars = 180,
        max_bytes = 720,
        default   = "无标题",
    )
    owner = bounded_external_text(
        photo.owner_name,
        max_chars = 100,
        max_bytes = 400,
        default   = photo.owner_id,
    )
    license_name, license_url = _license_line(photo)
    lines = [f"📷 {title_value}", f"👤 {owner}", f"📜 {license_name}"]
    if license_url:
        lines.append(license_url)
    if photo.taken_at:
        lines.append(f"🗓️ {photo.taken_at}")
    if photo.tags:
        tags = " · ".join(f"#{tag}" for tag in photo.tags[:8])
        lines.append(f"🏷️ {tags}")
    if detailed and photo.description:
        description = bounded_external_text(
            photo.description,
            max_chars = 700,
            max_bytes = 2_800,
        )
        lines.extend(("", description))
    lines.extend(("", f"🔗 {photo.page_url}"))
    if index is not None and total is not None:
        lines.append(f"第 {index + 1}/{total} 张 · /flickr more [1-5]")
    return "\n".join(lines)


def _image_cache(context: PluginContextProtocol) -> BoundedFileCache:
    return BoundedFileCache(Path(context.data_dir) / "images", _IMAGE_CACHE_LIMITS)


async def _download_photo(photo: FlickrPhoto, context: PluginContextProtocol) -> Path:
    digest = hashlib.sha256(photo.media_url.encode("utf-8")).hexdigest()
    cache  = _image_cache(context)
    cached = await run_sync(
        cache.get_any, tuple(f"{digest}{suffix}" for suffix in _IMAGE_EXTENSIONS)
    )
    if cached is not None:
        return cached
    response = await fetch_public_bytes(
        photo.media_url,
        timeout_seconds                  = 15.0,
        max_bytes                        = MAX_IMAGE_BYTES,
        allowed_content_types            = {"image/jpeg", "image/png", "image/webp", "image/gif"},
        allowed_content_type_prefixes    = (),
        allowed_hosts                    = _STATIC_IMAGE_HOSTS,
        allowed_schemes                  = ("https",),
        allow_transparent_proxy_fake_dns = True,
    )
    if response is None:
        raise SafeHttpError("Flickr image download returned no response")
    validated = await run_sync(
        validate_image_bytes,
        response.body,
        limits          = _IMAGE_LIMITS,
        allow_animation = False,
    )
    stored = await run_sync(cache.put, f"{digest}{validated.extension}", response.body)
    if stored is None:
        raise OSError("Flickr image cache rejected the payload")
    return stored


async def _render_photo(
    photo: FlickrPhoto,
    context: PluginContextProtocol,
    *,
    index: int | None,
    total: int | None,
    detailed: bool = False,
) -> Segments:
    caption = _photo_caption(photo, index=index, total=total, detailed=detailed)
    try:
        path = await _download_photo(photo, context)
    except (SafeHttpError, UnsafeUrlError, OSError, TimeoutError, ValueError) as exc:
        context.logger.warning("Flickr image fallback: %s", type(exc).__name__)
        return [text(f"{caption}\n⚠️ 图片下载暂时失败，可打开原图页查看")]
    return [image(str(path)), text(caption)]


async def _open_page(
    page: FlickrPage,
    *,
    summary: str,
    event: dict[str, Any],
    context: PluginContextProtocol,
) -> Segments:
    if not page.photos:
        return [text("🔍 没有找到可发送的 Flickr 公共照片")]
    runtime = _runtime(context)
    now     = time.monotonic()
    _prune_runtime(runtime, now=now)
    key  = _session_key(event, context)
    lock = _session_lock(runtime, key)
    async with lock:
        runtime["sessions"][key] = _BrowseSession(
            photos     = page.photos,
            next_index = 1,
            last_index = 0,
            summary    = bounded_external_text(
                summary,
                max_chars = 100,
                max_bytes = 400,
                default   = "当前 Flickr 结果",
            ),
            expires_at=now + SESSION_TTL_SECONDS,
        )
        return await _render_photo(
            page.photos[0],
            context,
            index = 0,
            total = len(page.photos),
        )


async def _more(
    count: int,
    *,
    event: dict[str, Any],
    context: PluginContextProtocol,
) -> Segments:
    runtime = _runtime(context)
    now     = time.monotonic()
    _prune_runtime(runtime, now=now)
    key  = _session_key(event, context)
    lock = _session_lock(runtime, key)
    async with lock:
        session = runtime["sessions"].get(key)
        if not isinstance(session, _BrowseSession) or session.expires_at <= now:
            runtime["sessions"].pop(key, None)
            return [text("⏳ 当前没有可继续的 Flickr 结果，请先搜索或浏览精选")]
        if session.next_index >= len(session.photos):
            return [text(f"✅ {session.summary}已经浏览完")]
        end             = min(session.next_index + count, len(session.photos))
        reply: Segments = []
        for index in range(session.next_index, end):
            reply.extend(
                await _render_photo(
                    session.photos[index],
                    context,
                    index = index,
                    total = len(session.photos),
                )
            )
        session.last_index = end - 1
        session.next_index = end
        session.expires_at = time.monotonic() + SESSION_TTL_SECONDS
        return reply


async def _last_photo(
    *,
    event: dict[str, Any],
    context: PluginContextProtocol,
) -> FlickrPhoto | None:
    runtime = _runtime(context)
    now     = time.monotonic()
    _prune_runtime(runtime, now=now)
    key  = _session_key(event, context)
    lock = _session_lock(runtime, key)
    async with lock:
        session = runtime["sessions"].get(key)
        if not isinstance(session, _BrowseSession) or session.expires_at <= now:
            return None
        return session.photos[session.last_index]


async def _dispatch(
    parsed: ParsedArgs,
    event: dict[str, Any],
    context: PluginContextProtocol,
) -> Segments:
    action = _normalize_action(parsed.first) if parsed.first else "interesting"
    if action == "help":
        if len(parsed.tokens) != 1 or parsed.options:
            raise FlickrUsageError("用法：/flickr help")
        return [text(HELP_TEXT)]

    if action == "interesting":
        if parsed.tokens or parsed.options:
            raise FlickrUsageError("用法：/flickr")
        client = FlickrClient(context)
        page   = await client.interesting()
        return await _open_page(page, summary="今日精选", event=event, context=context)

    if action in {"search", "commons"}:
        request = _parse_search(parsed, token_start=1)
        client = FlickrClient(context)
        page   = await client.search(
            query          = request.query,
            tags           = request.tags,
            sort           = request.sort,
            license_ids    = request.license_ids,
            min_taken_date = request.min_taken_date,
            max_taken_date = request.max_taken_date,
            commons_only   = action == "commons",
        )
        summary = "Flickr Commons" if action == "commons" else "Flickr 搜索"
        return await _open_page(page, summary=summary, event=event, context=context)

    if action == "user":
        _require_option_values(parsed, set())
        reference = " ".join(parsed.tokens[1:]).strip()
        if not reference:
            raise FlickrUsageError("用法：/flickr user <用户名或个人页 URL>")
        client  = FlickrClient(context)
        user_id = await client.resolve_user(reference)
        page    = await client.public_photos(user_id)
        return await _open_page(page, summary=f"用户 {reference}", event=event, context=context)

    if action == "album":
        _require_option_values(parsed, set())
        owner_reference, album_id = _parse_album_reference(parsed.tokens[1:])
        client  = FlickrClient(context)
        user_id = await client.resolve_user(owner_reference)
        page = await client.album_photos(user_id=user_id, album_id=album_id)
        return await _open_page(page, summary=f"相册 {album_id}", event=event, context=context)

    if action == "more":
        _require_option_values(parsed, set())
        if len(parsed.tokens) > 2:
            raise FlickrUsageError("用法：/flickr more [1-5]")
        count = (
            1
            if len(parsed.tokens) == 1
            else parse_int(parsed.tokens[1], minimum=1, maximum=MAX_MORE_COUNT)
        )
        if count is None:
            raise FlickrUsageError("more 数量需为 1 到 5 的整数")
        return await _more(count, event=event, context=context)

    if action == "info":
        _require_option_values(parsed, set())
        if len(parsed.tokens) > 2:
            raise FlickrUsageError("用法：/flickr info [照片ID或 URL]")
        photo: FlickrPhoto | None
        if len(parsed.tokens) == 2:
            client = FlickrClient(context)
            photo  = await client.photo_info(_parse_photo_reference(parsed.tokens[1]))
        else:
            photo = await _last_photo(event=event, context=context)
        if photo is None:
            return [text("⏳ 当前没有 Flickr 照片，请先搜索或提供照片 ID")]
        return await _render_photo(photo, context, index=None, total=None, detailed=True)

    raise FlickrUsageError("未知 Flickr 命令，请使用 /flickr help")


async def handle(
    _command: str,
    args: str,
    event: dict[str, Any],
    context: PluginContextProtocol,
) -> Segments:
    """处理 Flickr 公共只读命令。"""

    try:
        return await _dispatch(parse(args), event, context)
    except FlickrUsageError as exc:
        return [text(f"❌ {exc}")]
    except FlickrConfigurationError:
        return [
            text(
                "⚙️ Flickr API Key 未配置。请在 config/secrets.json 的 "
                "plugins.flickr.api_key 中填写新 Key"
            )
        ]
    except FlickrApiError as exc:
        if exc.code in {"1", "2"}:
            return [text("🔍 Flickr 未找到对应的公开内容")]
        if exc.code == "100":
            return [text("⚙️ Flickr API Key 无效或已失效，请更新配置")]
        return [text("❌ Flickr 暂时不可用，请稍后再试")]
    except FlickrError:
        return [text("❌ Flickr 暂时不可用，请稍后再试")]
    except Exception as exc:  # noqa: BLE001 - 公共入口必须转成带 request_id 的稳定错误
        return public_error_response(
            context,
            exc,
            logger    = context.logger,
            component = "flickr.handle",
        )


async def shutdown(context: PluginContextProtocol) -> None:
    """释放当前插件 generation 的内存浏览状态。"""

    state = getattr(context, "state", None)
    if isinstance(state, dict):
        state.pop("flickr_runtime", None)


__all__ = ["handle", "shutdown"]
