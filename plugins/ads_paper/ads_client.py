import json
import logging
import re
from typing import Any, Optional

import aiohttp

from .constants import (
    DEFAULT_MAX_RESULTS,
    DEFAULT_MAX_AUTHORS,
    DEFAULT_MAX_CITATIONS,
    DEFAULT_MAX_REFERENCES,
    ARXIV_URL_PATTERN,
    ARXIV_VERSION_PATTERN,
    ARXIV_NEW_FORMAT_PATTERN,
    ARXIV_OLD_FORMAT_PATTERN,
)

logger = logging.getLogger(__name__)

class ADSClient:
    def __init__(self, token: str, session: aiohttp.ClientSession):
        """Initialize ADS client with API token and shared HTTP session.
        
        Args:
            token: ADS API token
            session: Shared aiohttp ClientSession for connection pooling
        """
        self.token = token
        self.session = session
        self.base_url = "https://api.adsabs.harvard.edu/v1"
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

    async def search_papers(
        self,
        query: str,
        max_results: int = DEFAULT_MAX_RESULTS,
        fields: Optional[list[str]] = None
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
            fields = ["bibcode", "title", "author", "year", "citation_count", "arxiv_class", "identifier"]

        url = f"{self.base_url}/search/query"
        params = {
            "q": query,
            "fl": ",".join(fields),
            "rows": max_results,
            "sort": "citation_count desc"
        }

        try:
            async with self.session.get(url, headers=self.headers, params=params) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.error(f"ADS search failed: {resp.status} - {text}")
                    return []
                data = await resp.json()
                return data.get("response", {}).get("docs", [])
        except Exception as e:
            logger.exception(f"ADS search error: {e}")
            return []

    async def get_bibtex(self, bibcode: str) -> Optional[str]:
        """
        Get BibTeX citation for a paper.
        
        The ADS export API returns JSON with 'msg' and 'export' fields,
        but the Content-Type header may not be 'application/json'.
        We need to get text first, then parse JSON manually.
        """
        url = f"{self.base_url}/export/bibtex"
        payload = {"bibcode": [bibcode]}

        try:
            async with self.session.post(url, headers=self.headers, json=payload) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.error(f"ADS bibtex failed: {resp.status} - {text}")
                    return None
                # Get text first, then manually parse JSON
                # (ADS API may return JSON with unexpected Content-Type)
                text = await resp.text()
                data = json.loads(text)
                bibtex = data.get("export", "")
                return bibtex.strip() if bibtex else None
        except Exception as e:
            logger.exception(f"ADS bibtex error: {e}")
            return None

    async def get_paper_by_bibcode(self, bibcode: str) -> Optional[dict[str, Any]]:
        url = f"{self.base_url}/search/query"
        params = {
            "q": f"bibcode:{bibcode}",
            "fl": "bibcode,title,author,year,citation_count,arxiv_class,identifier,abstract",
            "rows": 1
        }

        try:
            async with self.session.get(url, headers=self.headers, params=params) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                docs = data.get("response", {}).get("docs", [])
                return docs[0] if docs else None
        except Exception as e:
            logger.exception(f"ADS get paper error: {e}")
            return None

    async def get_citations(self, bibcode: str, max_results: int = DEFAULT_MAX_CITATIONS) -> list[dict[str, Any]]:
        url = f"{self.base_url}/search/query"
        params = {
            "q": f"citations(bibcode:{bibcode})",
            "fl": "bibcode,title,author,year,citation_count",
            "rows": max_results,
            "sort": "citation_count desc"
        }

        try:
            async with self.session.get(url, headers=self.headers, params=params) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                return data.get("response", {}).get("docs", [])
        except Exception as e:
            logger.exception(f"ADS citations error: {e}")
            return []

    async def get_references(self, bibcode: str, max_results: int = DEFAULT_MAX_REFERENCES) -> list[dict[str, Any]]:
        url = f"{self.base_url}/search/query"
        params = {
            "q": f"references(bibcode:{bibcode})",
            "fl": "bibcode,title,author,year,citation_count",
            "rows": max_results,
            "sort": "citation_count desc"
        }

        try:
            async with self.session.get(url, headers=self.headers, params=params) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                return data.get("response", {}).get("docs", [])
        except Exception as e:
            logger.exception(f"ADS references error: {e}")
            return []

    async def search_by_author(self, author: str, max_results: int = DEFAULT_MAX_RESULTS) -> list[dict[str, Any]]:
        query = f'author:"{author}"'
        return await self.search_papers(query, max_results)

    async def search_by_arxiv_id(self, arxiv_id: str) -> Optional[dict[str, Any]]:
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
    def extract_arxiv_id(text: str) -> Optional[str]:
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
    def format_authors(authors: list[str], max_authors: int = DEFAULT_MAX_AUTHORS) -> str:
        if not authors:
            return "Unknown"
        if len(authors) <= max_authors:
            return ", ".join(authors)
        return ", ".join(authors[:max_authors]) + f" et al. ({len(authors)} authors)"

    @staticmethod
    def format_paper_info(paper: dict[str, Any]) -> str:
        title = paper.get("title", ["Unknown"])[0] if paper.get("title") else "Unknown"
        authors = ADSClient.format_authors(paper.get("author", []))
        year = paper.get("year", "N/A")
        citations = paper.get("citation_count", 0)
        bibcode = paper.get("bibcode", "")
        arxiv_id = ADSClient.extract_arxiv_id(bibcode)

        lines = [
            f"📄 {title}",
            f"   👤 {authors}",
            f"   📅 {year}",
            f"   📊 Cited: {citations}"
        ]
        if arxiv_id:
            lines.append(f"   🔗 arXiv: {arxiv_id}")
        lines.append(f"   📎 Bibcode: {bibcode}")

        return "\n".join(lines)
