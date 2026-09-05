"""执行 Pendo 的提醒投递、周期摘要、迁移和数据保留任务。"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any, Final, Literal, Protocol, cast, runtime_checkable

from core.delivery import DeliveryReceipt, send_with_receipt
from core.plugin_base import run_sync, segments
from core.public_errors import public_error_message

from ..config import PendoConfig
from ..models.item import EventItem, Item, ItemType, LedgerItem, TaskItem
from ..services.db import Database
from ..services.reminder import ReminderService
from ..utils.currency import currency_label, group_by_currency
from ..utils.db_ops import (
    get_database,
    get_user_settings_bundle_map,
)
from ..utils.formatters import ledger_amount_yuan
from ..utils.settings_utils import save_user_setting
from ..utils.time_utils import (
    TimezoneHelper,
    get_user_now_from_settings,
    now_in_timezone,
)

logger = logging.getLogger(__name__)

JsonObject        = dict[str, Any]
ActionList        = list[JsonObject]
SettingsBundle    = dict[str, JsonObject]
ReminderClaimKind = Literal["initial", "repeat"]
FinancePeriodKind = Literal["weekly", "monthly"]


@runtime_checkable
class _ActionSender(Protocol):
    """定时任务需要的最小 OneBot 发送接口。"""

    async def send_action(self, action: JsonObject) -> bool | None: ...


@dataclass(frozen=True, slots=True)
class _ReminderDelivery:
    """通过校验的单条提醒投递。"""

    user_id: str
    message: str
    item_id: str | None
    remind_time: str | None
    claim_token: str | None
    claim_kind: ReminderClaimKind
    claim_repeat_count: int
    delivery_key: str | None


@dataclass(slots=True)
class _ScheduleRun:
    """一次周期任务共享的数据库、发送上下文、时钟和返回动作。"""

    context: object
    db: Database
    current_utc: datetime
    messages: ActionList = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _PeriodicClaim:
    """数据库周期投递租约的最小必需字段。"""

    task_name: str
    owner_id: str
    period_key: str
    claim_token: str
    delivery_key: str


@dataclass(frozen=True, slots=True)
class _FinancePeriod:
    """一次财务总结的查询范围和展示信息。"""

    key: str
    start_date: str
    end_date: str
    label: str
    title: str


@dataclass(frozen=True, slots=True)
class _FinanceSchedule:
    """周报/月报之间唯一不同的调度常量。"""

    kind: FinancePeriodKind
    task_name: str
    marker_name: str
    component: str
    title: str


@dataclass(slots=True)
class _FinanceMetrics:
    """单次遍历账目后得到的财务总结指标。"""

    currency: str = "CNY"
    by_currency: dict[str, _FinanceMetrics] = field(default_factory=dict)
    item_count: int       = 0
    total_income: float   = 0.0
    total_expense: float  = 0.0
    total_transfer: float = 0.0
    transfer_count: int   = 0
    expense_by_category: dict[str, float] = field(default_factory=dict)
    income_by_category: dict[str, float] = field(default_factory=dict)
    account_flow: dict[str, dict[str, float]] = field(default_factory=dict)
    transfer_flow: dict[str, float] = field(default_factory=dict)
    top_expense: LedgerItem | None = None
    top_expense_amount: float      = 0.0


@dataclass(frozen=True, slots=True)
class _BriefingEvent:
    """每日简报中已经完成时区归一化的日程。"""

    sort_time: datetime
    time_text: str
    title: str
    location: str


_WEEKLY_FINANCE: Final = _FinanceSchedule(
    "weekly",
    "weekly_finance_summary",
    "last_weekly_finance_summary_date",
    "weekly_finance",
    "📆 本周财务总结",
)
_MONTHLY_FINANCE: Final = _FinanceSchedule(
    "monthly",
    "month_end_finance_summary",
    "last_month_end_finance_summary_date",
    "monthly_finance",
    "🧾 月底财务总结",
)

# 缓存提醒服务；插件卸载时由 cleanup_reminder_singleton 显式清空。
_reminder_service_singleton: ReminderService | None = None


def _next_reminder_retry_at() -> datetime:
    """返回一次投递失败后的最早重试时刻。"""

    return datetime.now(UTC) + timedelta(seconds=PendoConfig.REMINDER_REPEAT_INTERVAL_SECONDS)


def purge_expired_demo_users(db: Database, now: datetime | None = None) -> int:
    """延迟导入 Web 清理器，普通命令不因此依赖 PyJWT。"""
    from ..web.services.demo_space import purge_expired_demo_users as _purge_expired_demo_users

    return cast(int, _purge_expired_demo_users(db, now=now))


def _parse_reminder_delivery(raw: object) -> _ReminderDelivery | None:
    """把提醒服务的动态结果收敛为可安全投递的内部结构。"""

    if not isinstance(raw, Mapping):
        return None
    user_value = raw.get("user_id")
    message    = raw.get("message")
    if user_value in (None, "") or not isinstance(message, str):
        return None
    claim_kind: ReminderClaimKind = "repeat" if raw.get("claim_kind") == "repeat" else "initial"
    repeat_count                  = raw.get("claim_repeat_count", 0)
    return _ReminderDelivery(
        user_id            = str(user_value),
        message            = message,
        item_id            = str(raw["item_id"]) if raw.get("item_id") else None,
        remind_time        = str(raw["remind_time"]) if raw.get("remind_time") else None,
        claim_token        = str(raw["claim_token"]) if raw.get("claim_token") else None,
        claim_kind         = claim_kind,
        claim_repeat_count = repeat_count if isinstance(repeat_count, int) else 0,
        delivery_key       = str(raw["delivery_key"]) if raw.get("delivery_key") else None,
    )


async def _settle_reminder_delivery(
    db: Database,
    delivery: _ReminderDelivery,
    *,
    delivered: bool,
) -> None:
    """按 OneBot 确认结果完成或释放提醒租约。"""

    if delivery.item_id is None or delivery.remind_time is None:
        return
    if delivery.claim_token is None:
        if delivered:
            await run_sync(db.log_reminder, delivery.item_id, delivery.remind_time, sent=True)
        return
    if delivery.claim_kind == "repeat":
        if delivered:
            await run_sync(
                db.complete_reminder_repeat,
                delivery.item_id,
                delivery.remind_time,
                delivery.claim_token,
                delivery.claim_repeat_count,
            )
        else:
            await run_sync(
                db.release_reminder_repeat,
                delivery.item_id,
                delivery.remind_time,
                delivery.claim_token,
                delivery.claim_repeat_count,
                retry_at=_next_reminder_retry_at(),
            )
        return
    if delivered:
        await run_sync(
            db.complete_reminder_claim,
            delivery.item_id,
            delivery.remind_time,
            delivery.claim_token,
        )
        return

    # OneBot 暂时离线时不能每分钟重新领取同一批历史提醒，否则每条消息的
    # 网络超时会串成持续刷屏。复用“未确认提醒”的配置间隔，把失败租约延后
    # 到下一次允许尝试的时间；提醒本身仍保持 pending，不会被误记为已送达。
    await run_sync(
        db.release_reminder_claim,
        delivery.item_id,
        delivery.remind_time,
        delivery.claim_token,
        retry_at=_next_reminder_retry_at(),
    )


async def _deliver_reminder(
    context: object,
    messages: ActionList,
    db: Database,
    delivery: _ReminderDelivery,
) -> None:
    """发送一条提醒；无直接发送器时只返回动作，不伪造送达确认。"""

    action = _build_private_action(
        delivery.user_id,
        delivery.message,
        delivery_key=delivery.delivery_key,
    )
    if action is None:
        await _settle_reminder_delivery(db, delivery, delivered=False)
        return
    if not isinstance(context, _ActionSender):
        messages.append(action)
        return

    async def confirm() -> None:
        await _settle_reminder_delivery(db, delivery, delivered=True)

    async def reject() -> None:
        await _settle_reminder_delivery(db, delivery, delivered=False)

    receipt = DeliveryReceipt(
        expected_actions = 1,
        commit           = confirm,
        rollback         = reject,
        # 未知结果保持租约占用，等待租约自然过期，避免五分钟后立即重复提醒。
        unknown=lambda: None,
    )
    try:
        await send_with_receipt(context.send_action, action, receipt)
    except Exception as exc:
        await receipt.record(False)
        public_error_message(
            context,
            exc,
            logger    = logger,
            component = "pendo.scheduled.reminder_delivery",
        )
        return
    if receipt.callback_error is not None:
        public_error_message(
            context,
            receipt.callback_error,
            logger    = logger,
            component = "pendo.scheduled.reminder_settlement",
        )
        return
    if receipt.outcome is False:
        logger.warning(
            "提醒未被 OneBot 确认，保留待重试状态 item=%s",
            delivery.item_id,
        )
    elif receipt.outcome is None:
        logger.warning(
            "提醒投递结果未知，保留当前租约直至过期 item=%s",
            delivery.item_id,
        )


async def check_reminders(context: object) -> ActionList:
    """领取到期提醒，并按 OneBot 的明确确认结果结算租约。"""

    global _reminder_service_singleton
    db                   = cast(Database, get_database(context))
    messages: ActionList = []

    if _reminder_service_singleton is None or (
        isinstance(_reminder_service_singleton, ReminderService)
        and _reminder_service_singleton.db is not db
    ):
        _reminder_service_singleton = ReminderService(db)
    reminder_service = _reminder_service_singleton

    result = cast(JsonObject, await run_sync(reminder_service.check_and_send_reminders, context))
    raw_messages = result.get("messages")
    if not isinstance(raw_messages, list):
        return messages
    for raw in raw_messages:
        delivery = _parse_reminder_delivery(raw)
        if delivery is not None:
            await _deliver_reminder(context, messages, db, delivery)

    return messages


async def _send_daily_briefing_for_user(
    run: _ScheduleRun,
    user_id: str,
    bundle: SettingsBundle,
) -> None:
    """在单用户边界内领取、投递并标记每日简报。"""

    custom_settings = bundle["custom_settings"]
    user_settings   = bundle["settings"]
    user_now        = cast(datetime, get_user_now_from_settings(user_settings, run.current_utc))
    current_date    = user_now.date().isoformat()
    if custom_settings.get("last_daily_briefing_date") == current_date:
        return
    if not custom_settings.get("daily_briefing_enabled", True):
        return
    if not _is_scheduled_minute(
        user_now,
        str(user_settings.get("daily_report_time") or "08:00"),
    ):
        return

    claim = await _claim_periodic_delivery(
        run.db,
        "daily_briefing",
        user_id,
        current_date,
        user_now,
    )
    if claim is None:
        return
    try:
        briefing       = await _generate_briefing_content(user_id, run.db)
        delivery_claim = claim
        claim          = None
        delivered      = await _send_claimed_private(
            run.context,
            run.messages,
            run.db,
            delivery_claim,
            briefing,
        )
        if not delivered:
            logger.warning("Daily briefing was not delivery-confirmed for user %s", user_id)
            return
        await run_sync(
            save_user_setting,
            user_id,
            "last_daily_briefing_date",
            current_date,
            run.db,
        )
        logger.info("Sent daily briefing to user %s", user_id)
    except Exception:
        if claim is not None:
            await _release_periodic_delivery(run.db, claim)
        raise


async def send_daily_briefings(context: object, db: Database) -> ActionList:
    """向到达本地配置时刻且尚未发送的用户投递每日简报。"""

    run = _ScheduleRun(context, db, datetime.now(UTC))
    try:
        user_ids = await _get_active_user_ids(db)
        bundles  = cast(
            dict[str, SettingsBundle],
            await get_user_settings_bundle_map(user_ids, db),
        )
    except Exception as exc:
        public_error_message(
            context,
            exc,
            logger    = logger,
            component = "pendo.scheduled.daily_briefing",
        )
        return run.messages

    for user_id in user_ids:
        try:
            await _send_daily_briefing_for_user(run, user_id, bundles[user_id])
        except Exception as exc:
            public_error_message(
                context,
                exc,
                logger    = logger,
                component = "pendo.scheduled.daily_briefing_user",
            )
    return run.messages


async def _send_diary_reminder_for_user(
    run: _ScheduleRun,
    user_id: str,
    bundle: SettingsBundle,
) -> None:
    """在单用户边界内检查日记并投递当日提醒。"""

    custom_settings = bundle["custom_settings"]
    user_settings   = bundle["settings"]
    user_now        = cast(datetime, get_user_now_from_settings(user_settings, run.current_utc))
    current_date    = user_now.date().isoformat()
    if custom_settings.get("last_diary_remind_date") == current_date:
        return
    if not _is_scheduled_minute(
        user_now,
        str(user_settings.get("diary_remind_time") or "21:30"),
    ):
        return
    has_diary = cast(
        bool,
        await run_sync(run.db.has_diary_for_date, user_id, current_date),
    )
    if has_diary:
        await run_sync(
            save_user_setting,
            user_id,
            "last_diary_remind_date",
            current_date,
            run.db,
        )
        return

    claim = await _claim_periodic_delivery(
        run.db,
        "diary_reminder",
        user_id,
        current_date,
        user_now,
    )
    if claim is None:
        return
    try:
        delivery_claim = claim
        claim          = None
        delivered      = await _send_claimed_private(
            run.context,
            run.messages,
            run.db,
            delivery_claim,
            "📔 今天还没有写日记哦，记录一下美好的今天吧？\n发送 /pendo diary 开始",
        )
        if not delivered:
            logger.warning("Diary reminder was not delivery-confirmed for user %s", user_id)
            return
        await run_sync(
            save_user_setting,
            user_id,
            "last_diary_remind_date",
            current_date,
            run.db,
        )
    except Exception:
        if claim is not None:
            await _release_periodic_delivery(run.db, claim)
        raise


async def check_diary_reminders(context: object, db: Database) -> ActionList:
    """在每个用户的本地提醒时刻检查当天是否缺少日记。"""

    run = _ScheduleRun(context, db, datetime.now(UTC))
    try:
        user_ids = await _get_active_user_ids(db)
        bundles  = cast(
            dict[str, SettingsBundle],
            await get_user_settings_bundle_map(user_ids, db),
        )
    except Exception as exc:
        public_error_message(
            context,
            exc,
            logger    = logger,
            component = "pendo.scheduled.diary_reminder",
        )
        return run.messages

    for user_id in user_ids:
        try:
            await _send_diary_reminder_for_user(run, user_id, bundles[user_id])
        except Exception as exc:
            public_error_message(
                context,
                exc,
                logger    = logger,
                component = "pendo.scheduled.diary_reminder_user",
            )
    return run.messages


async def prune_operation_logs(_context: object, db: Database) -> ActionList:
    """执行审计日志保留策略，并记录本轮清理数量。"""

    result = cast(dict[str, int], await run_sync(db.prune_operation_logs))
    logger.info(
        "Pendo operation-log retention finished: deleted=%s redacted=%s",
        result["deleted"],
        result["redacted"],
    )
    return []


def _finance_period(user_now: datetime, schedule: _FinanceSchedule) -> _FinancePeriod:
    """按用户本地日期构造本周或本月的闭区间。"""

    end_date = user_now.date()
    if schedule.kind == "weekly":
        start_date = end_date - timedelta(days=user_now.weekday())
        iso_year, iso_week, _weekday = user_now.isocalendar()
        return _FinancePeriod(
            f"{iso_year}-W{iso_week:02d}",
            start_date.isoformat(),
            end_date.isoformat(),
            f"{start_date.strftime('%m/%d')} - {end_date.strftime('%m/%d')}",
            schedule.title,
        )
    start_date = end_date.replace(day=1)
    return _FinancePeriod(
        f"{user_now.year:04d}-{user_now.month:02d}",
        start_date.isoformat(),
        end_date.isoformat(),
        f"{start_date.strftime('%Y/%m/%d')} - {end_date.strftime('%Y/%m/%d')}",
        schedule.title,
    )


async def _send_finance_summary_for_user(
    run: _ScheduleRun,
    user_id: str,
    bundle: SettingsBundle,
    schedule: _FinanceSchedule,
) -> None:
    """领取并投递一个用户的周/月财务总结。"""

    user_now = cast(
        datetime,
        get_user_now_from_settings(bundle["settings"], run.current_utc),
    )
    current_date = user_now.date().isoformat()
    if bundle["custom_settings"].get(schedule.marker_name) == current_date:
        return
    period = _finance_period(user_now, schedule)
    claim  = await _claim_periodic_delivery(
        run.db,
        schedule.task_name,
        user_id,
        period.key,
        user_now,
    )
    if claim is None:
        return
    try:
        summary = await _generate_finance_summary_content(run.db, user_id, period)
        if summary:
            delivery_claim = claim
            claim          = None
            completed      = await _send_claimed_private(
                run.context,
                run.messages,
                run.db,
                delivery_claim,
                summary,
            )
        else:
            completed = await _complete_periodic_delivery(run.db, claim)
            claim     = None
        if not completed:
            return
        await run_sync(
            save_user_setting,
            user_id,
            schedule.marker_name,
            current_date,
            run.db,
        )
    except Exception:
        if claim is not None:
            await _release_periodic_delivery(run.db, claim)
        raise


async def _send_finance_summaries(
    context: object,
    db: Database,
    schedule: _FinanceSchedule,
) -> ActionList:
    """共享周报和月报的用户遍历、错误隔离与批量设置读取。"""

    run = _ScheduleRun(context, db, datetime.now(UTC))
    try:
        user_ids = await _get_active_user_ids(db)
        bundles  = cast(
            dict[str, SettingsBundle],
            await get_user_settings_bundle_map(user_ids, db),
        )
    except Exception as exc:
        public_error_message(
            context,
            exc,
            logger    = logger,
            component = f"pendo.scheduled.{schedule.component}",
        )
        return run.messages

    for user_id in user_ids:
        try:
            await _send_finance_summary_for_user(run, user_id, bundles[user_id], schedule)
        except Exception as exc:
            public_error_message(
                context,
                exc,
                logger    = logger,
                component = f"pendo.scheduled.{schedule.component}_user",
            )
    return run.messages


async def send_weekly_finance_summaries(context: object, db: Database) -> ActionList:
    """发送由周调度器触发的本周财务总结。"""

    return await _send_finance_summaries(context, db, _WEEKLY_FINANCE)


async def send_month_end_finance_summaries(context: object, db: Database) -> ActionList:
    """发送由月末调度器触发的本月财务总结。"""

    return await _send_finance_summaries(context, db, _MONTHLY_FINANCE)


async def cleanup_expired_demo_data(context: object, db: Database) -> ActionList:
    """定期清理过期的 Pendo Web demo 数据。"""
    try:
        cleaned = await run_sync(purge_expired_demo_users, db)
        if cleaned:
            logger.info("Cleaned up %s expired Pendo Web demo users", cleaned)
    except Exception as exc:
        public_error_message(
            context,
            exc,
            logger    = logger,
            component = "pendo.scheduled.demo_cleanup",
        )
    return []


# ============================================================
# 辅助函数
# ============================================================


async def _get_active_user_ids(db: Database) -> list[str]:
    """在线程池中读取至少有一条有效数据的用户。"""

    return cast(list[str], await run_sync(db.get_active_user_ids))


def _build_private_action(
    user_id: str,
    message: str,
    *,
    delivery_key: str | None = None,
) -> dict[str, Any] | None:
    """构造私聊动作；Web demo 等非 QQ 所有者不会进入 OneBot。"""

    try:
        numeric_user_id = int(str(user_id))
    except (TypeError, ValueError):
        logger.debug("Skipping scheduled private message for non-numeric owner_id=%s", user_id)
        return None
    action = {
        "action": "send_private_msg",
        "params": {"user_id": numeric_user_id, "message": segments(message)},
    }
    if delivery_key:
        # 稳定键用于租约恢复后的去重和响应关联，且不暴露所有者或周期明文。
        action["echo"] = delivery_key
    return action


async def _claim_periodic_delivery(
    db: Database,
    task_name: str,
    owner_id: str,
    period_key: str,
    current_time: datetime,
) -> _PeriodicClaim | None:
    """从真实数据库领取周期投递；测试替身也必须遵守同一契约。"""

    raw = cast(
        JsonObject | None,
        await run_sync(
            db.claim_scheduled_delivery,
            task_name,
            owner_id,
            period_key,
            now           = current_time.astimezone(UTC),
            lease_seconds = PendoConfig.REMINDER_CLAIM_LEASE_SECONDS,
        ),
    )
    if raw is None:
        return None
    claim_token  = raw.get("claim_token")
    delivery_key = raw.get("delivery_key")
    if not isinstance(claim_token, str) or not isinstance(delivery_key, str):
        raise ValueError("scheduled delivery claim is missing token or delivery key")
    return _PeriodicClaim(task_name, owner_id, period_key, claim_token, delivery_key)


async def _complete_periodic_delivery(
    db: Database,
    claim: _PeriodicClaim,
) -> bool:
    """把已确认投递的租约原子标记为完成。"""

    return cast(
        bool,
        await run_sync(
            db.complete_scheduled_delivery,
            claim.task_name,
            claim.owner_id,
            claim.period_key,
            claim.claim_token,
        ),
    )


async def _release_periodic_delivery(
    db: Database,
    claim: _PeriodicClaim,
) -> bool:
    """释放尚未确认投递的租约，使后续调度可以安全重试。"""

    return cast(
        bool,
        await run_sync(
            db.release_scheduled_delivery,
            claim.task_name,
            claim.owner_id,
            claim.period_key,
            claim.claim_token,
        ),
    )


async def _send_claimed_private(
    context: object,
    messages: ActionList,
    db: Database,
    claim: _PeriodicClaim,
    message: str,
) -> bool:
    """接管租约并发送；拒绝时释放，未知时保留，确认后完成。"""

    async def confirm() -> None:
        completed = await _complete_periodic_delivery(db, claim)
        if not completed:
            raise RuntimeError("scheduled delivery lease was lost after confirmed send")

    async def reject() -> None:
        await _release_periodic_delivery(db, claim)

    receipt = DeliveryReceipt(
        expected_actions = 1,
        commit           = confirm,
        rollback         = reject,
        # 传输结果未知时不释放租约，避免下一轮立即重复发送。
        unknown=lambda: None,
    )

    try:
        outcome = await _send_private_or_collect(
            context,
            messages,
            claim.owner_id,
            message,
            delivery_key = claim.delivery_key,
            receipt      = receipt,
        )
    except Exception:
        await receipt.record(False)
        raise
    if not receipt.resolved:
        await receipt.record(outcome)
    if receipt.callback_error is not None:
        raise receipt.callback_error
    if receipt.outcome is None:
        logger.warning(
            "Scheduled Pendo delivery outcome unknown; claim retained task=%s",
            claim.task_name,
        )
        return False
    return receipt.committed


async def _send_private_or_collect(
    context: object,
    messages: ActionList,
    user_id: str,
    message: str,
    *,
    delivery_key: str | None        = None,
    receipt: DeliveryReceipt | None = None,
) -> bool | None:
    action = _build_private_action(user_id, message, delivery_key=delivery_key)
    if action is None:
        return False
    if isinstance(context, _ActionSender):
        if receipt is not None:
            return await send_with_receipt(context.send_action, action, receipt)
        return await context.send_action(action)
    messages.append(action)
    # 返回动作不是送达确认，调用方不能据此写入已发送标记。
    return False


def _is_scheduled_minute(current_time: datetime, target_time: str) -> bool:
    """仅在合法 ``HH:MM`` 与当前本地分钟完全一致时返回真。"""

    try:
        target_hour, target_minute = map(int, target_time.split(":"))
    except ValueError:
        return False
    if not 0 <= target_hour <= 23 or not 0 <= target_minute <= 59:
        return False
    return current_time.hour == target_hour and current_time.minute == target_minute


async def _generate_finance_summary_content(
    db: Database,
    user_id: str,
    period: _FinancePeriod,
) -> str:
    """读取指定周期的账目，并生成可直接投递的总结。"""

    items = cast(
        list[Item],
        await run_sync(
            db.query_items_by_date_range,
            user_id,
            ItemType.LEDGER.value,
            "ledger_date",
            period.start_date,
            period.end_date,
        ),
    )
    metrics = _summarize_finance_items(items)
    return _format_finance_summary(metrics, period) if metrics.item_count else ""


def _summarize_finance_items(items: Sequence[Item]) -> _FinanceMetrics:
    """单次遍历账目，汇总金额、分类、账户和转账流向。"""

    groups = group_by_currency(item for item in items if isinstance(item, LedgerItem))
    if len(groups) > 1:
        return _FinanceMetrics(
            item_count  = sum(len(rows) for rows in groups.values()),
            by_currency = {code: _summarize_finance_items(rows) for code, rows in groups.items()},
        )
    metrics = _FinanceMetrics(currency=next(iter(groups), "CNY"))
    for item in items:
        if not isinstance(item, LedgerItem):
            continue
        metrics.item_count += 1
        amount = ledger_amount_yuan(item)

        if item.transaction_type == "income":
            metrics.total_income += amount
            category                             = item.ledger_category or "其他"
            metrics.income_by_category[category] = (
                metrics.income_by_category.get(category, 0.0) + amount
            )
            account = item.account_name or "现金"
            flow    = metrics.account_flow.setdefault(account, {"income": 0.0, "expense": 0.0})
            flow["income"] += amount
            continue

        if item.transaction_type == "expense":
            metrics.total_expense += amount
            category                              = item.ledger_category or "其他"
            metrics.expense_by_category[category] = (
                metrics.expense_by_category.get(category, 0.0) + amount
            )
            account = item.account_name or "现金"
            flow    = metrics.account_flow.setdefault(account, {"income": 0.0, "expense": 0.0})
            flow["expense"] += amount
            if metrics.top_expense is None or amount > metrics.top_expense_amount:
                metrics.top_expense        = item
                metrics.top_expense_amount = amount
            continue

        if item.transaction_type == "transfer":
            metrics.total_transfer += amount
            metrics.transfer_count += 1
            source                           = item.account_name or "现金"
            destination                      = item.counter_account_name or "未指定账户"
            flow_name                        = f"{source} → {destination}"
            metrics.transfer_flow[flow_name] = metrics.transfer_flow.get(flow_name, 0.0) + amount

    return metrics


def _format_finance_summary(metrics: _FinanceMetrics, period: _FinancePeriod) -> str:
    """把已经聚合的财务指标格式化为稳定的中文摘要。"""

    if metrics.by_currency:
        return "\n\n".join(
            f"【{code}】\n{_format_finance_summary(group, period)}"
            for code, group in metrics.by_currency.items()
        )
    label          = currency_label(metrics.currency)
    balance        = metrics.total_income - metrics.total_expense
    balance_prefix = "+" if balance >= 0 else ""
    lines          = [
        period.title,
        f"📅 范围: {period.label}",
        f"🧾 共 {metrics.item_count} 笔流水",
        "",
        f"💰 收入: {label}{metrics.total_income:.2f}",
        f"💸 支出: {label}{metrics.total_expense:.2f}",
        f"📊 结余: {balance_prefix}{label}{balance:.2f}",
    ]
    _append_finance_highlights(lines, metrics, period)

    if metrics.expense_by_category:
        lines.extend(("", "支出前 3 分类:"))
        ranked_categories = sorted(
            metrics.expense_by_category.items(),
            key     = lambda pair: pair[1],
            reverse = True,
        )
        for index, (category, amount) in enumerate(ranked_categories[:3], 1):
            lines.append(f"  {index}. {category} {label}{amount:.2f}")

    if metrics.account_flow:
        lines.extend(("", "账户收支:"))
        ranked_accounts = sorted(
            metrics.account_flow.items(),
            key     = lambda pair: pair[1]["income"] + pair[1]["expense"],
            reverse = True,
        )
        for index, (account, flow) in enumerate(ranked_accounts[:5], 1):
            net        = flow["income"] - flow["expense"]
            net_prefix = "+" if net >= 0 else ""
            lines.append(
                f"  {index}. {account} 收入{label}{flow['income']:.2f} "
                f"支出{label}{flow['expense']:.2f} 净额{net_prefix}{label}{net:.2f}"
            )

    if metrics.transfer_flow:
        lines.extend(("", "转账流向:"))
        ranked_transfers = sorted(
            metrics.transfer_flow.items(),
            key     = lambda pair: pair[1],
            reverse = True,
        )
        for index, (flow_name, amount) in enumerate(ranked_transfers[:5], 1):
            lines.append(f"  {index}. {flow_name} {label}{amount:.2f}")

    return "\n".join(lines)


def _append_finance_highlights(
    lines: list[str],
    metrics: _FinanceMetrics,
    period: _FinancePeriod,
) -> None:
    """追加转账合计、主要分类和最大单笔支出。"""

    label = currency_label(metrics.currency)
    if metrics.transfer_count:
        lines.append(f"🔁 转账: {label}{metrics.total_transfer:.2f}")

    top_expense_category = max(
        metrics.expense_by_category.items(),
        key     = lambda pair: pair[1],
        default = None,
    )
    top_income_category = max(
        metrics.income_by_category.items(),
        key     = lambda pair: pair[1],
        default = None,
    )
    if top_expense_category:
        lines.append(
            f"📂 最大支出分类: {top_expense_category[0]} {label}{top_expense_category[1]:.2f}"
        )
    if top_income_category:
        lines.append(
            f"📥 主要收入来源: {top_income_category[0]} {label}{top_income_category[1]:.2f}"
        )
    if metrics.top_expense is not None:
        title       = metrics.top_expense.title or "未命名支出"
        ledger_date = metrics.top_expense.ledger_date or period.start_date
        lines.append(
            f"🔥 最大单笔支出: {title} {label}{metrics.top_expense_amount:.2f} ({ledger_date})"
        )


async def _generate_briefing_content(user_id: str, db: Database) -> str:
    """批量读取当天条目，并生成用户本地时区的每日简报。"""

    user_now = now_in_timezone(user_id, db)
    today = user_now.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    tomorrow = today + timedelta(days=1)
    events, tasks, overdue_tasks = cast(
        tuple[list[EventItem], list[TaskItem], list[TaskItem]],
        await run_sync(
            db.get_briefing_items,
            user_id,
            today.isoformat(),
            tomorrow.isoformat(),
        ),
    )

    collection_ids = [
        event.event_collection_id for event in events if event.event_collection_id is not None
    ]
    collections = (
        cast(
            dict[str, dict[str, Any]],
            await run_sync(db.get_event_collections_by_ids, user_id, collection_ids),
        )
        if collection_ids
        else {}
    )
    user_timezone = TimezoneHelper.get_user_timezone(user_id, db)
    event_entries = _build_briefing_event_entries(
        events,
        collections,
        user_timezone,
        today,
        tomorrow,
    )
    briefing = _format_daily_briefing(event_entries, tasks, user_now)

    if overdue_tasks:
        overdue_lines = ["", f"⚠️ 逾期待办 ({len(overdue_tasks)}项):"]
        for task in overdue_tasks[:3]:
            deadline = _normalize_briefing_datetime(task.deadline_at, user_timezone)
            if deadline is not None:
                overdue_lines.append(
                    f"  - {task.title or '无标题'} (截止: {deadline.strftime('%m-%d')})"
                )
        briefing = f"{briefing}\n" + "\n".join(overdue_lines)

    return briefing


def _normalize_briefing_datetime(value: str | None, user_timezone: tzinfo) -> datetime | None:
    """将 ISO 时间归一化为用户时区的无时区对象，非法值直接忽略。"""

    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(user_timezone).replace(tzinfo=None)
    return parsed


def _build_briefing_event_entries(
    events: list[EventItem],
    collections: Mapping[str, Mapping[str, Any]],
    user_timezone: tzinfo,
    today: datetime,
    tomorrow: datetime,
) -> list[_BriefingEvent]:
    """构建当天日程；集合标题来自一次批量查询。"""

    entries: list[_BriefingEvent] = []
    for event in events:
        title = event.title or "无标题"
        if event.event_collection_id:
            collection_title = collections.get(event.event_collection_id, {}).get("title")
            if isinstance(collection_title, str) and collection_title:
                title = f"{collection_title} · {title}"
        start_time = _normalize_briefing_datetime(event.start_time, user_timezone)
        if start_time is not None and today <= start_time < tomorrow:
            entries.append(
                _BriefingEvent(
                    start_time,
                    start_time.strftime("%H:%M"),
                    title,
                    event.location,
                )
            )

    entries.sort(key=lambda event: event.sort_time)
    return entries


def _format_daily_briefing(
    events: list[_BriefingEvent],
    tasks: list[TaskItem],
    current_dt: datetime,
) -> str:
    """格式化每日简报，不执行数据库或网络操作。"""
    current_date  = current_dt.strftime("%Y年%m月%d日")
    weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    weekday       = weekday_names[current_dt.weekday()]

    lines = [f"☀️ 早上好！今天是{current_date} {weekday}", ""]

    if events:
        lines.append("🗓️ **今日日程**")
        for event in events[:5]:
            location = f" @{event.location}" if event.location else ""
            lines.append(f"  • {event.time_text} {event.title}{location}")
        if len(events) > 5:
            lines.append(f"  ...还有 {len(events) - 5} 项")
        lines.append("")
    else:
        lines.append("🗓️ 今日暂无日程安排")
        lines.append("")

    if tasks:
        lines.append("✅ **今日待办**")
        for task in tasks[:5]:
            title         = task.title or "无标题"
            priority      = task.priority if isinstance(task.priority, int) else 3
            priority_mark = "🔴" if priority <= 2 else "🟡" if priority == 3 else "⚪"
            lines.append(f"  {priority_mark} {title}")
        if len(tasks) > 5:
            lines.append(f"  ...还有 {len(tasks) - 5} 项")
        lines.append("")
    else:
        lines.append("✅ 今日暂无待办事项")
        lines.append("")

    lines.append("🌟 祝你今天工作顺利！")
    return "\n".join(lines)


async def migrate_undone_todos(context: object, db: Database) -> ActionList:
    """按用户本地日期，原子迁移昨天仍未完成的待办。"""

    messages: ActionList = []
    try:
        user_ids = await _get_active_user_ids(db)
    except Exception as exc:
        public_error_message(
            context,
            exc,
            logger    = logger,
            component = "pendo.scheduled.todo_migration",
        )
        return messages

    for user_id in user_ids:
        try:
            current_time = now_in_timezone(user_id, db)
            yesterday = (current_time - timedelta(days=1)).date().isoformat()
            today          = current_time.date().isoformat()
            migrated_count = cast(
                int,
                await run_sync(
                    db.migrate_undone_tasks_to_date,
                    user_id,
                    yesterday,
                    today,
                ),
            )
            if migrated_count > 0:
                await _send_private_or_collect(
                    context,
                    messages,
                    user_id,
                    f"📋 已将昨天的 {migrated_count} 个未完成待办迁移到今天"
                    "\n\n💡 使用 /pendo todo list today 查看",
                )
            logger.info(
                "Migrated %s undone todos for user %s from %s to %s",
                migrated_count,
                user_id,
                yesterday,
                today,
            )
        except Exception as exc:
            public_error_message(
                context,
                exc,
                logger    = logger,
                component = "pendo.scheduled.todo_migration_user",
            )

    return messages


def cleanup_reminder_singleton() -> None:
    """清除提醒服务单例；插件 cleanup 必须调用以释放旧数据库绑定。"""
    global _reminder_service_singleton
    _reminder_service_singleton = None
