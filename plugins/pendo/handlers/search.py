"""
搜索处理器
处理全文搜索和高级筛选
"""
from typing import Any
from datetime import datetime
import re
import logging
from core.plugin_base import run_sync
from ..utils.time_utils import parse_search_date_range
from ..utils.error_handlers import handle_command_errors
from ..utils.formatters import ItemFormatter, TYPE_NAMES, parse_remind_times

logger = logging.getLogger(__name__)

class SearchHandler:
    """搜索处理器
    
    负责处理全文搜索和高级筛选功能，包括：
    - 全文检索（使用SQLite FTS5）
    - 按类型、时间范围、标签等筛选
    - 结果排序和分组
    - 搜索结果格式化
    
    Attributes:
        db: 数据库服务实例
    """
    
    def __init__(self, db):
        self.db = db
    
    @handle_command_errors
    async def search(self, user_id: str, args: str, context: dict) -> dict[str, Any]:
        """
        搜索条目
        支持: 关键词搜索 + 筛选条件
        例如: /pendo search 会议 type=event range=last7d
        """
        if not args or not args.strip():
            return {
                'status': 'error',
                'message': '❌ 请提供搜索关键词\n\n用法:\n/pendo search <关键词>\n/pendo search <关键词> type=<类型> range=<时间范围>'
            }
        
        # 解析查询和过滤条件
        query, filters = self._parse_search_query(args)
        
        if not query:
            return {
                'status': 'error',
                'message': '❌ 请提供搜索关键词'
            }
        
        # 执行搜索
        results = await run_sync(self.db.items.search_items, user_id, query, filters)
        
        if not results:
            return {
                'status': 'success',
                'message': f'🔍 没有找到包含 "{query}" 的结果'
            }
        
        # 格式化输出
        message = self._format_search_results(results, query)
        
        # 显示筛选条件
        if filters:
            message += "\n\n**筛选条件:**\n"
            if filters.get('type'):
                message += f"• 类型: {TYPE_NAMES.get(filters['type'], filters['type'])}\n"
            if filters.get('start_date') or filters.get('end_date'):
                message += f"• 时间: {filters.get('start_date', '')} - {filters.get('end_date', '')}\n"
        
        message += (
            "\n💡 查看/操作示例: "
            "/pendo note view <id> | /pendo diary view <date> | "
            "/pendo event edit <id> <内容> | /pendo todo done <id>"
        )
        
        return {
            'status': 'success',
            'message': message
        }
    
    def _parse_search_query(self, args: str) -> tuple[str, dict[str, Any]]:
        """
        解析搜索查询和过滤条件
        返回: (query, filters)
        """
        filters = {}
        
        # 提取 type= 参数
        type_match = re.search(r'type=(\w+)', args)
        if type_match:
            filters['type'] = type_match.group(1)
            args = args.replace(type_match.group(0), '').strip()
        
        # 提取 range= 参数
        range_match = re.search(r'range=([^\s]+)', args)
        if range_match:
            range_str = range_match.group(1)
            start_date, end_date = parse_search_date_range(range_str)
            if start_date:
                filters['start_date'] = start_date
            if end_date:
                filters['end_date'] = end_date
            args = args.replace(range_match.group(0), '').strip()
        
        # 提取 status= 参数
        status_match = re.search(r'status=(\w+)', args)
        if status_match:
            filters['status'] = status_match.group(1)
            args = args.replace(status_match.group(0), '').strip()
        
        # 提取 category= 参数
        category_match = re.search(r'category=([^\s]+)', args)
        if category_match:
            filters['category'] = category_match.group(1)
            args = args.replace(category_match.group(0), '').strip()
        
        # 剩余的作为搜索关键词
        query = args.strip()
        
        return query, filters
    
    def _format_search_results(self, results: list, query: str) -> str:
        """格式化搜索结果

        显示更多信息：
        - 类型图标
        - 标题
        - 时间（日程的start_time，待办的due_time，日记的diary_date）
        - 提醒信息
        - 分类和标签
        """
        if not results:
            return f"🔍 搜索 \"{query}\"\n\n未找到相关内容"

        parts = [f"🔍 搜索 \"{query}\"", f"找到 {len(results)} 条结果", ""]

        for i, item in enumerate(results[:10], 1):
            item_type = item.type.value if hasattr(item.type, 'value') else item.type
            icon = ItemFormatter.format_type_icon(item_type)
            title = item.title
            if not title:
                title = ItemFormatter.truncate_content(item.content or '', 30, '...')
            item_id = item.id or ''

            # 基本行
            parts.append(f"{i}. {icon} {title}")

            # 时间信息
            time_info = self._get_item_time_info(item)
            if time_info:
                parts.append(f"   {time_info}")

            # 提醒信息（日程）
            if item_type == 'event' and item.remind_times:
                remind_times = parse_remind_times(item.remind_times)
                if remind_times:
                    parts.append(f"   🔔 {len(remind_times)} 个提醒")

            # 分类和标签
            meta_parts = []
            if item.category:
                meta_parts.append(f"📂 {item.category}")
            if item.tags:
                tags_str = ItemFormatter.format_tags(item.tags[:2])
                meta_parts.append(f"🏷️ {tags_str}")
            if meta_parts:
                parts.append(f"   {' | '.join(meta_parts)}")

            parts.append(f"   `{item_id}`")
            parts.append("")

        if len(results) > 10:
            parts.append(f"...还有 {len(results) - 10} 条结果")

        return "\n".join(parts)

    def _get_item_time_info(self, item) -> str:
        """获取条目的时间信息"""
        item_type = item.type.value if hasattr(item.type, 'value') else item.type

        if item_type == 'event' and item.start_time:
            dt_str = ItemFormatter.format_datetime(item.start_time)
            return f"🗓️ {dt_str}"

        if item_type == 'task' and item.due_time:
            dt_str = ItemFormatter.format_datetime(item.due_time)
            return f"⏱ {dt_str}"

        if item_type == 'diary' and item.diary_date:
            return f"📔 {item.diary_date}"

        return ""
    