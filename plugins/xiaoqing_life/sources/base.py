"""取材的统一数据结构、适配器协议与有界 JSON 请求。

所有请求只发往固定域名；响应体与 JSON 复杂度都有上限。只有目标主机恰好是
``api.bilibili.com`` 且存在有效的 B站 登录凭证时才附 Cookie（见 ``bili_auth``），其他主机永远不带。
图片地址来自外部内容，不在这里下载（梗图下载走 ``core.safe_http``）。
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
        "zh.wikipedia.org",
    }
)
BILI_API_HOST   = "api.bilibili.com"
WIKI_HOST       = "zh.wikipedia.org"
WIKI_USER_AGENT = "XiaoQingLife/0.5 (personal chat bot)"
SUBTITLE_SUFFIX = "hdslb.com"
SUBTITLE_BYTES  = 2 * 1024 * 1024
_BODY_LIMITS = BodyLimits(max_wire_bytes=4 * 1024 * 1024, max_decoded_bytes=8 * 1024 * 1024)
_JSON_LIMITS = JsonLimits(
    max_bytes        = _BODY_LIMITS.max_decoded_bytes,
    max_depth        = 32,
    max_nodes        = 200_000,
    max_string_chars = 2 * 1024 * 1024,
)
_SUBTITLE_LIMITS = BodyLimits(max_wire_bytes=SUBTITLE_BYTES, max_decoded_bytes=SUBTITLE_BYTES)
_SUBTITLE_JSON = JsonLimits(
    max_bytes        = SUBTITLE_BYTES,
    max_depth        = 8,
    max_nodes        = 100_000,
    max_string_chars = SUBTITLE_BYTES,
)
_XML_LIMITS = XmlLimits(max_bytes=8 * 1024 * 1024, max_nodes=200_000)
# 少数站点会把 JSON 标成 text/*；解析本身仍然严格。
_MIME_POLICY = MimePolicy(
    exact               = frozenset({"application/json", "text/json", "text/plain", "text/html"}),
    structured_suffixes = frozenset({"+json"}),
)

PASSAGE_CHARS    = 200
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
class SourcePassage:
    """认真读到的一段原文（简介、置顶、UP 主回复、字幕、百科、维基）。"""

    ref : str  # p<条目序号>.<序号>，消化提示里的编号
    kind: str  # desc / pinned / up_reply / subtitle / baike / wiki
    text: str  # 清洗后 ≤200 字
    at  : str = ""  # 字幕段的起始时间 "3:20"，其余为空


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
    # 深读材料；``desc_raw`` / ``cid`` 是 watch 留给深读的原始简介与分 P，不进提示词。
    passages        : tuple[SourcePassage, ...] = ()
    desc_raw        : str                       = ""
    cid             : int                       = 0
    subtitle_partial: bool                      = False


class Fetcher(Protocol):
    """适配器只依赖这一个入口，测试里用假实现替换。"""

    async def get_json(
        self, url: str, *, params: Mapping[str, str] | None = None, referer: str = ""
    ) -> Any: ...

    async def get_xml(
        self, url: str, *, params: Mapping[str, str] | None = None, referer: str = ""
    ) -> bytes: ...

    async def get_public_json(self, url: str) -> Any: ...


class CookieSource(Protocol):
    """B站 登录凭证：``cookie_header`` 为空表示没有可用凭证；``mark_invalid`` 记录失效。"""

    def cookie_header(self) -> str: ...

    def mark_invalid(self) -> None: ...


def is_subtitle_host(host: str) -> bool:
    return host == SUBTITLE_SUFFIX or host.endswith("." + SUBTITLE_SUFFIX)


class BoundedFetcher:
    """基于 ``context.http_session`` 的有界 GET；只有 ``api.bilibili.com`` 可能带登录 Cookie。"""

    def __init__(self, session: Any, credential: CookieSource | None = None) -> None:
        self._session    = session
        self._credential = credential

    def _cookie_for(self, host: str) -> str:
        """Cookie 只给 ``api.bilibili.com``；其他主机（图床、字幕 CDN、百科、维基、知乎）永远为空。"""

        if host != BILI_API_HOST or self._credential is None:
            return ""
        return self._credential.cookie_header()

    async def get_public_json(self, url: str) -> Any:
        """取字幕文件：只允许 https 的 ``hdslb.com`` 及其子域，不带 Cookie 与 Referer。"""

        parts = urlsplit(url)
        host  = (parts.hostname or "").lower()
        if parts.scheme != "https" or not is_subtitle_host(host) or parts.port not in (None, 443):
            raise SourceError("http", "host_not_allowed")
        try:
            response = await aiohttp_request_bounded(
                self._session,
                "GET",
                url,
                limits      = _SUBTITLE_LIMITS,
                mime_policy = _MIME_POLICY,
                headers     = {"User-Agent": USER_AGENT, "Accept": "application/json, */*"},
                request_kwargs={"timeout": aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)},
            )
            return parse_bounded_json(response, limits=_SUBTITLE_JSON)
        except BoundedHttpError as exc:
            raise SourceError("hdslb", type(exc).__name__) from None
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise SourceError("hdslb", type(exc).__name__) from None

    async def get_json(
        self, url: str, *, params: Mapping[str, str] | None = None, referer: str = ""
    ) -> Any:
        host = (urlsplit(url).hostname or "").lower()
        if host not in ALLOWED_HOSTS:
            raise SourceError("http", "host_not_allowed")
        headers = {
            "User-Agent": WIKI_USER_AGENT if host == WIKI_HOST else USER_AGENT,
            "Accept"    : "application/json, text/plain, */*",
        }
        if referer:
            headers["Referer"] = referer
        cookie = self._cookie_for(host)
        if cookie:
            headers["Cookie"] = cookie
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
            payload = parse_bounded_json(response, limits=_JSON_LIMITS)
        except BoundedHttpError as exc:
            raise SourceError(host, type(exc).__name__) from None
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise SourceError(host, type(exc).__name__) from None
        # 带了 Cookie 的响应说“未登录”：凭证已失效，之后不再附 Cookie。
        if cookie and isinstance(payload, dict) and payload.get("code") == -101:
            assert self._credential is not None
            self._credential.mark_invalid()
        return payload

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


_PIECE_RE = re.compile(r"(?<=[。！？!?\n])")


def segment_text(
    raw: Any,
    kind: str,
    blocked: Iterable[str],
    *,
    max_total: int,
    max_segments: int,
) -> list[SourcePassage]:
    """按换行和句末标点切开，逐段清洗、去屏蔽词，再并成 ≤200 字的段；合计不超过 ``max_total`` 字。

    返回的段 ``ref`` 为空，由 ``deep.assign_refs`` 按条目序号编号。
    """

    if not isinstance(raw, str) or not raw:
        return []
    blocked             = tuple(blocked)
    segments: list[str] = []
    current             = ""
    total               = 0
    for piece in _PIECE_RE.split(raw[: max_total * 8]):
        text = clean_text(piece, PASSAGE_CHARS)
        if not text or is_blocked(text, blocked):
            continue
        text = text[: max(0, max_total - total)]
        if not text:
            break
        total += len(text)
        if current and len(current) + len(text) > PASSAGE_CHARS:
            segments.append(current)
            current = ""
        current += text
        if len(segments) >= max_segments or total >= max_total:
            break
    if current and len(segments) < max_segments:
        segments.append(current)
    return [SourcePassage("", kind, text) for text in segments[:max_segments]]


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
