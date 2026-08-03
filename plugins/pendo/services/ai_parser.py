"""使用 LLM 解析日程和日记情绪，失败时降级到本地规则。"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from math import ceil
from typing import TYPE_CHECKING, Any, ClassVar, Final, cast

from dateutil import parser

from core.args import parse_int
from core.public_errors import public_error_message

from ..config import MOOD_ANALYSIS_CONFIG
from ..utils.settings_utils import parse_custom_settings
from ..utils.time_utils import now_in_timezone, parse_and_localize, parse_remind_times
from ..utils.validators import normalize_reminder_rules
from .rule_parser import RuleParser

if TYPE_CHECKING:
    from .db import Database

logger = logging.getLogger(__name__)
WEEKDAY_NAMES: Final = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
MOOD_WORD_GROUPS: Final = (
    "positive",
    "negative",
    "calm",
    "excited",
    "angry",
    "tired",
    "anxious",
    "grateful",
)
CHINESE_DIGITS: Final = {
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
OFFSET_NUMBER_PATTERN: Final = r"\d+|[零一二两三四五六七八九十百半]+"
OFFSET_UNIT_PATTERN: Final = r"分钟|min|m|小时|hour|h|天|day|d|周|week|w"
OFFSET_TOKEN_RE: Final = re.compile(rf"({OFFSET_NUMBER_PATTERN})\s*({OFFSET_UNIT_PATTERN})")
REMINDER_DESCRIPTION_RE: Final = re.compile(
    rf"(?:提前\s*)?({OFFSET_NUMBER_PATTERN})\s*(?:个)?\s*({OFFSET_UNIT_PATTERN})"
)
OFFSET_UNIT_SECONDS: Final = {
    "分钟": 60,
    "min": 60,
    "m": 60,
    "小时": 3600,
    "hour": 3600,
    "h": 3600,
    "天": 86400,
    "day": 86400,
    "d": 86400,
    "周": 604800,
    "week": 604800,
    "w": 604800,
}


def analyze_diary_mood_rule(content: str) -> tuple[str, int]:
    """使用本地关键词词典分析日记情绪，作为 AI 不可用时的唯一降级实现。"""
    counts: dict[str, int] = {}
    for group in MOOD_WORD_GROUPS:
        raw_words = MOOD_ANALYSIS_CONFIG.get(f"{group}_words", [])
        words = [str(word) for word in raw_words] if isinstance(raw_words, list) else []
        counts[group] = sum(1 for word in words if word in content)

    raw_scores = MOOD_ANALYSIS_CONFIG.get("base_scores", {})
    base_scores = (
        {str(key): value for key, value in raw_scores.items() if isinstance(value, int)}
        if isinstance(raw_scores, dict)
        else {}
    )
    raw_increment = MOOD_ANALYSIS_CONFIG.get("score_increment", 1)
    score_increment = raw_increment if isinstance(raw_increment, int) else 1

    positive = counts["positive"]
    negative = counts["negative"]
    if counts["grateful"]:
        return "grateful", min(10, base_scores.get("grateful", 7) + counts["grateful"])
    if counts["excited"]:
        return "excited", min(
            10,
            base_scores.get("excited", 8) + counts["excited"] + positive,
        )
    if counts["angry"] > negative or counts["angry"] >= 2:
        return "angry", max(1, base_scores.get("angry", 3) - counts["angry"])
    if counts["anxious"]:
        return "anxious", max(1, base_scores.get("anxious", 3) - counts["anxious"])
    if counts["tired"]:
        return "tired", max(1, base_scores.get("tired", 4) - counts["tired"])
    if positive > negative and positive > counts["calm"]:
        return "happy", min(10, base_scores.get("happy", 6) + positive * score_increment)
    if negative > positive:
        return "sad", max(1, base_scores.get("sad", 5) - negative * score_increment)
    if counts["calm"]:
        return "calm", base_scores.get("calm", 5)
    return "neutral", base_scores.get("neutral", 5)


class RateLimiter:
    """按用户维护滑动时间窗的轻量速率限制器。"""

    def __init__(self, max_calls: int = 10, time_window: int = 60) -> None:
        """初始化速率限制器

        Args:
            max_calls: 时间窗口内最大调用次数
            time_window: 时间窗口（秒）
        """
        if max_calls <= 0 or time_window <= 0:
            raise ValueError("rate limit max_calls and time_window must be positive")
        self.max_calls = max_calls
        self.time_window = time_window
        self.call_history: dict[str, list[float]] = {}

    def check_rate_limit(self, user_id: str) -> tuple[bool, int]:
        """检查是否超过速率限制

        Args:
            user_id: 用户ID

        Returns:
            (是否允许调用, 剩余秒数)
        """
        now = time.time()
        cutoff = now - self.time_window
        for key, timestamps in tuple(self.call_history.items()):
            active = [timestamp for timestamp in timestamps if timestamp > cutoff]
            if active:
                self.call_history[key] = active
            else:
                del self.call_history[key]

        # 未命中的用户先使用未挂载列表，只有实际放行时才创建桶。
        history = self.call_history.get(user_id, [])

        if len(history) >= self.max_calls:
            oldest = history[0]
            remaining = ceil(oldest + self.time_window - now)
            return False, max(remaining, 0)

        history.append(now)
        self.call_history[user_id] = history
        return True, 0


class AIParser:
    """AI解析器 - 专注于Event类型解析"""

    # 全局速率限制器实例
    _rate_limiter: ClassVar[RateLimiter] = RateLimiter(max_calls=20, time_window=60)

    # Event专用prompt模板
    PARSE_PROMPT_TEMPLATE: ClassVar[str] = """解析日程信息，提取时间、地点、提醒等字段。

