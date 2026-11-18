"""论文摘要的取得顺序：SciX 记录、arXiv 预印本、Europe PMC。

SciX 接口不返回部分出版商（Springer Nature 各刊等）的摘要。这些论文多数在 arXiv
有预印本，其余一部分被 Europe PMC 收录；两处都是公开接口，请求不带 SciX Token。
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any
from xml.etree import ElementTree

import aiohttp

from core.bounded_http import (
    JSON_MIME_POLICY,
    XML_MIME_POLICY,
    BodyLimits,
    BoundedHttpError,
    JsonLimits,
    XmlLimits,
    aiohttp_request_bounded,
    parse_bounded_json,
    validate_bounded_xml,
)
from core.log import LogEvent

from .formatting import clean_text
from .identifiers import arxiv_id_of, doi_of

log = logging.getLogger(__name__)

_ARXIV_API_URL  = "https://export.arxiv.org/api/query"
_EUROPE_PMC_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
_ATOM           = "{http://www.w3.org/2005/Atom}"
_FALLBACK_TIMEOUT = aiohttp.ClientTimeout(total=15, connect=8, sock_read=12)
_BODY_LIMITS = BodyLimits(max_wire_bytes=1024 * 1024, max_decoded_bytes=2 * 1024 * 1024)
_JSON_LIMITS = JsonLimits(max_bytes=_BODY_LIMITS.max_decoded_bytes, max_nodes=10_000)
_XML_LIMITS = XmlLimits(max_bytes=_BODY_LIMITS.max_decoded_bytes, max_nodes=10_000)
_EUROPE_PMC_LIMIT  = 4
_FALLBACK_FAILURES = (aiohttp.ClientError, TimeoutError, BoundedHttpError, ElementTree.ParseError)


@dataclass(frozen=True, slots=True)
class Abstract:
    """摘要正文及其出处；预印本摘要可能与发表版措辞不同，所以出处随正文一起展示。"""

    text  : str
    source: str


async def _arxiv_abstracts(session: aiohttp.ClientSession, arxiv_ids: list[str]) -> dict[str, str]:
    """一次请求取多篇 arXiv 摘要，返回 {不带版本号的编号: 摘要}。"""

    response = await aiohttp_request_bounded(
        session,
        "GET",
        _ARXIV_API_URL,
        limits         = _BODY_LIMITS,
        mime_policy    = XML_MIME_POLICY,
        request_kwargs = {
            "params" : {"id_list": ",".join(arxiv_ids), "max_results": len(arxiv_ids)},
            "timeout": _FALLBACK_TIMEOUT,
        },
    )
    feed = ElementTree.fromstring(validate_bounded_xml(response, limits=_XML_LIMITS))
    abstracts: dict[str, str] = {}
    for entry in feed.iter(f"{_ATOM}entry"):
        entry_id = (entry.findtext(f"{_ATOM}id") or "").rsplit("/abs/", 1)[-1]
        summary  = " ".join((entry.findtext(f"{_ATOM}summary") or "").split())
        for arxiv_id in arxiv_ids:
            # 条目 id 带版本号，如 2010.00870v1。
            if summary and (entry_id == arxiv_id or entry_id.startswith(f"{arxiv_id}v")):
                abstracts[arxiv_id] = summary
    return abstracts


async def _europe_pmc_abstract(session: aiohttp.ClientSession, doi: str) -> str:
    response = await aiohttp_request_bounded(
        session,
        "GET",
        _EUROPE_PMC_URL,
        limits         = _BODY_LIMITS,
        mime_policy    = JSON_MIME_POLICY,
        request_kwargs = {
            "params": {
                "query"     : f'DOI:"{doi}"',
                "resultType": "core",
                "format"    : "json",
                "pageSize"  : 1,
            },
            "timeout": _FALLBACK_TIMEOUT,
        },
    )
    data = parse_bounded_json(response, limits=_JSON_LIMITS)
    results = data.get("resultList", {}).get("result") if isinstance(data, dict) else None
    if not isinstance(results, list):
        return ""
    for result in results:
        if isinstance(result, dict) and str(result.get("doi", "")).casefold() == doi.casefold():
            return clean_text(result.get("abstractText"))
    return ""


async def resolve_abstracts(
    session: aiohttp.ClientSession, papers: list[dict[str, Any]]
) -> list[Abstract | None]:
    """为每篇论文取摘要，结果与 papers 一一对应；三处都没有时为 None。

    后两处只是补充来源：它们请求失败时记一条日志，这篇论文按没有摘要处理。
    """

    abstracts: list[Abstract | None] = []
    for paper in papers:
        text = clean_text(paper.get("abstract"))
        abstracts.append(Abstract(text, "SciX") if text else None)

    arxiv_ids = {
        index: arxiv_id
        for index, paper in enumerate(papers)
        if abstracts[index] is None and (arxiv_id := arxiv_id_of(paper))
    }
    if arxiv_ids:
        try:
            found = await _arxiv_abstracts(session, sorted(set(arxiv_ids.values())))
        except _FALLBACK_FAILURES as exc:
            log.warning(LogEvent("abstract.source_failed", source="arxiv", error=exc))
            found = {}
        for index, arxiv_id in arxiv_ids.items():
            if arxiv_id in found:
                abstracts[index] = Abstract(found[arxiv_id], "arXiv 预印本")

    dois = {
        index: doi
        for index, paper in enumerate(papers)
        if abstracts[index] is None and (doi := doi_of(paper))
    }
    if dois:
        limiter = asyncio.Semaphore(_EUROPE_PMC_LIMIT)

        async def fetch(doi: str) -> str:
            async with limiter:
                try:
                    return await _europe_pmc_abstract(session, doi)
                except _FALLBACK_FAILURES as exc:
                    log.warning(LogEvent("abstract.source_failed", source="europe_pmc", error=exc))
                    return ""

        texts = await asyncio.gather(*(fetch(doi) for doi in dois.values()))
        for index, text in zip(dois, texts, strict=True):
            if text:
                abstracts[index] = Abstract(text, "Europe PMC")
    return abstracts
