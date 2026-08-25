"""处理 Pendo 待办的规则解析、生命周期操作与列表展示。"""

from __future__ import annotations

import re
import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from typing import TYPE_CHECKING, Any, Final, TypedDict, cast

from core.plugin_base import run_sync

from ..config import PendoConfig
from ..core.exceptions import MissingRequiredFieldException
from ..core.router import TOP_LEVEL_REDIRECTS
from ..core.types import CommandMessage, PendoContext, SessionData
from ..models.item import ItemType, TaskItem, TaskStatus
from ..utils.db_ops import DbOpsMixin
from ..utils.error_handlers import handle_command_errors
from ..utils.formatters import UNSAFE_CONTROL_RE, ItemFormatter, paginate, single_line_text
from ..utils.identifiers import public_id
from ..utils.session_utils import safe_create_session, safe_end_session
from ..utils.time_utils import (
    TimezoneHelper,
    _parse_time_range_core,
    now_in_timezone,
    parse_date_optional,
)
from ..utils.validators import (
    default_task_plan_date,
    normalize_task_fields,
    validate_category,
    validate_tag,
    validate_title,
)

if TYPE_CHECKING:
    from ..services.db import Database


class _ExplicitTaskFields(TypedDict):
    """记录命令中实际出现的字段，避免编辑时覆盖未提及内容。"""

    plan_date: bool
    deadline_at: bool
    remind_times: bool
    category: bool
    priority: bool
    tags: bool


class _ParsedTask(TypedDict):
    """规则解析后的待办字段。"""

    title: str
    category: str
    plan_date: str | None
    deadline_at: str | None
    priority: int
    tags: list[str]
    remind_times: list[str]
    _explicit_fields: _ExplicitTaskFields


@dataclass(slots=True)
class _TaskListOptions:
    """一次待办列表查询的规范化选项。"""

    status: str | None = None
    priority: int | None = None
    show_all: bool = False
    page: int = 1
    category: str | None = None
    tag: str | None = None
    range_token: str | None = None
    shortcut: str | None = None

    @property
    def is_status_only(self) -> bool:
        """是否只按状态筛选（分页控制不改变展示分组）。"""
        return self.status is not None and not any(
            (self.category, self.tag, self.range_token, self.shortcut, self.priority)
        )


_WORD_APOSTROPHE_RE: Final = re.compile(r"(?<=\w)'(?=\w)")
_WORD_APOSTROPHE_SENTINEL: Final = "\ufdd0"
_TASK_TIME_KEYWORDS: Final = frozenset(
    {
        "today",
        "tomorrow",
        "week",
        "month",
        "year",
        "今天",
        "明天",
        "本周",
        "本月",
        "今年",
    }
)
_TASK_STATUS_ALIASES: Final = {
    "done": TaskStatus.DONE.value,
    "已完成": TaskStatus.DONE.value,
    "cancelled": TaskStatus.CANCELLED.value,
    "已取消": TaskStatus.CANCELLED.value,
    "open": TaskStatus.OPEN.value,
    "undone": TaskStatus.OPEN.value,
    "未完成": TaskStatus.OPEN.value,
    "todo": TaskStatus.OPEN.value,
}
_TASK_LIST_SHORTCUTS: Final = frozenset({"overdue", "upcoming", "inbox"})
_INLINE_FIELD_ALIASES: Final = {
    "plan": "plan_date",
    "date": "plan_date",
    "deadline": "deadline_at",
    "due": "deadline_at",
    "remind": "remind_times",
    "reminder": "remind_times",
    "cat": "category",
    "p": "priority",
}
_TITLE_QUOTE_PAIRS: Final = {
    '"': '"',
    "'": "'",
    "“": "”",
    "‘": "’",
    "「": "」",
    "『": "』",
}
_DEFAULT_INPUTS: Final = frozenset({"0", "默认", "跳过", "skip", "-"})
_NONE_INPUTS: Final = frozenset({"无", "不安排", "none", "null", "no", "不要", "清空"})
_INLINE_NONE_INPUTS: Final = frozenset({"none", "null", "clear", "unset", "无", "清空", "不安排"})
_TASK_COMMAND_MAX_CHARS: Final = 5_000


def _task_category_label(task: TaskItem) -> str:
    """返回适合展示的单行分类名。"""
    return single_line_text(task.category) or "未分类"


def _parse_date_text(value: str | None) -> datetime | None:
    """解析数据库日期，并把带时区的旧值归一为本地朴素时间。"""
    if not value:
        return None
    try:
        text = str(value)
        if len(text) == 10:
            return datetime.fromisoformat(f"{text}T00:00:00")
        parsed = datetime.fromisoformat(text)
        return parsed.replace(tzinfo=None) if parsed.tzinfo is not None else parsed
    except ValueError:
        return None


def _task_sort_key(task: TaskItem) -> tuple[int, str, str, str, str]:
    """生成优先级靠前且同值时仍稳定的待办排序键。"""
    plan = str(task.plan_date or "").strip() or "9999-12-31"
    deadline = str(task.deadline_at or "").strip() or "9999-12-31T99:99:99"
    return (task.priority or 3, plan, deadline, task.created_at or "", task.id or "")


def _task_status_value(task: TaskItem) -> str:
    """兼容模型枚举与遗留测试/数据中的纯字符串状态。"""
    status = task.status
    return status.value if isinstance(status, TaskStatus) else str(status)


def _validate_task_title(value: str) -> str:
    """校验待办标题，并拒绝公共校验器会静默截断的超长输入。"""
    if UNSAFE_CONTROL_RE.search(value):
        raise ValueError("待办标题包含不允许的控制字符")
    title = " ".join(value.split())
    if len(title) > 200:
        raise ValueError("待办标题不能超过 200 字")
    return validate_title(title)


def _validate_task_category(value: str) -> str:
    """校验待办分类，并拒绝静默截断。"""
    if UNSAFE_CONTROL_RE.search(value):
        raise ValueError("分类名包含不允许的控制字符")
    category = " ".join(value.split())
    if len(category) > 50:
        raise ValueError("分类名不能超过 50 字")
    return validate_category(category)


