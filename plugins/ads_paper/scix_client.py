"""SciX（NASA ADS 的继任服务）API 的有界异步客户端。"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import aiohttp

from core.bounded_http import (
    JSON_MIME_POLICY,
    BodyLimits,
    JsonLimits,
    MimePolicy,
    aiohttp_request_bounded,
    parse_bounded_json,
)

from .identifiers import PaperId

SCIX_API_BASE      = "https://api.scixplorer.org/v1"
SCIX_TOKEN_URL     = "https://scixplorer.org/user/settings/token"
_SCIX_ABSTRACT_URL = "https://scixplorer.org/abs/{}/abstract"

_DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=10, sock_read=20)
# similar() 这类算子要在整个库里算相似度，实测十几秒才返回。
SLOW_QUERY_TIMEOUT = aiohttp.ClientTimeout(total=45, connect=10, sock_read=40)
_BODY_LIMITS = BodyLimits(
    max_wire_bytes    = 2 * 1024 * 1024,
    max_decoded_bytes = 4 * 1024 * 1024,
)
_JSON_LIMITS = JsonLimits(
    max_bytes        = _BODY_LIMITS.max_decoded_bytes,
    max_depth        = 32,
    max_nodes        = 25_000,
    max_string_chars = 3 * 1024 * 1024,
)
_EXPORT_MIME_POLICY = MimePolicy(
    exact=frozenset(
        {
            "application/json",
            "text/json",
            "text/plain",
        }
    ),
    structured_suffixes=frozenset({"+json"}),
    # 导出端点返回合法 JSON 却不附 Content-Type。
    # 这里只放宽 MIME；正文仍受字节、深度、节点和字符串长度限制并严格解析 JSON。
    allow_missing=True,
)

# 每条记录只取前几位作者，合作组论文的上千人名单不进响应；总人数看 author_count。
_AUTHOR_LIMIT_FIELD = "[fields author=3]"
# 论文列表展示所需字段；各查询在此基础上按需追加。
PAPER_LIST_FIELDS = (
    "bibcode",
    "title",
    "author",
    _AUTHOR_LIMIT_FIELD,
    "author_count",
    "year",
    "pub",
    "doctype",
    "property",
    "citation_count",
    "identifier",
    "doi",
)
PAPER_DETAIL_FIELDS = (*PAPER_LIST_FIELDS, "abstract", "keyword", "uat")
EXPORT_FORMATS      = (
    "bibtex",
    "bibtexabs",
    "aastex",
    "mnras",
    "icarus",
    "soph",
    "ieee",
    "ris",
    "endnote",
)
_HIGHLIGHT_FIELDS = ("abstract", "body")


def escape_query_term(value: str) -> str:
    """把用户文本放进 SciX 查询的双引号之前先转义。"""

    return str(value).strip().replace("\\", "\\\\").replace('"', '\\"')


def abstract_url(bibcode: str) -> str:
    return _SCIX_ABSTRACT_URL.format(quote(bibcode, safe=""))


@dataclass(frozen=True, slots=True)
class SearchPage:
    """一页检索结果：命中总数、本页记录、按 bibcode 归并的命中片段。"""

    total     : int
    docs      : list[dict[str, Any]]
    highlights: dict[str, dict[str, list[str]]] = field(default_factory=dict)


def _highlights_by_bibcode(
    payload: dict[str, Any], docs: list[dict[str, Any]]
) -> dict[str, dict[str, list[str]]]:
    """SciX 按内部记录号给片段，这里换成 bibcode 并丢掉空项。"""

    raw = payload.get("highlighting")
    if not isinstance(raw, dict):
        return {}
    bibcodes = {str(doc.get("id")): doc.get("bibcode") for doc in docs}
    result: dict[str, dict[str, list[str]]] = {}
    for record_id, fields in raw.items():
        bibcode = bibcodes.get(str(record_id))
        if not isinstance(bibcode, str) or not isinstance(fields, dict):
            continue
        snippets = {
            name: [item for item in items if isinstance(item, str) and item.strip()]
            for name, items in fields.items()
            if isinstance(items, list)
        }
        snippets = {name: items for name, items in snippets.items() if items}
        if snippets:
            result[bibcode] = snippets
    return result


### SciX 检索客户端：所有请求共享核心 HTTP 会话并受响应体积约束
class SciXClient:
    def __init__(self, token: str, session: aiohttp.ClientSession) -> None:
        """token 为 SciX API Token；session 是核心共享的 HTTP 会话。"""

        self.session = session
        # Token 只随这些头发往 SciX API 源；重定向由有界传输层拒绝。
        self.headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        request_kwargs: dict[str, Any],
        mime_policy: MimePolicy        = JSON_MIME_POLICY,
        timeout: aiohttp.ClientTimeout = _DEFAULT_TIMEOUT,
    ) -> Any:
        response = await aiohttp_request_bounded(
            self.session,
            method,
            f"{SCIX_API_BASE}{path}",
            limits         = _BODY_LIMITS,
            mime_policy    = mime_policy,
            headers        = self.headers,
            request_kwargs = {**request_kwargs, "timeout": timeout},
        )
        return parse_bounded_json(response, limits=_JSON_LIMITS)

    async def _query(
        self,
        params: dict[str, Any],
        *,
        timeout: aiohttp.ClientTimeout = _DEFAULT_TIMEOUT,
    ) -> dict[str, Any]:
        """执行一次检索请求，并确认响应带有结果块。"""

        data = await self._request_json(
            "GET", "/search/query", request_kwargs={"params": params}, timeout=timeout
        )
        if not isinstance(data, dict) or not isinstance(data.get("response"), dict):
            raise ValueError("SciX search response is missing response data")
        return data

    async def search(
        self,
        query: str,
        *,
        fields: Sequence[str] = PAPER_LIST_FIELDS,
        rows: int,
        start: int = 0,
        sort: str,
        highlight: str                 = "",
        timeout: aiohttp.ClientTimeout = _DEFAULT_TIMEOUT,
    ) -> SearchPage:
        """按 SciX 查询语法检索；highlight 非空时附带摘要和正文里的命中片段。"""

        params: dict[str, Any] = {
            "q"    : query,
            "fl"   : ",".join(fields),
            "rows" : rows,
            "start": start,
            "sort" : sort,
        }
        if highlight:
            # SciX 按内部记录号返回片段，所以一并取 id。
            params.update(
                {
                    "fl"         : f"{params['fl']},id",
                    "hl"         : "true",
                    "hl.fl"      : ",".join(_HIGHLIGHT_FIELDS),
                    "hl.q"       : highlight,
                    "hl.snippets": 2,
                    "hl.fragsize": 200,
                }
            )
        data = await self._query(params, timeout=timeout)
        response = data["response"]
        docs     = response.get("docs")
        total    = response.get("numFound")
        if not isinstance(docs, list) or type(total) is not int:
            raise ValueError("SciX search response is missing the document list")
        papers = [doc for doc in docs if isinstance(doc, dict)]
        return SearchPage(total, papers, _highlights_by_bibcode(data, papers))

    async def lookup(
        self,
        paper_ids: Sequence[PaperId],
        *,
        fields: Sequence[str] = PAPER_DETAIL_FIELDS,
    ) -> list[dict[str, Any] | None]:
        """一次请求取回多篇论文，结果与 paper_ids 一一对应，未收录的为 None。

        ``identifier`` 字段同时收录正式 bibcode、曾用 bibcode、DOI 和 arXiv 编号，
        所以预印本编号会落到已发表的那条记录上。
        """

        if not paper_ids:
            return []
        terms = " OR ".join(f'"{escape_query_term(item.identifier)}"' for item in paper_ids)
        page  = await self.search(
            f"identifier:({terms})",
            fields = fields,
            rows   = 2 * len(paper_ids),
            sort   = "date desc",
        )
        by_identifier: dict[str, dict[str, Any]] = {}
        for doc in page.docs:
            known = [doc.get("bibcode"), *(doc.get("identifier") or [])]
            for item in known:
                if isinstance(item, str):
                    by_identifier.setdefault(item.casefold(), doc)
        return [by_identifier.get(item.identifier.casefold()) for item in paper_ids]

    async def facet(self, query: str, facet_field: str, limit: int) -> list[tuple[str, int]]:
        """按一个分面字段统计命中数，返回 (取值, 篇数)，篇数多的在前。"""

        data = await self._query(
            {
                "q"             : query,
                "fl"            : "id",
                "rows"          : 0,
                "facet"         : "true",
                "facet.field"   : facet_field,
                "facet.limit"   : limit,
                "facet.mincount": 1,
            }
        )
        counts = data.get("facet_counts")
        values = (
            counts.get("facet_fields", {}).get(facet_field) if isinstance(counts, dict) else None
        )
        if not isinstance(values, list):
            raise ValueError("SciX search response is missing facet counts")
        # Solr 把分面写成 [取值, 篇数, 取值, 篇数, …] 的扁平列表。
        return [
            (str(name), count)
            for name, count in zip(values[::2], values[1::2], strict=False)
            if type(count) is int
        ]

    async def citation_stats(self, query: str) -> tuple[int, dict[str, Any]]:
        """命中总数，以及这批论文被引次数的 sum、mean、max 等统计。"""

        data = await self._query(
            {
                "q"          : query,
                "fl"         : "id",
                "rows"       : 0,
                "stats"      : "true",
                "stats.field": "citation_count",
            }
        )
        total = data["response"].get("numFound")
        if type(total) is not int:
            raise ValueError("SciX search response is missing the match count")
        stats = data.get("stats")
        value = (
            stats.get("stats_fields", {}).get("citation_count") if isinstance(stats, dict) else None
        )
        return total, value if isinstance(value, dict) else {}

    async def export(self, bibcodes: Sequence[str], export_format: str = "bibtex") -> str:
        """按给定格式导出引用文本；服务没有返回内容时为空字符串。"""

        data = await self._request_json(
            "POST",
            f"/export/{export_format}",
            request_kwargs = {"json": {"bibcode": list(bibcodes)}},
            mime_policy    = _EXPORT_MIME_POLICY,
        )
        text = data.get("export") if isinstance(data, dict) else None
        return text.strip() if isinstance(text, str) else ""

    async def journals(self, term: str) -> list[tuple[str, str]]:
        """按刊名或缩写查期刊，返回 (bibstem, 刊名)。"""

        data = await self._request_json(
            "GET",
            f"/journals/journal/{quote(term.strip(), safe='')}",
            request_kwargs={},
        )
        entries = data.get("journal") if isinstance(data, dict) else None
        if not isinstance(entries, list):
            raise ValueError("SciX journal response is missing the journal list")
        return [
            (str(entry["bibstem"]), str(entry.get("name", "")))
            for entry in entries
            if isinstance(entry, dict) and entry.get("bibstem")
        ]
