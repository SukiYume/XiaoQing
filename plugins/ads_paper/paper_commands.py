import logging
from typing import Any, Optional

from core.plugin_base import segments

from .ads_client import ADSClient
from .constants import DEFAULT_MAX_RESULTS, MAX_TITLE_DISPLAY_LENGTH

logger = logging.getLogger(__name__)

async def resolve_paper_id_to_bibcode(
    client: ADSClient,
    paper_id: str
) -> Optional[str]:
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
        # 是 arXiv 相关的输入
        paper = await client.search_by_arxiv_id(paper_id)
        if paper:
            return paper.get("bibcode")
    
    # 否则假设是 bibcode，直接返回
    return paper_id

async def cmd_search(
    client: ADSClient,
    args: str
) -> list[dict[str, Any]]:
    if not args.strip():
        return segments("❌ 请提供搜索关键词\n用法: /paper search <关键词>")

    papers = await client.search_papers(args, max_results=5)

    if not papers:
        return segments(f"🔍 未找到与 '{args}' 相关的论文")

    lines = [f"📚 论文搜索结果 ({len(papers)} 条):\n"]
    for i, paper in enumerate(papers, 1):
        lines.append(f"{i}. {client.format_paper_info(paper)}\n")

    return segments("\n".join(lines))

async def cmd_author(
    client: ADSClient,
    args: str
) -> list[dict[str, Any]]:
    if not args.strip():
        return segments("❌ 请提供作者姓名\n用法: /paper author <作者姓名>")

    papers = await client.search_by_author(args, max_results=5)

    if not papers:
        return segments(f"🔍 未找到作者 '{args}' 的论文")

    lines = [f"👤 作者 '{args}' 的最新论文 ({len(papers)} 条):\n"]
    for i, paper in enumerate(papers, 1):
        lines.append(f"{i}. {client.format_paper_info(paper)}\n")

    return segments("\n".join(lines))

async def cmd_cite(
    client: ADSClient,
    args: str
) -> list[dict[str, Any]]:
    if not args.strip():
        return segments("❌ 请提供论文标识符\n用法: /paper cite <arXiv ID / arXiv链接 / Bibcode>")

    paper_id = args.strip()
    bibcode = await resolve_paper_id_to_bibcode(client, paper_id)

    if not bibcode:
        return segments(f"❌ 未找到论文: {paper_id}")

    bibtex = await client.get_bibtex(bibcode)
    if not bibtex:
        return segments(f"❌ 无法获取 BibTeX: {bibcode}")

    lines = ["📎 BibTeX 引用:\n", "```", bibtex, "```"]
    return segments("\n".join(lines))

async def cmd_cite_network(
    client: ADSClient,
    args: str
) -> list[dict[str, Any]]:
    if not args.strip():
        return segments("❌ 请提供论文标识符\n用法: /paper cite-network <arXiv ID / arXiv链接 / Bibcode>")

    paper_id = args.strip()
    bibcode = await resolve_paper_id_to_bibcode(client, paper_id)

    if not bibcode:
        return segments(f"❌ 未找到论文: {paper_id}")

    paper = await client.get_paper_by_bibcode(bibcode)
    if not paper:
        return segments(f"❌ 未找到论文: {bibcode}")

    citations = await client.get_citations(bibcode, max_results=5)
    references = await client.get_references(bibcode, max_results=5)

    title = paper.get("title", ["Unknown"])[0] if paper.get("title") else "Unknown"
    citation_count = paper.get("citation_count", 0)

    lines = [
        "📊 引用网络分析\n",
        f"📄 论文: {title}",
        f"📊 被引用次数: {citation_count}",
        f"📚 引用论文数: {len(references)}\n"
    ]

    if citations:
        lines.append("🔗 被以下论文引用 (前5篇):")
        for i, cit in enumerate(citations, 1):
            cit_title = cit.get("title", [""])[0] if cit.get("title") else ""
            cit_authors = ADSClient.format_authors(cit.get("author", []), max_authors=2)
            cit_year = cit.get("year", "")
            lines.append(f"  {i}. {cit_title[:MAX_TITLE_DISPLAY_LENGTH]}... - {cit_authors} {cit_year}")
        lines.append("")

    if references:
        lines.append("📖 引用了以下论文 (前5篇):")
        for i, ref in enumerate(references, 1):
            ref_title = ref.get("title", [""])[0] if ref.get("title") else ""
            ref_authors = ADSClient.format_authors(ref.get("author", []), max_authors=2)
            ref_year = ref.get("year", "")
            lines.append(f"  {i}. {ref_title[:MAX_TITLE_DISPLAY_LENGTH]}... - {ref_authors} {ref_year}")

    return segments("\n".join(lines))

async def cmd_related(
    client: ADSClient,
    args: str
) -> list[dict[str, Any]]:
    if not args.strip():
        return segments("❌ 请提供论文标识符\n用法: /paper related <arXiv ID / arXiv链接 / Bibcode>")

    paper_id = args.strip()
    bibcode = await resolve_paper_id_to_bibcode(client, paper_id)

    if not bibcode:
        return segments(f"❌ 未找到论文: {paper_id}")

    paper = await client.get_paper_by_bibcode(bibcode)
    if not paper:
        return segments(f"❌ 未找到论文: {bibcode}")

    title = paper.get("title", [""])[0] if paper.get("title") else ""
    keywords = title.split()[:3]
    query = " ".join(keywords)

    related = await client.search_papers(query, max_results=5)
    related = [p for p in related if p.get("bibcode") != bibcode]

    if not related:
        return segments("🔍 未找到相关论文")

    lines = [f"🔗 与 '{title[:MAX_TITLE_DISPLAY_LENGTH]}...' 相关的论文:\n"]
    for i, p in enumerate(related, 1):
        lines.append(f"{i}. {client.format_paper_info(p)}\n")

    return segments("\n".join(lines))
