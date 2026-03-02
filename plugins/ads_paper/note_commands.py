import asyncio
import logging
from datetime import datetime
from typing import Any

from core.plugin_base import segments
from core.args import parse

from .storage import PaperStorage
from .constants import DATE_FORMAT

logger = logging.getLogger(__name__)


async def _run_storage(func, *args):
    return await asyncio.to_thread(func, *args)

async def cmd_note(
    storage: PaperStorage,
    args: str,
    user_id: int
) -> list[dict[str, Any]]:
    parsed = parse(args)

    if not parsed:
        return segments("❌ 用法:\n  /paper note <ID> <内容> - 添加笔记\n  /paper note <ID> - 查看笔记\n  /paper note del <ID> <序号> - 删除笔记")

    first = parsed.first.lower()

    if first == "del" or first == "delete" or first == "删除":
        if len(parsed) < 3:
            return segments("❌ 用法: /paper note del <ID> <序号>")
        paper_id = parsed.get(1)
        try:
            index = int(parsed.get(2)) - 1
        except ValueError:
            return segments("❌ 序号必须是数字")

        if await _run_storage(storage.delete_paper_note, paper_id, index):
            return segments(f"✅ 已删除论文 {paper_id} 的笔记")
        return segments(f"❌ 删除失败，请检查 ID 和序号")

    if len(parsed) == 1:
        paper_id = parsed.first
        notes = await _run_storage(storage.get_paper_notes, paper_id)
        if not notes:
            return segments(f"📝 论文 {paper_id} 暂无笔记")

        lines = [f"📝 论文 {paper_id} 的笔记 ({len(notes)} 条):\n"]
        for i, note in enumerate(notes, 1):
            content = note.get("content", "")
            time = note.get("time", "")
            lines.append(f"  {i}. {content}")
            if time:
                lines.append(f"     📅 {time}")

        return segments("\n".join(lines))

    paper_id = parsed.first
    content = parsed.rest(1)

    if not content.strip():
        return segments("❌ 笔记内容不能为空")

    if await _run_storage(storage.add_paper_note, paper_id, content, user_id):
        notes = await _run_storage(storage.get_paper_notes, paper_id)
        return segments(f"✅ 已添加笔记到论文 {paper_id} (#{len(notes)})")
    return segments("❌ 添加笔记失败")

async def cmd_writing(
    storage: PaperStorage,
    args: str,
    user_id: int
) -> list[dict[str, Any]]:
    parsed = parse(args)

    if not parsed:
        sections = await _run_storage(storage.list_writing_sections)
        if not sections:
            return segments("💡 写作灵感箱暂无内容\n\n提示: 使用 '/paper writing <章节> <想法>' 添加灵感")

        lines = ["💡 写作灵感箱:\n"]
        for section in sections:
            ideas = await _run_storage(storage.get_writing_ideas, section)
            lines.append(f"  • {section} ({len(ideas)} 条)")

        return segments("\n".join(lines))

    first = parsed.first.lower()

    if first == "del" or first == "delete" or first == "删除":
        if len(parsed) < 3:
            return segments("❌ 用法: /paper writing del <章节> <序号>")
        section = parsed.get(1)
        try:
            index = int(parsed.get(2)) - 1
        except ValueError:
            return segments("❌ 序号必须是数字")

        if await _run_storage(storage.delete_writing_idea, section, index):
            return segments(f"✅ 已删除「{section}」中的灵感")
        return segments(f"❌ 删除失败，请检查章节和序号")

    if len(parsed) == 1:
        section = parsed.first
        ideas = await _run_storage(storage.get_writing_ideas, section)
        if not ideas:
            return segments(f"💡 「{section}」暂无灵感")

        lines = [f"💡 「{section}」灵感箱 ({len(ideas)} 条):\n"]
        for i, idea in enumerate(ideas, 1):
            content = idea.get("content", "")
            time = idea.get("time", "")
            lines.append(f"  {i}. {content}")
            if time:
                lines.append(f"     📅 {time}")

        return segments("\n".join(lines))

    section = parsed.first
    content = parsed.rest(1)

    if not content.strip():
        return segments("❌ 灵感内容不能为空")

    if await _run_storage(storage.add_writing_idea, section, content, user_id):
        ideas = await _run_storage(storage.get_writing_ideas, section)
        return segments(f"💡 已添加到「{section}」灵感箱 (#{len(ideas)})")
    return segments("❌ 添加灵感失败")