def _validate_task_tag(value: str) -> str:
    """校验待办标签，并拒绝静默截断。"""
    if UNSAFE_CONTROL_RE.search(value):
        raise ValueError("标签名包含不允许的控制字符")
    tag = value.strip().lstrip("#")
    if len(tag) > 20:
        raise ValueError("标签名不能超过 20 字")
    return validate_tag(tag)


def _parse_task_page(value: str) -> int:
    """解析从 1 开始的待办页码。"""
    try:
        page = int(value)
    except ValueError as exc:
        raise ValueError(f"无效页码: page:{value}") from exc
    if page < 1:
        raise ValueError(f"无效页码: page:{value}")
    return page


def _parse_task_priority(value: str) -> int:
    """解析 1 至 5 的待办优先级。"""
    if not re.fullmatch(r"[1-5]", value):
        raise ValueError("优先级必须在1-5之间")
    return int(value)


def _looks_like_task_time_range(value: str) -> bool:
    """判断列表参数是否像日期范围或相对时间快捷词。"""
    text = (value or "").strip().lower()
    return (
        text in _TASK_TIME_KEYWORDS
        or bool(re.fullmatch(r"last\d+d", text))
        or bool(re.fullmatch(r"\d{4}(?:-\d{2})?(?:-\d{2})?", text))
        or ".." in text
    )


def _find_inline_metadata_boundary(text: str) -> int | None:
    """查找引号外第一个待办元数据 token 的起点。"""
    closing_quote: str | None = None
    escaped = False
    for index, char in enumerate(text):
        if escaped:
            escaped = False
            continue
        if char == "\\" and closing_quote in {'"', "'"}:
            escaped = True
            continue
        if closing_quote is not None:
            if char == closing_quote:
                closing_quote = None
            continue
        if (
            char == "'"
            and index > 0
            and index + 1 < len(text)
            and text[index - 1].isalnum()
            and text[index + 1].isalnum()
        ):
            continue
        if char in _TITLE_QUOTE_PAIRS:
            closing_quote = _TITLE_QUOTE_PAIRS[char]
            continue
        if not char.isspace():
            continue
        remainder = text[index:].lstrip()
        candidate = remainder.split(maxsplit=1)[0]
        key, separator, _value = candidate.partition(":")
        if candidate.startswith("#") or (separator and key.casefold() in _INLINE_FIELD_ALIASES):
            return index
    return None


def _task_matches_range(task: TaskItem, start: datetime, end: datetime) -> bool:
    """计划日期或截止时间落入范围时视为命中。"""
    plan = _parse_date_text(task.plan_date)
    deadline = _parse_date_text(task.deadline_at)
    return bool(
        (plan and start.date() <= plan.date() <= end.date())
        or (deadline and start <= deadline <= end)
    )


def _task_is_overdue(task: TaskItem, now: datetime) -> bool:
    """兼容仅有计划日期或仅有截止时间的待办。"""
    plan = _parse_date_text(task.plan_date)
    deadline = _parse_date_text(task.deadline_at)
    return bool((deadline and deadline < now) or (plan and plan.date() < now.date()))


def _task_is_upcoming(task: TaskItem, now: datetime) -> bool:
    """计划日期或截止时间在未来时视为未来待办。"""
    plan = _parse_date_text(task.plan_date)
    deadline = _parse_date_text(task.deadline_at)
    return bool((plan and plan.date() > now.date()) or (deadline and deadline > now))


