"""NASA ADS 的有界异步客户端与论文字段格式化。"""

import logging
from typing import Any

import aiohttp

from core.bounded_http import (
    JSON_MIME_POLICY,
    BodyLimits,
    JsonLimits,
    MimePolicy,
    aiohttp_request_bounded,
    parse_bounded_json,
)
from core.public_errors import public_error_message

from .constants import (
    ADS_BIBCODE_PATTERN,
    ARXIV_NEW_FORMAT_PATTERN,
    ARXIV_OLD_FORMAT_PATTERN,
    ARXIV_URL_PATTERN,
    ARXIV_VERSION_PATTERN,
    DEFAULT_MAX_AUTHORS,
    DEFAULT_MAX_CITATIONS,
    DEFAULT_MAX_REFERENCES,
    DEFAULT_MAX_RESULTS,
)

logger = logging.getLogger(__name__)

_ADS_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=10, sock_read=20)
_ADS_BODY_LIMITS = BodyLimits(
    max_wire_bytes=2 * 1024 * 1024,
    max_decoded_bytes=4 * 1024 * 1024,
)
_ADS_JSON_LIMITS = JsonLimits(
    max_bytes=_ADS_BODY_LIMITS.max_decoded_bytes,
    max_depth=32,
    max_nodes=25_000,
    max_string_chars=3 * 1024 * 1024,
)
_ADS_BIBTEX_MIME_POLICY = MimePolicy(
    exact=frozenset(
        {
            "application/json",
            "text/json",
            "text/plain",
        }
    ),
    structured_suffixes=frozenset({"+json"}),
    # ADS 的 BibTeX 导出端点有时返回合法 JSON 却不附 Content-Type。
    # 这里只放宽 MIME；正文仍受字节、深度、节点和字符串长度限制并严格解析 JSON。
    allow_missing=True,
)


def _escape_ads_term(value: str) -> str:
    """Escape user text before embedding it in ADS query syntax."""

    return str(value).strip().replace("\\", "\\\\").replace('"', '\\"')


def _validate_bibcode(value: str) -> str:
    """Return a valid ADS bibcode or reject query-syntax input."""

    bibcode = str(value).strip()
    if not ADS_BIBCODE_PATTERN.fullmatch(bibcode):
        raise ValueError("invalid ADS bibcode")
    return bibcode


def paper_title(paper: dict[str, Any], default: str = "Unknown") -> str:
    """兼容 ADS 标题的列表或字符串表示，并始终返回完整字符串。"""

    raw_title = paper.get("title")
    if isinstance(raw_title, list):
        raw_title = raw_title[0] if raw_title else ""
    if isinstance(raw_title, str):
        title = raw_title.strip()
        if title:
            return title
    return default


