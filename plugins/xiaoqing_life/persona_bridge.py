"""读 xiaoqing_chat 的人设卡事实与〔作息〕；读不到就退回本插件配置，不阻止插件加载。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from core.clock import now_in_configured_timezone

from .config import LifeConfig

logger = logging.getLogger(__name__)

Availability = Literal["free", "busy", "asleep"]

_CHAT_DIR        = Path(__file__).resolve().parent.parent / "xiaoqing_chat"
_LOOKAHEAD_MIN   = 24 * 60
_SCAN_STEP_MIN   = 5
_WAKE_WINDOW_MIN = 120
PLAN_HORIZON_MIN = 12 * 60
_DEFAULT_NAME    = "小青"


@dataclass(frozen=True, slots=True)
class Presence:
    availability: Availability
    activity    : str
    # 距离下一次睡觉还有多少分钟；当前已在睡觉时为 0，24 小时内没有睡眠时段为 None。
    minutes_until_sleep: int | None
    # 睡眠时段结束后过了多少分钟（只看最近 2 小时）；没有或仍在睡为 None。
    minutes_since_wake: int | None = None


def now(context: Any) -> datetime:
    """一律配置时区；宿主机时区与用户不同，不用 ``time.localtime``。"""

    return now_in_configured_timezone(context)


def bot_name(context: Any) -> str:
    try:
        name = str(context.get_settings_snapshot().config.get("bot_name") or "").strip()
    except Exception:
        name = ""
    return name or _DEFAULT_NAME


def persona_card(context: Any) -> Any | None:
    """人设卡；xiaoqing_chat 不可导入或配置读失败时为 None。"""

    try:
        from plugins.xiaoqing_chat.config.config import load_xiaoqing_chat_config

        config = load_xiaoqing_chat_config(
            context_config = context.get_settings_snapshot().config,
            plugin_dir     = _CHAT_DIR,
        )
        return config.personality.card
    except Exception as exc:
        logger.debug("xiaoqing_life persona unavailable error_type=%s", type(exc).__name__)
        return None


def persona_facts(context: Any) -> str:
    card = persona_card(context)
    if card is None:
        return ""
    try:
        return str(card.facts(bot_name(context)))
    except Exception:
        return ""


def _quiet_presence(cfg: LifeConfig, moment: datetime) -> Presence:
    """没有〔作息〕时只按 ``quiet_hours`` 区分睡眠与空闲。"""

    start, end = cfg.quiet_window()

    def asleep_at(point: datetime) -> bool:
        minute = point.hour * 60 + point.minute
        if start <= end:
            return start <= minute < end
        return minute >= start or minute < end

    if asleep_at(moment):
        return Presence("asleep", "睡觉", 0)
    since = _since_wake(asleep_at, moment)
    for offset in range(_SCAN_STEP_MIN, _LOOKAHEAD_MIN + 1, _SCAN_STEP_MIN):
        if asleep_at(moment + timedelta(minutes=offset)):
            return Presence("free", "", offset, since)
    return Presence("free", "", None, since)


def _since_wake(asleep_at: Any, moment: datetime) -> int | None:
    """往回扫 2 小时，找到仍在睡觉的最近时刻，当前已醒时返回醒来后的分钟数。"""

    if asleep_at(moment):
        return None
    for offset in range(_SCAN_STEP_MIN, _WAKE_WINDOW_MIN + 1, _SCAN_STEP_MIN):
        if asleep_at(moment - timedelta(minutes=offset)):
            return offset
    return None


def _routine_presence(card: Any, moment: datetime) -> Presence | None:
    from plugins.xiaoqing_chat.life.schedule import routine_slot_now

    if not getattr(card, "routine", ()):
        return None
    slot = routine_slot_now(card, moment)
    if slot is not None and slot.availability == "asleep":
        return Presence("asleep", slot.activity, 0)
    until: int | None = None
    for offset in range(_SCAN_STEP_MIN, _LOOKAHEAD_MIN + 1, _SCAN_STEP_MIN):
        ahead = routine_slot_now(card, moment + timedelta(minutes=offset))
        if ahead is not None and ahead.availability == "asleep":
            until = offset
            break

    def asleep_at(point: datetime) -> bool:
        found = routine_slot_now(card, point)
        return found is not None and found.availability == "asleep"

    since = _since_wake(asleep_at, moment)
    if slot is None:
        return Presence("free", "", until, since)
    return Presence(slot.availability, slot.activity, until, since)


def upcoming_plans(context: Any, moment: datetime, limit: int = 3) -> list[str]:
    """今天剩下的作息时段（不含睡觉），形如 ``20:00 社团活动``；读不到作息就是空。"""

    card = persona_card(context)
    if card is None or not getattr(card, "routine", ()):
        return []
    try:
        from plugins.xiaoqing_chat.life.schedule import parse_routine

        slots = parse_routine(tuple(card.routine))
    except Exception:
        return []
    now_minute = moment.hour * 60 + moment.minute
    ahead      = sorted(
        (
            s
            for s in slots
            if moment.weekday() in s.days
            and s.availability != "asleep"
            and 0 < s.start - now_minute <= PLAN_HORIZON_MIN
        ),
        key=lambda s: s.start,
    )
    return [f"{s.start // 60:02d}:{s.start % 60:02d} {s.activity}" for s in ahead[:limit]]


def availability(context: Any, cfg: LifeConfig, moment: datetime) -> Presence:
    """此刻是空闲、在忙还是睡着，以及离睡觉还有多久。任何异常都按读不到作息处理。"""

    card = persona_card(context)
    if card is not None:
        try:
            presence = _routine_presence(card, moment)
            if presence is not None:
                return presence
        except Exception as exc:
            logger.debug("xiaoqing_life routine unreadable error_type=%s", type(exc).__name__)
    return _quiet_presence(cfg, moment)
