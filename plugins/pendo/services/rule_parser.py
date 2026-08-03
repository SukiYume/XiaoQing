"""基于关键词和正则的本地自然语言解析器，作为 AI 解析的降级路径。"""

import re
from datetime import datetime, timedelta
from typing import Any, Final

from ..models.item import ItemType
from ..utils.formatters import extract_tags
from ..utils.time_utils import TimezoneHelper

TIME_KEYWORDS: Final[tuple[tuple[str, int], ...]] = (
    # 长词必须在短词前，避免“下下周”先命中“下周”。
    ("下下周", 14),
    ("今天", 0),
    ("明天", 1),
    ("后天", 2),
    ("今晚", 0),
    ("明晚", 1),
    ("这周", 0),
    ("下周", 7),
)
PRIORITY_KEYWORDS: Final[tuple[tuple[str, int], ...]] = (
    ("高优先级", 2),
    ("低优先级", 4),
    ("紧急", 1),
    ("重要", 2),
    ("普通", 3),
    ("低", 4),
)
REPEAT_KEYWORDS: Final[tuple[tuple[str, str], ...]] = (
    ("每天", "DAILY"),
    ("每周", "WEEKLY"),
    ("每个月", "MONTHLY"),
    ("每月", "MONTHLY"),
)
NOTE_KEYWORDS: Final = ("想法", "灵感", "点子", "记录", "笔记", "idea")
EVENT_KEYWORDS: Final = ("会议", "开会", "约", "见面", "活动", "聚会", "上课")
TASK_KEYWORDS: Final = ("待办", "任务", "完成", "提交", "截止", "deadline", "todo")
DEADLINE_HINTS: Final = ("到", "截止", "之前", "前")
TIME_EXPRESSION_PATTERNS: Final = (
    r"\d{1,2}[点:：]",
    r"今天|明天|后天",
    r"\d{4}-\d{1,2}-\d{1,2}",
    r"下周|这周",
)
LOCATION_PATTERNS: Final = (
    r"在([^，,。.！!？?]+?)(开会|见面|会议)",
    r"地点[：:]\s*([^，,。.！!？?]+)",
)
REMINDER_UNIT_SECONDS: Final = {"分钟": 60, "小时": 3600, "天": 86400}
WEEKDAY_CODES: Final[tuple[tuple[str, str], ...]] = (
    ("周一", "MO"),
    ("周二", "TU"),
    ("周三", "WE"),
    ("周四", "TH"),
    ("周五", "FR"),
    ("周六", "SA"),
    ("周日", "SU"),
)
CATEGORY_KEYWORDS: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("健康", ("体检", "锻炼", "跑步", "健身", "运动", "医院")),
    ("工作", ("工作", "会议", "项目", "报告", "开会", "邮件", "周报")),
    ("学习", ("学习", "课程", "作业", "论文", "考试", "阅读")),
    ("生活", ("购物", "家务", "做饭", "买菜")),
    ("财务", ("理财", "投资", "报销", "账单", "还款")),
)


