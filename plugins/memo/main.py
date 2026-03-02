"""
笔记管理插件

支持分类存储、查看、删除、搜索笔记。

命令格式:
    memo                      - 列出所有分类
    memo <分类>               - 查看指定分类的笔记
    memo <分类> <内容>        - 添加笔记到指定分类
    memo del <分类> <序号>    - 删除指定笔记
    memo del <分类>           - 删除整个分类
    memo search <关键词>      - 搜索笔记
    memo clear                - 清空所有笔记（管理员）
    memo export               - 导出所有笔记
"""

import json
import logging
from datetime import datetime

from core.plugin_base import segments
from core.args import parse

logger = logging.getLogger(__name__)


def init(context=None) -> None:
    """插件初始化"""
    logger.info("Memo plugin initialized")


# ============================================================
# 数据结构
# ============================================================
# {
#     "category": [
#         {"content": "笔记内容", "time": "2024-01-01 12:00", "user": 123456}
#     ]
# }


# ============================================================
# 数据管理
# ============================================================

def _memo_path(context):
    """获取笔记文件路径"""
    return context.data_dir / "memo.json"


def _load_memo(context) -> dict:
    """加载笔记数据"""
    path = _memo_path(context)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        # 兼容旧格式（纯字符串列表）
        for cat, notes in data.items():
            if notes and isinstance(notes[0], str):
                data[cat] = [{"content": n, "time": "", "user": 0} for n in notes]
        return data
    except Exception:
        return {}


def _save_memo(context, data: dict) -> None:
    """保存笔记数据"""
    path = _memo_path(context)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=4), encoding="utf-8")


def _get_user_id(event: dict) -> int:
    """获取用户 ID"""
    return event.get("user_id", 0)


def _now_str() -> str:
    """获取当前时间字符串"""
    return datetime.now().strftime("%Y-%m-%d %H:%M")


# ============================================================
# 主处理函数
# ============================================================

async def handle(command: str, args: str, event: dict, context) -> list:
    """命令处理入口"""
    try:
        parsed = parse(args)
        
        # 无参数：列出所有分类
        if not parsed:
            return _list_categories(context)
        
        first = parsed.first.lower()
        
        # 子命令路由
        if first in {"help", "帮助", "?"}:
            return segments(_show_help())
        
        if first in {"del", "delete", "删除"}:
            return _handle_delete(parsed, context)
        
        if first in {"search", "搜索"}:
            keyword = parsed.rest(1)
            if not keyword:
                return segments("❌ 请提供搜索关键词\n用法: /memo search <关键词>")
            return _search_notes(keyword, context)
        
        if first in {"clear", "清空"}:
            if not context.secrets.get("admin_user_ids") or \
               _get_user_id(event) not in context.secrets.get("admin_user_ids", []):
                return segments("❌ 仅管理员可执行此操作")
            return _clear_all(context)
        
        if first in {"export", "导出"}:
            return _export_all(context)
        
        # 一个参数：查看指定分类
        if len(parsed) == 1:
            return _view_category(parsed.first, context)
        
        # 两个或更多参数：添加笔记
        category = parsed.first
        content = parsed.rest(1)
        return _add_note(category, content, event, context)
        
    except Exception as e:
        logger.exception("Memo handle error: %s", e)
        return segments(f"处理请求时出错: {str(e)}")


# ============================================================
# 子命令处理
# ============================================================

def _handle_delete(parsed, context) -> list[dict]:
    """处理删除命令"""
    if len(parsed) < 2:
        return segments("❌ 用法:\n  memo del <分类> - 删除整个分类\n  memo del <分类> <序号> - 删除指定笔记")
    
    category = parsed.get(1)
    data = _load_memo(context)
    
    if category not in data:
        return segments(f"❌ 分类 '{category}' 不存在")
    
    # 删除整个分类
    if len(parsed) == 2:
        count = len(data[category])
        del data[category]
        _save_memo(context, data)
        logger.info("Memo category deleted: '%s' (%d notes)", category, count)
        return segments(f"✅ 已删除分类 '{category}'（共 {count} 条笔记）")
    
    # 删除指定笔记
    try:
        index = int(parsed.get(2)) - 1  # 用户输入从 1 开始
        notes = data[category]
        if index < 0 or index >= len(notes):
            return segments(f"❌ 序号无效，该分类共 {len(notes)} 条笔记")
        
        deleted = notes.pop(index)
        if not notes:
            del data[category]  # 如果分类为空，删除分类
        _save_memo(context, data)
        
        content = deleted.get("content", deleted) if isinstance(deleted, dict) else deleted
        preview = content[:30] + "..." if len(content) > 30 else content
        logger.info("Memo deleted from '%s': #%d", category, index + 1)
        return segments(f"✅ 已删除: {preview}")
    except ValueError:
        return segments("❌ 请输入有效的序号")


def _search_notes(keyword: str, context) -> list[dict]:
    """搜索笔记"""
    data = _load_memo(context)
    if not data:
        return segments("暂无笔记")
    
    results = []
    keyword_lower = keyword.lower()
    
    for category, notes in data.items():
        for i, note in enumerate(notes, 1):
            content = note.get("content", "") if isinstance(note, dict) else note
            if keyword_lower in content.lower() or keyword_lower in category.lower():
                preview = content[:50] + "..." if len(content) > 50 else content
                results.append(f"  [{category}#{i}] {preview}")
    
    if not results:
        logger.debug("Memo search: no results for '%s'", keyword)
        return segments(f"🔍 未找到包含 '{keyword}' 的笔记")
    
    lines = [f"🔍 搜索结果 ({len(results)} 条):"]
    lines.extend(results[:20])  # 最多显示 20 条
    if len(results) > 20:
        lines.append(f"  ... 还有 {len(results) - 20} 条结果")
    
    logger.info("Memo search: found %d results for '%s'", len(results), keyword)
    return segments("\n".join(lines))


