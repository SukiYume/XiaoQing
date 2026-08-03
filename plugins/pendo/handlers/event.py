"""日程命令处理器，负责创建、查看、编辑、删除和提醒管理。"""

import logging
import re
import uuid
from datetime import datetime
from itertools import islice
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, cast
from zoneinfo import ZoneInfo

from core.plugin_base import run_sync

from ..config import PendoConfig
from ..core.router import TOP_LEVEL_REDIRECTS
from ..core.types import CommandMessage, PendoContext
from ..models.item import EventItem, ItemType
from ..services.event_graph import EventFamily, EventGraphService
from ..utils.db_ops import DbOpsMixin
from ..utils.error_handlers import error_result, handle_command_errors
from ..utils.formatters import (
    ItemFormatter,
    MessageBuilder,
    is_tag_token,
)
from ..utils.session_utils import safe_create_session
from ..utils.settings_utils import resolve_default_category
from ..utils.time_utils import (
    TimezoneHelper,
    now_in_timezone,
    parse_event_time_range,
    parse_remind_times,
    resolve_source_wall_time,
    utc_now_iso,
)
from ..utils.validators import (
    build_remind_times_from_rules,
    derive_reminder_rules,
    normalize_event_fields,
    with_start_time_reminder_rule,
)
from .event_support import (
    ensure_event_reminder_rules,
    ensure_event_reminders,
    ensure_start_time_reminder,
    event_display_timezone,
    format_conflicts,
    format_event_created,
    format_event_reminders,
    format_milestone_event_created,
    format_recurring_event_created,
    get_remind_status,
    get_remind_status_label,
    recalculate_event_reminders,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ..services.db import Database

TIME_RANGE_KEYWORDS = frozenset({"today", "tomorrow", "week", "month", "year"})
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

    def build_reminder_rules_from_description(self, description: str) -> list[dict[str, int]]: ...


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

    db: "Database"
    ai_parser: EventAIParserProtocol
    reminder_service: ReminderServiceProtocol

    def __init__(
        self,
        db: "Database",
        ai_parser: EventAIParserProtocol,
        reminder_service: ReminderServiceProtocol,
    ) -> None:
        self.db = db
        self.ai_parser = ai_parser
        self.reminder_service = reminder_service
        self.event_graph = EventGraphService(db)

    async def _fetch_event_rows(
        self, user_id: str, start_date: str, end_date: str
    ) -> list[EventItem]:
        rows = await run_sync(self.db.get_events_for_range, user_id, start_date, end_date)
        return cast(list[EventItem], rows)

    def _load_reminder_list_metadata(
        self,
        user_id: str,
        events: list[EventItem],
    ) -> tuple[
        dict[str, dict[str, Any]],
        dict[str, dict[str, dict[str, Any]]],
    ]:
        """批量读取集合上下文和提醒日志，避免 N+1 查询。"""
        collection_ids = [
            cast(str, event.event_collection_id)
            for event in events
            if getattr(event, "event_collection_id", None)
        ]
        event_ids = [str(event.id) for event in events]
        return (
            self.db.get_event_collections_by_ids(user_id, collection_ids),
            self.db.get_reminder_logs_by_item_ids(user_id, event_ids),
        )

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

        dispatched = await self._dispatch_known_command(
            command,
            user_id,
            rest,
            context,
            group_id,
        )
        if dispatched is not None:
            return dispatched

        # 常见拼写错误
        if command == "reminder":
            return error_result("❌ 没有这个命令\n\n正确用法是: /pendo event reminders <id>")

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

    async def _dispatch_known_command(
        self,
        command: str,
        user_id: str,
        rest: str,
        context: PendoContext,
        group_id: int | None,
    ) -> CommandMessage | None:
        """分发已知日程子命令；未匹配时交回上层生成纠错提示。"""
        if command == "add":
            return await self.add_event(user_id, rest, context, group_id)
        if command == "view":
            return await self.view_event(user_id, rest, context)
        if command == "edit":
            return await self.edit_event(user_id, rest, context)
        if command == "delete":
            return await self.delete_event(user_id, rest, context)
        if command == "list":
            return await self.list_events(user_id, rest or "today", context)
        if command == "reminders":
            return await self.handle_reminders(user_id, rest, context)
        return None

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

        # 用 AI 解析自然语言。
        parsed = await self.ai_parser.parse_event_with_ai(text, user_id)
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
                    "group_id": group_id,
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
                    "group_id": group_id,
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
        milestones, milestone_error = self._normalize_milestones(parsed_data.get("milestones"))
        if milestone_error:
            return {"status": "error", "message": milestone_error}
        if milestones:
            parsed_data["milestones"] = milestones

        # 有里程碑时可从第一项推断开始时间，无需再次向用户追问。
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

        # 重复规则优先于多节点，避免 AI 同时生成两者时走错路径。
        if parsed_data.get("rrule"):
            return await self._create_recurring_event(
                user_id, parsed_data, remind_times, allow_conflict
            )

        # 处理多时间节点日程（2个及以上节点）
        if milestones and isinstance(milestones, list) and len(milestones) >= 2:
            return await self._create_milestone_event(
                user_id, parsed_data, remind_times, allow_conflict
            )

        # 创建单次日程
        return await self._create_single_event(user_id, parsed_data, remind_times, allow_conflict)

    @staticmethod
    def _normalize_milestones(
        raw_milestones: Any,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """校验并按时间排列 AI 返回的多节点日程。"""
        if raw_milestones in (None, []):
            return [], None
        if not isinstance(raw_milestones, list):
            return [], "❌ 多时间节点格式无效，请重新描述各节点时间"

        milestones: list[dict[str, Any]] = []
        awareness: set[bool] = set()
        for index, raw_milestone in enumerate(raw_milestones, 1):
            milestone, error = EventHandler._normalize_milestone(raw_milestone, index)
            if error or milestone is None:
                return [], error or f"❌ 第 {index} 个时间节点格式无效"
            normalized_time = milestone["time"]
            awareness.add(datetime.fromisoformat(normalized_time).tzinfo is not None)
            milestones.append(milestone)

        if len(awareness) > 1:
            return [], "❌ 多时间节点不能混用带时区和不带时区的时间"
        milestones.sort(key=lambda milestone: datetime.fromisoformat(milestone["time"]))
        return milestones, None

    @staticmethod
    def _normalize_milestone(
        raw_milestone: Any, index: int
    ) -> tuple[dict[str, Any] | None, str | None]:
        """规范一个多节点日程，并保留节点名称和备注等业务字段。"""
        if not isinstance(raw_milestone, dict):
            return None, f"❌ 第 {index} 个时间节点格式无效"
        node_time = raw_milestone.get("time")
        if not isinstance(node_time, str) or not node_time.strip():
            return None, f"❌ 第 {index} 个时间节点缺少有效时间"

        fields: dict[str, Any] = {"start_time": node_time}
        if raw_milestone.get("end_time") not in (None, ""):
            fields["end_time"] = raw_milestone["end_time"]
        try:
            normalized_fields = normalize_event_fields(fields, partial=True)
        except (TypeError, ValueError):
            return None, f"❌ 第 {index} 个时间节点的时间格式无效"
        normalized_time = normalized_fields.get("start_time")
        if not isinstance(normalized_time, str):
            return None, f"❌ 第 {index} 个时间节点的时间格式无效"

        milestone = dict(raw_milestone)
        milestone["time"] = normalized_time
        normalized_end = normalized_fields.get("end_time")
        if normalized_end is not None:
            milestone["end_time"] = normalized_end
        return milestone, None

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
        if isinstance(conflicts, list) and conflicts:
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

        created_at = utc_now_iso()
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
            created_at=created_at,
            updated_at=created_at,
        )

        item_id = await self._db_create_with_log(event_item, owner_id=user_id, action="create")
        event_item.id = item_id

        return {
            "status": "success",
            "message": format_event_created(event_item.to_dict()),
            "item_id": item_id,
        }

    async def _create_recurring_event(
        self,
        user_id: str,
        parsed_data: dict[str, Any],
        remind_times: list[str],
        allow_conflict: bool = False,
    ) -> CommandMessage:
        """创建重复日程"""
        start_dt = datetime.fromisoformat(parsed_data["start_time"])
        end_dt = (
            datetime.fromisoformat(parsed_data["end_time"]) if parsed_data.get("end_time") else None
        )
        duration = end_dt - start_dt if end_dt else None
        user_now = await run_sync(now_in_timezone, user_id, self.db)
        instances, exhausted = self._expand_recurring_instances(
            parsed_data["rrule"], start_dt, user_now
        )

        if not instances:
            message = (
                "❌ 所有重复实例均已过期，没有未来可创建的日程"
                if exhausted
                else "❌ 没有生成任何重复实例"
            )
            return {"status": "error", "message": message}

        reminder_rules = parsed_data.get("reminder_rules", [])
        if not reminder_rules:
            reminder_rules = ensure_event_reminder_rules(parsed_data, remind_times)

        if not allow_conflict:
            for instance_dt in instances:
                instance_end_dt = instance_dt + duration if duration else None
                conflict = await self._check_conflict(
                    user_id,
                    parsed_data,
                    instance_dt.isoformat(),
                    instance_end_dt.isoformat() if instance_end_dt else None,
                    allow_conflict,
                )
                if conflict:
                    return conflict

        collection_id = uuid.uuid4().hex
        collection_payload = {
            "id": collection_id,
            "owner_id": user_id,
            "kind": "recurring",
            "title": parsed_data.get("title", "无标题日程"),
            "content": parsed_data.get("content", ""),
            "category": parsed_data.get("category", "未分类"),
            "location": parsed_data.get("location", ""),
            "tags": parsed_data.get("tags", []),
            "notes": parsed_data.get("notes", ""),
            "context": parsed_data.get("context", {}),
            "timezone": parsed_data.get("timezone", "Asia/Shanghai"),
            "rrule": parsed_data["rrule"],
            "reminder_rules": reminder_rules,
            "start_time": instances[0].isoformat(),
            "end_time": (
                (instances[-1] + duration).isoformat() if duration else instances[-1].isoformat()
            ),
        }
        children: list[tuple[str, EventItem]] = []

        created_at = utc_now_iso()
        for index, instance_dt in enumerate(instances, 1):
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
                notes=parsed_data.get("notes", ""),  # 重复事件必须保留用户备注。
                event_role="recurring_occurrence",
                event_collection_id=collection_id,
                event_collection_kind="recurring",
                event_index=index,
                event_node_key=instance_dt.strftime("%Y%m%d"),
                created_at=created_at,
                updated_at=created_at,
            )

            instance_id = f"{collection_id}_{instance_dt.strftime('%Y%m%d')}"
            instance_item.id = instance_id
            children.append((instance_id, instance_item))
        await run_sync(
            self.db.create_event_collection_with_children,
            collection_payload,
            children,
            operation_action="create_recurring",
        )
        created_ids = [child_id for child_id, _child in children]

        return {
            "status": "success",
            "message": format_recurring_event_created(
                parsed_data.get("title", "无标题"),
                len(created_ids),
                len(reminder_rules),
                collection_id,
            ),
            "item_id": collection_id,
        }

    @staticmethod
    def _expand_recurring_instances(
        rule: str, start_dt: datetime, user_now: datetime
    ) -> tuple[list[datetime], bool]:
        """按起始墙钟时间展开有界重复规则，并恢复原始时区偏移。"""
        from dateutil.rrule import rrulestr

        start_wall = start_dt.replace(tzinfo=None) if start_dt.tzinfo else start_dt
        now_wall = (
            user_now.astimezone(start_dt.tzinfo).replace(tzinfo=None)
            if start_dt.tzinfo is not None
            else user_now.replace(tzinfo=None)
        )
        if start_wall <= now_wall:
            next_dt = rrulestr(rule, dtstart=start_wall).after(now_wall, inc=False)
            if next_dt is None:
                return [], True
            start_wall = next_dt

        rrule_obj = rrulestr(rule, dtstart=start_wall)
        instances = list(islice(rrule_obj, PendoConfig.EVENT_MAX_RRULE_COUNT))
        if start_dt.tzinfo is not None:
            # dateutil 使用无时区墙钟时间展开规则；写回时恢复用户输入的偏移。
            instances = [instance.replace(tzinfo=start_dt.tzinfo) for instance in instances]
        return instances, False

    async def _create_milestone_event(
        self,
        user_id: str,
        parsed_data: dict[str, Any],
        remind_times: list[str],
        allow_conflict: bool = False,
    ) -> CommandMessage:
        """创建多时间节点事件集合和可独立操作的节点 leaf。"""
        milestones = parsed_data["milestones"]
        start_time = parsed_data.get("start_time") or milestones[0]["time"]
        end_time = parsed_data.get("end_time") or milestones[-1]["time"]

        conflict = await self._check_conflict(
            user_id, parsed_data, start_time, end_time, allow_conflict
        )
        if conflict:
            return conflict

        reminder_rules = parsed_data.get("reminder_rules", [])
        if not reminder_rules:
            reminder_rules = ensure_event_reminder_rules(parsed_data, remind_times)

        collection_id = uuid.uuid4().hex
        collection_payload = {
            "id": collection_id,
            "owner_id": user_id,
            "kind": "multi_node",
            "title": parsed_data.get("title", "无标题日程"),
            "content": parsed_data.get("content", ""),
            "category": parsed_data.get("category", "未分类"),
            "location": parsed_data.get("location", ""),
            "tags": parsed_data.get("tags", []),
            "notes": parsed_data.get("notes", ""),
            "context": parsed_data.get("context", {}),
            "timezone": parsed_data.get("timezone", "Asia/Shanghai"),
            "reminder_rules": reminder_rules,
            "start_time": start_time,
            "end_time": end_time,
        }

        children: list[tuple[str, EventItem]] = []
        all_reminders: set[str] = set()
        created_at = utc_now_iso()
        for index, milestone in enumerate(milestones, 1):
            node_time = milestone["time"]
            node_key = f"m{index:02d}"
            node_reminders = build_remind_times_from_rules(node_time, reminder_rules)
            all_reminders.update(node_reminders)
            node = EventItem(
                owner_id=user_id,
                title=milestone.get("name", "无标题节点"),
                content=parsed_data.get("content", ""),
                start_time=node_time,
                end_time=milestone.get("end_time"),
                location=parsed_data.get("location", ""),
                tags=parsed_data.get("tags", []),
                category=parsed_data.get("category", "未分类"),
                context=parsed_data.get("context", {}),
                remind_times=node_reminders,
                reminder_rules=reminder_rules,
                notes=milestone.get("notes", ""),
                event_role="multi_node_child",
                event_collection_id=collection_id,
                event_collection_kind="multi_node",
                event_index=index,
                event_node_key=node_key,
                created_at=created_at,
                updated_at=created_at,
            )
            node_id = f"{collection_id}_{node_key}"
            node.id = node_id
            children.append((node_id, node))

        await run_sync(
            self.db.create_event_collection_with_children,
            collection_payload,
            children,
            operation_action="create_multi_node",
        )

        event_payload = {
            **parsed_data,
            "id": collection_id,
            "start_time": start_time,
            "end_time": end_time,
            "remind_times": sorted(all_reminders),
        }

        return {
            "status": "success",
            "message": format_milestone_event_created(event_payload),
            "item_id": collection_id,
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
        if error := self._single_token_error(
            event_id, "❌ 日程详情只接受一个ID\n例如: /pendo event view abc12345"
        ):
            return error

        family = await self._load_event_family(user_id, event_id)
        if family.kind == "missing":
            item = await self._db_get_item(event_id, owner_id=user_id)
            if item is not None and not isinstance(item, EventItem):
                return self._build_wrong_type_message(event_id, "日程", item)
            return {"status": "error", "message": f"❌ 找不到日程 {event_id}"}
        return self._format_event_family_detail(family, event_id)

    async def _load_event_family(self, user_id: str, event_or_collection_id: str) -> EventFamily:
        return cast(
            EventFamily,
            await run_sync(self.event_graph.load_by_id, user_id, event_or_collection_id),
        )

    def _format_event_family_detail(self, family: EventFamily, query_id: str) -> CommandMessage:
        if family.collection and family.leaf is None:
            return self._format_collection_detail(family.collection, family.children)

        if family.leaf is None:
            return {"status": "error", "message": f"❌ 找不到日程 {query_id}"}
        return self._format_leaf_detail(family)

    @staticmethod
    def _format_collection_detail(
        collection: dict[str, Any], children: list[EventItem]
    ) -> CommandMessage:
        """格式化日程集合及其节点摘要。"""
        kind_label = "多时间节点事件" if collection.get("kind") == "multi_node" else "重复日程"
        lines = [
            f"📋 **{collection.get('title') or '无标题'}**",
            "",
            f"🗺️ {kind_label} ({len(children)}个节点)",
        ]
        if collection.get("start_time"):
            lines.append(
                f"⏰ {EventHandler._format_full_time_range(collection['start_time'], collection.get('end_time'), collection)}"
            )
        if collection.get("category"):
            lines.append(f"📂 分类: {collection['category']}")
        if collection.get("content"):
            lines.append(f"📄 内容: {collection['content']}")
        if collection.get("location"):
            lines.append(f"📍 {collection['location']}")
        if collection.get("notes"):
            lines.append(f"📝 {collection['notes']}")
        if collection.get("tags"):
            lines.append(f"🏷️ {', '.join(collection['tags'])}")
        lines.append("")
        for child in children:
            child_time = EventHandler._format_full_time_range(
                child.start_time, child.end_time, child
            )
            lines.append(f"  📌 {child_time} {child.title or '无标题'} `{child.id}`")
        lines.extend(
            [
                "",
                f"`{collection['id']}`",
                f"💡 /pendo event edit {collection['id']} <内容> 编辑标题/元信息",
            ]
        )
        return {"status": "success", "message": "\n".join(lines)}

    def _format_leaf_detail(self, family: EventFamily) -> CommandMessage:
        """格式化单次日程或集合中的一个节点。"""
        event = family.leaf
        if event is None:
            return {"status": "error", "message": "❌ 找不到日程"}

        collection = family.collection
        title = event.title or "无标题"
        remind_times = parse_remind_times(event.remind_times)
        lines = [f"📋 **{title}**", ""]

        if collection:
            lines.append(f"🗓️ 所属: {collection.get('title') or '无标题'}")
            lines.append("📌 节点日程" if collection.get("kind") == "multi_node" else "🔄 重复实例")
        else:
            lines.append("📆 单次事件")
        lines.append(f"⏰ {self._format_full_time_range(event.start_time, event.end_time, event)}")

        self._append_event_metadata(lines, event)
        self._append_reminder_preview(lines, remind_times, event)
        self._append_sibling_summary(lines, event, family.children if collection else [])

        lines.append(f"\n`{event.id}`")
        lines.append(f"💡 /pendo event reminders {event.id} | /pendo event edit {event.id} <内容>")
        return {"status": "success", "message": "\n".join(lines)}

    @staticmethod
    def _append_event_metadata(lines: list[str], event: EventItem) -> None:
        """追加分类、内容、地点、备注和标签。"""
        if event.category:
            lines.append(f"📂 分类: {event.category}")
        if event.content:
            lines.append(f"📄 内容: {event.content}")
        if event.location:
            lines.append(f"📍 {event.location}")
        if event.notes:
            lines.append(f"📝 {event.notes}")
        if event.tags:
            lines.append(f"🏷️ {', '.join(event.tags)}")

    @staticmethod
    def _format_full_time_range(
        start_time: str | None,
        end_time: str | None,
        timezone_source: EventItem | dict[str, Any] | None = None,
    ) -> str:
        """格式化详情页时间，始终保留年份和完整日期。"""
        if not start_time:
            return "未设置时间"
        display_timezone = event_display_timezone(timezone_source or {})
        start = ItemFormatter.format_datetime(start_time, tz=display_timezone)
        if not end_time:
            return start
        return f"{start} - {ItemFormatter.format_datetime(end_time, tz=display_timezone)}"

    @staticmethod
    def _append_reminder_preview(
        lines: list[str],
        remind_times: list[str],
        timezone_source: EventItem | dict[str, Any],
    ) -> None:
        """追加最多五条提醒预览。"""
        lines.append("")
        if not remind_times:
            lines.append("🔔 未设置提醒")
            return
        lines.append(f"🔔 提醒 ({len(remind_times)}个):")
        display_timezone = event_display_timezone(timezone_source)
        for remind_time in remind_times[:5]:
            formatted = ItemFormatter.format_datetime(
                remind_time, "%m月%d日 %H:%M", tz=display_timezone
            )
            lines.append(f"  ⏰ {formatted}")
        if len(remind_times) > 5:
            lines.append(f"  … 共{len(remind_times)}个提醒")

    @staticmethod
    def _append_sibling_summary(
        lines: list[str], event: EventItem, children: list[EventItem]
    ) -> None:
        """追加同集合其他节点的简要索引。"""
        siblings = [child for child in children if child.id != event.id]
        if not siblings:
            return
        lines.extend(["", "同组其他节点:"])
        for sibling in siblings:
            sibling_time = EventHandler._format_full_time_range(
                sibling.start_time, sibling.end_time, sibling
            )
            lines.append(f"  • {sibling_time} {sibling.title or '无标题'} `{sibling.id}`")

    _CN_WEEKDAYS: ClassVar[tuple[str, ...]] = (
        "周一",
        "周二",
        "周三",
        "周四",
        "周五",
        "周六",
        "周日",
    )
    _RANGE_LABELS: ClassVar[dict[str, str]] = {
        "today": "今日",
        "今天": "今日",
        "tomorrow": "明日",
        "明天": "明日",
        "week": "本周",
        "本周": "本周",
        "month": "本月",
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

    @staticmethod
    def _format_simple_list_item(
        event: EventItem,
        current_dt: datetime,
    ) -> tuple[str | None, str]:
        """格式化单次事件的列表项。返回 (date_str, text)。"""
        if not event.start_time:
            return None, ""
        ev_start_dt = TimezoneHelper.parse(event.start_time, current_dt.tzinfo)
        date_str = ev_start_dt.strftime("%Y-%m-%d")
        time_str = ItemFormatter.format_time_range(
            event.start_time,
            event.end_time,
            tz=current_dt.tzinfo or TimezoneHelper.DEFAULT_TZ,
        )
        text = f"• {time_str} {event.title or '无标题'}"
        if event.location:
            text += f" @ {ItemFormatter.truncate_content(event.location, 15)}"
        text += f" `{event.id}`\n"
        return date_str, text

    @staticmethod
    def _format_graph_list_item(
        event: EventItem,
        current_dt: datetime,
        collection: dict[str, Any],
    ) -> tuple[str | None, str]:
        if not event.start_time:
            return None, ""
        ev_start_dt = TimezoneHelper.parse(event.start_time, current_dt.tzinfo)
        date_str = ev_start_dt.strftime("%Y-%m-%d")
        time_str = ItemFormatter.format_time_range(
            event.start_time,
            event.end_time,
            tz=current_dt.tzinfo or TimezoneHelper.DEFAULT_TZ,
        )
        collection_title = collection.get("title") or "无标题"
        marker = "📌" if collection.get("kind") == "multi_node" else "🔄"
        text = f"• {time_str} {collection_title} · {event.title or '无标题'} {marker}"
        if event.location:
            text += f" @ {ItemFormatter.truncate_content(event.location, 15)}"
        text += f" `{event.id}`\n"
        return date_str, text

    @staticmethod
    def _parse_event_list_filters(
        raw_query: str,
    ) -> tuple[str, str | None, str | None, CommandMessage | None]:
        """拆分时间范围、分类和标签过滤条件。"""
        category: str | None = None
        tag: str | None = None
        range_parts: list[str] = []
        for part in raw_query.split():
            if part.startswith("cat:"):
                category = part[4:]
                if not category:
                    return (
                        "",
                        None,
                        None,
                        {
                            "status": "error",
                            "message": "❌ cat: 后面需要分类名",
                        },
                    )
            elif is_tag_token(part):
                tag = part[1:]
            else:
                range_parts.append(part)
        return " ".join(range_parts) or "today", category, tag, None

    @staticmethod
    def _event_sort_key(event: EventItem, current_dt: datetime) -> datetime:
        """把日程开始时间统一到用户时区后排序。"""
        try:
            return TimezoneHelper.parse(event.start_time or "", current_dt.tzinfo)
        except (TypeError, ValueError):
            return datetime.max.replace(tzinfo=current_dt.tzinfo)

    @staticmethod
    def _event_matches_list_filters(
        event: EventItem,
        collection: dict[str, Any] | None,
        category: str | None,
        tag: str | None,
    ) -> bool:
        """同时考虑节点和集合元信息的列表过滤。"""
        if category and category not in {
            event.category or "",
            str((collection or {}).get("category") or ""),
        }:
            return False
        event_tags = set(event.tags or [])
        collection_tags = set((collection or {}).get("tags") or [])
        return not tag or tag in event_tags or tag in collection_tags

    async def list_events(
        self, user_id: str, time_range: str, context: PendoContext
    ) -> CommandMessage:
        """列出日程

        支持额外过滤参数 (可与时间范围组合使用):
        - cat:xxx  -> 按分类筛选
        - #tag     -> 按标签筛选
        """
        # 单个事件 ID 直接转到详情视图。
        if self._looks_like_id(time_range.strip()):
            return await self.view_event(user_id, time_range.strip(), context)

        time_range, cat_filter, tag_filter, filter_error = self._parse_event_list_filters(
            time_range
        )
        if filter_error:
            return filter_error

        current_dt = await run_sync(now_in_timezone, user_id, self.db)
        try:
            start_date, end_date = parse_event_time_range(
                time_range,
                now=current_dt.replace(tzinfo=None),
                strict=True,
            )
        except ValueError:
            return {
                "status": "error",
                "message": (
                    "❌ 无法解析时间范围，请使用 today/week/month/year、"
                    "last7d 或 YYYY-MM-DD..YYYY-MM-DD"
                ),
            }

        events = await self._fetch_event_rows(user_id, start_date, end_date)

        # 数据库已按日期粗筛，这里再按用户时区精确判定区间重叠。
        start_dt, end_dt = datetime.fromisoformat(start_date), datetime.fromisoformat(end_date)
        events = [
            event for event in events if self._event_in_range(event, start_dt, end_dt, current_dt)
        ]
        collection_ids = [
            str(event.event_collection_id) for event in events if event.event_collection_id
        ]
        collection_map = await run_sync(
            self.db.get_event_collections_by_ids,
            user_id,
            collection_ids,
        )
        events = [
            event
            for event in events
            if self._event_matches_list_filters(
                event,
                collection_map.get(str(event.event_collection_id))
                if event.event_collection_id
                else None,
                cat_filter,
                tag_filter,
            )
        ]
        events.sort(key=lambda event: self._event_sort_key(event, current_dt))
        return self._format_event_list_result(
            events,
            collection_map,
            time_range,
            start_dt,
            end_dt,
            current_dt,
            cat_filter,
            tag_filter,
        )

    def _format_event_list_result(
        self,
        events: list[EventItem],
        collection_map: dict[str, dict[str, Any]],
        time_range: str,
        start_dt: datetime,
        end_dt: datetime,
        current_dt: datetime,
        category: str | None,
        tag: str | None,
    ) -> CommandMessage:
        """按日期分组渲染日程列表。"""
        filter_labels = [
            label
            for label in (f"分类:{category}" if category else "", f"#{tag}" if tag else "")
            if label
        ]
        filter_suffix = f" [{', '.join(filter_labels)}]" if filter_labels else ""
        title = self._format_list_title(time_range, start_dt, end_dt)

        if not events:
            return {
                "status": "success",
                "message": f"🗓️ {title}{filter_suffix} 没有日程安排\n\n💡 用 /pendo event add <内容> 添加日程",
            }

        message = f"🗓️ **{title}**{filter_suffix} (共{len(events)}项)\n"
        current_date: str | None = None

        for event in events:
            collection = (
                collection_map.get(str(event.event_collection_id))
                if event.event_collection_id
                else None
            )

            if collection:
                date_str, text = self._format_graph_list_item(event, current_dt, collection)
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

    # ==================== 编辑日程 ====================

    async def edit_event(self, user_id: str, args: str, context: PendoContext) -> CommandMessage:
        """编辑日程"""
        parts = args.split(maxsplit=1)
        if len(parts) < 2:
            return {"status": "error", "message": "❌ 用法: /pendo event edit <id> <修改内容>"}

        event_id, changes = parts[0], parts[1]
        family = await self._load_event_family(user_id, event_id)
        if family.collection and family.leaf is None:
            return await self._edit_collection(user_id, family, changes)
        if family.leaf is not None:
            return await self._edit_single_instance(user_id, family.leaf.id, changes)

        single_event_id, _, error = await self._resolve_single_event_id_or_message(
            user_id, event_id
        )
        if error:
            return error
        if single_event_id:
            return await self._edit_single_instance(user_id, single_event_id, changes)
        return {"status": "error", "message": f"❌ 找不到日程 {event_id}"}

    async def _edit_collection(
        self, user_id: str, family: EventFamily, changes: str
    ) -> CommandMessage:
        collection = family.collection
        if not collection:
            return {"status": "error", "message": "❌ 找不到日程集合"}

        pseudo = EventItem(
            id=str(collection["id"]),
            owner_id=user_id,
            title=str(collection.get("title") or ""),
            content=str(collection.get("content") or ""),
            category=str(collection.get("category") or "未分类"),
            location=str(collection.get("location") or ""),
            tags=list(collection.get("tags") or []),
            notes=str(collection.get("notes") or ""),
            start_time=collection.get("start_time"),
            end_time=collection.get("end_time"),
        )
        updates = await self._parse_updates(changes, pseudo)
        allowed = {"title", "content", "category", "location", "tags", "notes"}
        collection_updates = {k: v for k, v in updates.items() if k in allowed}
        if not collection_updates:
            return {"status": "warning", "message": "⚠️ 未识别到有效的集合元信息修改"}

        success = await run_sync(
            self.db.update_event_collection,
            collection["id"],
            collection_updates,
            user_id,
            operation_log={
                "user_id": user_id,
                "action": "edit_event_collection",
                "item_type": "event",
                "item_id": collection["id"],
                "details": {"updates": collection_updates},
            },
        )
        if not success:
            return {"status": "error", "message": f"❌ 更新失败: {collection['id']}"}
        return {
            "status": "success",
            "message": f"✅ 已更新日程集合: {collection_updates.get('title', collection.get('title') or '无标题')}",
        }

    async def _edit_single_instance(
        self, user_id: str, instance_id: str, changes: str
    ) -> CommandMessage:
        """编辑单个日程实例"""
        event, wrong_type = await self._db_get_typed_item_or_message(
            instance_id, user_id, ItemType.EVENT.value, "日程"
        )
        if wrong_type:
            return wrong_type
        if not event:
            return {"status": "error", "message": f"❌ 找不到日程 {instance_id}"}
        event = cast(EventItem, event)

        updates = await self._parse_updates(changes, event)
        if not updates:
            return {"status": "warning", "message": "⚠️ 未识别到有效的修改内容"}

        title = event.title
        if "start_time" in updates and "remind_times" not in updates:
            updates["remind_times"] = recalculate_event_reminders(event, updates)
        elif "remind_times" in updates:
            updates["remind_times"] = ensure_start_time_reminder(
                updates["remind_times"],
                updates.get("start_time") or event.start_time,
            )

        await self._db_update_with_log(
            instance_id,
            updates,
            user_id,
            action="edit_event",
            expected_version=event.version,
        )

        return {
            "status": "success",
            "message": f"✅ 已更新日程: {updates.get('title', title)}\n\n💡 /pendo event reminders {instance_id} 查看提醒 | /pendo undo 撤销编辑",
        }

    # ==================== 删除日程 ====================

    async def delete_event(
        self, user_id: str, event_id: str, context: PendoContext
    ) -> CommandMessage:
        """删除日程"""
        if not event_id:
            return {"status": "error", "message": "❌ 请指定要删除的日程ID"}

        event_id = event_id.strip()
        family = await self._load_event_family(user_id, event_id)
        if family.collection and family.leaf is None:
            return await self._delete_collection(user_id, family)
        if family.leaf is not None:
            return await self._delete_single_instance(user_id, family.leaf.id)

        single_event_id, _, error = await self._resolve_single_event_id_or_message(
            user_id, event_id
        )
        if error:
            return error
        if single_event_id:
            return await self._delete_single_instance(user_id, single_event_id)

        return {"status": "error", "message": f"❌ 找不到日程 {event_id}"}

    async def _delete_collection(self, user_id: str, family: EventFamily) -> CommandMessage:
        collection = family.collection
        if not collection:
            return {"status": "error", "message": "❌ 找不到日程集合"}
        deleted = await run_sync(
            self.db.delete_event_collection,
            collection["id"],
            user_id,
            cascade=True,
            operation_log={
                "user_id": user_id,
                "action": "delete_event_collection",
                "item_type": "event",
                "item_id": collection["id"],
                "details": {"child_ids": [child.id for child in family.children]},
            },
        )
        if not deleted:
            return {"status": "error", "message": f"❌ 删除失败: {collection['id']}"}
        return {
            "status": "success",
            "message": f"🗑️ 已删除日程集合: {collection.get('title') or '无标题'}\n📊 共删除 {len(family.children)} 个节点",
        }

    async def _delete_single_instance(self, user_id: str, instance_id: str) -> CommandMessage:
        """原子删除单个实例，并在需要时清理空的多节点集合。"""
        deleted = await run_sync(self.db.delete_event_instance, instance_id, user_id)
        if not deleted:
            return {"status": "error", "message": f"❌ 找不到日程 {instance_id}"}
        event_title, _collection_deleted = deleted
        return {
            "status": "success",
            "message": f"🗑️ 已删除日程: {event_title or '无标题'}\n{PendoConfig.UNDO_HINT}",
        }

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
        if parts and parts[0].lower() in {"delete", "remove", "rm"}:
            rest = parts[1] if len(parts) > 1 else ""
            return await self.delete_event_reminders(user_id, rest, context)
        # "list" 是子命令关键字，其后可跟可选的日期范围
        if parts and parts[0].lower() == "list":
            args = parts[1] if len(parts) > 1 else "today"
        # 顶层命令误放到 reminders 下（如 /pendo event reminders snooze xxx）
        if parts and parts[0].lower() == "snooze":
            item_id = parts[1].split()[0] if len(parts) > 1 else "<id>"
            return {
                "status": "error",
                "message": f"❌ 正确用法:\n\n/pendo snooze {item_id} <时间>",
            }
        return await self.list_reminders(user_id, args, context)

    async def delete_event_reminders(
        self, user_id: str, args: str, context: PendoContext
    ) -> CommandMessage:
        """删除指定事件/系列的一个或多个提醒。"""
        parts = (args or "").split(maxsplit=1)
        if len(parts) < 2:
            return {
                "status": "error",
                "message": (
                    "❌ 用法: /pendo event reminders delete <id> <all|today|future|提醒时间>\n"
                    "例如: /pendo event reminders delete abc12345 2030-06-01 09:00"
                ),
            }

        query_id = parts[0].strip()
        selector = parts[1].strip()
        events, collection_id, error = await self._resolve_events_for_reminder_command(
            user_id, query_id
        )
        if error:
            return error
        if not events:
            return {"status": "error", "message": f"❌ 找不到日程 {query_id}"}

        now = now_in_timezone(user_id, self.db)
        deleted_count = 0
        pending_updates: dict[str, tuple[list[str], list[dict[str, int]]]] = {}
        event_versions: dict[str, int] = {}
        updated_rules: list[list[dict[str, int]]] = []
        for event in events:
            current_times = parse_remind_times(event.remind_times)
            selected = set(self._select_reminders_for_confirmation(event, selector, now))
            if not selected:
                updated_rules.append(list(event.reminder_rules or []))
                continue
            next_times = [
                remind_time for remind_time in current_times if remind_time not in selected
            ]
            rules = (
                derive_reminder_rules(event.start_time, next_times)
                if event.start_time and next_times
                else []
            )
            pending_updates[str(event.id)] = (next_times, rules)
            event_versions[str(event.id)] = event.version
            deleted_count += len(selected)
            updated_rules.append(rules)

        if deleted_count == 0:
            return {
                "status": "warning",
                "message": f"⚠️ 没有找到匹配 `{selector}` 的提醒",
            }

        if collection_id is not None:
            first_rules = updated_rules[0]
            collection_rules = (
                first_rules if all(rules == first_rules for rules in updated_rules) else None
            )
            await run_sync(
                self.db.update_event_collection_reminders,
                collection_id,
                user_id,
                pending_updates,
                collection_rules,
            )
        else:
            for event_id, (remind_times, reminder_rules) in pending_updates.items():
                await self._db_update_item(
                    event_id,
                    {"remind_times": remind_times, "reminder_rules": reminder_rules},
                    owner_id=user_id,
                    expected_version=event_versions[event_id],
                )

        return {
            "status": "success",
            "message": (
                f"🗑️ 已删除 {deleted_count} 个提醒\n"
                f"💡 用 /pendo event reminders {query_id} 查看当前提醒"
            ),
        }

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
        events, _collection_id, error = await self._resolve_events_for_reminder_command(
            user_id, query_id
        )
        if error:
            return error
        if not events:
            return {"status": "error", "message": f"❌ 找不到日程 {query_id}"}

        now = now_in_timezone(user_id, self.db)
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
            remind_dt = self._normalize_remind_time(remind_time, now)
            if remind_dt is None:
                continue
            user_action = "preconfirmed" if remind_dt > now else "confirmed"
            await run_sync(
                self.db.confirm_reminder,
                event.id,
                user_action,
                owner_id=user_id,
                remind_time=remind_time,
                allow_future=True,
            )
            confirmed_count += 1

        if confirmed_count == 0:
            return {"status": "warning", "message": "⚠️ 匹配到的提醒时间均无法解析"}

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
    ) -> tuple[list[EventItem], str | None, CommandMessage | None]:
        """解析提醒命令目标，并标记是否明确指向整个集合。"""
        family = await self._load_event_family(user_id, query_id)
        if family.collection and family.leaf is None:
            return family.children, str(family.collection["id"]), None
        if family.leaf is not None:
            return [family.leaf], None, None

        single_event_id, event, error = await self._resolve_single_event_id_or_message(
            user_id, query_id
        )
        if error:
            return [], None, error
        if single_event_id and event:
            return [event], None, None

        return [], None, None

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
                if (parsed := EventHandler._normalize_remind_time(remind_time, now)) is not None
                and parsed > now
            ]
        if lowered == "today":
            return [
                remind_time
                for remind_time in remind_times
                if (parsed := EventHandler._normalize_remind_time(remind_time, now)) is not None
                and parsed.date() == now.date()
            ]

        matched = [
            remind_time
            for remind_time in remind_times
            if EventHandler._matches_reminder_selector(remind_time, selector, now)
        ]
        return matched

    @staticmethod
    def _normalize_remind_time(remind_time: str, reference: datetime) -> datetime | None:
        """把提醒时间统一到当前用户时区。"""
        try:
            return TimezoneHelper.parse(remind_time, reference.tzinfo)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _matches_reminder_selector(
        cls, remind_time: str, selector: str, reference: datetime
    ) -> bool:
        selector = (selector or "").strip()
        if not selector:
            return False

        normalized = cls._normalize_remind_time(remind_time, reference)
        if normalized is None:
            return False
        reminder_candidates = {remind_time, remind_time.replace("T", " ")}
        reminder_without_seconds = normalized.replace(second=0, microsecond=0).isoformat(
            timespec="minutes"
        )
        reminder_candidates.add(reminder_without_seconds)
        reminder_candidates.add(reminder_without_seconds.replace("T", " "))
        if selector in reminder_candidates:
            return True

        formats = ("%Y-%m-%d %H:%M", "%m-%d %H:%M", "%m月%d日 %H:%M")
        for fmt in formats:
            try:
                parsed = datetime.strptime(selector, fmt)
            except ValueError:
                continue

            if fmt.startswith("%m"):
                parsed = parsed.replace(year=normalized.year)
            normalized_wall_time = normalized.replace(tzinfo=None, second=0, microsecond=0)
            return parsed == normalized_wall_time
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

        family = await self._load_event_family(user_id, event_id)
        if family.collection and family.leaf is None:
            return await self._set_collection_reminders(user_id, family, reminder_desc)

        event, wrong_type = await self._db_get_typed_item_or_message(
            event_id, user_id, ItemType.EVENT.value, "日程"
        )
        if wrong_type:
            return wrong_type
        if not event:
            return {"status": "error", "message": f"❌ 找不到日程 {event_id}"}
        event = cast(EventItem, event)

        # 用 AI 解析提醒描述，事件开始时间是偏移计算基准。
        base_time = event.start_time
        if not base_time:
            return {"status": "error", "message": "❌ 该日程没有开始时间，无法计算提醒"}

        reminder_rules = await run_sync(
            self.ai_parser.build_reminder_rules_from_description,
            reminder_desc,
        )

        if not reminder_rules:
            return {
                "status": "error",
                "message": '❌ 未能从描述中解析出提醒时间，请尝试: "提前1天" "提前2小时30分钟" 等',
            }

        reminder_rules = with_start_time_reminder_rule(reminder_rules)
        remind_times = build_remind_times_from_rules(base_time, reminder_rules)
        await self._db_update_item(
            event_id,
            {"reminder_rules": reminder_rules, "remind_times": remind_times},
            owner_id=user_id,
            expected_version=event.version,
        )

        lines = [f"✅ 已更新提醒: {event.title or '无标题'}", f"🔔 共 {len(remind_times)} 个提醒"]
        display_timezone = event_display_timezone(event)
        for t in remind_times:
            lines.append(
                f"  ⏰ {ItemFormatter.format_datetime(t, '%m月%d日 %H:%M', tz=display_timezone)}"
            )
        lines.append(f"\n💡 用 /pendo event reminders {event_id} 查看详情")
        return {"status": "success", "message": "\n".join(lines)}

    async def _set_collection_reminders(
        self,
        user_id: str,
        family: EventFamily,
        reminder_desc: str,
    ) -> CommandMessage:
        collection = family.collection
        if not collection:
            return {"status": "error", "message": "❌ 找不到日程集合"}
        if not family.children:
            return {"status": "warning", "message": "⚠️ 该日程集合没有可设置提醒的节点"}

        reminder_rules = await run_sync(
            self.ai_parser.build_reminder_rules_from_description,
            reminder_desc,
        )

        if not reminder_rules:
            return {
                "status": "error",
                "message": '❌ 未能从描述中解析出提醒时间，请尝试: "提前1天" "提前2小时30分钟" 等',
            }

        reminder_rules = with_start_time_reminder_rule(reminder_rules)
        child_updates = {
            str(child.id): (
                build_remind_times_from_rules(child.start_time, reminder_rules),
                reminder_rules,
            )
            for child in family.children
        }
        await run_sync(
            self.db.update_event_collection_reminders,
            collection["id"],
            user_id,
            child_updates,
            reminder_rules,
        )
        return {
            "status": "success",
            "message": (
                f"✅ 已更新日程集合提醒: {collection.get('title') or '无标题'}\n"
                f"📊 共更新 {len(family.children)} 个节点"
            ),
        }

    @classmethod
    def _events_with_reminders_in_range(
        cls,
        events: list[EventItem],
        start_dt: datetime,
        end_dt: datetime,
        reference: datetime,
    ) -> list[tuple[EventItem, list[str]]]:
        """保留至少有一个提醒落在查询范围内的日程。"""
        matched: list[tuple[EventItem, list[str]]] = []
        for event in events:
            in_range = [
                remind_time
                for remind_time in parse_remind_times(event.remind_times)
                if cls._remind_in_range(remind_time, start_dt, end_dt, reference)
            ]
            if in_range:
                matched.append((event, in_range))
        return matched

    async def list_reminders(
        self, user_id: str, args: str, context: PendoContext
    ) -> CommandMessage:
        """查看日程提醒"""
        query = (args or "today").strip()

        # ID 查询直接显示该日程或集合的提醒详情。
        if self._looks_like_id(query):
            return await self._format_reminders_by_id(user_id, query)

        # 按用户时区解析范围，显式错误不得静默回退到今天。
        current_dt = await run_sync(now_in_timezone, user_id, self.db)
        try:
            start_date, end_date = parse_event_time_range(
                query,
                now=current_dt.replace(tzinfo=None),
                strict=True,
            )
        except ValueError:
            return {
                "status": "error",
                "message": "❌ 无法解析提醒时间范围",
            }
        start_dt, end_dt = datetime.fromisoformat(start_date), datetime.fromisoformat(end_date)

        # 直接读取该用户全部有提醒日程，避免“提前 30 天”一类
        # 人为窗口漏掉更早设置的提醒。
        reminder_items = await run_sync(self.db.get_all_events_with_reminders, user_id)
        events = [item for item in reminder_items if isinstance(item, EventItem)]

        # 不要求日程本身在范围内，只看提醒时刻。
        event_reminders = self._events_with_reminders_in_range(
            events,
            start_dt,
            end_dt,
            current_dt,
        )

        title = self._format_list_title(query, start_dt, end_dt)
        if not event_reminders:
            return {"status": "success", "message": f"🔔 {title} 没有提醒"}

        # 按最早的范围内提醒时间排序
        event_reminders.sort(
            key=lambda pair: (
                self._normalize_remind_time(pair[1][0], current_dt)
                or datetime.max.replace(tzinfo=current_dt.tzinfo)
            )
        )
        reminder_events = [event for event, _remind_times in event_reminders]
        collection_map, log_maps = await run_sync(
            self._load_reminder_list_metadata,
            user_id,
            reminder_events,
        )
        message = f"🔔 **{title}** (共{len(event_reminders)}项)\n"
        display_timezone = current_dt.tzinfo or TimezoneHelper.DEFAULT_TZ
        for event, remind_times in event_reminders:
            log_map = log_maps.get(str(event.id), {})
            time_str = ItemFormatter.format_datetime(
                event.start_time or "", "%m月%d日 %H:%M", tz=display_timezone
            )
            display_title = event.title or "无标题"
            if getattr(event, "event_collection_id", None):
                collection = collection_map.get(cast(str, event.event_collection_id))
                if collection:
                    display_title = f"{collection.get('title') or '无标题'} · {display_title}"
            message += f"\n🗓️ {time_str} {display_title} `{event.id}`\n"
            for t in remind_times:
                t_str = ItemFormatter.format_datetime(t, "%m-%d %H:%M", tz=display_timezone)
                status = get_remind_status(log_map.get(t))
                message += f"  ⏰ {t_str} {status}\n"

        return {"status": "success", "message": message}

    async def _format_reminders_by_id(self, user_id: str, query_id: str) -> CommandMessage:
        """按ID格式化提醒信息

        支持 collection id、leaf id 或单次事件 id。
        """
        family = await self._load_event_family(user_id, query_id)
        if family.collection and family.leaf is None:
            log_maps = await run_sync(
                self.db.get_reminder_logs_by_item_ids,
                user_id,
                [str(child.id) for child in family.children],
            )
            return self._format_collection_reminders(family, log_maps)
        if family.collection and family.leaf is not None:
            log_maps = await run_sync(
                self.db.get_reminder_logs_by_item_ids,
                user_id,
                [str(family.leaf.id)],
            )
            return self._format_leaf_reminders_with_collection(
                family,
                log_maps.get(str(family.leaf.id), {}),
            )
        if family.leaf is not None:
            log_maps = await run_sync(
                self.db.get_reminder_logs_by_item_ids,
                user_id,
                [str(family.leaf.id)],
            )
            return format_event_reminders(
                family.leaf,
                log_maps.get(str(family.leaf.id), {}),
            )

        # 事件图未命中时再读取原条目，以便返回准确的类型错误。
        item = await self._db_get_item(query_id, owner_id=user_id)
        if item:
            if not isinstance(item, EventItem):
                return self._build_wrong_type_message(query_id, "日程", item)
            event = cast(EventItem, item)
            log_maps = await run_sync(
                self.db.get_reminder_logs_by_item_ids,
                user_id,
                [str(event.id)],
            )
            return format_event_reminders(event, log_maps.get(str(event.id), {}))

        return {"status": "error", "message": f"❌ 找不到日程: {query_id}"}

    def _format_leaf_reminders_with_collection(
        self,
        family: EventFamily,
        log_map: dict[str, dict[str, Any]],
    ) -> CommandMessage:
        event = family.leaf
        collection = family.collection
        if event is None or collection is None:
            return {"status": "error", "message": "❌ 找不到日程"}
        remind_times = parse_remind_times(event.remind_times)
        if not remind_times:
            return {
                "status": "info",
                "message": (
                    f"🔔 日程: {collection.get('title') or '无标题'} · {event.title or '无标题'}\n\n"
                    "未设置提醒"
                ),
            }

        builder = MessageBuilder()
        display_timezone = event_display_timezone(event)
        builder.add_line(f"🔔 **{collection.get('title') or '无标题'}**")
        builder.add_line(f"📌 {event.title or '无标题'}")
        builder.add_line(
            f"🗓️ 节点时间: {ItemFormatter.format_datetime(event.start_time or '', '%m月%d日 %H:%M', tz=display_timezone)}"
        )
        builder.add_line("─" * 30)
        for index, remind_time in enumerate(remind_times, 1):
            time_str = ItemFormatter.format_datetime(
                remind_time, "%m月%d日 %H:%M", tz=display_timezone
            )
            status = get_remind_status_label(log_map.get(remind_time))
            builder.add_line(f"⏰ **提醒 {index}**: {time_str}  {status}")
        return {"status": "success", "message": builder.build()}

    def _format_collection_reminders(
        self,
        family: EventFamily,
        log_maps: dict[str, dict[str, dict[str, Any]]],
    ) -> CommandMessage:
        collection = family.collection
        if not collection:
            return {"status": "error", "message": "❌ 找不到日程集合"}
        builder = MessageBuilder()
        builder.add_line(f"🔔 **{collection.get('title') or '无标题'}** 的提醒列表")
        builder.add_line(f"📊 共 {len(family.children)} 个节点")
        builder.add_line("─" * 30)
        for index, child in enumerate(family.children, 1):
            remind_times = parse_remind_times(child.remind_times)
            builder.add_blank()
            display_timezone = event_display_timezone(child)
            child_time = ItemFormatter.format_datetime(
                child.start_time or "", "%m月%d日 %H:%M", tz=display_timezone
            )
            builder.add_line(f"**{index}.** 📌 {child_time} {child.title or '无标题'}")
            if not remind_times:
                builder.add_line("     ⏰ 无提醒")
            else:
                log_map = log_maps.get(str(child.id), {})
                for remind_time in remind_times:
                    formatted_time = ItemFormatter.format_datetime(
                        remind_time, "%m-%d %H:%M", tz=display_timezone
                    )
                    status = get_remind_status(log_map.get(remind_time))
                    builder.add_line(f"     ⏰ {formatted_time} {status}")
            builder.add_line(f"     🆔 `{child.id}`")
        return {"status": "success", "message": builder.build()}

    # ==================== 辅助方法 ====================

    @classmethod
    def _remind_in_range(
        cls, t_str: str, start_dt: datetime, end_dt: datetime, reference: datetime
    ) -> bool:
        """判断单个提醒时间是否在查询范围内"""
        remind_dt = cls._normalize_remind_time(t_str, reference)
        if remind_dt is None:
            return False
        return start_dt <= remind_dt.replace(tzinfo=None) <= end_dt

    @staticmethod
    def _event_in_range(
        event: EventItem,
        start_dt: datetime,
        end_dt: datetime,
        reference: datetime,
    ) -> bool:
        """按用户时区判断日程与查询区间是否重叠。"""
        if not event.start_time:
            return False
        try:
            event_start = TimezoneHelper.parse(event.start_time, reference.tzinfo).replace(
                tzinfo=None
            )
            event_end = (
                TimezoneHelper.parse(event.end_time, reference.tzinfo).replace(tzinfo=None)
                if event.end_time
                else None
            )
        except (TypeError, ValueError):
            return False
        if event_end is not None:
            return event_start <= end_dt and event_end >= start_dt
        return start_dt <= event_start <= end_dt

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
            return EventHandler._attach_event_timezone(wall_time, event_timezone)

        match = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", candidate)
        if match and current_dt is not None:
            hour = int(match.group(1))
            minute = match.group(2)
            second = match.group(3) or "00"
            wall_time = f"{current_dt.date().isoformat()}T{hour:02d}:{minute}:{second}"
            return EventHandler._attach_event_timezone(wall_time, event_timezone)

        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", candidate):
            time_part = "00:00:00"
            if current_dt is not None:
                time_part = current_dt.time().isoformat(timespec="seconds")
            return EventHandler._attach_event_timezone(
                f"{candidate}T{time_part}",
                event_timezone,
            )

        return candidate or None

    @staticmethod
    def _attach_event_timezone(wall_time: str, event_timezone: ZoneInfo) -> str | None:
        """Resolve one event wall time to an instant, rejecting DST gaps and folds."""
        try:
            normalized = normalize_event_fields(
                {"start_time": wall_time},
                partial=True,
            )["start_time"]
            parsed = datetime.fromisoformat(normalized)
            return resolve_source_wall_time(parsed, "start_time", event_timezone).isoformat(
                timespec="seconds"
            )
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

    def _looks_like_id(self, text: str) -> bool:
        """判断是否像ID（collection id、recurring occurrence id 或 node id）"""
        if not text:
            return False
        # 新集合使用完整 UUID 十六进制字符串；同时兼容历史 8 位 ID。
        collection_id_pattern = r"[0-9a-f]{8}(?:[0-9a-f]{24})?"
        if "_" in text:
            parts = text.rsplit("_", 1)
            return (
                re.fullmatch(collection_id_pattern, parts[0], re.IGNORECASE) is not None
                and re.fullmatch(r"(?:\d{8}|m\d{2,})", parts[1], re.IGNORECASE) is not None
            )
        return re.fullmatch(collection_id_pattern, text, re.IGNORECASE) is not None

    async def _resolve_single_event_id_or_message(
        self, user_id: str, event_id: str
    ) -> tuple[str | None, EventItem | None, CommandMessage | None]:
        """解析直接日程或 leaf ID，并生成类型错误消息。"""
        event_id = (event_id or "").strip()
        item = await self._db_get_item(event_id, owner_id=user_id)
        if item is None:
            return None, None, {"status": "error", "message": f"❌ 找不到日程 {event_id}"}
        if not isinstance(item, EventItem):
            return None, None, self._build_wrong_type_message(event_id, "日程", item)
        return event_id, item, None
