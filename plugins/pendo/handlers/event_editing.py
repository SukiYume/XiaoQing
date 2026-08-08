"""日程编辑指令解析、AI 候选合并和字段级语义门禁。"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, Protocol, cast
from zoneinfo import ZoneInfo

from ..models.item import EventItem
from ..utils.time_utils import TimezoneHelper, resolve_source_wall_time
from ..utils.validators import normalize_event_fields
from .event_support import event_display_timezone

logger = logging.getLogger(__name__)


class EventEditAIParserProtocol(Protocol):
    """编辑解析只依赖 AI 服务的局部契约。"""

    async def parse_event_with_ai(
        self,
        text: str,
        user_id: str,
        *,
        partial: bool = False,
        fallback_text: str | None = None,
    ) -> dict[str, Any]: ...

    def parse_natural_language(self, text: str, user_id: str) -> dict[str, Any]: ...


class EventEditingMixin:
    """把编辑解析与命令分发、持久化生命周期分离。"""

    _TITLE_SCAFFOLD_MARKERS = ("[编辑现有日程]", "原标题", "原时间", "用户修改指令")
    _TITLE_RENAME_RE = re.compile(r"(改名|重命名|标题|名称|叫做|名字)")
    _TITLE_SCHEDULE_RE = re.compile(
        r"(提醒|提前|分钟|小时|天|周|今天|明天|后天|上午|中午|下午|晚上|\d{1,2}[点时:：])"
    )
    _CATEGORY_EDIT_RE = re.compile(r"(分类|归类|类别|类目)")
    _CONTENT_EDIT_RE = re.compile(r"(内容|描述|详情|补充|说明)")
    _LOCATION_EDIT_RE = re.compile(
        r"(@|地点|位置|场地|地址|(?:会场|会议地点)\s*(?:改为|改成|改到|在|[:：]))"
    )
    _NOTES_EDIT_RE = re.compile(r"(?:备注|说明)\s*(?:改为|改成|设为|设置为|为|成|[:：])?\s*(.+)")
    _EDIT_VALUE_STOP_CHARS = " ，,。；;\n\t"
    _AI_EDIT_FIELDS = (
        "title",
        "content",
        "start_time",
        "end_time",
        "location",
        "category",
        "tags",
    )
    ai_parser: EventEditAIParserProtocol

    async def _parse_updates(self, changes: str, current_event: EventItem) -> dict[str, Any]:
        """解析更新内容

        尝试使用AI解析，失败时降级到规则解析。
        通过 prompt 指示 AI 不要随意修改标题，避免把编辑指令误设为标题。
        """
        explicit_updates = self._parse_explicit_edit_updates(changes, current_event)
        parsed = await self._parse_ai_edit_updates(changes, current_event)
        updates = self._merge_ai_edit_updates(
            changes,
            current_event,
            parsed,
            explicit_updates,
        )

        if parsed.get("remind_times") and not explicit_updates:
            updates["remind_times"] = parsed["remind_times"]

        if (
            not explicit_updates
            and parsed.get("notes") is not None
            and parsed.get("notes") != getattr(current_event, "notes", None)
        ):
            updates["notes"] = parsed["notes"]

        heuristic_notes = self._extract_notes_update(changes)
        if heuristic_notes is not None and heuristic_notes != getattr(current_event, "notes", None):
            updates["notes"] = heuristic_notes

        return updates

    async def _parse_ai_edit_updates(
        self, changes: str, current_event: EventItem
    ) -> dict[str, Any]:
        """请 AI 只返回用户明确要修改的字段，失败时回退规则解析。"""
        current_title = getattr(current_event, "title", "") or ""
        current_start = getattr(current_event, "start_time", "") or ""
        edit_prompt = (
            f"[编辑现有日程] 原标题：{current_title}，原时间：{current_start}。"
            f"用户修改指令：{changes}。"
            f"请只返回需要修改的字段，未提及的字段不要更改。"
            f'若用户未明确要求修改标题（如使用"改名""重命名""标题改为"等词），'
            f"则不要返回title字段。"
        )

        try:
            parsed = await self.ai_parser.parse_event_with_ai(
                edit_prompt,
                current_event.owner_id,
                partial=True,
                fallback_text=changes,
            )
        except Exception as exc:
            logger.warning(
                "AI解析失败，降级到规则解析 error_type=%s",
                type(exc).__name__,
            )
            parsed = self.ai_parser.parse_natural_language(changes, current_event.owner_id)
        return parsed

    def _merge_ai_edit_updates(
        self,
        changes: str,
        current_event: EventItem,
        parsed: dict[str, Any],
        explicit_updates: dict[str, Any],
    ) -> dict[str, Any]:
        """在显式规则结果上补入通过语义校验的 AI 字段。"""
        updates = dict(explicit_updates)
        explicit_only_fields = {"title", "content", "location", "category", "tags"}
        for key in self._AI_EDIT_FIELDS:
            if key in updates:
                continue
            if explicit_updates and key in explicit_only_fields:
                continue
            candidate = parsed.get(key)
            current_val = getattr(current_event, key, None)
            if candidate in (None, "", [], {}) or candidate == current_val:
                continue
            if not self._should_apply_ai_edit_field(key, changes, current_event, candidate):
                continue
            updates[key] = candidate
        return updates

    @classmethod
    def _should_apply_ai_edit_field(
        cls,
        key: str,
        changes: str,
        current_event: EventItem,
        candidate: Any,
    ) -> bool:
        """对 AI 候选值应用字段级语义门禁。"""
        if key == "title":
            return cls._should_apply_title_update(changes, current_event, candidate)
        if key == "content":
            return cls._should_apply_content_update(changes, candidate)
        if key == "category":
            return cls._should_apply_category_update(changes, candidate)
        if key == "location":
            return cls._should_apply_location_update(changes, candidate)
        return True

    @classmethod
    def _parse_explicit_edit_updates(cls, changes: str, current_event: EventItem) -> dict[str, Any]:
        updates: dict[str, Any] = {}

        title = cls._extract_edit_value(
            changes,
            (
                r"(?:标题|名字|名称)\s*(?:改为|改成|改到|设为|设置为|重命名为|:|：)\s*([^，,。；;\n]+)",
                r"(?:改名|重命名)\s*(?:为|成)?\s*([^，,。；;\n]+)",
            ),
        )
        if title is not None and title != getattr(current_event, "title", None):
            updates["title"] = title

        location = cls._extract_edit_value(
            changes,
            (
                r"(?:地点|位置|场地|地址|会场|会议地点)\s*(?:改为|改成|改到|设为|设置为|在|到|:|：)\s*([^，,。；;\n]+)",
                r"@([^\s，,。；;\n]+)",
            ),
        )
        if location is not None and location != getattr(current_event, "location", None):
            updates["location"] = location

        category = cls._extract_edit_value(
            changes,
            (
                r"(?:分类|归类|类别|类目)\s*(?:改为|改成|设为|设置为|为|成|到|:|：)\s*([^，,。；;\n]+)",
            ),
        )
        if category is not None and category != getattr(current_event, "category", None):
            updates["category"] = category

        content = cls._extract_edit_value(
            changes,
            (r"(?:内容|描述|详情)\s*(?:改为|改成|设为|设置为|为|成|:|：)\s*(.+)",),
        )
        if content is not None and content != getattr(current_event, "content", None):
            updates["content"] = content

        notes = cls._extract_notes_update(changes)
        if notes is not None and notes != getattr(current_event, "notes", None):
            updates["notes"] = notes

        if not any(key in updates for key in ("title", "location", "category", "content", "notes")):
            start_time = cls._extract_start_time_update(changes, current_event)
            if start_time is not None and start_time != getattr(current_event, "start_time", None):
                updates["start_time"] = start_time

        return updates

    @classmethod
    def _extract_edit_value(cls, changes: str, patterns: tuple[str, ...]) -> str | None:
        for pattern in patterns:
            match = re.search(pattern, changes, re.IGNORECASE)
            if not match:
                continue
            value = (match.group(1) or "").strip(cls._EDIT_VALUE_STOP_CHARS)
            return value or None
        return None

    @classmethod
    def _extract_start_time_update(cls, changes: str, current_event: EventItem) -> str | None:
        text = changes.strip()
        match = re.search(
            r"(?:开始时间|时间)\s*(?:改为|改成|改到|设为|设置为|调整到|调整为|到|:|：)\s*(.+)",
            text,
        )
        if not match:
            match = re.match(r"\s*(?:改到|改为|改成|调整到|调整为)\s*(.+)", text)
        if not match:
            return None

        candidate = match.group(1).strip(cls._EDIT_VALUE_STOP_CHARS)
        candidate = cls._normalize_datetime_candidate(candidate, current_event)
        if candidate is None:
            return None
        try:
            normalized = normalize_event_fields({"start_time": candidate}, partial=True).get(
                "start_time"
            )
            return normalized if isinstance(normalized, str) else None
        except ValueError:
            return None

    @staticmethod
    def _normalize_datetime_candidate(candidate: str, current_event: EventItem) -> str | None:
        current_start = getattr(current_event, "start_time", None)
        event_timezone = event_display_timezone(current_event)
        try:
            current_dt = (
                TimezoneHelper.parse(current_start, event_timezone) if current_start else None
            )
        except (TypeError, ValueError):
            current_dt = None

        match = re.fullmatch(
            r"(\d{4}-\d{2}-\d{2})(?:[T\s]+)(\d{1,2}):(\d{2})(?::(\d{2}))?",
            candidate,
        )
        if match:
            hour = int(match.group(2))
            minute = match.group(3)
            second = match.group(4) or "00"
            wall_time = f"{match.group(1)}T{hour:02d}:{minute}:{second}"
            return EventEditingMixin._attach_event_timezone(wall_time, event_timezone)

        match = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", candidate)
        if match and current_dt is not None:
            hour = int(match.group(1))
            minute = match.group(2)
            second = match.group(3) or "00"
            wall_time = f"{current_dt.date().isoformat()}T{hour:02d}:{minute}:{second}"
            return EventEditingMixin._attach_event_timezone(wall_time, event_timezone)

        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", candidate):
            time_part = "00:00:00"
            if current_dt is not None:
                time_part = current_dt.time().isoformat(timespec="seconds")
            return EventEditingMixin._attach_event_timezone(
                f"{candidate}T{time_part}",
                event_timezone,
            )

        return candidate or None

    @staticmethod
    def _attach_event_timezone(wall_time: str, event_timezone: ZoneInfo) -> str | None:
        """把日程墙钟时间解析成唯一时刻，并拒绝 DST 缺口和重叠。"""
        try:
            normalized = str(
                normalize_event_fields(
                    {"start_time": wall_time},
                    partial=True,
                )["start_time"]
            )
            parsed = datetime.fromisoformat(normalized)
            resolved = cast(
                datetime,
                resolve_source_wall_time(parsed, "start_time", event_timezone),
            )
            return resolved.isoformat(timespec="seconds")
        except (KeyError, TypeError, ValueError):
            return None

    @classmethod
    def _should_apply_title_update(
        cls, changes: str, current_event: EventItem, candidate: Any
    ) -> bool:
        if not isinstance(candidate, str):
            return False
        title = candidate.strip()
        if not title or title in ("未命名事件", "无标题"):
            return False
        if any(marker in title for marker in cls._TITLE_SCAFFOLD_MARKERS):
            return False

        current_title = (getattr(current_event, "title", "") or "").strip()
        if title == current_title:
            return False

        normalized_changes = changes.strip()
        if cls._TITLE_RENAME_RE.search(normalized_changes):
            return True

        if title == normalized_changes:
            return cls._TITLE_SCHEDULE_RE.search(normalized_changes) is None

        # 没有明确改名指令时，不接受 AI 从时间、地点或备注编辑中补写的新标题。
        return False

    @classmethod
    def _should_apply_category_update(cls, changes: str, candidate: Any) -> bool:
        if not isinstance(candidate, str):
            return False
        category = candidate.strip()
        if not category or category == "未分类":
            return False
        return cls._CATEGORY_EDIT_RE.search(changes) is not None

    @classmethod
    def _should_apply_content_update(cls, changes: str, candidate: Any) -> bool:
        if not isinstance(candidate, str):
            return False
        content = candidate.strip()
        if not content:
            return False
        if any(marker in content for marker in cls._TITLE_SCAFFOLD_MARKERS):
            return False
        return cls._CONTENT_EDIT_RE.search(changes) is not None

    @classmethod
    def _should_apply_location_update(cls, changes: str, candidate: Any) -> bool:
        if not isinstance(candidate, str):
            return False
        location = candidate.strip()
        if not location:
            return False
        # AI 可能从“备注从北京南坐 G123 去会场”推断出地点；
        # 除非用户明确要求改地点，否则只应作为备注。
        has_note_directive = cls._NOTES_EDIT_RE.search(changes) is not None
        has_location_directive = cls._LOCATION_EDIT_RE.search(changes) is not None
        return has_location_directive or not has_note_directive

    @classmethod
    def _extract_notes_update(cls, changes: str) -> str | None:
        match = cls._NOTES_EDIT_RE.search(changes)
        if not match:
            return None
        notes = match.group(1).strip(cls._EDIT_VALUE_STOP_CHARS)
        return notes or None
