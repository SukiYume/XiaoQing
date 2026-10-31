"""AI 摘要、每日推荐与个人 BibTeX 文献库命令。"""

import asyncio
import logging
from datetime import UTC, date, datetime
from typing import Any

from core.plugin_base import PluginContextProtocol, Segments, segments
from core.public_errors import public_error_message, public_error_response

from .ads_client import ADSClient, _escape_ads_term, paper_title
from .bibtex import citation_entries
from .constants import DEFAULT_DAILY_PAPERS
from .paper_commands import paper_list_text, resolve_bibcode_arg, resolve_paper_arg
from .storage import PaperStorage

logger = logging.getLogger(__name__)


def _utc_today() -> date:
    return datetime.now(UTC).date()


def _paper_entry_date(paper: dict[str, Any]) -> date | None:
    raw = paper.get("entdate")
    if isinstance(raw, list):
        raw = raw[0] if raw else ""
    value = str(raw or "").strip()
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _daily_topic_query(topics: list[str], day: date) -> str:
    """任一关键词命中且当天及以后进入 ADS 的论文。"""

    quoted = [f'"{escaped}"' for topic in topics if (escaped := _escape_ads_term(topic))]
    return f"({' OR '.join(quoted)}) AND entdate:[{day.isoformat()} TO NOW]"


def _summary_messages(title: str, abstract: str) -> list[dict[str, Any]]:
    """构造论文摘要任务；模型、凭据和 fallback 由 core route 负责。"""

    prompt = f"""请用中文总结以下论文的要点，包括：
1. 研究背景和动机
2. 主要方法和创新点
3. 关键结果和结论
4. 研究意义

论文标题: {title}

摘要:
{abstract}

请用简洁清晰的语言总结，不超过300字。"""
    return [{"role": "user", "content": prompt}]


async def cmd_summarize(
    client: ADSClient,
    args: str,
    context: PluginContextProtocol,
) -> Segments:
    """通过 Core 的 ``summary`` route 生成摘要，不读取插件私有 AI 凭据。"""

    _bibcode, paper, error = await resolve_paper_arg(client, args, "summarize")
    if paper is None:
        return error or []

    title = paper_title(paper, default="")
    raw_abstract = paper.get("abstract", "")
    abstract     = raw_abstract.strip() if isinstance(raw_abstract, str) else ""

    if not abstract:
        return segments(f"⚠️ 论文 '{title}' 没有摘要")

    ai = context.capabilities.ai
    if ai is None:
        lines = [
            f"📄 论文: {title}\n",
            f"📝 摘要:\n{abstract}\n",
            "💡 提示: 配置 AI summary route 后可生成 AI 摘要",
        ]
        return segments("\n".join(lines))

    try:
        result  = await ai.complete("summary", _summary_messages(title, abstract))
        summary = result.content
        if not summary:
            raise RuntimeError("AI summary response is empty")

        return segments(f"📄 论文: {title}\n\n🤖 AI 摘要:\n{summary}")
    except Exception as exc:
        error_message = public_error_message(
            context,
            exc,
            logger    = logger,
            component = "ads_paper.summarize",
        )
        lines = [
            f"📄 论文: {title}\n",
            f"📝 原始摘要:\n{abstract}\n",
            f"❌ AI 摘要生成失败\n{error_message}",
        ]
        return segments("\n".join(lines))


async def cmd_daily(
    client: ADSClient,
    storage: PaperStorage,
    user_id: int,
) -> Segments:
    """按用户主题查询并严格保留 UTC 当天进入 ADS 的论文。"""

    topics = await asyncio.to_thread(storage.get_topics, user_id)
    if not topics:
        return segments("🏷️ 请先添加研究兴趣关键词\n用法: /paper topics add <关键词>")

    today  = _utc_today()
    query  = _daily_topic_query(topics, today)
    papers = await client.search_papers(
        query,
        max_results = DEFAULT_DAILY_PAPERS,
        fields      = [
            "bibcode",
            "title",
            "author",
            "year",
            "entdate",
            "citation_count",
            "arxiv_class",
            "identifier",
        ],
        sort="entdate desc,bibcode asc",
    )
    papers = [paper for paper in papers if _paper_entry_date(paper) == today]
    # 日期已被上一步严格过滤为同一天，本地稳定排序只需 bibcode。
    papers.sort(key=lambda paper: str(paper.get("bibcode", "") or ""))
    papers = papers[:DEFAULT_DAILY_PAPERS]

    if not papers:
        return segments(f"🔍 未找到与关键词 '{', '.join(topics)}' 相关的新论文")

    return segments(
        paper_list_text(client, papers, f"📚 今日推荐论文 (基于关键词: {', '.join(topics)})\n")
    )


async def cmd_ref_add(
    client: ADSClient,
    args: str,
    storage: PaperStorage,
    context: PluginContextProtocol,
    user_id: int,
) -> Segments:
    """获取一篇论文的 BibTeX，并原子加入当前用户的文献库。"""

    bibcode, error = await resolve_bibcode_arg(client, args, "ref_add")
    if bibcode is None:
        return error or []

    bibtex = await client.get_bibtex(bibcode)
    if not bibtex:
        return segments(f"❌ 无法获取 BibTeX: {bibcode}")

    try:
        if not await asyncio.to_thread(storage.add_reference, user_id, bibcode, bibtex):
            return segments(f"⚠️ 该引用已在文献库中 (Bibcode: {bibcode})")

        return segments(f"✅ 已添加到文献库\n\n📎 BibTeX:\n\n{bibtex}")
    except Exception as exc:
        return public_error_response(
            context,
            exc,
            logger    = logger,
            component = "ads_paper.ref_add",
        )


async def cmd_refs(
    storage: PaperStorage,
    context: PluginContextProtocol,
    user_id: int,
) -> Segments:
    """读取并结构化展示当前用户的 BibTeX 文献库。"""

    try:
        content = await asyncio.to_thread(storage.get_references, user_id)
        if not content:
            return segments("📚 文献库为空\n\n提示: 使用 '/paper ref_add <ID>' 添加引用")
        entries = citation_entries(content)

        if not entries:
            return segments("📚 文献库为空")

        lines = [f"📚 文献库 ({len(entries)} 条引用):\n"]
        for i, entry in enumerate(entries, 1):
            # 有标题显示标题（最长 60 字），否则显示条目类型与引用键
            if entry.title:
                lines.append(f"  {i}. {entry.title[:60]}{'...' if len(entry.title) > 60 else ''}")
            else:
                lines.append(f"  {i}. @{entry.entry_type}{{{entry.citation_key}}}")

        return segments("\n".join(lines))
    except Exception as exc:
        return public_error_response(
            context,
            exc,
            logger    = logger,
            component = "ads_paper.refs",
        )
