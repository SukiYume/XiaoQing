"""处理 Pendo 全文检索、高级筛选与紧凑结果展示。"""

from __future__ import annotations

import re
import shlex
from collections import defaultdict
from datetime import datetime, tzinfo
from typing import TYPE_CHECKING, Any, Final, cast

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
    PRIORITY_ICONS,
    STATUS_ICONS,
    TAG_TOKEN_RE,
    TYPE_NAMES,
    UNSAFE_CONTROL_RE,
    ItemFormatter,
    ledger_amount_yuan,
    single_line_text,
)
from ..utils.time_utils import (
    TimezoneHelper,
    get_user_local_wall_time,
    parse_remind_times,
    parse_search_date_range,
)
from ..utils.validators import sanitize_search_keyword, validate_tag

if TYPE_CHECKING:
    from ..services.db import Database

SearchItem = EventItem | TaskItem | DiaryItem | NoteItem | LedgerItem
SearchFilters = dict[str, Any]

_ALLOWED_SEARCH_TYPES: Final = frozenset({"event", "task", "note", "diary", "ledger"})
_ALLOWED_TASK_STATUS: Final = frozenset({"open", "done", "cancelled"})
_ALLOWED_TRANSACTION_TYPES: Final = frozenset({"income", "expense", "transfer"})
_SEARCH_TYPE_ORDER: Final = ("event", "task", "ledger", "diary", "note")
_SEARCH_RESULT_LIMIT: Final = 15
_SEARCH_INPUT_MAX_CHARS: Final = 2_000
_SEARCH_QUERY_MAX_CHARS: Final = 100
_SEARCH_TITLE_MAX_CHARS: Final = 60
_FILTER_TOKEN_RE: Final = re.compile(
    r"^(type|status|category|transaction_type|account|merchant|tag|tags|range)[=:](.*)$",
    re.IGNORECASE,
)
_FILTER_KEY_ALIASES: Final = {
    "account": "account_name",
    "tag": "tags",
}
_LEDGER_FILTER_KEYS: Final = frozenset({"transaction_type", "account_name", "merchant"})
_DATE_FIELD_BY_TYPE: Final = {
    "event": "start_time",
    "task": "plan_date",
    "diary": "diary_date",
    "ledger": "ledger_date",
    "note": "created_at",
}
_DATE_ONLY_SEARCH_FIELDS: Final = frozenset({"plan_date", "diary_date", "ledger_date"})
_TYPE_ACTION_HINTS: Final[dict[str, str]] = {
    "event": "/pendo event view <id>",
    "task": "/pendo todo view <id>",
    "note": "/pendo note view <id>",
    "diary": "/pendo diary view <日期或ID>",
    "ledger": "/pendo ledger view <id>",
}
_TRANSACTION_LABELS: Final = {"income": "收入", "expense": "支出", "transfer": "转账"}
_TRANSACTION_SIGNS: Final = {"income": "+", "expense": "-", "transfer": "↔"}


