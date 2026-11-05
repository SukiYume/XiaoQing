"""取材的统一数据结构、适配器协议与有界 JSON 请求。

所有请求只发往固定域名，不带 Cookie；响应体与 JSON 复杂度都有上限。图片地址来自
外部内容，不在这里下载（梗图下载走 ``core.safe_http``）。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

import aiohttp

from core.bounded_http import (
    XML_MIME_POLICY,
    BodyLimits,
    BoundedHttpError,
    JsonLimits,
    MimePolicy,
    XmlLimits,
    aiohttp_request_bounded,
    parse_bounded_json,
    validate_bounded_xml,
)

from ..safety import clean_text, is_blocked

REQUEST_TIMEOUT_SECONDS = 10
USER_AGENT              = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
ALLOWED_HOSTS = frozenset(
    {
        "api.bilibili.com",
        "api.zhihu.com",
        "weibo.com",
        "top.baidu.com",
        "baike.baidu.com",
    }
)
_BODY_LIMITS = BodyLimits(max_wire_bytes=4 * 1024 * 1024, max_decoded_bytes=8 * 1024 * 1024)
_JSON_LIMITS = JsonLimits(
    max_bytes        = _BODY_LIMITS.max_decoded_bytes,
    max_depth        = 32,
    max_nodes        = 200_000,
    max_string_chars = 2 * 1024 * 1024,
)
_XML_LIMITS = XmlLimits(max_bytes=8 * 1024 * 1024, max_nodes=200_000)
# 少数站点会把 JSON 标成 text/*；解析本身仍然严格。
_MIME_POLICY = MimePolicy(
    exact               = frozenset({"application/json", "text/json", "text/plain", "text/html"}),
    structured_suffixes = frozenset({"+json"}),
)

TITLE_CHARS      = 80
TEXT_CHARS       = 300
COMMENT_CHARS    = 200
REPLY_CHARS      = 120
MAX_COMMENT_PICS = 4
MAX_REPLIES      = 3
_SAFE_ID_RE      = re.compile(r"[^0-9A-Za-z_\-一-鿿]+")


class SourceError(Exception):
    """某个来源这次取不到；只携带来源名与简短原因，不带外部原文。"""

    def __init__(self, source: str, reason: str) -> None:
        super().__init__(f"{source}: {reason}")
        self.source = source
        self.reason = reason


@dataclass(frozen=True, slots=True)
class SourceComment:
    ref     : str
    text    : str
    likes   : int
    pictures: tuple[str, ...] = ()
    replies : tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SourceDanmaku:
    ref  : str
    text : str
    count: int


@dataclass(frozen=True, slots=True)
class SourceItem:
    item_id   : str
    source    : str
    title     : str
    text      : str
    url       : str
    heat      : int
    tags      : tuple[str, ...]
    comments  : tuple[SourceComment, ...] = ()
    uploader  : str                       = ""
    duration_s: int                       = 0
    # 弹幕最密的时间点描述（"第 3 分钟弹幕最密"）与高频弹幕；只有被她“点开”的视频才有。
    dense_moment: str                       = ""
    danmaku     : tuple[SourceDanmaku, ...] = ()


class Fetcher(Protocol):
    """适配器只依赖这一个入口，测试里用假实现替换。"""

    async def get_json(
        self, url: str, *, params: Mapping[str, str] | None = None, referer: str = ""
    ) -> Any: ...

    async def get_xml(
        self, url: str, *, params: Mapping[str, str] | None = None, referer: str = ""
    ) -> bytes: ...


class BoundedFetcher:
    """基于 ``context.http_session`` 的有界 JSON GET。"""

    def __init__(self, session: Any) -> None:
        self._session = session

    async def get_json(
        self, url: str, *, params: Mapping[str, str] | None = None, referer: str = ""
    ) -> Any:
        host = (urlsplit(url).hostname or "").lower()
        if host not in ALLOWED_HOSTS:
            raise SourceError("http", "host_not_allowed")
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json, text/plain, */*"}
        if referer:
            headers["Referer"] = referer
        try:
            response = await aiohttp_request_bounded(
                self._session,
                "GET",
                url,
                limits         = _BODY_LIMITS,
                mime_policy    = _MIME_POLICY,
                headers        = headers,
                request_kwargs = {
                    "params" : dict(params or {}),
                    "timeout": aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS),
                },
            )
            return parse_bounded_json(response, limits=_JSON_LIMITS)
        except BoundedHttpError as exc:
            raise SourceError(host, type(exc).__name__) from None
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise SourceError(host, type(exc).__name__) from None

    async def get_xml(
        self, url: str, *, params: Mapping[str, str] | None = None, referer: str = ""
    ) -> bytes:
        """有界 XML：拒绝 DTD / 实体，解压（含裸 deflate）与节点数都有上限。"""

        host = (urlsplit(url).hostname or "").lower()
        if host not in ALLOWED_HOSTS:
            raise SourceError("http", "host_not_allowed")
        headers = {"User-Agent": USER_AGENT, "Accept": "text/xml, application/xml, */*"}
        if referer:
            headers["Referer"] = referer
        try:
            response = await aiohttp_request_bounded(
                self._session,
                "GET",
                url,
                limits         = _BODY_LIMITS,
                mime_policy    = XML_MIME_POLICY,
                headers        = headers,
                request_kwargs = {
                    "params" : dict(params or {}),
                    "timeout": aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS),
                },
            )
            return validate_bounded_xml(response, limits=_XML_LIMITS)
        except BoundedHttpError as exc:
            raise SourceError(host, type(exc).__name__) from None
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise SourceError(host, type(exc).__name__) from None


def to_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return max(0, int(value))
    if isinstance(value, str):
        digits = re.sub(r"[^\d]", "", value)
        return int(digits[:12]) if digits else 0
    return 0


def safe_id(value: Any) -> str:
    return _SAFE_ID_RE.sub("", str(value))[:60]


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def require_ok(source: str, payload: Any, *, key: str = "code") -> dict[str, Any]:
    """B 站这类接口用 ``code`` 表示成败；非 0 或结构不对一律算来源失败。"""

    data = as_dict(payload)
    if not data:
        raise SourceError(source, "bad_payload")
    if key and data.get(key) not in (0, "0", None):
        raise SourceError(source, f"code_{safe_id(data.get(key))}")
    return data


def build_item(
    *,
    source: str,
    native_id: str,
    title: Any,
    text: Any,
    url: str,
    heat: int,
    tags: Iterable[Any],
    blocked: Iterable[str],
    comments: tuple[SourceComment, ...] = (),
    id_prefix: str                      = "",
    uploader: Any                       = "",
) -> SourceItem | None:
    """清洗并过滤成统一条目；标题为空、native_id 为空或命中屏蔽词时丢弃整条。"""

    clean_title = clean_text(title, TITLE_CHARS)
    clean_body  = clean_text(text, TEXT_CHARS)
    identifier  = safe_id(native_id)
    if not clean_title or not identifier:
        return None
    clean_tags = tuple(t for t in (clean_text(tag, 12) for tag in tags) if t)[:5]
    haystack   = " ".join((clean_title, clean_body, *clean_tags))
    if is_blocked(haystack, blocked):
        return None
    return SourceItem(
        item_id  = f"{id_prefix or source}:{identifier}",
        source   = source,
        title    = clean_title,
        text     = clean_body,
        url      = url,
        heat     = heat,
        tags     = clean_tags,
        comments = comments,
        uploader = clean_text(uploader, 20),
    )