当前时间: {current_date} ({current_weekday})
用户输入: {text}

返回JSON:
{{
  "title": "简洁标题",
  "start_time": "YYYY-MM-DDTHH:MM:SS或null（多节点milestones时留null）",
  "end_time": "YYYY-MM-DDTHH:MM:SS或null（多节点milestones时留null）",
  "location": "地点或null",
  "category": "工作|学习|生活|健康|财务|社交",
  "remind_offsets": ["提前1天", "提前1小时"],
  "rrule": "RFC5545格式或null",
  "milestones": [
    {{"name": "节点名称", "time": "YYYY-MM-DDTHH:MM:SS"}}
  ],
  "notes": "备注内容或null"
}}

规则:
- 相对时间转绝对时间(明天→具体日期)
- 无时间则默认09:00
- milestones 只用于两个及以上独立时间节点；系统会把每个 milestone 创建成可独立删除、修改、查询的日程节点，并用 title 作为整体日程标题
- 若用户描述两个及以上具名时间点(如注册截止、会议开始、会议结束等事件节点)，填milestones列表，start_time/end_time留null
- 只有一个时间点的截止、申请、提交、面试、会议等普通单次日程必须写入start_time，milestones留空列表[]
- "提前X天/周/小时提醒"是提醒偏移量，必须放入remind_offsets，绝对不能作为milestones节点
- 普通单次事件milestones留空列表[]
- 重复事件: 设置rrule，milestones必须留空列表[]，start_time设为第一次发生的时间
- 重复: 每天→FREQ=DAILY, 每周→FREQ=WEEKLY, 每月X号→FREQ=MONTHLY;BYMONTHDAY=X
- 重复N次→添加;COUNT=N
- 提醒支持: 分钟/小时/天/周
- notes提取用户标注为"备注"的内容(URL、说明等)

仅返回JSON。"""

    DIARY_MOOD_PROMPT_TEMPLATE: ClassVar[str] = """判断这篇日记的主情绪。

当前时间: {current_date} ({current_weekday})
日记内容: {text}

返回 JSON:
{{
  "mood": "happy|calm|excited|sad|angry|tired|anxious|grateful|neutral",
  "mood_score": 1-10
}}

