"""Flickr REST API 的有界只读客户端与响应模型。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit

from core.bounded_http import (
    BodyLimits,
    BoundedHttpError,
    JsonLimits,
    MimePolicy,
    aiohttp_request_bounded,
    parse_bounded_json,
)
from core.plugin_base import PluginContextProtocol, has_control_characters

API_ENDPOINT = "https://api.flickr.com/services/rest"
API_TIMEOUT_SECONDS = 15.0
MAX_API_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_RESULTS_PER_REQUEST = 100

PHOTO_EXTRAS = ",".join(
    (
        "description",
        "license",
        "date_taken",
        "owner_name",
        "tags",
        "media",
        "url_c",
        "url_l",
        "url_z",
        "url_m",
    )
)
PHOTOSET_EXTRAS = ",".join(("license", "date_taken", "owner_name", "tags", "media", "url_m"))

JSON_MIME_POLICY = MimePolicy(
    exact=frozenset({"application/json", "application/javascript", "text/json", "text/javascript"})
)
API_BODY_LIMITS = BodyLimits(
    max_wire_bytes=MAX_API_RESPONSE_BYTES,
    max_decoded_bytes=MAX_API_RESPONSE_BYTES,
    max_decompression_ratio=20,
    ratio_grace_bytes=64 * 1024,
    chunk_bytes=64 * 1024,
)
API_JSON_LIMITS = JsonLimits(
    max_bytes=MAX_API_RESPONSE_BYTES,
    max_depth=24,
    max_nodes=20_000,
    # Flickr 列表会汇总照片标题、描述、标签和多种 URL；总字符串预算需覆盖
    # 合法的 100 项响应，最终内存与传输规模仍由 2 MiB 字节上限约束。
    max_string_chars=1_000_000,
    max_number_chars=128,
)

_SAFE_API_KEY = re.compile(r"[A-Za-z0-9._-]{8,256}\Z")
_SAFE_OPAQUE_ID = re.compile(r"[A-Za-z0-9@_-]{1,128}\Z")
_FLICKR_HOSTS = frozenset({"flickr.com", "www.flickr.com", "m.flickr.com"})
_STATIC_IMAGE_HOST = "live.staticflickr.com"
_NSID = re.compile(r"[A-Za-z0-9_-]+@N[A-Za-z0-9_-]+\Z", re.IGNORECASE)


class FlickrError(RuntimeError):
    """Flickr 插件可以转成稳定用户提示的基础异常。"""


class FlickrConfigurationError(FlickrError):
    """Flickr API Key 缺失或格式非法。"""


class FlickrTransportError(FlickrError):
    """远端传输失败。"""


class FlickrProtocolError(FlickrError):
    """远端响应不满足已声明的 JSON 契约。"""


class FlickrApiError(FlickrError):
    """Flickr 在 HTTP 200 响应中返回业务错误。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"Flickr API error {code}")


@dataclass(frozen=True, slots=True)
class FlickrPhoto:
    """一张可安全展示的 Flickr 公共照片。"""

    photo_id: str
    owner_id: str
    owner_name: str
    title: str
    description: str
    license_id: str
    taken_at: str
    tags: tuple[str, ...]
    media_url: str
    page_url: str


@dataclass(frozen=True, slots=True)
class FlickrPage:
    """一个 Flickr 标准照片列表页面。"""

    photos: tuple[FlickrPhoto, ...]
    page: int
    pages: int
    total: int


def _plugin_api_key(context: PluginContextProtocol) -> str:
    settings = context.get_settings_snapshot().plugin_secrets("flickr")
    value = settings.get("api_key")
    if not isinstance(value, str):
        raise FlickrConfigurationError("Flickr api_key is not configured")
    api_key = value.strip()
    if _SAFE_API_KEY.fullmatch(api_key) is None:
        raise FlickrConfigurationError("Flickr api_key has an invalid format")
    return api_key


def _safe_id(value: object) -> str | None:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return None
    candidate = str(value).strip()
    return candidate if _SAFE_OPAQUE_ID.fullmatch(candidate) else None


