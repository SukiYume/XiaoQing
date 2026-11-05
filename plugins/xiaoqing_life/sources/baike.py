"""百度百科词条卡片：查一个说法是不是网络流行语。"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from ..safety import clean_text, is_blocked
from .base import Fetcher, SourceError, as_dict, require_ok

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
