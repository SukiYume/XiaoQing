"""A 岛匿名版的有界异步 API 客户端与图片缓存。"""

import asyncio
import hashlib
import logging
import re
import time
import uuid as uuidlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import aiohttp

from core.bounded_file_cache import BoundedFileCache, FileCacheLimits
from core.bounded_http import (
    JSON_MIME_POLICY,
    BodyLimits,
    JsonLimits,
    aiohttp_request_bounded,
    parse_bounded_json,
)
from core.image_validation import (
    WEB_IMAGE_FORMAT_EXTENSIONS,
    WEB_IMAGE_MIME_FORMATS,
    ImageValidationError,
    ImageValidationLimits,
    validate_image_bytes,
    validate_image_path,
)
from core.plugin_base import bounded_external_text
from core.safe_http import SafeHttpError, fetch_public_bytes

logger = logging.getLogger(__name__)

API_HOST  = "https://www.nmbxd1.com"
IMAGE_CDN = "https://image.nmb.best"
APP_ID    = "A-Island-IOS-App"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)
MAX_IMAGE_BYTES = 8 * 1024 * 1024
IMAGE_LIMITS    = ImageValidationLimits(
    max_bytes=MAX_IMAGE_BYTES, max_pixels=20_000_000, max_frames=120
)
IMAGE_TIMEOUT_SECONDS     = 15
MAX_EXTERNAL_RESULT_CHARS = 512
FORUM_CACHE_TTL_SECONDS   = 60 * 60
MAX_FORUM_CACHE_ENTRIES   = 1_000
IMAGE_CACHE_LIMITS        = FileCacheLimits(
    max_entries = 256,
    max_bytes   = 256 * 1024 * 1024,
    ttl_seconds = 7 * 24 * 60 * 60,
)
_ADNMB_BODY_LIMITS = BodyLimits(
    max_wire_bytes    = 4 * 1024 * 1024,
    max_decoded_bytes = 8 * 1024 * 1024,
)
_ADNMB_JSON_LIMITS = JsonLimits(
    max_bytes        = _ADNMB_BODY_LIMITS.max_decoded_bytes,
    max_depth        = 32,
    max_nodes        = 100_000,
    max_string_chars = 6 * 1024 * 1024,
)

# API 端点
ENDPOINTS = {
    "forum_list": "/Api/getForumList",
    "timeline"  : "/Api/timeline",
    "forum"     : "/Api/showf",
    "thread"    : "/Api/thread",
    "ref"       : "/Api/ref",
    "feed"      : "/Api/feed",
    "add_feed"  : "/Api/addFeed",
    "del_feed"  : "/Api/delFeed",
}


@dataclass(frozen=True, slots=True)
class Post:
    """帖子/回复数据结构"""

    id     : str
    time   : str
    user_id: str
    content: str
    img    : str = ""

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Post":
        """从 API JSON 响应构建 Post 对象"""
        content = str(data.get("content", "") or "")[:65_536]
        # 标签内部排除再次出现的起始符，使畸形正文的扫描保持线性。
        content = re.sub(r"<[^<>]+>", "", content)
        content = content.replace("&gt;", ">").replace("&bull;", "")

        img = ""
        if data.get("img") and data.get("ext"):
            img = f"{data['img']}{data['ext']}"

        return cls(
            id      = str(data.get("id", "")),
            time    = str(data.get("now", data.get("time", "")) or ""),
            user_id = str(data.get("user_hash", data.get("userid", "")) or ""),
            content = content,
            img     = img,
        )

    def format_text(self) -> str:
        """格式化为可读文本"""
        return f"{self.id} - {self.user_id}\n{self.time}\n{self.content}"


@dataclass(frozen=True, slots=True)
class Thread:
    """串数据结构（包含主帖和回复）"""

    main_post: Post
    replies  : list[Post]

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Thread":
        """从 API JSON 响应构建 Thread 对象"""
        main_post           = Post.from_json(data)
        replies: list[Post] = []
        raw_replies         = data.get("Replies", [])
        if not isinstance(raw_replies, list):
            raw_replies = []
        for reply_data in raw_replies:
            if not isinstance(reply_data, dict):
                continue
            # 跳过 Admin 回复和特殊 ID (9999999)
            if reply_data.get("user_hash") == "Admin" or str(reply_data.get("id")) == "9999999":
                continue
            replies.append(Post.from_json(reply_data))

        return cls(main_post=main_post, replies=replies)


