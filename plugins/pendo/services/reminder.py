"""负责事件和待办的提醒领取、静默时段判断及重复投递。"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import TYPE_CHECKING, Any, cast

from ..config import PendoConfig
from ..models.item import get_item_type_value
from ..utils.formatters import ItemFormatter
from ..utils.identifiers import public_id
from ..utils.settings_utils import parse_custom_settings
from ..utils.time_utils import (
    TimezoneHelper,
    now_in_timezone,
    parse_and_localize,
    parse_hhmm_to_minutes,
    parse_remind_times,
)

if TYPE_CHECKING:
    from .db import Database

logger = logging.getLogger(__name__)


class ReminderService:
    """生成带持久化租约的提醒投递消息。"""

    def __init__(self, db: Database) -> None:
        self.db                                   = db
        self._last_history_prune_day: date | None = None

    def _current_user_time(self, user_id: str | None) -> datetime:
        return cast(
            datetime,
            now_in_timezone(user_id, self.db) if user_id else now_in_timezone(),
        )

    def _parse_user_time(self, dt_str: str, user_id: str | None) -> datetime:
        return cast(
            datetime,
            parse_and_localize(dt_str, user_id, self.db) if user_id else parse_and_localize(dt_str),
        )

    def check_and_send_reminders(self, _context: Any = None) -> dict[str, Any]:
        """领取所有到期提醒，并生成首次及未确认重复投递消息。"""
        self._prune_reminder_history()
        messages = self._collect_initial_reminders()
        messages.extend(self._check_unconfirmed_repeats())
        sent_count = len(messages)
        return {
            "status": "success",
            "sent": sent_count,
            "messages": messages,
            "message": f"发送了{sent_count}条提醒",
        }

    def _collect_initial_reminders(self) -> list[dict[str, Any]]:
        """逐条目收集首次投递；一个坏条目不能中断其他用户的提醒。"""
        messages: list[dict[str, Any]] = []
        for item in self.db.get_due_reminder_items(now=datetime.now(UTC)):
            try:
                messages.extend(self._collect_item_reminders(item))
            except Exception as exc:
                logger.warning("处理提醒条目失败 error_type=%s", type(exc).__name__)
        return messages

    def _prune_reminder_history(self) -> None:
        """每个 UTC 自然日最多清理一次过期的已确认提醒历史。"""

        current = datetime.now(UTC)
        if self._last_history_prune_day == current.date():
            return
        try:
            self.db.prune_reminder_logs(
                before=current - timedelta(days=PendoConfig.REMINDER_LOG_RETENTION_DAYS)
            )
        except Exception as exc:
            logger.warning("清理提醒历史失败 error_type=%s", type(exc).__name__)
            return
        self._last_history_prune_day = current.date()

    def _collect_item_reminders(self, item: Any) -> list[dict[str, Any]]:
        """校验一个条目设置，并逐个领取已到期的提醒点。"""
        if not self._is_active_reminder_item(item):
            return []
        settings = self.db.get_user_settings(item.owner_id)
        if not parse_custom_settings(settings).get("reminder_enabled", True):
            return []

        remind_times = parse_remind_times(getattr(item, "remind_times", []))
        if not remind_times:
            return []
        current_time = self._current_user_time(item.owner_id)
        log_map = {str(log["remind_time"]): log for log in self.db.get_reminder_logs(item.id)}
        messages: list[dict[str, Any]] = []
        for remind_time in remind_times:
            try:
                delivery = self._claim_initial_delivery(
                    item,
                    remind_time,
                    current_time,
                    log_map.get(remind_time),
                    settings,
                )
            except Exception as exc:
                logger.warning("处理提醒点失败 error_type=%s", type(exc).__name__)
                continue
            if delivery is not None:
                messages.append(delivery)
        return messages

    def _claim_initial_delivery(
        self,
        item: Any,
        remind_time: str,
        current_time: datetime,
        log: dict[str, Any] | None,
        settings: dict[str, Any],
    ) -> dict[str, Any] | None:
        """原子领取一个首次提醒，并在静默时段延迟或生成投递消息。"""
        scheduled_time = self._parse_user_time(remind_time, item.owner_id)
        if current_time < scheduled_time:
            return None
        # 首次见到的提醒只允许落在当前检查窗口内，防止启动时补发多年历史。
        # 已经实际尝试但投递失败的提醒则允许按退避时间重试；否则 5 分钟退避
        # 会天然超过 2 分钟检查窗口，记录虽写成待重试却永远不会再被领取。
        overdue_seconds         = (current_time - scheduled_time).total_seconds()
        failure_count           = log.get("failure_count", 0) if log else 0
        is_delivery_retry       = type(failure_count) is int and failure_count > 0
        allowed_overdue_seconds = (
            PendoConfig.REMINDER_STALE_AFTER_SECONDS
            if is_delivery_retry
            else PendoConfig.REMINDER_CHECK_WINDOW_SECONDS
        )
        if overdue_seconds > allowed_overdue_seconds:
            return None
        if log and log.get("confirmed_at"):
            return None

        claim_token = self.db.claim_reminder(
            item.id,
            remind_time,
            now           = current_time,
            lease_seconds = PendoConfig.REMINDER_CLAIM_LEASE_SECONDS,
        )
        if not claim_token:
            return None
        claim_token = str(claim_token)
        try:
            if self._should_suppress(item, current_time, settings=settings):
                self.db.release_reminder_claim(
                    item.id,
                    remind_time,
                    claim_token,
                    retry_at=self._next_quiet_hours_end(
                        item.owner_id,
                        current_time,
                        settings=settings,
                    ),
                )
                return None
            message = self._build_reminder_message(item, remind_time)
            return self._make_msg(item, message, remind_time, claim_token)
        except Exception:
            self.db.release_reminder_claim(item.id, remind_time, claim_token)
            raise

    def _check_unconfirmed_repeats(
        self, current_time: datetime | None = None
    ) -> list[dict[str, Any]]:
        """检查未确认的提醒，按间隔重复发送

        每个 (item_id, remind_time) 在 DB 中只有一行，直接读取 repeat_count 和 last_sent_at。
        """
        messages: list[dict[str, Any]] = []
        try:
            logs = self.db.get_unconfirmed_sent_reminders()
        except Exception as exc:
            logger.warning("读取未确认提醒失败 error_type=%s", type(exc).__name__)
            return []

        for log in logs:
            try:
                delivery = self._build_repeat_delivery(log, current_time)
            except Exception as exc:
                logger.warning("处理重复提醒失败 error_type=%s", type(exc).__name__)
                continue
            if delivery is not None:
                messages.append(delivery)
        return messages

    def _build_repeat_delivery(
        self,
        log: dict[str, Any],
        current_time: datetime | None,
    ) -> dict[str, Any] | None:
        """检查一个未确认提醒，必要时领取下一次重复投递。"""
        item_id      = str(log["item_id"])
        remind_time  = str(log["remind_time"])
        repeat_count = int(log["repeat_count"])
        item         = self.db.get_item(item_id)
        if not item or not self._is_active_reminder_item(item):
            return None

        settings = self.db.get_user_settings(item.owner_id)
        if not parse_custom_settings(settings).get("reminder_enabled", True):
            return None
        user_now = current_time or self._current_user_time(item.owner_id)
        if not self._repeat_is_due(log, item, user_now, settings):
            return None

        claim_token = self.db.claim_reminder_repeat(
            item_id,
            remind_time,
            repeat_count,
            now           = user_now,
            lease_seconds = PendoConfig.REMINDER_CLAIM_LEASE_SECONDS,
        )
        if not claim_token:
            return None
        claim_token = str(claim_token)
        try:
            message = self._build_reminder_message(item, remind_time, repeat_count=repeat_count)
            return self._make_msg(
                item,
                message,
                remind_time,
                claim_token,
                claim_kind   = "repeat",
                repeat_count = repeat_count,
            )
        except Exception:
            self.db.release_reminder_repeat(
                item_id,
                remind_time,
                claim_token,
                repeat_count,
            )
            raise

    def _repeat_is_due(
        self,
        log: dict[str, Any],
        item: Any,
        user_now: datetime,
        settings: dict[str, Any],
    ) -> bool:
        """判断重复间隔、最大次数和静默时段是否允许本次投递。"""
        repeat_count       = int(log["repeat_count"])
        last_sent_at       = self._parse_user_time(str(log["last_sent_at"]), item.owner_id)
        seconds_since_last = (user_now - last_sent_at).total_seconds()
        if repeat_count >= PendoConfig.REMINDER_MAX_REPEATS + 1:
            if seconds_since_last >= PendoConfig.REMINDER_AUTO_CONFIRM_AFTER_FINAL_SEND_SECONDS:
                self.db.confirm_reminder(
                    str(log["item_id"]),
                    user_action = "auto_confirmed",
                    remind_time = str(log["remind_time"]),
                )
            return False
        # 服务长期离线后，不应把数月或数年前的“未确认”提醒重新复活。
        # 一天内仍保持原有的 5 分钟重复节奏，超过保鲜期才自动结束历史提醒。
        if seconds_since_last >= PendoConfig.REMINDER_STALE_AFTER_SECONDS:
            self.db.confirm_reminder(
                str(log["item_id"]),
                user_action = "auto_confirmed",
                remind_time = str(log["remind_time"]),
            )
            return False
        if seconds_since_last < PendoConfig.REMINDER_REPEAT_INTERVAL_SECONDS:
            return False
        return not self._should_suppress(item, user_now, settings=settings)

    @staticmethod
    def _is_active_reminder_item(item: Any) -> bool:
        """只允许有效日程和未完成待办继续进入提醒队列。"""
        item_type = get_item_type_value(getattr(item, "type", None), default="")
        if item_type == "task":
            status = getattr(item, "status", "open")
            return getattr(status, "value", status) == "open"
        return True

    def _should_suppress(
        self,
        item: Any,
        check_time: datetime,
        *,
        settings: dict[str, Any] | None = None,
    ) -> bool:
        """静默时段抑制普通提醒，但保留重要条目和静默时段内发生的日程。"""
        if not self._is_in_quiet_hours(item.owner_id, check_time, settings=settings):
            return False
        if self._is_important_item(item):
            return False

        # 例如 06:00 的日程本身就在静默时段，不能因为静默规则错过事件。
        start_time = getattr(item, "start_time", None)
        if start_time:
            try:
                event_time = self._parse_user_time(str(start_time), item.owner_id)
            except (TypeError, ValueError):
                logger.warning("提醒条目开始时间无效")
            else:
                if self._is_in_quiet_hours(item.owner_id, event_time, settings=settings):
                    return False
        return True

    @staticmethod
    def _make_msg(
        item: Any,
        message: str,
        remind_time_str: str,
        claim_token: str | None = None,
        *,
        claim_kind: str   = "initial",
        repeat_count: int = 0,
    ) -> dict[str, Any]:
        """构建调度器和发送层共享的稳定投递载荷。"""
        attempt  = repeat_count + 1
        identity = f"{item.id}\0{remind_time_str}\0{attempt}".encode()
        context  = getattr(item, "context", None)
        return {
            "user_id": item.owner_id,
            "group_id": context.get("group_id") if isinstance(context, dict) else None,
            "message": message,
            "item_id": item.id,
            "remind_time": remind_time_str,
            "claim_token": claim_token,
            "claim_kind": claim_kind,
            "claim_repeat_count": repeat_count,
            "delivery_key": f"pendo-reminder-{hashlib.sha256(identity).hexdigest()}",
        }

    def confirm_reminder(
        self,
        item_id: str,
        user_action: str        = "confirmed",
        owner_id: str | None    = None,
        remind_time: str | None = None,
        allow_future: bool      = False,
    ) -> dict[str, Any]:
        """把用户确认请求交给数据库原子更新提醒日志。"""
        return cast(
            dict[str, Any],
            self.db.confirm_reminder(
                item_id,
                user_action,
                owner_id     = owner_id,
                remind_time  = remind_time,
                allow_future = allow_future,
            ),
        )

    def detect_conflict(
        self, user_id: str, start_time: str, end_time: str | None = None
    ) -> list[dict[str, Any]]:
        """按绝对时间检测半开区间冲突，兼容无时区和带偏移的 ISO 时间。"""
        start_dt = self._parse_user_time(start_time, user_id)
        end_dt   = (
            self._parse_user_time(end_time, user_id) if end_time else start_dt + timedelta(hours=1)
        )
        if end_dt <= start_dt:
            raise ValueError("日程结束时间必须晚于开始时间")

        # SQLite 中混存无偏移和带偏移 ISO 字符串时，字典序不等于绝对时间顺序；
        # 因此只在 SQL 层筛选类型，再统一本地化后比较。分页接口避免静默漏掉第
        # 1001 条以后仍可能冲突的日程。
        items = self.db.get_all_items(user_id, filters={"type": "event"}, page_size=200)
        conflicts: list[dict[str, Any]] = []

        for item in items:
            item_start_text = getattr(item, "start_time", None)
            if not item_start_text:
                continue
            try:
                item_start    = self._parse_user_time(str(item_start_text), user_id)
                item_end_text = getattr(item, "end_time", None)
                item_end      = (
                    self._parse_user_time(str(item_end_text), user_id)
                    if item_end_text
                    else item_start + timedelta(hours=1)
                )
            except (TypeError, ValueError):
                logger.warning("跳过时间格式无效的已有日程")
                continue

            # 区间重叠：item 与 [start_dt, end_dt) 有交叉
            if not (end_dt <= item_start or start_dt >= item_end):
                conflicts.append(
                    {
                        "id": item.id,
                        "title": item.title,
                        "start_time": item_start_text,
                        "end_time": getattr(item, "end_time", None),
                    }
                )

        return conflicts

    def _build_reminder_message(
        self, item: Any, remind_time: str, repeat_count: int | None = None
    ) -> str:
        """构建日程或待办提醒，集合条目同时展示集合标题和节点标题。"""
        item_type = get_item_type_value(getattr(item, "type", None), default="")
        title = item.title or "无标题"
        if repeat_count is not None:
            max_repeats = PendoConfig.REMINDER_MAX_REPEATS
            header      = f"⏰ **提醒 (第{repeat_count + 1}次，共{max_repeats + 1}次)**"
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

        owner_id         = getattr(item, "owner_id", None)
        display_timezone = (
            TimezoneHelper.get_user_timezone(str(owner_id), self.db)
            if owner_id
            else TimezoneHelper.DEFAULT_TZ
        )
        target_time = getattr(item, "start_time", None) or getattr(item, "deadline_at", None)
        target_line = self._format_target_line(
            item_type,
            collection,
            target_time,
            getattr(item, "plan_date", None),
            display_timezone,
        )
        if target_line:
            lines.append(target_line)

        reminder_slot = self._format_reminder_slot(
            remind_time,
            target_time,
            owner_id,
            display_timezone=display_timezone,
        )
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

        display_id = public_id(getattr(item, "id", ""))
        lines.append(f"\n/pendo confirm {display_id}")
        lines.append(f"/pendo snooze {display_id} 10m")

        return "\n".join(lines)

    @staticmethod
    def _format_target_line(
        item_type: str,
        collection: dict[str, Any] | None,
        target_time: str | None,
        plan_date: str | None,
        display_timezone: tzinfo,
    ) -> str | None:
        """格式化提醒对象的目标时间；无具体时刻的待办退回计划日期。"""
        if target_time:
            formatted = ItemFormatter.format_datetime(
                target_time, "%m月%d日 %H:%M", tz=display_timezone
            )
            if collection:
                label = "节点时间" if collection.get("kind") == "multi_node" else "事件时间"
            else:
                label = "截止时间" if item_type == "task" else "事件时间"
            return f"🎯 {label}: {formatted}"
        if item_type == "task" and plan_date:
            return f"📅 计划日期: {plan_date}"
        return None

    def _get_event_collection_for_item(self, item: Any) -> dict[str, Any] | None:
        collection_id = getattr(item, "event_collection_id", None)
        if not collection_id or not hasattr(self.db, "get_event_collection"):
            return None
        try:
            return cast(
                dict[str, Any] | None,
                self.db.get_event_collection(
                    str(collection_id),
                    getattr(item, "owner_id", None),
                ),
            )
        except Exception as exc:
            logger.warning(
                "读取日程集合失败 error_type=%s",
                type(exc).__name__,
            )
            return None

    def _format_reminder_slot(
        self,
        remind_time: str,
        target_time: str | None,
        user_id: str | None = None,
        *,
        display_timezone: tzinfo,
    ) -> str:
        """格式化提醒点，展示相对目标时间的偏移和原始提醒时间。"""
        remind_dt_str = cast(
            str,
            ItemFormatter.format_datetime(remind_time, "%m月%d日 %H:%M", tz=display_timezone),
        )
        if not target_time:
            return remind_dt_str

        try:
            remind_dt = self._parse_user_time(remind_time, user_id)
            target_dt = self._parse_user_time(target_time, user_id)
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

        parts: list[str] = []
        if days:
            parts.append(f"{days}天")
        if hours:
            parts.append(f"{hours}小时")
        if minutes:
            parts.append(f"{minutes}分钟")
        if not parts:
            parts.append(f"{seconds}秒" if seconds else "0分钟")
        return "".join(parts)

    def _is_in_quiet_hours(
        self,
        user_id: str,
        remind_time: datetime,
        *,
        settings: dict[str, Any] | None = None,
    ) -> bool:
        """按用户本地时钟判断提醒是否落在静默时段。"""
        try:
            resolved      = settings if settings is not None else self.db.get_user_settings(user_id)
            start_minutes = cast(
                int | None,
                parse_hhmm_to_minutes(
                    resolved.get("quiet_hours_start", PendoConfig.DEFAULT_QUIET_HOURS_START)
                ),
            )
            end_minutes = cast(
                int | None,
                parse_hhmm_to_minutes(
                    resolved.get("quiet_hours_end", PendoConfig.DEFAULT_QUIET_HOURS_END)
                ),
            )

            if start_minutes is None or end_minutes is None:
                return False

            current = remind_time.hour * 60 + remind_time.minute

            if start_minutes > end_minutes:
                return current >= start_minutes or current < end_minutes
            return start_minutes <= current < end_minutes
        except Exception as exc:
            # 设置异常不能阻塞整个提醒调度；记录异常类型后按“非静默”处理。
            logger.warning("读取静默时段失败 error_type=%s", type(exc).__name__)
            return False

    def _next_quiet_hours_end(
        self,
        user_id: str,
        current_time: datetime,
        *,
        settings: dict[str, Any] | None = None,
    ) -> datetime:
        """计算跨午夜静默时段结束后的下一次可投递时刻。"""
        try:
            resolved    = settings if settings is not None else self.db.get_user_settings(user_id)
            end_minutes = cast(
                int | None,
                parse_hhmm_to_minutes(
                    resolved.get("quiet_hours_end", PendoConfig.DEFAULT_QUIET_HOURS_END)
                ),
            )
        except Exception as exc:
            logger.warning("读取静默结束时间失败 error_type=%s", type(exc).__name__)
            end_minutes = None
        if end_minutes is None:
            return current_time + timedelta(minutes=1)
        candidate = current_time.replace(
            hour        = end_minutes // 60,
            minute      = end_minutes % 60,
            second      = 0,
            microsecond = 0,
        )
        if candidate <= current_time:
            candidate += timedelta(days=1)
        return candidate

    @staticmethod
    def _is_important_item(item: Any) -> bool:
        """按显式优先级、标签和标题识别可穿透静默规则的重要条目。"""
        priority = getattr(item, "priority", None)
        if isinstance(priority, (int, float)) and not isinstance(priority, bool) and priority <= 2:
            return True

        important_tags = {"重要", "紧急", "important", "urgent"}
        tags           = getattr(item, "tags", None) or []
        if any(str(tag).strip().casefold() in important_tags for tag in tags):
            return True

        title = str(getattr(item, "title", "") or "").casefold()
        return any(keyword in title for keyword in ("重要", "紧急", "会议", "deadline", "截止"))
