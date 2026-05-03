"""
搜索处理器
处理全文搜索和高级筛选
"""

import logging
import re
from collections import defaultdict
from typing import TYPE_CHECKING, Any, cast

from core.plugin_base import run_sync

from ..config import PendoConfig
from ..core.types import CommandMessage, PendoContext
from ..models.item import (
    DiaryItem,
    EventItem,
    LedgerItem,
    NoteItem,
    TaskItem,
    get_item_type_value,
)
from ..utils.error_handlers import handle_command_errors
from ..utils.formatters import (
    BOUNDARY_TAG_TOKEN_RE,
    PRIORITY_ICONS,
    STATUS_ICONS,
    TYPE_NAMES,
    ItemFormatter,
    extract_kv_param,
)
from ..utils.time_utils import parse_remind_times, parse_search_date_range

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ..services.db import Database

SearchItem = EventItem | TaskItem | DiaryItem | NoteItem | LedgerItem
_ALLOWED_SEARCH_TYPES = {"event", "task", "note", "diary", "ledger"}
_ALLOWED_TASK_STATUS = {"open", "done", "cancelled"}
_ALLOWED_TRANSACTION_TYPES = {"income", "expense", "transfer"}

# 操作提示：根据类型提供对应的操作命令
_TYPE_ACTION_HINTS: dict[str, str] = {
    "event": "/pendo event view <id>",
    "task": "/pendo todo view <id>",
    "note": "/pendo note view <id>",
    "diary": "/pendo diary view <日期或ID>",
    "ledger": "/pendo ledger view <id>",
}


