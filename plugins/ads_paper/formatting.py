"""把 SciX 记录排成聊天消息里的文本块。"""

import html
import re
from typing import Any

from .constants import DEFAULT_MAX_AUTHORS, MAX_TITLE_DISPLAY_LENGTH
from .identifiers import arxiv_id_of, doi_of
from .scix_client import abstract_url

_SUPERSCRIPT_PATTERN = re.compile(r"<sup>(.*?)</sup>", re.IGNORECASE | re.DOTALL)
# 只认排版标签：摘要里的 "z<6 且 M>10" 这类不等式不是标记。
_TAG_PATTERN = re.compile(
    r"</?(?:sub|sup|inf|i|b|em|strong|u|tt|pre|p|br|h[1-6]|span|div|a|ul|ol|li)\b[^<>]*>",
    re.IGNORECASE,
)
_EMPHASIS_PATTERN = re.compile(r"</?em>", re.IGNORECASE)

# 期刊论文是默认类型，不单独标注。
_DOCTYPE_LABELS = {
    "article"      : "",
    "eprint"       : "预印本",
    "inproceedings": "会议论文",
    "proceedings"  : "会议文集",
    "abstract"     : "会议摘要",
    "inbook"       : "书章",
    "book"         : "专著",
    "phdthesis"    : "博士论文",
    "mastersthesis": "硕士论文",
    "proposal"     : "观测申请",
    "catalog"      : "星表",
    "software"     : "软件",
    "dataset"      : "数据集",
    "circular"     : "通告",
    "techreport"   : "技术报告",
    "erratum"      : "勘误",
    "editorial"    : "编辑部文章",
}


def clean_text(value: Any) -> str:
    """去掉 SciX 文本里的 HTML 标记和实体；上标写成 ^，免得 10<SUP>16</SUP> 读成 1016。"""

    if not isinstance(value, str):
        return ""
    text = _SUPERSCRIPT_PATTERN.sub(r"^\1", value)
    text = _TAG_PATTERN.sub("", text)
    return " ".join(html.unescape(text).split())


def highlight_text(value: str) -> str:
    """命中片段里的 <em> 换成【】，其余标记照 clean_text 处理。"""

    marked = _EMPHASIS_PATTERN.sub(lambda match: "】" if "/" in match.group() else "【", value)
    return clean_text(marked)


def paper_title(paper: dict[str, Any], default: str = "Unknown") -> str:
    """兼容 SciX 标题的列表或字符串表示，并始终返回完整字符串。"""

    raw_title = paper.get("title")
    if isinstance(raw_title, list):
        raw_title = raw_title[0] if raw_title else ""
    return clean_text(raw_title) or default


def short_title(paper: dict[str, Any]) -> str:
    """按移动端消息宽度截断标题，只在确实截断时添加省略号。"""

    title = paper_title(paper, default="")
    if len(title) <= MAX_TITLE_DISPLAY_LENGTH:
        return title
    return f"{title[:MAX_TITLE_DISPLAY_LENGTH]}…"


def format_authors(paper: dict[str, Any], max_authors: int = DEFAULT_MAX_AUTHORS) -> str:
    """列出前几位作者；人数以 author_count 为准，名单本身可能只取回了前几位。

    编辑部文章这类记录没有作者，返回空字符串。
    """

    authors = paper.get("author")
    if isinstance(authors, str):
        names = [authors]
    elif isinstance(authors, list):
        names = [str(author) for author in authors if isinstance(author, (str, int))]
    else:
        names = []
    if not names:
        return ""
    count = paper.get("author_count")
    total = max(count, len(names)) if type(count) is int else len(names)
    shown = "; ".join(names[:max_authors])
    return shown if total <= max_authors else f"{shown} 等 {total} 人"


def doctype_label(paper: dict[str, Any]) -> str:
    doctype = paper.get("doctype")
    if not isinstance(doctype, str):
        return ""
    return _DOCTYPE_LABELS.get(doctype, doctype)


def _is_refereed(paper: dict[str, Any]) -> bool:
    properties = paper.get("property")
    return isinstance(properties, list) and "REFEREED" in properties


def format_paper(paper: dict[str, Any], *, with_doi: bool = False) -> str:
    """一篇论文的多行信息块：标题、作者、出处与计数、标识、链接。

    列表里每篇只列 Bibcode 和 arXiv 编号，DOI 留给单篇详情，省下长列表的篇幅。
    """

    bibcode   = paper.get("bibcode")
    citations = paper.get("citation_count")
    facts     = [
        str(paper.get("year") or "年份未知"),
        clean_text(paper.get("pub")),
        doctype_label(paper),
        "同行评议" if _is_refereed(paper) else "",
        f"被引 {citations}" if citations is not None else "",
    ]
    arxiv_id = arxiv_id_of(paper)
    doi      = doi_of(paper) if with_doi else None
    ids      = [
        bibcode if isinstance(bibcode, str) else "",
        f"arXiv:{arxiv_id}" if arxiv_id else "",
        f"doi:{doi}" if doi else "",
    ]
    authors = format_authors(paper)
    lines   = [f"📄 {paper_title(paper)}"]
    if authors:
        lines.append(f"   👤 {authors}")
    lines.append(f"   📅 {' · '.join(fact for fact in facts if fact)}")
    if any(ids):
        lines.append(f"   📎 {' · '.join(item for item in ids if item)}")
    if isinstance(bibcode, str) and bibcode:
        lines.append(f"   🔗 {abstract_url(bibcode)}")
    return "\n".join(lines)


def paper_list_text(papers: list[dict[str, Any]], heading: str) -> str:
    """编号列出论文，每篇之间空一行。"""

    blocks = [f"{index}. {format_paper(paper)}" for index, paper in enumerate(papers, 1)]
    return "\n\n".join([heading, *blocks])
