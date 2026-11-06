"""B 站字幕：WBI 签名的 player 接口 → 字幕轨道 → hdslb 字幕文件 → 清洗切段。

只在已登录且凭证有效时由深读调用。WBI 签名只用于字幕这一个接口，不给其他接口加签名。
字幕文件走 ``get_public_json``：只允许 https 的 ``hdslb.com`` 子域，不带 Cookie。
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from collections.abc import Callable, Iterable
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote, urlsplit

from ..safety import clean_text, is_blocked
from .base import (
    PASSAGE_CHARS,
    Fetcher,
    SourceError,
    SourcePassage,
    as_dict,
    as_list,
    is_subtitle_host,
    require_ok,
)
from .bilibili import API, REFERER

logger = logging.getLogger(__name__)

# 官方前端 JS 里的字符重排表，取前 32 位为 mixin key。
MIXIN = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
)  # fmt: skip
WBI_TTL_SECONDS  = 3600
FULL_CHARS       = 1200
PARTIAL_SEGMENTS = 6
LINE_CHARS       = 80
_STRIP_RE        = re.compile(r"[!'()*]")

# (过期时间, mixin key)；进程内缓存 1 小时。
_wbi_cache: tuple[float, str] | None = None


def mixin_key(img_url: str, sub_url: str) -> str:
    """两个图片文件名（不含扩展名）拼成 64 字符，按重排表取前 32 位；不足 64 位返回空。"""

    raw = "".join(PurePosixPath(urlsplit(url).path).stem for url in (img_url, sub_url))
    return "".join(raw[i] for i in MIXIN)[:32] if len(raw) >= 64 else ""


def sign(params: dict[str, str], key: str, wts: int) -> dict[str, str]:
    """值去掉 ``!'()*``，加 ``wts``，按键排序 urlencode（空格为 ``%20``）后拼 key 求 md5。"""

    values        = {k: _STRIP_RE.sub("", str(v)) for k, v in params.items()}
    values["wts"] = str(wts)
    # 与前端 ``encodeURIComponent`` 一致（``/`` 也编码）。
    query = "&".join(f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in sorted(values.items()))
    values["w_rid"] = hashlib.md5((query + key).encode()).hexdigest()  # noqa: S324 - 接口约定
    return values


async def wbi_key(fetch: Fetcher, now: Callable[[], float] = time.time) -> str:
    """从 ``nav`` 取 ``wbi_img``；mixin key 缓存 1 小时（每小时至多请求一次）。"""

    global _wbi_cache
    if _wbi_cache is not None and _wbi_cache[0] > now():
        return _wbi_cache[1]
    nav = as_dict(
        as_dict(await fetch.get_json(f"{API}/x/web-interface/nav", referer=REFERER)).get("data")
    )
    img = as_dict(nav.get("wbi_img"))
    key = mixin_key(str(img.get("img_url") or ""), str(img.get("sub_url") or ""))
    if not key:
        raise SourceError("bili_wbi", "keys_unavailable")
    _wbi_cache = (now() + WBI_TTL_SECONDS, key)
    return key


def pick_track(tracks: Iterable[Any]) -> dict[str, Any] | None:
    """优先 ``lan`` 以 ``zh`` 或 ``ai-zh`` 开头的轨道，其次第一条有地址的轨道。"""

    rows = [t for t in (as_dict(x) for x in tracks) if t.get("subtitle_url")]
    for track in rows:
        lan = str(track.get("lan") or "").lower()
        if lan.startswith(("zh", "ai-zh")):
            return track
    return rows[0] if rows else None


def normalize_url(url: Any) -> str | None:
    """补 ``https:``、``http:`` 改 ``https:``；主机必须是 ``hdslb.com`` 或其子域，否则返回 None。"""

    if not isinstance(url, str) or not url.strip():
        return None
    text = url.strip()
    if text.startswith("//"):
        text = "https:" + text
    elif text.startswith("http://"):
        text = "https://" + text[len("http://") :]
    parts = urlsplit(text)
    host  = (parts.hostname or "").lower()
    if parts.scheme != "https" or parts.username or not is_subtitle_host(host):
        return None
    return text


def format_at(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, sec = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{sec:02d}" if hours else f"{minutes}:{sec:02d}"


def _lines(body: Any, blocked: tuple[str, ...]) -> list[tuple[float, str]]:
    """逐行清洗、去屏蔽词和空行。"""

    rows: list[tuple[float, str]] = []
    for raw in as_list(body):
        row  = as_dict(raw)
        text = clean_text(row.get("content"), LINE_CHARS)
        if not text or is_blocked(text, blocked):
            continue
        try:
            start = float(row.get("from") or 0)
        except (TypeError, ValueError):
            start = 0.0
        rows.append((start, text))
    return rows


def _pack(rows: list[tuple[float, str]], start: int, limit: int) -> tuple[str, float, int]:
    """从 ``start`` 起把连续行并到 ≤``limit`` 字；返回（文本, 起始秒, 用掉的行数）。"""

    text = ""
    used = 0
    for _, line in rows[start:]:
        joined = f"{text} {line}".strip() if text else line
        if len(joined) > limit:
            if not text:
                text, used = line[:limit], 1
            break
        text, used = joined, used + 1
    return text, rows[start][0], used


def split_subtitle(body: Any, blocked: Iterable[str]) -> tuple[list[SourcePassage], bool]:
    """总长 ≤1200 字全部给出；否则全片均匀取 6 个起点、各收集连续行到 ≤200 字（节选）。"""

    rows = _lines(body, tuple(blocked))
    if not rows:
        return [], False
    total                    = sum(len(text) for _, text in rows)
    out: list[SourcePassage] = []
    if total <= FULL_CHARS:
        index = 0
        while index < len(rows):
            text, begin, used = _pack(rows, index, PASSAGE_CHARS)
            out.append(SourcePassage("", "subtitle", text, format_at(begin)))
            index += max(1, used)
        return out, False
    for n in range(PARTIAL_SEGMENTS):
        index = n * len(rows) // PARTIAL_SEGMENTS
        text, begin, _ = _pack(rows, index, PASSAGE_CHARS)
        out.append(SourcePassage("", "subtitle", text, format_at(begin)))
    return out, True


async def read(
    fetch: Fetcher,
    bvid: str,
    cid: int,
    blocked: Iterable[str],
    *,
    now: Callable[[], float] = time.time,
) -> tuple[list[SourcePassage], bool]:
    """取字幕并切段；没有字幕轨道返回 ``([], False)``，网络 / 结构问题抛 ``SourceError``。"""

    key    = await wbi_key(fetch, now)
    params = sign({"bvid": bvid, "cid": str(cid)}, key, int(now()))
    player = require_ok(
        "bili_player",
        await fetch.get_json(f"{API}/x/player/wbi/v2", params=params, referer=REFERER),
    )
    tracks = as_list(as_dict(as_dict(player.get("data")).get("subtitle")).get("subtitles"))
    track  = pick_track(tracks)
    if track is None:
        return [], False
    url = normalize_url(track.get("subtitle_url"))
    if url is None:
        logger.info("xiaoqing_life step=source name=bili_subtitle skipped reason=bad_host")
        return [], False
    document = as_dict(await fetch.get_public_json(url))
    return split_subtitle(document.get("body"), blocked)