class SearchHandler:
    """搜索处理器

    负责处理全文搜索和高级筛选功能，包括：
    - 全文检索（使用SQLite FTS5）
    - 按类型、时间范围、标签等筛选
    - 结果按类型分组显示
    - 搜索结果格式化

    Attributes:
        db: 数据库服务实例
    """

    def __init__(self, db: "Database"):
        self.db = db

    @handle_command_errors
    async def search(self, user_id: str, args: str, context: PendoContext) -> CommandMessage:
        """
        搜索条目
        支持: 关键词搜索 + 筛选条件
        例如: /pendo search 会议 type=event range=last7d
        """
        if not args or not args.strip():
            return {
                "status": "error",
                "message": (
                    "❌ 请提供搜索关键词\n\n"
                    "用法:\n"
                    "/pendo search <关键词>\n"
                    "/pendo search <关键词> type=<类型> range=<时间范围>\n\n"
                    "可用筛选:\n"
                    "• type=event/task/note/diary/ledger\n"
                    "• range=today/week/month/year/last7d/last30d/YYYY-MM/start..end\n"
                    "• #标签 或 tag=<标签>\n"
                    "• status=open/done/cancelled (待办)\n"
                    "• category=<分类> (type=ledger 时按账目分类)\n"
                    "• transaction_type=income/expense/transfer (记账)"
                ),
            }

        # 解析查询和过滤条件
        try:
            query, filters = self._parse_search_query(args)
        except ValueError as exc:
            return {"status": "error", "message": f"❌ {str(exc)}"}

        if not query:
            return {"status": "error", "message": "❌ 请提供搜索关键词"}

        # 执行搜索
        results = cast(
            list[SearchItem], await run_sync(self.db.items.search_items, user_id, query, filters)
        )

        if not results:
            filter_hint = ""
            if filters:
                filter_hint = "\n💡 试试去掉筛选条件扩大搜索范围"
            return {"status": "success", "message": f'🔍 没有找到包含 "{query}" 的结果{filter_hint}'}

        # 格式化输出
        message = self._format_search_results(results, query, filters)

        return {"status": "success", "message": message}

    def _parse_search_query(self, args: str) -> tuple[str, dict[str, Any]]:
        """解析搜索查询和过滤条件，返回 (query, filters)"""
        filters: dict[str, Any] = {}

        type_val, args = extract_kv_param(args, "type")
        if type_val:
            if type_val not in _ALLOWED_SEARCH_TYPES:
                raise ValueError(f"无效类型: {type_val}")
            filters["type"] = type_val

        status_val, args = extract_kv_param(args, "status")
        if status_val:
            if status_val not in _ALLOWED_TASK_STATUS:
                raise ValueError(f"无效待办状态: {status_val}")
            filters["status"] = status_val

        category_val, args = extract_kv_param(args, "category")
        if category_val:
            if filters.get("type") == "ledger":
                filters["ledger_category"] = category_val
            else:
                filters["category"] = category_val

        transaction_val, args = extract_kv_param(args, "transaction_type")
        if transaction_val:
            if transaction_val not in _ALLOWED_TRANSACTION_TYPES:
                raise ValueError(f"无效交易类型: {transaction_val}")
            filters["transaction_type"] = transaction_val

        for key, filter_key in [
            ("account", "account_name"),
            ("merchant", "merchant"),
        ]:
            val, args = extract_kv_param(args, key)
            if val:
                filters[filter_key] = val

        for key in ("tag", "tags"):
            tag_val, args = extract_kv_param(args, key)
            if tag_val:
                filters["tags"] = tag_val

        tag_match = BOUNDARY_TAG_TOKEN_RE.search(args)
        if tag_match:
            filters["tags"] = tag_match.group(1)
            args = (args[: tag_match.start()] + args[tag_match.end() :]).strip()

        range_val, args = extract_kv_param(args, "range")
        if range_val:
            start_date, end_date = parse_search_date_range(range_val, strict=True)
            if start_date:
                date_field = self._resolve_search_date_field(filters.get("type"))
                filters["date_field"] = date_field
                if self._is_date_only_search_field(date_field):
                    start_date = start_date[:10]
                    if end_date:
                        end_date = end_date[:10]
                filters["start_date"] = start_date
            if end_date:
                filters["end_date"] = end_date

        return args.strip(), filters

    @staticmethod
    def _resolve_search_date_field(item_type: str | None) -> str:
        mapping = {
            "event": "start_time",
            "task": "plan_date",
            "diary": "diary_date",
            "ledger": "ledger_date",
            "note": "created_at",
        }
        return mapping.get(item_type or "", "created_at")

    @staticmethod
    def _is_date_only_search_field(date_field: str) -> bool:
        return date_field in {"plan_date", "diary_date", "ledger_date"}

    def _format_search_results(
        self, results: list[SearchItem], query: str, filters: dict[str, Any],
    ) -> str:
        """格式化搜索结果，按类型分组显示"""
        if not results:
            return f'🔍 搜索 "{query}"\n\n未找到相关内容'

        # 按类型分组
        grouped: dict[str, list[SearchItem]] = defaultdict(list)
        for item in results:
            item_type = get_item_type_value(item.type, default=str(item.type))
            grouped[item_type].append(item)

        # 构建输出
        parts: list[str] = [
            "🔎 搜索结果",
            f"关键词: {query}",
            f"命中: {len(results)} 条",
        ]

        # 显示筛选条件（紧凑行）
        filter_parts = self._format_filter_summary(filters)
        if filter_parts:
            parts.append(f"筛选: {filter_parts}")

        parts.append("━━━━━━━━━━━━━━━━━━")
        parts.append("")

        # 类型显示顺序
        type_order = ["event", "task", "ledger", "diary", "note"]
        # 如果只有一个类型，不显示分组标题
        single_type = len(grouped) == 1

        total_shown = 0
        max_display = 15  # 最多显示15条

        for item_type in type_order:
            items = grouped.get(item_type)
            if not items:
                continue

            if not single_type:
                type_name = TYPE_NAMES.get(item_type, item_type)
                parts.append(f"【{type_name}】{len(items)} 条")

            for item in items:
                if total_shown >= max_display:
                    break
                parts.append(self._format_item_line(item, query))
                total_shown += 1

            if total_shown >= max_display:
                break
            parts.append("")

        remaining = len(results) - total_shown
        if remaining > 0:
            parts.append(f"...还有 {remaining} 条结果，请添加筛选条件缩小范围")

        # 操作提示：只列出结果中出现的类型
        hint_types = [t for t in type_order if t in grouped]
        hints = [_TYPE_ACTION_HINTS[t] for t in hint_types if t in _TYPE_ACTION_HINTS]
        if hints:
            parts.append("")
            parts.append(f"💡 操作: {' | '.join(hints)}")

        return "\n".join(parts)

    def _format_item_line(self, item: SearchItem, query: str) -> str:
        """格式化单条搜索结果为紧凑的多行块"""
        item_type = get_item_type_value(item.type, default=str(item.type))
        icon = ItemFormatter.format_type_icon(item_type)
        title = item.title
        if isinstance(item, EventItem) and getattr(item, "event_collection_id", None):
            try:
                collection = self.db.items.get_event_collection(
                    item.event_collection_id,
                    item.owner_id,
                )
            except Exception:
                collection = None
            if collection:
                title = f"{collection.get('title') or '无标题'} · {title or '无标题'}"
        if not title:
            title = ItemFormatter.truncate_content(item.content or "", 40, "...")
        item_id = item.id or ""

        lines: list[str] = []

        # === 第一行: 图标 + 标题 + 类型特有摘要 ===
        main_line = f"• {icon} {title}"

        # 记账: 在标题行追加金额
        if isinstance(item, LedgerItem):
            tx_type = getattr(item, "transaction_type", "expense")
            sign = "+" if tx_type == "income" else ("↔ " if tx_type == "transfer" else "-")
            main_line += f"  {sign}¥{item.amount:.2f}"

        # 待办: 在标题行追加状态
        if isinstance(item, TaskItem):
            status_icon = STATUS_ICONS.get(
                item.status.value if hasattr(item.status, "value") else str(item.status), "⬜"
            )
            main_line += f" {status_icon}"
            if item.priority and item.priority <= 2:
                main_line += f" {PRIORITY_ICONS.get(item.priority, '')}"

        lines.append(main_line)

        # === 第二行: 时间 + 元数据 ===
        detail_parts: list[str] = []

        # 时间信息
        time_info = self._get_item_time_info(item)
        if time_info:
            detail_parts.append(time_info)

        # 记账分类
        if isinstance(item, LedgerItem) and item.ledger_category:
            detail_parts.append(f"📂{item.ledger_category}")
        if isinstance(item, LedgerItem) and getattr(item, "account_name", ""):
            account = item.account_name
            counter = getattr(item, "counter_account_name", "") or ""
            detail_parts.append(f"🏦{account + '→' + counter if counter else account}")
        if isinstance(item, LedgerItem) and getattr(item, "merchant", ""):
            detail_parts.append(f"🏷️{item.merchant}")

        # 通用分类和标签（非记账类型）
        if not isinstance(item, LedgerItem):
            if item.category and item.category != "未分类":
                detail_parts.append(f"📂{item.category}")
            if item.tags:
                tags_str = ItemFormatter.format_tags(item.tags[:2])
                detail_parts.append(f"🏷️{tags_str}")

        # 提醒信息（日程）
        if isinstance(item, EventItem) and item.remind_times:
            remind_times = parse_remind_times(item.remind_times)
            if remind_times:
                detail_parts.append(f"🔔{len(remind_times)}个提醒")

        # 内容预览（当标题和内容不同时，显示内容片段）
        preview = self._get_content_preview(item, query)
        if preview:
            detail_parts.append(f"「{preview}」")

        if detail_parts:
            lines.append(f"  {' | '.join(detail_parts)}")

        # ID 行
        lines.append(f"  ID: `{item_id}`")

        return "\n".join(lines)

    def _get_content_preview(self, item: SearchItem, query: str) -> str:
        """提取包含关键词的内容片段预览"""
        content = item.content or ""
        # 如果没有内容或内容与标题相同，跳过
        if not content or content == item.title:
            return ""

        # 记账条目用 remark 作为预览来源
        if isinstance(item, LedgerItem):
            content = item.remark or ""
            if not content:
                return ""

        preview_len = PendoConfig.SEARCH_CONTENT_PREVIEW_LENGTH

        # 尝试找到关键词所在位置，截取上下文
        query_lower = query.lower()
        content_lower = content.lower()
        idx = content_lower.find(query_lower)
        if idx >= 0:
            # 以关键词为中心截取
            start = max(0, idx - 10)
            end = min(len(content), idx + len(query) + preview_len - 10)
            snippet = content[start:end]
            if start > 0:
                snippet = "..." + snippet
            if end < len(content):
                snippet = snippet + "..."
            return snippet

        # 关键词不在 content 里（可能在 title/tags 匹配），截取开头
        return ItemFormatter.truncate_content(content, preview_len, "...")

    def _get_item_time_info(self, item: SearchItem) -> str:
        """获取条目的时间信息"""
        if isinstance(item, EventItem) and item.start_time:
            dt_str = ItemFormatter.format_datetime(item.start_time)
            return f"🗓️{dt_str}"

        if isinstance(item, TaskItem):
            if item.plan_date:
                return f"📅{item.plan_date}"
            if item.deadline_at:
                dt_str = ItemFormatter.format_datetime(item.deadline_at)
                return f"⏱{dt_str}"

        if isinstance(item, DiaryItem):
            if item.entry_time:
                return f"📔{item.entry_time[:16].replace('T', ' ')}"
            if item.diary_date:
                return f"📔{item.diary_date}"

        if isinstance(item, LedgerItem) and item.ledger_date:
            return f"📅{item.ledger_date}"

        return ""

    def _format_filter_summary(self, filters: dict[str, Any]) -> str:
        """格式化筛选条件为紧凑的一行"""
        parts: list[str] = []
        if filters.get("type"):
            parts.append(f"类型={TYPE_NAMES.get(filters['type'], filters['type'])}")
        if filters.get("status"):
            parts.append(f"状态={filters['status']}")
        if filters.get("category"):
            parts.append(f"分类={filters['category']}")
        if filters.get("ledger_category"):
            parts.append(f"分类={filters['ledger_category']}")
        if filters.get("tags"):
            parts.append(f"标签=#{filters['tags']}")
        if filters.get("transaction_type"):
            transaction_type = filters["transaction_type"]
            label = {"income": "收入", "expense": "支出", "transfer": "转账"}.get(transaction_type, transaction_type)
            parts.append(f"交易={label}")
        if filters.get("account_name"):
            parts.append(f"账户={filters['account_name']}")
        if filters.get("merchant"):
            parts.append(f"商户={filters['merchant']}")
        if filters.get("start_date") or filters.get("end_date"):
            parts.append(f"时间={filters.get('start_date', '')}~{filters.get('end_date', '')}")
        return " | ".join(parts)
