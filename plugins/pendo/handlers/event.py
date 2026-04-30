"""
日程(Event)处理器
处理日程相关的所有操作，使用AI解析自然语言
"""

from typing import Any, TYPE_CHECKING, Protocol, cast
from datetime import datetime, timedelta
from itertools import islice
import logging
import re
import uuid
from core.plugin_base import run_sync
from ..utils.db_ops import DbOpsMixin
from ..utils.error_handlers import error_result, handle_command_errors
from ..config import PendoConfig
from ..utils.time_utils import parse_event_time_range, TimezoneHelper, now_in_timezone, parse_remind_times
from ..models.item import EventItem, ItemType
from ..core.types import PendoContext, CommandMessage
from ..core.router import TOP_LEVEL_REDIRECTS
from ..utils.settings_utils import resolve_default_category
from ..utils.validators import build_remind_times_from_rules
from ..services.event_graph import EventFamily, EventGraphService
from .event_support import (
    ensure_start_time_reminder,
    ensure_event_reminders,
    ensure_event_reminder_rules,
    format_conflicts,
    format_event_created,
    format_event_reminders,
    format_milestone_event_created,
    format_recurring_event_created,
    get_remind_status,
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
        self.event_graph = EventGraphService(db)

    async def _fetch_event_rows(self, user_id: str, start_date: str, end_date: str) -> list[EventItem]:
        rows = await run_sync(self.db.items.get_events_for_range, user_id, start_date, end_date)
        return cast(list[EventItem], rows)

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

    def _new_event_collection_id(self) -> str:
        """Generate a collection ID that does not collide with known rows."""
        for _ in range(20):
            candidate = uuid.uuid4().hex[:8]
            try:
                collection = self.db.items.get_event_collection(candidate)
                item = self.db.items.get_item(candidate)
            except Exception:
                return candidate
            if collection is None and item is None:
                return candidate
            if not isinstance(collection, dict) and not isinstance(item, EventItem):
                return candidate
        return uuid.uuid4().hex[:12]

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
        self,
        user_id: str,
        parsed_data: dict[str, Any],
        remind_times: list[str],
        allow_conflict: bool = False,
    ) -> CommandMessage:
        """创建重复日程"""
        try:
            from dateutil.rrule import rrulestr

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
            instances = list(islice(rrule_obj, PendoConfig.EVENT_MAX_RRULE_COUNT))

            if not instances:
                return {"status": "error", "message": "❌ 没有生成任何重复实例"}

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

            collection_id = self._new_event_collection_id()
            await run_sync(
                self.db.items.create_event_collection,
                {
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
                        (instances[-1] + duration).isoformat()
                        if duration
                        else instances[-1].isoformat()
                    ),
                },
            )
            created_ids = []

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
                    notes=parsed_data.get("notes", ""),  # I-9修复：重复事件也传入notes
                    event_role="recurring_occurrence",
                    event_collection_id=collection_id,
                    event_collection_kind="recurring",
                    event_index=index,
                    event_node_key=instance_dt.strftime("%Y%m%d"),
                    created_at=datetime.now().isoformat(),
                    updated_at=datetime.now().isoformat(),
                )

                instance_id = f"{collection_id}_{instance_dt.strftime('%Y%m%d')}"
                instance_item.id = instance_id

                await run_sync(self.db.items.insert_item, instance_item, instance_id)
                created_ids.append(instance_id)

            # 记录日志
            await run_sync(
                self.db.logs.log_operation,
                user_id=user_id,
                action="create_recurring",
                item_type="event",
                item_id=collection_id,
                details={"title": parsed_data.get("title"), "instances": len(created_ids)},
            )

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

        collection_id = self._new_event_collection_id()
        await run_sync(
            self.db.items.create_event_collection,
            {
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
            },
        )

        created_nodes: list[EventItem] = []
        all_reminders: set[str] = set()
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
                created_at=datetime.now().isoformat(),
                updated_at=datetime.now().isoformat(),
            )
            node_id = f"{collection_id}_{node_key}"
            node.id = node_id
            await run_sync(self.db.items.insert_item, node, node_id)
            created_nodes.append(node)

        await run_sync(
            self.db.logs.log_operation,
            user_id=user_id,
            action="create_multi_node",
            item_type="event",
            item_id=collection_id,
            details={
                "title": parsed_data.get("title"),
                "child_ids": [node.id for node in created_nodes],
            },
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

        family = await self._load_event_family(user_id, event_id)
        if family.kind != "missing":
            return self._format_event_family_detail(family, event_id)

        single_event_id, event, error = await self._resolve_single_event_id_or_message(
            user_id, event_id
        )
        if error:
            return error
        if not single_event_id or not event:
            return {"status": "error", "message": f"❌ 找不到日程 {event_id}"}

        title = event.title or "无标题"
        notes = getattr(event, "notes", None) or ""
        remind_times = parse_remind_times(event.remind_times)

        lines = [f"📋 **{title}**", ""]

        time_str = ItemFormatter.format_time_range(event.start_time, event.end_time)
        event_type = (
            "🔄 重复日程"
            if getattr(event, "event_collection_kind", None) == "recurring"
            else "📆 单次事件"
        )
        lines.append(f"{event_type}")
        lines.append(f"⏰ {time_str}")

        if event.location:
            lines.append(f"📍 {event.location}")
        if notes:
            lines.append(f"📝 {notes}")
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

    async def _load_event_family(self, user_id: str, event_or_collection_id: str) -> EventFamily:
        return await run_sync(self.event_graph.load_by_id, user_id, event_or_collection_id)

    def _format_event_family_detail(
        self, family: EventFamily, query_id: str
    ) -> CommandMessage:
        if family.collection and family.leaf is None:
            collection = family.collection
            children = family.children
            kind_label = "多时间节点事件" if collection.get("kind") == "multi_node" else "重复日程"
            lines = [
                f"📋 **{collection.get('title') or '无标题'}**",
                "",
                f"🗺️ {kind_label} ({len(children)}个节点)",
            ]
            if collection.get("location"):
                lines.append(f"📍 {collection['location']}")
            if collection.get("notes"):
                lines.append(f"📝 {collection['notes']}")
            if collection.get("tags"):
                lines.append(f"🏷️ {', '.join(collection['tags'])}")
            lines.append("")
            for child in children:
                child_time = ItemFormatter.format_datetime(
                    child.start_time or "",
                    "%m月%d日 %H:%M",
                )
                lines.append(f"  📌 {child_time} {child.title or '无标题'} `{child.id}`")
            lines.append("")
            lines.append(f"`{collection['id']}`")
            lines.append(f"💡 /pendo event edit {collection['id']} <内容> 编辑标题/元信息")
            return {"status": "success", "message": "\n".join(lines)}

        if family.leaf is None:
            return {"status": "error", "message": f"❌ 找不到日程 {query_id}"}

        event = family.leaf
        collection = family.collection
        title = event.title or "无标题"
        remind_times = parse_remind_times(event.remind_times)
        lines = [f"📋 **{title}**", ""]

        if collection:
            lines.append(f"🗓️ 所属: {collection.get('title') or '无标题'}")
            lines.append("📌 节点日程" if collection.get("kind") == "multi_node" else "🔄 重复实例")
        else:
            lines.append("📆 单次事件")
        lines.append(f"⏰ {ItemFormatter.format_time_range(event.start_time, event.end_time)}")

        if event.location:
            lines.append(f"📍 {event.location}")
        if event.notes:
            lines.append(f"📝 {event.notes}")
        if event.tags:
            lines.append(f"🏷️ {', '.join(event.tags)}")

        lines.append("")
        if remind_times:
            lines.append(f"🔔 提醒 ({len(remind_times)}个):")
            for t in remind_times[:5]:
                lines.append(f"  ⏰ {ItemFormatter.format_datetime(t, '%m月%d日 %H:%M')}")
            if len(remind_times) > 5:
                lines.append(f"  … 共{len(remind_times)}个提醒")
        else:
            lines.append("🔔 未设置提醒")

        if collection and family.children:
            siblings = [child for child in family.children if child.id != event.id]
            if siblings:
                lines.append("")
                lines.append("同组其他节点:")
                for sibling in siblings:
                    sibling_time = ItemFormatter.format_datetime(
                        sibling.start_time or "",
                        "%m月%d日 %H:%M",
                    )
                    lines.append(f"  • {sibling_time} {sibling.title or '无标题'} `{sibling.id}`")

        lines.append(f"\n`{event.id}`")
        lines.append(f"💡 /pendo event reminders {event.id} | /pendo event edit {event.id} <内容>")
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

    @staticmethod
    def _format_graph_list_item(
        event: EventItem,
        current_dt: datetime,
        collection: dict[str, Any],
    ) -> tuple[str | None, str]:
        if not event.start_time:
            return None, ""
        ev_start_dt = datetime.fromisoformat(event.start_time)
        date_str = ev_start_dt.strftime("%Y-%m-%d")
        time_str = ItemFormatter.format_time_range(event.start_time, event.end_time)
        collection_title = collection.get("title") or "无标题"
        marker = "📌" if collection.get("kind") == "multi_node" else "🔄"
        text = f"• {time_str} {collection_title} · {event.title or '无标题'} {marker}"
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
            events = await self._fetch_event_rows(
                user_id, start_date, end_date
            )

            # 时间过滤
            # 多节点事件用区间重叠；单次事件只看 start_time
            start_dt, end_dt = datetime.fromisoformat(start_date), datetime.fromisoformat(end_date)
            events = [e for e in events if self._event_in_range(e, start_dt, end_dt)]

            # 分类/标签过滤
            if cat_filter:
                events = [e for e in events if (e.category or "") == cat_filter]
            if tag_filter:
                events = [e for e in events if tag_filter in (e.tags or [])]

            events.sort(key=lambda event: event.start_time or "")

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
            collection_cache: dict[str, dict[str, Any] | None] = {}

            for event in events:
                collection = None
                if getattr(event, "event_collection_id", None):
                    collection_id = cast(str, event.event_collection_id)
                    if collection_id not in collection_cache:
                        collection_cache[collection_id] = self.db.items.get_event_collection(
                            collection_id,
                            user_id,
                        )
                    collection = collection_cache[collection_id]

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
            self.db.items.update_event_collection,
            collection["id"],
            collection_updates,
            user_id,
        )
        if not success:
            return {"status": "error", "message": f"❌ 更新失败: {collection['id']}"}

        await self._db_log_operation(
            user_id=user_id,
            action="edit_event_collection",
            item_type="event",
            item_id=collection["id"],
            details={"updates": collection_updates},
        )
        return {
            "status": "success",
            "message": f"✅ 已更新日程集合: {collection_updates.get('title', collection.get('title') or '无标题')}",
        }

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
                instance_id, updates, user_id, action="edit_event"
            )


            return {
                "status": "success",
                "message": f"✅ 已更新日程: {updates.get('title', title)}\n\n💡 /pendo event reminders {instance_id} 查看提醒 | /pendo undo 撤销编辑",
            }
        except Exception as e:
            logger.exception("Failed to edit instance: %s", e)
            return {"status": "error", "message": f"❌ 编辑失败: {str(e)}"}

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
        await run_sync(
            lambda: self.db.items.delete_event_collection(
                collection["id"],
                user_id,
                cascade=True,
            )
        )
        await self._db_log_operation(
            user_id=user_id,
            action="delete_event_collection",
            item_type="event",
            item_id=collection["id"],
            details={"child_ids": [child.id for child in family.children]},
        )
        return {
            "status": "success",
            "message": f"🗑️ 已删除日程集合: {collection.get('title') or '无标题'}\n📊 共删除 {len(family.children)} 个节点",
        }

    async def _delete_single_instance(self, user_id: str, instance_id: str) -> CommandMessage:
        """删除单个实例"""
        event = cast(EventItem | None, await self._db_get_item(instance_id, owner_id=user_id))
        if not event:
            return {"status": "error", "message": f"❌ 找不到日程 {instance_id}"}

        await self._db_soft_delete_with_log(instance_id, user_id, item_type=ItemType.EVENT.value)

        collection_id = getattr(event, "event_collection_id", None)
        if collection_id and getattr(event, "event_collection_kind", None) == "multi_node":
            remaining = await run_sync(self.db.items.get_collection_events, collection_id, user_id)
            if not remaining:
                await run_sync(
                    lambda: self.db.items.delete_event_collection(
                        collection_id,
                        user_id,
                        cascade=False,
                    )
                )

        e_title = event.title
        return {
            "status": "success",
            "message": f"🗑️ 已删除日程: {e_title or '无标题'}\n💡 5分钟内可使用 /pendo undo 撤销",
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
        family = await self._load_event_family(user_id, query_id)
        if family.collection and family.leaf is None:
            return family.children, None
        if family.leaf is not None:
            return [family.leaf], None

        single_event_id, event, error = await self._resolve_single_event_id_or_message(
            user_id, query_id
        )
        if error:
            return [], error
        if single_event_id and event:
            return [event], None

        return [], None

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

        for child in family.children:
            await self._db_update_item(
                child.id,
                {
                    "reminder_rules": reminder_rules,
                    "remind_times": build_remind_times_from_rules(child.start_time, reminder_rules),
                },
                owner_id=user_id,
            )
        await run_sync(
            self.db.items.update_event_collection,
            collection["id"],
            {"reminder_rules": reminder_rules},
            user_id,
        )
        return {
            "status": "success",
            "message": (
                f"✅ 已更新日程集合提醒: {collection.get('title') or '无标题'}\n"
                f"📊 共更新 {len(family.children)} 个节点"
            ),
        }

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
            events = await self._fetch_event_rows(
                user_id, start_date, extended_end
            )

            # 只保留至少有一个提醒时间在查询范围内的条目（不要求事件本身在范围内）
            event_reminders: list[tuple[EventItem, list[str]]] = []
            for e in events:
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
                time_str = ItemFormatter.format_datetime(event.start_time or "", "%m月%d日 %H:%M")
                display_title = event.title or "无标题"
                if getattr(event, "event_collection_id", None):
                    collection = self.db.items.get_event_collection(
                        event.event_collection_id,
                        user_id,
                    )
                    if collection:
                        display_title = (
                            f"{collection.get('title') or '无标题'} · {display_title}"
                        )
                message += f"\n🗓️ {time_str} {display_title} `{event.id}`\n"
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

        支持 collection id、leaf id 或单次事件 id。
        """
        family = await self._load_event_family(user_id, query_id)
        if family.collection and family.leaf is None:
            return self._format_collection_reminders(family)
        if family.collection and family.leaf is not None:
            return self._format_leaf_reminders_with_collection(family)

        # 先尝试直接获取
        item = await self._db_get_item(query_id, owner_id=user_id)
        if item:
            if not isinstance(item, EventItem):
                return self._build_wrong_type_message(query_id, "日程", item)
            event = cast(EventItem, item)
            return format_event_reminders(event, self._build_log_map(event.id))

        return {"status": "error", "message": f"❌ 找不到日程: {query_id}"}

    def _format_leaf_reminders_with_collection(self, family: EventFamily) -> CommandMessage:
        event = family.leaf
        collection = family.collection
        if event is None or collection is None:
            return {"status": "error", "message": "❌ 找不到日程"}
        log_map = self._build_log_map(event.id)
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
        builder.add_line(f"🔔 **{collection.get('title') or '无标题'}**")
        builder.add_line(f"📌 {event.title or '无标题'}")
        builder.add_line(
            f"🗓️ 节点时间: {ItemFormatter.format_datetime(event.start_time or '', '%m月%d日 %H:%M')}"
        )
        builder.add_line("─" * 30)
        status_labels = {"✅": "✅ 已确认", "📩": "📩 已发送未确认", "⏳": "⏳ 待发送"}
        for index, remind_time in enumerate(remind_times, 1):
            time_str = ItemFormatter.format_datetime(remind_time, "%m月%d日 %H:%M")
            status = status_labels[get_remind_status(log_map.get(remind_time))]
            builder.add_line(f"⏰ **提醒 {index}**: {time_str}  {status}")
        return {"status": "success", "message": builder.build()}

    def _format_collection_reminders(self, family: EventFamily) -> CommandMessage:
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
            child_time = ItemFormatter.format_datetime(child.start_time or "", "%m月%d日 %H:%M")
            builder.add_line(f"**{index}.** 📌 {child_time} {child.title or '无标题'}")
            if not remind_times:
                builder.add_line("     ⏰ 无提醒")
            else:
                log_map = self._build_log_map(child.id)
                for remind_time in remind_times:
                    formatted_time = ItemFormatter.format_datetime(remind_time, "%m-%d %H:%M")
                    status = get_remind_status(log_map.get(remind_time))
                    builder.add_line(f"     ⏰ {formatted_time} {status}")
            builder.add_line(f"     🆔 `{child.id}`")
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

        每个可调度事件都是 leaf，按自己的 start_time 判断。
        """
        if not e.start_time:
            return False
        e_start = datetime.fromisoformat(e.start_time)
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

        if parsed.get("remind_times"):
            updates["remind_times"] = parsed["remind_times"]

        if (
            parsed.get("notes") is not None
            and parsed.get("notes") != getattr(current_event, "notes", None)
        ):
            updates["notes"] = parsed["notes"]

        heuristic_notes = self._extract_notes_update(changes)
        if (
            heuristic_notes is not None
            and heuristic_notes != getattr(current_event, "notes", None)
        ):
            updates["notes"] = heuristic_notes

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

    def _looks_like_id(self, text: str) -> bool:
        """判断是否像ID（collection id、recurring occurrence id 或 node id）"""
        if not text:
            return False
        if "_" in text:
            parts = text.rsplit("_", 1)
            return (
                re.match(r"^[0-9a-f]{8}$", parts[0]) is not None
                and re.match(r"^(\d{8}|m\d{2,})$", parts[1]) is not None
            )
        return re.match(r"^[0-9a-f]{8}$", text) is not None

    async def _resolve_single_event_id_or_message(
        self, user_id: str, event_id: str
    ) -> tuple[str | None, EventItem | None, CommandMessage | None]:
        """Resolve direct event/leaf IDs."""
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
            return None, None, {"status": "error", "message": f"❌ 找不到日程 {event_id}"}
        if not isinstance(item, EventItem):
            return None, None, self._build_wrong_type_message(event_id, "日程", item)
        return event_id, cast(EventItem, item), None

    def _build_log_map(self, event_id: str) -> dict[str, dict[str, Any]]:
        """Build {remind_time_iso: log_dict} for an event.

        每个 (item_id, remind_time) 在 DB 中只有一行，无需去重。
        """
        return {log["remind_time"]: log for log in self.db.get_reminder_logs(event_id)}
