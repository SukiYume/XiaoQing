"""处理笔记创建、查询、编辑、引用与删除。"""

from __future__ import annotations

import re
import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final, TypedDict, cast

from core.plugin_base import run_sync

from ..config import PendoConfig
from ..core.types import CommandMessage, PendoContext
from ..models.item import ItemType, NoteItem, get_item_type_value
from ..utils.db_ops import DbOpsMixin
from ..utils.error_handlers import handle_command_errors
from ..utils.formatters import (
    TAG_TOKEN_RE,
    ItemFormatter,
    extract_tags,
    paginate,
)
from ..utils.identifiers import public_id
from ..utils.settings_utils import resolve_default_category
from ..utils.time_utils import (
    TimezoneHelper,
    _parse_time_range_core,
    get_user_local_wall_time,
)
from ..utils.validators import normalize_note_fields

if TYPE_CHECKING:
    from ..services.db import Database


_ITEM_TYPE_LABELS: Final = {
    "event": "日程",
    "task": "待办",
    "note": "笔记",
    "diary": "日记",
    "ledger": "账目",
}
_QUOTED_OR_TOKEN_PATTERN: Final = r"""(?:"[^"]*"|'[^']*'|“[^”]*”|\S+)"""
_TITLE_CONTENT_RE: Final = re.compile(r"^([^\n]+?)\s+content[\s:]+(.*)$", re.IGNORECASE | re.DOTALL)
_INLINE_TITLE_RE: Final = re.compile(
    rf"(^|\s)(title:{_QUOTED_OR_TOKEN_PATTERN})(?=\s|$)", re.IGNORECASE
)
_INLINE_REFERENCE_RE: Final = re.compile(rf"(?<!\S)ref:({_QUOTED_OR_TOKEN_PATTERN})", re.IGNORECASE)
_METADATA_SUFFIX_RE: Final  = re.compile(
    rf"(?:^|\s)(cat:{_QUOTED_OR_TOKEN_PATTERN}|ref:{_QUOTED_OR_TOKEN_PATTERN}|#[\w\u4e00-\u9fa5-]+)\s*$",
    re.IGNORECASE,
)
_NOTE_TIME_RANGE_RE: Final = re.compile(
    r"last\d+d|\d{4}(?:-\d{2}(?:-\d{2})?)?(?:\.\.\d{4}-\d{2}-\d{2})?"
)
_CONTENT_MARKER_RE: Final            = re.compile(r"content(?:[\s:]+(.*))?", re.IGNORECASE)
_EMPTY_INLINE_TITLE_RE: Final        = re.compile(r"(^|\s)title:(?=\s|$)", re.IGNORECASE)
_MALFORMED_METADATA_SUFFIX_RE: Final = re.compile(
    r"""(?:^|\s)(cat|ref):(?:["'“].*)?\s*$""", re.IGNORECASE
)


class NoteExplicitFields(TypedDict):
    category: bool
    tags: bool
    references: bool


class NoteMetadata(TypedDict):
    text: str
    category: str | None
    tags: list[str]
    reference_ids: list[str]


class ParsedNoteText(TypedDict):
    title: str
    content: str
    category: str
    tags: list[str]
    reference_ids: list[str]
    _title_provided: bool
    _content_provided: bool
    _explicit_fields: NoteExplicitFields


@dataclass(frozen=True, slots=True)
class NoteListFilters:
    """笔记列表筛选；日期边界采用用户本地墙钟时间。"""

    category: str | None   = None
    tag: str | None        = None
    start_date: str | None = None
    end_date: str | None   = None
    time_label: str | None = None
    show_all: bool         = False
    page: int              = 1
    page_explicit: bool    = False

    @property
    def has_semantic_filter(self) -> bool:
        return bool(self.category or self.tag or self.start_date or self.end_date)


def _split_note_control_tokens(tokens: list[str]) -> tuple[list[str], bool, int, bool]:
    """提取独立的 ``all`` 与 ``page:N``，不误伤 ``#all``。"""
    remaining: list[str] = []
    show_all             = False
    page                 = 1
    page_seen            = False
    for token in tokens:
        normalized = token.casefold()
        if normalized == "all":
            if show_all:
                raise ValueError("all 参数不能重复")
            show_all = True
            continue
        if normalized.startswith("page:"):
            if page_seen:
                raise ValueError("page:N 参数不能重复")
            raw_page = token.split(":", 1)[1]
            if not raw_page.isdecimal() or int(raw_page) < 1:
                raise ValueError(f"无效页码: {token}")
            page      = int(raw_page)
            page_seen = True
            continue
        remaining.append(token)
    if show_all and page_seen:
        raise ValueError("all 与 page:N 不能同时使用")
    return remaining, show_all, page, page_seen


def _split_note_named_tokens(tokens: list[str]) -> tuple[list[str], dict[str, str]]:
    """提取 cat:、since: 与单个 #tag，保留裸时间/分类参数。"""
    remaining: list[str]  = []
    named: dict[str, str] = {}
    for token in tokens:
        normalized = token.casefold()
        prefix, separator, value = token.partition(":")
        normalized_prefix = prefix.casefold()
        if separator and normalized_prefix in {"cat", "since"}:
            if not value or normalized_prefix in named:
                raise ValueError(f"无效或重复的 {prefix}: 参数")
            named[normalized_prefix] = value
            continue
        if normalized.startswith("#"):
            if not TAG_TOKEN_RE.fullmatch(token) or "tag" in named:
                raise ValueError(f"无效或重复的标签: {token}")
            named["tag"] = token[1:]
            continue
        remaining.append(token)
    return remaining, named


