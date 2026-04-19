"""
提醒服务（精简版）
只处理event类型的提醒
"""

import logging
from typing import Any
from datetime import datetime, timedelta

from ..config import REMINDER_POLICIES, PendoConfig
from ..utils.time_utils import (
    parse_hhmm_to_minutes,
    parse_and_localize,
    now_in_timezone,
)
from ..utils.settings_utils import parse_custom_settings
from ..utils.formatters import ItemFormatter

logger = logging.getLogger(__name__)


class ReminderService:
    """提醒服务"""

    def __init__(self, db):
        self.db = db
        self.default_policies = REMINDER_POLICIES

    def calculate_remind_times(self, item_data, policy_type: str = "default") -> list[str]:
        """根据策略计算提醒时间点"""
        base_time = None
        start_time = (
            getattr(item_data, "start_time", None)
            if not isinstance(item_data, dict)
            else item_data.get("start_time")
        )
        due_time = (
            getattr(item_data, "due_time", None)
            if not isinstance(item_data, dict)
            else item_data.get("due_time")
        )
        if start_time:
            base_time = datetime.fromisoformat(start_time)
        elif due_time:
            base_time = datetime.fromisoformat(due_time)

        if not base_time:
            return []

        policy: dict[str, Any] = self.default_policies.get(
            policy_type, self.default_policies["default"]
        )
        now = datetime.now(base_time.tzinfo) if base_time.tzinfo else datetime.now()
        remind_times = []

        reminders = policy.get("reminders", [])
        for reminder in reminders:
            if not isinstance(reminder, dict):
                continue
            remind_time = base_time
            offset_days = reminder.get("offset_days")
            offset_hours = reminder.get("offset_hours")
            offset_minutes = reminder.get("offset_minutes")
            if isinstance(offset_days, (int, float)):
                remind_time += timedelta(days=offset_days)
            if isinstance(offset_hours, (int, float)):
                remind_time += timedelta(hours=offset_hours)
            if isinstance(offset_minutes, (int, float)):
                remind_time += timedelta(minutes=offset_minutes)

            if remind_time > now:
                remind_times.append(remind_time.isoformat())

        return sorted(remind_times)

    def check_and_send_reminders(self, context=None) -> dict[str, Any]:
        """检查并发送到期的提醒，包括重复未确认的提醒"""
        current_time = now_in_timezone()
        sent_count = 0
        messages = []

        try:
            # 1. 检查新到期的提醒
            items = self.db.get_all_events_with_reminders(future_hours=0)

            for item in items:
                settings = self.db.get_user_settings(item.owner_id)
                custom_settings = parse_custom_settings(settings)
                if not custom_settings.get("reminder_enabled", True):
                    continue

                remind_times = item.remind_times if hasattr(item, "remind_times") else []
                if not remind_times:
                    continue

                log_map = {}
                if hasattr(self.db, "get_reminder_logs"):
                    try:
                        log_map = {
                            log["remind_time"]: log for log in self.db.get_reminder_logs(item.id)
                        }
                    except Exception as e:
                        logger.warning("读取提醒日志失败: %s", e)

                for remind_time_str in remind_times:
                    try:
                        remind_time = parse_and_localize(remind_time_str)
                        time_diff = (current_time - remind_time).total_seconds()

                        if 0 <= time_diff <= PendoConfig.REMINDER_CHECK_WINDOW_SECONDS:
                            log = log_map.get(remind_time_str)
                            if log and log.get("confirmed_at"):
                                continue
                            if not self.db.is_reminder_sent(item.id, remind_time_str):
                                if self._should_suppress(item, remind_time):
                                    continue
                                message = self._build_reminder_message(item, remind_time_str)
                                messages.append(self._make_msg(item, message, remind_time_str))
                                sent_count += 1
                    except Exception as e:
                        logger.warning("处理提醒失败: %s, error: %s", remind_time_str, e)

            # 2. 重复发送未确认的提醒
            repeat_messages = self._check_unconfirmed_repeats(current_time)
            messages.extend(repeat_messages)
            sent_count += len(repeat_messages)

        except Exception as e:
            logger.exception("检查提醒时出错: %s", e)

        return {
            "status": "success",
            "sent": sent_count,
            "messages": messages,
            "message": f"发送了{sent_count}条提醒",
        }

    def _check_unconfirmed_repeats(self, current_time) -> list[dict[str, Any]]:
        """检查未确认的提醒，按间隔重复发送

        每个 (item_id, remind_time) 在 DB 中只有一行，直接读取 repeat_count 和 last_sent_at。
        """
        messages = []
        repeat_interval = PendoConfig.REMINDER_REPEAT_INTERVAL_SECONDS
        max_repeats = PendoConfig.REMINDER_MAX_REPEATS
        auto_confirm_after = PendoConfig.REMINDER_AUTO_CONFIRM_AFTER_FINAL_SEND_SECONDS

        try:
            for log in self.db.get_unconfirmed_sent_reminders():
                item_id = log["item_id"]
                remind_time_str = log["remind_time"]
                repeat_count = log["repeat_count"]
                last_sent_at = parse_and_localize(log["last_sent_at"])
                seconds_since_last = (current_time - last_sent_at).total_seconds()

                # 已达最大重复次数：超时后自动确认
                if repeat_count >= max_repeats + 1:
                    if seconds_since_last >= auto_confirm_after:
                        self.db.confirm_reminder(
                            item_id, user_action="auto_confirmed", remind_time=remind_time_str,
                        )
                    continue

                # 判断是否到了下一次重发的时间窗口
                if not (repeat_interval <= seconds_since_last
                        <= repeat_interval + PendoConfig.REMINDER_CHECK_WINDOW_SECONDS):
                    continue

                item = self.db.get_item(item_id)
                if not item:
                    continue

                if self._should_suppress(item, current_time):
                    continue

                message = self._build_reminder_message(item, remind_time_str, repeat_count=repeat_count)
                messages.append(self._make_msg(item, message, remind_time_str))

        except Exception as e:
            logger.warning("检查未确认提醒重复时出错: %s", e)

        return messages

    def _should_suppress(self, item, check_time: datetime) -> bool:
        """判断是否应抑制发送（静默时间 + 非重要事件）"""
        if not self._is_in_quiet_hours(item.owner_id, check_time):
            return False
        if self._is_important_item(item):
            return False
        # 若事件本身也在静默时间内（如早晨6点的事件），仍发送
        start_time = getattr(item, "start_time", None)
        if start_time and self._is_in_quiet_hours(item.owner_id, parse_and_localize(start_time)):
            return False
        return True

    @staticmethod
    def _make_msg(item, message: str, remind_time_str: str) -> dict[str, Any]:
        """构建统一的消息字典"""
        return {
            "user_id": item.owner_id,
            "group_id": item.context.get("group_id") if isinstance(item.context, dict) else None,
            "message": message,
            "item_id": item.id,
            "remind_time": remind_time_str,
        }

    def confirm_reminder(
        self,
        item_id: str,
        user_action: str = "confirmed",
        owner_id: str | None = None,
        remind_time: str | None = None,
        allow_future: bool = False,
    ) -> dict[str, Any]:
        """用户确认提醒"""
        return self.db.confirm_reminder(
            item_id,
            user_action,
            owner_id=owner_id,
            remind_time=remind_time,
            allow_future=allow_future,
        )

    def get_pending_reminders(self, user_id: str, hours: int = 24) -> list[dict[str, Any]]:
        """获取未来N小时内的待发送提醒"""
        now = datetime.now()
        future = now + timedelta(hours=hours)
        pending = []

        items = self.db.get_items(user_id, filters={"type": "event"})

        for item in items:
            remind_times = item.remind_times or []
            for remind_time_str in remind_times:
                try:
                    remind_time = datetime.fromisoformat(remind_time_str)
                    if now <= remind_time <= future:
                        pending.append(
                            {
                                "item_id": item.id,
                                "title": item.title,
                                "type": "event",
                                "remind_time": remind_time_str,
                            }
                        )
                except (ValueError, TypeError):
                    # 无效的时间格式，跳过此提醒
                    pass

        return sorted(pending, key=lambda x: x["remind_time"])

    def detect_conflict(
        self, user_id: str, start_time: str, end_time: str | None = None
    ) -> list[dict[str, Any]]:
        """检测日程冲突"""
        start_dt = datetime.fromisoformat(start_time).replace(tzinfo=None)
        end_dt = (
            datetime.fromisoformat(end_time).replace(tzinfo=None)
            if end_time
            else start_dt + timedelta(hours=1)
        )

        # S-5修复：使用区间重叠条件（start_time <= end_dt AND end_time >= start_dt）
        # 原来只过滤 start_time >= start_dt - 1day，会漏掉 start_time 更早但 end_time
        # 延伸到查询窗口内的长跨度事件。只设 end_date 上界让 SQL 过滤
        # start_time > end_dt 的远未来事件，不设 start_date 下界，
        # 由后续 Python 层的区间重叠判断过滤掉不相交的早期事件。
        items = self.db.get_items(
            user_id,
            filters={
                "type": "event",
                "date_field": "start_time",
                "end_date": end_dt.isoformat(),  # start_time <= end_dt
            },
            limit=1000,
        )
        conflicts = []

        for item in items:
            if not item.start_time:
                continue

            milestones = getattr(item, "milestones", None) or []
            if milestones:
                # 多节点事件：逐个里程碑做点冲突检测（各节点视为1小时事件）
                # 避免用整段 start_time~end_time 误判长跨度事件
                overlaps = False
                for m in milestones:
                    m_time = m.get("time", "")
                    if not m_time:
                        continue
                    try:
                        m_start = datetime.fromisoformat(m_time).replace(tzinfo=None)
                        m_end = m_start + timedelta(hours=1)
                        if not (end_dt <= m_start or start_dt >= m_end):
                            overlaps = True
                            break
                    except (ValueError, TypeError):
                        continue
                if overlaps:
                    conflicts.append(
                        {
                            "id": item.id,
                            "title": item.title,
                            "start_time": item.start_time,
                            "end_time": item.end_time,
                        }
                    )
            else:
                item_start = datetime.fromisoformat(item.start_time).replace(tzinfo=None)
                item_end = (
                    datetime.fromisoformat(item.end_time).replace(tzinfo=None)
                    if item.end_time
                    else item_start + timedelta(hours=1)
                )

                # 区间重叠：item 与 [start_dt, end_dt) 有交叉
                if not (end_dt <= item_start or start_dt >= item_end):
                    conflicts.append(
                        {
                            "id": item.id,
                            "title": item.title,
                            "start_time": item.start_time,
                            "end_time": item.end_time,
                        }
                    )

        return conflicts

    def _build_reminder_message(
        self, item, remind_time: str, repeat_count: int | None = None
    ) -> str:
        """构建提醒消息，支持里程碑事件和重复提醒

        Args:
            item: 事件条目
            remind_time: 提醒时间字符串
            repeat_count: 当前是第几次重复提醒（首次为 None）
        """
        title = item.title or "无标题"
        if repeat_count is not None:
            max_repeats = PendoConfig.REMINDER_MAX_REPEATS
            header = f"⏰ **提醒 (第{repeat_count + 1}次，共{max_repeats + 1}次)**"
        else:
            header = "⏰ **提醒**"
        lines = [header, f"🗓️ {title}"]

        milestones = getattr(item, "milestones", None) or []
        target_time = getattr(item, "start_time", None)
        if milestones:
            milestone = self._find_closest_milestone_info(milestones, remind_time)
            if milestone:
                milestone_name = milestone.get("name", "")
                target_time = milestone.get("time") or target_time
                if milestone_name:
                    lines.append(f"📌 {milestone_name}")
                if target_time:
                    milestone_dt = ItemFormatter.format_datetime(target_time, "%m月%d日 %H:%M")
                    lines.append(f"🎯 节点时间: {milestone_dt}")
            elif target_time:
                dt_str = ItemFormatter.format_datetime(target_time, "%m月%d日 %H:%M")
                lines.append(f"🎯 事件时间: {dt_str}")
        else:
            if target_time:
                dt_str = ItemFormatter.format_datetime(target_time, "%m月%d日 %H:%M")
                lines.append(f"🎯 事件时间: {dt_str}")

        reminder_slot = self._format_reminder_slot(remind_time, target_time)
        if reminder_slot:
            lines.append(f"🔔 对应提醒点: {reminder_slot}")

        if item.location:
            lines.append(f"📍 {item.location}")

        notes = getattr(item, "notes", None)
        if notes:
            lines.append(f"📝 {notes}")

        if repeat_count is None and getattr(item, "parent_id", None):
            lines.append("🔄 重复日程")

        lines.append(f"\n/pendo confirm {item.id}")
        lines.append(f"/pendo snooze {item.id} 10m")

        return "\n".join(lines)

    def _find_closest_milestone_info(
        self, milestones: list[dict[str, Any]], remind_time: str
    ) -> dict[str, Any] | None:
        """根据 remind_time 找到时间最近（且在其后）的里程碑"""
        try:
            remind_dt = datetime.fromisoformat(remind_time)
            best = None
            best_diff = None
            for m in milestones:
                try:
                    m_dt = datetime.fromisoformat(m.get("time", ""))
                    diff = (m_dt - remind_dt).total_seconds()
                    if diff >= 0 and (best_diff is None or diff < best_diff):
                        best = m
                        best_diff = diff
                except (ValueError, TypeError):
                    continue
            return best or (milestones[0] if milestones else None)
        except (ValueError, TypeError):
            return milestones[0] if milestones else None

    def _format_reminder_slot(self, remind_time: str, target_time: str | None) -> str:
        """格式化提醒点，展示相对目标时间的偏移和原始提醒时间。"""
        remind_dt_str = ItemFormatter.format_datetime(remind_time, "%m月%d日 %H:%M")
        if not target_time:
            return remind_dt_str

        try:
            remind_dt = datetime.fromisoformat(remind_time)
            target_dt = datetime.fromisoformat(target_time)
        except (ValueError, TypeError):
            return remind_dt_str

        diff_seconds = int(round((target_dt - remind_dt).total_seconds()))
        if diff_seconds > 0:
            relation = f"提前{self._format_duration(abs(diff_seconds))}"
        elif diff_seconds < 0:
            relation = f"晚于目标{self._format_duration(abs(diff_seconds))}"
        else:
            relation = "准时"

        return f"{relation}（{remind_dt_str}）"

    @staticmethod
    def _format_duration(total_seconds: int) -> str:
        """格式化秒数为中文时长。"""
        total_seconds = max(0, int(total_seconds))
        days, remainder = divmod(total_seconds, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, seconds = divmod(remainder, 60)

        parts = []
        if days:
            parts.append(f"{days}天")
        if hours:
            parts.append(f"{hours}小时")
        if minutes:
            parts.append(f"{minutes}分钟")
        if not parts:
            parts.append(f"{seconds}秒" if seconds else "0分钟")
        return "".join(parts)

    def _is_in_quiet_hours(self, user_id: str, remind_time: datetime) -> bool:
        """检查是否在静默时段"""
        try:
            settings = self.db.get_user_settings(user_id)
            start_minutes = parse_hhmm_to_minutes(settings.get("quiet_hours_start", "23:00"))
            end_minutes = parse_hhmm_to_minutes(settings.get("quiet_hours_end", "07:00"))

            if start_minutes is None or end_minutes is None:
                return False

            current = remind_time.hour * 60 + remind_time.minute

            if start_minutes > end_minutes:
                return current >= start_minutes or current < end_minutes
            return start_minutes <= current < end_minutes
        except (AttributeError, KeyError, ValueError):
            # 设置获取失败或数据格式错误
            return False

    def _is_important_item(self, item) -> bool:
        """判断是否是重要事件

        优先级: 1=紧急 2=高 3=中 4=低
        """
        priority = getattr(item, "priority", None)
        if priority is not None and isinstance(priority, (int, float)) and priority <= 2:
            return True

        tags = item.tags if hasattr(item, "tags") and item.tags else []
        if any(tag in ["重要", "紧急", "important", "urgent"] for tag in tags):
            return True

        title = item.title or ""
        if any(kw in title for kw in ["重要", "紧急", "会议", "deadline", "截止"]):
            return True

        return False