class ADSClient:
    def __init__(
        self,
        token: str,
        session: aiohttp.ClientSession,
        context: Any | None = None,
    ) -> None:
        """Initialize ADS client with API token and shared HTTP session.

        Args:
            token: ADS API token
            session: Shared aiohttp ClientSession for connection pooling
        """
        self.token = token
        self.session = session
        self.context = context
        self.base_url = "https://api.adsabs.harvard.edu/v1"
        self.headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        request_kwargs: dict[str, Any],
        mime_policy: MimePolicy = JSON_MIME_POLICY,
    ) -> Any:
        response = await aiohttp_request_bounded(
            self.session,
            method,
            url,
            limits=_ADS_BODY_LIMITS,
            mime_policy=mime_policy,
            headers=self.headers,
            request_kwargs={
                **request_kwargs,
                "timeout": _ADS_REQUEST_TIMEOUT,
            },
        )
        return parse_bounded_json(response, limits=_ADS_JSON_LIMITS)

    async def _search_docs(
        self,
        query: str,
        *,
        fields: list[str],
        max_results: int,
        sort: str,
        component: str,
    ) -> list[dict[str, Any]]:
        """执行统一 ADS 搜索，并只返回结构正确的论文对象。"""

        if type(max_results) is not int or max_results <= 0:
            raise ValueError("max_results must be a positive integer")
        params = {
            "q": query,
            "fl": ",".join(fields),
            "rows": max_results,
            "sort": sort,
        }
        try:
            data = await self._request_json(
                "GET",
                f"{self.base_url}/search/query",
                request_kwargs={"params": params},
            )
            if not isinstance(data, dict):
                return []
            response = data.get("response", {})
            if not isinstance(response, dict):
                return []
            docs = response.get("docs", [])
            if not isinstance(docs, list):
                return []
            return [doc for doc in docs if isinstance(doc, dict)]
        except Exception as exc:
            public_error_message(
                self.context,
                exc,
                logger=logger,
                component=component,
            )
            return []

    async def search_papers(
        self,
        query: str,
        max_results: int = DEFAULT_MAX_RESULTS,
        fields: list[str] | None = None,
        *,
        sort: str = "citation_count desc",
    ) -> list[dict[str, Any]]:
        """
        Search for papers using ADS search API.

        Args:
            query: Search query string (supports ADS query syntax)
            max_results: Maximum number of results to return
            fields: list of fields to retrieve

        Returns:
            List of paper dictionaries from ADS API response
        """
        if fields is None:
            fields = [
                "bibcode",
                "title",
                "author",
                "year",
                "date",
                "citation_count",
                "arxiv_class",
                "identifier",
            ]

        return await self._search_docs(
            query,
            fields=fields,
            max_results=max_results,
            sort=sort,
            component="ads_paper.search",
        )

    async def get_bibtex(self, bibcode: str) -> str | None:
        """
        Get BibTeX citation for a paper.

        The ADS export API returns JSON with 'msg' and 'export' fields, but
        historically also labels that JSON as text.  Only this endpoint uses
        the narrow legacy text MIME compatibility policy.
        """
        url = f"{self.base_url}/export/bibtex"
        payload = {"bibcode": [bibcode]}

        try:
            data = await self._request_json(
                "POST",
                url,
                request_kwargs={"json": payload},
                mime_policy=_ADS_BIBTEX_MIME_POLICY,
            )
            if not isinstance(data, dict):
                return None
            bibtex = data.get("export", "")
            return bibtex.strip() if isinstance(bibtex, str) and bibtex else None
        except Exception as exc:
            public_error_message(
                self.context,
                exc,
                logger=logger,
                component="ads_paper.bibtex",
            )
            return None

    async def get_paper_by_bibcode(self, bibcode: str) -> dict[str, Any] | None:
        bibcode = _validate_bibcode(bibcode)
        docs = await self._search_docs(
            f"bibcode:{_escape_ads_term(bibcode)}",
            fields=[
                "bibcode",
                "title",
                "author",
                "year",
                "citation_count",
                "arxiv_class",
                "identifier",
                "abstract",
            ],
            max_results=1,
            sort="citation_count desc",
            component="ads_paper.get_paper",
        )
        return docs[0] if docs else None

    async def get_citations(
        self, bibcode: str, max_results: int = DEFAULT_MAX_CITATIONS
    ) -> list[dict[str, Any]]:
        bibcode = _validate_bibcode(bibcode)
        return await self._search_docs(
            f"citations(bibcode:{_escape_ads_term(bibcode)})",
            fields=["bibcode", "title", "author", "year", "citation_count"],
            max_results=max_results,
            sort="citation_count desc",
            component="ads_paper.citations",
        )

    async def get_references(
        self, bibcode: str, max_results: int = DEFAULT_MAX_REFERENCES
    ) -> list[dict[str, Any]]:
        bibcode = _validate_bibcode(bibcode)
        return await self._search_docs(
            f"references(bibcode:{_escape_ads_term(bibcode)})",
            fields=["bibcode", "title", "author", "year", "citation_count"],
            max_results=max_results,
            sort="citation_count desc",
            component="ads_paper.references",
        )

    async def search_by_author(
        self, author: str, max_results: int = DEFAULT_MAX_RESULTS
    ) -> list[dict[str, Any]]:
        query = f'author:"{_escape_ads_term(author)}"'
        return await self.search_papers(query, max_results)

    async def search_by_arxiv_id(self, arxiv_id: str) -> dict[str, Any] | None:
        arxiv_id = self._normalize_arxiv_id(arxiv_id)
        query = f"arxiv:{arxiv_id}"
        papers = await self.search_papers(query, max_results=1)
        return papers[0] if papers else None

    @staticmethod
    def _normalize_arxiv_id(arxiv_id: str) -> str:
        """
        Normalize arXiv ID from various formats.
        Supports:
        - URLs: https://arxiv.org/abs/2401.12345 or http://arxiv.org/abs/astro-ph/0701089
        - New format: 2401.12345 or 2401.12345v1
        - Old format: astro-ph/0701089 or astro-ph/0701089v1
        """
        arxiv_id = arxiv_id.strip()
        if arxiv_id.startswith("http"):
            # Extract from URL - supports both old and new formats
            # New: https://arxiv.org/abs/2401.12345v1
            # Old: https://arxiv.org/abs/astro-ph/0701089v1
            match = ARXIV_URL_PATTERN.search(arxiv_id)
            if match:
                return match.group(1)
        # Remove version number if present (e.g., v1, v2)
        arxiv_id = ARXIV_VERSION_PATTERN.sub("", arxiv_id)
        return arxiv_id

    @staticmethod
    def extract_arxiv_id(text: str) -> str | None:
        """
        Extract arXiv ID from text (e.g., bibcode).
        Supports:
        - New format: 2401.12345 or 0706.0001
        - Old format: astro-ph/0701089, hep-th/9901001, etc.
        """
        # Try new format first (YYMM.NNNNN or YYMM.NNNNNN)
        match = ARXIV_NEW_FORMAT_PATTERN.search(text)
        if match:
            return match.group(1)
        # Try old format (archive/YYMMNNN)
        match = ARXIV_OLD_FORMAT_PATTERN.search(text)
        if match:
            return match.group(1)
        return None

    @staticmethod
    def format_authors(authors: Any, max_authors: int = DEFAULT_MAX_AUTHORS) -> str:
        if isinstance(authors, str):
            normalized = [authors]
        elif isinstance(authors, list):
            normalized = [str(author) for author in authors if isinstance(author, (str, int))]
        else:
            normalized = []
        if not normalized:
            return "Unknown"
        if len(normalized) <= max_authors:
            return ", ".join(normalized)
        return ", ".join(normalized[:max_authors]) + f" et al. ({len(normalized)} authors)"

    @staticmethod
    def format_paper_info(paper: dict[str, Any]) -> str:
        title = paper_title(paper)
        authors = ADSClient.format_authors(paper.get("author", []))
        year = paper.get("year", "N/A")
        citations = paper.get("citation_count", 0)
        bibcode = paper.get("bibcode", "")
        arxiv_id = ADSClient.extract_arxiv_id(bibcode)

        lines = [f"📄 {title}", f"   👤 {authors}", f"   📅 {year}", f"   📊 Cited: {citations}"]
        if arxiv_id:
            lines.append(f"   🔗 arXiv: {arxiv_id}")
        lines.append(f"   📎 Bibcode: {bibcode}")

        return "\n".join(lines)