def _looks_like_note_time_range(value: str) -> bool:
    normalized = value.casefold()
    return normalized in {"today", "今天", "week", "本周", "month", "本月", "year", "今年"} or bool(
        _NOTE_TIME_RANGE_RE.fullmatch(normalized)
    )


class NoteHandler(DbOpsMixin):
    """按明确的命令语法处理笔记，不依赖 AI 解析。"""

    def __init__(self, db: Database):
        self.db = db

    @handle_command_errors
    async def handle(
        self, user_id: str, args: str, context: PendoContext, group_id: int | None = None
    ) -> CommandMessage:
        """处理笔记相关命令

        命令格式：
        - /pendo note add <内容> [cat:xxx] [#tag] [ref:条目ID]
        - /pendo note add title:<标题> content <正文> [cat:xxx] [#tag] [ref:条目ID]
        - /pendo note add title:<标题>\n<正文多行>\ncat:xxx #tag ref:条目ID
        - /pendo note list [cat:xxx] [#tag] [all|page:n]
        - /pendo note view <id>
        - /pendo note edit <id> <新内容> [cat:xxx] [#tag]
        - /pendo note delete <id|cat:xxx>
        """
        parts = args.split(maxsplit=1)
        if not parts or not parts[0]:
            # 无参数查询表示请求概览，不应按语法错误处理。
            return await self.list_notes(user_id, "", context)

        command = parts[0].casefold()
        rest    = parts[1] if len(parts) > 1 else ""

        if command == "add":
            return await self.create_note(user_id, rest, context, group_id)

        handlers: dict[str, Callable[[str, str, PendoContext], Awaitable[CommandMessage]]] = {
            "list": self.list_notes,
            "view": self.view_note,
            "edit": self.edit_note,
            "append": self.append_note,
            "tag": self.tag_note,
            "untag": self.untag_note,
            "link": self.link_note,
            "delete": self.delete_note,
        }
        if handler := handlers.get(command):
            return await handler(user_id, rest, context)
        return {
            "status": "error",
            "message": (
                f"❌ 未知笔记命令: {command}\n\n"
                "可用命令:\n"
                "• /pendo note add <内容> [cat:xxx] [#tag] [ref:条目ID]\n"
                "• /pendo note add title:<标题> content <正文> [cat:xxx] [#tag]\n"
                "• /pendo note add title:<标题> 后换行写正文，结尾可加 cat:xxx #tag\n"
                "• /pendo note list [cat:xxx] [#tag]\n"
                "• /pendo note view <id>\n"
                "• /pendo note edit <id> <新内容>\n"
                "• /pendo note append <id> <追加内容>\n"
                "• /pendo note tag <id> #标签 / untag <id> #标签\n"
                "• /pendo note link <id> <关联条目ID>\n"
                "• /pendo note delete <id|cat:xxx>"
            ),
        }

    async def create_note(
        self, user_id: str, content: str, context: PendoContext, group_id: int | None = None
    ) -> CommandMessage:
        """创建笔记（支持cat:xxx和#tag语法）"""
        if not content:
            return {"status": "error", "message": "❌ 请提供内容"}

        try:
            parsed = self._parse_note_text(content)
        except ValueError as exc:
            return {"status": "error", "message": f"❌ {exc}"}
        if not parsed["category"]:
            parsed["category"] = await run_sync(resolve_default_category, self.db, user_id)

        try:
            references, related_items = await self._resolve_note_references(
                user_id,
                parsed["reference_ids"],
            )
        except ValueError as exc:
            return {"status": "error", "message": f"❌ {exc}"}

        try:
            normalized = normalize_note_fields(
                {
                    "title": parsed["title"],
                    "content": parsed["content"],
                    "category": parsed["category"],
                    "tags": parsed["tags"],
                    "references": references,
                    "related_items": related_items,
                },
                partial=False,
            )
        except ValueError as exc:
            return {"status": "error", "message": f"❌ {exc}"}

        current   = await get_user_local_wall_time(user_id, self.db)
        note_item = NoteItem(
            owner_id      = user_id,
            title         = normalized["title"],
            content       = normalized["content"],
            tags          = normalized["tags"],
            category      = normalized["category"],
            references    = normalized["references"],
            related_items = normalized["related_items"],
            context       = {"group_id": group_id} if group_id is not None else {},
            created_at    = current.isoformat(),
            updated_at    = current.isoformat(),
        )

        item_id = await self._db_create_with_log(note_item, owner_id=user_id, action="create_note")

        tags_str = ItemFormatter.format_tags(normalized["tags"])
        message  = "✅ 已记录笔记\n\n"
        message += f"📝 {normalized['title']}\n"
        message += f"📂 分类: {normalized['category']}\n"
        if tags_str:
            message += f"🏷️ 标签: {tags_str}\n"
        display_id = public_id(item_id)
        message += f"`{display_id}`\n\n"
        message += f"💡 用 /pendo note view {display_id} 查看详情"

        return {"status": "success", "message": message, "item_id": item_id}

    @staticmethod
    def _type_label(item_type: str) -> str:
        return _ITEM_TYPE_LABELS.get(item_type, "条目")

    @staticmethod
    def _merge_references(
        existing_refs: list[dict[str, Any]] | None,
        new_refs: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        seen: set[str]               = set()
        for ref in [*(existing_refs or []), *new_refs]:
            if not isinstance(ref, dict):
                continue
            ref_id = str(ref.get("id") or "").strip()
            if not ref_id or ref_id in seen:
                continue
            seen.add(ref_id)
            merged.append(ref)
        return merged

    @staticmethod
    def _related_from_references(refs: list[dict[str, Any]]) -> list[str]:
        related: list[str] = []
        seen: set[str]     = set()
        for ref in refs:
            ref_id = str(ref.get("id") or "").strip()
            if ref_id and ref_id not in seen:
                seen.add(ref_id)
                related.append(ref_id)
        return related

    async def _resolve_note_references(
        self,
        user_id: str,
        ref_ids: list[str],
        source_note_id: str | None = None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        references: list[dict[str, Any]] = []
        seen: set[str]                   = set()
        for raw_id in ref_ids:
            ref_id = str(raw_id or "").strip()
            if not ref_id:
                continue
            target = await self._db_get_item(ref_id, user_id)
            if target is None:
                raise ValueError(f"关联条目不存在: {ref_id}")
            resolved_ref_id = str(target.id)
            if source_note_id and resolved_ref_id == source_note_id:
                raise ValueError("笔记不能关联自身")
            if resolved_ref_id in seen:
                continue
            seen.add(resolved_ref_id)
            item_type = get_item_type_value(target.type, default="item")
            references.append(
                {
                    "kind": "item",
                    "id": resolved_ref_id,
                    "type": item_type,
                    "title": target.title or "无标题",
                }
            )
        return references, self._related_from_references(references)

    async def _find_note_backlinks(self, user_id: str, note_id: str) -> list[NoteItem]:
        return cast(
            list[NoteItem],
            await run_sync(self.db.get_note_backlinks, user_id, note_id),
        )

    def _parse_note_text(self, text: str) -> ParsedNoteText:
        """解析笔记文本（纯规则解析）

        支持格式：
        - 内容 cat:xxx #tag1 #tag2
        - title:标题 content 详细内容 cat:xxx #tag1 #tag2
        - title:标题\n正文多行... cat:xxx #tag1 #tag2
        - 内容 title:标题 cat:xxx #tag1 #tag2
        """
        meta         = self._extract_note_metadata(text)
        content_text = meta["text"]
        title, content_text = self._split_explicit_title(content_text)
        explicit_title = bool(title)
        if not title:
            title, content_text = self._extract_inline_title_token(content_text)
            explicit_title = bool(title)

        # 如果没有显式的 title，title 和 content 都是 content_text
        content_provided = bool(content_text.strip())
        if not title:
            title = content_text

        return {
            "title": title,
            "content": content_text,
            "category": meta["category"] or "",
            "tags": meta["tags"],
            "reference_ids": meta["reference_ids"],
            "_title_provided": explicit_title or content_provided,
            "_content_provided": content_provided,
            "_explicit_fields": {
                "category": bool(meta["category"]),
                "tags": bool(meta["tags"]),
                "references": bool(meta["reference_ids"]),
            },
        }

    @staticmethod
    def _normalize_title_token(raw_value: str) -> str | None:
        """清洗 title token 值，支持中英文及单双引号。"""
        value = (raw_value or "").strip()
        if not value:
            return None

        quote_pairs = {'"': '"', "'": "'", "“": "”"}
        if value[0] in quote_pairs:
            if not value.endswith(quote_pairs[value[0]]):
                raise ValueError("title: 标题引号未闭合")
            value = value[1:-1].strip()

        return value or None

    @classmethod
    def _split_explicit_title(cls, text: str) -> tuple[str | None, str]:
        """拆分 title: 前缀，兼容 inline content 和换行正文。"""
        content_text = (text or "").strip()
        if not content_text.casefold().startswith("title:"):
            return None, content_text

        title_payload = content_text[6:].lstrip()
        if not title_payload:
            raise ValueError("title: 后缺少标题")

        if title_payload[0] in {'"', "'", "“"}:
            return cls._split_quoted_title(title_payload)
        return cls._split_unquoted_title(title_payload)

    @classmethod
    def _split_quoted_title(cls, title_payload: str) -> tuple[str, str]:
        closing_quote = {'"': '"', "'": "'", "“": "”"}[title_payload[0]]
        end_quote     = title_payload.find(closing_quote, 1)
        if end_quote < 0:
            raise ValueError("title: 标题引号未闭合")

        title = cls._normalize_title_token(title_payload[: end_quote + 1])
        if not title:
            raise ValueError("title: 后缺少标题")
        remainder      = title_payload[end_quote + 1 :].lstrip()
        content_marker = _CONTENT_MARKER_RE.fullmatch(remainder)
        if content_marker:
            remainder = content_marker.group(1) or ""
        return title, remainder.strip()

    @classmethod
    def _split_unquoted_title(cls, title_payload: str) -> tuple[str, str]:
        inline_match = _TITLE_CONTENT_RE.match(title_payload)
        if inline_match:
            return inline_match.group(1).strip(), inline_match.group(2).strip()

        first_line, sep, remainder = title_payload.partition("\n")
        if sep:
            if first_line.strip():
                return first_line.strip(), remainder.strip()
            raise ValueError("title: 后缺少标题")

        # 单行未加 content 关键字时，按 metadata token 语义处理：title 值取第一个 token。
        parts = title_payload.split(maxsplit=1)
        title_token = cls._normalize_title_token(parts[0])
        if not title_token:
            raise ValueError("title: 后缺少标题")
        remainder_text = parts[1].strip() if len(parts) > 1 else ""
        return title_token, remainder_text

    @staticmethod
    def _extract_inline_title_token(text: str) -> tuple[str | None, str]:
        """从文本任意位置提取 title: token（如：xxx title:测试）。"""
        content_text = (text or "").strip()
        if not content_text:
            return None, content_text
        if _EMPTY_INLINE_TITLE_RE.search(content_text):
            raise ValueError("title: 后缺少标题")

        token_match = _INLINE_TITLE_RE.search(content_text)
        if not token_match:
            return None, content_text

        token     = token_match.group(2)
        raw_title = token[6:]
        title     = NoteHandler._normalize_title_token(raw_title)

        if not title:
            raise ValueError("title: 后缺少标题")

        before    = content_text[: token_match.start(2)].strip()
        after     = content_text[token_match.end(2) :].strip()
        remaining = f"{before} {after}".strip() if before and after else (before or after)

        return title, remaining

    @classmethod
    def _extract_inline_references(cls, text: str) -> tuple[str, list[str]]:
        """提取正文中的 ref: 参数，并拒绝显式的空引用。"""
        reference_ids: list[str] = []
        for match in _INLINE_REFERENCE_RE.finditer(text):
            ref_id = cls._normalize_metadata_token_value(match.group(1))
            if not ref_id:
                raise ValueError("ref: 后缺少条目ID")
            reference_ids.append(ref_id)
        return _INLINE_REFERENCE_RE.sub(" ", text).strip(), reference_ids

    @classmethod
    def _extract_note_metadata(cls, text: str) -> NoteMetadata:
        """分类和标签仅取自尾部；ref: 可位于正文中，Markdown 标题保持原样。"""
        lines = (text or "").rstrip().splitlines()
        if not lines:
            return {"text": "", "category": None, "tags": [], "reference_ids": []}

        category: str | None     = None
        tags: list[str]          = []
        reference_ids: list[str] = []

        for idx in range(len(lines) - 1, -1, -1):
            current = lines[idx].rstrip()
            if not current.strip():
                continue

            remaining, line_category, line_tags, line_refs, had_metadata = (
                cls._strip_note_metadata_suffix(current)
            )
            if not had_metadata:
                break

            if line_category:
                if category is not None:
                    raise ValueError("cat: 分类参数不能重复")
                category = line_category
            if line_tags:
                tags = line_tags + tags
            if line_refs:
                reference_ids = line_refs + reference_ids

            if remaining.strip():
                lines[idx] = remaining.rstrip()
                break

            lines = lines[:idx]

        return_text = "\n".join(lines).strip()
        return_text, inline_refs = cls._extract_inline_references(return_text)
        reference_ids = cls._dedupe_reference_ids([*reference_ids, *inline_refs])

        return {
            "text": return_text,
            "category": category,
            "tags": tags,
            "reference_ids": reference_ids,
        }

    @staticmethod
    def _normalize_metadata_token_value(raw_value: str) -> str:
        value       = (raw_value or "").strip()
        quote_pairs = {'"': '"', "'": "'", "“": "”"}
        if value and value[0] in quote_pairs:
            if not value.endswith(quote_pairs[value[0]]):
                raise ValueError("元数据参数中的引号未闭合")
            value = value[1:-1].strip()
        return value

    @staticmethod
    def _dedupe_reference_ids(values: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str]    = set()
        for value in values:
            ref_id = str(value or "").strip()
            if ref_id and ref_id not in seen:
                seen.add(ref_id)
                result.append(ref_id)
        return result

    @staticmethod
    def _strip_note_metadata_suffix(
        line: str,
    ) -> tuple[str, str | None, list[str], list[str], bool]:
        """从单行尾部反复剥离 cat:、ref: 与 #tag token。"""
        remaining                = line.rstrip()
        category                 = None
        tags: list[str]          = []
        reference_ids: list[str] = []
        had_metadata             = False

        while remaining:
            token_match = _METADATA_SUFFIX_RE.search(remaining)
            if not token_match:
                malformed = _MALFORMED_METADATA_SUFFIX_RE.search(remaining)
                if malformed:
                    key = malformed.group(1).casefold()
                    raise ValueError(f"{key}: 参数为空或引号未闭合")
                break

            token        = token_match.group(1)
            had_metadata = True

            token_lower = token.casefold()
            if token_lower.startswith("cat:"):
                if category is not None:
                    raise ValueError("cat: 分类参数不能重复")
                category = NoteHandler._normalize_metadata_token_value(token[4:])
                if not category:
                    raise ValueError("cat: 后缺少分类名")
            elif token_lower.startswith("ref:"):
                ref_id = NoteHandler._normalize_metadata_token_value(token[4:])
                if not ref_id:
                    raise ValueError("ref: 后缺少条目ID")
                reference_ids.insert(0, ref_id)
            elif token.startswith("#"):
                tags.insert(0, token[1:])

            remaining = remaining[: token_match.start()].rstrip()

        return remaining, category, tags, reference_ids, had_metadata

    @staticmethod
    def _resolve_bare_list_tokens(
        tokens: list[str], named: dict[str, str], user_now: datetime
    ) -> tuple[str | None, str | None, str | None, str | None]:
        """把剩余裸参数依次解释为时间范围和分类。"""
        remaining              = list(tokens)
        category               = named.get("cat")
        start_date: str | None = None
        end_date: str | None   = None
        time_label: str | None = None

        if since_value := named.get("since"):
            start, end = _parse_time_range_core(since_value, user_now, strict=True)
            start_date, end_date, time_label = start.isoformat(), end.isoformat(), since_value

        if start_date is None and remaining:
            candidate = remaining[0]
            try:
                start, end = _parse_time_range_core(candidate, user_now, strict=True)
            except ValueError:
                if _looks_like_note_time_range(candidate):
                    raise
            else:
                start_date, end_date, time_label = start.isoformat(), end.isoformat(), candidate
                remaining.pop(0)
        if start_date is not None and remaining and _looks_like_note_time_range(remaining[0]):
            raise ValueError("时间范围参数不能重复")

        if category is None and remaining:
            category = remaining.pop(0)
        if remaining:
            raise ValueError(f"无法识别的笔记列表参数: {' '.join(remaining)}")
        return category, start_date, end_date, time_label

    @classmethod
    def _parse_list_filters(cls, filter_text: str, user_now: datetime) -> NoteListFilters:
        try:
            tokens = shlex.split((filter_text or "").strip(), comments=False, posix=True)
        except ValueError as exc:
            raise ValueError("列表参数中的引号未闭合") from exc

        tokens, show_all, page, page_explicit = _split_note_control_tokens(tokens)
        tokens, named = _split_note_named_tokens(tokens)
        category, start_date, end_date, time_label = cls._resolve_bare_list_tokens(
            tokens, named, user_now
        )
        return NoteListFilters(
            category      = category,
            tag           = named.get("tag"),
            start_date    = start_date,
            end_date      = end_date,
            time_label    = time_label,
            show_all      = show_all,
            page          = page,
            page_explicit = page_explicit,
        )

    async def _load_notes(self, user_id: str, filters: NoteListFilters) -> list[NoteItem]:
        database_filters: dict[str, Any] = {"type": ItemType.NOTE.value}
        if filters.category:
            database_filters["category"] = filters.category
        if filters.tag:
            database_filters["tags"] = filters.tag
        if filters.start_date or filters.end_date:
            database_filters["date_field"] = "created_at"
        if filters.start_date:
            database_filters["start_date"] = filters.start_date
        if filters.end_date:
            database_filters["end_date"] = filters.end_date

        notes = cast(
            list[NoteItem],
            await run_sync(self.db.get_all_items, user_id, database_filters),
        )
        notes.sort(
            key=lambda note: (
                note.updated_at or "",
                note.created_at or "",
                str(note.id),
            ),
            reverse=True,
        )
        return notes

    @staticmethod
    def _format_list_filter_text(filters: NoteListFilters) -> str:
        labels: list[str] = []
        if filters.time_label:
            labels.append(f"时间: {filters.time_label}")
        if filters.category:
            labels.append(f"分类: {filters.category}")
        if filters.tag:
            labels.append(f"标签: #{filters.tag}")
        suffix = " - " + ", ".join(labels) if labels else ""
        if filters.show_all:
            suffix += " (全部显示)"
        elif filters.page_explicit:
            suffix += f" (第{filters.page}页)"
        return suffix

    @staticmethod
    def _format_category_overview(notes: list[NoteItem]) -> str:
        grouped: dict[str, list[NoteItem]] = {}
        for note in notes:
            grouped.setdefault(note.category or "未分类", []).append(note)

        lines = ["**分类概览**", ""]
        for category in sorted(grouped, key=str.casefold):
            category_notes = grouped[category]
            tags           = {tag for note in category_notes for tag in (note.tags or [])}
            tag_text       = (
                f" {ItemFormatter.format_tags(sorted(tags, key=str.casefold))}" if tags else ""
            )
            lines.append(f"📂 **{category}** ({len(category_notes)}项){tag_text}")
        lines.extend(("", "💡 用 /pendo note list <分类名> 查看该分类的笔记"))
        return "\n".join(lines)

    @staticmethod
    def _format_note_rows(notes: list[NoteItem], filters: NoteListFilters) -> str:
        display_raw, _, has_more = paginate(
            notes, filters.page, PendoConfig.LIST_PAGE_SIZE, filters.show_all
        )
        display_notes = cast(list[NoteItem], display_raw)
        if not display_notes and filters.page > 1:
            raise ValueError(f"第 {filters.page} 页没有笔记")

        lines: list[str] = []
        offset           = (filters.page - 1) * PendoConfig.LIST_PAGE_SIZE
        for index, note in enumerate(display_notes, offset + 1):
            title    = ItemFormatter.truncate_content(note.title or "无标题", 40)
            tags     = ItemFormatter.format_tags(note.tags or [])
            metadata = f"📂 {note.category or '未分类'}"
            if tags:
                metadata += f" | 🏷️ {tags}"
            lines.extend((f"{index}. {title}", f"   {metadata}", f"   `{note.display_id}`", ""))
        if has_more:
            lines.append(f"... (使用 'all' 显示全部或 'page:{filters.page + 1}' 查看下一页)")
        return "\n".join(lines).rstrip()

    async def list_notes(
        self, user_id: str, filter_str: str, context: PendoContext
    ) -> CommandMessage:
        """列出笔记

        格式：
        - /pendo note list -> 显示所有分类及精确计数
        - /pendo note list month -> 按本月创建时间筛选
        - /pendo note list cat:xxx -> 按分类筛选
        - /pendo note list #tag -> 按标签筛选
        - /pendo note list cat:xxx #tag -> 同时筛选
        - /pendo note list since:week -> 按时间筛选（today/week/month/year/last7d/YYYY-MM等）
        - /pendo note list cat:xxx all -> 显示该分类全部笔记
        - /pendo note list cat:xxx page:2 -> 显示该分类第2页
        """
        try:
            user_now = await get_user_local_wall_time(user_id, self.db)
            filters  = self._parse_list_filters(filter_str, user_now)
        except ValueError as exc:
            return {"status": "error", "message": f"❌ {exc}"}

        notes       = await self._load_notes(user_id, filters)
        filter_text = self._format_list_filter_text(filters)
        if not notes:
            return {"status": "success", "message": f"📝 没有找到笔记{filter_text}"}

        message       = f"📝 **笔记列表**{filter_text} (共{len(notes)}项)\n\n"
        overview_only = (
            not filters.has_semantic_filter and not filters.show_all and not filters.page_explicit
        )
        if overview_only:
            return {
                "status": "success",
                "message": message + self._format_category_overview(notes),
            }

        try:
            rows = self._format_note_rows(notes, filters)
        except ValueError as exc:
            return {"status": "error", "message": f"❌ {exc}"}
        message += f"{rows}\n\n💡 用 /pendo note view <id> 查看详情"
        return {"status": "success", "message": message}

    async def view_note(self, user_id: str, note_id: str, context: PendoContext) -> CommandMessage:
        """查看笔记详情"""
        if not note_id:
            return {"status": "error", "message": "❌ 请指定ID"}

        note_id = note_id.strip()
        if error := self._single_token_error(
            note_id, "❌ 笔记详情只接受一个ID\n例如: /pendo note view abc12345"
        ):
            return error

        note, wrong_type = await self._db_get_typed_item_or_message(
            note_id, user_id, ItemType.NOTE.value, "笔记"
        )
        if wrong_type:
            return wrong_type
        note = cast(NoteItem, note)

        current = await get_user_local_wall_time(user_id, self.db)
        last_viewed = normalize_note_fields({"last_viewed": current.isoformat()}, partial=True)
        await self._db_update_item(
            note_id,
            last_viewed,
            owner_id         = user_id,
            expected_version = note.version,
            touch            = False,
        )

        message = f"📝 **{note.title or '无标题'}**\n\n"

        display_timezone = await run_sync(
            TimezoneHelper.get_user_timezone,
            user_id,
            self.db,
        )
        created_str = ItemFormatter.format_datetime(
            note.created_at or "",
            tz=display_timezone,
        )
        message += f"🗓️ 创建: {created_str}\n"
        message += f"📂 分类: {note.category or '未分类'}\n"

        if note.tags:
            tags_str = ItemFormatter.format_tags(note.tags)
            message += f"🏷️ 标签: {tags_str}\n"

        message += "\n---\n\n"

        message += note.content or ""

        if note.references:
            message += "\n\n---\n\n🔗 **关联条目**\n"
            for ref in note.references[:10]:
                if not isinstance(ref, dict):
                    continue
                ref_id    = ref.get("id", "")
                ref_type  = self._type_label(str(ref.get("type") or ""))
                ref_title = ref.get("title") or "无标题"
                message += f"• {ref_type}: {ref_title} `{public_id(ref_id)}`\n"

        backlinks = await self._find_note_backlinks(user_id, note_id)
        if backlinks:
            message += "\n↩️ **被这些笔记引用**\n"
            for backlink in backlinks:
                message += f"• {backlink.title or '无标题'} `{backlink.display_id}`\n"

        return {"status": "success", "message": message}

    async def _build_edit_updates(
        self,
        user_id: str,
        note_id: str,
        note: NoteItem,
        parsed: ParsedNoteText,
    ) -> dict[str, Any] | None:
        """仅组装用户明确提供的字段；返回 ``None`` 表示没有有效修改。"""
        updates: dict[str, Any] = {"type": ItemType.NOTE.value}
        explicit_fields         = parsed["_explicit_fields"]

        if parsed["_title_provided"]:
            updates["title"] = parsed["title"]
        if parsed["_content_provided"]:
            updates["content"] = parsed["content"]
        if explicit_fields["category"]:
            updates["category"] = parsed["category"]
        if explicit_fields["tags"]:
            updates["tags"] = parsed["tags"]
        if explicit_fields["references"]:
            new_refs, _ = await self._resolve_note_references(
                user_id, parsed["reference_ids"], source_note_id=note_id
            )
            merged_refs              = self._merge_references(note.references, new_refs)
            updates["references"]    = merged_refs
            updates["related_items"] = self._related_from_references(merged_refs)

        if set(updates) == {"type"}:
            return None
        normalized = normalize_note_fields(updates, partial=True)
        changed = any(
            key != "type" and getattr(note, key, None) != value for key, value in normalized.items()
        )
        return normalized if changed else None

    async def edit_note(self, user_id: str, args: str, context: PendoContext) -> CommandMessage:
        """编辑笔记

        格式：/pendo note edit <id> <新内容> [cat:xxx] [#tag]
        """
        parts = args.split(maxsplit=1)
        if len(parts) < 2:
            return {
                "status": "error",
                "message": (
                    "❌ 用法: /pendo note edit <id> <新内容> [cat:xxx] [#tag]\n"
                    "也支持: /pendo note edit <id> title:<标题> content <正文>\n"
                    "或 title:<标题> 后直接换行写正文"
                ),
            }

        note_id     = parts[0].strip()
        new_content = parts[1]

        note, wrong_type = await self._db_get_typed_item_or_message(
            note_id, user_id, ItemType.NOTE.value, "笔记"
        )
        if wrong_type:
            return wrong_type
        note = cast(NoteItem, note)

        try:
            parsed = self._parse_note_text(new_content)
        except ValueError as exc:
            return {"status": "error", "message": f"❌ {exc}"}

        try:
            normalized_updates = await self._build_edit_updates(user_id, str(note.id), note, parsed)
        except ValueError as exc:
            return {"status": "error", "message": f"❌ {exc}"}
        if normalized_updates is None:
            return {"status": "warning", "message": "⚠️ 未识别到有效修改，或笔记内容没有变化"}

        await self._db_update_with_log(
            note_id,
            normalized_updates,
            user_id,
            action           = "edit_note",
            expected_version = note.version,
        )

        display_title = str(normalized_updates.get("title") or note.title or "无标题笔记")
        return {
            "status": "success",
            "message": f"✅ 已更新笔记\n\n📝 {display_title}\n\n💡 /pendo note view {note.display_id} 查看详情 | /pendo undo 撤销编辑",
        }

    async def append_note(self, user_id: str, args: str, context: PendoContext) -> CommandMessage:
        """追加内容到已有笔记。"""
        parts = args.split(maxsplit=1)
        if len(parts) < 2:
            return {"status": "error", "message": "❌ 用法: /pendo note append <id> <追加内容>"}
        note_id  = parts[0].strip()
        addition = parts[1].strip()
        if not addition:
            return {"status": "error", "message": "❌ 请提供要追加的内容"}

        note, wrong_type = await self._db_get_typed_item_or_message(
            note_id, user_id, ItemType.NOTE.value, "笔记"
        )
        if wrong_type:
            return wrong_type
        note         = cast(NoteItem, note)
        content      = (note.content or "").rstrip()
        next_content = f"{content}\n\n{addition}" if content else addition
        try:
            updates = normalize_note_fields(
                {"content": next_content, "type": ItemType.NOTE.value}, partial=True
            )
        except ValueError as exc:
            return {"status": "error", "message": f"❌ {exc}"}
        if updates["content"] == (note.content or ""):
            return {"status": "warning", "message": "⚠️ 追加内容清洗后没有产生变化"}
        await self._db_update_with_log(
            note_id,
            updates,
            user_id,
            action           = "edit_note",
            expected_version = note.version,
        )
        return {
            "status": "success",
            "message": f"✅ 已追加到笔记\n\n📝 {note.title or '无标题'}\n\n💡 /pendo note view {note.display_id} 查看详情 | /pendo undo 撤销",
        }

    @staticmethod
    def _extract_tag_args(text: str) -> list[str]:
        tags = extract_tags(text)
        if not tags:
            tags = [part.lstrip("#") for part in (text or "").split() if part.strip()]
        clean: list[str] = []
        seen: set[str]   = set()
        for tag in tags:
            tag = tag.strip()
            key = tag.casefold()
            if tag and key not in seen:
                seen.add(key)
                clean.append(tag)
        return clean

    async def tag_note(self, user_id: str, args: str, context: PendoContext) -> CommandMessage:
        parts = args.split(maxsplit=1)
        if len(parts) < 2:
            return {"status": "error", "message": "❌ 用法: /pendo note tag <id> #标签"}
        note_id        = parts[0].strip()
        requested_tags = self._extract_tag_args(parts[1])
        if not requested_tags:
            return {"status": "error", "message": "❌ 请提供标签"}
        try:
            tags = cast(
                list[str],
                normalize_note_fields({"tags": requested_tags}, partial=True)["tags"],
            )
        except ValueError as exc:
            return {"status": "error", "message": f"❌ {exc}"}
        note, wrong_type = await self._db_get_typed_item_or_message(
            note_id, user_id, ItemType.NOTE.value, "笔记"
        )
        if wrong_type:
            return wrong_type
        note   = cast(NoteItem, note)
        merged = list(note.tags or [])
        seen   = {tag.casefold() for tag in merged}
        for tag in tags:
            if tag.casefold() not in seen:
                seen.add(tag.casefold())
                merged.append(tag)
        if merged == list(note.tags or []):
            return {"status": "info", "message": "ℹ️ 这些标签已存在，笔记未改变"}
        try:
            updates = normalize_note_fields(
                {"tags": merged, "type": ItemType.NOTE.value}, partial=True
            )
        except ValueError as exc:
            return {"status": "error", "message": f"❌ {exc}"}
        saved_tags = cast(list[str], updates["tags"])
        await self._db_update_with_log(
            note_id,
            updates,
            user_id,
            action           = "edit_note",
            expected_version = note.version,
        )
        return {
            "status": "success",
            "message": f"✅ 已更新标签: {ItemFormatter.format_tags(saved_tags)}",
        }

    async def untag_note(self, user_id: str, args: str, context: PendoContext) -> CommandMessage:
        parts = args.split(maxsplit=1)
        if len(parts) < 2:
            return {"status": "error", "message": "❌ 用法: /pendo note untag <id> #标签"}
        note_id        = parts[0].strip()
        requested_tags = self._extract_tag_args(parts[1])
        if not requested_tags:
            return {"status": "error", "message": "❌ 请提供标签"}
        try:
            normalized_tags = cast(
                list[str],
                normalize_note_fields({"tags": requested_tags}, partial=True)["tags"],
            )
        except ValueError as exc:
            return {"status": "error", "message": f"❌ {exc}"}
        tags = {tag.casefold() for tag in normalized_tags}
        note, wrong_type = await self._db_get_typed_item_or_message(
            note_id, user_id, ItemType.NOTE.value, "笔记"
        )
        if wrong_type:
            return wrong_type
        note      = cast(NoteItem, note)
        remaining = [tag for tag in (note.tags or []) if tag.casefold() not in tags]
        if remaining == list(note.tags or []):
            return {"status": "info", "message": "ℹ️ 笔记中没有这些标签，未做修改"}
        try:
            updates = normalize_note_fields(
                {"tags": remaining, "type": ItemType.NOTE.value}, partial=True
            )
        except ValueError as exc:
            return {"status": "error", "message": f"❌ {exc}"}
        saved_tags = cast(list[str], updates["tags"])
        await self._db_update_with_log(
            note_id,
            updates,
            user_id,
            action           = "edit_note",
            expected_version = note.version,
        )
        return {
            "status": "success",
            "message": f"✅ 已更新标签: {ItemFormatter.format_tags(saved_tags) or '无'}",
        }

    async def link_note(self, user_id: str, args: str, context: PendoContext) -> CommandMessage:
        parts = args.split(maxsplit=1)
        if len(parts) < 2:
            return {"status": "error", "message": "❌ 用法: /pendo note link <id> <关联条目ID>"}
        note_id   = parts[0].strip()
        target_id = parts[1].strip()
        if error := self._single_token_error(
            target_id, "❌ 关联命令只接受一个目标ID\n例如: /pendo note link note123 event456"
        ):
            return error
        if target_id == note_id:
            return {"status": "error", "message": "❌ 笔记不能关联自身"}
        note, wrong_type = await self._db_get_typed_item_or_message(
            note_id, user_id, ItemType.NOTE.value, "笔记"
        )
        if wrong_type:
            return wrong_type
        note = cast(NoteItem, note)
        try:
            new_refs, _ = await self._resolve_note_references(
                user_id, [target_id], source_note_id=note_id
            )
        except ValueError as exc:
            return {"status": "error", "message": f"❌ {exc}"}
        existing_refs = self._merge_references(note.references, [])
        merged_refs   = self._merge_references(existing_refs, new_refs)
        if merged_refs == existing_refs:
            return {"status": "info", "message": "ℹ️ 该关联已存在，笔记未改变"}
        try:
            updates = normalize_note_fields(
                {
                    "references": merged_refs,
                    "related_items": self._related_from_references(merged_refs),
                    "type": ItemType.NOTE.value,
                },
                partial=True,
            )
        except ValueError as exc:
            return {"status": "error", "message": f"❌ {exc}"}
        await self._db_update_with_log(
            note_id,
            updates,
            user_id,
            action           = "edit_note",
            expected_version = note.version,
        )
        ref = new_refs[0]
        return {
            "status": "success",
            "message": (
                f"✅ 已关联 {self._type_label(str(ref.get('type') or ''))}: "
                f"{ref.get('title') or '无标题'} `{public_id(ref.get('id'))}`"
            ),
        }

    async def delete_note(self, user_id: str, args: str, context: PendoContext) -> CommandMessage:
        """删除笔记

        格式：
        - /pendo note delete <id> -> 删除单个笔记
        - /pendo note delete cat:xxx -> 删除整个分类下的笔记
        - 不允许按标签删除
        """
        if not args:
            return {"status": "error", "message": "❌ 请指定ID或cat:xxx"}

        try:
            tokens = shlex.split(args.strip(), comments=False, posix=True)
        except ValueError:
            return {"status": "error", "message": "❌ 删除参数中的引号未闭合"}
        if len(tokens) != 1:
            return {
                "status": "error",
                "message": "❌ 删除笔记只接受一个ID或一个 cat:分类 参数",
            }

        target = tokens[0]
        if target.startswith("#"):
            return {
                "status": "error",
                "message": "❌ 不支持按标签删除，请使用 /pendo note delete <id> 或 /pendo note delete cat:xxx",
            }

        if target.casefold().startswith("cat:"):
            category = target[4:].strip()
            if not category:
                return {"status": "error", "message": "❌ 分类名不能为空"}
            return await self._delete_category_notes(user_id, category, context)

        note_id = target
        note, wrong_type = await self._db_get_typed_item_or_message(
            note_id, user_id, ItemType.NOTE.value, "笔记"
        )
        if wrong_type:
            return wrong_type
        note = cast(NoteItem, note)

        await self._db_soft_delete_with_log(note_id, user_id, item_type=ItemType.NOTE.value)

        return {
            "status": "success",
            "message": f"🗑️ 已删除: {note.title or '无标题'}\n\n{PendoConfig.UNDO_HINT}",
        }

    async def _delete_category_notes(
        self, user_id: str, category: str, context: PendoContext
    ) -> CommandMessage:
        """删除整个分类下的笔记"""
        filters  = {"type": ItemType.NOTE.value, "category": category}
        note_ids = await run_sync(self.db.get_item_ids, user_id, filters)

        if not note_ids:
            return {"status": "success", "message": f"📂 分类 {category} 下没有笔记"}

        deleted_count = await self._db_batch_soft_delete_with_log(
            note_ids,
            user_id,
            ItemType.NOTE.value,
            "delete_note",
        )

        return {
            "status": "success",
            "message": (
                f"🗑️ 已删除分类 {category} 下的 {deleted_count} 个笔记\n\n{PendoConfig.UNDO_HINT}"
            ),
        }
