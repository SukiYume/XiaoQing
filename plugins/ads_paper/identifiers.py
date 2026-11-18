"""论文标识解析：bibcode、arXiv 编号、DOI 以及它们的常见链接写法。"""

import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import unquote

_BIBCODE_PATTERN   = re.compile(r"\d{4}[A-Za-z0-9.&]{15}")
_ARXIV_NEW_PATTERN = re.compile(r"(\d{4}\.\d{4,5})(?:v\d+)?")
_ARXIV_OLD_PATTERN = re.compile(r"([a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?")
_DOI_PATTERN       = re.compile(r"10\.\d{4,9}/[^\s\"\\]+")
_ARXIV_URL_PATTERN = re.compile(
    r"https?://(?:www\.|export\.)?arxiv\.org/(?:abs|pdf|html)/(.+?)(?:\.pdf)?/?", re.IGNORECASE
)
_DOI_URL_PATTERN  = re.compile(r"https?://(?:dx\.)?doi\.org/(.+)", re.IGNORECASE)
_SCIX_URL_PATTERN = re.compile(
    r"https?://(?:www\.)?(?:scixplorer\.org|ui\.adsabs\.harvard\.edu|adsabs\.harvard\.edu)"
    r"/abs/([^/?#]+)(?:[/?#].*)?",
    re.IGNORECASE,
)
_PREFIX_PATTERN = re.compile(r"(arxiv|doi):\s*", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class PaperId:
    """一篇论文的规范标识；``value`` 不带前缀和 arXiv 版本号。"""

    kind : Literal["bibcode", "arxiv", "doi"]
    value: str

    @property
    def identifier(self) -> str:
        """SciX ``identifier`` 字段里对应的写法。"""

        return f"arXiv:{self.value}" if self.kind == "arxiv" else self.value


def _parse_arxiv(text: str) -> PaperId | None:
    match = _ARXIV_NEW_PATTERN.fullmatch(text) or _ARXIV_OLD_PATTERN.fullmatch(text)
    return PaperId("arxiv", match.group(1)) if match else None


def _parse_doi(text: str) -> PaperId | None:
    return PaperId("doi", text) if _DOI_PATTERN.fullmatch(text) else None


def parse_paper_id(text: str) -> PaperId | None:
    """识别一个论文标识；写法不属于任何一种时返回 None。

    支持：
    - bibcode：2018NatAs...2..206B
    - arXiv：2401.12345、2401.12345v2、astro-ph/0701089、arXiv:2401.12345，以及 arxiv.org 的 abs/pdf/html 链接
    - DOI：10.1038/s41550-017-0377-9、doi:10.1038/…、https://doi.org/10.1038/…
    - SciX 或 ADS 的摘要页链接
    """

    text = text.strip()
    if match := _SCIX_URL_PATTERN.fullmatch(text):
        return parse_paper_id(unquote(match.group(1)))
    if match := _ARXIV_URL_PATTERN.fullmatch(text):
        return _parse_arxiv(match.group(1))
    if match := _DOI_URL_PATTERN.fullmatch(text):
        return _parse_doi(unquote(match.group(1)))
    if match := _PREFIX_PATTERN.match(text):
        rest = text[match.end() :]
        return _parse_arxiv(rest) if match.group(1).lower() == "arxiv" else _parse_doi(rest)
    if _BIBCODE_PATTERN.fullmatch(text):
        return PaperId("bibcode", text)
    return _parse_arxiv(text) or _parse_doi(text)


def arxiv_id_of(paper: dict) -> str | None:
    """从 SciX 记录的 ``identifier`` 列表里取 arXiv 编号。"""

    identifiers = paper.get("identifier")
    if not isinstance(identifiers, list):
        return None
    for item in identifiers:
        if isinstance(item, str) and item.lower().startswith("arxiv:"):
            parsed = _parse_arxiv(item[6:])
            if parsed is not None:
                return parsed.value
    return None


def doi_of(paper: dict) -> str | None:
    """取出版商 DOI；arXiv 自己登记的 10.48550 DOI 不算。"""

    dois = paper.get("doi")
    if not isinstance(dois, list):
        return None
    for item in dois:
        if isinstance(item, str) and _DOI_PATTERN.fullmatch(item) and "arxiv" not in item.lower():
            return item
    return None