def _visible_text(value: object, *, limit: int, default: str = "") -> str:
    if not isinstance(value, str):
        return default
    normalized = " ".join(value.replace("\r", " ").replace("\n", " ").split())
    if not normalized or has_control_characters(normalized, include_c1=True):
        return default
    return normalized[:limit]


def _content_text(value: object, *, limit: int) -> str:
    if isinstance(value, Mapping):
        value = value.get("_content")
    return _visible_text(value, limit=limit)


def _nonnegative_int(value: object, *, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return default
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default
    return result if result >= 0 else default


def _license_id(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return "0"
    candidate = str(value).strip()
    return candidate if re.fullmatch(r"[0-9]{1,4}", candidate) else "0"


def _validated_image_url(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 2_048:
        return None
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").rstrip(".").casefold() != _STATIC_IMAGE_HOST
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/")
    ):
        return None
    return value.strip()


def _constructed_image_url(photo_id: str, server: str, secret: str) -> str:
    return (
        f"https://{_STATIC_IMAGE_HOST}/"
        f"{quote(server, safe='')}/{quote(photo_id, safe='')}_{quote(secret, safe='')}_c.jpg"
    )


def _photo_page_url(owner_id: str, photo_id: str) -> str:
    return (
        "https://www.flickr.com/photos/"
        f"{quote(owner_id, safe='@_-')}/{quote(photo_id, safe='@_-')}/"
    )


def _select_media_url(item: Mapping[str, Any], *, photo_id: str) -> str | None:
    for field in ("url_c", "url_l", "url_z", "url_m"):
        candidate = _validated_image_url(item.get(field))
        if candidate is not None:
            return candidate
    server = _safe_id(item.get("server"))
    secret = _safe_id(item.get("secret"))
    if server is None or secret is None:
        return None
    return _constructed_image_url(photo_id, server, secret)


def _parse_tags(value: object) -> tuple[str, ...]:
    candidates: list[object] = []
    if isinstance(value, str):
        candidates.extend(value.split())
    elif isinstance(value, Mapping):
        raw = value.get("tag")
        if isinstance(raw, list):
            candidates.extend(raw)

    tags: list[str] = []
    for item in candidates:
        if isinstance(item, Mapping):
            item = item.get("raw", item.get("_content"))
        tag = _visible_text(item, limit=48)
        if tag and tag.casefold() not in {existing.casefold() for existing in tags}:
            tags.append(tag)
        if len(tags) >= 12:
            break
    return tuple(tags)


def _photo_from_list_item(
    item: object,
    *,
    default_owner_id: str = "",
    default_owner_name: str = "",
) -> FlickrPhoto | None:
    if not isinstance(item, Mapping):
        return None
    if item.get("media") not in {None, "photo"}:
        return None
    photo_id = _safe_id(item.get("id"))
    owner_id = _safe_id(item.get("owner")) or _safe_id(default_owner_id)
    if photo_id is None or owner_id is None:
        return None
    media_url = _select_media_url(item, photo_id=photo_id)
    if media_url is None:
        return None
    return FlickrPhoto(
        photo_id=photo_id,
        owner_id=owner_id,
        owner_name=_visible_text(
            item.get("ownername"),
            limit=160,
            default=_visible_text(default_owner_name, limit=160, default=owner_id),
        ),
        title=_visible_text(item.get("title"), limit=300, default="无标题"),
        description=_content_text(item.get("description"), limit=2_000),
        license_id=_license_id(item.get("license")),
        taken_at=_visible_text(item.get("datetaken"), limit=64),
        tags=_parse_tags(item.get("tags")),
        media_url=media_url,
        page_url=_photo_page_url(owner_id, photo_id),
    )


def _parse_photo_page(
    payload: Mapping[str, Any],
    *,
    container_name: str,
    default_owner_id: str = "",
    default_owner_name: str = "",
) -> FlickrPage:
    container = payload.get(container_name)
    if not isinstance(container, Mapping):
        raise FlickrProtocolError("Flickr photo list container is missing")
    raw_photos = container.get("photo", [])
    if not isinstance(raw_photos, list):
        raise FlickrProtocolError("Flickr photo list has an invalid shape")
    photos = tuple(
        photo
        for item in raw_photos
        if (
            photo := _photo_from_list_item(
                item,
                default_owner_id=default_owner_id,
                default_owner_name=default_owner_name,
            )
        )
        is not None
    )
    return FlickrPage(
        photos=photos,
        page=_nonnegative_int(container.get("page"), default=1),
        pages=_nonnegative_int(container.get("pages"), default=1),
        total=_nonnegative_int(container.get("total"), default=len(photos)),
    )


def _user_id_from_payload(payload: Mapping[str, Any]) -> str:
    user = payload.get("user")
    user_id = _safe_id(user.get("id")) if isinstance(user, Mapping) else None
    if user_id is None:
        raise FlickrProtocolError("Flickr user response has no valid id")
    return user_id


def _photo_from_info(payload: Mapping[str, Any]) -> FlickrPhoto:
    photo = payload.get("photo")
    if not isinstance(photo, Mapping):
        raise FlickrProtocolError("Flickr photo info is missing")
    owner = photo.get("owner")
    if not isinstance(owner, Mapping):
        raise FlickrProtocolError("Flickr photo owner is missing")
    photo_id = _safe_id(photo.get("id"))
    owner_id = _safe_id(owner.get("nsid"))
    if photo_id is None or owner_id is None:
        raise FlickrProtocolError("Flickr photo identifiers are invalid")
    media_url = _select_media_url(photo, photo_id=photo_id)
    if media_url is None:
        raise FlickrProtocolError("Flickr photo has no safe image URL")
    dates = photo.get("dates")
    taken_at = dates.get("taken") if isinstance(dates, Mapping) else ""
    owner_name = owner.get("realname") or owner.get("username") or owner_id
    return FlickrPhoto(
        photo_id=photo_id,
        owner_id=owner_id,
        owner_name=_visible_text(owner_name, limit=160, default=owner_id),
        title=_content_text(photo.get("title"), limit=300) or "无标题",
        description=_content_text(photo.get("description"), limit=2_000),
        license_id=_license_id(photo.get("license")),
        taken_at=_visible_text(taken_at, limit=64),
        tags=_parse_tags(photo.get("tags")),
        media_url=media_url,
        page_url=_photo_page_url(owner_id, photo_id),
    )


class FlickrClient:
    """仅访问 Flickr 固定 REST 端点的公共只读客户端。"""

    def __init__(self, context: PluginContextProtocol) -> None:
        self.context = context
        self.api_key = _plugin_api_key(context)

    async def _call(self, method: str, **parameters: object) -> Mapping[str, Any]:
        session = getattr(self.context, "http_session", None)
        if session is None:
            raise FlickrTransportError("Flickr HTTP session is unavailable")
        query: dict[str, object] = {
            "method": method,
            "api_key": self.api_key,
            "format": "json",
            "nojsoncallback": "1",
            **parameters,
        }
        try:
            response = await aiohttp_request_bounded(
                session,
                "GET",
                API_ENDPOINT,
                limits=API_BODY_LIMITS,
                mime_policy=JSON_MIME_POLICY,
                request_kwargs={"params": query, "timeout": API_TIMEOUT_SECONDS},
            )
            payload = parse_bounded_json(response, limits=API_JSON_LIMITS)
        except BoundedHttpError as exc:
            raise FlickrTransportError("Flickr bounded request failed") from exc
        except (OSError, TimeoutError, RuntimeError, ValueError) as exc:
            raise FlickrTransportError("Flickr request failed") from exc

        if not isinstance(payload, Mapping):
            raise FlickrProtocolError("Flickr response root is not an object")
        status = payload.get("stat")
        if status == "fail":
            raw_code = payload.get("code")
            code = (
                str(raw_code)
                if isinstance(raw_code, int) and not isinstance(raw_code, bool)
                else _visible_text(raw_code, limit=16, default="unknown")
            )
            raise FlickrApiError(code)
        if status != "ok":
            raise FlickrProtocolError("Flickr response has no success status")
        return payload

    async def interesting(self) -> FlickrPage:
        payload = await self._call(
            "flickr.interestingness.getList",
            extras=PHOTO_EXTRAS,
            per_page=MAX_RESULTS_PER_REQUEST,
            page=1,
        )
        return _parse_photo_page(payload, container_name="photos")

    async def search(
        self,
        *,
        query: str,
        tags: str,
        sort: str,
        license_ids: str | None,
        min_taken_date: str | None,
        max_taken_date: str | None,
        commons_only: bool = False,
    ) -> FlickrPage:
        parameters: dict[str, object] = {
            "text": query,
            "tags": tags,
            "tag_mode": "all",
            "sort": sort,
            "safe_search": 1,
            "media": "photos",
            "extras": PHOTO_EXTRAS,
            "per_page": MAX_RESULTS_PER_REQUEST,
            "page": 1,
        }
        if license_ids is not None:
            parameters["license"] = license_ids
        if min_taken_date is not None:
            parameters["min_taken_date"] = min_taken_date
        if max_taken_date is not None:
            parameters["max_taken_date"] = max_taken_date
        if commons_only:
            parameters["is_commons"] = 1
        payload = await self._call("flickr.photos.search", **parameters)
        return _parse_photo_page(payload, container_name="photos")

    async def resolve_user(self, reference: str) -> str:
        candidate = reference.strip()
        if _NSID.fullmatch(candidate):
            return candidate
        if not candidate or len(candidate) > 512 or has_control_characters(candidate):
            raise FlickrProtocolError("Flickr user reference is invalid")

        if "://" in candidate:
            try:
                parsed = urlsplit(candidate)
                port = parsed.port
            except ValueError as exc:
                raise FlickrProtocolError("Flickr user URL is invalid") from exc
            host = (parsed.hostname or "").rstrip(".").casefold()
            if (
                parsed.scheme.casefold() not in {"http", "https"}
                or host not in _FLICKR_HOSTS
                or parsed.username is not None
                or parsed.password is not None
                or port not in {None, 80, 443}
            ):
                raise FlickrProtocolError("Flickr user URL is invalid")
            parts = [part for part in parsed.path.split("/") if part]
            if len(parts) < 2 or parts[0].casefold() not in {"photos", "people"}:
                raise FlickrProtocolError("Flickr user URL is invalid")
            slug = parts[1]
            if _NSID.fullmatch(slug):
                return slug
            lookup_url = f"https://www.flickr.com/photos/{quote(slug, safe='@_-')}/"
            payload = await self._call("flickr.urls.lookupUser", url=lookup_url)
            return _user_id_from_payload(payload)

        payload = await self._call("flickr.people.findByUsername", username=candidate)
        return _user_id_from_payload(payload)

    async def public_photos(self, user_id: str) -> FlickrPage:
        payload = await self._call(
            "flickr.people.getPublicPhotos",
            user_id=user_id,
            safe_search=1,
            extras=PHOTO_EXTRAS,
            per_page=MAX_RESULTS_PER_REQUEST,
            page=1,
        )
        return _parse_photo_page(
            payload,
            container_name="photos",
            default_owner_id=user_id,
        )

    async def album_photos(self, *, user_id: str, album_id: str) -> FlickrPage:
        payload = await self._call(
            "flickr.photosets.getPhotos",
            user_id=user_id,
            photoset_id=album_id,
            media="photos",
            extras=PHOTOSET_EXTRAS,
            per_page=MAX_RESULTS_PER_REQUEST,
            page=1,
        )
        return _parse_photo_page(
            payload,
            container_name="photoset",
            default_owner_id=user_id,
        )

    async def photo_info(self, photo_id: str) -> FlickrPhoto:
        payload = await self._call("flickr.photos.getInfo", photo_id=photo_id)
        return _photo_from_info(payload)


__all__ = [
    "API_ENDPOINT",
    "FlickrApiError",
    "FlickrClient",
    "FlickrConfigurationError",
    "FlickrError",
    "FlickrPage",
    "FlickrPhoto",
    "FlickrProtocolError",
    "FlickrTransportError",
]
