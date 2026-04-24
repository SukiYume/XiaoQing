"""
AI自然语言解析服务
使用LLM进行自然语言理解，失败时降级到规则解析
"""

import re
import json
import logging
import time
from typing import Any, Optional
from datetime import datetime, timedelta
from dateutil import parser
from collections import defaultdict
from .rule_parser import RuleParser
from ..config import MOOD_ANALYSIS_CONFIG
from ..utils.time_utils import now_in_timezone, parse_and_localize
from ..utils.validators import normalize_reminder_rules

logger = logging.getLogger(__name__)


class RateLimiter:
    """简单的速率限制器"""

    def __init__(self, max_calls: int = 10, time_window: int = 60):
        """初始化速率限制器

        Args:
            max_calls: 时间窗口内最大调用次数
            time_window: 时间窗口（秒）
        """
        self.max_calls = max_calls
        self.time_window = time_window
        self.call_history = defaultdict(list)

    def check_rate_limit(self, user_id: str) -> tuple[bool, int]:
        """检查是否超过速率限制

        Args:
            user_id: 用户ID

        Returns:
            (是否允许调用, 剩余秒数)
        """
        now = time.time()
        history = self.call_history[user_id]

        # 移除超出时间窗口的记录
        history[:] = [t for t in history if now - t < self.time_window]

        if len(history) >= self.max_calls:
            oldest = history[0]
            remaining = int(oldest + self.time_window - now)
            return False, max(remaining, 0)

        history.append(now)
        return True, 0

    def reset(self, user_id: str | None = None):
        """重置速率限制

        Args:
            user_id: 用户ID，如果为None则重置所有用户
        """
        if user_id:
            self.call_history.pop(user_id, None)
        else:
            self.call_history.clear()


