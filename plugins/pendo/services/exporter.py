"""
Pendo Markdown 导出服务。

插件端只保留导出能力：
- `/pendo export <filename> [range] [type]`
- 生成单个 Markdown 档案文件
- 由上层命令处理器负责将文件通过 OneBot 发给 QQ 用户
"""

from __future__ import annotations

import logging
import re
from contextvars import ContextVar
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any, ClassVar, Protocol

from ..models.item import get_item_type_value
from ..utils.currency import currency_label
from ..utils.error_handlers import error_result, success_result
from ..utils.identifiers import public_id
from ..utils.time_utils import TimezoneHelper, parse_search_date_range

logger                               = logging.getLogger(__name__)
_EXPORT_TIMEZONE: ContextVar[tzinfo] = ContextVar(
    "pendo_export_timezone", default=TimezoneHelper.DEFAULT_TZ
)


class _ExportItemsRepository(Protocol):
    """导出器依赖的最小条目仓储接口。"""

    def get_items(
        self,
        owner_id: str,
        filters: dict[str, Any] | None = None,
        limit: int                     = 100,
        offset: int                    = 0,
        *,
        use_cache: bool = True,
    ) -> list[Any]: ...

    def get_all_items(
        self,
        owner_id: str,
        filters: dict[str, Any] | None = None,
        *,
        page_size: int = 200,
    ) -> list[Any]: ...

    def get_event_collection(
        self, collection_id: str, owner_id: str | None = None
    ) -> dict[str, Any] | None: ...


class _ExportDatabase(_ExportItemsRepository, Protocol):
    """导出器依赖的最小数据库接口。"""

    def get_user_settings(self, user_id: str) -> dict[str, Any]: ...

    def log_transfer(
        self,
        owner_id: str,
        action: str,
        bundle_id: str | None                 = None,
        filename: str | None                  = None,
        types: list[str] | None               = None,
        record_count: int                     = 0,
        result_summary: dict[str, Any] | None = None,
    ) -> int: ...


def _sanitize_user_id(user_id: str) -> str:
    """清洗用户 ID，避免路径遍历。"""
    return re.sub(r"[^a-zA-Z0-9_-]", "_", user_id)


def _sanitize_export_filename(raw_name: str) -> str:
    """清洗导出文件名，保留可读性并强制 .md 后缀。"""
    name = (raw_name or "").strip().strip('"').strip("'")
    if not name:
        return ""
    name = re.sub(r"[<>:\"/\\|?*\x00-\x1f]+", "_", name).strip(" .")
    if not name:
        return ""
    if not name.lower().endswith(".md"):
        name = f"{name}.md"
    return name


