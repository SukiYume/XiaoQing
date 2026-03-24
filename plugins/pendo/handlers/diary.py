"""
日记(Diary)处理器
处理日记相关的所有操作，不需要AI解析
"""

from typing import Any, TYPE_CHECKING, cast
from datetime import datetime
import logging
from ..models.item import ItemType, DiaryItem
from ..models.constants import ItemFields
from ..core.types import PendoContext, CommandMessage
from core.plugin_base import run_sync
from ..utils.db_ops import DbOpsMixin
from ..utils.error_handlers import handle_command_errors
from ..utils.session_utils import safe_create_session, safe_end_session
from ..config import PendoConfig, DIARY_TEMPLATES, MOOD_ANALYSIS_CONFIG
from ..utils.time_utils import parse_date_optional, parse_diary_range
from ..utils.formatters import ItemFormatter

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
        # ai_parser保留接口兼容性，但不使用
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

    @handle_command_errors
    async def handle(
        self, user_id: str, args: str, context: PendoContext, group_id: int | None = None
    ) -> CommandMessage:
        """处理日记相关命令

        命令格式：
        - /pendo diary add [日期] <内容> -> 写日记
        - /pendo diary list [范围] -> 查看日记列表
        - /pendo diary view <日期> -> 查看日记详情
        - /pendo diary template -> 查看所有模板
        - /pendo diary <模板ID> -> 使用模板写日记
        - /pendo diary delete <日期> -> 删除日记
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

        diary_date = parse_date_optional(first_arg)

        if diary_date:
            # 第一个参数是日期
            content_text = rest
        else:
            # 第一个参数不是日期，整个args都是内容
            diary_date = datetime.now().strftime("%Y-%m-%d")
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

        # 检查是否已有当天日记
        existing = await self._get_diary_by_date(user_id, diary_date)

        if existing:
            # 已有日记，追加内容
            new_content = (existing.content or "") + "\n\n---\n\n" + parsed["content"]
            # 重新分析情绪（基于完整的新内容）
            new_mood, new_mood_score = self._analyze_mood(new_content)

            updates = {
                "content": new_content,
                "mood": new_mood,
                "mood_score": new_mood_score,
                "updated_at": datetime.now().isoformat(),
                "type": ItemType.DIARY.value,
            }

            # 如果新内容指定了天气或地点，更新
            if parsed.get("weather"):
                updates["weather"] = parsed["weather"]
            if parsed.get("location"):
                updates["location"] = parsed["location"]

            await self._db_update_with_log(existing.id, updates, user_id, action="append_diary")

            return {"status": "success", "message": f"✅ 已追加到 {diary_date} 的日记"}

        # 创建新日记
        return await self.create_diary(user_id, diary_date, parsed, context, group_id=group_id)

    async def create_diary(
        self,
        user_id: str,
        diary_date: str,
        parsed_or_content: dict[str, Any] | str,
        context: PendoContext,
        template_id: str | None = None,
        group_id: int | None = None,
    ) -> CommandMessage:
        """创建日记"""
        # 兼容旧接口：支持直接传入字符串内容
        if isinstance(parsed_or_content, str):
            parsed = self._parse_diary_text(parsed_or_content)
        else:
            parsed = parsed_or_content

        content = str(parsed.get("content", ""))

        # 简单情绪分析
        mood, mood_score = self._analyze_mood(content)

        # 生成标题
        title = f"{diary_date}的日记"

        # 创建数据
        from ..models.item import DiaryItem

        diary_item = DiaryItem(
            owner_id=user_id,
            title=title,
            content=content,
            diary_date=diary_date,
            mood=mood,
            mood_score=mood_score,
            template_id=template_id,
            category="日记",
            context={"group_id": group_id} if group_id else {},
            created_at=datetime.now().isoformat(),
            updated_at=datetime.now().isoformat(),
        )

        # 添加天气和地点（如果有）
        if parsed.get("weather"):
            diary_item.weather = parsed["weather"]
        location = parsed.get("location")
        if isinstance(location, str):
            diary_item.location = location

        # 保存到数据库
        item_id = await self._db_create_with_log(
            diary_item, owner_id=user_id, action="create_diary"
        )

        diary_item.id = item_id

        # 格式化返回消息
        message = f"✅ 已记录 {diary_date} 的日记\n\n"
        if mood:
            mood_emojis = cast(dict[str, str], MOOD_ANALYSIS_CONFIG.get("mood_emojis", {}))
            emoji = mood_emojis.get(mood, "📝")
            message += f"{emoji} 情绪: {mood}"
            if mood_score:
                message += f" ({mood_score}/10)"
            message += "\n"
        if parsed.get("weather"):
            message += f"🌤️ 天气: {parsed['weather']}\n"
        if parsed.get("location"):
            message += f"📍 地点: {parsed['location']}\n"
        message += f"`{item_id}`\n\n"
        message += f"💡 用 /pendo diary view {diary_date} 查看"

        return {"status": "success", "message": message, "item_id": item_id}

    async def view_diary(
        self, user_id: str, date_str: str, context: PendoContext
    ) -> CommandMessage:
        """查看日记"""
        diary_date = parse_date_optional(date_str)

        if not diary_date:
            return {"status": "error", "message": "❌ 无法解析日期，请使用 YYYY-MM-DD 格式"}

        # 获取日记
        diary = await self._get_diary_by_date(user_id, diary_date)

        if not diary:
            return {
                "status": "success",
                "message": f"📔 您还没有写 {diary_date} 的日记\n\n💡 用 /pendo diary add {diary_date} <内容> 开始写",
            }

        # 格式化输出
        message = f"📔 **{diary_date}的日记**\n\n"

        # 元信息
        if diary.mood:
            mood_emojis = cast(dict[str, str], MOOD_ANALYSIS_CONFIG.get("mood_emojis", {}))
            emoji = mood_emojis.get(diary.mood, "😐")
            message += f"{emoji} 情绪: {diary.mood}"
            if diary.mood_score:
                message += f" ({diary.mood_score}/10)"
            message += "\n"

        if diary.weather:
            message += f"🌤️ 天气: {diary.weather}\n"

        if hasattr(diary, "location") and diary.location:
            message += f"📍 地点: {diary.location}\n"

        message += "\n---\n\n"

        # 内容
        message += diary.content or ""

        return {"status": "success", "message": message}

    async def list_diaries(
        self, user_id: str, range_str: str, context: PendoContext
    ) -> CommandMessage:
        """列出日记

        格式：
        - /pendo diary list -> 默认本月
        - /pendo diary list today/tomorrow/week/year
        - /pendo diary list YYYY-MM (如 2026-02)
        - /pendo diary list last7d
        - /pendo diary list start..end
        """
        # 解析时间范围（默认本月）
        if not range_str or not range_str.strip():
            range_str = datetime.now().strftime("%Y-%m")
        start_date, end_date = parse_diary_range(range_str)

        # 查询日记
        diaries = await self._fetch_diaries(user_id, start_date, end_date)

        if not diaries:
            return {
                "status": "success",
                "message": f"📔 {range_str or 'today'} 没有日记\n\n💡 用 /pendo diary add [日期] <内容> 开始写日记",
            }

        # 格式化输出
        message = f"📔 **日记列表** (共{len(diaries)}篇)\n\n"

        for diary in diaries:
            date = diary.diary_date or ""

            # 情绪图标
            mood_emoji = "📝"
            if diary.mood:
                mood_emojis = cast(dict[str, str], MOOD_ANALYSIS_CONFIG.get("mood_emojis", {}))
                mood_emoji = mood_emojis.get(diary.mood, "📝")

            # 预览
            content_preview = ItemFormatter.truncate_content(
                diary.content or "", PendoConfig.SEARCH_CONTENT_PREVIEW_LENGTH
            )

            message += f"{mood_emoji} **{date}**\n"
            message += f"  _{content_preview}_\n"
            message += f"  `{diary.id}`\n\n"

        message += f"💡 用 /pendo diary view <日期> 查看完整日记"

        return {"status": "success", "message": message}

    async def delete_diary(
        self, user_id: str, date_str: str, context: PendoContext
    ) -> CommandMessage:
        """删除指定日期的日记"""
        diary_date = parse_date_optional(date_str)

        if not diary_date:
            return {"status": "error", "message": "❌ 无法解析日期，请使用 YYYY-MM-DD 格式"}

        # 获取日记
        diary = await self._get_diary_by_date(user_id, diary_date)

        if not diary:
            return {"status": "error", "message": f"❌ 没有找到 {diary_date} 的日记"}

        # 软删除
        await self._db_soft_delete_with_log(diary.id, user_id, item_type=ItemType.DIARY.value)

        return {
            "status": "success",
            "message": f"🗑️ 已删除 {diary_date} 的日记\n\n💡 5分钟内可用 /pendo undo 撤销",
        }

    async def start_template_session(
        self, user_id: str, template_id: str, context: PendoContext, group_id: int | None = None
    ) -> CommandMessage:
        """启动日记模板多轮对话"""
        template = self.templates.get(template_id)
        if not template:
            return {"status": "error", "message": "❌ 模板不存在"}

        diary_date = datetime.now().strftime("%Y-%m-%d")
        prompts = cast(list[str], template.get("prompts", []))

        if not prompts:
            return {"status": "error", "message": "❌ 该模板没有预设问题"}

        # 创建会话
        if await safe_create_session(
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

    async def handle_session_message(
        self, text: str, context: PendoContext, session: dict[str, Any]
    ) -> CommandMessage:
        """处理会话消息"""
        # 记录当前问题的答案
        answers = session.get("answers", [])
        answers.append(text)
        session["answers"] = answers

        # 移动到下一步
        step = session.get("step", 0) + 1
        session["step"] = step

        prompts = session.get("prompts", [])
        total_steps = session.get("total_steps", 0)

        # I-3修复：session 始终是字典，直接用字典赋值（删除冗余的 .set() 分支）

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
        # 拼接内容
        content = ""
        for q, a in zip(prompts, answers):
            content += f"**{q}**\n{a}\n\n"

        return await self.create_diary(
            user_id, diary_date, content.strip(), context, template_id, group_id
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
        for i, (tid, tpl) in enumerate(usable, 1):
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
            "  同一天再写会自动追加\n\n"
            "**模板写日记:**\n"
            f"• /pendo diary template <编号> - 模板引导写日记\n"
            f"  可选: {template_hint}\n\n"
            "**查看:**\n"
            "• /pendo diary list [范围] - 日记列表(默认本月)\n"
            "  范围: today, week, YYYY-MM, last7d\n"
            "• /pendo diary view <日期> - 查看详情\n\n"
            "**其他:**\n"
            "• /pendo diary delete <日期> - 删除日记"
        )

    async def _get_diary_by_date(self, user_id: str, diary_date: str) -> DiaryItem | None:
        """根据日期获取日记，返回DiaryItem dataclass"""

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

    def _parse_diary_text(self, text: str) -> dict[str, str | None]:
        """解析日记文本（提取天气、地点等元信息）

        支持格式：
        - weather:晴 location:北京 内容
        - 内容 weather:晴
        """
        import re

        result = {"content": "", "weather": None, "location": None}

        # 提取 weather:xxx
        weather_match = re.search(r"weather:(\S+)", text)
        if weather_match:
            result["weather"] = weather_match.group(1)
            text = text.replace(weather_match.group(0), "").strip()

        # 提取 location:xxx
        location_match = re.search(r"location:(\S+)", text)
        if location_match:
            result["location"] = location_match.group(1)
            text = text.replace(location_match.group(0), "").strip()

        # 剩余内容
        result["content"] = text.strip()

        return result

    def _analyze_mood(self, content: str) -> tuple[str | None, int | None]:
        """分析日记内容的情绪

        使用配置中的情绪词典进行简单关键词匹配分析。

        Args:
            content: 日记内容

        Returns:
            (情绪类型, 情绪分数) 元组，如果没有识别到情绪则返回 (None, None)
        """
        positive_words = cast(list[str], MOOD_ANALYSIS_CONFIG.get("positive_words", []))
        negative_words = cast(list[str], MOOD_ANALYSIS_CONFIG.get("negative_words", []))
        calm_words = cast(list[str], MOOD_ANALYSIS_CONFIG.get("calm_words", []))
        excited_words = cast(list[str], MOOD_ANALYSIS_CONFIG.get("excited_words", []))
        angry_words = cast(list[str], MOOD_ANALYSIS_CONFIG.get("angry_words", []))

        pos_count = sum(1 for word in positive_words if word in content)
        neg_count = sum(1 for word in negative_words if word in content)
        calm_count = sum(1 for word in calm_words if word in content)
        excited_count = sum(1 for word in excited_words if word in content)
        angry_count = sum(1 for word in angry_words if word in content)

        # 根据关键词出现次数和数量确定情绪类型
        base_scores = cast(dict[str, int], MOOD_ANALYSIS_CONFIG.get("base_scores", {}))
        raw_increment = MOOD_ANALYSIS_CONFIG.get("score_increment", 1)
        score_increment = raw_increment if isinstance(raw_increment, int) else 1

        # 按优先级判断情绪
        # S-3修复：删去 or (excited_count + pos_count) >= 2，
        # 该条件在 excited_count==0、pos_count>=2 时将 happy 误判为 excited
        if excited_count > 0:
            mood = "excited"
            score = min(10, base_scores.get("excited", 8) + excited_count + pos_count)
            return mood, score
        elif angry_count > neg_count or angry_count >= 2:
            mood = "angry"
            score = max(1, base_scores.get("angry", 3) - angry_count)
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

        return None, None
