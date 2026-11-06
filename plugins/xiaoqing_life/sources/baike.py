"""百度百科词条卡片：查一个说法是不是网络流行语。"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import quote

from ..safety import clean_text, is_blocked, normalize_for_match
from .base import TEXT_CHARS, Fetcher, SourceError, SourceItem, as_dict, require_ok, segment_text

_SLANG_MARKERS = (
    "网络流行语",
    "网络用语",
    "网络热词",
    "网络梗",
    "流行语",
    "网络用词",
    "网络语",
    "热梗",
    "流行梗",
)
_API = "https://baike.baidu.com/api/openapi/BaikeLemmaCardApi"


@dataclass(frozen=True, slots=True)
class BaikeEntry:
    term   : str
    meaning: str


def _is_slang(desc: str, abstract: str) -> bool:
    return any(marker in desc or marker in abstract for marker in _SLANG_MARKERS)


async def lookup(fetch: Fetcher, term: str, blocked: Iterable[str]) -> BaikeEntry | None:
    """查到且 desc/abstract 表明是网络流行语才返回；其余（含查不到）返回 None。"""

    try:
        payload = require_ok(
            "baike_lookup",
            await fetch.get_json(
                _API,
                params={
                    "scope"    : "103",
                    "format"   : "json",
                    "appid"    : "379020",
                    "bk_key"   : term,
                    "bk_length": "600",
                },
                referer="https://baike.baidu.com/",
            ),
            key="",
        )
    except SourceError as exc:
        if exc.reason == "bad_payload":
            return None
        raise
    desc     = clean_text(as_dict(payload).get("desc"), 80)
    abstract = clean_text(as_dict(payload).get("abstract"), 300)
    title    = clean_text(as_dict(payload).get("title"), 30)
    if not title or not _is_slang(desc, abstract):
        return None
    meaning = (abstract or desc)[:120]
    if is_blocked(" ".join((title, meaning)), blocked):
        return None
    return BaikeEntry(term=term, meaning=meaning)


async def read(fetch: Fetcher, term: str, blocked: Iterable[str]) -> SourceItem | None:
    """百科阅读：同一个卡片接口，不要求流行语标记；摘要切成 ≤3 段 ``baike``。查不到返回 None。"""

    try:
        payload = require_ok(
            "baike_read",
            await fetch.get_json(
                _API,
                params={
                    "scope"    : "103",
                    "format"   : "json",
                    "appid"    : "379020",
                    "bk_key"   : term,
                    "bk_length": "600",
                },
                referer="https://baike.baidu.com/",
            ),
            key="",
        )
    except SourceError as exc:
        if exc.reason == "bad_payload":
            return None
        raise
    title    = clean_text(payload.get("title"), 30)
    desc     = clean_text(payload.get("desc"), 80)
    abstract = payload.get("abstract")
    # 任何一部分命中屏蔽词就整条不读（不是只删掉那一句）。
    if not title or is_blocked(f"{title} {desc} {clean_text(abstract, 4000)}", blocked):
        return None
    passages = segment_text(abstract, "baike", blocked, max_total=600, max_segments=3)
    if not passages:
        return None
    return SourceItem(
        item_id  = f"baike_read:{normalize_for_match(title)[:40]}",
        source   = "baike_read",
        title    = title,
        text     = desc or passages[0].text[:TEXT_CHARS],
        url      = f"https://baike.baidu.com/item/{quote(title)}",
        heat     = 0,
        tags     = (),
        passages = tuple(passages),
    )
