"""
待办(Task)处理器
按照计划日期、硬截止和分类管理待办事项，不需要AI解析
"""

from typing import Any, TYPE_CHECKING, Iterable, cast
from datetime import datetime, timedelta
import re
import logging
from ..models.item import ItemType, TaskStatus, TaskItem
from ..models.constants import ItemFields
from ..core.exceptions import OwnershipException, MissingRequiredFieldException
from ..core.types import PendoContext, CommandMessage
from ..core.router import TOP_LEVEL_REDIRECTS
from ..utils.time_utils import now_in_timezone, TimezoneHelper
from core.plugin_base import run_sync
from ..utils.db_ops import DbOpsMixin
from ..utils.error_handlers import handle_command_errors
from ..config import PendoConfig
from ..utils.formatters import ItemFormatter, format_success_message, extract_metadata, paginate
from ..utils.validators import default_task_plan_date, normalize_task_fields

logger = logging.getLogger(__name__)


def _enum_val(x):
    """Return the .value of an Enum, or x itself if it's already a plain value."""
    return x.value if hasattr(x, "value") else x


if TYPE_CHECKING:
    from ..services.db import Database


def _sort_category_keys(keys: Iterable[str]) -> list[str]:
    return sorted(keys)


def _task_category_label(task: TaskItem) -> str:
    return str(getattr(task, "category", None) or "").strip() or "未分类"


def _parse_date_text(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value)
        if len(text) == 10:
            return datetime.fromisoformat(f"{text}T00:00:00")
        return datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None


def _task_plan_key(task: TaskItem) -> str:
    return str(getattr(task, "plan_date", None) or "").strip()


def _task_deadline_key(task: TaskItem) -> str:
    return str(getattr(task, "deadline_at", None) or "").strip()


def _task_sort_key(task: TaskItem) -> tuple:
    plan = _task_plan_key(task) or "9999-12-31"
    deadline = _task_deadline_key(task) or "9999-12-31T99:99:99"
    return (_enum_val(getattr(task, "priority", None)) or 3, plan, deadline, task.created_at or "")