class TaskHandler(DbOpsMixin):
    """以确定性规则管理待办，不依赖 AI 解析。"""

    def __init__(self, db: Database):
        """保存待办读写所需的数据库入口。"""
        self.db = db

    def _user_local_now(self, user_id: str) -> datetime:
        """读取用户墙钟，并统一成内部比较使用的朴素时间。"""
        current = now_in_timezone(user_id, self.db)
        return current.replace(tzinfo=None) if current.tzinfo else current

    @handle_command_errors
    async def handle(
        self, user_id: str, args: str, context: PendoContext, group_id: int | None = None
    ) -> CommandMessage:
        """处理待办相关命令

        命令格式：
        - /pendo todo add
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
            return await self.list_all_categories(user_id)

        command = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""

        if command == "add":
            if rest.strip():
                return await self.add_task(user_id, rest, context, group_id)
            return await self.start_add_session(user_id, context, group_id)

        handlers: dict[
            str,
            Callable[[str, str, PendoContext], Awaitable[CommandMessage]],
        ] = {
            "list": self.list_tasks,
            "view": self.view_task,
            "done": self.mark_done,
            "cancel": self.mark_cancelled,
            "undone": self.mark_undone,
            "delete": self.delete_task,
            "edit": self.edit_task,
        }
        if handler := handlers.get(command):
            return await handler(user_id, rest, context)

        if command in TOP_LEVEL_REDIRECTS:
            return {"status": "error", "message": f"❌ 正确用法:\n\n{TOP_LEVEL_REDIRECTS[command]}"}

        if self._should_treat_as_list_shortcut(command, rest):
            return await self.list_tasks(user_id, args, context)

        return {
            "status": "error",
            "message": (
                f"❌ 未知待办命令: {command}\n\n"
                "可用命令:\n"
                "• /pendo todo add\n"
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

    @classmethod
    def _should_treat_as_list_shortcut(cls, command: str, rest: str) -> bool:
        """保留分类简写，同时拒绝明显拼错的多词子命令。"""
        command = (command or "").strip().lower()
        rest = (rest or "").strip()
        if not command:
            return False
        if (
            command in _TASK_STATUS_ALIASES
            or command in _TASK_LIST_SHORTCUTS
            or _looks_like_task_time_range(command)
        ):
            return True
        if not rest:
            # 单个未知词按分类简写处理，例如 `/pendo todo 工作`。
            return True
        return all(cls._classify_task_list_token(token)[0] != "category" for token in rest.split())

    async def start_add_session(
        self, user_id: str, context: PendoContext, group_id: int | None = None
    ) -> CommandMessage:
        """开始交互式添加待办会话。"""
        await safe_create_session(
            context,
            initial_data={
                "type": PendoConfig.SESSION_TYPE_TASK_ADD,
                "owner_id": user_id,
                "group_id": group_id,
                "step": "title",
                "data": {},
            },
            timeout=PendoConfig.SESSION_TIMEOUT_SECONDS,
        )

        return {
            "status": "success",
            "message": (
                "✅ 开始添加待办，请先输入待办内容：\n\n"
                "例如：写项目周报\n"
                "下一步只需要填写计划日期，填写后会直接创建待办；输入 0 使用默认计划日期。\n\n"
                "💡 截止、提醒、分类、优先级和标签请用一条命令或 edit 设置：\n"
                "/pendo todo add 写周报 cat:工作 p:2 plan:2026-05-01\n"
                '💡 输入"退出"可取消'
            ),
        }

    async def handle_session_step(
        self, user_id: str, text: str, session: SessionData, context: PendoContext
    ) -> CommandMessage:
        """处理交互式添加待办会话的每一步。"""
        step = session.get("step", "title")
        raw_data = session.get("data", {})
        if not isinstance(raw_data, dict):
            return {"status": "error", "message": "❌ 待办会话数据异常，请重新开始添加"}
        data: dict[str, Any] = raw_data
        raw_group_id = session.get("group_id")
        group_id = (
            raw_group_id
            if isinstance(raw_group_id, int) and not isinstance(raw_group_id, bool)
            else None
        )
        value = str(text or "").strip()

        if step == "title":
            return await self._step_task_title(user_id, value, data, session)
        if step == "plan_date":
            return await self._step_task_plan_date(user_id, value, data, context, group_id)

        return {"status": "error", "message": "❌ 会话状态异常"}

    async def add_task(
        self, user_id: str, text: str, context: PendoContext, group_id: int | None = None
    ) -> CommandMessage:
        """添加待办

        格式：
        - /pendo todo add 事件  -> 计划到当天（晚上8点后自动计划到第二天）
        - /pendo todo add 事件 plan:2026-05-01 deadline:2026-05-01T18:00 cat:工作 p:1
        """
        if not text or not text.strip():
            return {
                "status": "error",
                "message": "❌ 请提供待办内容\n\n用法: /pendo todo add <内容> [plan:YYYY-MM-DD] [deadline:YYYY-MM-DDTHH:MM] [cat:xxx] [p:1-5]",
            }

        try:
            local_now = self._user_local_now(user_id)
            parsed = self._parse_task_text(text, user_id, default_now=local_now)
            return await self._create_task_from_parsed(user_id, parsed, group_id, local_now)
        except ValueError as exc:
            return {"status": "error", "message": f"❌ {exc}"}

    async def _create_task_from_parsed(
        self,
        user_id: str,
        parsed: _ParsedTask,
        group_id: int | None,
        local_now: datetime,
    ) -> CommandMessage:
        """规范化并持久化待办，再生成与实际存储值一致的回执。"""
        task_payload = normalize_task_fields(
            {
                "owner_id": user_id,
                "title": parsed["title"],
                "content": "",
                "category": parsed["category"],
                "plan_date": parsed["plan_date"],
                "deadline_at": parsed["deadline_at"],
                "priority": parsed["priority"],
                "status": TaskStatus.OPEN.value,
                "tags": parsed["tags"],
                "remind_times": parsed["remind_times"],
                "context": {"group_id": group_id} if group_id else {},
                "created_at": local_now.isoformat(),
                "updated_at": local_now.isoformat(),
            }
        )
        task_item = TaskItem(**task_payload)

        item_id = await self._db_create_with_log(task_item, owner_id=user_id, action="create_task")

        explicit_fields = parsed["_explicit_fields"]
        category = task_item.category
        priority = task_item.priority
        tags = task_item.tags
        remind_times = task_item.remind_times
        plan_label = task_item.plan_date or "未安排"
        if local_now.hour >= 20 and task_item.plan_date == (local_now + timedelta(days=1)).strftime(
            "%Y-%m-%d"
        ):
            plan_label += " (明天)"

        lines = [
            "✅ 已添加待办",
            "",
            f"📝 {single_line_text(task_item.title)}",
            f"📅 计划: {plan_label}",
        ]
        if task_item.deadline_at:
            display_timezone = await run_sync(
                TimezoneHelper.get_user_timezone,
                user_id,
                self.db,
            )
            lines.append(
                f"⏰ 截止: {ItemFormatter.format_datetime(task_item.deadline_at, tz=display_timezone)}"
            )
        if remind_times:
            lines.append(f"🔔 提醒: {len(remind_times)} 个")
        if category != "未分类" or explicit_fields["category"]:
            lines.append(f"📂 分类: {single_line_text(category)}")
        if priority != 3 or explicit_fields["priority"]:
            lines.append(f"⚡ 优先级: {ItemFormatter.format_priority(priority)}")
        if tags:
            lines.append(f"🏷️ 标签: {ItemFormatter.format_tags(tags)}")
        display_id = public_id(item_id)
        lines.extend((f"`{display_id}`", "", f"💡 用 /pendo todo done {display_id} 完成"))
        return {"status": "success", "message": "\n".join(lines), "item_id": item_id}

    async def _step_task_title(
        self, user_id: str, text: str, data: dict[str, Any], session: SessionData
    ) -> CommandMessage:
        """校验轻量新增会话的标题并进入计划日期步骤。"""
        if not text or self._is_default_input(text):
            return {"status": "info", "message": "❌ 待办内容不能为空，请输入待办内容："}
        try:
            data["title"] = _validate_task_title(text)
        except ValueError as exc:
            return {"status": "info", "message": f"❌ {exc}，请重新输入待办内容："}

        session.set("data", data)
        session.set("step", "plan_date")
        default_plan = default_task_plan_date(self._user_local_now(user_id))
        return {
            "status": "success",
            "message": (
                "📅 计划日期？\n\n"
                f"0. {default_plan}（默认）\n"
                "也可以输入 today / 明天 / 2026-05-01 / 05-01。\n"
                "输入 无 或 不安排 可放入未安排。"
            ),
        }

    async def _step_task_plan_date(
        self,
        user_id: str,
        text: str,
        data: dict[str, Any],
        context: PendoContext,
        group_id: int | None,
    ) -> CommandMessage:
        """解析计划日期，创建待办并结束轻量新增会话。"""
        local_now = self._user_local_now(user_id)
        if self._is_default_input(text):
            plan_date = default_task_plan_date(local_now)
        elif self._is_none_input(text):
            plan_date = None
        else:
            plan_date = parse_date_optional(text, local_now)
            if not plan_date:
                return {
                    "status": "info",
                    "message": "❌ 无法解析计划日期，请输入 today / 明天 / 2026-05-01，或输入 0 使用默认值：",
                }

        parsed: _ParsedTask = {
            "title": str(data.get("title") or ""),
            "category": "未分类",
            "plan_date": plan_date,
            "deadline_at": None,
            "priority": 3,
            "tags": [],
            "remind_times": [],
            "_explicit_fields": {
                "plan_date": True,
                "deadline_at": False,
                "remind_times": False,
                "category": False,
                "priority": False,
                "tags": False,
            },
        }
        try:
            result = await self._create_task_from_parsed(user_id, parsed, group_id, local_now)
        except ValueError as exc:
            return {
                "status": "info",
                "message": f"❌ {exc}，请重新输入计划日期或输入 0 使用默认值：",
            }

        await safe_end_session(context)
        return result

    @staticmethod
    def _is_default_input(text: str) -> bool:
        """判断交互输入是否要求采用默认值。"""
        return str(text or "").strip().lower() in _DEFAULT_INPUTS

    @staticmethod
    def _is_none_input(text: str) -> bool:
        """判断交互输入是否要求显式留空。"""
        return str(text or "").strip().lower() in _NONE_INPUTS

    @staticmethod
    def _tokenize_task_text(text: str) -> list[str]:
        """按 shell 风格处理成对引号，同时限制异常大命令。"""
        if len(text) > _TASK_COMMAND_MAX_CHARS:
            raise ValueError(f"待办命令不能超过 {_TASK_COMMAND_MAX_CHARS} 字")
        if UNSAFE_CONTROL_RE.search(text):
            raise ValueError("待办参数包含不允许的控制字符")
        if _WORD_APOSTROPHE_SENTINEL in text:
            raise ValueError("待办参数包含不支持的字符")
        try:
            # shlex 会把单词中的自然撇号当作引号；先用哨兵保护，分词后再原样恢复。
            shell_text = _WORD_APOSTROPHE_RE.sub(_WORD_APOSTROPHE_SENTINEL, text)
            return [
                token.replace(_WORD_APOSTROPHE_SENTINEL, "'")
                for token in shlex.split(shell_text, comments=False, posix=True)
            ]
        except ValueError as exc:
            raise ValueError("待办参数中的引号未闭合") from exc

    @staticmethod
    def _parse_inline_task_token(token: str) -> tuple[str, str]:
        """把一个 token 识别为标题、标签或规范字段。"""
        if token.startswith("#"):
            return "tag", token[1:]
        key, separator, value = token.partition(":")
        field = _INLINE_FIELD_ALIASES.get(key.casefold()) if separator else None
        if field is None:
            return "title", token
        if not value.strip():
            raise ValueError(f"{key}: 后面需要参数值")
        return field, value.strip()

    @classmethod
    def _collect_inline_task_tokens(
        cls, tokens: list[str]
    ) -> tuple[list[str], dict[str, str], list[str]]:
        """一次遍历收集标题、字段和标签，并拒绝重复字段。"""
        title_tokens: list[str] = []
        raw_fields: dict[str, str] = {}
        tags: list[str] = []
        seen: set[str] = set()
        for token in tokens:
            field, value = cls._parse_inline_task_token(token)
            if field == "title":
                title_tokens.append(value)
                continue
            if field != "tag":
                if field in raw_fields:
                    raise ValueError(f"待办参数不能重复: {field}")
                raw_fields[field] = value
                continue
            tag = _validate_task_tag(value)
            key = tag.casefold()
            if key in seen:
                continue
            seen.add(key)
            tags.append(tag)
        return title_tokens, raw_fields, tags

    def _parse_task_schedule_fields(
        self,
        raw_fields: dict[str, str],
        user_id: str,
        *,
        apply_defaults: bool,
        default_now: datetime | None,
    ) -> tuple[str | None, str | None, list[str]]:
        """解析计划、截止和提醒字段，并支持显式清空。"""
        plan_raw = raw_fields.get("plan_date")
        if plan_raw is None:
            plan_date = (
                default_task_plan_date(default_now or self._user_local_now(user_id))
                if apply_defaults
                else None
            )
        else:
            plan_date = None if plan_raw.casefold() in _INLINE_NONE_INPUTS else plan_raw

        deadline_raw = raw_fields.get("deadline_at")
        deadline_at = (
            None
            if deadline_raw is None or deadline_raw.casefold() in _INLINE_NONE_INPUTS
            else deadline_raw
        )

        remind_raw = raw_fields.get("remind_times")
        if remind_raw is None or remind_raw.casefold() in _INLINE_NONE_INPUTS:
            remind_times: list[str] = []
        else:
            remind_times = [
                value.strip() for value in re.split(r"[,，;；]", remind_raw) if value.strip()
            ]
            if not remind_times:
                raise ValueError("remind: 后面需要至少一个提醒时间")
        return plan_date, deadline_at, remind_times

    def _parse_task_text(
        self,
        text: str,
        user_id: str,
        *,
        apply_defaults: bool = True,
        default_now: datetime | None = None,
    ) -> _ParsedTask:
        """一次分词解析标题和内联字段，不依赖 AI。

        支持格式：
        - 事件内容 plan:2026-05-01 deadline:2026-05-01T18:00 cat:工作 p:1
        - cat:工作 事件内容 p:1
        """
        title_tokens, raw_fields, tags = self._collect_inline_task_tokens(
            self._tokenize_task_text(text)
        )
        title_text = " ".join(title_tokens).strip()
        title = _validate_task_title(title_text) if title_text else ""
        category = (
            _validate_task_category(raw_fields["category"])
            if "category" in raw_fields
            else "未分类"
        )

        priority_raw = raw_fields.get("priority")
        if priority_raw is not None and not re.fullmatch(r"[1-5]", priority_raw):
            raise ValueError("优先级必须在1-5之间")
        priority = int(priority_raw) if priority_raw is not None else 3
        plan_date, deadline_at, remind_times = self._parse_task_schedule_fields(
            raw_fields,
            user_id,
            apply_defaults=apply_defaults,
            default_now=default_now,
        )

        return {
            "title": title,
            "category": category,
            "plan_date": plan_date,
            "deadline_at": deadline_at,
            "priority": priority,
            "tags": tags,
            "remind_times": remind_times,
            "_explicit_fields": {
                "plan_date": "plan_date" in raw_fields,
                "deadline_at": "deadline_at" in raw_fields,
                "remind_times": "remind_times" in raw_fields,
                "category": "category" in raw_fields,
                "priority": "priority" in raw_fields,
                "tags": bool(tags),
            },
        }

    @classmethod
    def _extract_title_edit_directive(cls, text: str) -> tuple[str | None, str]:
        """拆出自然语言改名指令，并保留后续结构化编辑字段。"""
        match = re.match(
            r"\s*(?:(?:标题|名字|名称)\s*(?:改为|改成|改到|设为|设置为|重命名为|:|：)"
            r"|(?:改名|重命名)\s*(?:为|成)?)\s*(.+?)\s*$",
            text,
        )
        if not match:
            return None, text
        raw_value = match.group(1).strip()
        cls._tokenize_task_text(raw_value)

        boundary = _find_inline_metadata_boundary(raw_value)
        if boundary is not None:
            title = raw_value[:boundary].strip(" ，,。；;")
            remaining = raw_value[boundary:].strip()
        else:
            title = raw_value.strip(" ，,。；;")
            remaining = ""
        if len(title) >= 2 and _TITLE_QUOTE_PAIRS.get(title[0]) == title[-1]:
            title = title[1:-1].strip()
        return (title or None), remaining

    async def list_all_categories(self, user_id: str) -> CommandMessage:
        """汇总当前用户各分类的待办状态。"""
        tasks = cast(
            list[TaskItem],
            await run_sync(self.db.get_all_items, user_id, {"type": ItemType.TASK.value}),
        )

        if not tasks:
            return {
                "status": "success",
                "message": "📝 **待办列表**\n\n暂无待办事项\n\n💡 用 /pendo todo add 交互式添加，或用 /pendo todo add <内容> 快捷添加",
            }

        categories: dict[str, dict[str, int]] = {}
        for task in tasks:
            category = _task_category_label(task)
            stats = categories.setdefault(category, {"done": 0, "open": 0, "cancelled": 0})
            status = _task_status_value(task)
            bucket = (
                status if status in {TaskStatus.DONE.value, TaskStatus.CANCELLED.value} else "open"
            )
            stats[bucket] += 1

        lines = ["📝 **待办分类列表**", ""]
        for cat in sorted(categories):
            stats = categories[cat]
            total = stats["done"] + stats["open"] + stats["cancelled"]
            detail = f"{stats['open']}未完成/{stats['done']}完成"
            if stats["cancelled"]:
                detail += f"/{stats['cancelled']}取消"
            lines.append(f"📂 **{cat}** ({detail}/{total}总)")

        lines.extend(
            (
                "",
                "💡 用 /pendo todo list <分类名> 查看详情",
                "💡 用 /pendo todo list today 查看今日待办",
            )
        )
        return {"status": "success", "message": "\n".join(lines)}

    @staticmethod
    def _classify_task_list_token(token: str) -> tuple[str, str]:
        """识别一个列表参数；未命中控制语法时按分类名处理。"""
        lower = token.casefold()
        if lower in _TASK_STATUS_ALIASES:
            return "status", _TASK_STATUS_ALIASES[lower]
        if lower == "all":
            return "all", "all"
        if token.startswith("#"):
            return "tag", token[1:]
        key, separator, value = token.partition(":")
        if separator and key.casefold() in {"page", "p", "cat"}:
            return {"page": "page", "p": "priority", "cat": "category"}[key.casefold()], value
        if lower in _TASK_LIST_SHORTCUTS:
            return "shortcut", lower
        if _looks_like_task_time_range(token):
            return "range", token
        return "category", token

    @staticmethod
    def _apply_task_list_token(
        options: _TaskListOptions,
        seen: set[str],
        kind: str,
        value: str,
    ) -> None:
        """把一个已分类参数写入查询选项，并拒绝重复或越界值。"""
        if kind in seen:
            raise ValueError(f"待办列表参数不能重复: {kind}")
        seen.add(kind)
        if kind == "status":
            options.status = value
        elif kind == "all":
            options.show_all = True
        elif kind == "page":
            options.page = _parse_task_page(value)
        elif kind == "priority":
            options.priority = _parse_task_priority(value)
        elif kind == "category":
            options.category = _validate_task_category(value)
        elif kind == "tag":
            options.tag = _validate_task_tag(value)
        elif kind == "range":
            options.range_token = value
        elif kind == "shortcut":
            options.shortcut = value

    @classmethod
    def _parse_task_list_options(cls, filter_str: str) -> _TaskListOptions:
        """单次分词并规范化列表参数。"""
        tokens = cls._tokenize_task_text(filter_str)
        options = _TaskListOptions()
        seen: set[str] = set()
        for token in tokens:
            kind, value = cls._classify_task_list_token(token)
            cls._apply_task_list_token(options, seen, kind, value)
        if options.show_all and "page" in seen:
            raise ValueError("all 与 page: 不能同时使用")
        if options.range_token and options.shortcut:
            raise ValueError("时间范围与 overdue/upcoming/inbox 不能同时使用")
        return options

    def _filter_task_list_by_time(
        self,
        tasks: list[TaskItem],
        options: _TaskListOptions,
        user_id: str,
    ) -> tuple[list[TaskItem], str | None]:
        """在用户本地时间中执行范围和快捷筛选。"""
        if not options.range_token and not options.shortcut:
            return tasks, None

        user_now = self._user_local_now(user_id)
        if options.range_token:
            start, end = _parse_time_range_core(options.range_token, user_now, strict=True)
            label = {"today": "今天", "tomorrow": "明天"}.get(
                options.range_token.casefold(), options.range_token
            )
            return [task for task in tasks if _task_matches_range(task, start, end)], label
        if options.shortcut == "overdue":
            return [task for task in tasks if _task_is_overdue(task, user_now)], "已滞后"
        if options.shortcut == "upcoming":
            return [task for task in tasks if _task_is_upcoming(task, user_now)], "未来"
        return [task for task in tasks if not task.plan_date], "收件箱"

    @classmethod
    def _task_list_label(cls, options: _TaskListOptions, time_label: str | None) -> str:
        """把生效的列表筛选条件组合为可读标题。"""
        labels: list[str] = []
        if options.category:
            labels.append(options.category)
        if options.tag:
            labels.append(f"#{options.tag}")
        if time_label:
            labels.append(time_label)
        if options.status:
            labels.append(cls._status_text(options.status))
        if options.priority is not None:
            labels.append(f"优先级 {options.priority}")
        return " · ".join(labels) or "全部"

    @staticmethod
    def _build_task_list_filters(options: _TaskListOptions) -> dict[str, Any]:
        """构造可直接交给数据库的待办筛选条件。"""
        filters: dict[str, Any] = {"type": ItemType.TASK.value}
        if options.category:
            filters["category"] = options.category
        if options.tag:
            filters["tags"] = options.tag
        if options.status:
            filters["status"] = options.status
        elif (options.range_token or options.shortcut) and not options.show_all:
            filters["status"] = TaskStatus.OPEN.value
        return filters

    async def list_tasks(
        self, user_id: str, filter_str: str, context: PendoContext
    ) -> CommandMessage:
        """按状态、分类、标签、日期、优先级与分页选项列出待办。"""
        filter_str = (filter_str or "").strip()
        if not filter_str:
            return await self.list_all_categories(user_id)

        try:
            options = self._parse_task_list_options(filter_str)
        except ValueError as exc:
            return {"status": "error", "message": f"❌ {exc}"}

        tasks = cast(
            list[TaskItem],
            await run_sync(
                self.db.get_all_items,
                user_id,
                self._build_task_list_filters(options),
            ),
        )
        try:
            tasks, time_label = self._filter_task_list_by_time(tasks, options, user_id)
        except ValueError as exc:
            return {"status": "error", "message": f"❌ {exc}"}
        if options.priority is not None:
            tasks = [task for task in tasks if task.priority == options.priority]

        label = self._task_list_label(options, time_label)
        if not tasks:
            return {"status": "success", "message": f"📝 **{label}** 的待办\n\n暂无待办事项"}

        page_size = PendoConfig.LIST_PAGE_SIZE
        if not options.show_all and (options.page - 1) * page_size >= len(tasks):
            return {"status": "error", "message": f"❌ 第 {options.page} 页超出范围"}

        display_timezone = await run_sync(
            TimezoneHelper.get_user_timezone,
            user_id,
            self.db,
        )
        if options.is_status_only:
            return self._format_status_task_list(tasks, options, display_timezone)
        tasks.sort(key=_task_sort_key)
        return self._format_flat_task_list(tasks, options, label, display_timezone)

    @staticmethod
    def _task_list_entry_lines(
        task: TaskItem,
        index: int,
        *,
        display_timezone: tzinfo,
        indent: str = "",
        include_schedule: bool = True,
    ) -> list[str]:
        """格式化一条列表记录，统一图标、标题、时间和 ID。"""
        status_icon = ItemFormatter.format_status_icon(_task_status_value(task))
        priority_icon = ItemFormatter.format_priority_icon(task.priority)
        title = single_line_text(task.title) or "无标题"
        lines = [f"{indent}{index}. {status_icon} {priority_icon} {title}"]
        if include_schedule and (task.plan_date or task.deadline_at):
            schedule = f"📅 {single_line_text(task.plan_date) or '未安排'}"
            if task.deadline_at:
                deadline = single_line_text(
                    ItemFormatter.format_datetime(
                        task.deadline_at,
                        tz=display_timezone,
                    )
                )
                schedule += f"  ⏰ {deadline}"
            lines.append(f"{indent}   {schedule}")
        lines.append(f"{indent}   `{public_id(getattr(task, 'id', ''))}`")
        return lines

    def _format_flat_task_list(
        self,
        tasks: list[TaskItem],
        options: _TaskListOptions,
        label: str,
        display_timezone: tzinfo,
    ) -> CommandMessage:
        """格式化普通列表，并显示精确总数和下一页提示。"""
        page_size = PendoConfig.LIST_PAGE_SIZE
        display, page_info, has_more = paginate(tasks, options.page, page_size, options.show_all)
        lines = [f"📝 **{label}** 的待办 (共{len(tasks)}项){page_info}", ""]
        start = 0 if options.show_all else (options.page - 1) * page_size
        for index, task in enumerate(display, start + 1):
            lines.extend(
                self._task_list_entry_lines(
                    task,
                    index,
                    display_timezone=display_timezone,
                )
            )
            lines.append("")
        if has_more:
            lines.append(f"... 使用 'all' 显示全部或 'page:{options.page + 1}' 查看下一页")
        lines.append("💡 /pendo todo done <id> 完成 | /pendo todo undone <id> 重开")
        return {"status": "success", "message": "\n".join(lines)}

    def _format_status_task_list(
        self,
        tasks: list[TaskItem],
        options: _TaskListOptions,
        display_timezone: tzinfo,
    ) -> CommandMessage:
        """按分类展示单一状态结果，并让分页跨分类稳定生效。"""
        categories: dict[str, list[TaskItem]] = {}
        for task in tasks:
            categories.setdefault(_task_category_label(task), []).append(task)

        ordered_rows: list[tuple[str, TaskItem]] = []
        for category in sorted(categories):
            categories[category].sort(key=_task_sort_key)
            ordered_rows.extend((category, task) for task in categories[category])

        page_size = PendoConfig.LIST_PAGE_SIZE
        display, page_info, has_more = paginate(
            ordered_rows, options.page, page_size, options.show_all
        )
        status_text = self._status_text(options.status or TaskStatus.OPEN.value)
        lines = [f"📝 所有分类的{status_text}待办 (共{len(tasks)}项){page_info}", ""]
        current_category: str | None = None
        start = 0 if options.show_all else (options.page - 1) * page_size
        for index, (category, task) in enumerate(display, start + 1):
            if category != current_category:
                if current_category is not None:
                    lines.append("")
                lines.append(f"📂 **{category}** ({len(categories[category])}项)")
                current_category = category
            lines.extend(
                self._task_list_entry_lines(
                    task,
                    index,
                    display_timezone=display_timezone,
                    indent="  ",
                    include_schedule=False,
                )
            )

        if has_more:
            remaining = len(tasks) - (start + len(display))
            lines.extend(
                (
                    "",
                    f"... 还有 {remaining} 项，可用 'all' 或 'page:{options.page + 1}' 继续查看",
                )
            )
        lines.append(
            "💡 /pendo todo done <id> 完成 | /pendo todo cancel <id> 取消 | /pendo todo undone <id> 重开"
        )
        return {"status": "success", "message": "\n".join(lines)}

    @classmethod
    def _format_task_detail(
        cls,
        task: TaskItem,
        task_id: str,
        display_timezone: tzinfo,
    ) -> str:
        """格式化待办详情，标题保持单行，正文保留原有段落。"""
        status = _task_status_value(task)
        lines = [f"📝 **{single_line_text(task.title) or '无标题'}**", ""]
        lines.append(f"{ItemFormatter.format_status_icon(status)} 状态: {cls._status_text(status)}")
        lines.append(
            f"{ItemFormatter.format_priority_icon(task.priority)} "
            f"优先级: {ItemFormatter.format_priority(task.priority or 3)}"
        )
        lines.append(f"📂 分类: {_task_category_label(task)}")
        if task.plan_date:
            lines.append(f"📅 计划: {single_line_text(task.plan_date)}")
        for value, prefix in (
            (task.deadline_at, "⏰ 截止"),
            (task.completed_at, "✅ 完成"),
            (task.cancelled_at, "🚫 取消"),
        ):
            if value:
                formatted = single_line_text(
                    ItemFormatter.format_datetime(value, tz=display_timezone)
                )
                lines.append(f"{prefix}: {formatted}")
        if task.remind_times:
            lines.append(f"🔔 提醒: {len(task.remind_times)} 个")
        if task.tags:
            lines.append(f"🏷️ 标签: {ItemFormatter.format_tags(task.tags)}")
        lines.append("")
        if task.content:
            lines.extend((task.content, ""))
        display_id = public_id(getattr(task, "id", ""))
        lines.extend(
            (
                f"`{display_id}`",
                f"💡 /pendo todo done {display_id} | /pendo todo cancel {display_id} | "
                f"/pendo todo edit {display_id} <内容>",
            )
        )
        return "\n".join(lines)

    async def view_task(self, user_id: str, task_id: str, context: PendoContext) -> CommandMessage:
        """查看一个待办的完整详情。"""
        task_id = task_id.strip()
        if not task_id:
            raise MissingRequiredFieldException("task_id")
        if error := self._single_token_error(
            task_id, "❌ 待办详情只接受一个ID\n例如: /pendo todo view abc12345"
        ):
            return error
        task, wrong_type = await self._db_get_typed_item_or_message(
            task_id, user_id, ItemType.TASK.value, "待办"
        )
        if wrong_type:
            return wrong_type
        task = cast(TaskItem, task)
        display_timezone = await run_sync(
            TimezoneHelper.get_user_timezone,
            user_id,
            self.db,
        )
        return {
            "status": "success",
            "message": self._format_task_detail(task, task_id, display_timezone),
        }

    async def _transition_task_status(
        self,
        user_id: str,
        task_id: str,
        target_status: str,
        action: str,
    ) -> tuple[str, TaskItem | None, CommandMessage | None]:
        """统一校验 ID、条目类型和状态幂等性，再执行一次状态写入。"""
        task_id = task_id.strip()
        if not task_id:
            raise MissingRequiredFieldException("task_id")
        if error := self._single_token_error(
            task_id, "❌ 待办状态操作只接受一个ID\n例如: /pendo todo done abc12345"
        ):
            return task_id, None, error
        task, wrong_type = await self._db_get_typed_item_or_message(
            task_id, user_id, ItemType.TASK.value, "待办"
        )
        if wrong_type:
            return task_id, None, wrong_type
        task = cast(TaskItem, task)
        if _task_status_value(task) == target_status:
            # “已是已完成状态”会叠加两个完成体标记；这里使用状态名本身。
            status_text = self._status_text(target_status).removeprefix("已")
            title = single_line_text(task.title) or "无标题"
            return (
                task_id,
                task,
                {
                    "status": "success",
                    "message": f"ℹ️ 待办已是{status_text}状态: {title}",
                },
            )

        updates: dict[str, Any] = {
            "status": target_status,
            "completed_at": None,
            "cancelled_at": None,
            "type": ItemType.TASK.value,
        }
        if target_status in {TaskStatus.DONE.value, TaskStatus.CANCELLED.value}:
            timestamp = TimezoneHelper.format_for_storage(now_in_timezone(user_id, self.db))
            timestamp_field = (
                "completed_at" if target_status == TaskStatus.DONE.value else "cancelled_at"
            )
            updates[timestamp_field] = timestamp
        await self._db_update_with_log(
            task_id,
            updates,
            user_id,
            action=action,
            expected_version=task.version,
        )
        return task_id, task, None

    async def mark_done(self, user_id: str, task_id: str, context: PendoContext) -> CommandMessage:
        """把待办标记为完成；重复执行不产生新审计记录。"""
        _, task, result = await self._transition_task_status(
            user_id, task_id, TaskStatus.DONE.value, "complete_task"
        )
        if result:
            return result
        assert task is not None
        return {
            "status": "success",
            "message": f"✅ 已完成: {single_line_text(task.title) or '无标题'}\n\n🎉 干得好！\n💡 用 /pendo todo list 查看待办",
        }

    async def mark_cancelled(
        self, user_id: str, task_id: str, context: PendoContext
    ) -> CommandMessage:
        """把待办标记为取消；重复执行不产生新审计记录。"""
        task_id, task, result = await self._transition_task_status(
            user_id, task_id, TaskStatus.CANCELLED.value, "cancel_task"
        )
        if result:
            return result
        assert task is not None
        return {
            "status": "success",
            "message": f"🚫 已取消: {single_line_text(task.title) or '无标题'}\n\n💡 用 /pendo todo undone {public_id(task.id)} 可重新打开",
        }

    async def mark_undone(
        self, user_id: str, task_id: str, context: PendoContext
    ) -> CommandMessage:
        """重新打开待办；重复执行不产生新审计记录。"""
        task_id, task, result = await self._transition_task_status(
            user_id, task_id, TaskStatus.OPEN.value, "reopen_task"
        )
        if result:
            return result
        assert task is not None
        return {
            "status": "success",
            "message": (
                f"↩️ 已重新打开: {single_line_text(task.title) or '无标题'}\n\n"
                f"💡 用 /pendo todo done {public_id(task.id)} 完成 | "
                f"/pendo todo cancel {public_id(task.id)} 取消"
            ),
        }

    async def delete_task(self, user_id: str, args: str, context: PendoContext) -> CommandMessage:
        """软删除单个待办，或删除 ``cat:分类`` 下的全部待办。"""
        if not args or not args.strip():
            raise MissingRequiredFieldException("id或cat:xxx")

        try:
            tokens = self._tokenize_task_text(args)
        except ValueError as exc:
            return {"status": "error", "message": f"❌ {exc}"}
        if len(tokens) != 1:
            return {
                "status": "error",
                "message": "❌ 删除操作只接受一个待办ID或一个 cat:分类 参数",
            }

        target = tokens[0]
        if not target:
            raise MissingRequiredFieldException("id或cat:xxx")
        if target.casefold().startswith("cat:"):
            try:
                category = _validate_task_category(target.partition(":")[2])
            except ValueError as exc:
                return {"status": "error", "message": f"❌ {exc}"}
            return await self._delete_category_tasks(user_id, category)

        task_id = target
        task, wrong_type = await self._db_get_typed_item_or_message(
            task_id, user_id, ItemType.TASK.value, "待办"
        )
        if wrong_type:
            return wrong_type
        task = cast(TaskItem, task)

        await self._db_soft_delete_with_log(task_id, user_id, item_type=ItemType.TASK.value)

        return {
            "status": "success",
            "message": (
                f"🗑️ 已删除: {single_line_text(task.title) or '无标题'}\n\n{PendoConfig.UNDO_HINT}"
            ),
        }

    async def _delete_category_tasks(self, user_id: str, category: str) -> CommandMessage:
        """用共享批量审计操作软删除一个分类。"""
        try:
            category = _validate_task_category(category)
        except ValueError as exc:
            return {"status": "error", "message": f"❌ {exc}"}
        filters = {"type": ItemType.TASK.value, "category": category}
        task_ids = await run_sync(self.db.get_item_ids, user_id, filters)

        if not task_ids:
            return {"status": "success", "message": f"📂 分类 {category} 下没有待办"}
        deleted_count = await self._db_batch_soft_delete_with_log(
            task_ids,
            user_id,
            ItemType.TASK.value,
            "delete_task",
        )

        return {
            "status": "success",
            "message": (
                f"🗑️ 已删除分类 {category} 下的 {deleted_count} 个待办\n\n{PendoConfig.UNDO_HINT}"
            ),
        }

    @staticmethod
    def _build_task_edit_updates(
        parsed: _ParsedTask,
        title_directive: str | None,
    ) -> dict[str, Any]:
        """只收集命令中明确提及的字段。"""
        updates: dict[str, Any] = {}
        if title_directive is not None:
            updates["title"] = _validate_task_title(title_directive)
        elif parsed["title"]:
            updates["title"] = parsed["title"]

        explicit = parsed["_explicit_fields"]
        for field in (
            "category",
            "plan_date",
            "deadline_at",
            "remind_times",
            "priority",
            "tags",
        ):
            if explicit[field]:
                updates[field] = parsed[field]
        return updates

    async def edit_task(self, user_id: str, args: str, context: PendoContext) -> CommandMessage:
        """仅更新明确给出的标题、计划、截止、提醒、分类、优先级或标签。"""
        parts = args.split(maxsplit=1)
        if len(parts) < 2:
            return {"status": "error", "message": "❌ 用法: /pendo todo edit <id> <新内容>"}

        task_id = parts[0].strip()
        new_content = parts[1]

        task, wrong_type = await self._db_get_typed_item_or_message(
            task_id, user_id, ItemType.TASK.value, "待办"
        )
        if wrong_type:
            return wrong_type
        task = cast(TaskItem, task)

        try:
            title_directive, parse_content = self._extract_title_edit_directive(new_content)
            parsed = self._parse_task_text(parse_content, user_id, apply_defaults=False)
            updates = self._build_task_edit_updates(parsed, title_directive)
            # 字段归一化会校验日期等显式参数，也属于用户输入错误边界。
            # 留在这里可避免 ValueError 被外层兜底包装成内部错误码。
            updates = normalize_task_fields(updates, partial=True)
        except ValueError as exc:
            return {"status": "error", "message": f"❌ 参数无效: {exc}"}

        if not updates:
            return {"status": "warning", "message": "⚠️ 未识别到有效的待办修改内容"}

        if not any(getattr(task, field) != value for field, value in updates.items()):
            return {"status": "warning", "message": "⚠️ 待办内容没有变化"}
        updates["type"] = ItemType.TASK.value
        await self._db_update_with_log(
            task_id,
            updates,
            user_id,
            action="edit_task",
            expected_version=task.version,
        )

        display_title = single_line_text(updates.get("title") or task.title) or "无标题待办"
        return {
            "status": "success",
            "message": (
                f"✅ 已更新待办: {display_title}\n\n"
                f"💡 /pendo todo done {public_id(task.id)} 完成 | "
                f"/pendo todo cancel {public_id(task.id)} 取消 | /pendo undo 撤销编辑"
            ),
        }

    @staticmethod
    def _status_text(status: str) -> str:
        """把内部待办状态转换为中文显示文本。"""
        return {
            TaskStatus.DONE.value: "已完成",
            TaskStatus.CANCELLED.value: "已取消",
        }.get(status, "未完成")
