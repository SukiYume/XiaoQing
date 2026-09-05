"""ADS 论文搜索、引用、关系网络与相关论文命令。"""

from typing import Any

from core.plugin_base import Segments, segments

from .ads_client import ADSClient, _validate_bibcode, paper_title
from .constants import MAX_TITLE_DISPLAY_LENGTH


def _short_title(paper: dict[str, Any]) -> str:
    """按移动端消息宽度截断标题，只在确实截断时添加省略号。"""

    title = paper_title(paper, default="")
    if len(title) <= MAX_TITLE_DISPLAY_LENGTH:
        return title
    return f"{title[:MAX_TITLE_DISPLAY_LENGTH]}…"


def _relation_preview(papers: list[dict[str, Any]], heading: str) -> list[str]:
    """格式化引用关系列表，统一标题和作者的显示边界。"""

    if not papers:
        return []
    lines = [heading]
    for index, paper in enumerate(papers, 1):
        authors = ADSClient.format_authors(paper.get("author", []), max_authors=2)
        year = paper.get("year", "")
        lines.append(f"  {index}. {_short_title(paper)} - {authors} {year}")
    return lines


async def resolve_paper_id_to_bibcode(client: ADSClient, paper_id: str) -> str | None:
    """
    将各种格式的论文标识符转换为 bibcode

    支持的输入格式:
    - arXiv ID: 2401.12345, astro-ph/0701089
    - arXiv URL: https://arxiv.org/abs/2401.12345
    - Bibcode: 2026arXiv260122115P

    Returns:
        bibcode 字符串，如果未找到则返回 None
    """
    paper_id = paper_id.strip()

    # 如果看起来像 arXiv ID 或 URL，先尝试通过 arXiv 搜索
    # 使用 normalize 处理 URL，extract 处理纯 ID
    normalized_id = ADSClient._normalize_arxiv_id(paper_id)
    if normalized_id != paper_id or ADSClient.extract_arxiv_id(paper_id):
        paper = await client.search_by_arxiv_id(paper_id)
        if not paper:
            return None
        bibcode = paper.get("bibcode")
        if not isinstance(bibcode, str):
            return None
        try:
            return _validate_bibcode(bibcode)
        except ValueError:
            return None

    # Only valid ADS bibcodes may be embedded in ADS query syntax.
    try:
        return _validate_bibcode(paper_id)
    except ValueError:
        return None


async def cmd_search(client: ADSClient, args: str) -> Segments:
    if not args.strip():
        return segments("❌ 请提供搜索关键词\n用法: /paper search <关键词>")

    papers = await client.search_papers(args, max_results=5)

    if not papers:
        return segments(f"🔍 未找到与 '{args}' 相关的论文")

    lines = [f"📚 论文搜索结果 ({len(papers)} 条):\n"]
    for i, paper in enumerate(papers, 1):
        lines.append(f"{i}. {client.format_paper_info(paper)}\n")

    return segments("\n".join(lines))


async def cmd_author(client: ADSClient, args: str) -> Segments:
    if not args.strip():
        return segments("❌ 请提供作者姓名\n用法: /paper author <作者姓名>")

    papers = await client.search_by_author(args, max_results=5)

    if not papers:
        return segments(f"🔍 未找到作者 '{args}' 的论文")

    lines = [f"👤 作者 '{args}' 的最新论文 ({len(papers)} 条):\n"]
    for i, paper in enumerate(papers, 1):
        lines.append(f"{i}. {client.format_paper_info(paper)}\n")

    return segments("\n".join(lines))


async def cmd_cite(client: ADSClient, args: str) -> Segments:
    if not args.strip():
        return segments("❌ 请提供论文标识符\n用法: /paper cite <arXiv ID / arXiv链接 / Bibcode>")

    paper_id = args.strip()
    bibcode  = await resolve_paper_id_to_bibcode(client, paper_id)

    if not bibcode:
        return segments(f"❌ 未找到论文: {paper_id}")

    bibtex = await client.get_bibtex(bibcode)
    if not bibtex:
        return segments(f"❌ 无法获取 BibTeX: {bibcode}")

    lines = ["📎 BibTeX 引用:\n", "```", bibtex, "```"]
    return segments("\n".join(lines))


async def cmd_cite_network(client: ADSClient, args: str) -> Segments:
    if not args.strip():
        return segments(
            "❌ 请提供论文标识符\n用法: /paper cite-network <arXiv ID / arXiv链接 / Bibcode>"
        )

    paper_id = args.strip()
    bibcode  = await resolve_paper_id_to_bibcode(client, paper_id)

    if not bibcode:
        return segments(f"❌ 未找到论文: {paper_id}")

    paper = await client.get_paper_by_bibcode(bibcode)
    if not paper:
        return segments(f"❌ 未找到论文: {bibcode}")

    citations = await client.get_citations(bibcode, max_results=5)
    references = await client.get_references(bibcode, max_results=5)

    title          = paper_title(paper)
    citation_count = paper.get("citation_count", 0)

    lines = [
        "📊 引用网络分析\n",
        f"📄 论文: {title}",
        f"📊 被引用次数: {citation_count}",
        f"📚 本次展示参考文献: {len(references)} 篇（最多 5 篇）\n",
    ]

    citation_lines = _relation_preview(citations, "🔗 被以下论文引用 (前5篇):")
    if citation_lines:
        lines.extend(citation_lines)
        lines.append("")

    lines.extend(_relation_preview(references, "📖 引用了以下论文 (前5篇):"))

    return segments("\n".join(lines))


async def cmd_related(client: ADSClient, args: str) -> Segments:
    if not args.strip():
        return segments(
            "❌ 请提供论文标识符\n用法: /paper related <arXiv ID / arXiv链接 / Bibcode>"
        )

    paper_id = args.strip()
    bibcode  = await resolve_paper_id_to_bibcode(client, paper_id)

    if not bibcode:
        return segments(f"❌ 未找到论文: {paper_id}")

    paper = await client.get_paper_by_bibcode(bibcode)
    if not paper:
        return segments(f"❌ 未找到论文: {bibcode}")

    title = paper_title(paper, default="")
    keywords = title.split()[:3]
    query    = " ".join(keywords)

    related = await client.search_papers(query, max_results=5)
    related = [p for p in related if p.get("bibcode") != bibcode]

    if not related:
        return segments("🔍 未找到相关论文")

    lines = [f"🔗 与 '{_short_title(paper)}' 相关的论文:\n"]
    for i, p in enumerate(related, 1):
        lines.append(f"{i}. {client.format_paper_info(p)}\n")

    return segments("\n".join(lines))