class TaskHandler(DbOpsMixin):
    """待办处理器

    按计划日期(plan/date)、硬截止(deadline/due)和文字分类(cat)管理待办事项。

    不需要AI解析，直接规则解析
    """

    def __init__(self, db: "Database"):
        self.db = db

    def _user_local_now(self, user_id: str) -> datetime:
        current = now_in_timezone(user_id, self.db)
        return current.replace(tzinfo=None) if current.tzinfo else current

    @handle_command_errors
    async def handle(
        self, user_id: str, args: str, context: PendoContext, group_id: int | None = None
    ) -> CommandMessage:
        """处理待办相关命令

        命令格式：
        - /pendo todo add <内容> [plan:YYYY-MM-DD] [deadline:YYYY-MM-DDTHH:MM] [cat:xxx] [p:1-5]
        - /pendo todo list [today/open/done/cancelled/overdue/upcoming/inbox/分类] [all|page:n]
        - /pendo todo view <id>
        - /pendo todo done <id>
        - /pendo todo cancel <id>
        - /pendo todo undone <id>
        - /pendo todo delete <id|cat:xxx>
        - /pendo todo edit <id> <内容>
        """
        parts = args.split(maxsplit=1)
        if not parts:
            return await self.list_all_categories(user_id, context)

        command = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""

        handlers = {
            "add": lambda: self.add_task(user_id, rest, context, group_id),
            "list": lambda: self.list_tasks(user_id, rest, context),
            "view": lambda: self.view_task(user_id, rest, context),
            "done": lambda: self.mark_done(user_id, rest, context),
            "cancel": lambda: self.mark_cancelled(user_id, rest, context),
            "undone": lambda: self.mark_undone(user_id, rest, context),
            "delete": lambda: self.delete_task(user_id, rest, context),
            "edit": lambda: self.edit_task(user_id, rest, context),
        }

        handler = handlers.get(command)
        if handler:
            return await handler()

        if self._should_treat_as_list_shortcut(command, rest):
            return await self.list_tasks(user_id, args, context)

        if command in TOP_LEVEL_REDIRECTS:
            return {"status": "error", "message": f"❌ 正确用法:\n\n{TOP_LEVEL_REDIRECTS[command]}"}

        return {
            "status": "error",
            "message": (
                f"❌ 未知待办命令: {command}\n\n"
                "可用命令:\n"
                "• /pendo todo add <内容>\n"
                "• /pendo todo list [分类]\n"
                "• /pendo todo view <id>\n"
                "• /pendo todo done <id>\n"
                "• /pendo todo cancel <id>\n"
                "• /pendo todo undone <id>\n"
                "• /pendo todo edit <id> <内容>\n"
                "• /pendo todo delete <id|cat:分类>"
            ),
        }

    @staticmethod
    def _should_treat_as_list_shortcut(command: str, rest: str) -> bool:
        """Preserve shorthand list queries while rejecting obvious mistyped commands."""
        command = (command or "").strip().lower()
        rest = (rest or "").strip()

        if not command:
            return False

        single_token_shortcuts = {
            "today",
            "open",
            "done",
            "undone",
            "cancelled",
            "todo",
            "overdue",
            "upcoming",
            "inbox",
            "已完成",
            "未完成",
            "已取消",
        }
        if command in single_token_shortcuts or re.fullmatch(r"\d{4}-\d{2}-\d{2}", command):
            return True

        if not rest:
            # `/pendo todo 工作`
            return True

        valid_modifiers = {
            "open",
            "done",
            "undone",
            "cancelled",
            "todo",
            "已完成",
            "未完成",
            "已取消",
            "all",
        }
        for token in rest.split():
            lower = token.lower()
            if lower in valid_modifiers:
                continue
            if token.startswith("page:"):
                try:
                    int(token.split(":", 1)[1])
                    continue
                except (IndexError, ValueError):
                    return False
            if token.startswith("p:"):
                try:
                    int(token.split(":", 1)[1])
                    continue
                except (IndexError, ValueError):
                    return False
            return False

        return True

    async def add_task(
        self, user_id: str, text: str, context: PendoContext, group_id: int | None = None
    ) -> CommandMessage:
        """添加待办

        格式：
        - /pendo todo add 事件  -> 计划到当天（晚上8点后自动计划到第二天）
        - /pendo todo add 事件 plan:2026-05-01 deadline:2026-05-01T18:00 cat:工作 p:1
        """
        if not text:
            return {
                "status": "error",
                "message": "❌ 请提供待办内容\n\n用法: /pendo todo add <内容> [plan:YYYY-MM-DD] [deadline:YYYY-MM-DDTHH:MM] [cat:xxx] [p:1-5]",
            }

        # 解析参数
        parsed = self._parse_task_text(text, user_id)

        # 创建待办数据
        from ..models.item import TaskItem

        local_now = self._user_local_now(user_id)
        task_payload = normalize_task_fields(
            {
                "owner_id": user_id,
                "title": parsed["title"],
                "content": parsed.get("content", ""),
                "category": parsed["category"],
                "plan_date": parsed["plan_date"],
                "deadline_at": parsed["deadline_at"],
                "priority": parsed["priority"],
                "status": TaskStatus.OPEN.value,
                "tags": parsed.get("tags", []),
                "remind_times": parsed.get("remind_times", []),
                "context": {"group_id": group_id} if group_id else {},
                "created_at": local_now.isoformat(),
                "updated_at": local_now.isoformat(),
            }
        )
        task_item = TaskItem(**task_payload)

        # 保存到数据库
        item_id = await self._db_create_with_log(task_item, owner_id=user_id, action="create_task")

        task_item.id = item_id

        # 格式化返回消息
        priority_str = ItemFormatter.format_priority(parsed["priority"])
        message = f"✅ 已添加待办\n\n"
        message += f"📝 {parsed['title']}\n"
        message += f"📅 计划: {parsed['plan_date'] or '未安排'}\n"
        if parsed.get("deadline_at"):
            message += f"⏰ 截止: {ItemFormatter.format_datetime(parsed['deadline_at'])}\n"
        message += f"📂 分类: {parsed['category']}"

        now = local_now
        if now.hour >= 20 and parsed["plan_date"] == (now + timedelta(days=1)).strftime("%Y-%m-%d"):
            message += " (明天)"

        message += f"\n⚡ 优先级: {priority_str}\n"
        message += f"`{item_id}`\n\n"
        message += f"💡 用 /pendo todo done {item_id} 完成"

        return {"status": "success", "message": message, "item_id": item_id}

    def _parse_task_text(self, text: str, user_id: str) -> dict[str, Any]:
        """解析待办文本（纯规则解析，不用AI）

        支持格式：
        - 事件内容 plan:2026-05-01 deadline:2026-05-01T18:00 cat:工作 p:1
        - cat:工作 事件内容 p:1
        """
        plan_date, text = self._extract_inline_param(text, ("plan", "date"))
        deadline_at, text = self._extract_inline_param(text, ("deadline", "due"))
        remind_raw, text = self._extract_inline_param(text, ("remind", "reminder"))
        meta = extract_metadata(text, with_priority=True)
        remind_times = [
            value.strip()
            for value in re.split(r"[,，]", remind_raw or "")
            if value.strip()
        ]
        return {
            "title": meta["text"] or "无标题待办",
            "content": "",
            "category": meta["category"] or "未分类",
            "plan_date": plan_date or default_task_plan_date(self._user_local_now(user_id)),
            "deadline_at": deadline_at,
            "priority": meta["priority"] or 3,
            "tags": meta["tags"],
            "remind_times": remind_times,
        }

    @staticmethod
    def _extract_inline_param(text: str, keys: tuple[str, ...]) -> tuple[str | None, str]:
        for key in keys:
            match = re.search(rf"{re.escape(key)}:(\S+)", text)
            if match:
                return match.group(1), text.replace(match.group(0), "").strip()
        return None, text

    async def list_all_categories(self, user_id: str, context: PendoContext) -> CommandMessage:
        """列出所有分类（/pendo todo list 不带参数时）"""
        # 查询所有未删除的待办，按分类分组
        tasks = cast(
            list[TaskItem], await run_sync(self.db.items.get_items, user_id, {"type": ItemType.TASK.value}, 1000)
        )

        if not tasks:
            return {
                "status": "success",
                "message": "📝 **待办列表**\n\n暂无待办事项\n\n💡 用 /pendo todo add <内容> 添加待办",
            }

        # 按分类分组统计
        categories = {}
        for task in tasks:
            cat = _task_category_label(task)
            if cat not in categories:
                categories[cat] = {"done": 0, "open": 0, "cancelled": 0}

            status_val = _enum_val(task.status)

            if status_val == TaskStatus.DONE.value:
                categories[cat]["done"] += 1
            elif status_val == TaskStatus.CANCELLED.value:
                categories[cat]["cancelled"] += 1
            else:
                categories[cat]["open"] += 1

        # 格式化输出
        message = "📝 **待办分类列表**\n\n"

        for cat in _sort_category_keys(categories):
            stats = categories[cat]
            total = stats["done"] + stats["open"] + stats["cancelled"]
            detail = f"{stats['open']}未完成/{stats['done']}完成"
            if stats["cancelled"]:
                detail += f"/{stats['cancelled']}取消"
            message += f"📂 **{cat}** ({detail}/{total}总)\n"

        message += f"\n💡 用 /pendo todo list <分类名> 查看详情"
        message += f"\n💡 用 /pendo todo list today 查看今日待办"

        return {"status": "success", "message": message}

    async def list_tasks(
        self, user_id: str, filter_str: str, context: PendoContext
    ) -> CommandMessage:
        """列出待办

        格式：
        - /pendo todo list -> 列出所有分类
        - /pendo todo list today -> 列出今天的待办
        - /pendo todo list cat [open/done/cancelled] -> 列出指定分类
        - /pendo todo list done -> 列出所有分类下已完成的待办
        - /pendo todo list open -> 列出所有分类下未完成的待办
        - /pendo todo list cancelled -> 列出所有分类下已取消的待办
        - /pendo todo list cat all -> 显示该分类全部待办
        - /pendo todo list cat page:2 -> 显示该分类第2页
        """
        filter_str = (filter_str or "").strip()

        if not filter_str:
            return await self.list_all_categories(user_id, context)

        parts = filter_str.split()
        category = parts[0]

        # 检查是否是全局状态筛选
        global_status = None
        if category.lower() in ["done", "已完成"]:
            global_status = TaskStatus.DONE.value
            return await self.list_all_tasks_by_status(user_id, global_status, context, filter_str)
        elif category.lower() in ["cancelled", "已取消"]:
            global_status = TaskStatus.CANCELLED.value
            return await self.list_all_tasks_by_status(user_id, global_status, context, filter_str)
        elif category.lower() in ["open", "undone", "未完成", "todo"]:
            global_status = TaskStatus.OPEN.value
            return await self.list_all_tasks_by_status(user_id, global_status, context, filter_str)

        shortcut = category.lower()
        today_key = self._user_local_now(user_id).strftime("%Y-%m-%d")
        now_iso = self._user_local_now(user_id).isoformat()
        all_for_python_filter = False
        if category.lower() == "today":
            all_for_python_filter = True
        elif shortcut in {"overdue", "upcoming", "inbox"}:
            all_for_python_filter = True

        # 解析参数
        status_filter = None
        priority_filter = None
        show_all = False
        page_num = 1

        for i, part in enumerate(parts[1:], 1):
            part_lower = part.lower()
            if part_lower in ["done", "已完成"]:
                status_filter = TaskStatus.DONE.value
            elif part_lower in ["cancelled", "已取消"]:
                status_filter = TaskStatus.CANCELLED.value
            elif part_lower in ["open", "undone", "未完成", "todo"]:
                status_filter = TaskStatus.OPEN.value
            elif part_lower == "all":
                show_all = True
            elif part.startswith("page:"):
                try:
                    page_num = int(part.split(":")[1])
                except (IndexError, ValueError):
                    pass
            elif part.startswith("p:"):
                try:
                    priority_filter = int(part[2:])
                except ValueError:
                    pass

        # 构建查询条件
        filters = {"type": ItemType.TASK.value}
        if not all_for_python_filter:
            filters["category"] = category

        if status_filter:
            filters["status"] = status_filter
        elif all_for_python_filter:
            filters["status"] = TaskStatus.OPEN.value

        # 查询（如果显示全部或分页，增加limit）
        query_limit = 1000 if all_for_python_filter or show_all or page_num > 1 else PendoConfig.DEFAULT_SEARCH_LIMIT
        tasks = cast(
            list[TaskItem], await run_sync(self.db.items.get_items, user_id, filters, query_limit)
        )

        if shortcut == "today":
            tasks = [t for t in tasks if _task_plan_key(t) == today_key or _task_deadline_key(t)[:10] == today_key]
            category = "今天"
        elif shortcut == "overdue":
            tasks = [
                t for t in tasks
                if (_task_deadline_key(t) and _task_deadline_key(t) < now_iso)
                or (_task_plan_key(t) and _task_plan_key(t) < today_key)
            ]
            category = "已滞后"
        elif shortcut == "upcoming":
            tasks = [
                t for t in tasks
                if (_task_plan_key(t) and _task_plan_key(t) > today_key)
                or (_task_deadline_key(t) and _task_deadline_key(t) > now_iso)
            ]
            category = "未来"
        elif shortcut == "inbox":
            tasks = [t for t in tasks if not _task_plan_key(t)]
            category = "收件箱"

        # 应用优先级过滤
        if priority_filter is not None:
            tasks = [t for t in tasks if (_enum_val(t.priority) or 3) == priority_filter]

        if not tasks:
            return {"status": "success", "message": f"📝 **{category}** 的待办\n\n暂无待办事项"}

        # 按优先级排序，再按创建时间排序
        tasks.sort(key=_task_sort_key)

        # 分页处理
        page_size = PendoConfig.LIST_PAGE_SIZE
        display_tasks, page_info, has_more = paginate(tasks, page_num, page_size, show_all)

        # 格式化输出
        message = f"📝 **{category}** 的待办 (共{len(tasks)}项){page_info}\n\n"

        for idx, task in enumerate(display_tasks, 1):
            status_icon = ItemFormatter.format_status_icon(_enum_val(task.status))
            priority_icon = ItemFormatter.format_priority_icon(_enum_val(task.priority))
            title = task.title or "无标题"
            task_id = task.id or ""
            global_idx = (page_num - 1) * page_size + idx
            message += f"{global_idx}. {status_icon} {priority_icon} {title}\n"
            if _task_plan_key(task) or _task_deadline_key(task):
                message += f"   📅 {_task_plan_key(task) or '未安排'}"
                if _task_deadline_key(task):
                    message += f"  ⏰ {ItemFormatter.format_datetime(_task_deadline_key(task))}"
                message += "\n"
            message += f"   `{task_id}`\n\n"

        if has_more and not show_all:
            message += f"   ... (使用 'all' 显示全部或 'page:{page_num + 1}' 查看下一页)\n"

        message += f"💡 /pendo todo done <id> 完成 | /pendo todo undone <id> 重开"

        return {"status": "success", "message": message}

    async def list_all_tasks_by_status(
        self, user_id: str, status: str, context: PendoContext, filter_str: str = ""
    ) -> CommandMessage:
        """列出所有分类下指定状态的待办

        Args:
            user_id: 用户ID
            status: 任务状态 (open/done/cancelled)
            context: 上下文
            filter_str: 过滤字符串（支持 all/page:n）

        Returns:
            待办列表消息
        """
        # 解析参数
        show_all = False
        page_num = 1

        if filter_str:
            parts = filter_str.split()
            for part in parts:
                part_lower = part.lower()
                if part_lower == "all":
                    show_all = True
                elif part.startswith("page:"):
                    try:
                        page_num = int(part.split(":")[1])
                    except (IndexError, ValueError):
                        pass

        # 查询所有指定状态的待办
        query_limit = 1000 if show_all or page_num > 1 else PendoConfig.DEFAULT_SEARCH_LIMIT
        filters = {"type": ItemType.TASK.value, "status": status}
        tasks = cast(
            list[TaskItem], await run_sync(self.db.items.get_items, user_id, filters, query_limit)
        )

        if not tasks:
            status_text = self._status_text(status)
            return {
                "status": "success",
                "message": f"📝 所有分类的{status_text}待办\n\n暂无{status_text}待办事项",
            }

        # 按分类分组
        categories = {}
        for task in tasks:
            cat = _task_category_label(task)
            if cat not in categories:
                categories[cat] = []
            categories[cat].append(task)

        # 分页信息
        page_size = PendoConfig.LIST_PAGE_SIZE
        if show_all:
            page_info = " (全部显示)"
        elif page_num > 1:
            page_info = f" (第{page_num}页)"
        else:
            page_info = ""

        # 格式化输出
        status_text = self._status_text(status)
        total_count = sum(len(cats) for cats in categories.values())

        # 初始消息头
        message = f"📝 所有分类的{status_text}待办 (共{total_count}项){page_info}\n\n"

        # 确定显示范围（计算起始和结束位置）
        if show_all:
            start_idx = 0
            end_idx = total_count
        else:
            start_idx = (page_num - 1) * page_size
            end_idx = min(start_idx + page_size, total_count)

        processed_count = 0
        for cat in _sort_category_keys(categories):
            sorted_tasks = categories[cat]
            sorted_tasks.sort(key=_task_sort_key)

            cat_start_idx = processed_count
            cat_end_idx = cat_start_idx + len(sorted_tasks)
            processed_count = cat_end_idx

            if cat_end_idx <= start_idx:
                continue
            if cat_start_idx >= end_idx:
                break

            display_start = max(0, start_idx - cat_start_idx)
            display_end = min(len(sorted_tasks), end_idx - cat_start_idx)
            cat_tasks = sorted_tasks[display_start:display_end]
            cat_display_count = len(cat_tasks)

            if cat_display_count == 0:
                continue

            message += f"📂 **{cat}** ({len(categories[cat])}项)\n"

            for idx, task in enumerate(cat_tasks, 1):
                status_icon = ItemFormatter.format_status_icon(_enum_val(task.status))
                priority_icon = ItemFormatter.format_priority_icon(_enum_val(task.priority))
                title = task.title or "无标题"
                task_id = task.id or ""

                message += f"  {idx}. {status_icon} {priority_icon} {title}\n"
                message += f"     `{task_id}`\n"

            message += "\n"

        # 分页提示
        if not show_all and end_idx < total_count:
            remaining = total_count - end_idx
            if page_num == 1:
                message += f"... 还有{remaining}项 (使用 'all' 显示全部或 'page:2' 查看第2页)\n"
            else:
                message += f"... (使用 'page:{page_num + 1}' 查看下一页)\n"

        message += f"💡 /pendo todo done <id> 完成 | /pendo todo cancel <id> 取消 | /pendo todo undone <id> 重开"

        return {"status": "success", "message": message}

    async def view_task(self, user_id: str, task_id: str, context: PendoContext) -> CommandMessage:
        """查看单个待办详情"""
        if not task_id:
            raise MissingRequiredFieldException("task_id")

        task_id = task_id.strip()
        task, wrong_type = await self._db_get_typed_item_or_message(
            task_id, user_id, ItemType.TASK.value, "待办"
        )
        if wrong_type:
            return wrong_type
        task = cast(TaskItem, task)
        status_value = _enum_val(task.status)
        status_label = self._status_text(status_value)

        lines = [f"📝 **{task.title or '无标题'}**", ""]
        lines.append(f"{ItemFormatter.format_status_icon(status_value)} 状态: {status_label}")
        lines.append(
            f"{ItemFormatter.format_priority_icon(_enum_val(task.priority))} 优先级: {ItemFormatter.format_priority(_enum_val(task.priority) or 3)}"
        )
        lines.append(f"📂 分类: {_task_category_label(task)}")

        if task.plan_date:
            lines.append(f"📅 计划: {task.plan_date}")
        if task.deadline_at:
            lines.append(f"⏰ 截止: {ItemFormatter.format_datetime(task.deadline_at)}")
        if task.completed_at:
            lines.append(f"✅ 完成: {ItemFormatter.format_datetime(task.completed_at)}")
        if task.cancelled_at:
            lines.append(f"🚫 取消: {ItemFormatter.format_datetime(task.cancelled_at)}")
        if task.remind_times:
            lines.append(f"🔔 提醒: {len(task.remind_times)} 个")
        if task.tags:
            lines.append(f"🏷️ 标签: {ItemFormatter.format_tags(task.tags)}")

        lines.append("")
        if task.content:
            lines.append(task.content)
            lines.append("")

        lines.append(f"`{task_id}`")
        lines.append(f"💡 /pendo todo done {task_id} | /pendo todo cancel {task_id} | /pendo todo edit {task_id} <内容>")
        return {"status": "success", "message": "\n".join(lines)}

    async def mark_done(self, user_id: str, task_id: str, context: PendoContext) -> CommandMessage:
        """标记为完成"""
        if not task_id:
            raise MissingRequiredFieldException("task_id")

        task_id = task_id.strip()

        task, wrong_type = await self._db_get_typed_item_or_message(
            task_id, user_id, ItemType.TASK.value, "待办"
        )
        if wrong_type:
            return wrong_type
        task = cast(TaskItem, task)

        # 更新状态
        now = now_in_timezone(user_id, self.db)
        updates = {
            ItemFields.STATUS: TaskStatus.DONE.value,
            "completed_at": TimezoneHelper.format_for_storage(now),
            "cancelled_at": None,
            "type": ItemType.TASK.value,
        }
        await self._db_update_with_log(task_id, updates, user_id, action="complete_task")

        return {
            "status": "success",
            "message": f"✅ 已完成: {task.title or '无标题'}\n\n🎉 干得好！\n💡 用 /pendo todo list 查看待办",
        }

    async def mark_cancelled(
        self, user_id: str, task_id: str, context: PendoContext
    ) -> CommandMessage:
        """标记为已取消"""
        if not task_id:
            raise MissingRequiredFieldException("task_id")

        task_id = task_id.strip()

        task, wrong_type = await self._db_get_typed_item_or_message(
            task_id, user_id, ItemType.TASK.value, "待办"
        )
        if wrong_type:
            return wrong_type
        task = cast(TaskItem, task)

        now = now_in_timezone(user_id, self.db)
        updates = {
            ItemFields.STATUS: TaskStatus.CANCELLED.value,
            "completed_at": None,
            "cancelled_at": TimezoneHelper.format_for_storage(now),
            "type": ItemType.TASK.value,
        }
        await self._db_update_with_log(task_id, updates, user_id, action="cancel_task")

        return {
            "status": "success",
            "message": f"🚫 已取消: {task.title or '无标题'}\n\n💡 用 /pendo todo undone {task_id} 可重新打开",
        }

    async def mark_undone(
        self, user_id: str, task_id: str, context: PendoContext
    ) -> CommandMessage:
        """标记为未完成"""
        if not task_id:
            raise MissingRequiredFieldException("task_id")

        task_id = task_id.strip()

        # 获取任务并检查权限
        task, wrong_type = await self._db_get_typed_item_or_message(
            task_id, user_id, ItemType.TASK.value, "待办"
        )
        if wrong_type:
            return wrong_type
        task = cast(TaskItem, task)

        updates = {
            ItemFields.STATUS: TaskStatus.OPEN.value,
            "completed_at": None,
            "cancelled_at": None,
            "type": ItemType.TASK.value,
        }
        await self._db_update_with_log(task_id, updates, user_id, action="reopen_task")

        return {
            "status": "success",
            "message": f"↩️ 已重新打开: {task.title or '无标题'}\n\n💡 用 /pendo todo done {task_id} 完成 | /pendo todo cancel {task_id} 取消",
        }

    async def delete_task(self, user_id: str, args: str, context: PendoContext) -> CommandMessage:
        """删除待办

        格式：
        - /pendo todo delete <id> -> 删除单个待办
        - /pendo todo delete cat:xxx -> 删除整个分类下的待办
        """
        if not args:
            raise MissingRequiredFieldException("id或cat:xxx")

        args = args.strip()

        # 检查是否是cat:xxx格式
        cat_match = re.match(r"cat:(\S+)", args)
        if cat_match:
            category = cat_match.group(1)
            return await self._delete_category_tasks(user_id, category, context)

        # 单个ID删除
        task_id = args
        task, wrong_type = await self._db_get_typed_item_or_message(
            task_id, user_id, ItemType.TASK.value, "待办"
        )
        if wrong_type:
            return wrong_type
        task = cast(TaskItem, task)

        # 软删除
        await self._db_soft_delete_with_log(task_id, user_id, item_type=ItemType.TASK.value)

        return {
            "status": "success",
            "message": f"🗑️ 已删除: {task.title or '无标题'}\n\n💡 5分钟内可用 /pendo undo 撤销",
        }

    async def _delete_category_tasks(
        self, user_id: str, category: str, context: PendoContext
    ) -> CommandMessage:
        """删除整个分类下的待办（批量操作）"""
        # 查询该分类下所有待办
        filters = {"type": ItemType.TASK.value, "category": category}
        tasks = cast(
            list[TaskItem], await run_sync(self.db.items.get_items, user_id, filters, 1000)
        )

        if not tasks:
            return {"status": "success", "message": f"📂 分类 {category} 下没有待办"}

        task_ids = [task.id for task in tasks]
        deleted_count = await self._db_batch_soft_delete_with_log(
            task_ids,
            user_id,
            ItemType.TASK.value,
            "delete_task",
        )

        return {
            "status": "success",
            "message": f"🗑️ 已删除分类 {category} 下的 {deleted_count} 个待办\n\n💡 5分钟内可用 /pendo undo 撤销",
        }

    async def edit_task(self, user_id: str, args: str, context: PendoContext) -> CommandMessage:
        """编辑待办

        格式：/pendo todo edit <id> <新内容> [plan:YYYY-MM-DD] [deadline:YYYY-MM-DDTHH:MM] [cat:xxx] [p:1-5]
        """
        parts = args.split(maxsplit=1)
        if len(parts) < 2:
            return {"status": "error", "message": "❌ 用法: /pendo todo edit <id> <新内容>"}

        task_id = parts[0].strip()
        new_content = parts[1]

        # 获取任务
        task, wrong_type = await self._db_get_typed_item_or_message(
            task_id, user_id, ItemType.TASK.value, "待办"
        )
        if wrong_type:
            return wrong_type
        task = cast(TaskItem, task)

        # 解析新内容
        parsed = self._parse_task_text(new_content, user_id)

        # 构建更新
        updates = {"title": parsed["title"], "type": ItemType.TASK.value}

        # 只有明确指定才更新
        if "cat:" in new_content:
            updates["category"] = parsed["category"]
        if "plan:" in new_content or "date:" in new_content:
            updates["plan_date"] = parsed["plan_date"]
        if "deadline:" in new_content or "due:" in new_content:
            updates["deadline_at"] = parsed["deadline_at"]
        if "remind:" in new_content or "reminder:" in new_content:
            updates["remind_times"] = parsed["remind_times"]
        if "p:" in new_content:
            updates["priority"] = parsed["priority"]
        if parsed.get("tags"):
            updates["tags"] = parsed["tags"]

        updates = normalize_task_fields(updates, partial=True)
        updates["type"] = ItemType.TASK.value
        await self._db_update_with_log(task_id, updates, user_id, action="edit_task")

        return {
            "status": "success",
            "message": f"✅ 已更新待办: {parsed['title']}\n\n💡 /pendo todo done {task_id} 完成 | /pendo todo cancel {task_id} 取消 | /pendo undo 撤销编辑",
        }

    @staticmethod
    def _status_text(status: str) -> str:
        return {
            TaskStatus.DONE.value: "已完成",
            TaskStatus.CANCELLED.value: "已取消",
        }.get(status, "未完成")