class AdnmbClient:
    """A岛 API 异步客户端"""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        cache_dir: Path,
        uuid: str = "",
    ) -> None:
        self.session = session
        self.cache_dir = cache_dir
        self.uuid = uuid or str(uuidlib.uuid5(uuidlib.NAMESPACE_URL, str(cache_dir.resolve())))
        self._forum_cache: dict[str, str] | None = None
        self._forum_cache_expires_at = 0.0
        self._image_cache = BoundedFileCache(cache_dir, IMAGE_CACHE_LIMITS)
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        """Clear wrapper-owned caches. The application owns ``session``."""
        self._forum_cache            = None
        self._forum_cache_expires_at = 0.0
        self._closed                 = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("ADnmb client is closed")

    async def _get(self, endpoint: str, **params: Any) -> Any:
        """发送 GET 请求"""
        self._ensure_open()
        url            = f"{API_HOST}{ENDPOINTS.get(endpoint, endpoint)}"
        request_params = {"appid": APP_ID, "__t": int(time.time() * 1000), **params}
        response       = await aiohttp_request_bounded(
            self.session,
            "GET",
            url,
            limits         = _ADNMB_BODY_LIMITS,
            mime_policy    = JSON_MIME_POLICY,
            request_kwargs = {
                "params" : request_params,
                "timeout": REQUEST_TIMEOUT,
            },
        )
        return parse_bounded_json(response, limits=_ADNMB_JSON_LIMITS)

    async def get_forum_list(self) -> dict[str, str]:
        """获取板块列表（板块名 -> ID），结果缓存一小时。"""

        now = time.monotonic()
        if self._forum_cache is not None and now < self._forum_cache_expires_at:
            return dict(self._forum_cache)

        data = await self._get("forum_list")
        if not isinstance(data, list):
            return {}
        forums = (
            forum
            for group in data
            if isinstance(group, dict) and isinstance(group.get("forums"), list)
            for forum in group["forums"]
            if isinstance(forum, dict)
        )
        forum_list: dict[str, str] = {}
        for forum in forums:
            name, forum_id = forum.get("name"), forum.get("id")
            if isinstance(name, str) and name and forum_id is not None:
                forum_list[name] = str(forum_id)
                if len(forum_list) >= MAX_FORUM_CACHE_ENTRIES:
                    break

        self._forum_cache            = forum_list
        self._forum_cache_expires_at = now + FORUM_CACHE_TTL_SECONDS
        return dict(forum_list)

    @staticmethod
    def _threads(data: Any) -> list[Thread]:
        """解析串列表，跳过畸形条目和管理员串。"""

        if not isinstance(data, list):
            return []
        return [
            Thread.from_json(item)
            for item in data
            if isinstance(item, dict) and item.get("user_hash") != "Admin"
        ]

    async def get_timeline(self, page: int = 1) -> list[Thread]:
        return self._threads(await self._get("timeline", id="-1", page=page))

    async def get_forum(self, forum_name: str, page: int = 1) -> list[Thread]:
        forum_id = (await self.get_forum_list()).get(forum_name)
        if not forum_id:
            return []
        return self._threads(await self._get("forum", id=forum_id, page=page))

    async def get_thread(self, thread_id: str, page: int = 1) -> Thread | None:
        data = await self._get("thread", id=thread_id, page=page)
        return Thread.from_json(data) if isinstance(data, dict) else None

    async def get_ref(self, ref_id: str) -> Post | None:
        data = await self._get("ref", id=ref_id, page=1)
        return Post.from_json(data) if isinstance(data, dict) and "id" in data else None

    async def get_feed(self, page: int = 1) -> list[Post]:
        data = await self._get("feed", page=page, uuid=self.uuid)
        if not isinstance(data, list):
            return []
        return [Post.from_json(item) for item in data if isinstance(item, dict)]

    async def _change_feed(self, endpoint: str, thread_id: str, default: str) -> str:
        """订阅增删接口返回一段提示文本，按外部文本边界截断。"""

        result = await self._get(endpoint, tid=thread_id, uuid=self.uuid)
        return bounded_external_text(
            result,
            max_chars = MAX_EXTERNAL_RESULT_CHARS,
            max_bytes = MAX_EXTERNAL_RESULT_CHARS * 4,
            default   = default,
        )

    async def add_feed(self, thread_id: str) -> str:
        return await self._change_feed("add_feed", thread_id, "添加订阅失败")

    async def del_feed(self, thread_id: str) -> str:
        return await self._change_feed("del_feed", thread_id, "删除订阅失败")

    async def download_image(self, img_path: str, use_thumb: bool = False) -> Path | None:
        """下载图片到本地缓存"""
        self._ensure_open()
        if not img_path:
            return None

        url = f"{IMAGE_CDN}/{'thumb' if use_thumb else 'image'}/{img_path}"

        # 缓存文件名取 URL 摘要；先查缓存并复验，失效则删除重新下载
        digest      = hashlib.sha256(url.encode("utf-8")).hexdigest()
        cached_path = await asyncio.to_thread(
            self._image_cache.get_any,
            tuple(f"{digest}{extension}" for extension in WEB_IMAGE_FORMAT_EXTENSIONS.values()),
        )
        if cached_path is not None:
            try:
                await asyncio.to_thread(
                    validate_image_path,
                    cached_path,
                    limits            = IMAGE_LIMITS,
                    format_extensions = WEB_IMAGE_FORMAT_EXTENSIONS,
                )
                return cached_path
            except (ImageValidationError, OSError):
                try:
                    await asyncio.to_thread(cached_path.unlink, missing_ok=True)
                except OSError:
                    logger.debug("Invalid ADNMB image cache could not be removed")

        try:
            fetched = await fetch_public_bytes(
                url,
                timeout_seconds               = IMAGE_TIMEOUT_SECONDS,
                max_bytes                     = MAX_IMAGE_BYTES,
                allowed_content_type_prefixes = ("image/",),
                allowed_hosts                 = {"image.nmb.best"},
                allowed_schemes               = {"https"},
            )
            if fetched is None:
                logger.warning("Image download failed: %s", url)
                return None

            content_type = (
                str(fetched.headers.get("Content-Type", "")).split(";", 1)[0].strip().casefold()
            )
            expected_format = WEB_IMAGE_MIME_FORMATS.get(content_type)
            if expected_format is None:
                raise ImageValidationError(
                    "unsupported_format",
                    "ADNMB image Content-Type is not supported",
                )
            validated = await asyncio.to_thread(
                validate_image_bytes,
                fetched.body,
                limits            = IMAGE_LIMITS,
                format_extensions = WEB_IMAGE_FORMAT_EXTENSIONS,
                expected_format   = expected_format,
            )
            filename = f"{digest}{validated.extension}"
            return cast(
                Path | None,
                await asyncio.to_thread(self._image_cache.put, filename, fetched.body),
            )
        except (SafeHttpError, ValueError) as exc:
            logger.warning("Image download rejected error_type=%s", type(exc).__name__)
        except Exception as exc:
            logger.warning("Image download error error_type=%s", type(exc).__name__)

        return None
