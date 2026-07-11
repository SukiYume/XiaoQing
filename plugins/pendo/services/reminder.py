"""
提醒服务（精简版）
只处理event类型的提醒
"""

import logging
from datetime import datetime, timedelta
from typing import Any

from ..config import REMINDER_POLICIES, PendoConfig
from ..utils.formatters import ItemFormatter
from ..utils.settings_utils import parse_custom_settings
from ..utils.time_utils import (
    now_in_timezone,
    parse_and_localize,
    parse_hhmm_to_minutes,
)

logger = logging.getLogger(__name__)


class ReminderService:
    """提醒服务"""

    def __init__(self, db):
        self.db = db
        self.default_policies = REMINDER_POLICIES

    def _current_user_time(self, user_id: str | None) -> datetime:
        return now_in_timezone(user_id, self.db) if user_id else now_in_timezone()

    def _parse_user_time(self, dt_str: str, user_id: str | None) -> datetime:
        return parse_and_localize(dt_str, user_id, self.db) if user_id else parse_and_localize(dt_str)

    def calculate_remind_times(self, item_data, policy_type: str = "default") -> list[str]:
        """根据策略计算提醒时间点"""
        base_time = None
        owner_id = (
            getattr(item_data, "owner_id", None)
            if not isinstance(item_data, dict)
            else item_data.get("owner_id")
        )
        start_time = (
            getattr(item_data, "start_time", None)
            if not isinstance(item_data, dict)
            else item_data.get("start_time")
        )
        deadline_at = (
            getattr(item_data, "deadline_at", None)
            if not isinstance(item_data, dict)
            else item_data.get("deadline_at")
        )
        if start_time:
            base_time = self._parse_user_time(start_time, owner_id)
        elif deadline_at:
            base_time = self._parse_user_time(deadline_at, owner_id)

        if not base_time:
            return []

        policy: dict[str, Any] = self.default_policies.get(
            policy_type, self.default_policies["default"]
        )
        now = (
            self._current_user_time(owner_id)
            if owner_id
            else (datetime.now(base_time.tzinfo) if base_time.tzinfo else datetime.now())
        )
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
        sent_count = 0
        messages = []

        try:
            # 1. 检查新到期的提醒
            items = self.db.get_all_events_with_reminders(future_hours=0)

            for item in items:
                if not self._is_active_reminder_item(item):
                    continue

                settings = self.db.get_user_settings(item.owner_id)
                custom_settings = parse_custom_settings(settings)
                if not custom_settings.get("reminder_enabled", True):
                    continue

                current_time = self._current_user_time(item.owner_id)
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
                        remind_time = self._parse_user_time(remind_time_str, item.owner_id)
                        # Due reminders remain eligible after downtime.  A
                        # persistent DB lease, rather than a 120-second window,
                        # decides which overlapping scheduler may deliver it.
                        if current_time < remind_time:
                            continue
                        log = log_map.get(remind_time_str)
                        if log and log.get("confirmed_at"):
                            continue
                        claim_token = self.db.claim_reminder(
                            item.id,
                            remind_time_str,
                            now=current_time,
                            lease_seconds=PendoConfig.REMINDER_CLAIM_LEASE_SECONDS,
                        )
                        if not claim_token:
                            continue
                        if self._should_suppress(item, current_time):
                            self.db.release_reminder_claim(
                                item.id,
                                remind_time_str,
                                claim_token,
                                retry_at=self._next_quiet_hours_end(item.owner_id, current_time),
                            )
                            continue
                        message = self._build_reminder_message(item, remind_time_str)
                        messages.append(self._make_msg(item, message, remind_time_str, claim_token))
                        sent_count += 1
                    except Exception as e:
                        logger.warning("处理提醒失败: %s, error: %s", remind_time_str, e)

            # 2. 重复发送未确认的提醒
            repeat_messages = self._check_unconfirmed_repeats()
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

    def _check_unconfirmed_repeats(self, current_time: datetime | None = None) -> list[dict[str, Any]]:
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
                item = self.db.get_item(item_id)
                if not item or not self._is_active_reminder_item(item):
                    continue

                user_now = current_time or self._current_user_time(item.owner_id)
                last_sent_at = self._parse_user_time(log["last_sent_at"], item.owner_id)
                seconds_since_last = (user_now - last_sent_at).total_seconds()

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

                if self._should_suppress(item, user_now):
                    continue

                message = self._build_reminder_message(item, remind_time_str, repeat_count=repeat_count)
                messages.append(self._make_msg(item, message, remind_time_str))

        except Exception as e:
            logger.warning("检查未确认提醒重复时出错: %s", e)

        return messages

    @staticmethod
    def _is_active_reminder_item(item) -> bool:
        item_type = getattr(getattr(item, "type", None), "value", getattr(item, "type", ""))
        if item_type == "task":
            return getattr(item, "status", "open") == "open"
        return True

    def _should_suppress(self, item, check_time: datetime) -> bool:
        """判断是否应抑制发送（静默时间 + 非重要事件）"""
        if not self._is_in_quiet_hours(item.owner_id, check_time):
            return False
        if self._is_important_item(item):
            return False
        # 若事件本身也在静默时间内（如早晨6点的事件），仍发送
        start_time = getattr(item, "start_time", None)
        if start_time and self._is_in_quiet_hours(
            item.owner_id,
            self._parse_user_time(start_time, item.owner_id),
        ):
            return False
        return True

    @staticmethod
    def _make_msg(item, message: str, remind_time_str: str, claim_token: str | None = None) -> dict[str, Any]:
        """构建统一的消息字典"""
        return {
            "user_id": item.owner_id,
            "group_id": item.context.get("group_id") if isinstance(item.context, dict) else None,
            "message": message,
            "item_id": item.id,
            "remind_time": remind_time_str,
            "claim_token": claim_token,
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
        now = self._current_user_time(user_id)
        future = now + timedelta(hours=hours)
        pending = []

        items = (
            self.db.get_items(user_id, filters={"type": "event"}, limit=1000)
            + self.db.get_items(user_id, filters={"type": "task"}, limit=1000)
        )

        for item in items:
            if not self._is_active_reminder_item(item):
                continue
            remind_times = item.remind_times or []
            for remind_time_str in remind_times:
                try:
                    remind_time = self._parse_user_time(remind_time_str, user_id)
                    if now <= remind_time <= future:
                        pending.append(
                            {
                                "item_id": item.id,
                                "title": item.title,
                                "type": getattr(getattr(item, "type", None), "value", getattr(item, "type", "")),
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
        """构建提醒消息，支持事件集合 leaf 和重复提醒

        Args:
            item: 事件条目
            remind_time: 提醒时间字符串
            repeat_count: 当前是第几次重复提醒（首次为 None）
        """
        item_type = getattr(getattr(item, "type", None), "value", getattr(item, "type", ""))
        title = item.title or "无标题"
        if repeat_count is not None:
            max_repeats = PendoConfig.REMINDER_MAX_REPEATS
            header = f"⏰ **提醒 (第{repeat_count + 1}次，共{max_repeats + 1}次)**"
        else:
            header = "⏰ **提醒**"
        lines = [header]

        collection = (
            self._get_event_collection_for_item(item)
            if item_type == "event" or getattr(item, "event_collection_id", None)
            else None
        )
        if collection:
            lines.append(f"🗓️ {collection.get('title') or '无标题'}")
            lines.append(f"📌 {title}")
        elif item_type == "task":
            lines.append(f"✅ {title}")
        else:
            lines.append(f"🗓️ {title}")

        target_time = getattr(item, "start_time", None) or getattr(item, "deadline_at", None)
        if collection:
            if target_time:
                dt_str = ItemFormatter.format_datetime(target_time, "%m月%d日 %H:%M")
                label = "节点时间" if collection.get("kind") == "multi_node" else "事件时间"
                lines.append(f"🎯 {label}: {dt_str}")
        else:
            if target_time:
                dt_str = ItemFormatter.format_datetime(target_time, "%m月%d日 %H:%M")
                label = "截止时间" if item_type == "task" else "事件时间"
                lines.append(f"🎯 {label}: {dt_str}")
            elif item_type == "task" and getattr(item, "plan_date", None):
                lines.append(f"📅 计划日期: {item.plan_date}")

        reminder_slot = self._format_reminder_slot(remind_time, target_time)
        if reminder_slot:
            lines.append(f"🔔 对应提醒点: {reminder_slot}")

        if getattr(item, "location", None):
            lines.append(f"📍 {item.location}")

        notes = getattr(item, "notes", None)
        if notes:
            lines.append(f"📝 {notes}")

        is_recurring = bool(
            getattr(item, "event_collection_kind", None) == "recurring"
            or (collection and collection.get("kind") == "recurring")
        )
        if repeat_count is None and is_recurring:
            lines.append("🔄 重复日程")

        lines.append(f"\n/pendo confirm {item.id}")
        lines.append(f"/pendo snooze {item.id} 10m")

        return "\n".join(lines)

    def _get_event_collection_for_item(self, item) -> dict[str, Any] | None:
        collection_id = getattr(item, "event_collection_id", None)
        if not collection_id or not hasattr(self.db, "get_event_collection"):
            return None
        try:
            return self.db.get_event_collection(collection_id, getattr(item, "owner_id", None))
        except Exception as e:
            logger.warning("读取日程集合失败: %s", e)
            return None

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

    def _next_quiet_hours_end(self, user_id: str, current_time: datetime) -> datetime:
        """Calculate the next permitted local send time across midnight."""
        settings = self.db.get_user_settings(user_id)
        end_minutes = parse_hhmm_to_minutes(settings.get("quiet_hours_end", "07:00"))
        if end_minutes is None:
            return current_time + timedelta(minutes=1)
        candidate = current_time.replace(
            hour=end_minutes // 60,
            minute=end_minutes % 60,
            second=0,
            microsecond=0,
        )
        if candidate <= current_time:
            candidate += timedelta(days=1)
        return candidate

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
