"""ADS 论文搜索、引用、关系网络与相关论文命令。"""

from typing import Any

from core.delivery import CommandOutcome, DeliverySegments
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
        lines.append(f"  {index}. {_short_title(paper)} - {authors} {paper.get('year', '')}")
    return lines


async def resolve_paper_id_to_bibcode(client: ADSClient, paper_id: str) -> str | None:
    """
    将论文标识符转换为 bibcode，未找到时返回 None。支持：
    - arXiv ID: 2401.12345, astro-ph/0701089
    - arXiv URL: https://arxiv.org/abs/2401.12345
    - Bibcode: 2026arXiv260122115P
    """

    paper_id = paper_id.strip()
    # 看起来像 arXiv ID 或链接时，先在 ADS 中按 arXiv 编号检索
    if ADSClient._normalize_arxiv_id(paper_id) != paper_id or ADSClient.extract_arxiv_id(paper_id):
        paper   = await client.search_by_arxiv_id(paper_id)
        bibcode = paper.get("bibcode") if paper else None
        if not isinstance(bibcode, str):
            return None
        paper_id = bibcode
    # 只有合法 bibcode 才能拼进 ADS 查询语法
    try:
        return _validate_bibcode(paper_id)
    except ValueError:
        return None


async def resolve_bibcode_arg(
    client: ADSClient, args: str, command: str
) -> tuple[str | None, Segments | None]:
    """解析命令参数中的论文标识；失败时返回给用户的错误消息。"""

    paper_id = args.strip()
    if not paper_id:
        return None, segments(
            f"❌ 请提供论文标识符\n用法: /paper {command} <arXiv ID / arXiv链接 / Bibcode>"
        )
    bibcode = await resolve_paper_id_to_bibcode(client, paper_id)
    if not bibcode:
        return None, segments(f"❌ 未找到论文: {paper_id}")
    return bibcode, None


async def resolve_paper_arg(
    client: ADSClient,
    args: str,
    command: str,
) -> tuple[str, dict[str, Any] | None, Segments | None]:
    """在 resolve_bibcode_arg 的基础上再取回论文元数据。"""

    bibcode, error = await resolve_bibcode_arg(client, args, command)
    if bibcode is None:
        return "", None, error
    paper = await client.get_paper_by_bibcode(bibcode)
    if not paper:
        return bibcode, None, segments(f"❌ 未找到论文: {bibcode}")
    return bibcode, paper, None


def paper_list_text(client: ADSClient, papers: list[dict[str, Any]], heading: str) -> str:
    """编号列出论文，每篇之间空一行。"""

    lines = [heading]
    lines.extend(
        f"{index}. {client.format_paper_info(paper)}\n" for index, paper in enumerate(papers, 1)
    )
    return "\n".join(lines)


async def cmd_search(client: ADSClient, args: str) -> Segments:
    if not args.strip():
        return segments("❌ 请提供搜索关键词\n用法: /paper search <关键词>")
    papers = await client.search_papers(args, max_results=5)
    if not papers:
        return DeliverySegments(
            segments(f"🔍 未找到与 '{args}' 相关的论文"), outcome=CommandOutcome("completed")
        )
    text = paper_list_text(client, papers, f"📚 论文搜索结果 ({len(papers)} 条):\n")
    return DeliverySegments(segments(text), outcome=CommandOutcome("completed"))


async def cmd_author(client: ADSClient, args: str) -> Segments:
    if not args.strip():
        return segments("❌ 请提供作者姓名\n用法: /paper author <作者姓名>")
    papers = await client.search_by_author(args, max_results=5)
    if not papers:
        return DeliverySegments(
            segments(f"🔍 未找到作者 '{args}' 的论文"), outcome=CommandOutcome("completed")
        )
    text = paper_list_text(client, papers, f"👤 作者 '{args}' 的最新论文 ({len(papers)} 条):\n")
    return DeliverySegments(segments(text), outcome=CommandOutcome("completed"))


async def cmd_cite(client: ADSClient, args: str) -> Segments:
    bibcode, error = await resolve_bibcode_arg(client, args, "cite")
    if bibcode is None:
        return error or []
    bibtex = await client.get_bibtex(bibcode)
    if not bibtex:
        return segments(f"❌ 无法获取 BibTeX: {bibcode}")
    return segments(f"📎 BibTeX 引用:\n\n{bibtex}")


async def cmd_cite_network(client: ADSClient, args: str) -> Segments:
    bibcode, paper, error = await resolve_paper_arg(client, args, "cite-network")
    if paper is None:
        return error or []

    citations = await client.get_citations(bibcode, max_results=5)
    references = await client.get_references(bibcode, max_results=5)
    lines = [
        "📊 引用网络分析\n",
        f"📄 论文: {paper_title(paper)}",
        f"📊 被引用次数: {paper.get('citation_count', 0)}",
        f"📚 本次展示参考文献: {len(references)} 篇（最多 5 篇）\n",
    ]
    citation_lines = _relation_preview(citations, "🔗 被以下论文引用 (前5篇):")
    if citation_lines:
        lines.extend([*citation_lines, ""])
    lines.extend(_relation_preview(references, "📖 引用了以下论文 (前5篇):"))
    return segments("\n".join(lines))


async def cmd_related(client: ADSClient, args: str) -> Segments:
    bibcode, paper, error = await resolve_paper_arg(client, args, "related")
    if paper is None:
        return error or []

    # 用标题前三个词检索，排除论文本身
    query = " ".join(paper_title(paper, default="").split()[:3])
    related = [
        item
        for item in await client.search_papers(query, max_results=5)
        if item.get("bibcode") != bibcode
    ]
    if not related:
        return segments("🔍 未找到相关论文")
    return segments(
        paper_list_text(client, related, f"🔗 与 '{_short_title(paper)}' 相关的论文:\n")
    )
