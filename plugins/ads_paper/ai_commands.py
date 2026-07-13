import asyncio
import logging
import re
from typing import Any

from core.plugin_base import segments
from core.public_errors import public_error_message, public_error_response

from .ads_client import ADSClient
from .constants import DEFAULT_DAILY_PAPERS
from .paper_commands import resolve_paper_id_to_bibcode

logger = logging.getLogger(__name__)

async def cmd_summarize(
    client: ADSClient,
    args: str,
    context  # Type: PluginContext, but avoid circular import
) -> list[dict[str, Any]]:
    if not args.strip():
        return segments("❌ 请提供论文标识符\n用法: /paper summarize <arXiv ID / arXiv链接 / Bibcode>")

    paper_id = args.strip()
    bibcode = await resolve_paper_id_to_bibcode(client, paper_id)

    if not bibcode:
        return segments(f"❌ 未找到论文: {paper_id}")

    paper = await client.get_paper_by_bibcode(bibcode)
    if not paper:
        return segments(f"❌ 未找到论文: {bibcode}")

    title = paper.get("title", [""])[0] if paper.get("title") else ""
    abstract = paper.get("abstract", "")

    if not abstract:
        return segments(f"⚠️ 论文 '{title}' 没有摘要")

    llm_config = context.secrets.get("plugins", {}).get("ads_paper", {})
    api_base = llm_config.get("api_base")
    api_key = llm_config.get("api_key")
    model = llm_config.get("model")

    if not api_base or not api_key or not model:
        lines = [
            f"📄 论文: {title}\n",
            f"📝 摘要:\n{abstract}\n",
            "💡 提示: 配置 LLM 后可生成 AI 摘要"
        ]
        return segments("\n".join(lines))

    try:
        from .llm_client import generate_summary
        summary = await generate_summary(
            context.http_session,
            api_base,
            api_key,
            model,
            title,
            abstract
        )

        lines = [
            f"📄 论文: {title}\n",
            f"🤖 AI 摘要:\n{summary}"
        ]
        return segments("\n".join(lines))
    except Exception as exc:
        error_message = public_error_message(
            context,
            exc,
            logger=logger,
            component="ads_paper.summarize",
        )
        lines = [
            f"📄 论文: {title}\n",
            f"📝 原始摘要:\n{abstract}\n",
            f"❌ AI 摘要生成失败\n{error_message}",
        ]
        return segments("\n".join(lines))

async def cmd_daily(
    client: ADSClient,
    context,  # Type: PluginContext, but avoid circular import
    user_id: int,
) -> list[dict[str, Any]]:
    from .storage import PaperStorage
    storage = PaperStorage(context.data_dir)

    topics = await asyncio.to_thread(storage.get_topics, user_id)
    if not topics:
        return segments("🏷️ 请先添加研究兴趣关键词\n用法: /paper topics add <关键词>")

    query = " OR ".join(topics)
    papers = await client.search_papers(query, max_results=DEFAULT_DAILY_PAPERS)

    if not papers:
        return segments(f"🔍 未找到与关键词 '{', '.join(topics)}' 相关的新论文")

    lines = [f"📚 今日推荐论文 (基于关键词: {', '.join(topics)})\n"]
    for i, paper in enumerate(papers, 1):
        lines.append(f"{i}. {client.format_paper_info(paper)}\n")

    return segments("\n".join(lines))

async def cmd_ref_add(
    client: ADSClient,
    args: str,
    context,  # Type: PluginContext, but avoid circular import
    user_id: int,
) -> list[dict[str, Any]]:
    if not args.strip():
        return segments("❌ 请提供论文标识符\n用法: /paper ref_add <arXiv ID / arXiv链接 / Bibcode>")

    paper_id = args.strip()
    bibcode = await resolve_paper_id_to_bibcode(client, paper_id)

    if not bibcode:
        return segments(f"❌ 未找到论文: {paper_id}")

    bibtex = await client.get_bibtex(bibcode)
    if not bibtex:
        return segments(f"❌ 无法获取 BibTeX: {bibcode}")

    from .storage import PaperStorage
    storage = PaperStorage(context.data_dir)

    try:
        if not await asyncio.to_thread(storage.add_reference, user_id, bibcode, bibtex):
            return segments(f"⚠️ 该引用已在文献库中 (Bibcode: {bibcode})")

        lines = [
            "✅ 已添加到文献库\n",
            "📎 BibTeX:\n",
            "```",
            bibtex,
            "```"
        ]
        return segments("\n".join(lines))
    except Exception as exc:
        return public_error_response(
            context,
            exc,
            logger=logger,
            component="ads_paper.ref_add",
        )

async def cmd_refs(
    context,  # Type: PluginContext, but avoid circular import
    user_id: int,
) -> list[dict[str, Any]]:
    try:
        from .storage import PaperStorage
        storage = PaperStorage(context.data_dir)
        content = await asyncio.to_thread(storage.get_references, user_id)
        if not content:
            return segments("📚 文献库为空\n\n提示: 使用 '/paper ref_add <ID>' 添加引用")
        entries = [e.strip() for e in content.split("@") if e.strip()]

        if not entries:
            return segments("📚 文献库为空")

        lines = [f"📚 文献库 ({len(entries)} 条引用):\n"]
        for i, entry in enumerate(entries, 1):
            entry_lines = entry.split("\n")
            title_line = next(
                (line for line in entry_lines if "title" in line.lower()),
                "",
            )
            if title_line:
                # Improved title extraction with error handling
                # Improved title extraction using regex to handle various BibTeX formats
                # Matches: title = "{Title}", title = "Title", title = {Title}
                title_match = re.search(r'title\s*=\s*["{]*(.*?)[}"],?$', title_line, re.IGNORECASE)
                if title_match:
                    title = title_match.group(1)
                    lines.append(f"  {i}. {title[:60]}...")
                else:
                    lines.append(f"  {i}. {title_line.strip()[:60]}...")
            else:
                lines.append(f"  {i}. @entry...")

        return segments("\n".join(lines))
    except Exception as exc:
        return public_error_response(
            context,
            exc,
            logger=logger,
            component="ads_paper.refs",
        )