async def cmd_topics(
    storage: PaperStorage,
    args: str
) -> list[dict[str, Any]]:
    parsed = parse(args)

    if not parsed:
        topics = await _run_storage(storage.get_topics)
        if not topics:
            return segments("🏷️ 暂无研究兴趣关键词\n\n提示: 使用 '/paper topics add <关键词>' 添加")

        lines = ["🏷️ 研究兴趣关键词:\n"]
        for i, topic in enumerate(topics, 1):
            lines.append(f"  {i}. {topic}")

        return segments("\n".join(lines))

    first = parsed.first.lower()

    if first == "add" or first == "添加":
        if len(parsed) < 2:
            return segments("❌ 用法: /paper topics add <关键词>")
        keyword = parsed.rest(1)
        if await _run_storage(storage.add_topic, keyword):
            return segments(f"✅ 已添加关键词: {keyword}")
        return segments(f"⚠️ 关键词 '{keyword}' 已存在")

    if first == "remove" or first == "rm" or first == "删除":
        if len(parsed) < 2:
            return segments("❌ 用法: /paper topics remove <关键词>")
        keyword = parsed.rest(1)
        if await _run_storage(storage.remove_topic, keyword):
            return segments(f"✅ 已删除关键词: {keyword}")
        return segments(f"❌ 关键词 '{keyword}' 不存在")

    if first == "clear" or first == "清空":
        if await _run_storage(storage.clear_topics):
            return segments("✅ 已清空所有关键词")
        return segments("❌ 清空失败")

    return segments("❌ 未知命令\n用法: /paper topics [add/remove/clear] [关键词]")

async def cmd_deadline(
    storage: PaperStorage,
    args: str,
    user_id: int
) -> list[dict[str, Any]]:
    parsed = parse(args)

    if not parsed:
        deadlines = await _run_storage(storage.get_deadlines)
        if not deadlines:
            return segments("📅 暂无截稿日期\n\n提示: 使用 '/paper deadline add <名称> <日期>' 添加")

        lines = ["📅 截稿日期提醒:\n"]
        for i, dl in enumerate(deadlines, 1):
            name = dl.get("name", "")
            date = dl.get("date", "")
            lines.append(f"  {i}. {name} - {date}")

        return segments("\n".join(lines))

    first = parsed.first.lower()

    if first == "add" or first == "添加":
        if len(parsed) < 3:
            return segments("❌ 用法: /paper deadline add <名称> <日期 (YYYY-MM-DD)>")
        name = parsed.get(1)
        date = parsed.rest(2)
        
        # Validate date format
        try:
            datetime.strptime(date, DATE_FORMAT)
        except ValueError:
            return segments("❌ 日期格式错误，请使用 YYYY-MM-DD 格式（如：2026-03-15）")
        
        if await _run_storage(storage.add_deadline, name, date, user_id):
            return segments(f"✅ 已添加截稿日期: {name} - {date}")
        return segments("❌ 添加失败")

    if first == "del" or first == "delete" or first == "删除":
        if len(parsed) < 2:
            return segments("❌ 用法: /paper deadline del <序号>")
        try:
            index = int(parsed.get(1)) - 1
        except ValueError:
            return segments("❌ 序号必须是数字")

        if await _run_storage(storage.delete_deadline, index):
            return segments("✅ 已删除截稿日期")
        return segments("❌ 删除失败，请检查序号")

    return segments("❌ 未知命令\n用法: /paper deadline [add/del] [参数]")
