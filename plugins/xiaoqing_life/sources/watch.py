"""“看”一个 B 站视频：详情、标签与弹幕（不下载视频、不看画面）。

她读到的是简介、标签、弹幕和评论区，所以经历里不能声称看完或看到画面。弹幕按归一化文本计频，
高频句是流行语候选的统计预筛；弹幕最密的时间点（30 秒分桶）记成 ``dense_moment``。
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import replace
from xml.etree import ElementTree

from ..safety import clean_text, is_blocked, is_profane, normalize_for_match
from .base import (
    TEXT_CHARS,
    Fetcher,
    SourceDanmaku,
    SourceError,
    SourceItem,
    as_dict,
    as_list,
    require_ok,
    to_int,
)
from .bilibili import API, REFERER, bvid_of

logger = logging.getLogger(__name__)

MAX_DANMAKU_REFS  = 20
MAX_DANMAKU_CHARS = 40
BUCKET_SECONDS    = 30
MAX_DENSE_MOMENTS = 2
MIN_DENSE_COUNT   = 3
MAX_TAGS          = 6
MAX_DANMAKU_ROWS  = 20000
_RUN_RE           = re.compile(r"(.)\1{2,}")


def process_danmaku(
    rows: Iterable[tuple[float, str]],
    item_index: int,
    blocked: Iterable[str],
    *,
    top: int = MAX_DANMAKU_REFS,
) -> tuple[tuple[SourceDanmaku, ...], str]:
    """去超长刷屏、纯符号与屏蔽词，按归一化文本计频取前 ``top`` 个，并找最密的时间桶。"""

    blocked                 = tuple(blocked)
    counts : Counter[str]   = Counter()
    shown  : dict[str, str] = {}
    buckets: Counter[int]   = Counter()
    for seconds, raw in rows:
        if len(raw) > MAX_DANMAKU_CHARS:
            continue
        text = clean_text(raw, MAX_DANMAKU_CHARS)
        key  = _RUN_RE.sub(r"\1\1\1", normalize_for_match(text))
        if not key or is_blocked(text, blocked) or is_profane(text):
            continue
        counts[key] += 1
        shown.setdefault(key, text)
        buckets[int(seconds) // BUCKET_SECONDS] += 1
    ranked  = counts.most_common(top)
    danmaku = tuple(
        SourceDanmaku(f"d{item_index}.{n}", shown[key], count)
        for n, (key, count) in enumerate(ranked, start=1)
    )
    return danmaku, _dense_moment(buckets)


def _dense_moment(buckets: Counter[int]) -> str:
    dense = [(b, c) for b, c in buckets.most_common(MAX_DENSE_MOMENTS) if c >= MIN_DENSE_COUNT]
    spots = []
    for bucket, _ in sorted(dense):
        start = bucket * BUCKET_SECONDS
        spots.append(f"{start // 60}:{start % 60:02d}")
    return "、".join(spots) + " 前后弹幕最密" if spots else ""


def parse_danmaku_xml(body: bytes) -> list[tuple[float, str]]:
    """``<d p="时间秒,...">文本</d>``；解析失败返回空。"""

    try:
        root = ElementTree.fromstring(body)  # noqa: S314 - 已拒绝 DTD 与实体
    except ElementTree.ParseError:
        return []
    rows: list[tuple[float, str]] = []
    for node in root.iter("d"):
        try:
            seconds = float(str(node.get("p", "0")).split(",", 1)[0])
        except ValueError:
            continue
        if node.text:
            rows.append((seconds, node.text))
        if len(rows) >= MAX_DANMAKU_ROWS:
            break
    return rows


async def _view(fetch: Fetcher, bvid: str) -> dict[str, object]:
    payload = require_ok(
        "bili_view",
        await fetch.get_json(f"{API}/x/web-interface/view", params={"bvid": bvid}, referer=REFERER),
    )
    return as_dict(payload.get("data"))


async def _tags(fetch: Fetcher, bvid: str) -> list[str]:
    payload = require_ok(
        "bili_tags",
        await fetch.get_json(f"{API}/x/tag/archive/tags", params={"bvid": bvid}, referer=REFERER),
    )
    names = (as_dict(row).get("tag_name") for row in as_list(payload.get("data")))
    return [clean_text(n, 12) for n in names if n][:MAX_TAGS]


async def watch(
    fetch: Fetcher, item: SourceItem, item_index: int, blocked: Iterable[str]
) -> SourceItem:
    """补充详情、标签、弹幕；任何一步失败只是少一块材料，条目照常可用。"""

    bvid = bvid_of(item)
    if not bvid:
        return item
    blocked = tuple(blocked)
    updated = item
    cid     = 0
    try:
        view  = await _view(fetch, bvid)
        owner = as_dict(view.get("owner")).get("name")
        desc  = clean_text(view.get("desc"), TEXT_CHARS)
        cid   = to_int(view.get("cid"))
        if not is_blocked(desc, blocked):
            updated = replace(
                updated,
                text       = desc or updated.text,
                uploader   = clean_text(owner, 20) or updated.uploader,
                duration_s = to_int(view.get("duration")),
            )
    except SourceError as exc:
        logger.info("xiaoqing_life step=source name=bili_view failed reason=%s", exc.reason)
    try:
        tags = await _tags(fetch, bvid)
        updated = replace(updated, tags=tuple(dict.fromkeys((*updated.tags, *tags)))[:8])
    except SourceError as exc:
        logger.info("xiaoqing_life step=source name=bili_tags failed reason=%s", exc.reason)
    if cid:
        try:
            body = await fetch.get_xml(
                f"{API}/x/v1/dm/list.so", params={"oid": str(cid)}, referer=REFERER
            )
            danmaku, dense = process_danmaku(parse_danmaku_xml(body), item_index, blocked)
            updated = replace(updated, danmaku=danmaku, dense_moment=dense)
        except SourceError as exc:
            logger.info("xiaoqing_life step=source name=bili_danmaku failed reason=%s", exc.reason)
    return updated