class AIParser:
    """AI解析器 - 专注于Event类型解析"""

    # 全局速率限制器实例
    _rate_limiter = RateLimiter(max_calls=20, time_window=60)

    # Event专用prompt模板
    PARSE_PROMPT_TEMPLATE = """解析日程信息，提取时间、地点、提醒等字段。

当前时间: {current_date} ({current_weekday})
用户输入: {text}

返回JSON:
{{
  "title": "简洁标题",
  "start_time": "YYYY-MM-DDTHH:MM:SS或null（有milestones时留null）",
  "end_time": "YYYY-MM-DDTHH:MM:SS或null（有milestones时留null）",
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
- 若用户描述多个具名时间点(如截止、开始、结束、里程碑等事件节点)，填milestones列表，start_time/end_time留null
- "提前X天/周/小时提醒"是提醒偏移量，必须放入remind_offsets，绝对不能作为milestones节点
- 普通单次事件milestones留空列表[]
- 重复事件: 设置rrule，milestones必须留空列表[]，start_time设为第一次发生的时间
- 重复: 每天→FREQ=DAILY, 每周→FREQ=WEEKLY, 每月X号→FREQ=MONTHLY;BYMONTHDAY=X
- 重复N次→添加;COUNT=N
- 提醒支持: 分钟/小时/天/周
- notes提取用户标注为"备注"的内容(URL、说明等)

仅返回JSON。"""

    DIARY_MOOD_PROMPT_TEMPLATE = """判断这篇日记的主情绪。

当前时间: {current_date} ({current_weekday})
日记内容: {text}

返回 JSON:
{{
  "mood": "happy|sad|calm|excited|angry",
  "mood_score": 1-10
}}

规则:
- 只允许以上 5 种 mood
- 即使内容偏平淡，也应优先判断为 calm，而不是返回空
- score 表示情绪强度，1 最弱，10 最强
- 只返回 JSON，不要解释。"""

    def __init__(self, context=None, db=None):
        self.context = context
        self.db = db
        self.rule_parser = RuleParser()

    def _get_llm_secrets(self):
        """获取LLM配置"""
        if self.context and hasattr(self.context, "secrets"):
            return self.context.secrets.get("plugins", {}).get("pendo", {})
        return {}

    async def _call_llm(
        self, messages: list[dict[str, str]], temperature: float = 0.3
    ) -> Optional[str]:
        """调用LLM API"""
        try:
            from .llm_client import chat_completions_with_fallback_paths

            secrets = self._get_llm_secrets()
            api_base = secrets.get("api_base", "")
            api_key = secrets.get("api_key", "")
            model = secrets.get("model", "")
            proxy = secrets.get("proxy", "")

            if not api_key:
                return None

            raw, _ = await chat_completions_with_fallback_paths(
                session=getattr(self.context, "http_session", None),
                api_base=api_base,
                api_key=api_key,
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=1000,
                timeout_seconds=30,
                max_retry=2,
                retry_interval_seconds=1,
                proxy=proxy,
            )
            return raw
        except Exception as e:
            logger.warning("LLM调用失败: %s", e)
            return None

    def parse_natural_language(self, text: str, user_id: str) -> dict[str, Any]:
        """同步规则解析（固定返回 event 类型），也用作 AI 解析的降级路径"""
        parsed = self.rule_parser.parse(text, user_id)
        parsed["parse_source"] = "rule"
        parsed["type"] = "event"
        return parsed

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
            current_now = now_in_timezone(user_id, self.db)
            current_date = current_now.strftime("%Y-%m-%d %H:%M")
            weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
            current_weekday = weekday_names[current_now.weekday()]

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

            try:
                parsed = json.loads(self._extract_json(response))
                if not isinstance(parsed, dict):
                    return self._fallback_event_result(source_text, user_id, partial=partial)
            except (json.JSONDecodeError, ValueError):
                return self._fallback_event_result(source_text, user_id, partial=partial)

            logger.info("AI解析结果: %s", json.dumps(parsed, ensure_ascii=False))

            return self._build_event_result(parsed, source_text, user_id, partial=partial)

        except Exception as e:
            logger.exception("AI解析失败: %s", e)
            return self._fallback_event_result(source_text, user_id, partial=partial)

    async def analyze_diary_mood(self, text: str, user_id: str) -> tuple[str | None, int | None]:
        """使用 AI 分析日记主情绪，失败时降级到规则情绪分析。"""
        allowed, wait_seconds = self._rate_limiter.check_rate_limit(user_id)
        if not allowed:
            logger.warning("用户 %s 超过AI情绪分析速率限制，等待 %s 秒", user_id, wait_seconds)
            return self._fallback_diary_mood(text)

        try:
            current_date = datetime.now().strftime("%Y-%m-%d %H:%M")
            weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
            current_weekday = weekday_names[datetime.now().weekday()]

            prompt = self.DIARY_MOOD_PROMPT_TEMPLATE.format(
                current_date=current_date, current_weekday=current_weekday, text=text
            )
            messages = [
                {"role": "system", "content": "你是日记情绪分析助手，只返回JSON。"},
                {"role": "user", "content": prompt},
            ]
            response = await self._call_llm(messages, temperature=0.1)
            if not response:
                return self._fallback_diary_mood(text)

            try:
                parsed = json.loads(self._extract_json(response))
            except (json.JSONDecodeError, ValueError):
                return self._fallback_diary_mood(text)

            if not isinstance(parsed, dict):
                return self._fallback_diary_mood(text)

            mood = str(parsed.get("mood") or "").strip().lower()
            if mood not in {"happy", "sad", "calm", "excited", "angry"}:
                return self._fallback_diary_mood(text)

            raw_score = parsed.get("mood_score")
            try:
                score = int(raw_score)
            except (TypeError, ValueError):
                score = None

            if score is None:
                return mood, None
            return mood, min(10, max(1, score))
        except Exception as e:
            logger.exception("AI 日记情绪分析失败: %s", e)
            return self._fallback_diary_mood(text)

    def _build_event_result(
        self,
        parsed: dict[str, Any],
        source_text: str,
        user_id: str,
        *,
        partial: bool,
    ) -> dict[str, Any]:
        """将 AI/规则解析结果统一整理为 event 字段。"""
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
                    "category": parsed.get("category", "未分类"),
                }
            )

        # 时间字段
        for field in ["start_time", "end_time"]:
            if parsed.get(field):
                try:
                    dt = parser.parse(str(parsed[field]))
                    result[field] = dt.isoformat()
                except (ValueError, TypeError):
                    pass

        # 地点和重复规则
        if parsed.get("location"):
            result["location"] = parsed["location"]
        if parsed.get("rrule"):
            result["rrule"] = parsed["rrule"]

        # milestones（多时间节点事件）
        raw_milestones = parsed.get("milestones")
        if raw_milestones and isinstance(raw_milestones, list) and len(raw_milestones) >= 2:
            valid_milestones = []
            for m in raw_milestones:
                if isinstance(m, dict) and m.get("name") and m.get("time"):
                    try:
                        dt = parser.parse(str(m["time"]))
                        valid_milestones.append({"name": m["name"], "time": dt.isoformat()})
                    except (ValueError, TypeError):
                        pass
            if len(valid_milestones) >= 2:
                result["milestones"] = valid_milestones
                result["start_time"] = valid_milestones[0]["time"]
                result["end_time"] = valid_milestones[-1]["time"]
                if parsed.get("remind_offsets"):
                    result["remind_times"] = self.build_remind_times_for_milestones(
                        valid_milestones, parsed["remind_offsets"], user_id=user_id
                    )

        # notes
        if parsed.get("notes"):
            result["notes"] = str(parsed["notes"])

        # 提醒时间（仅单次事件；多节点事件在 milestones 块中处理）
        if (
            not result.get("remind_times")
            and parsed.get("remind_offsets")
            and result.get("start_time")
        ):
            result["remind_times"] = self.build_remind_times_from_offsets(
                result["start_time"], parsed["remind_offsets"], user_id=user_id
            )

        if parsed.get("remind_offsets"):
            result["reminder_rules"] = self.build_reminder_rules_from_offsets(
                parsed["remind_offsets"]
            )

        return result

    def _extract_json(self, response: str) -> str:
        """从响应中提取JSON"""
        if not response:
            return ""

        text = response.strip()

        # 尝试提取代码块内的内容（非贪婪匹配）
        code_block = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
        if code_block:
            text = code_block.group(1).strip()

        # 尝试提取JSON对象
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if match:
            return match.group(0).strip()

        return text

    def _fallback_diary_mood(self, content: str) -> tuple[str | None, int | None]:
        """日记情绪分析的规则兜底。"""
        positive_words = MOOD_ANALYSIS_CONFIG.get("positive_words", [])
        negative_words = MOOD_ANALYSIS_CONFIG.get("negative_words", [])
        calm_words = MOOD_ANALYSIS_CONFIG.get("calm_words", [])
        excited_words = MOOD_ANALYSIS_CONFIG.get("excited_words", [])
        angry_words = MOOD_ANALYSIS_CONFIG.get("angry_words", [])
        base_scores = MOOD_ANALYSIS_CONFIG.get("base_scores", {})
        raw_increment = MOOD_ANALYSIS_CONFIG.get("score_increment", 1)
        score_increment = raw_increment if isinstance(raw_increment, int) else 1

        pos_count = sum(1 for word in positive_words if word in content)
        neg_count = sum(1 for word in negative_words if word in content)
        calm_count = sum(1 for word in calm_words if word in content)
        excited_count = sum(1 for word in excited_words if word in content)
        angry_count = sum(1 for word in angry_words if word in content)

        if excited_count > 0:
            return "excited", min(10, int(base_scores.get("excited", 8)) + excited_count + pos_count)
        if angry_count > neg_count or angry_count >= 2:
            return "angry", max(1, int(base_scores.get("angry", 3)) - angry_count)
        if pos_count > neg_count and pos_count > calm_count:
            return "happy", min(10, int(base_scores.get("happy", 6)) + pos_count * score_increment)
        if neg_count > pos_count:
            return "sad", max(1, int(base_scores.get("sad", 5)) - neg_count * score_increment)
        if calm_count > 0:
            return "calm", int(base_scores.get("calm", 5))

        return None, None

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
        now = now_in_timezone(user_id, self.db) if user_id else datetime.now(start_dt.tzinfo)
        remind_times = []

        for offset in offsets:
            delta = self._parse_offset(offset)
            if delta:
                remind_time = start_dt - delta
                if remind_time > now:
                    remind_times.append(remind_time.isoformat())

        return remind_times

    def build_reminder_rules_from_offsets(self, offsets: list[str]) -> list[dict[str, int]]:
        """Build semantic reminder rules from natural-language offsets."""
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
        all_times = set()
        for milestone in milestones:
            t = milestone.get("time")
            if not t:
                continue
            times = self.build_remind_times_from_offsets(t, offsets, user_id=user_id)
            all_times.update(times)
        return sorted(all_times)

    def build_remind_times_from_description(
        self,
        description: str,
        base_time: str,
        user_id: str | None = None,
    ) -> list[str]:
        """从自然语言描述中提取所有偏移量并生成提醒时间

        例如: "提前1天和2小时提醒" → offsets=["1天", "2小时"]
        """
        pattern = r"(?:提前\s*)?(\d+|[一二三四五六七八九十半两]+)\s*(?:个)?\s*(分钟|min|小时|hour|天|day|周|week)"
        offsets = ["".join(m) for m in re.findall(pattern, description)]
        if not offsets:
            return []
        return self.build_remind_times_from_offsets(base_time, offsets, user_id=user_id)

    def build_reminder_rules_from_description(self, description: str) -> list[dict[str, int]]:
        """Extract semantic reminder rules from a natural-language reminder description."""
        pattern = r"(?:提前\s*)?(\d+|[一二三四五六七八九十半两]+)\s*(?:个)?\s*(分钟|min|m|小时|hour|h|天|day|d|周|week|w)"
        offsets = ["".join(m) for m in re.findall(pattern, description)]
        if offsets:
            return self.build_reminder_rules_from_offsets(offsets)
        if any(token in str(description) for token in ("准时", "开始时", "到点")):
            return [{"offset_seconds": 0}]
        return []

    def _parse_offset(self, offset: str) -> Optional[timedelta]:
        """解析偏移量字符串"""
        match = re.search(
            r"(\d+|[一二三四五六七八九十半两]+)\s*(分钟|min|m|小时|hour|h|天|day|d|周|week|w)",
            str(offset),
        )
        if not match:
            return None

        num = self._parse_chinese_number(match.group(1))
        if num is None:
            try:
                num = int(match.group(1))
            except (ValueError, TypeError):
                # 无法解析为整数
                return None

        unit = match.group(2)
        if unit in ["分钟", "min", "m"]:
            return timedelta(minutes=float(num))
        elif unit in ["小时", "hour", "h"]:
            return timedelta(hours=float(num))
        elif unit in ["天", "day", "d"]:
            return timedelta(days=float(num))
        elif unit in ["周", "week", "w"]:
            return timedelta(weeks=float(num))
        return None

    def _parse_chinese_number(self, text: str) -> Optional[float]:
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
        if text == "两":
            return 2.0

        digits = {
            "零": 0,
            "一": 1,
            "二": 2,
            "三": 3,
            "四": 4,
            "五": 5,
            "六": 6,
            "七": 7,
            "八": 8,
            "九": 9,
        }

        if text.isdigit():
            return float(text)

        # 处理包含"十"的情况
        if "十" in text:
            parts = text.split("十")
            # "十" -> 10
            # "二十" -> 20
            # "二十一" -> 21
            left = digits.get(parts[0], 1) if parts[0] else 1
            right = digits.get(parts[1], 0) if len(parts) > 1 and parts[1] else 0
            return float(left * 10 + right)

        # 处理包含"百"的情况
        if "百" in text:
            parts = text.split("百")
            # "一百" -> 100
            # "一百二十" -> 120
            # "一百二十三" -> 123
            hundred_part = digits.get(parts[0], 1) if parts[0] else 1
            remaining = parts[1] if len(parts) > 1 else ""
            # 处理剩余部分（可能是"二十"、"三"等）
            if "十" in remaining:
                ten_parts = remaining.split("十")
                ten_left = digits.get(ten_parts[0], 1) if ten_parts[0] else 1
                ten_right = (
                    digits.get(ten_parts[1], 0) if len(ten_parts) > 1 and ten_parts[1] else 0
                )
                return float(hundred_part * 100 + ten_left * 10 + ten_right)
            elif remaining in digits:
                return float(hundred_part * 100 + digits[remaining])
            return float(hundred_part * 100)

        if text in digits:
            return float(digits[text])

        return None
