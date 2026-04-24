"""
日程(Event)处理器
处理日程相关的所有操作，使用AI解析自然语言
"""

from typing import Any, TYPE_CHECKING, Protocol, cast
from datetime import datetime, timedelta
import logging
import re
from core.plugin_base import run_sync
from ..utils.db_ops import DbOpsMixin
from ..utils.error_handlers import error_result, handle_command_errors
from ..config import PendoConfig
from ..utils.time_utils import parse_event_time_range, TimezoneHelper, now_in_timezone, parse_remind_times
from ..models.item import EventItem, ItemType
from ..core.types import PendoContext, CommandMessage
from ..core.router import TOP_LEVEL_REDIRECTS
from ..utils.settings_utils import resolve_default_category
from ..utils.validators import build_remind_times_from_rules, merge_milestone_metadata
from .event_support import (
    apply_offsets,
    calculate_remind_offsets,
    ensure_start_time_reminder,
    ensure_event_reminders,
    ensure_event_reminder_rules,
    format_conflicts,
    format_event_created,
    format_event_reminders,
    format_milestone_event_created,
    format_recurring_event_created,
    get_remind_status,
    group_reminders_by_milestone,
    recalculate_event_reminders,
)
from ..utils.formatters import (
    ItemFormatter,
    MessageBuilder,
)
from ..utils.session_utils import safe_create_session

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ..services.db import Database

EventRows = tuple[list[EventItem], list[EventItem]]
TIME_RANGE_KEYWORDS = frozenset(["today", "tomorrow", "week", "month", "year"])
TIME_RANGE_RE = re.compile(r"^(last\d+d|\d{4}|\d{4}-\d{2}|\d{4}-\d{2}-\d{2}|\d{2}-\d{2})$")


class EventAIParserProtocol(Protocol):
    async def parse_event_with_ai(
        self,
        text: str,
        user_id: str,
        *,
        partial: bool = False,
        fallback_text: str | None = None,
    ) -> dict[str, Any]: ...

    def parse_natural_language(self, text: str, user_id: str) -> dict[str, Any]: ...

    def build_remind_times_from_offsets(self, start_time: str, offsets: list[Any]) -> list[str]: ...

    def build_remind_times_from_description(
        self, description: str, base_time: str
    ) -> list[str]: ...

    def build_reminder_rules_from_description(
        self, description: str
    ) -> list[dict[str, int]]: ...


class ReminderServiceProtocol(Protocol):
    def detect_conflict(
        self, user_id: str, start_time: str, end_time: str | None
    ) -> list[dict[str, Any]]: ...