规则:
- 只允许以上 mood
- 即使内容偏平淡，也应优先判断为 neutral 或 calm，而不是返回空
- score 表示情绪强度，1 最弱，10 最强
- 只返回 JSON，不要解释。"""

    def __init__(
        self,
        context: Any | None = None,
        db: Database | None = None,
        *,
        now_factory: Callable[[Any], datetime] | None = None,
    ) -> None:
        self.context = context
        self.db = db
        self.rule_parser = RuleParser()
        self._now_factory = now_factory

    def _now(self, tz: Any = None) -> datetime:
        """使用可注入时钟，使日期相关解析可稳定测试。"""
        if self._now_factory is not None:
            return self._now_factory(tz)
        return datetime.now(tz)

    def _record_degraded_error(self, exc: BaseException, *, component: str) -> None:
        """记录可降级故障，同时避免把原始异常文本暴露到日志。"""
        if self.context is not None:
            public_error_message(
                self.context,
                exc,
                logger=logger,
                component=component,
            )
            return
        logger.warning(
            "%s degraded error_type=%s",
            component,
            type(exc).__name__,
        )

    def has_sensitive_data_consent(self, user_id: str) -> bool:
        """返回用户是否明确允许把日记内容交给外部 AI 分析。"""
        if self.db is None:
            return False
        try:
            settings = self.db.get_user_settings(user_id)
            return bool(parse_custom_settings(settings).get("ai_sensitive_data_consent", False))
        except Exception as exc:
            self._record_degraded_error(exc, component="pendo.ai_parser.consent")
            return False

    async def _call_llm(
        self, messages: list[dict[str, str]], temperature: float = 0.3
    ) -> str | None:
        """通过 core 的 pendo.parse route 调用模型。"""

        try:
            ai = getattr(getattr(self.context, "capabilities", None), "ai", None)
            if ai is None:
                return None
            result = await ai.complete(
                "parse",
                messages,
                temperature=temperature,
            )
            return result.content or None
        except Exception as exc:
            self._record_degraded_error(exc, component="pendo.ai_parser.llm")
            return None

    def parse_natural_language(self, text: str, user_id: str) -> dict[str, Any]:
        """同步规则解析（固定返回 event 类型），也用作 AI 解析的降级路径"""
        parsed = self.rule_parser.parse(text, user_id)
        parsed["parse_source"] = "rule"
        parsed["type"] = "event"
        return parsed

    def _prompt_time_context(self, user_id: str) -> tuple[str, str]:
        """返回用户本地的当前时间文本和星期文本。"""
        user_now = now_in_timezone(user_id, self.db)
        return user_now.strftime("%Y-%m-%d %H:%M"), WEEKDAY_NAMES[user_now.weekday()]

    def _fallback_event_result(
        self, source_text: str, user_id: str, *, partial: bool
    ) -> dict[str, Any]:
        """规则解析兜底并统一整理返回结果。"""
        parsed = self.parse_natural_language(source_text, user_id)
        return self._build_event_result(parsed, source_text, user_id, partial=partial)

    async def parse_event_with_ai(
        self,
        text: str,
        user_id: str,
        *,
        partial: bool = False,
        fallback_text: str | None = None,
    ) -> dict[str, Any]:
        """使用AI解析日程（专用于event类型）

        Args:
            text: 用户输入文本
            user_id: 用户ID
            partial: True 时仅返回明确解析出的字段，不填充 title/category/content 默认值
            fallback_text: AI失败时用于规则解析的原始文本

        Returns:
            解析后的event数据字典
        """
        source_text = fallback_text or text

        # 检查速率限制
        allowed, wait_seconds = self._rate_limiter.check_rate_limit(user_id)
        if not allowed:
            logger.warning("用户 %s 超过AI解析速率限制，等待 %s 秒", user_id, wait_seconds)
            return self._fallback_event_result(source_text, user_id, partial=partial)

        try:
            current_date, current_weekday = self._prompt_time_context(user_id)

            prompt = self.PARSE_PROMPT_TEMPLATE.format(
                current_date=current_date, current_weekday=current_weekday, text=text
            )

            messages = [
                {"role": "system", "content": "你是日程解析助手，只返回JSON。"},
                {"role": "user", "content": prompt},
            ]

            response = await self._call_llm(messages)
            if not response:
                return self._fallback_event_result(source_text, user_id, partial=partial)

            parsed = self._parse_json_object(response)
            if parsed is None:
                return self._fallback_event_result(source_text, user_id, partial=partial)

            logger.info("AI event parse completed: user=%s fields=%s", user_id, sorted(parsed))

            return self._build_event_result(parsed, source_text, user_id, partial=partial)

        except Exception as exc:
            self._record_degraded_error(exc, component="pendo.ai_parser.event")
            return self._fallback_event_result(source_text, user_id, partial=partial)

    async def analyze_diary_mood(self, text: str, user_id: str) -> tuple[str | None, int | None]:
        """使用 AI 分析日记主情绪，失败时降级到规则情绪分析。"""
        if not self.has_sensitive_data_consent(user_id):
            logger.info(
                "AI diary analysis skipped: user=%s has not consented (chars=%d)",
                user_id,
                len(text),
            )
            return analyze_diary_mood_rule(text)
        allowed, wait_seconds = self._rate_limiter.check_rate_limit(user_id)
        if not allowed:
            logger.warning("用户 %s 超过AI情绪分析速率限制，等待 %s 秒", user_id, wait_seconds)
            return analyze_diary_mood_rule(text)

        try:
            current_date, current_weekday = self._prompt_time_context(user_id)

            prompt = self.DIARY_MOOD_PROMPT_TEMPLATE.format(
                current_date=current_date, current_weekday=current_weekday, text=text
            )
            messages = [
                {"role": "system", "content": "你是日记情绪分析助手，只返回JSON。"},
                {"role": "user", "content": prompt},
            ]
            response = await self._call_llm(messages, temperature=0.1)
            if not response:
                return analyze_diary_mood_rule(text)

            parsed = self._parse_json_object(response)
            if parsed is None:
                return analyze_diary_mood_rule(text)

            mood = str(parsed.get("mood") or "").strip().lower()
            allowed_moods = set(MOOD_ANALYSIS_CONFIG.get("allowed_moods", [])) or {
                "happy",
                "sad",
                "calm",
                "excited",
                "angry",
            }
            if mood not in allowed_moods:
                return analyze_diary_mood_rule(text)

            raw_score = parsed.get("mood_score")
            if raw_score is None:
                score = None
            else:
                try:
                    score = int(raw_score)
                except (TypeError, ValueError):
                    score = None

            if score is None:
                return mood, None
            return mood, min(10, max(1, score))
        except Exception as exc:
            self._record_degraded_error(exc, component="pendo.ai_parser.diary_mood")
            return analyze_diary_mood_rule(text)

    def _build_event_result(
        self,
        parsed: dict[str, Any],
        source_text: str,
        user_id: str,
        *,
        partial: bool,
    ) -> dict[str, Any]:
        """将 AI/规则解析结果统一整理为 event 字段。"""
        result = self._build_event_base_fields(parsed, source_text, user_id, partial=partial)
        result.update(self._normalize_event_datetimes(parsed))

        for field in ("location", "rrule", "notes"):
            if value := parsed.get(field):
                result[field] = str(value)

        offsets = self._normalize_offset_list(parsed.get("remind_offsets"))
        milestones = self._normalize_milestones(parsed.get("milestones"))
        self._apply_event_milestones(result, milestones, offsets, user_id)
        self._apply_event_reminders(result, parsed, offsets, user_id)
        return result

    @staticmethod
    def _build_event_base_fields(
        parsed: dict[str, Any],
        source_text: str,
        user_id: str,
        *,
        partial: bool,
    ) -> dict[str, Any]:
        """构造事件身份字段及完整/局部解析各自的文本字段。"""
        result: dict[str, Any] = {
            "type": "event",
            "owner_id": user_id,
            "parse_source": parsed.get("parse_source", "ai"),
        }
        if partial:
            title = str(parsed.get("title") or "").strip()
            if title:
                result["title"] = title

            category = str(parsed.get("category") or "").strip()
            if category and category != "未分类":
                result["category"] = category

            content = parsed.get("content")
            if content not in (None, "", source_text):
                result["content"] = str(content)
        else:
            result.update(
                {
                    "title": parsed.get("title", source_text[:50]) or source_text[:50],
                    "content": source_text,
                    "category": parsed.get("category") or "未分类",
                }
            )
        return result

    @staticmethod
    def _normalize_event_datetimes(parsed: dict[str, Any]) -> dict[str, str]:
        """把 LLM 返回的起止时间规范成 ISO 字符串。"""
        normalized: dict[str, str] = {}
        for field in ("start_time", "end_time"):
            if parsed.get(field):
                try:
                    dt = parser.parse(str(parsed[field]))
                    normalized[field] = dt.isoformat()
                except (ValueError, TypeError):
                    continue
        return normalized

    @staticmethod
    def _normalize_offset_list(value: Any) -> list[str]:
        """只接受 LLM 返回的非空偏移字符串列表。"""
        if not isinstance(value, list):
            return []
        return [text for raw in value if (text := str(raw or "").strip())]

    @staticmethod
    def _normalize_milestones(value: Any) -> list[dict[str, str]]:
        """过滤缺字段或时间无效的多节点事件。"""
        if not isinstance(value, list):
            return []
        milestones: list[dict[str, str]] = []
        for raw in value:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name") or "").strip()
            raw_time = raw.get("time")
            if not name or not raw_time:
                continue
            try:
                milestone_time = parser.parse(str(raw_time)).isoformat()
            except (ValueError, TypeError):
                continue
            milestones.append({"name": name, "time": milestone_time})
        return milestones

    def _apply_event_milestones(
        self,
        result: dict[str, Any],
        milestones: list[dict[str, str]],
        offsets: list[str],
        user_id: str,
    ) -> None:
        """把多节点事件或误判的单节点事件映射到兼容返回结构。"""
        if len(milestones) >= 2:
            result["milestones"] = milestones
            result["start_time"] = milestones[0]["time"]
            result["end_time"] = milestones[-1]["time"]
            if offsets:
                result["remind_times"] = self.build_remind_times_for_milestones(
                    milestones,
                    offsets,
                    user_id=user_id,
                )
        elif len(milestones) == 1 and not result.get("start_time"):
            result["start_time"] = milestones[0]["time"]

    def _apply_event_reminders(
        self,
        result: dict[str, Any],
        parsed: dict[str, Any],
        offsets: list[str],
        user_id: str,
    ) -> None:
        """保留规则解析的绝对提醒，并为 AI 偏移量生成时间和语义规则。"""
        explicit_times = parse_remind_times(parsed.get("remind_times"))
        if explicit_times and not result.get("remind_times"):
            result["remind_times"] = explicit_times

        if not result.get("remind_times") and offsets and result.get("start_time"):
            result["remind_times"] = self.build_remind_times_from_offsets(
                str(result["start_time"]),
                offsets,
                user_id=user_id,
            )

        if offsets:
            result["reminder_rules"] = self.build_reminder_rules_from_offsets(offsets)

    @staticmethod
    def _parse_json_object(response: str) -> dict[str, Any] | None:
        """从纯 JSON、代码块或带说明文字的响应中提取第一个 JSON 对象。"""
        if not response:
            return None

        text = response.strip()
        code_block = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
        if code_block:
            text = code_block.group(1).strip()

        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            decoder = json.JSONDecoder()
            for match in re.finditer(r"\{", text):
                try:
                    parsed, _end = decoder.raw_decode(text[match.start() :])
                except (json.JSONDecodeError, ValueError):
                    continue
                if isinstance(parsed, dict):
                    return cast(dict[str, Any], parsed)
            return None
        return cast(dict[str, Any], parsed) if isinstance(parsed, dict) else None

    def build_remind_times_from_offsets(
        self,
        start_time: str,
        offsets: list[str],
        user_id: str | None = None,
    ) -> list[str]:
        """根据偏移量构建提醒时间"""
        if not start_time or not offsets:
            return []

        start_dt = parse_and_localize(start_time, user_id, self.db)
        now = now_in_timezone(user_id, self.db) if user_id else self._now(start_dt.tzinfo)
        remind_times: set[str] = set()

        for offset in offsets:
            delta = self._parse_offset(offset)
            if delta:
                remind_time = start_dt - delta
                if remind_time > now:
                    remind_times.add(remind_time.isoformat())

        return sorted(remind_times)

    def build_reminder_rules_from_offsets(self, offsets: list[str]) -> list[dict[str, int]]:
        """把自然语言偏移量转成去重后的语义提醒规则。"""
        if not offsets:
            return []

        rules: list[dict[str, int]] = []
        for offset in offsets:
            delta = self._parse_offset(offset)
            if delta:
                rules.append({"offset_seconds": int(delta.total_seconds())})
        if rules:
            rules.append({"offset_seconds": 0})
        return normalize_reminder_rules(rules)

    def build_remind_times_for_milestones(
        self,
        milestones: list[dict[str, Any]],
        offsets: list[str],
        user_id: str | None = None,
    ) -> list[str]:
        """为多个里程碑的每个时间节点应用提醒偏移，返回去重后的平铺列表"""
        all_times: set[str] = set()
        for milestone in milestones:
            t = milestone.get("time")
            if not t:
                continue
            times = self.build_remind_times_from_offsets(str(t), offsets, user_id=user_id)
            all_times.update(times)
        return sorted(all_times)

    def build_reminder_rules_from_description(self, description: str) -> list[dict[str, int]]:
        """从自然语言提醒描述中提取语义规则。"""
        offsets = ["".join(match) for match in REMINDER_DESCRIPTION_RE.findall(description)]
        if offsets:
            return self.build_reminder_rules_from_offsets(offsets)
        if any(token in str(description) for token in ("准时", "开始时", "到点")):
            return [{"offset_seconds": 0}]
        return []

    def _parse_offset(self, offset: str) -> timedelta | None:
        """解析偏移量字符串"""
        match = OFFSET_TOKEN_RE.search(str(offset))
        if not match:
            return None

        num = self._parse_chinese_number(match.group(1))
        if num is None:
            return None
        return timedelta(seconds=num * OFFSET_UNIT_SECONDS[match.group(2)])

    @classmethod
    def _parse_chinese_number(cls, text: str) -> float | None:
        """解析中文数字

        支持格式：
        - 基本数字：一、二、三...九、十
        - 组合数字：十一、二十、一百、一百二十三等
        - 特殊数字：半(0.5)、两(2)
        """
        if not text:
            return None
        if text == "半":
            return 0.5

        arabic_number = parse_int(text, minimum=0)
        if arabic_number is not None:
            return float(arabic_number)

        if "百" in text:
            left, remaining = text.split("百", 1)
            hundreds = CHINESE_DIGITS.get(left, 1) if left else 1
            remaining = remaining.lstrip("零")
            remainder = cls._parse_chinese_number(remaining) if remaining else 0.0
            return None if remainder is None else hundreds * 100.0 + remainder

        if "十" in text:
            left, right = text.split("十", 1)
            tens = CHINESE_DIGITS.get(left, 1) if left else 1
            if right and right not in CHINESE_DIGITS:
                return None
            return float(tens * 10 + CHINESE_DIGITS.get(right, 0))

        if text in CHINESE_DIGITS:
            return float(CHINESE_DIGITS[text])

        return None
