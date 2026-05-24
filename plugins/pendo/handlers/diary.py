"""
日记(Diary)处理器
处理日记相关的所有操作，不需要AI解析
"""

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

from core.plugin_base import run_sync

from ..config import DIARY_TEMPLATES, MOOD_ANALYSIS_CONFIG, PendoConfig
from ..core.types import CommandMessage, PendoContext
from ..models.constants import ItemFields
from ..models.item import DiaryItem, ItemType
from ..utils.db_ops import DbOpsMixin
from ..utils.error_handlers import handle_command_errors
from ..utils.formatters import ItemFormatter, TAG_TOKEN_RE
from ..utils.session_utils import safe_create_reply_scoped_session, safe_end_session
from ..utils.time_utils import now_in_timezone, parse_date_optional, parse_diary_range
from ..utils.validators import normalize_diary_fields, normalize_diary_mood

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ..services.db import Database

TemplateDef = dict[str, str | list[str]]


class DiaryHandler(DbOpsMixin):
    """日记处理器

    负责处理日记（Diary）相关的所有操作：
    - 写日记（直接写或使用模板）
    - 查看和列表日记
    - 模板管理

    不需要AI解析
    """

    def __init__(self, db: "Database", ai_parser: object | None = None):
        self.db = db
        self.ai_parser = ai_parser
        # ai_parser 用于日记情绪分析（_analyze_mood），为 None 时降级到规则分析
        # 日记模板（从配置读取）
        self.templates: dict[str, TemplateDef] = cast(dict[str, TemplateDef], DIARY_TEMPLATES)

    async def _fetch_diaries(self, user_id: str, start_date: str, end_date: str) -> list[DiaryItem]:
        """获取日记列表"""
        return cast(
            list[DiaryItem],
            await run_sync(
                self.db.items.query_items_by_date_range,
                user_id,
                ItemType.DIARY.value,
                ItemFields.DIARY_DATE,
                start_date,
                end_date,
            ),
        )

    def _user_now(self, user_id: str) -> datetime:
        return now_in_timezone(user_id, self.db).replace(tzinfo=None)

    @staticmethod
    def _entry_time_for_diary_date(user_now: datetime, diary_date: str) -> str:
        return f"{diary_date}T{user_now.strftime('%H:%M:%S')}"

    async def _resolve_diary_query(
        self, user_id: str, query: str
    ) -> tuple[DiaryItem | None, str | None, CommandMessage | None]:
        """Resolve a diary by date or item ID."""
        query = (query or "").strip()
        diary_date = parse_date_optional(query)
        if diary_date:
            return await self._get_diary_by_date(user_id, diary_date), diary_date, None

        item = await self._db_get_item(query, owner_id=user_id)
        if not item:
            return None, None, {"status": "error", "message": f"❌ 找不到日记 {query}"}
        if not isinstance(item, DiaryItem):
            return None, None, self._build_wrong_type_message(query, "日记", item)

        diary = cast(DiaryItem, item)
        return diary, diary.diary_date or query, None

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

        handlers = {
            "add": lambda: self.add_diary(user_id, rest, context, group_id),
            "list": lambda: self.list_diaries(user_id, rest, context),
            "view": lambda: self.view_diary(user_id, rest, context),
            "template": lambda: self._handle_template_command(user_id, rest, context, group_id),
            "delete": lambda: self.delete_diary(user_id, rest, context),
        }

        handler = handlers.get(command)
        if handler:
            return await handler()

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
            return {
                "status": "error",
                "message": "❌ 请提供日记内容\n\n用法: /pendo diary add [日期] <内容> [weather:xxx] [location:xxx]",
            }

        # 尝试解析第一个参数是否是日期
        parts = args.split(maxsplit=1)
        first_arg = parts[0]
        rest = parts[1] if len(parts) > 1 else ""

        user_now = self._user_now(user_id)
        diary_date = parse_date_optional(first_arg)

        if diary_date:
            # 第一个参数是日期
            content_text = rest
        else:
            # 第一个参数不是日期，整个args都是内容
            diary_date = user_now.strftime("%Y-%m-%d")
            content_text = args

        _USAGE_MSG = (
            "❌ 请提供日记内容\n\n用法: /pendo diary add [日期] <内容> [weather:xxx] [location:xxx]"
        )
        if not content_text:
            return {"status": "error", "message": _USAGE_MSG}

        # 解析内容、天气、地点
        parsed = self._parse_diary_text(content_text)

        if not parsed["content"]:
            return {"status": "error", "message": _USAGE_MSG}

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
        user_now = self._user_now(user_id)
        content = str(parsed.get("content", ""))

        manual_mood = parsed.get("mood")
        manual_score = parsed.get("mood_score")
        if manual_mood or manual_score not in (None, ""):
            mood = manual_mood
            mood_score = manual_score
        else:
            mood, mood_score = await self._analyze_mood(content, user_id)

        entry_time = parsed.get("entry_time") or self._entry_time_for_diary_date(user_now, diary_date)
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
            "tags": cast(list[str], parsed.get("tags") or []),
            "category": "日记",
            "context": {"group_id": group_id} if group_id else {},
            "created_at": user_now.isoformat(timespec="seconds"),
            "updated_at": user_now.isoformat(timespec="seconds"),
        }
        try:
            item_data = normalize_diary_fields(item_data, partial=False)
        except ValueError as exc:
            return {"status": "error", "message": f"❌ {exc}"}

        entry_time = item_data.get("entry_time") or self._entry_time_for_diary_date(user_now, diary_date)
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
            mood_emojis = cast(dict[str, str], MOOD_ANALYSIS_CONFIG.get("mood_emojis", {}))
            emoji = mood_emojis.get(diary_item.mood, "📝")
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
        if not query:
            query = self._user_now(user_id).strftime("%Y-%m-%d")
        elif error := self._single_token_error(
            query, "❌ 日记详情只接受一个日期或ID\n例如: /pendo diary view 2026-05-10"
        ):
            return error

        query_date = parse_date_optional(query)
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

        diary, diary_date, error = await self._resolve_diary_query(user_id, query)
        if error:
            return error

        if not diary:
            return {
                "status": "success",
                "message": f"📔 您还没有写 {diary_date} 的日记\n\n💡 用 /pendo diary add {diary_date} <内容> 开始写",
            }

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
        import re as _re
        range_str = (range_str or "").strip()

        # 解析 mood:/cat:/#tag 过滤参数
        mood_filter = None
        category_filter = None
        tag_filter = None
        mood_match = _re.search(r"mood:(\S+)", range_str)
        if mood_match:
            try:
                mood_filter = normalize_diary_mood(mood_match.group(1))
            except ValueError:
                mood_filter = mood_match.group(1).lower()
            range_str = range_str.replace(mood_match.group(0), "").strip()

        cat_match = _re.search(r"cat:(\S+)", range_str)
        if cat_match:
            category_filter = cat_match.group(1)
            range_str = range_str.replace(cat_match.group(0), "").strip()

        tag_match = TAG_TOKEN_RE.search(range_str)
        if tag_match:
            tag_filter = tag_match.group(1)
            range_str = range_str.replace(tag_match.group(0), "").strip()

        # 解析时间范围（默认本月）
        if not range_str:
            range_str = self._user_now(user_id).strftime("%Y-%m")
        try:
            start_date, end_date = parse_diary_range(range_str, strict=True)
        except ValueError as exc:
            return {"status": "error", "message": f"❌ {str(exc)}"}

        # 查询日记
        diaries = await self._fetch_diaries(user_id, start_date, end_date)

        # 应用情绪过滤
        if mood_filter:
            diaries = [d for d in diaries if (d.mood or "").lower() == mood_filter]
        if category_filter:
            diaries = [d for d in diaries if (d.category or "") == category_filter]
        if tag_filter:
            diaries = [d for d in diaries if tag_filter in (d.tags or [])]

        filter_labels = []
        if mood_filter:
            filter_labels.append(f"情绪:{mood_filter}")
        if category_filter:
            filter_labels.append(f"分类:{category_filter}")
        if tag_filter:
            filter_labels.append(f"#{tag_filter}")
        filter_suffix = f" [{', '.join(filter_labels)}]" if filter_labels else ""

        if not diaries:
            return {
                "status": "success",
                "message": f"📔 {range_str or 'today'}{filter_suffix} 没有日记\n\n💡 用 /pendo diary add [日期] <内容> 开始写日记",
            }

        # 格式化输出
        message = f"📔 **日记列表**{filter_suffix} (共{len(diaries)}篇)\n\n"

        for diary in diaries:
            date = diary.diary_date or ""
            entry_time = self._format_entry_time(diary)

            # 情绪图标
            mood_emoji = "📝"
            if diary.mood:
                mood_emojis = cast(dict[str, str], MOOD_ANALYSIS_CONFIG.get("mood_emojis", {}))
                mood_emoji = mood_emojis.get(diary.mood, "📝")

            # 预览
            content_preview = ItemFormatter.truncate_content(
                diary.content or "", PendoConfig.SEARCH_CONTENT_PREVIEW_LENGTH
            )

            message += f"{mood_emoji} **{date} {entry_time}**\n"
            message += f"  _{content_preview}_\n"
            message += f"  `{diary.id}`\n\n"

        message += "💡 用 /pendo diary view <日期或ID> 查看完整日记"

        return {"status": "success", "message": message}

    async def delete_diary(
        self, user_id: str, date_str: str, context: PendoContext
    ) -> CommandMessage:
        """按日期或 ID 删除日记"""
        query = (date_str or "").strip()
        if not query:
            return {"status": "error", "message": "❌ 请指定要删除的日记日期或ID"}

        query_date = parse_date_optional(query)
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
            diary = entries[0]
            await self._db_soft_delete_with_log(diary.id, user_id, item_type=ItemType.DIARY.value)
            return {
                "status": "success",
                "message": f"🗑️ 已删除 {query_date} 的日记条目\n\n💡 5分钟内可用 /pendo undo 撤销",
            }

        diary, diary_date, error = await self._resolve_diary_query(user_id, query)
        if error:
            return error

        if not diary:
            return {"status": "error", "message": f"❌ 没有找到 {diary_date} 的日记"}

        # 软删除
        await self._db_soft_delete_with_log(diary.id, user_id, item_type=ItemType.DIARY.value)

        return {
            "status": "success",
            "message": f"🗑️ 已删除 {diary_date} 的日记条目\n\n💡 5分钟内可用 /pendo undo 撤销",
        }

    async def start_template_session(
        self, user_id: str, template_id: str, context: PendoContext, group_id: int | None = None
    ) -> CommandMessage:
        """启动日记模板多轮对话"""
        template = self.templates.get(template_id)
        if not template:
            return {"status": "error", "message": "❌ 模板不存在"}

        diary_date = self._user_now(user_id).strftime("%Y-%m-%d")
        prompts = cast(list[str], template.get("prompts", []))

        if not prompts:
            return {"status": "error", "message": "❌ 该模板没有预设问题"}

        # 创建会话
        if await safe_create_reply_scoped_session(
            context,
                initial_data={
                    "type": "diary_template",
                    "owner_id": user_id,
                    "template_id": template_id,
                    "diary_date": diary_date,
                    "group_id": group_id,
                    "prompts": prompts,
                    "step": 0,
                    "answers": [],
                    "total_steps": len(prompts),
                },
                timeout=300.0,  # 5分钟超时
            ):
            first_question = prompts[0]
            return {
                "status": "success",
                "message": f"📋 **开始写{template['name']}** ({diary_date})\n\n1/{len(prompts)}: {first_question}\n\n(发送 '退出' 可随时结束)",
            }
        else:
            # Fallback：直接显示模板
            return await self.use_template(user_id, template_id, diary_date)

    async def use_template(self, user_id: str, template_id: str, diary_date: str) -> CommandMessage:
        """Fallback when multi-turn session creation is unavailable."""
        template = self.templates.get(template_id)
        if not template:
            return {"status": "error", "message": "❌ 模板不存在"}
        prompts = cast(list[str], template.get("prompts", []))
        lines = [f"📋 **{template.get('name', template_id)}** ({diary_date})", ""]
        lines.extend(f"{idx}. {prompt}" for idx, prompt in enumerate(prompts, 1))
        lines.append("")
        lines.append("💡 用 /pendo diary add 写下答案，或稍后重试模板引导。")
        return {"status": "success", "message": "\n".join(lines)}

    async def handle_session_message(
        self, text: str, context: PendoContext, session: dict[str, Any]
    ) -> CommandMessage:
        """处理会话消息"""
        # 记录当前问题的答案
        answers = session.get("answers", [])
        answers.append(text)
        session.set("answers", answers)

        # 移动到下一步
        step = session.get("step", 0) + 1
        session.set("step", step)

        prompts = session.get("prompts", [])
        total_steps = session.get("total_steps", 0)

        # 检查是否完成
        if step >= total_steps:
            # 完成所有问题，生成日记
            await safe_end_session(context)

            owner_id = session.get("owner_id")
            diary_date = session.get("diary_date")
            template_id = session.get("template_id")
            group_id = session.get("group_id")
            if (
                not isinstance(owner_id, str)
                or not isinstance(diary_date, str)
                or not isinstance(template_id, str)
            ):
                return {"status": "error", "message": "❌ 会话数据缺失，无法提交模板日记"}
            group_id_val = group_id if isinstance(group_id, int) else None

            return await self._submit_template_result(
                owner_id,
                diary_date,
                template_id,
                cast(list[str], prompts),
                cast(list[str], answers),
                group_id_val,
                context,
            )

        # 下一个问题
        next_question = prompts[step]
        return {"status": "question", "message": f"{step + 1}/{total_steps}: {next_question}"}

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
            for q, a in zip(prompts, answers, strict=False)
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

        # 按编号匹配
        try:
            idx = int(arg)
            if 1 <= idx <= len(usable):
                template_id = usable[idx - 1][0]
                return await self.start_template_session(user_id, template_id, context, group_id)
            else:
                return {"status": "error", "message": f"❌ 无效编号，可选 1-{len(usable)}"}
        except ValueError:
            pass

        # 按名称匹配
        for tid, tpl in usable:
            if tpl["name"] == arg:
                return await self.start_template_session(user_id, tid, context, group_id)

        # 按ID匹配
        if arg in self.templates and self.templates[arg].get("prompts"):
            return await self.start_template_session(user_id, arg, context, group_id)

        return {"status": "error", "message": f"❌ 未找到模板: {arg}\n\n{self._show_template_list()['message']}"}

    def _get_usable_templates(self) -> list[tuple[str, dict]]:
        """获取有 prompts 的可用模板列表"""
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
        title_prefix = f"**{index}. {diary.title or '日记条目'}**" if index else f"**{diary.title or '日记条目'}**"
        lines = [title_prefix]
        entry_time = self._format_entry_time(diary)
        if entry_time:
            lines.append(f"🕘 时间: {entry_time}")
        if diary.mood:
            mood_emojis = cast(dict[str, str], MOOD_ANALYSIS_CONFIG.get("mood_emojis", {}))
            emoji = mood_emojis.get(diary.mood, "😐")
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
        """根据日期获取当天所有日记条目。"""

        def _fetch():
            conn = self.db.conn_manager.get_connection()
            cursor = conn.cursor()
            cursor.execute(
                f"""
                SELECT * FROM items
                WHERE owner_id = ?
                AND type = '{ItemType.DIARY.value}'
                AND diary_date = ?
                AND deleted = 0
                ORDER BY COALESCE(entry_time, created_at, updated_at) DESC, updated_at DESC
                """,
                (user_id, diary_date),
            )
            entries: list[DiaryItem] = []
            for row in cursor.fetchall():
                item = self.db.items._row_to_item(row)
                if isinstance(item, DiaryItem):
                    entries.append(item)
            return entries

        return await run_sync(_fetch)

    async def _get_diary_by_date(self, user_id: str, diary_date: str) -> DiaryItem | None:
        """根据日期获取最近一条日记，返回DiaryItem dataclass"""

        def _fetch():
            conn = self.db.conn_manager.get_connection()
            cursor = conn.cursor()

            cursor.execute(
                f"""
                SELECT * FROM items
                WHERE owner_id = ?
                AND type = '{ItemType.DIARY.value}'
                AND diary_date = ?
                AND deleted = 0
                ORDER BY COALESCE(entry_time, created_at, updated_at) DESC, updated_at DESC
                LIMIT 1
            """,
                (user_id, diary_date),
            )

            row = cursor.fetchone()
            if row:
                item = self.db.items._row_to_item(row)
                return item if isinstance(item, DiaryItem) else None
            return None

        return await run_sync(_fetch)

    def _parse_diary_text(self, text: str) -> dict[str, Any]:
        """解析日记文本（提取天气、地点等元信息）

        支持格式：
        - weather:晴 location:北京 内容
        - 内容 weather:"多云转晴" location:"上海 徐汇"
        - 内容 mood:happy score:8 tags:工作,复盘 favorite:true
        """
        import re

        result: dict[str, Any] = {
            "content": "",
            "weather": None,
            "location": None,
            "mood": None,
            "mood_score": None,
            "tags": [],
            "is_favorite": False,
        }

        pattern = re.compile(
            r"(?P<key>weather|location|mood|score|tags|tag|favorite|fav):"
            r"(?P<value>\"[^\"]*\"|'[^']*'|\S+)"
        )

        def _clean(raw: str) -> str:
            raw = raw.strip()
            if (raw.startswith('"') and raw.endswith('"')) or (raw.startswith("'") and raw.endswith("'")):
                return raw[1:-1].strip()
            return raw

        for match in list(pattern.finditer(text)):
            key = match.group("key")
            value = _clean(match.group("value"))
            if key == "weather":
                result["weather"] = value
            elif key == "location":
                result["location"] = value
            elif key == "mood":
                result["mood"] = value
            elif key == "score":
                result["mood_score"] = value
            elif key in {"tags", "tag"}:
                result["tags"] = [item.strip() for item in re.split(r"[,，]", value) if item.strip()]
            elif key in {"favorite", "fav"}:
                result["is_favorite"] = value.lower() in {"1", "true", "yes", "y", "on", "是", "收藏"}
            text = text.replace(match.group(0), " ").strip()

        # 剩余内容
        result["content"] = re.sub(r"[ \t]{2,}", " ", text).strip()

        return result

    async def _analyze_mood(self, content: str, user_id: str) -> tuple[str | None, int | None]:
        """优先使用 AI 判别日记情绪，失败时降级到规则分析。"""
        if self.ai_parser and hasattr(self.ai_parser, "analyze_diary_mood"):
            try:
                return await self.ai_parser.analyze_diary_mood(content, user_id)
            except Exception:
                logger.exception("AI 情绪分析失败，回退到规则分析")

        return self._analyze_mood_rule(content)

    def _analyze_mood_rule(self, content: str) -> tuple[str | None, int | None]:
        """使用配置词典进行规则情绪分析。"""
        positive_words = cast(list[str], MOOD_ANALYSIS_CONFIG.get("positive_words", []))
        negative_words = cast(list[str], MOOD_ANALYSIS_CONFIG.get("negative_words", []))
        calm_words = cast(list[str], MOOD_ANALYSIS_CONFIG.get("calm_words", []))
        excited_words = cast(list[str], MOOD_ANALYSIS_CONFIG.get("excited_words", []))
        angry_words = cast(list[str], MOOD_ANALYSIS_CONFIG.get("angry_words", []))
        tired_words = cast(list[str], MOOD_ANALYSIS_CONFIG.get("tired_words", []))
        anxious_words = cast(list[str], MOOD_ANALYSIS_CONFIG.get("anxious_words", []))
        grateful_words = cast(list[str], MOOD_ANALYSIS_CONFIG.get("grateful_words", []))

        pos_count = sum(1 for word in positive_words if word in content)
        neg_count = sum(1 for word in negative_words if word in content)
        calm_count = sum(1 for word in calm_words if word in content)
        excited_count = sum(1 for word in excited_words if word in content)
        angry_count = sum(1 for word in angry_words if word in content)
        tired_count = sum(1 for word in tired_words if word in content)
        anxious_count = sum(1 for word in anxious_words if word in content)
        grateful_count = sum(1 for word in grateful_words if word in content)

        # 根据关键词出现次数和数量确定情绪类型
        base_scores = cast(dict[str, int], MOOD_ANALYSIS_CONFIG.get("base_scores", {}))
        raw_increment = MOOD_ANALYSIS_CONFIG.get("score_increment", 1)
        score_increment = raw_increment if isinstance(raw_increment, int) else 1

        # 按优先级判断情绪
        # S-3修复：删去 or (excited_count + pos_count) >= 2，
        # 该条件在 excited_count==0、pos_count>=2 时将 happy 误判为 excited
        if grateful_count > 0:
            mood = "grateful"
            score = min(10, base_scores.get("grateful", 7) + grateful_count)
            return mood, score
        if excited_count > 0:
            mood = "excited"
            score = min(10, base_scores.get("excited", 8) + excited_count + pos_count)
            return mood, score
        elif angry_count > neg_count or angry_count >= 2:
            mood = "angry"
            score = max(1, base_scores.get("angry", 3) - angry_count)
            return mood, score
        elif anxious_count > 0:
            mood = "anxious"
            score = max(1, base_scores.get("anxious", 3) - anxious_count)
            return mood, score
        elif tired_count > 0:
            mood = "tired"
            score = max(1, base_scores.get("tired", 4) - tired_count)
            return mood, score
        elif pos_count > neg_count and pos_count > calm_count:
            mood = "happy"
            score = min(10, base_scores.get("happy", 6) + pos_count * score_increment)
            return mood, score
        elif neg_count > pos_count:
            mood = "sad"
            score = max(1, base_scores.get("sad", 5) - neg_count * score_increment)
            return mood, score
        elif calm_count > 0:
            mood = "calm"
            return mood, base_scores.get("calm", 5)

        return "neutral", base_scores.get("neutral", 5)