class EventHandler(DbOpsMixin):
    """日程处理器"""

    _TITLE_SCAFFOLD_MARKERS = ("[编辑现有日程]", "原标题", "原时间", "用户修改指令")
    _TITLE_RENAME_RE = re.compile(r"(改名|重命名|标题|名称|叫做|名字)")
    _TITLE_SCHEDULE_RE = re.compile(
        r"(提醒|提前|分钟|小时|天|周|今天|明天|后天|上午|中午|下午|晚上|\d{1,2}[点时:：])"
    )
    _CATEGORY_EDIT_RE = re.compile(r"(分类|归类|类别|类目)")
    _CONTENT_EDIT_RE = re.compile(r"(内容|描述|详情|补充|说明)")
    _NOTES_EDIT_RE = re.compile(r"(?:备注(?:改为|改成|[:：])?|备注)\s*(.+)")
    _MILESTONE_VERBS = ("改成", "改到", "改为", "调整到", "挪到", "挪成")

    db: "Database"
    ai_parser: EventAIParserProtocol
    reminder_service: ReminderServiceProtocol

    def __init__(
        self,
        db: "Database",
        ai_parser: EventAIParserProtocol,
        reminder_service: ReminderServiceProtocol,
    ):
        self.db = db
        self.ai_parser = ai_parser
        self.reminder_service = reminder_service

    async def _fetch_event_rows(self, user_id: str, start_date: str, end_date: str) -> EventRows:
        rows = await run_sync(self.db.items.get_events_for_range, user_id, start_date, end_date)
        return cast(EventRows, rows)

    @handle_command_errors
    async def handle(
        self, user_id: str, args: str, context: PendoContext, group_id: int | None = None
    ) -> CommandMessage:
        """处理日程相关命令"""
        parts = args.split(maxsplit=1)
        if not parts:
            return await self.list_events(user_id, "today", context)

        command = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""

        handlers = {
            "add": lambda: self.add_event(user_id, rest, context, group_id),
            "view": lambda: self.view_event(user_id, rest, context),
            "edit": lambda: self.edit_event(user_id, rest, context),
            "delete": lambda: self.delete_event(user_id, rest, context),
            "list": lambda: self.list_events(user_id, rest or "today", context),
            "reminders": lambda: self.handle_reminders(user_id, rest, context),
        }

        handler = handlers.get(command)
        if handler:
            return await handler()

        # 常见拼写错误
        common_typos = {
            "reminder": "reminders",
        }
        if command in common_typos:
            correct = common_typos[command]
            return error_result(f"❌ 没有这个命令\n\n正确用法是: /pendo event {correct} <id>")

        # 顶层命令误放到 event 下（如 /pendo event confirm xxx）
        if command in TOP_LEVEL_REDIRECTS:
            return error_result(f"❌ 正确用法:\n\n{TOP_LEVEL_REDIRECTS[command]}")

        # 未知命令：仅当第一个词看起来是时间范围时才 fallback 到 list_events
        # 否则直接报错，避免把 "confirm xxx" 之类的误操作渲染成列表
        if command in TIME_RANGE_KEYWORDS or TIME_RANGE_RE.match(command) or ".." in args:
            return await self.list_events(user_id, args, context)

        return error_result(
            f"❌ 未知子命令: event {command}\n\n可用命令: add, list, view, edit, delete, reminders"
        )

    # ==================== 添加日程 ====================

    async def add_event(
        self, user_id: str, text: str, context: PendoContext, group_id: int | None = None
    ) -> CommandMessage:
        """从文本添加日程"""
        if not text:
            return {
                "status": "error",
                "message": "❌ 请提供日程内容\n例如: /pendo event add 明天9点开会",
            }

        # AI解析自然语言
        parsed = cast(
            dict[str, Any], await self.ai_parser.parse_event_with_ai(text, user_id)
        )
        if group_id:
            parsed["context"] = {"group_id": group_id}

        result = await self.create_event(user_id, parsed, context)

        # 处理需要补充信息的情况
        if result.get("status") == "need_info":
            await safe_create_session(
                context,
                initial_data={
                    "type": PendoConfig.SESSION_TYPE_EVENT_INFO,
                    "owner_id": user_id,
                    "data": result.get("data", parsed),
                },
                timeout=PendoConfig.SESSION_TIMEOUT_SECONDS,
            )
        elif result.get("status") == "need_confirm":
            await safe_create_session(
                context,
                initial_data={
                    "type": PendoConfig.SESSION_TYPE_EVENT_CONFLICT,
                    "owner_id": user_id,
                    "data": result.get("data", parsed),
                },
                timeout=PendoConfig.SESSION_TIMEOUT_SECONDS,
            )
        return result

    async def create_event(
        self,
        user_id: str,
        parsed_data: dict[str, Any],
        context: PendoContext,
        allow_conflict: bool = False,
    ) -> CommandMessage:
        """创建日程"""
        milestones = parsed_data.get("milestones")

        # I-1修复：milestones存在时，start_time可以从第一个里程碑推断，不触发need_info
        if not parsed_data.get("start_time"):
            if milestones and isinstance(milestones, list) and len(milestones) >= 1:
                parsed_data["start_time"] = milestones[0].get("time")
            if not parsed_data.get("start_time"):
                return {
                    "status": "need_info",
                    "message": "❌ 请问日程的开始时间是？(例如: 明天9点)",
                    "pending_action": "create_event",
                    "missing_fields": ["start_time"],
                    "data": parsed_data,
                }

        if not str(parsed_data.get("category") or "").strip():
            parsed_data["category"] = resolve_default_category(self.db, user_id)

        # 确保有提醒时间
        remind_times = ensure_event_reminders(
            parsed_data,
            build_from_offsets=self.ai_parser.build_remind_times_from_offsets,
        )
        reminder_rules = ensure_event_reminder_rules(parsed_data, remind_times)
        parsed_data["reminder_rules"] = reminder_rules
        if not (milestones and isinstance(milestones, list) and len(milestones) >= 2):
            remind_times = build_remind_times_from_rules(parsed_data["start_time"], reminder_rules)

        # 处理重复日程（rrule优先级高于milestones，避免AI同时生成两者时走错路径）
        if parsed_data.get("rrule"):
            return await self._create_recurring_event(user_id, parsed_data, remind_times)

        # 处理多时间节点日程（2个及以上节点）
        if milestones and isinstance(milestones, list) and len(milestones) >= 2:
            return await self._create_milestone_event(
                user_id, parsed_data, remind_times, allow_conflict
            )

        # 创建单次日程
        return await self._create_single_event(user_id, parsed_data, remind_times, allow_conflict)

    async def _check_conflict(
        self,
        user_id: str,
        parsed_data: dict[str, Any],
        start_time: str,
        end_time: str | None,
        allow_conflict: bool,
    ) -> dict[str, Any] | None:
        """检查时间冲突，有冲突时返回 need_confirm dict，否则返回 None"""
        if allow_conflict:
            return None
        conflicts = await run_sync(
            self.reminder_service.detect_conflict, user_id, start_time, end_time
        )
        if conflicts:
            return {
                "status": "need_confirm",
                "message": format_conflicts(conflicts, parsed_data),
                "pending_action": "create_event_with_conflict",
                "data": parsed_data,
            }
        return None

    async def _create_single_event(
        self,
        user_id: str,
        parsed_data: dict[str, Any],
        remind_times: list[str],
        allow_conflict: bool,
    ) -> CommandMessage:
        """创建单次日程"""
        conflict = await self._check_conflict(
            user_id,
            parsed_data,
            parsed_data["start_time"],
            parsed_data.get("end_time"),
            allow_conflict,
        )
        if conflict:
            return conflict

        event_item = EventItem(
            owner_id=user_id,
            title=parsed_data.get("title", "无标题日程"),
            content=parsed_data.get("content", ""),
            start_time=parsed_data["start_time"],
            end_time=parsed_data.get("end_time"),
            location=parsed_data.get("location", ""),
            tags=parsed_data.get("tags", []),
            category=parsed_data.get("category", "未分类"),
            context=parsed_data.get("context", {}),
            remind_times=remind_times,
            reminder_rules=parsed_data.get("reminder_rules", []),
            notes=parsed_data.get("notes", ""),
            created_at=datetime.now().isoformat(),
            updated_at=datetime.now().isoformat(),
        )

        item_id = await self._db_create_with_log(event_item, owner_id=user_id, action="create")
        event_item.id = item_id

        return {
            "status": "success",
            "message": format_event_created(event_item.to_dict()),
            "item_id": item_id,
        }

    async def _create_recurring_event(
        self, user_id: str, parsed_data: dict[str, Any], remind_times: list[str]
    ) -> CommandMessage:
        """创建重复日程"""
        try:
            from dateutil.rrule import rrulestr
            import uuid

            start_dt = datetime.fromisoformat(parsed_data["start_time"])
            end_dt = (
                datetime.fromisoformat(parsed_data["end_time"])
                if parsed_data.get("end_time")
                else None
            )
            duration = end_dt - start_dt if end_dt else None
            start_dt_naive = start_dt.replace(tzinfo=None) if start_dt.tzinfo else start_dt
            now = datetime.now()

            # 如果起始时间已过，用 rrule.after 找到下一个未来实例作为新起点
            if start_dt_naive <= now:
                probe = rrulestr(parsed_data["rrule"], dtstart=start_dt_naive)
                next_dt = probe.after(now, inc=False)
                if next_dt is None:
                    return {
                        "status": "error",
                        "message": "❌ 所有重复实例均已过期，没有未来可创建的日程",
                    }
                start_dt_naive = next_dt

            rrule_obj = rrulestr(parsed_data["rrule"], dtstart=start_dt_naive)
            instances = list(rrule_obj)[: PendoConfig.EVENT_MAX_RRULE_COUNT]

            if not instances:
                return {"status": "error", "message": "❌ 没有生成任何重复实例"}

            reminder_rules = parsed_data.get("reminder_rules", [])

            # 生成父ID
            parent_id = uuid.uuid4().hex[:8]
            created_ids = []

            for instance_dt in instances:
                instance_end_dt = instance_dt + duration if duration else None
                instance_item = EventItem(
                    owner_id=user_id,
                    title=parsed_data.get("title", "无标题日程"),
                    content=parsed_data.get("content", ""),
                    start_time=instance_dt.isoformat(),
                    end_time=instance_end_dt.isoformat() if instance_end_dt else None,
                    location=parsed_data.get("location", ""),
                    tags=parsed_data.get("tags", []),
                    category=parsed_data.get("category", "未分类"),
                    context=parsed_data.get("context", {}),
                    remind_times=build_remind_times_from_rules(
                        instance_dt.isoformat(),
                        reminder_rules,
                    ),
                    reminder_rules=reminder_rules,
                    notes=parsed_data.get("notes", ""),  # I-9修复：重复事件也传入notes
                    parent_id=parent_id,
                    rrule=parsed_data["rrule"],
                    created_at=datetime.now().isoformat(),
                    updated_at=datetime.now().isoformat(),
                )

                instance_id = f"{parent_id}_{instance_dt.strftime('%Y%m%d')}"
                instance_item.id = instance_id

                await run_sync(self.db.items.insert_item, instance_item, instance_id)
                created_ids.append(instance_id)

            # 记录日志
            await run_sync(
                self.db.logs.log_operation,
                user_id=user_id,
                action="create_recurring",
                item_type="event",
                item_id=parent_id,
                details={"title": parsed_data.get("title"), "instances": len(created_ids)},
            )

            return {
                "status": "success",
                "message": format_recurring_event_created(
                    parsed_data.get("title", "无标题"),
                    len(created_ids),
                    len(reminder_rules),
                    parent_id,
                ),
                "item_id": parent_id,
            }
        except Exception as e:
            logger.exception("创建重复日程失败: %s", e)
            return {"status": "error", "message": f"❌ 创建重复日程失败: {str(e)}"}

    async def _create_milestone_event(
        self,
        user_id: str,
        parsed_data: dict[str, Any],
        remind_times: list[str],
        allow_conflict: bool = False,
    ) -> CommandMessage:
        """创建多时间节点事件（单条记录，多个里程碑）"""
        milestones = parsed_data["milestones"]
        start_time = parsed_data.get("start_time") or milestones[0]["time"]
        end_time = parsed_data.get("end_time") or milestones[-1]["time"]

        conflict = await self._check_conflict(
            user_id, parsed_data, start_time, end_time, allow_conflict
        )
        if conflict:
            return conflict

        event_item = EventItem(
            owner_id=user_id,
            title=parsed_data.get("title", "无标题日程"),
            content=parsed_data.get("content", ""),
            start_time=start_time,
            end_time=end_time,
            location=parsed_data.get("location", ""),
            tags=parsed_data.get("tags", []),
            category=parsed_data.get("category", "未分类"),
            context=parsed_data.get("context", {}),
            remind_times=remind_times,
            reminder_rules=parsed_data.get("reminder_rules", []),
            milestones=milestones,
            notes=parsed_data.get("notes", ""),
            created_at=datetime.now().isoformat(),
            updated_at=datetime.now().isoformat(),
        )

        item_id = await self._db_create_with_log(event_item, owner_id=user_id, action="create")
        event_item.id = item_id

        return {
            "status": "success",
            "message": format_milestone_event_created(event_item.to_dict()),
            "item_id": item_id,
        }

    # ==================== 查看日程 ====================

    async def view_event(
        self, user_id: str, event_id: str, context: PendoContext
    ) -> CommandMessage:
        """查看单个事件详情"""
        event_id = (event_id or "").strip()
        if not event_id:
            return {
                "status": "error",
                "message": "❌ 请指定事件ID\n例如: /pendo event view abc12345",
            }

        single_event_id, event, error = await self._resolve_single_event_id_or_message(
            user_id, event_id, allow_series_fallback=False
        )
        if error:
            return error
        if not single_event_id or not event:
            return {"status": "error", "message": f"❌ 找不到日程 {event_id}"}

        title = event.title or "无标题"
        milestones = getattr(event, "milestones", None) or []
        notes = getattr(event, "notes", None) or ""
        remind_times = parse_remind_times(event.remind_times)

        lines = [f"📋 **{title}**", ""]

        if milestones and len(milestones) >= 2:
            lines.append(f"🗺️ 多时间节点事件 ({len(milestones)}个节点)")
            for m in milestones:
                t_str = ItemFormatter.format_datetime(m.get("time", ""), "%m月%d日 %H:%M")
                lines.append(f"  📌 {m.get('name', ''):<10}  {t_str}")
                milestone_notes = str(m.get("notes", "") or "").strip()
                if milestone_notes:
                    lines.append(f"     📝 {milestone_notes}")
        else:
            time_str = ItemFormatter.format_time_range(event.start_time, event.end_time)
            event_type = (
                "🔄 重复日程"
                if getattr(event, "rrule", None) or getattr(event, "parent_id", None)
                else "📆 单次事件"
            )
            lines.append(f"{event_type}")
            lines.append(f"⏰ {time_str}")

        if event.location:
            lines.append(f"📍 {event.location}")
        if notes:
            label = "📝 全局备注" if milestones and len(milestones) >= 2 else "📝"
            lines.append(f"{label}: {notes}" if label.endswith("全局备注") else f"{label} {notes}")
        if event.tags:
            lines.append(f"🏷️ {', '.join(event.tags)}")

        lines.append("")
        if remind_times:
            lines.append(f"🔔 提醒 ({len(remind_times)}个):")
            for t in remind_times[:5]:
                lines.append(f"  ⏰ {ItemFormatter.format_datetime(t, '%m月%d日 %H:%M')}")
            if len(remind_times) > 5:
                lines.append(
                    f"  … 共{len(remind_times)}个提醒，用 /pendo event reminders {event_id} 查看全部"
                )
        else:
            lines.append("🔔 未设置提醒")

        lines.append(f"\n`{event_id}`")
        lines.append(f"💡 /pendo event reminders {event_id} | /pendo event edit {event_id} <内容>")

        return {"status": "success", "message": "\n".join(lines)}

    _CN_WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    _RANGE_LABELS = {
        "today": "今日",
        "今天": "今日",
        "tomorrow": "明日",
        "明天": "明日",
        "week": "未来7天",
        "本周": "本周",
        "month": "未来30天",
        "本月": "本月",
        "year": "本年",
        "今年": "本年",
    }

    @classmethod
    def _format_list_title(cls, time_range: str, start_dt: datetime, end_dt: datetime) -> str:
        """生成人可读的列表标题，附带实际日期范围"""
        label = cls._RANGE_LABELS.get(time_range.strip().lower(), time_range)
        date_range = f"{start_dt.strftime('%m月%d日')}–{end_dt.strftime('%m月%d日')}"
        return f"{label} · {date_range}"

    @staticmethod
    def _format_day_delta(target_dt: datetime, current_dt: datetime) -> str:
        delta_days = (target_dt.date() - current_dt.date()).days
        if delta_days == 0:
            return "今天"
        if delta_days > 0:
            return f"{delta_days}天后"
        return f"{abs(delta_days)}天前"

    @classmethod
    def _format_day_header(cls, target_dt: datetime, current_dt: datetime) -> str:
        weekday = cls._CN_WEEKDAYS[target_dt.weekday()]
        return f"**{target_dt.strftime('%m月%d日')} {weekday}** - {cls._format_day_delta(target_dt, current_dt)}"

    @classmethod
    def _format_milestone_list_item(
        cls,
        event: EventItem,
        milestones: list[dict[str, Any]],
        start_dt: datetime,
        end_dt: datetime,
        current_dt: datetime,
    ) -> tuple[str | None, str]:
        """格式化多节点事件的列表项。返回 (date_str, text)，无匹配时返回 (None, "")。"""
        in_range = []
        for m in milestones:
            try:
                m_dt = datetime.fromisoformat(m.get("time", ""))
                if start_dt <= m_dt <= end_dt:
                    in_range.append((m, m_dt))
            except (ValueError, TypeError):
                pass
        if not in_range:
            return None, ""

        date_str = in_range[0][1].strftime("%Y-%m-%d")
        start_str = ItemFormatter.format_datetime(event.start_time or "", "%m-%d")
        end_str = ItemFormatter.format_datetime(event.end_time, "%m-%d") if event.end_time else ""
        date_range = f"{start_str}~{end_str}" if end_str else start_str

        text = f"• {date_range} {event.title or '无标题'} 🗺️{len(milestones)}节点"
        if event.location:
            text += f" @ {ItemFormatter.truncate_content(event.location, 15)}"
        text += f" `{event.id}`\n"
        for m, m_dt in in_range:
            m_str = ItemFormatter.format_datetime(m["time"], "%m-%d")
            text += f"  📌 {m_str} {m.get('name', '')} - {cls._format_day_delta(m_dt, current_dt)}\n"
        return date_str, text

    @staticmethod
    def _format_simple_list_item(
        event: EventItem, current_dt: datetime,
    ) -> tuple[str | None, str]:
        """格式化单次事件的列表项。返回 (date_str, text)。"""
        if not event.start_time:
            return None, ""
        ev_start_dt = datetime.fromisoformat(event.start_time)
        date_str = ev_start_dt.strftime("%Y-%m-%d")
        time_str = ItemFormatter.format_time_range(event.start_time, event.end_time)
        text = f"• {time_str} {event.title or '无标题'}"
        if event.location:
            text += f" @ {ItemFormatter.truncate_content(event.location, 15)}"
        text += f" `{event.id}`\n"
        return date_str, text

    async def list_events(
        self, user_id: str, time_range: str, context: PendoContext
    ) -> CommandMessage:
        """列出日程

        支持额外过滤参数 (可与时间范围组合使用):
        - cat:xxx  -> 按分类筛选
        - #tag     -> 按标签筛选
        """
        # 如果传入的是事件ID，转发到 view
        if self._looks_like_id(time_range.strip()):
            return await self.view_event(user_id, time_range.strip(), context)

        # 解析额外过滤参数
        cat_filter = None
        tag_filter = None
        filter_parts = time_range.split()
        clean_parts = []
        for part in filter_parts:
            if part.startswith("cat:"):
                cat_filter = part[4:]
            elif re.match(r"^#\w+$", part):
                tag_filter = part[1:]
            else:
                clean_parts.append(part)
        time_range = " ".join(clean_parts)

        try:
            start_date, end_date = parse_event_time_range(time_range)
            normal_events, repeat_events = await self._fetch_event_rows(
                user_id, start_date, end_date
            )
            events = normal_events + repeat_events

            # 时间过滤
            # 多节点事件用区间重叠；单次事件只看 start_time
            start_dt, end_dt = datetime.fromisoformat(start_date), datetime.fromisoformat(end_date)
            events = [e for e in events if self._event_in_range(e, start_dt, end_dt)]

            # 分类/标签过滤
            if cat_filter:
                events = [e for e in events if (e.category or "") == cat_filter]
            if tag_filter:
                events = [e for e in events if tag_filter in (e.tags or [])]

            def _effective_sort_time(e: EventItem) -> str:
                m_list = e.milestones if hasattr(e, "milestones") else []
                if m_list and len(m_list) >= 2:
                    for m in m_list:
                        try:
                            m_dt = datetime.fromisoformat(m.get("time", ""))
                            if start_dt <= m_dt <= end_dt:
                                return m_dt.isoformat()
                        except (ValueError, TypeError):
                            pass
                return e.start_time or ""

            events.sort(key=_effective_sort_time)

            # 构建过滤描述
            filter_labels = []
            if cat_filter:
                filter_labels.append(f"分类:{cat_filter}")
            if tag_filter:
                filter_labels.append(f"#{tag_filter}")
            filter_suffix = f" [{', '.join(filter_labels)}]" if filter_labels else ""

            if not events:
                title = self._format_list_title(time_range, start_dt, end_dt)
                return {
                    "status": "success",
                    "message": f"🗓️ {title}{filter_suffix} 没有日程安排\n\n💡 用 /pendo event add <内容> 添加日程",
                }

            # 格式化输出
            title = self._format_list_title(time_range, start_dt, end_dt)
            message = f"🗓️ **{title}**{filter_suffix} (共{len(events)}项)\n"
            current_date = None
            current_dt = now_in_timezone(user_id, self.db)

            for event in events:
                milestones = event.milestones if hasattr(event, "milestones") else []
                if milestones and len(milestones) >= 2:
                    date_str, text = self._format_milestone_list_item(
                        event, milestones, start_dt, end_dt, current_dt
                    )
                else:
                    date_str, text = self._format_simple_list_item(event, current_dt)
                if not text:
                    continue
                if date_str != current_date:
                    current_date = date_str
                    header_dt = datetime.fromisoformat(date_str) if date_str else current_dt
                    message += f"\n{self._format_day_header(header_dt, current_dt)}\n"
                message += text

            message += "\n💡 /pendo event reminders <id> 查看提醒 · event edit <id> <内容> 编辑"

            return {"status": "success", "message": message}
        except Exception as e:
            logger.exception("Failed to list events: %s", e)
            return {"status": "error", "message": f"❌ 获取日程失败: {str(e)}"}

    # ==================== 编辑日程 ====================

    async def edit_event(self, user_id: str, args: str, context: PendoContext) -> CommandMessage:
        """编辑日程"""
        parts = args.split(maxsplit=1)
        if len(parts) < 2:
            return {"status": "error", "message": "❌ 用法: /pendo event edit <id> <修改内容>"}

        event_id, changes = parts[0], parts[1]
        single_event_id, _, error = await self._resolve_single_event_id_or_message(
            user_id, event_id, allow_series_fallback=True
        )
        if error:
            return error
        if single_event_id:
            return await self._edit_single_instance(user_id, single_event_id, changes)
        return await self._edit_all_instances(user_id, event_id, changes)

    async def _edit_single_instance(
        self, user_id: str, instance_id: str, changes: str
    ) -> CommandMessage:
        """编辑单个日程实例"""
        try:
            event, wrong_type = await self._db_get_typed_item_or_message(
                instance_id, user_id, ItemType.EVENT.value, "日程"
            )
            if wrong_type:
                return wrong_type
            if not event:
                return {"status": "error", "message": f"❌ 找不到日程 {instance_id}"}
            event = cast(EventItem, event)

            updates = await self._parse_updates(changes, event)
            updates = self._normalize_event_updates(event, updates)
            if not updates:
                return {"status": "warning", "message": "⚠️ 未识别到有效的修改内容"}

            title = event.title
            if ("start_time" in updates or "milestones" in updates) and "remind_times" not in updates:
                updates["remind_times"] = recalculate_event_reminders(event, updates)
            elif "remind_times" in updates:
                updates["remind_times"] = ensure_start_time_reminder(
                    updates["remind_times"],
                    updates.get("start_time") or event.start_time,
                )

            await self._db_update_with_log(
                instance_id, updates, user_id, action="edit_event"
            )


            return {
                "status": "success",
                "message": f"✅ 已更新日程: {updates.get('title', title)}\n\n💡 /pendo event reminders {instance_id} 查看提醒 | /pendo undo 撤销编辑",
            }
        except Exception as e:
            logger.exception("Failed to edit instance: %s", e)
            return {"status": "error", "message": f"❌ 编辑失败: {str(e)}"}

    async def _edit_all_instances(
        self, user_id: str, parent_id: str, changes: str
    ) -> CommandMessage:
        """编辑所有重复实例"""
        try:
            instances = await self._db_find_instances(user_id, parent_id, columns="id, title, start_time")
            if not instances:
                return {"status": "error", "message": f"❌ 找不到日程 {parent_id}"}

            first_event = cast(
                EventItem | None, await self._db_get_item(instances[0][0], owner_id=user_id)
            )
            if first_event is None:
                return {"status": "error", "message": f"❌ 找不到日程 {parent_id}"}
            updates = await self._parse_updates(changes, first_event)
            if not updates:
                return {"status": "warning", "message": "⚠️ 未识别到有效的修改内容"}

            # 加载所有实例完整对象
            instance_items: dict[str, EventItem | None] = {
                row[0]: cast(EventItem | None, await self._db_get_item(row[0], owner_id=user_id))
                for row in instances
            }

            # 计算每个实例的个性化更新（时间偏移）
            per_instance_updates = self._compute_per_instance_updates(
                updates, first_event, instance_items
            )

            instance_ids = [row[0] for row in instances]

            # 保存旧值快照用于 undo
            old_values = self._snapshot_old_values(first_event, updates)

            # 批量更新
            await self._db_batch_update_items(per_instance_updates, user_id)

            await self._db_log_operation(
                user_id=user_id,
                action="edit_event",
                item_type="event",
                item_id=parent_id,
                details={"updates": updates, "old_values": old_values, "instance_ids": instance_ids},
            )

            return {
                "status": "success",
                "message": f"✅ 已更新重复日程: {updates.get('title', first_event.title)}\n📊 共更新 {len(instance_ids)} 个实例\n\n💡 /pendo event reminders {parent_id} 查看提醒 | /pendo undo 撤销编辑",
            }
        except Exception as e:
            logger.exception("Failed to edit all instances: %s", e)
            return {"status": "error", "message": f"❌ 编辑失败: {str(e)}"}

    def _compute_per_instance_updates(
        self,
        updates: dict[str, Any],
        first_event: EventItem,
        instance_items: dict[str, EventItem | None],
    ) -> dict[str, dict[str, Any]]:
        """根据首实例的更新计算每个子实例的个性化更新（含时间偏移）"""
        new_first_start = (
            datetime.fromisoformat(updates["start_time"]) if "start_time" in updates else None
        )
        old_first_start = (
            datetime.fromisoformat(first_event.start_time)
            if "start_time" in updates and first_event.start_time
            else None
        )
        start_delta = (
            new_first_start - old_first_start if new_first_start and old_first_start else None
        )

        new_duration = self._compute_new_duration(updates, first_event, new_first_start)
        explicit_remind_offsets: list[timedelta] | None = None
        if "remind_times" in updates and first_event.start_time:
            offset_base = new_first_start or datetime.fromisoformat(first_event.start_time)
            explicit_remind_offsets = calculate_remind_offsets(offset_base, updates["remind_times"])

        result: dict[str, dict[str, Any]] = {}
        for item_id, inst in instance_items.items():
            if inst is None:
                continue
            inst_update = dict(updates)

            if start_delta is not None and inst.start_time:
                inst_start = datetime.fromisoformat(inst.start_time) + start_delta
                inst_update["start_time"] = inst_start.isoformat()

                if new_duration is not None:
                    inst_update["end_time"] = (inst_start + new_duration).isoformat()
                elif "end_time" not in updates and inst.end_time and inst.start_time:
                    inst_duration = (
                        datetime.fromisoformat(inst.end_time)
                        - datetime.fromisoformat(inst.start_time)
                    )
                    inst_update["end_time"] = (inst_start + inst_duration).isoformat()

                if explicit_remind_offsets is not None:
                    inst_update["remind_times"] = ensure_start_time_reminder(
                        apply_offsets(inst_start, explicit_remind_offsets),
                        inst_start.isoformat(),
                    )
                else:
                    inst_update["remind_times"] = recalculate_event_reminders(inst, inst_update)
            elif "end_time" in inst_update and inst.start_time and new_duration is not None:
                inst_start = datetime.fromisoformat(inst.start_time)
                inst_update["end_time"] = (inst_start + new_duration).isoformat()
            elif explicit_remind_offsets is not None and inst.start_time:
                inst_start = datetime.fromisoformat(inst.start_time)
                inst_update["remind_times"] = ensure_start_time_reminder(
                    apply_offsets(inst_start, explicit_remind_offsets),
                    inst_start.isoformat(),
                )

            result[item_id] = inst_update
        return result

    def _normalize_event_updates(
        self, event: EventItem, updates: dict[str, Any]
    ) -> dict[str, Any]:
        """在持久化前保持事件字段一致性。"""
        normalized = dict(updates)
        current_milestones = getattr(event, "milestones", None) or []
        explicit_milestones = normalized.get("milestones")

        if explicit_milestones:
            normalized_milestones = self._normalize_milestones(explicit_milestones)
            if normalized_milestones:
                normalized_milestones = cast(
                    list[dict[str, Any]],
                    merge_milestone_metadata(current_milestones, normalized_milestones),
                )
                normalized["milestones"] = normalized_milestones
                normalized["start_time"] = normalized_milestones[0]["time"]
                normalized["end_time"] = normalized_milestones[-1]["time"]
            return normalized

        if current_milestones and "start_time" in normalized and event.start_time:
            shifted = self._shift_milestones(current_milestones, event.start_time, normalized["start_time"])
            if shifted:
                normalized["milestones"] = shifted
                normalized["start_time"] = shifted[0]["time"]
                normalized["end_time"] = shifted[-1]["time"]
        elif current_milestones and "end_time" in normalized:
            shifted = [dict(m) for m in current_milestones]
            if shifted:
                shifted[-1]["time"] = str(normalized["end_time"])
                normalized["milestones"] = self._normalize_milestones(shifted)
                normalized["start_time"] = normalized["milestones"][0]["time"]
                normalized["end_time"] = normalized["milestones"][-1]["time"]

        return normalized

    @staticmethod
    def _normalize_milestones(milestones: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Sort milestone payloads and normalize times to ISO strings."""
        normalized: list[dict[str, Any]] = []
        for milestone in milestones:
            try:
                name = str(milestone.get("name", "")).strip()
                time_str = datetime.fromisoformat(str(milestone.get("time", ""))).isoformat()
            except (TypeError, ValueError):
                continue
            if not name:
                continue
            normalized_row: dict[str, Any] = {"name": name, "time": time_str}
            notes = str(milestone.get("notes", "") or "").strip()
            if notes:
                normalized_row["notes"] = notes
            normalized.append(normalized_row)
        normalized.sort(key=lambda item: item["time"])
        return normalized

    def _shift_milestones(
        self, milestones: list[dict[str, Any]], old_start_time: str, new_start_time: str
    ) -> list[dict[str, Any]]:
        """Shift all milestone times by the same delta as the event start."""
        old_start = datetime.fromisoformat(old_start_time)
        new_start = datetime.fromisoformat(new_start_time)
        delta = new_start - old_start
        shifted: list[dict[str, Any]] = []
        for milestone in milestones:
            try:
                base_time = datetime.fromisoformat(str(milestone.get("time", "")))
            except (TypeError, ValueError):
                continue
            name = str(milestone.get("name", "")).strip()
            if not name:
                continue
            shifted.append({"name": name, "time": (base_time + delta).isoformat()})
        shifted.sort(key=lambda item: item["time"])
        return cast(
            list[dict[str, Any]],
            merge_milestone_metadata(milestones, shifted),
        )

    def _compute_new_duration(
        self, updates: dict[str, Any], first_event: EventItem, new_first_start: datetime | None,
    ) -> timedelta | None:
        """计算更新后的事件时长"""
        if "end_time" in updates and first_event.start_time:
            base_start = new_first_start or datetime.fromisoformat(first_event.start_time)
            if isinstance(updates["end_time"], str):
                return datetime.fromisoformat(updates["end_time"]) - base_start
        elif "start_time" in updates and first_event.end_time and first_event.start_time:
            return (
                datetime.fromisoformat(first_event.end_time)
                - datetime.fromisoformat(first_event.start_time)
            )
        return None

    @staticmethod
    def _snapshot_old_values(event: EventItem, updates: dict[str, Any]) -> dict[str, Any]:
        """保存旧值快照用于 undo（委托给 DbOpsMixin）"""
        return DbOpsMixin._snapshot_item_values(event, updates)

    # ==================== 删除日程 ====================

    async def delete_event(
        self, user_id: str, event_id: str, context: PendoContext
    ) -> CommandMessage:
        """删除日程"""
        if not event_id:
            return {"status": "error", "message": "❌ 请指定要删除的日程ID"}

        event_id = event_id.strip()
        single_event_id, _, error = await self._resolve_single_event_id_or_message(
            user_id, event_id, allow_series_fallback=True
        )
        if error:
            return error
        if single_event_id:
            return await self._delete_single_instance(user_id, single_event_id)

        # 父ID：删除所有实例
        return await self._delete_all_instances(user_id, event_id)

    async def _delete_single_instance(self, user_id: str, instance_id: str) -> CommandMessage:
        """删除单个实例"""
        event = cast(EventItem | None, await self._db_get_item(instance_id, owner_id=user_id))
        if not event:
            return {"status": "error", "message": f"❌ 找不到日程 {instance_id}"}

        await self._db_soft_delete_with_log(instance_id, user_id, item_type=ItemType.EVENT.value)

        e_title = event.title
        return {
            "status": "success",
            "message": f"🗑️ 已删除日程: {e_title or '无标题'}\n💡 5分钟内可使用 /pendo undo 撤销",
        }

    async def _delete_all_instances(self, user_id: str, parent_id: str) -> CommandMessage:
        """删除父ID下所有实例"""
        try:
            instances = await self._db_find_instances(user_id, parent_id, columns="id, title, rrule")
            if not instances:
                return {"status": "error", "message": f"❌ 找不到日程 {parent_id}"}

            title = instances[0][1]
            is_recurring = len(instances) > 1 or bool(instances[0][2])
            instance_ids = [row[0] for row in instances]

            await self._db_batch_soft_delete(instance_ids, user_id)

            if is_recurring:
                return {
                    "status": "success",
                    "message": f"🗑️ 已删除重复日程: {title}\n📊 共删除 {len(instances)} 个实例\n💡 5分钟内可使用 /pendo undo 撤销",
                }
            return {
                "status": "success",
                "message": f"🗑️ 已删除日程: {title or '无标题'}\n💡 5分钟内可使用 /pendo undo 撤销",
            }
        except Exception as e:
            logger.exception("Failed to delete instances: %s", e)
            return {"status": "error", "message": f"❌ 删除失败: {str(e)}"}

    # ==================== 查看/修改提醒 ====================

    async def handle_reminders(
        self, user_id: str, args: str, context: PendoContext
    ) -> CommandMessage:
        """分发提醒子命令：set <id> <描述> 或 list [范围]"""
        parts = (args or "").split(maxsplit=1)
        if parts and parts[0].lower() == "set":
            rest = parts[1] if len(parts) > 1 else ""
            return await self.set_reminders(user_id, rest, context)
        if parts and parts[0].lower() == "confirm":
            rest = parts[1] if len(parts) > 1 else ""
            return await self.confirm_event_reminders(user_id, rest, context)
        # "list" 是子命令关键字，其后可跟可选的日期范围
        if parts and parts[0].lower() == "list":
            args = parts[1] if len(parts) > 1 else "today"
        # 顶层命令误放到 reminders 下（如 /pendo event reminders snooze xxx）
        if parts and parts[0].lower() in ("snooze",):
            cmd = parts[0].lower()
            item_id = parts[1].split()[0] if len(parts) > 1 else "<id>"
            hint = (
                f"/pendo {cmd} {item_id}" if cmd == "confirm" else f"/pendo {cmd} {item_id} <时间>"
            )
            return {"status": "error", "message": f"❌ 正确用法:\n\n{hint}"}
        return await self.list_reminders(user_id, args, context)

    async def confirm_event_reminders(
        self, user_id: str, args: str, context: PendoContext
    ) -> CommandMessage:
        """提前确认指定事件/系列的提醒。"""
        parts = (args or "").split(maxsplit=1)
        if not parts:
            return {
                "status": "error",
                "message": (
                    "❌ 用法: /pendo event reminders confirm <id> [today|future|all|提醒时间]\n"
                    "例如: /pendo event reminders confirm abc12345 today"
                ),
            }

        query_id = parts[0].strip()
        selector = parts[1].strip() if len(parts) > 1 else "future"
        events, error = await self._resolve_events_for_reminder_command(user_id, query_id)
        if error:
            return error
        if not events:
            return {"status": "error", "message": f"❌ 找不到日程 {query_id}"}

        now = now_in_timezone(user_id, self.db).replace(tzinfo=None)
        matched: list[tuple[EventItem, str]] = []
        for event in events:
            for remind_time in self._select_reminders_for_confirmation(event, selector, now):
                matched.append((event, remind_time))

        if not matched:
            return {
                "status": "warning",
                "message": f"⚠️ 没有找到匹配 `{selector}` 的提醒",
            }

        confirmed_count = 0
        for event, remind_time in matched:
            remind_dt = datetime.fromisoformat(remind_time)
            if remind_dt.tzinfo is not None:
                remind_dt = remind_dt.astimezone(TimezoneHelper.DEFAULT_TZ).replace(tzinfo=None)
            user_action = "preconfirmed" if remind_dt > now else "confirmed"
            await run_sync(
                self.db.confirm_reminder,
                event.id,
                user_action,
                user_id,
                remind_time,
                True,
            )
            confirmed_count += 1

        subject = events[0].title or "无标题"
        scope = "系列" if len(events) > 1 else "日程"
        return {
            "status": "success",
            "message": (
                f"✅ 已确认 {confirmed_count} 个提醒\n"
                f"🗓️ {scope}: {subject}\n"
                f"💡 用 /pendo event reminders {query_id} 查看当前状态"
            ),
        }

    async def _resolve_events_for_reminder_command(
        self, user_id: str, query_id: str
    ) -> tuple[list[EventItem], CommandMessage | None]:
        single_event_id, event, error = await self._resolve_single_event_id_or_message(
            user_id, query_id, allow_series_fallback=True
        )
        if error:
            return [], error
        if single_event_id and event:
            return [event], None

        rows = await self._db_find_instances(user_id, query_id)
        if not rows:
            return [], None

        events: list[EventItem] = []
        for row in rows:
            item = self.db.items._row_to_item(row)
            if isinstance(item, EventItem):
                events.append(item)
        return events, None

    @staticmethod
    def _select_reminders_for_confirmation(
        event: EventItem, selector: str, now: datetime
    ) -> list[str]:
        remind_times = parse_remind_times(event.remind_times)
        lowered = (selector or "future").strip().lower()

        if lowered == "all":
            return remind_times
        if lowered == "future":
            return [
                remind_time
                for remind_time in remind_times
                if EventHandler._normalize_remind_time(remind_time) > now
            ]
        if lowered == "today":
            return [
                remind_time
                for remind_time in remind_times
                if EventHandler._normalize_remind_time(remind_time).date() == now.date()
            ]

        matched = [remind_time for remind_time in remind_times if EventHandler._matches_reminder_selector(remind_time, selector)]
        return matched

    @staticmethod
    def _normalize_remind_time(remind_time: str) -> datetime:
        remind_dt = datetime.fromisoformat(remind_time)
        if remind_dt.tzinfo is not None:
            remind_dt = remind_dt.astimezone(TimezoneHelper.DEFAULT_TZ).replace(tzinfo=None)
        return remind_dt

    @classmethod
    def _matches_reminder_selector(cls, remind_time: str, selector: str) -> bool:
        selector = (selector or "").strip()
        if not selector:
            return False

        normalized = cls._normalize_remind_time(remind_time)
        selector_candidates = [selector]
        if "T" in remind_time:
            selector_candidates.append(remind_time.replace("T", " "))

        for candidate in selector_candidates:
            if candidate == remind_time or candidate == remind_time.replace("T", " "):
                return True

        formats = ("%Y-%m-%d %H:%M", "%m-%d %H:%M", "%m月%d日 %H:%M")
        for fmt in formats:
            try:
                parsed = datetime.strptime(selector, fmt)
            except ValueError:
                continue

            if fmt.startswith("%m"):
                parsed = parsed.replace(year=normalized.year)
            return parsed == normalized.replace(second=0, microsecond=0)
        return False

    async def set_reminders(self, user_id: str, args: str, context: PendoContext) -> CommandMessage:
        """修改现有事件的提醒时间

        用法: /pendo event reminders set <id> <提醒描述>
        例如: /pendo event reminders set abc12345 提前1天和2小时提醒
        """
        parts = args.split(maxsplit=1)
        if len(parts) < 2:
            return {
                "status": "error",
                "message": "❌ 用法: /pendo event reminders set <id> <提醒描述>\n例如: /pendo event reminders set abc12345 提前1天和2小时提醒",
            }

        event_id, reminder_desc = parts[0].strip(), parts[1].strip()

        event, wrong_type = await self._db_get_typed_item_or_message(
            event_id, user_id, ItemType.EVENT.value, "日程"
        )
        if wrong_type:
            return wrong_type
        if not event:
            return {"status": "error", "message": f"❌ 找不到日程 {event_id}"}
        event = cast(EventItem, event)

        # 用AI解析提醒描述，基准时间使用事件start_time
        base_time = event.start_time
        if not base_time:
            return {"status": "error", "message": "❌ 该日程没有开始时间，无法计算提醒"}

        try:
            reminder_rules = await run_sync(
                self.ai_parser.build_reminder_rules_from_description,
                reminder_desc,
            )
        except Exception as e:
            logger.exception("解析提醒描述失败: %s", e)
            return {"status": "error", "message": f"❌ 解析提醒描述失败: {e}"}

        if not reminder_rules:
            return {
                "status": "error",
                "message": '❌ 未能从描述中解析出提醒时间，请尝试: "提前1天" "提前2小时30分钟" 等',
            }

        remind_times = build_remind_times_from_rules(base_time, reminder_rules)

        # 对多节点事件，为每个里程碑都应用同样的偏移
        milestones = getattr(event, "milestones", None) or []
        if milestones:
            all_times: set[str] = set()
            for m in milestones:
                m_time = m.get("time")
                if not m_time:
                    continue
                try:
                    times = build_remind_times_from_rules(m_time, reminder_rules)
                    all_times.update(times)
                except Exception as e:
                    logger.warning("里程碑 %s 提醒时间解析失败: %s", m_time, e)
            remind_times = sorted(all_times)

        remind_times = ensure_start_time_reminder(remind_times, base_time)
        await self._db_update_item(
            event_id,
            {"reminder_rules": reminder_rules, "remind_times": remind_times},
            owner_id=user_id,
        )

        lines = [f"✅ 已更新提醒: {event.title or '无标题'}", f"🔔 共 {len(remind_times)} 个提醒"]
        for t in remind_times:
            lines.append(f"  ⏰ {ItemFormatter.format_datetime(t, '%m月%d日 %H:%M')}")
        lines.append(f"\n💡 用 /pendo event reminders {event_id} 查看详情")
        return {"status": "success", "message": "\n".join(lines)}

    async def list_reminders(
        self, user_id: str, args: str, context: PendoContext
    ) -> CommandMessage:
        """查看日程提醒"""
        query = (args or "today").strip()

        try:
            # 如果是ID
            if self._looks_like_id(query):
                return await self._format_reminders_by_id(user_id, query)

            # 按范围查询
            start_date, end_date = parse_event_time_range(query)
            start_dt, end_dt = datetime.fromisoformat(start_date), datetime.fromisoformat(end_date)

            # 提醒可比事件早最多 N 天触发，扩展 DB 查询范围以捕获"提醒在今天但事件在未来"的情况
            _MAX_REMIND_LEAD_DAYS = 30
            extended_end = (end_dt + timedelta(days=_MAX_REMIND_LEAD_DAYS)).isoformat()
            normal_events, repeat_events = await self._fetch_event_rows(
                user_id, start_date, extended_end
            )

            # 只保留至少有一个提醒时间在查询范围内的条目（不要求事件本身在范围内）
            event_reminders: list[tuple[EventItem, list[str]]] = []
            for e in normal_events + repeat_events:
                if not e.remind_times:
                    continue
                in_range = [
                    t
                    for t in parse_remind_times(e.remind_times)
                    if self._remind_in_range(t, start_dt, end_dt)
                ]
                if in_range:
                    event_reminders.append((e, in_range))

            title = self._format_list_title(query, start_dt, end_dt)
            if not event_reminders:
                return {"status": "success", "message": f"🔔 {title} 没有提醒"}

            # 按最早的范围内提醒时间排序
            event_reminders.sort(key=lambda x: x[1][0])
            message = f"🔔 **{title}** (共{len(event_reminders)}项)\n"
            for event, remind_times in event_reminders:
                log_map = self._build_log_map(event.id)
                milestones = getattr(event, "milestones", None) or []
                if milestones and len(milestones) >= 2:
                    # 多节点事件：显示日期区间，并按里程碑分组提醒
                    start_str = ItemFormatter.format_datetime(event.start_time or "", "%m月%d日")
                    end_str = ItemFormatter.format_datetime(event.end_time or "", "%m月%d日") if event.end_time else ""
                    date_range = f"{start_str}~{end_str}" if end_str else start_str
                    message += f"\n🗓️ {date_range} {event.title or '无标题'} 🗺️{len(milestones)}节点 `{event.id}`\n"
                    for m, m_reminds in group_reminders_by_milestone(remind_times, milestones):
                        m_str = ItemFormatter.format_datetime(m["time"], "%m-%d")
                        message += f"  📌 {m_str} {m.get('name', '')}\n"
                        milestone_notes = str(m.get("notes", "") or "").strip()
                        if milestone_notes:
                            message += f"    📝 {milestone_notes}\n"
                        for t in m_reminds:
                            t_str = ItemFormatter.format_datetime(t, "%m-%d %H:%M")
                            status = get_remind_status(log_map.get(t))
                            message += f"    ⏰ {t_str} {status}\n"
                else:
                    time_str = ItemFormatter.format_datetime(event.start_time or "", "%m月%d日 %H:%M")
                    message += f"\n🗓️ {time_str} {event.title or '无标题'} `{event.id}`\n"
                    for t in remind_times:
                        t_str = ItemFormatter.format_datetime(t, "%m-%d %H:%M")
                        status = get_remind_status(log_map.get(t))
                        message += f"  ⏰ {t_str} {status}\n"

            return {"status": "success", "message": message}
        except Exception as e:
            logger.exception("Failed to list reminders: %s", e)
            return {"status": "error", "message": f"❌ 获取提醒失败: {str(e)}"}

    async def _format_reminders_by_id(self, user_id: str, query_id: str) -> CommandMessage:
        """按ID格式化提醒信息

        支持以下格式：
        - 普通ID (8位): 查询单个事件
        - 实例ID (parent_YYYYMMDD): 查询单个实例
        - 父ID (8位，有parent_id的事件): 显示所有实例的提醒汇总
        """
        # 先尝试直接获取
        item = await self._db_get_item(query_id, owner_id=user_id)
        if item:
            if not isinstance(item, EventItem):
                return self._build_wrong_type_message(query_id, "日程", item)
            event = cast(EventItem, item)
            return format_event_reminders(event, self._build_log_map(event.id))

        # 如果直接查找失败，可能是parent_id，尝试查找所有子实例
        rows = await self._db_find_instances(user_id, query_id)
        instances: list[EventItem] = [
            item
            for row in rows
            if isinstance((item := self.db.items._row_to_item(row)), EventItem)
        ]

        if not instances:
            return {"status": "error", "message": f"❌ 找不到日程: {query_id}"}

        # 如果只有一个实例且ID匹配，返回单个
        if len(instances) == 1 and instances[0].id == query_id:
            return format_event_reminders(instances[0], self._build_log_map(instances[0].id))

        # 多个实例：汇总显示
        title = instances[0].title or "无标题"
        builder = MessageBuilder()
        builder.add_line(f"🔔 **{title}** 的提醒列表")
        builder.add_line(f"📊 共 {len(instances)} 个日程实例")
        builder.add_line("─" * 30)

        for i, instance in enumerate(instances, 1):
            remind_times = parse_remind_times(instance.remind_times)
            time_str = ItemFormatter.format_datetime(instance.start_time or "", "%m月%d日 %H:%M")
            builder.add_blank()
            builder.add_line(f"**{i}.** 🗓️ {time_str}")
            if remind_times:
                log_map = self._build_log_map(instance.id)
                for remind_time in remind_times:
                    formatted_time = ItemFormatter.format_datetime(remind_time, "%m-%d %H:%M")
                    t_iso = remind_time
                    status = get_remind_status(log_map.get(t_iso))
                    builder.add_line(f"     ⏰ {formatted_time} {status}")
            else:
                builder.add_line(f"     ⏰ 无提醒")
            builder.add_line(f"     🆔 `{instance.id}`")

        return {"status": "success", "message": builder.build()}

    # ==================== 辅助方法 ====================

    @staticmethod
    def _remind_in_range(t_str: str, start_dt: datetime, end_dt: datetime) -> bool:
        """判断单个提醒时间是否在查询范围内"""
        try:
            t_dt = datetime.fromisoformat(t_str)
            # 若为带时区的字符串（如 UTC 存储），转换为本地 naive 时间后比较
            if t_dt.tzinfo is not None:
                t_dt = t_dt.astimezone(TimezoneHelper.DEFAULT_TZ).replace(tzinfo=None)
            return start_dt <= t_dt <= end_dt
        except (ValueError, TypeError):
            return False

    @staticmethod
    def _event_in_range(e: EventItem, start_dt: datetime, end_dt: datetime) -> bool:
        """判断事件是否在查询范围内

        多节点事件：先用总区间重叠快速筛选，再检查是否有至少一个节点在范围内。
        单次事件：只看 start_time 是否在范围内。
        """
        if not e.start_time:
            return False
        e_start = datetime.fromisoformat(e.start_time)
        milestones = getattr(e, "milestones", None) or []
        if milestones and len(milestones) >= 2 and e.end_time:
            e_end = datetime.fromisoformat(e.end_time)
            # 快速排除：总区间与查询区间不重叠
            if not (e_start <= end_dt and e_end >= start_dt):
                return False
            # 精确检查：至少一个里程碑节点落在查询范围内
            for m in milestones:
                try:
                    m_dt = datetime.fromisoformat(m.get("time", ""))
                    if start_dt <= m_dt <= end_dt:
                        return True
                except (ValueError, TypeError):
                    pass
            return False
        return start_dt <= e_start <= end_dt

    async def _parse_updates(self, changes: str, current_event: EventItem) -> dict[str, Any]:
        """解析更新内容

        尝试使用AI解析，失败时降级到规则解析。
        通过 prompt 指示 AI 不要随意修改标题，避免把编辑指令误设为标题。
        """
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
        except Exception as e:
            logger.warning("AI解析失败，降级到规则解析: %s", e)
            parsed = self.ai_parser.parse_natural_language(changes, current_event.owner_id)

        updates = {}
        for key in ["title", "content", "start_time", "end_time", "location", "category", "tags"]:
            candidate = parsed.get(key)
            current_val = getattr(current_event, key, None)
            if candidate in (None, "", [], {}) or candidate == current_val:
                continue
            if key == "title":
                if not self._should_apply_title_update(changes, current_event, candidate):
                    continue
            if key == "content":
                if not self._should_apply_content_update(changes, candidate):
                    continue
            if key == "category":
                if not self._should_apply_category_update(changes, candidate):
                    continue
            updates[key] = candidate

        heuristic_milestones = self._extract_targeted_milestone_update(changes, current_event)
        targeted_milestone_notes = self._milestone_notes_were_updated(
            getattr(current_event, "milestones", None) or [],
            heuristic_milestones,
        )

        if parsed.get("remind_times"):
            updates["remind_times"] = parsed["remind_times"]

        if (
            parsed.get("notes") is not None
            and parsed.get("notes") != getattr(current_event, "notes", None)
            and not targeted_milestone_notes
        ):
            updates["notes"] = parsed["notes"]

        if parsed.get("milestones"):
            updates["milestones"] = parsed["milestones"]

        heuristic_notes = self._extract_notes_update(changes)
        if (
            heuristic_notes is not None
            and heuristic_notes != getattr(current_event, "notes", None)
            and not targeted_milestone_notes
        ):
            updates["notes"] = heuristic_notes

        if heuristic_milestones:
            updates["milestones"] = heuristic_milestones

        return updates

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

        return True

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
    def _extract_notes_update(cls, changes: str) -> str | None:
        match = cls._NOTES_EDIT_RE.search(changes)
        if not match:
            return None
        notes = match.group(1).strip(" ，,。；;")
        return notes or None

    @classmethod
    def _milestone_notes_were_updated(
        cls,
        current_milestones: list[dict[str, Any]],
        updated_milestones: list[dict[str, Any]] | None,
    ) -> bool:
        if not updated_milestones:
            return False

        for idx, updated in enumerate(updated_milestones):
            current = current_milestones[idx] if idx < len(current_milestones) else {}
            current_notes = str(current.get("notes", "") or "").strip()
            updated_notes = str(updated.get("notes", "") or "").strip()
            if current_notes != updated_notes:
                return True
        return False

    @classmethod
    def _extract_targeted_milestone_update(
        cls, changes: str, current_event: EventItem
    ) -> list[dict[str, Any]] | None:
        milestones = getattr(current_event, "milestones", None) or []
        if not milestones:
            return None

        updated: list[dict[str, Any]] | None = None
        matched_indexes: list[int] = []
        for idx, milestone in enumerate(milestones):
            name = str(milestone.get("name", "")).strip()
            if not name or name not in changes:
                continue
            matched_indexes.append(idx)
            for verb in cls._MILESTONE_VERBS:
                pattern = rf"(?:把)?{re.escape(name)}(?:时间)?\s*{verb}\s*(?P<dt>[^，,；;。]+)"
                match = re.search(pattern, changes)
                if not match:
                    continue
                parsed_time = cls._parse_edit_datetime_fragment(
                    match.group("dt"),
                    reference_time=str(milestone.get("time", "") or current_event.start_time or ""),
                )
                if not parsed_time:
                    continue
                updated = [dict(item) for item in milestones]
                updated[idx]["time"] = parsed_time

        unique_indexes = sorted(set(matched_indexes))
        if len(unique_indexes) == 1:
            idx = unique_indexes[0]
            targeted_notes = cls._extract_notes_update(changes)
            if targeted_notes is not None:
                updated = updated or [dict(item) for item in milestones]
                updated[idx]["notes"] = targeted_notes

        return updated

    @classmethod
    def _parse_edit_datetime_fragment(
        cls, fragment: str, *, reference_time: str
    ) -> str | None:
        text = (fragment or "").strip(" ，,。；;")
        if not text:
            return None

        reference_dt = None
        if reference_time:
            try:
                reference_dt = datetime.fromisoformat(reference_time)
            except (ValueError, TypeError):
                reference_dt = None
        reference_dt = reference_dt or datetime.now()

        normalized = text.replace("T", " ")
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                return datetime.strptime(normalized, fmt).isoformat()
            except ValueError:
                continue

        relative_match = re.search(
            r"(今天|明天|后天|前天)(?:\s*(上午|中午|下午|晚上|凌晨))?\s*(\d{1,2})(?:[点时:：](\d{1,2}))?",
            text,
        )
        if relative_match:
            base_date = parse_date_optional(relative_match.group(1), reference_dt)
            if base_date:
                hour = int(relative_match.group(3))
                minute = int(relative_match.group(4) or 0)
                hour = cls._apply_cn_period(relative_match.group(2), hour)
                return datetime.strptime(base_date, "%Y-%m-%d").replace(
                    hour=hour,
                    minute=minute,
                    second=0,
                ).isoformat()

        full_match = re.search(
            r"(?:(\d{4})[年/-])?\s*(\d{1,2})[月/-](\d{1,2})(?:日|号)?"
            r"(?:\s*(上午|中午|下午|晚上|凌晨))?\s*(\d{1,2})(?:[点时:：](\d{1,2}))?",
            text,
        )
        if not full_match:
            return None

        year = int(full_match.group(1) or reference_dt.year)
        month = int(full_match.group(2))
        day = int(full_match.group(3))
        hour = cls._apply_cn_period(full_match.group(4), int(full_match.group(5)))
        minute = int(full_match.group(6) or 0)
        try:
            return datetime(year, month, day, hour, minute, 0).isoformat()
        except ValueError:
            return None

    @staticmethod
    def _apply_cn_period(period: str | None, hour: int) -> int:
        if period in ("下午", "晚上") and 1 <= hour < 12:
            return hour + 12
        if period == "中午":
            if hour == 0:
                return 12
            if 1 <= hour < 11:
                return hour + 12
        if period == "凌晨" and hour == 12:
            return 0
        return hour

    def _looks_like_id(self, text: str) -> bool:
        """判断是否像ID（8位十六进制字符，或 8位十六进制_YYYYMMDD日期数字）"""
        if not text:
            return False
        if "_" in text:
            parts = text.rsplit("_", 1)
            # L-2修复：使用正则代替 len()==8 判断，更健壮且易扩展
            return (
                re.match(r"^[0-9a-f]{8}$", parts[0]) is not None
                and re.match(r"^\d{8}$", parts[1]) is not None
            )
        return re.match(r"^[0-9a-f]{8}$", text) is not None

    async def _resolve_single_event_id_or_message(
        self, user_id: str, event_id: str, *, allow_series_fallback: bool
    ) -> tuple[str | None, EventItem | None, CommandMessage | None]:
        """Resolve direct event/instance IDs and return series fallback when requested."""
        event_id = (event_id or "").strip()

        if "_" in event_id and self._looks_like_id(event_id):
            event, wrong_type = await self._db_get_typed_item_or_message(
                event_id, user_id, ItemType.EVENT.value, "日程"
            )
            if wrong_type:
                return None, None, wrong_type
            if not event:
                return None, None, {"status": "error", "message": f"❌ 找不到日程 {event_id}"}
            return event_id, cast(EventItem, event), None

        item = await self._db_get_item(event_id, owner_id=user_id)
        if item is None:
            if allow_series_fallback:
                return None, None, None
            return None, None, {"status": "error", "message": f"❌ 找不到日程 {event_id}"}
        if not isinstance(item, EventItem):
            return None, None, self._build_wrong_type_message(event_id, "日程", item)
        return event_id, cast(EventItem, item), None

    def _build_log_map(self, event_id: str) -> dict[str, dict[str, Any]]:
        """Build {remind_time_iso: log_dict} for an event.

        每个 (item_id, remind_time) 在 DB 中只有一行，无需去重。
        """
        return {log["remind_time"]: log for log in self.db.get_reminder_logs(event_id)}
