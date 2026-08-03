"""日记命令、模板会话、结构化元数据和情绪降级处理。"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final, Protocol, TypedDict, cast

from core.plugin_base import run_sync
from core.public_errors import public_error_message

from ..config import DIARY_TEMPLATES, MOOD_ANALYSIS_CONFIG, PendoConfig
from ..core.types import CommandMessage, PendoContext, SessionData
from ..models.item import DiaryItem, ItemType
from ..services.ai_parser import analyze_diary_mood_rule
from ..utils.db_ops import DbOpsMixin
from ..utils.error_handlers import handle_command_errors
from ..utils.formatters import TAG_TOKEN_RE, ItemFormatter
from ..utils.session_utils import safe_create_session, safe_end_session
from ..utils.time_utils import get_user_local_wall_time, parse_date_optional, parse_diary_range
from ..utils.validators import normalize_diary_fields, normalize_diary_mood

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ..services.db import Database


class DiaryTemplate(TypedDict):
    """运行时使用的日记模板结构。"""

    name: str
    prompts: list[str]


class DiaryMoodAnalyzer(Protocol):
    """日记处理器所需的最小 AI 情绪分析接口。"""

    async def analyze_diary_mood(
        self, text: str, user_id: str
    ) -> tuple[str | None, int | None]: ...


_DIARY_USAGE_MESSAGE: Final = (
    "❌ 请提供日记内容\n\n用法: /pendo diary add [日期] <内容> [weather:xxx] [location:xxx]"
)
_DIARY_METADATA_RE: Final = re.compile(
    r"(?P<key>weather|location|mood|score|tags|tag|favorite|fav):"
    r"(?P<value>\"[^\"]*\"|'[^']*'|\S+)",
    re.IGNORECASE,
)
_DIARY_LIST_FILTER_RE: Final = re.compile(
    r"(?<!\S)(?P<key>mood|cat):(?P<value>\"[^\"]*\"|'[^']*'|\S+)",
    re.IGNORECASE,
)
_TRUE_METADATA_VALUES: Final = frozenset({"1", "true", "yes", "y", "on", "是", "收藏"})
_FALSE_METADATA_VALUES: Final = frozenset({"0", "false", "no", "n", "off", "否", "不收藏"})
_MOOD_EMOJIS: Final[dict[str, str]] = cast(
    dict[str, str], MOOD_ANALYSIS_CONFIG.get("mood_emojis", {})
)


@dataclass(frozen=True, slots=True)
class DiaryListFilters:
    """日记列表命令中分离出的范围和元数据筛选。"""

    time_range: str
    mood: str | None
    category: str | None
    tag: str | None


class DiaryHandler(DbOpsMixin):
    """处理日记写入、查询、模板会话和可选 AI 情绪分析。"""

    def __init__(
        self,
        db: "Database",
        ai_parser: DiaryMoodAnalyzer | None = None,
    ) -> None:
        self.db = db
        self.ai_parser = ai_parser
        self.templates = cast(dict[str, DiaryTemplate], DIARY_TEMPLATES)

    async def _fetch_diaries(self, user_id: str, start_date: str, end_date: str) -> list[DiaryItem]:
        """读取并按日期、记录时间和 ID 稳定倒序排列日记。"""
        items = await run_sync(
            self.db.query_items_by_date_range,
            user_id,
            ItemType.DIARY.value,
            "diary_date",
            start_date,
            end_date,
        )
        diaries = [item for item in items if isinstance(item, DiaryItem)]
        diaries.sort(
            key=lambda diary: (
                diary.diary_date or "",
                diary.entry_time or diary.created_at or diary.updated_at or "",
                diary.updated_at or "",
                str(diary.id),
            ),
            reverse=True,
        )
        return diaries

    @staticmethod
    def _entry_time_for_diary_date(user_now: datetime, diary_date: str) -> str:
        return f"{diary_date}T{user_now.strftime('%H:%M:%S')}"

    async def _resolve_diary_id(
        self, user_id: str, query: str
    ) -> tuple[DiaryItem | None, CommandMessage | None]:
        """按 ID 读取日记，并返回准确的跨类型提示。"""
        query = (query or "").strip()
        item = await self._db_get_item(query, owner_id=user_id)
        if not item:
            return None, {"status": "error", "message": f"❌ 找不到日记 {query}"}
        if not isinstance(item, DiaryItem):
            return None, self._build_wrong_type_message(query, "日记", item)
        return item, None

    @handle_command_errors
    async def handle(
        self, user_id: str, args: str, context: PendoContext, group_id: int | None = None
    ) -> CommandMessage:
        """处理日记相关命令

        命令格式：
        - /pendo diary add [日期] <内容> -> 写日记
        - /pendo diary list [范围] -> 查看日记列表
        - /pendo diary view [日期|ID] -> 查看日记详情
        - /pendo diary template -> 查看所有模板
        - /pendo diary <模板ID> -> 使用模板写日记
        - /pendo diary delete <日期|ID> -> 删除日记
        """
        if not args or not args.strip():
            return {"status": "success", "message": self._show_help()}

        parts = args.split(maxsplit=1)
        command = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""

        if command == "add":
            return await self.add_diary(user_id, rest, context, group_id)
        if command == "list":
            return await self.list_diaries(user_id, rest, context)
        if command == "view":
            return await self.view_diary(user_id, rest, context)
        if command == "template":
            return await self._handle_template_command(user_id, rest, context, group_id)
        if command == "delete":
            return await self.delete_diary(user_id, rest, context)

        # 检查是否是模板ID
        if args.strip() in self.templates:
            return await self.start_template_session(user_id, args.strip(), context, group_id)

        # 未知命令，给出帮助
        return {"status": "error", "message": f"❌ 未知日记命令: {command}\n\n{self._show_help()}"}

    async def add_diary(
        self, user_id: str, args: str, context: PendoContext, group_id: int | None = None
    ) -> CommandMessage:
        """添加日记

        格式：
        - /pendo diary add <内容> -> 写今天的日记
        - /pendo diary add <日期> <内容> -> 写指定日期的日记
        - /pendo diary add weather:晴 location:北京 <内容> -> 带天气和地点
        """
        if not args:
            return {"status": "error", "message": _DIARY_USAGE_MESSAGE}

        # 尝试解析第一个参数是否是日期
        parts = args.split(maxsplit=1)
        first_arg = parts[0]
        rest = parts[1] if len(parts) > 1 else ""

        user_now = await get_user_local_wall_time(user_id, self.db)
        diary_date = parse_date_optional(first_arg, now=user_now)

        if diary_date:
            # 第一个参数是日期
            content_text = rest
        else:
            # 第一个参数不是日期，整个args都是内容
            diary_date = user_now.strftime("%Y-%m-%d")
            content_text = args

        if not content_text:
            return {"status": "error", "message": _DIARY_USAGE_MESSAGE}

        # 元数据属于显式命令参数，非法布尔值等必须明确拒绝，不能静默改成 False。
        try:
            parsed = self._parse_diary_text(content_text)
        except ValueError as exc:
            return {"status": "error", "message": f"❌ {exc}"}

        if not parsed["content"]:
            return {"status": "error", "message": _DIARY_USAGE_MESSAGE}

        parsed["entry_time"] = self._entry_time_for_diary_date(user_now, diary_date)
        return await self.create_diary(user_id, diary_date, parsed, context, group_id=group_id)

    async def create_diary(
        self,
        user_id: str,
        diary_date: str,
        parsed: dict[str, Any],
        context: PendoContext,
        template_id: str | None = None,
        group_id: int | None = None,
    ) -> CommandMessage:
        """创建日记"""
        user_now = await get_user_local_wall_time(user_id, self.db)
        content = str(parsed.get("content") or "")

        manual_mood = parsed.get("mood")
        manual_score = parsed.get("mood_score")
        if manual_mood or manual_score not in (None, ""):
            mood = manual_mood
            mood_score = manual_score
        else:
            mood, mood_score = await self._analyze_mood(content, user_id)

        entry_time = parsed.get("entry_time") or self._entry_time_for_diary_date(
            user_now, diary_date
        )
        item_data = {
            "owner_id": user_id,
            "title": str(parsed.get("title") or "").strip(),
            "content": content,
            "diary_date": diary_date,
            "entry_time": entry_time,
            "mood": mood,
            "mood_score": mood_score,
            "weather": parsed.get("weather") or "",
            "location": parsed.get("location") or "",
            "template_id": parsed.get("template_id") or template_id,
            "template_answers": parsed.get("template_answers") or [],
            "is_favorite": parsed.get("is_favorite", False),
            "tags": parsed.get("tags") or [],
            "category": "日记",
            "context": {"group_id": group_id} if group_id else {},
            "created_at": user_now.isoformat(timespec="seconds"),
            "updated_at": user_now.isoformat(timespec="seconds"),
        }
        try:
            item_data = normalize_diary_fields(item_data, partial=False)
        except ValueError as exc:
            return {"status": "error", "message": f"❌ {exc}"}

        entry_time = item_data.get("entry_time") or self._entry_time_for_diary_date(
            user_now, diary_date
        )
        entry_dt = datetime.fromisoformat(str(entry_time))
        entry_label = entry_dt.strftime("%H:%M")

        if not str(item_data.get("title") or "").strip():
            item_data["title"] = f"{diary_date} {entry_label} 日记"

        diary_item = DiaryItem(**item_data)

        # 保存到数据库
        item_id = await self._db_create_with_log(
            diary_item, owner_id=user_id, action="create_diary"
        )

        diary_item.id = item_id

        # 格式化返回消息
        message = f"✅ 已记录 {diary_date} {entry_label} 的日记\n\n"
        if diary_item.mood:
            emoji = _MOOD_EMOJIS.get(diary_item.mood, "📝")
            message += f"{emoji} 情绪: {diary_item.mood}"
            if diary_item.mood_score:
                message += f" ({diary_item.mood_score}/10)"
            message += "\n"
        if diary_item.weather:
            message += f"🌤️ 天气: {diary_item.weather}\n"
        if diary_item.location:
            message += f"📍 地点: {diary_item.location}\n"
        message += f"`{item_id}`\n\n"
        message += f"💡 用 /pendo diary view {diary_date} 查看当天所有记录"

        return {"status": "success", "message": message, "item_id": item_id}

    async def view_diary(
        self, user_id: str, date_str: str, context: PendoContext
    ) -> CommandMessage:
        """查看日记"""
        query = (date_str or "").strip()
        user_now = await get_user_local_wall_time(user_id, self.db)
        if not query:
            query = user_now.strftime("%Y-%m-%d")
        elif error := self._single_token_error(
            query, "❌ 日记详情只接受一个日期或ID\n例如: /pendo diary view 2026-05-10"
        ):
            return error

        query_date = parse_date_optional(query, now=user_now)
        if query_date:
            entries = await self._get_diaries_by_date(user_id, query_date)
            if not entries:
                return {
                    "status": "success",
                    "message": f"📔 您还没有写 {query_date} 的日记\n\n💡 用 /pendo diary add {query_date} <内容> 开始写",
                }
            message = f"📔 **{query_date} 的日记** ({len(entries)} 条)\n\n"
            for index, entry in enumerate(entries, 1):
                message += self._format_diary_entry_detail(entry, index=index)
                if index < len(entries):
                    message += "\n---\n\n"
            return {"status": "success", "message": message}

        diary, error = await self._resolve_diary_id(user_id, query)
        if error:
            return error

        if not diary:
            return {"status": "error", "message": f"❌ 找不到日记 {query}"}

        diary_date = diary.diary_date or query
        message = f"📔 **{diary_date} 的日记条目**\n\n"
        message += self._format_diary_entry_detail(diary)

        return {"status": "success", "message": message}

    async def list_diaries(
        self, user_id: str, range_str: str, context: PendoContext
    ) -> CommandMessage:
        """列出日记

        格式：
        - /pendo diary list -> 默认本月
        - /pendo diary list today/tomorrow/week/month/year
        - /pendo diary list YYYY-MM (如 2026-02)
        - /pendo diary list last7d
        - /pendo diary list start..end
        - /pendo diary list mood:happy -> 按情绪筛选
        """
        try:
            filters = self._parse_list_filters(range_str)
        except ValueError as exc:
            return {"status": "error", "message": f"❌ {exc}"}

        user_now = await get_user_local_wall_time(user_id, self.db)
        time_range = filters.time_range or user_now.strftime("%Y-%m")
        try:
            start_date, end_date = parse_diary_range(time_range, now=user_now, strict=True)
        except ValueError as exc:
            return {"status": "error", "message": f"❌ {exc}"}

        diaries = await self._fetch_diaries(user_id, start_date, end_date)
        diaries = [diary for diary in diaries if self._matches_list_filters(diary, filters)]

        filter_labels: list[str] = []
        if filters.mood:
            filter_labels.append(f"情绪:{filters.mood}")
        if filters.category:
            filter_labels.append(f"分类:{filters.category}")
        if filters.tag:
            filter_labels.append(f"#{filters.tag}")
        filter_suffix = f" [{', '.join(filter_labels)}]" if filter_labels else ""

        if not diaries:
            return {
                "status": "success",
                "message": f"📔 {time_range}{filter_suffix} 没有日记\n\n💡 用 /pendo diary add [日期] <内容> 开始写日记",
            }

        return self._format_diary_list(diaries, filter_suffix)

    @staticmethod
    def _parse_list_filters(raw_query: str) -> DiaryListFilters:
        """从列表命令提取 mood、category 和单个标签筛选。"""
        query = (raw_query or "").strip()
        metadata: dict[str, str] = {}
        for match in _DIARY_LIST_FILTER_RE.finditer(query):
            value = match.group("value").strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                value = value[1:-1].strip()
            if value:
                metadata[match.group("key").lower()] = value
        query = _DIARY_LIST_FILTER_RE.sub(" ", query)

        tag = None
        if tag_match := TAG_TOKEN_RE.search(query):
            tag = tag_match.group(1)
            query = f"{query[: tag_match.start()]} {query[tag_match.end() :]}"

        mood = normalize_diary_mood(metadata["mood"]) if metadata.get("mood") else None
        return DiaryListFilters(
            time_range=re.sub(r"\s+", " ", query).strip(),
            mood=mood,
            category=metadata.get("cat"),
            tag=tag,
        )

    @staticmethod
    def _matches_list_filters(diary: DiaryItem, filters: DiaryListFilters) -> bool:
        """判断一条日记是否满足全部元数据筛选。"""
        if filters.mood and (diary.mood or "").lower() != filters.mood:
            return False
        if filters.category and (diary.category or "") != filters.category:
            return False
        return not filters.tag or filters.tag in (diary.tags or [])

    def _format_diary_list(self, diaries: list[DiaryItem], filter_suffix: str) -> CommandMessage:
        """格式化已经排序和筛选的日记列表。"""
        lines = [f"📔 **日记列表**{filter_suffix} (共{len(diaries)}篇)", ""]
        for diary in diaries:
            content_preview = ItemFormatter.truncate_content(
                diary.content or "",
                PendoConfig.SEARCH_CONTENT_PREVIEW_LENGTH,
            )
            lines.extend(
                (
                    f"{_MOOD_EMOJIS.get(diary.mood or '', '📝')} "
                    f"**{diary.diary_date or ''} {self._format_entry_time(diary)}**",
                    f"  _{content_preview}_",
                    f"  `{diary.id}`",
                    "",
                )
            )
        lines.append("💡 用 /pendo diary view <日期或ID> 查看完整日记")
        return {"status": "success", "message": "\n".join(lines)}

    async def delete_diary(
        self, user_id: str, date_str: str, context: PendoContext
    ) -> CommandMessage:
        """按日期或 ID 删除日记"""
        query = (date_str or "").strip()
        if not query:
            return {"status": "error", "message": "❌ 请指定要删除的日记日期或ID"}

        user_now = await get_user_local_wall_time(user_id, self.db)
        query_date = parse_date_optional(query, now=user_now)
        if query_date:
            entries = await self._get_diaries_by_date(user_id, query_date)
            if not entries:
                return {"status": "error", "message": f"❌ 没有找到 {query_date} 的日记"}
            if len(entries) > 1:
                lines = [f"❌ {query_date} 有 {len(entries)} 条日记，请按 ID 删除："]
                for entry in entries:
                    preview = ItemFormatter.truncate_content(
                        entry.content or "", PendoConfig.SEARCH_CONTENT_PREVIEW_LENGTH
                    )
                    lines.append(f"• `{entry.id}` {self._format_entry_time(entry)} {preview}")
                return {"status": "error", "message": "\n".join(lines)}
            dated_diary = entries[0]
            await self._db_soft_delete_with_log(
                dated_diary.id, user_id, item_type=ItemType.DIARY.value
            )
            return {
                "status": "success",
                "message": f"🗑️ 已删除 {query_date} 的日记条目\n\n{PendoConfig.UNDO_HINT}",
            }

        diary, error = await self._resolve_diary_id(user_id, query)
        if error:
            return error

        if not diary:
            return {"status": "error", "message": f"❌ 没有找到日记 {query}"}

        # 软删除
        await self._db_soft_delete_with_log(diary.id, user_id, item_type=ItemType.DIARY.value)

        diary_date = diary.diary_date or query
        return {
            "status": "success",
            "message": f"🗑️ 已删除 {diary_date} 的日记条目\n\n{PendoConfig.UNDO_HINT}",
        }

    async def start_template_session(
        self, user_id: str, template_id: str, context: PendoContext, group_id: int | None = None
    ) -> CommandMessage:
        """启动日记模板多轮对话"""
        template = self.templates.get(template_id)
        if not template:
            return {"status": "error", "message": "❌ 模板不存在"}

        diary_date = (await get_user_local_wall_time(user_id, self.db)).strftime("%Y-%m-%d")
        prompts = template.get("prompts", [])

        if not prompts:
            return {"status": "error", "message": "❌ 该模板没有预设问题"}

        # 创建会话
        if await safe_create_session(
            context,
            initial_data={
                "type": PendoConfig.SESSION_TYPE_DIARY_TEMPLATE,
                "template_id": template_id,
                "diary_date": diary_date,
                "group_id": group_id,
                "prompts": prompts,
                "step": 0,
                "answers": [],
            },
            timeout=PendoConfig.SESSION_TIMEOUT_SECONDS,
        ):
            first_question = prompts[0]
            return {
                "status": "success",
                "message": f"📋 **开始写{template['name']}** ({diary_date})\n\n1/{len(prompts)}: {first_question}\n\n(发送 '退出' 可随时结束)",
            }
        return self._format_template_fallback(template_id, diary_date)

    def _format_template_fallback(self, template_id: str, diary_date: str) -> CommandMessage:
        """会话创建不可用时，直接显示可手动填写的模板。"""
        template = self.templates.get(template_id)
        if not template:
            return {"status": "error", "message": "❌ 模板不存在"}
        prompts = template.get("prompts", [])
        lines = [f"📋 **{template.get('name', template_id)}** ({diary_date})", ""]
        lines.extend(f"{idx}. {prompt}" for idx, prompt in enumerate(prompts, 1))
        lines.append("")
        lines.append("💡 用 /pendo diary add 写下答案，或稍后重试模板引导。")
        return {"status": "success", "message": "\n".join(lines)}

    async def handle_session_message(
        self, user_id: str, text: str, context: PendoContext, session: SessionData
    ) -> CommandMessage:
        """校验模板会话状态，记录当前答案并推进到下一题。"""
        raw_prompts = session.get("prompts", [])
        raw_answers = session.get("answers", [])
        raw_step = session.get("step", 0)
        prompts = (
            [prompt for prompt in raw_prompts if isinstance(prompt, str) and prompt.strip()]
            if isinstance(raw_prompts, list)
            else []
        )
        answers = (
            [answer for answer in raw_answers if isinstance(answer, str)]
            if isinstance(raw_answers, list)
            else []
        )
        step = raw_step if isinstance(raw_step, int) and not isinstance(raw_step, bool) else -1
        if not prompts or not 0 <= step < len(prompts) or len(answers) != step:
            await safe_end_session(context)
            return {"status": "error", "message": "❌ 模板会话状态损坏，请重新开始"}

        answers.append(text)
        session.set("answers", answers)
        step += 1
        session.set("step", step)

        if step == len(prompts):
            await safe_end_session(context)

            diary_date = session.get("diary_date")
            template_id = session.get("template_id")
            group_id = session.get("group_id")
            if (
                not isinstance(diary_date, str)
                or not isinstance(template_id, str)
                or template_id not in self.templates
            ):
                return {"status": "error", "message": "❌ 会话数据缺失，无法提交模板日记"}
            group_id_val = (
                group_id if isinstance(group_id, int) and not isinstance(group_id, bool) else None
            )

            return await self._submit_template_result(
                user_id,
                diary_date,
                template_id,
                prompts,
                answers,
                group_id_val,
                context,
            )

        next_question = prompts[step]
        return {"status": "question", "message": f"{step + 1}/{len(prompts)}: {next_question}"}

    async def _submit_template_result(
        self,
        user_id: str,
        diary_date: str,
        template_id: str,
        prompts: list[str],
        answers: list[str],
        group_id: int | None,
        context: PendoContext,
    ) -> CommandMessage:
        """提交模板结果"""
        template_answers = [
            {"prompt": q, "answer": a}
            for q, a in zip(prompts, answers, strict=True)
            if str(q or "").strip() or str(a or "").strip()
        ]
        content = "\n\n".join(f"**{row['prompt']}**\n{row['answer']}" for row in template_answers)

        return await self.create_diary(
            user_id,
            diary_date,
            {"content": content.strip(), "template_answers": template_answers},
            context,
            template_id,
            group_id,
        )

    async def _handle_template_command(
        self, user_id: str, args: str, context: PendoContext, group_id: int | None = None
    ) -> CommandMessage:
        """处理模板命令

        支持:
        - /pendo diary template          -> 列出所有模板
        - /pendo diary template 1        -> 按编号启动模板
        - /pendo diary template 三件好事 -> 按名称启动模板
        - /pendo diary template mood     -> 按ID启动模板
        """
        if not args or not args.strip():
            return self._show_template_list()

        arg = args.strip()
        usable = self._get_usable_templates()

        # 纯数字按展示编号匹配，其他输入再按名称或模板 ID 匹配。
        if arg.isdecimal():
            idx = int(arg)
            if 1 <= idx <= len(usable):
                template_id = usable[idx - 1][0]
                return await self.start_template_session(user_id, template_id, context, group_id)
            return {"status": "error", "message": f"❌ 无效编号，可选 1-{len(usable)}"}

        # 按名称匹配
        for tid, tpl in usable:
            if tpl["name"] == arg:
                return await self.start_template_session(user_id, tid, context, group_id)

        # 按ID匹配
        if arg in self.templates and self.templates[arg].get("prompts"):
            return await self.start_template_session(user_id, arg, context, group_id)

        return {
            "status": "error",
            "message": f"❌ 未找到模板: {arg}\n\n{self._show_template_list()['message']}",
        }

    def _get_usable_templates(self) -> list[tuple[str, DiaryTemplate]]:
        """获取至少包含一个问题的可用模板列表。"""
        return [(tid, tpl) for tid, tpl in self.templates.items() if tpl.get("prompts")]

    def _show_template_list(self) -> CommandMessage:
        """显示模板列表"""
        usable = self._get_usable_templates()

        message = "📋 **日记模板**\n\n"
        for i, (_tid, tpl) in enumerate(usable, 1):
            prompts = tpl.get("prompts", [])
            message += f"**{i}. {tpl['name']}**\n"
            for prompt in prompts[:2]:
                message += f"  • {prompt}\n"
            if len(prompts) > 2:
                message += f"  • ...(共{len(prompts)}题)\n"
            message += "\n"

        message += "用法: /pendo diary template <编号|名称>"
        return {"status": "success", "message": message}

    def _show_help(self) -> str:
        """显示日记帮助信息"""
        usable = self._get_usable_templates()
        template_hint = " | ".join(f"{i}.{tpl['name']}" for i, (_, tpl) in enumerate(usable, 1))

        return (
            "📔 **日记帮助**\n\n"
            "**写日记:**\n"
            "• /pendo diary add <内容> - 写今天的日记\n"
            "• /pendo diary add <日期> <内容> - 写指定日期\n"
            "  同一天可写多条，系统会按时间排序\n\n"
            "**模板写日记:**\n"
            f"• /pendo diary template <编号> - 模板引导写日记\n"
            f"  可选: {template_hint}\n\n"
            "**查看:**\n"
            "• /pendo diary list [范围] [mood:情绪] [cat:分类] [#标签] - 日记列表(默认本月)\n"
            "  范围: today, tomorrow, week, month, year, YYYY-MM, last7d/last30d, start..end\n"
            "• /pendo diary view [日期|ID] - 查看详情\n\n"
            "**其他:**\n"
            "• /pendo diary delete <日期|ID> - 删除日记"
        )

    def _format_entry_time(self, diary: DiaryItem) -> str:
        raw = diary.entry_time or diary.created_at or ""
        try:
            return datetime.fromisoformat(str(raw)).strftime("%H:%M")
        except ValueError:
            return ""

    def _format_diary_entry_detail(self, diary: DiaryItem, *, index: int | None = None) -> str:
        title_prefix = (
            f"**{index}. {diary.title or '日记条目'}**"
            if index
            else f"**{diary.title or '日记条目'}**"
        )
        lines = [title_prefix]
        entry_time = self._format_entry_time(diary)
        if entry_time:
            lines.append(f"🕘 时间: {entry_time}")
        if diary.mood:
            emoji = _MOOD_EMOJIS.get(diary.mood, "😐")
            mood_line = f"{emoji} 情绪: {diary.mood}"
            if diary.mood_score:
                mood_line += f" ({diary.mood_score}/10)"
            lines.append(mood_line)
        if diary.weather:
            lines.append(f"🌤️ 天气: {diary.weather}")
        if diary.location:
            lines.append(f"📍 地点: {diary.location}")
        if diary.is_favorite:
            lines.append("⭐ 收藏")
        if diary.template_answers:
            lines.append(f"📋 模板: {diary.template_id or '未命名模板'}")
        lines.append(f"`{diary.id}`")
        lines.append("")
        lines.append(diary.content or "")
        return "\n".join(lines)

    async def _get_diaries_by_date(self, user_id: str, diary_date: str) -> list[DiaryItem]:
        """通过统一仓储查询当天全部日记，避免 Handler 直接依赖 SQL 私有实现。"""
        return await self._fetch_diaries(user_id, diary_date, diary_date)

    def _parse_diary_text(self, text: str) -> dict[str, Any]:
        """解析日记文本（提取天气、地点等元信息）

        支持格式：
        - weather:晴 location:北京 内容
        - 内容 weather:"多云转晴" location:"上海 徐汇"
        - 内容 mood:happy score:8 tags:工作,复盘 favorite:true
        """
        result: dict[str, Any] = {
            "content": "",
            "weather": None,
            "location": None,
            "mood": None,
            "mood_score": None,
            "tags": [],
            "is_favorite": False,
        }

        content_parts: list[str] = []
        previous_end = 0
        for match in _DIARY_METADATA_RE.finditer(text):
            content_parts.extend((text[previous_end : match.start()], " "))
            previous_end = match.end()
            key = match.group("key").lower()
            value = match.group("value").strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                value = value[1:-1].strip()
            if key == "weather":
                result["weather"] = value
            elif key == "location":
                result["location"] = value
            elif key == "mood":
                result["mood"] = value
            elif key == "score":
                result["mood_score"] = value
            elif key in {"tags", "tag"}:
                new_tags = [item.strip() for item in re.split(r"[,，]", value) if item.strip()]
                old_tags = result["tags"] if isinstance(result["tags"], list) else []
                result["tags"] = list(dict.fromkeys([*old_tags, *new_tags]))
            elif key in {"favorite", "fav"}:
                normalized_value = value.casefold()
                if normalized_value in _TRUE_METADATA_VALUES:
                    result["is_favorite"] = True
                elif normalized_value in _FALSE_METADATA_VALUES:
                    result["is_favorite"] = False
                else:
                    raise ValueError("favorite 只接受 true/false、yes/no、on/off、1/0 或 是/否")

        content_parts.append(text[previous_end:])
        result["content"] = re.sub(r"[ \t]{2,}", " ", "".join(content_parts)).strip()

        return result

    async def _analyze_mood(self, content: str, user_id: str) -> tuple[str | None, int | None]:
        """优先使用 AI 判别日记情绪，失败时降级到规则分析。"""
        if self.ai_parser is not None:
            try:
                return await self.ai_parser.analyze_diary_mood(content, user_id)
            except Exception as exc:
                context = getattr(self.ai_parser, "context", None)
                public_error_message(
                    context,
                    exc,
                    logger=getattr(context, "logger", None) or logger,
                    component="pendo.diary.mood_fallback",
                )

        return cast(tuple[str | None, int | None], analyze_diary_mood_rule(content))