class SearchHandler:
    """解析搜索条件，通过数据库分页检索，并按条目类型展示结果。"""

    def __init__(self, db: Database):
        self.db = db

    @handle_command_errors
    async def search(self, user_id: str, args: str, context: PendoContext) -> CommandMessage:
        """搜索条目，例如 ``/pendo search 会议 type=event range=last7d``。"""
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

        try:
            query, filters = self._parse_search_query(
                args,
                await get_user_local_wall_time(user_id, self.db),
            )
        except ValueError as exc:
            return {"status": "error", "message": f"❌ {exc}"}

        raw_results, total = await run_sync(
            self.db.search_items_page,
            user_id,
            query,
            filters,
            limit=_SEARCH_RESULT_LIMIT,
            offset=0,
        )
        results = cast(list[SearchItem], raw_results)

        if not results:
            filter_hint = "\n💡 试试去掉筛选条件扩大搜索范围" if filters else ""
            return {
                "status": "success",
                "message": f'🔍 没有找到包含 "{query}" 的结果{filter_hint}',
            }

        collection_titles = await self._load_event_collection_titles(user_id, results)
        display_timezone = await run_sync(
            TimezoneHelper.get_user_timezone,
            user_id,
            self.db,
        )
        message = self._format_search_results(
            results,
            total,
            query,
            filters,
            collection_titles,
            display_timezone,
        )
        return {"status": "success", "message": message}

    @staticmethod
    def _parse_filter_token(token: str) -> tuple[str, str] | None:
        """识别一个筛选 token；普通关键词返回 ``None``。"""
        if token.startswith("#"):
            tag_match = TAG_TOKEN_RE.fullmatch(token)
            if not tag_match:
                raise ValueError(f"无效标签: {token}")
            return "tags", tag_match.group(1)

        filter_match = _FILTER_TOKEN_RE.fullmatch(token)
        if not filter_match:
            return None
        raw_key, raw_value = filter_match.groups()
        key = _FILTER_KEY_ALIASES.get(raw_key.casefold(), raw_key.casefold())
        value = raw_value.strip()
        if not value:
            raise ValueError(f"{raw_key} 筛选值不能为空")
        return key, value

    @classmethod
    def _tokenize_search_args(cls, args: str) -> tuple[list[str], dict[str, str]]:
        """一次分词，分离关键词与筛选条件，并拒绝重复条件。"""
        if len(args) > _SEARCH_INPUT_MAX_CHARS:
            raise ValueError(f"搜索参数不能超过 {_SEARCH_INPUT_MAX_CHARS} 字")
        if UNSAFE_CONTROL_RE.search(args):
            raise ValueError("搜索参数包含不允许的控制字符")
        try:
            tokens = shlex.split(args, comments=False, posix=True)
        except ValueError as exc:
            raise ValueError("搜索参数中的引号未闭合") from exc

        query_tokens: list[str] = []
        raw_filters: dict[str, str] = {}
        for token in tokens:
            parsed = cls._parse_filter_token(token)
            if parsed is None:
                query_tokens.append(token)
                continue
            key, value = parsed
            if key in raw_filters:
                raise ValueError(f"筛选条件不能重复: {key}")
            raw_filters[key] = value
        return query_tokens, raw_filters

    @staticmethod
    def _infer_filter_type(raw_filters: dict[str, str]) -> str | None:
        """验证枚举筛选，并从专属字段推断待办或账目类型。"""
        item_type = raw_filters.get("type")
        if item_type:
            item_type = item_type.casefold()
            if item_type not in _ALLOWED_SEARCH_TYPES:
                raise ValueError(f"无效类型: {item_type}")

        status = raw_filters.get("status")
        if status and status.casefold() not in _ALLOWED_TASK_STATUS:
            raise ValueError(f"无效待办状态: {status}")
        transaction_type = raw_filters.get("transaction_type")
        if transaction_type and transaction_type.casefold() not in _ALLOWED_TRANSACTION_TYPES:
            raise ValueError(f"无效交易类型: {transaction_type}")

        has_ledger_filter = bool(_LEDGER_FILTER_KEYS.intersection(raw_filters))
        if status and has_ledger_filter:
            raise ValueError("待办状态不能与账目专属筛选同时使用")
        inferred_type = "task" if status else ("ledger" if has_ledger_filter else None)
        if item_type and inferred_type and item_type != inferred_type:
            raise ValueError(f"{item_type} 类型不能使用 {inferred_type} 专属筛选")
        return item_type or inferred_type

    @staticmethod
    def _bounded_filter_value(value: str, label: str, max_length: int) -> str:
        """校验用于精确匹配的文本筛选，禁止静默截断。"""
        normalized = value.strip()
        if UNSAFE_CONTROL_RE.search(normalized):
            raise ValueError(f"{label}包含不允许的控制字符")
        if len(normalized) > max_length:
            raise ValueError(f"{label}不能超过 {max_length} 字")
        return normalized

    @staticmethod
    def _apply_date_filter(
        filters: SearchFilters,
        range_value: str,
        user_now: datetime,
    ) -> None:
        start_date, end_date = parse_search_date_range(range_value, now=user_now, strict=True)
        date_field = _DATE_FIELD_BY_TYPE.get(str(filters.get("type") or ""), "created_at")
        filters["date_field"] = date_field
        if date_field in _DATE_ONLY_SEARCH_FIELDS:
            start_date = start_date[:10] if start_date else None
            end_date = end_date[:10] if end_date else None
        if start_date:
            filters["start_date"] = start_date
        if end_date:
            filters["end_date"] = end_date

    @classmethod
    def _normalize_search_filters(
        cls,
        raw_filters: dict[str, str],
        user_now: datetime,
    ) -> SearchFilters:
        filters: SearchFilters = {}
        item_type = cls._infer_filter_type(raw_filters)
        if item_type:
            filters["type"] = item_type
        if status := raw_filters.get("status"):
            filters["status"] = status.casefold()
        if category := raw_filters.get("category"):
            category_key = "ledger_category" if item_type == "ledger" else "category"
            category_limit = 60 if item_type == "ledger" else 50
            filters[category_key] = cls._bounded_filter_value(category, "分类筛选", category_limit)
        if transaction_type := raw_filters.get("transaction_type"):
            filters["transaction_type"] = transaction_type.casefold()
        for key, label, limit in (
            ("account_name", "账户筛选", 80),
            ("merchant", "商户筛选", 120),
        ):
            if value := raw_filters.get(key):
                filters[key] = cls._bounded_filter_value(value, label, limit)
        if tag := raw_filters.get("tags"):
            bounded_tag = cls._bounded_filter_value(tag, "标签筛选", 20)
            filters["tags"] = validate_tag(bounded_tag)
        if range_value := raw_filters.get("range"):
            cls._apply_date_filter(filters, range_value, user_now)
        return filters

    @classmethod
    def _parse_search_query(
        cls,
        args: str,
        user_now: datetime,
    ) -> tuple[str, SearchFilters]:
        """返回已规范化关键词和可直接交给数据库的筛选条件。"""
        query_tokens, raw_filters = cls._tokenize_search_args(args)
        raw_query = " ".join(query_tokens).strip()
        if not raw_query:
            raise ValueError("请提供搜索关键词")
        if len(raw_query) > _SEARCH_QUERY_MAX_CHARS:
            raise ValueError(f"搜索关键词不能超过 {_SEARCH_QUERY_MAX_CHARS} 字")
        query = sanitize_search_keyword(raw_query)
        if not query:
            raise ValueError("搜索关键词清洗后为空")
        return query, cls._normalize_search_filters(raw_filters, user_now)

    async def _load_event_collection_titles(
        self,
        user_id: str,
        results: list[SearchItem],
    ) -> dict[str, str]:
        """批量读取当前页日程集合标题，避免在格式化循环中阻塞事件循环。"""
        collection_ids = list(
            dict.fromkeys(
                item.event_collection_id
                for item in results
                if isinstance(item, EventItem) and item.event_collection_id
            )
        )
        if not collection_ids:
            return {}
        collections = cast(
            dict[str, dict[str, Any]],
            await run_sync(
                self.db.get_event_collections_by_ids,
                user_id,
                collection_ids,
            ),
        )
        return {
            collection_id: str(collection.get("title") or "无标题")
            for collection_id, collection in collections.items()
        }

    def _format_search_results(
        self,
        results: list[SearchItem],
        total: int,
        query: str,
        filters: SearchFilters,
        collection_titles: dict[str, str],
        display_timezone: tzinfo,
    ) -> str:
        """按固定类型顺序展示当前页，并保留数据库返回的精确总数。"""
        if not results:
            return f'🔍 搜索 "{query}"\n\n未找到相关内容'

        grouped: dict[str, list[SearchItem]] = defaultdict(list)
        for item in results:
            item_type = get_item_type_value(item.type, default=str(item.type))
            grouped[item_type].append(item)

        parts: list[str] = [
            "🔎 搜索结果",
            f"关键词: {query}",
            f"命中: {total} 条",
        ]
        filter_parts = self._format_filter_summary(filters)
        if filter_parts:
            parts.append(f"筛选: {filter_parts}")
        parts.extend(("━━━━━━━━━━━━━━━━━━", ""))

        ordered_types: list[str] = [
            item_type for item_type in _SEARCH_TYPE_ORDER if item_type in grouped
        ]
        ordered_types.extend(sorted(set(grouped).difference(_SEARCH_TYPE_ORDER)))
        single_type = len(grouped) == 1
        for item_type in ordered_types:
            items = grouped[item_type]
            if not single_type:
                type_name = TYPE_NAMES.get(item_type, item_type)
                parts.append(f"【{type_name}】本页 {len(items)} 条")
            for item in items:
                collection_id = item.event_collection_id if isinstance(item, EventItem) else None
                collection_title = collection_titles.get(collection_id or "")
                parts.append(
                    self._format_item_line(
                        item,
                        query,
                        collection_title,
                        display_timezone=display_timezone,
                    )
                )
            parts.append("")

        remaining = max(0, total - len(results))
        if remaining > 0:
            parts.append(f"...还有 {remaining} 条结果，请添加筛选条件缩小范围")

        hints = [
            _TYPE_ACTION_HINTS[item_type]
            for item_type in _SEARCH_TYPE_ORDER
            if item_type in grouped
        ]
        if hints:
            parts.extend(("", f"💡 操作: {' | '.join(hints)}"))

        return "\n".join(parts)

    @staticmethod
    def _format_item_title(
        item: SearchItem,
        collection_title: str | None,
    ) -> str:
        title = single_line_text(item.title)
        if isinstance(item, EventItem) and collection_title:
            collection = single_line_text(collection_title) or "无标题"
            title = f"{collection} · {title or '无标题'}"
        if not title:
            title = single_line_text(item.content) or "无标题"
        return ItemFormatter.truncate_content(title, _SEARCH_TITLE_MAX_CHARS, "...")

    @staticmethod
    def _format_item_heading(item: SearchItem, title: str) -> str:
        item_type = get_item_type_value(item.type, default=str(item.type))
        icon = ItemFormatter.format_type_icon(item_type)
        main_line = f"• {icon} {title}"
        if isinstance(item, LedgerItem):
            sign = _TRANSACTION_SIGNS.get(item.transaction_type, "-")
            main_line += f"  {sign}¥{ledger_amount_yuan(item):.2f}"
        elif isinstance(item, TaskItem):
            status_icon = STATUS_ICONS.get(item.status.value, "⬜")
            main_line += f" {status_icon}"
            if item.priority <= 2:
                main_line += f" {PRIORITY_ICONS.get(item.priority, '')}"
        return main_line

    @staticmethod
    def _ledger_detail_parts(item: LedgerItem) -> list[str]:
        details: list[str] = []
        if item.ledger_category:
            details.append(f"📂{single_line_text(item.ledger_category)}")
        if item.account_name:
            account = single_line_text(item.account_name)
            counter = single_line_text(item.counter_account_name)
            details.append(f"🏦{account + '→' + counter if counter else account}")
        if item.merchant:
            details.append(f"🏷️{single_line_text(item.merchant)}")
        return details

    @staticmethod
    def _common_detail_parts(item: SearchItem) -> list[str]:
        details: list[str] = []
        if item.category and item.category != "未分类":
            details.append(f"📂{single_line_text(item.category)}")
        if item.tags:
            tags = [single_line_text(tag) for tag in item.tags[:2]]
            details.append(f"🏷️{ItemFormatter.format_tags(tags)}")
        return details

    def _format_item_details(
        self,
        item: SearchItem,
        query: str,
        display_timezone: tzinfo,
    ) -> list[str]:
        details: list[str] = []
        if time_info := self._get_item_time_info(item, display_timezone):
            details.append(time_info)
        if isinstance(item, LedgerItem):
            details.extend(self._ledger_detail_parts(item))
        else:
            details.extend(self._common_detail_parts(item))
        if isinstance(item, EventItem) and item.remind_times:
            remind_times = parse_remind_times(item.remind_times)
            if remind_times:
                details.append(f"🔔{len(remind_times)}个提醒")
        if preview := self._get_content_preview(item, query):
            details.append(f"「{preview}」")
        return details

    def _format_item_line(
        self,
        item: SearchItem,
        query: str,
        collection_title: str | None = None,
        *,
        display_timezone: tzinfo,
    ) -> str:
        """把一条结果格式化为标题、元数据和 ID 三个紧凑区段。"""
        title = self._format_item_title(item, collection_title)
        lines = [self._format_item_heading(item, title)]
        if details := self._format_item_details(item, query, display_timezone):
            lines.append(f"  {' | '.join(details)}")
        lines.append(f"  ID: `{item.id or ''}`")
        return "\n".join(lines)

    def _get_content_preview(self, item: SearchItem, query: str) -> str:
        """提取单行内容片段，并尽量把命中位置保留在固定预算内。"""
        source = item.remark if isinstance(item, LedgerItem) else item.content
        content = single_line_text(source)
        if not content or content == single_line_text(item.title):
            return ""

        preview_len = PendoConfig.SEARCH_CONTENT_PREVIEW_LENGTH
        idx = content.casefold().find(query.casefold())
        if idx >= 0:
            start = max(0, idx - 10)
            end = min(len(content), start + preview_len)
            snippet = content[start:end]
            if start > 0:
                snippet = f"...{snippet}"
            if end < len(content):
                snippet = f"{snippet}..."
            return snippet

        return ItemFormatter.truncate_content(content, preview_len, "...")

    def _get_item_time_info(self, item: SearchItem, display_timezone: tzinfo) -> str:
        """获取条目的时间信息"""
        if isinstance(item, EventItem) and item.start_time:
            dt_str = ItemFormatter.format_datetime(item.start_time, tz=display_timezone)
            return f"🗓️{dt_str}"

        if isinstance(item, TaskItem):
            if item.plan_date:
                return f"📅{item.plan_date}"
            if item.deadline_at:
                dt_str = ItemFormatter.format_datetime(item.deadline_at, tz=display_timezone)
                return f"⏱{dt_str}"

        if isinstance(item, DiaryItem):
            if item.entry_time:
                dt_str = ItemFormatter.format_datetime(item.entry_time, tz=display_timezone)
                return f"📔{dt_str}"
            if item.diary_date:
                return f"📔{item.diary_date}"

        if isinstance(item, LedgerItem) and item.ledger_date:
            return f"📅{item.ledger_date}"

        return ""

    def _format_filter_summary(self, filters: SearchFilters) -> str:
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
            label = _TRANSACTION_LABELS.get(transaction_type, transaction_type)
            parts.append(f"交易={label}")
        if filters.get("account_name"):
            parts.append(f"账户={filters['account_name']}")
        if filters.get("merchant"):
            parts.append(f"商户={filters['merchant']}")
        if filters.get("start_date") or filters.get("end_date"):
            parts.append(f"时间={filters.get('start_date', '')}~{filters.get('end_date', '')}")
        return " | ".join(parts)