def _clear_all(context) -> list[dict]:
    """清空所有笔记"""
    data = _load_memo(context)
    total = sum(len(notes) for notes in data.values())
    _save_memo(context, {})
    logger.warning("All memos cleared: %d notes deleted", total)
    return segments(f"✅ 已清空所有笔记（共 {total} 条）")


def _export_all(context) -> list[dict]:
    """导出所有笔记"""
    data = _load_memo(context)
    if not data:
        return segments("暂无笔记")
    
    lines = ["📚 笔记导出\n" + "=" * 30]
    for category, notes in sorted(data.items()):
        lines.append(f"\n【{category}】")
        for i, note in enumerate(notes, 1):
            if isinstance(note, dict):
                content = note.get("content", "")
                time = note.get("time", "")
                time_str = f" ({time})" if time else ""
                lines.append(f"  {i}. {content}{time_str}")
            else:
                lines.append(f"  {i}. {note}")
    
    total = sum(len(notes) for notes in data.values())
    lines.append(f"\n{'=' * 30}")
    lines.append(f"共 {len(data)} 个分类，{total} 条笔记")
    
    return segments("\n".join(lines))


def _show_help() -> str:
    """显示帮助信息"""
    return (
        "📝 笔记管理\n"
        "═══════════════════════\n\n"
        "📌 基本操作:\n\n"
        "1️⃣ /memo\n"
        "   列出所有分类\n\n"
        "2️⃣ /memo <分类>\n"
        "   查看指定分类的所有笔记\n\n"
        "3️⃣ /memo <分类> <内容>\n"
        "   添加笔记到指定分类\n\n"
        "🗑️ 删除操作:\n\n"
        "4️⃣ /memo del <分类>\n"
        "   删除整个分类\n\n"
        "5️⃣ /memo del <分类> <序号>\n"
        "   删除指定笔记\n\n"
        "🔧 其他功能:\n\n"
        "6️⃣ /memo search <关键词>\n"
        "   搜索包含关键词的笔记\n\n"
        "7️⃣ /memo export\n"
        "   导出所有笔记\n\n"
        "💡 示例:\n"
        "   /memo 待办 买菜\n"
        "   /memo 待办\n"
        "   /memo del 待办 1\n"
        "   /memo search 会议\n\n"
        "═══════════════════════"
    )


# ============================================================
# 基础功能
# ============================================================

def _list_categories(context) -> list[dict]:
    """列出所有分类"""
    data = _load_memo(context)
    if not data:
        return segments("暂无笔记\n\n提示: 使用 'memo <分类> <内容>' 添加第一条笔记")
    
    lines = ["📚 笔记分类:"]
    total = 0
    for category, notes in sorted(data.items()):
        count = len(notes)
        total += count
        # 显示最新一条的预览
        if notes:
            latest = notes[-1]
            content = latest.get("content", latest) if isinstance(latest, dict) else latest
            preview = content[:20] + "..." if len(content) > 20 else content
            lines.append(f"  • {category} ({count}) - {preview}")
        else:
            lines.append(f"  • {category} ({count})")
    
    lines.append(f"\n共 {total} 条笔记 | 输入 'memo help' 查看帮助")
    return segments("\n".join(lines))


def _view_category(category: str, context) -> list[dict]:
    """查看指定分类"""
    data = _load_memo(context)
    
    # 模糊匹配
    if category not in data:
        matches = [c for c in data.keys() if category.lower() in c.lower()]
        if len(matches) == 1:
            category = matches[0]
        elif len(matches) > 1:
            return segments(f"❓ 找到多个匹配的分类: {', '.join(matches)}")
        else:
            return segments(f"❌ 分类 '{category}' 不存在\n\n现有分类: {', '.join(data.keys()) or '无'}")
    
    notes = data[category]
    if not notes:
        return segments(f"分类 '{category}' 暂无笔记")
    
    lines = [f"📝 {category} ({len(notes)} 条):"]
    lines.append("-" * 20)
    
    for i, note in enumerate(notes, 1):
        if isinstance(note, dict):
            content = note.get("content", "")
            time = note.get("time", "")
            if time:
                lines.append(f"  {i}. {content}")
                lines.append(f"     📅 {time}")
            else:
                lines.append(f"  {i}. {content}")
        else:
            lines.append(f"  {i}. {note}")
    
    lines.append("-" * 20)
    lines.append("提示: 'memo del {} <序号>' 删除".format(category))
    
    return segments("\n".join(lines))


def _add_note(category: str, content: str, event: dict, context) -> list[dict]:
    """添加笔记"""
    if not content.strip():
        return segments("❌ 笔记内容不能为空")
    
    data = _load_memo(context)
    
    note = {
        "content": content.strip(),
        "time": _now_str(),
        "user": _get_user_id(event)
    }
    
    data.setdefault(category, []).append(note)
    _save_memo(context, data)
    
    count = len(data[category])
    logger.info("Memo added to category '%s': #%d", category, count)
    return segments(f"✅ 已添加到 '{category}' (#{count})")