class RuleParser:
    """把常见中文时间、类型和元数据表达解析为条目字段。"""

    def parse(self, text: str, user_id: str, *, now: datetime | None = None) -> dict[str, Any]:
        """解析自然语言，返回结构化数据"""
        item_type = self._detect_type(text)
        result: dict[str, Any] = {
            "type": item_type,
            "title": text[:50],
            "content": text,
            "tags": extract_tags(text),
            "category": self._extract_category(text) or "未分类",
            "owner_id": user_id,
            "needs_confirmation": [],
        }

        current_time = now or TimezoneHelper.now()
        if current_time.tzinfo is None:
            raise ValueError("rule-parser now must be timezone-aware")
        time_info = self._extract_time(text, current_time)
        self._apply_time_fields(result, item_type, time_info)
        self._apply_type_metadata(result, item_type, text)

        remind_times = self._extract_reminders(text, time_info, current_time)
        if remind_times and item_type in {ItemType.EVENT, ItemType.TASK}:
            result["remind_times"] = remind_times

        self._append_missing_fields(result, item_type)
        return result

    @staticmethod
    def _apply_time_fields(
        result: dict[str, Any],
        item_type: ItemType,
        time_info: dict[str, str] | None,
    ) -> None:
        """把通用时间解析结果映射到事件或待办字段。"""
        if not time_info:
            return
        if item_type == ItemType.EVENT:
            for field in ("start_time", "end_time"):
                if value := time_info.get(field):
                    result[field] = value
            return
        if item_type == ItemType.TASK:
            deadline_at = time_info.get("deadline_at")
            start_time = time_info.get("start_time")
            if deadline_at:
                result["deadline_at"] = deadline_at
                result["plan_date"] = deadline_at[:10]
            elif start_time:
                result["plan_date"] = start_time[:10]

    def _apply_type_metadata(self, result: dict[str, Any], item_type: ItemType, text: str) -> None:
        """提取只属于事件或待办的元数据。"""
        if item_type == ItemType.EVENT:
            if location := self._extract_location(text):
                result["location"] = location
            if rrule := self._extract_rrule(text):
                result["rrule"] = rrule
        elif item_type == ItemType.TASK:
            if priority := self._extract_priority(text):
                result["priority"] = priority

    @staticmethod
    def _append_missing_fields(result: dict[str, Any], item_type: ItemType) -> None:
        """记录后续交互仍需用户补充的关键字段。"""
        missing = result["needs_confirmation"]
        if item_type == ItemType.EVENT and not result.get("start_time"):
            missing.append("start_time")
        if item_type == ItemType.TASK and not result.get("plan_date"):
            missing.append("plan_date")

    def _detect_type(self, text: str) -> ItemType:
        """检测条目类型"""
        text_lower = text.lower()
        if any(keyword in text_lower for keyword in NOTE_KEYWORDS):
            return ItemType.NOTE
        if any(keyword in text for keyword, _frequency in REPEAT_KEYWORDS):
            return ItemType.EVENT
        if any(keyword in text for keyword in EVENT_KEYWORDS):
            return ItemType.EVENT
        if any(keyword in text_lower for keyword in TASK_KEYWORDS):
            return ItemType.TASK
        if self._has_time_expression(text):
            return ItemType.TASK if any(hint in text for hint in DEADLINE_HINTS) else ItemType.EVENT
        return ItemType.NOTE

    def _extract_time(self, text: str, now: datetime) -> dict[str, str] | None:
        """提取时间信息"""
        result = self._extract_relative_time(text, now)
        if absolute_start := self._extract_absolute_start(text, now):
            result["start_time"] = absolute_start

        if ("截止" in text or "deadline" in text.lower()) and "start_time" in result:
            result["deadline_at"] = result.pop("start_time")
            result.pop("end_time", None)
        return result or None

    def _extract_relative_time(self, text: str, now: datetime) -> dict[str, str]:
        """解析今天、明天、下周等相对日期。"""
        for keyword, days_offset in TIME_KEYWORDS:
            if keyword not in text:
                continue
            target_date = now + timedelta(days=days_offset)
            start_match = re.search(r"(\d{1,2})[点:：](\d{1,2})?", text)
            start_time = self._clock_on_date(target_date, start_match) or target_date.replace(
                hour=9, minute=0, second=0, microsecond=0
            )
            result = {"start_time": start_time.isoformat()}
            end_match = re.search(r"到\s*(\d{1,2})[点:：](\d{1,2})?", text)
            if end_time := self._clock_on_date(target_date, end_match):
                result["end_time"] = end_time.isoformat()
            return result
        return {}

    def _extract_absolute_start(self, text: str, now: datetime) -> str | None:
        """解析 YYYY-MM-DD 及其可选时分。"""
        date_match = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
        if date_match is None:
            return None
        year, month, day = map(int, date_match.groups())
        try:
            target_date = datetime(year, month, day, tzinfo=now.tzinfo)
        except ValueError:
            return None
        time_match = re.search(r"(\d{1,2})[点:：](\d{1,2})?", text)
        return (self._clock_on_date(target_date, time_match) or target_date).isoformat()

    @staticmethod
    def _clock_on_date(base: datetime, match: re.Match[str] | None) -> datetime | None:
        """把合法的时分匹配应用到指定日期，非法时钟返回 ``None``。"""
        if match is None:
            return None
        hour = int(match.group(1))
        minute = int(match.group(2) or 0)
        if not 0 <= hour < 24 or not 0 <= minute < 60:
            return None
        return base.replace(hour=hour, minute=minute, second=0, microsecond=0)

    def _has_time_expression(self, text: str) -> bool:
        """检查是否包含时间表达"""
        return any(re.search(pattern, text) for pattern in TIME_EXPRESSION_PATTERNS)

    def _extract_location(self, text: str) -> str | None:
        """提取地点"""
        for pattern in LOCATION_PATTERNS:
            match = re.search(pattern, text)
            if match:
                return match.group(1).strip()
        return None

    def _extract_rrule(self, text: str) -> str | None:
        """提取重复规则"""
        for keyword, freq in REPEAT_KEYWORDS:
            if keyword in text:
                rrule_parts = [f"FREQ={freq}"]

                # 提取重复次数
                count_match = re.search(r"重复(\d+)(个月|周|天|次)", text)
                if count_match:
                    rrule_parts.append(f"COUNT={count_match.group(1)}")

                # 提取每月日期
                if freq == "MONTHLY":
                    day_match = re.search(r"(\d{1,2})[号日]", text)
                    if day_match:
                        rrule_parts.append(f"BYMONTHDAY={day_match.group(1)}")

                # 提取星期几
                if freq == "WEEKLY":
                    for cn_day, en_day in WEEKDAY_CODES:
                        if cn_day in text:
                            rrule_parts.append(f"BYDAY={en_day}")
                            break

                return ";".join(rrule_parts)
        return None

    def _extract_priority(self, text: str) -> int | None:
        """提取优先级"""
        text_lower = text.lower()
        for keyword, priority in PRIORITY_KEYWORDS:
            if keyword in text_lower:
                return priority
        return None

    def _extract_reminders(
        self, text: str, time_info: dict[str, str] | None, now: datetime
    ) -> list[str]:
        """提取提醒时间"""
        if not time_info or "start_time" not in time_info:
            return []

        reminders: set[str] = set()
        start_time = datetime.fromisoformat(time_info["start_time"])

        patterns = [
            r"提前(\d+)(分钟|小时|天)",
            r"(\d+)(分钟|小时|天)前提醒",
        ]

        for pattern in patterns:
            for match in re.finditer(pattern, text):
                num = int(match.group(1))
                unit = match.group(2)

                remind_time = start_time - timedelta(seconds=num * REMINDER_UNIT_SECONDS[unit])

                if remind_time > now:
                    reminders.add(remind_time.isoformat())

        return sorted(reminders)

    def _extract_category(self, text: str) -> str | None:
        """提取分类"""
        for category, keywords in CATEGORY_KEYWORDS:
            if any(kw in text for kw in keywords):
                return category
        return None