class ExporterService:
    """Pendo 导出服务。"""

    _EXPORT_TYPE_MAP: ClassVar[dict[str, str]] = {
        "event": "event",
        "todo": "task",
        "task": "task",
        "note": "note",
        "ledger": "ledger",
        "diary": "diary",
        "all": "all",
        "全部": "all",
        "*": "all",
    }
    _TYPE_ORDER: ClassVar[tuple[str, ...]] = ("event", "task", "note", "ledger", "diary")
    _TYPE_LABELS: ClassVar[dict[str, str]] = {
        "event": "日程",
        "task": "待办",
        "note": "笔记",
        "ledger": "记账",
        "diary": "日记",
    }
    _TYPE_EXPORT_NAMES: ClassVar[dict[str, str]] = {
        "event": "event",
        "task": "todo",
        "note": "note",
        "ledger": "ledger",
        "diary": "diary",
    }
    _TYPE_SUMMARY_HINTS: ClassVar[dict[str, str]] = {
        "event": "按开始时间归档，保留地点、提醒、日程集合与节点上下文。",
        "task": "按计划日期、截止时间或创建时间排序，保留优先级、提醒与完成状态。",
        "note": "按创建时间归档，保留分类、标签与关联条目信息。",
        "ledger": "按记账日期归档，突出收支方向、金额与分类。",
        "diary": "按日记日期归档，保留天气、地点、心情等记录。",
    }

    def __init__(self, db: _ExportDatabase, export_root: Path) -> None:
        self.db = db
        self._export_root = Path(export_root)
        self._event_collection_cache: dict[tuple[str | None, str], dict[str, Any] | None] = {}

    def export_markdown(self, user_id: str, args: str, context: dict[str, Any]) -> dict[str, Any]:
        """导出为单个 Markdown 档案文件。"""
        token = _EXPORT_TIMEZONE.set(TimezoneHelper.get_user_timezone(user_id, self.db))
        try:
            return self._export_markdown(user_id, args, context)
        finally:
            _EXPORT_TIMEZONE.reset(token)

    def _export_markdown(self, user_id: str, args: str, context: dict[str, Any]) -> dict[str, Any]:
        """在调用级时区上下文中收集并渲染档案。"""
        self._event_collection_cache.clear()
        params = self._parse_export_args(args)
        if params.get("status") == "error":
            return params

        items = self._collect_items(
            user_id        = user_id,
            selected_types = params["types"],
            start_date     = params["start_date"],
            end_date       = params["end_date"],
        )
        if not items:
            return success_result(
                f"没有找到符合条件的数据\n文件名: {params['filename']}\n"
                f"时间范围: {params['range_label']}\n类型: {params['type_label']}"
            )

        export_dir = self._export_root / _sanitize_user_id(user_id)
        export_dir.mkdir(parents=True, exist_ok=True)
        file_path = export_dir / params["filename"]

        markdown = self._render_markdown_document(user_id, items, params)
        file_path.write_text(markdown, encoding="utf-8")

        counts = self._build_counts(items)
        self._log_export(user_id, params, file_path, items, counts)

        return success_result(
            (
                f"已导出 {len(items)} 条记录到 `{params['filename']}`\n"
                f"时间范围: {params['range_label']}\n"
                f"类型: {params['type_label']}"
            ),
            file_path    = str(file_path.resolve()),
            file_name    = params["filename"],
            counts       = counts,
            record_count = len(items),
            range_label  = params["range_label"],
            type_label   = params["type_label"],
        )

    def _parse_export_args(self, args: str) -> dict[str, Any]:
        tokens = self._tokenize_args(args)
        if tokens and tokens[0].lower() == "md":
            tokens = tokens[1:]

        if not tokens:
            return error_result(
                "请提供导出文件名。\n\n"
                "示例:\n"
                "/pendo export 我的档案\n"
                "/pendo export 工作回顾 last30d event,todo\n"
                "/pendo export 账本快照 2026-03 ledger"
            )

        filename = _sanitize_export_filename(tokens[0])
        if not filename:
            return error_result("导出文件名无效，请换一个更简单的名字")

        rest        = tokens[1:]
        range_token = None
        if rest and self._looks_like_range_spec(rest[0]):
            range_token = rest.pop(0)

        type_token = ",".join(rest) if rest else None

        range_info = self._parse_range_spec(range_token)
        if range_info.get("status") == "error":
            return range_info

        type_info = self._parse_type_spec(type_token)
        if type_info.get("status") == "error":
            return type_info

        return {
            "status": "success",
            "filename": filename,
            "start_date": range_info["start_date"],
            "end_date": range_info["end_date"],
            "range_label": range_info["label"],
            "types": type_info["types"],
            "type_label": type_info["label"],
        }

    def _tokenize_args(self, args: str) -> list[str]:
        pattern           = r'"([^"]+)"|\'([^\']+)\'|(\S+)'
        tokens: list[str] = []
        for match in re.finditer(pattern, args or ""):
            token = next((group for group in match.groups() if group), "")
            if token:
                tokens.append(token.strip())
        return tokens

    def _looks_like_range_spec(self, token: str) -> bool:
        normalized = (token or "").strip().lower()
        if not normalized:
            return False
        if normalized in {
            "all",
            "全部",
            "*",
            "today",
            "tomorrow",
            "week",
            "month",
            "year",
            "今天",
            "本周",
            "本月",
            "今年",
        }:
            return True
        if ".." in normalized:
            return True
        if re.fullmatch(r"last\d+d", normalized):
            return True
        if re.fullmatch(r"\d{4}", normalized):
            return True
        if re.fullmatch(r"\d{4}-\d{2}", normalized):
            return True
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", normalized):
            return True
        return False

    def _parse_range_spec(self, token: str | None) -> dict[str, Any]:
        normalized = (token or "").strip()
        if not normalized or normalized.lower() in {"all", "*"} or normalized == "全部":
            return {
                "status": "success",
                "start_date": None,
                "end_date": None,
                "label": "全部时间",
            }

        start_date, end_date = parse_search_date_range(
            normalized, TimezoneHelper.now(_EXPORT_TIMEZONE.get()).replace(tzinfo=None)
        )
        if not start_date or not end_date:
            return error_result(f"无法解析时间范围: {normalized}")

        return {
            "status": "success",
            "start_date": start_date,
            "end_date": end_date,
            "label": f"{self._format_time_value(start_date)} .. {self._format_time_value(end_date)}",
        }

    def _parse_type_spec(self, token: str | None) -> dict[str, Any]:
        normalized = (token or "").strip()
        if not normalized:
            types = list(self._TYPE_ORDER)
            return {
                "status": "success",
                "types": types,
                "label": "全部类型",
            }

        parts = [part.strip().lower() for part in re.split(r"[,，+/|]", normalized) if part.strip()]
        if not parts:
            return error_result(f"无法解析类型筛选: {token}")

        selected: list[str] = []
        for part in parts:
            mapped = self._EXPORT_TYPE_MAP.get(part)
            if mapped is None:
                allowed = ", ".join(["event", "todo", "note", "ledger", "diary"])
                return error_result(f"未知导出类型: {part}\n可选类型: {allowed}")
            if mapped == "all":
                selected = list(self._TYPE_ORDER)
                break
            if mapped not in selected:
                selected.append(mapped)

        if not selected:
            selected = list(self._TYPE_ORDER)

        ordered = [item_type for item_type in self._TYPE_ORDER if item_type in selected]
        label   = "、".join(self._TYPE_LABELS[item_type] for item_type in ordered)
        return {
            "status": "success",
            "types": ordered,
            "label": label,
        }

    def _collect_items(
        self,
        user_id: str,
        selected_types: list[str],
        start_date: str | None,
        end_date: str | None,
    ) -> list[Any]:
        items: list[Any] = []
        for item_type in selected_types:
            # 正式数据库按页读完，避免旧版 10000 条上限造成静默截断；轻量测试
            # 仓储仍可只实现 get_items。
            if hasattr(self.db, "get_all_items"):
                type_items = self.db.get_all_items(
                    user_id,
                    filters   = {"type": item_type},
                    page_size = 500,
                )
            else:
                type_items = self.db.get_items(
                    user_id,
                    filters   = {"type": item_type},
                    limit     = 10000,
                    use_cache = False,
                )
            for item in type_items:
                if self._item_matches_range(item, item_type, start_date, end_date):
                    items.append(item)
        return items

    def _item_matches_range(
        self,
        item: Any,
        item_type: str,
        start_date: str | None,
        end_date: str | None,
    ) -> bool:
        if not start_date or not end_date:
            return True

        item_dt = self._get_sort_datetime(item, item_type)
        if item_dt is None:
            return False

        start_dt = self._parse_datetime(start_date)
        end_dt   = self._parse_datetime(end_date)
        if start_dt is None or end_dt is None:
            return True
        return start_dt <= item_dt <= end_dt

    def _get_sort_datetime(self, item: Any, item_type: str) -> datetime | None:
        candidates: list[str | None] = []
        if item_type == "event":
            candidates = [getattr(item, "start_time", None), getattr(item, "created_at", None)]
        elif item_type == "task":
            candidates = [
                getattr(item, "plan_date", None),
                getattr(item, "deadline_at", None),
                getattr(item, "created_at", None),
            ]
        elif item_type == "ledger":
            candidates = [getattr(item, "ledger_date", None), getattr(item, "created_at", None)]
        elif item_type == "diary":
            candidates = [
                getattr(item, "entry_time", None),
                getattr(item, "diary_date", None),
                getattr(item, "created_at", None),
            ]
        else:
            candidates = [getattr(item, "created_at", None)]

        for raw in candidates:
            parsed = self._parse_datetime(raw)
            if parsed is not None:
                return parsed
        return None

    def _parse_datetime(self, value: str | None) -> datetime | None:
        if not value:
            return None

        text = str(value).strip()
        if not text:
            return None

        try:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
                dt = datetime.strptime(text, "%Y-%m-%d")
            else:
                dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None

        if dt.tzinfo is not None:
            # 范围与显示均采用当前导出用户的时区，再比较日历墙钟。
            return dt.astimezone(_EXPORT_TIMEZONE.get()).replace(tzinfo=None)
        return dt

    def _build_counts(self, items: list[Any]) -> dict[str, int]:
        counts = dict.fromkeys(self._TYPE_ORDER, 0)
        for item in items:
            item_type = get_item_type_value(getattr(item, "type", None))
            if item_type in counts:
                counts[item_type] += 1
        return counts

    def _render_markdown_document(
        self,
        user_id: str,
        items: list[Any],
        params: dict[str, Any],
    ) -> str:
        now = TimezoneHelper.now(_EXPORT_TIMEZONE.get()).isoformat(timespec="seconds")
        counts              = self._build_counts(items)
        sections: list[str] = []

        header = [
            f"# Pendo 导出档案 · {Path(params['filename']).stem}",
            "",
            "> 这是一份可读性优先的 Markdown 档案，适合备份、分享和归档。",
            ">",
            f"> - 导出时间: {now}",
            f"> - 用户标识: {user_id}",
            f"> - 时间范围: {params['range_label']}",
            f"> - 类型筛选: {params['type_label']}",
            f"> - 总条目数: {len(items)}",
            "",
            "## 导出摘要",
            "",
            "| 类型 | 数量 | 说明 |",
            "| --- | ---: | --- |",
        ]
        for item_type in self._TYPE_ORDER:
            if counts[item_type] <= 0:
                continue
            header.append(
                f"| {self._TYPE_LABELS[item_type]} (`{self._TYPE_EXPORT_NAMES[item_type]}`) | "
                f"{counts[item_type]} | {self._TYPE_SUMMARY_HINTS[item_type]} |"
            )
        header.extend(
            [
                "",
                "---",
                "",
                "## 目录",
                "",
            ]
        )

        for item_type in self._TYPE_ORDER:
            if counts[item_type] <= 0:
                continue
            header.append(
                f"- [{self._TYPE_LABELS[item_type]} · {counts[item_type]} 条]"
                f"(#{self._TYPE_LABELS[item_type]})"
            )

        for item_type in self._TYPE_ORDER:
            type_items = [
                item
                for item in items
                if get_item_type_value(getattr(item, "type", None)) == item_type
            ]
            if not type_items:
                continue
            sections.append(self._render_type_section(item_type, type_items))

        footer = [
            "---",
            "",
            "_由 Pendo 导出生成。此文件为静态快照，不会随原始数据自动同步。_",
            "",
        ]

        return "\n".join(header + sections + footer)

    def _render_type_section(self, item_type: str, items: list[Any]) -> str:
        sorted_items = sorted(
            items,
            key=lambda item: self._get_sort_datetime(item, item_type) or datetime.min,
        )
        blocks = [
            f"## {self._TYPE_LABELS[item_type]}",
            "",
            f"> 共 {len(sorted_items)} 条，导出名为 `{self._TYPE_EXPORT_NAMES[item_type]}`。",
            "",
        ]
        for index, item in enumerate(sorted_items, start=1):
            blocks.append(self._render_item_block(item_type, item, index))
        return "\n".join(blocks)

    def _render_item_block(self, item_type: str, item: Any, index: int) -> str:
        title      = re.sub(r"[\r\n]+", " ", self._display_title(item_type, item)).strip()
        meta_rows  = self._build_common_meta_rows(item_type, item)
        extra_rows = self._build_type_specific_rows(item_type, item)
        all_rows   = meta_rows + extra_rows

        lines = [
            f"### {index:02d}. {title}",
            "",
            f"> `{self._TYPE_EXPORT_NAMES[item_type]}` · `id:{getattr(item, 'id', 'N/A')}`",
            "",
            "| 字段 | 内容 |",
            "| --- | --- |",
        ]
        for label, value in all_rows:
            lines.append(f"| {label} | {value} |")

        body_sections = self._build_body_sections(item_type, item)
        if body_sections:
            lines.extend([""] + body_sections)

        lines.extend(["", "---", ""])
        return "\n".join(lines)

    def _build_common_meta_rows(self, item_type: str, item: Any) -> list[tuple[str, str]]:
        category = getattr(item, "category", "")
        if item_type == "event" and self._is_uncategorized(category):
            collection = self._get_event_collection(item)
            if collection:
                category = collection.get("category") or category
        rows = [
            ("分类", self._escape_table(self._value_or_dash(category))),
            ("标签", self._escape_table(self._format_tags(getattr(item, "tags", [])))),
            (
                "创建时间",
                self._escape_table(self._format_time_value(getattr(item, "created_at", None))),
            ),
            (
                "更新时间",
                self._escape_table(self._format_time_value(getattr(item, "updated_at", None))),
            ),
        ]
        if item_type == "ledger":
            rows[0] = (
                "分类",
                self._escape_table(self._value_or_dash(getattr(item, "ledger_category", ""))),
            )
        return rows

    def _build_type_specific_rows(self, item_type: str, item: Any) -> list[tuple[str, str]]:
        if item_type == "event":
            collection = self._get_event_collection(item)
            return [
                (
                    "开始时间",
                    self._escape_table(self._format_time_value(getattr(item, "start_time", None))),
                ),
                (
                    "结束时间",
                    self._escape_table(self._format_time_value(getattr(item, "end_time", None))),
                ),
                (
                    "地点",
                    self._escape_table(
                        self._value_or_dash(
                            getattr(item, "location", "")
                            or ((collection or {}).get("location") or "")
                        )
                    ),
                ),
                (
                    "提醒",
                    self._escape_table(
                        self._format_time_list(getattr(item, "remind_times", []), empty="未设置")
                    ),
                ),
            ]

        if item_type == "task":
            return [
                (
                    "计划日期",
                    self._escape_table(self._format_time_value(getattr(item, "plan_date", None))),
                ),
                (
                    "截止时间",
                    self._escape_table(self._format_time_value(getattr(item, "deadline_at", None))),
                ),
                (
                    "优先级",
                    self._escape_table(self._format_priority(getattr(item, "priority", None))),
                ),
                (
                    "状态",
                    self._escape_table(self._format_task_status(getattr(item, "status", None))),
                ),
                (
                    "完成时间",
                    self._escape_table(
                        self._format_time_value(getattr(item, "completed_at", None))
                    ),
                ),
                (
                    "取消时间",
                    self._escape_table(
                        self._format_time_value(getattr(item, "cancelled_at", None))
                    ),
                ),
                (
                    "提醒",
                    self._escape_table(
                        self._format_time_list(getattr(item, "remind_times", []), empty="未设置")
                    ),
                ),
            ]

        if item_type == "note":
            return []

        if item_type == "ledger":
            amount           = getattr(item, "amount", None)
            transaction_type = getattr(item, "transaction_type", "")
            amount_text      = self._format_ledger_amount(
                amount, transaction_type, getattr(item, "currency", "CNY")
            )
            return [
                (
                    "记账日期",
                    self._escape_table(self._format_time_value(getattr(item, "ledger_date", None))),
                ),
                ("交易类型", self._escape_table(self._format_transaction_type(transaction_type))),
                ("金额", self._escape_table(amount_text)),
                ("币种", self._escape_table(self._value_or_dash(getattr(item, "currency", "")))),
                (
                    "账户",
                    self._escape_table(self._value_or_dash(getattr(item, "account_name", ""))),
                ),
                (
                    "转入账户",
                    self._escape_table(
                        self._value_or_dash(getattr(item, "counter_account_name", ""))
                    ),
                ),
                ("商户", self._escape_table(self._value_or_dash(getattr(item, "merchant", "")))),
                ("备注", self._escape_table(self._value_or_dash(getattr(item, "remark", "")))),
            ]

        if item_type == "diary":
            return [
                (
                    "日记日期",
                    self._escape_table(self._format_time_value(getattr(item, "diary_date", None))),
                ),
                (
                    "记录时间",
                    self._escape_table(self._format_time_value(getattr(item, "entry_time", None))),
                ),
                ("天气", self._escape_table(self._value_or_dash(getattr(item, "weather", "")))),
                ("地点", self._escape_table(self._value_or_dash(getattr(item, "location", "")))),
                ("心情", self._escape_table(self._value_or_dash(getattr(item, "mood", "")))),
                (
                    "心情分数",
                    self._escape_table(self._value_or_dash(getattr(item, "mood_score", ""))),
                ),
                ("收藏", "是" if getattr(item, "is_favorite", False) else "否"),
                ("模板", self._escape_table(self._value_or_dash(getattr(item, "template_id", "")))),
            ]

        return []

    def _display_title(self, item_type: str, item: Any) -> str:
        title = (getattr(item, "title", "") or "").strip() or "无标题"
        if item_type != "event":
            return title
        collection = self._get_event_collection(item)
        if not collection:
            return title
        collection_title = str(collection.get("title") or "").strip()
        if not collection_title:
            return title
        if collection_title == title:
            return title
        return f"{collection_title} · {title}"

    def _build_body_sections(self, item_type: str, item: Any) -> list[str]:
        sections: list[str] = []
        content             = (getattr(item, "content", "") or "").strip()
        if content:
            self._append_body_section(sections, "**正文**", [content])

        if item_type == "note":
            references = self._format_note_references(getattr(item, "references", []))
            self._append_body_section(sections, "**关联条目**", references)

        if item_type == "event":
            self._append_event_body_sections(sections, item)

        if item_type == "diary":
            answers = self._format_template_answers(getattr(item, "template_answers", []))
            self._append_body_section(sections, "**模板回答**", answers)

        return sections

    @staticmethod
    def _append_body_section(sections: list[str], heading: str, lines: list[str]) -> None:
        """追加一个非空 Markdown 段落，并统一处理段间空行。"""
        if not lines:
            return
        if sections:
            sections.append("")
        sections.extend([heading, "", *lines])

    def _append_event_body_sections(self, sections: list[str], item: Any) -> None:
        """追加日程集合上下文和节点备注。"""
        collection = (
            self._get_event_collection(item) if getattr(item, "event_collection_id", None) else None
        )
        if collection:
            collection_lines = [
                f"- 集合标题: {self._value_or_dash(collection.get('title'))}",
                f"- 集合类型: {self._value_or_dash(collection.get('kind'))}",
            ]
            collection_notes = str(collection.get("notes") or "").strip()
            if collection_notes:
                collection_lines.append(f"- 集合备注: {collection_notes}")
            self._append_body_section(sections, "**所属日程集合**", collection_lines)

        notes = str(getattr(item, "notes", "") or "").strip()
        self._append_body_section(sections, "**补充备注**", [notes] if notes else [])

    def _get_event_collection(self, item: Any) -> dict[str, Any] | None:
        collection_id = getattr(item, "event_collection_id", None)
        owner_id      = getattr(item, "owner_id", None)
        if not collection_id or not hasattr(self.db, "get_event_collection"):
            return None
        cache_key = (str(owner_id) if owner_id else None, str(collection_id))
        if cache_key in self._event_collection_cache:
            return self._event_collection_cache[cache_key]
        try:
            collection = self.db.get_event_collection(str(collection_id), cache_key[0])
        except Exception as exc:
            logger.warning("读取导出日程集合失败 error_type=%s", type(exc).__name__)
            collection = None
        self._event_collection_cache[cache_key] = collection
        return collection

    def _is_uncategorized(self, value: Any) -> bool:
        return str(value or "").strip() in {"", "未分类"}

    def _format_tags(self, tags: Any) -> str:
        if not tags:
            return "无"
        if isinstance(tags, list):
            filtered = [str(tag).strip() for tag in tags if str(tag).strip()]
            return " ".join(f"`#{tag}`" for tag in filtered) if filtered else "无"
        return str(tags)

    def _format_note_references(self, references: Any) -> list[str]:
        if not isinstance(references, list):
            return []
        labels = {
            "event": "日程",
            "task": "待办",
            "note": "笔记",
            "diary": "日记",
            "ledger": "账目",
            "item": "条目",
        }
        lines: list[str] = []
        seen: set[str]   = set()
        for ref in references:
            if not isinstance(ref, dict):
                continue
            ref_id = str(ref.get("id") or "").strip()
            if not ref_id or ref_id in seen:
                continue
            seen.add(ref_id)
            ref_type = str(ref.get("type") or ref.get("kind") or "item").strip()
            label    = labels.get(ref_type, labels.get(str(ref.get("kind") or ""), "条目"))
            title    = self._value_or_dash(ref.get("title"))
            lines.append(f"- {label}: {title} (`{public_id(ref_id)}`)")
        return lines

    def _format_template_answers(self, answers: Any) -> list[str]:
        if not isinstance(answers, list):
            return []
        lines: list[str] = []
        for item in answers:
            if not isinstance(item, dict):
                continue
            prompt = self._value_or_dash(item.get("prompt"))
            answer = self._value_or_dash(item.get("answer"))
            if prompt == "无" and answer == "无":
                continue
            lines.append(f"- {prompt}: {answer}")
        return lines

    def _format_priority(self, value: Any) -> str:
        mapping = {
            1: "P1 / 紧急",
            2: "P2 / 高",
            3: "P3 / 中",
            4: "P4 / 低",
            5: "P5 / 最低",
        }
        raw = getattr(value, "value", value)
        try:
            return mapping.get(int(raw), str(raw))
        except (TypeError, ValueError):
            return self._value_or_dash(raw)

    def _format_task_status(self, value: Any) -> str:
        mapping = {
            "open": "待处理",
            "done": "已完成",
            "cancelled": "已取消",
        }
        raw = getattr(value, "value", value)
        return mapping.get(str(raw), self._value_or_dash(raw))

    def _format_transaction_type(self, value: Any) -> str:
        mapping = {
            "income": "收入",
            "expense": "支出",
            "transfer": "转账",
        }
        return mapping.get(str(value), self._value_or_dash(value))

    def _format_ledger_amount(
        self, amount: Any, transaction_type: Any, currency: Any = "CNY"
    ) -> str:
        transaction_type_text = self._format_transaction_type(transaction_type)
        try:
            return f"{transaction_type_text} {currency_label(currency)}{float(amount):.2f}"
        except (TypeError, ValueError):
            return self._value_or_dash(amount)

    def _format_time_value(self, value: Any) -> str:
        if value in (None, ""):
            return "未设置"
        text = str(value).strip()
        if not text:
            return "未设置"

        parsed = self._parse_datetime(text)
        if parsed is None:
            return text

        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return parsed.strftime("%Y-%m-%d")
        return parsed.replace(tzinfo=_EXPORT_TIMEZONE.get()).isoformat(sep=" ", timespec="minutes")

    def _format_time_list(self, values: Any, empty: str = "无") -> str:
        if not values:
            return empty
        if not isinstance(values, list):
            return self._format_time_value(values)
        rendered = [self._format_time_value(value) for value in values if value]
        return "、".join(rendered) if rendered else empty

    def _value_or_dash(self, value: Any) -> str:
        text = str(value).strip() if value not in (None, "") else ""
        return text or "无"

    def _escape_table(self, text: str) -> str:
        return str(text).replace("|", "\\|").replace("\n", "<br>")

    def _log_export(
        self,
        user_id: str,
        params: dict[str, Any],
        file_path: Path,
        items: list[Any],
        counts: dict[str, int],
    ) -> None:
        if not hasattr(self.db, "log_transfer"):
            return
        try:
            self.db.log_transfer(
                owner_id=user_id,
                action="export",
                filename=file_path.name,
                types=[self._TYPE_EXPORT_NAMES[item_type] for item_type in params["types"]],
                record_count=len(items),
                result_summary={
                    "range": params["range_label"],
                    "type_label": params["type_label"],
                    "counts": counts,
                    "path": str(file_path.resolve()),
                },
            )
        except Exception as exc:
            # 审计日志失败不应影响导出主流程
            logger.warning("记录导出审计日志失败 error_type=%s", type(exc).__name__)
