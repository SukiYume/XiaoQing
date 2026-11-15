"""读取 ``config.plugins.xiaoqing_memory``；数值越界时钳制到边界，类型不对时用默认值。"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from core.ai import declare_route_defaults

PLUGIN_NAME = "xiaoqing_memory"

declare_route_defaults(
    PLUGIN_NAME,
    {
        # 夜间整理：写笔记、并页面、核对。离线任务，不赶时间。
        "reasoning": {
            "temperature"           : 0.3,
            "top_p"                 : 0.9,
            "max_tokens"            : 3000,
            "timeout_seconds"       : 60,
            "total_timeout_seconds" : 180,
            "max_retry"             : 1,
            "retry_interval_seconds": 2,
        },
    },
)

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


@dataclass(frozen=True, slots=True)
class MemoryConfig:
    enabled: bool = True
    # 原始记录保留多少天（按整月删）。
    event_keep_days: int = 365
    # 夜间整理窗口（本地时间，分钟数）；积压够多时窗口外也整理。
    night_start_minute    : int = 2 * 60
    night_end_minute      : int = 5 * 60
    backlog_trigger_events: int = 150
    # 每天整理用的模型调用上限，先占后用。
    max_llm_calls_per_day : int = 16
    max_page_rewrites     : int = 6
    verify_calls_per_night: int = 2
    batch_chars           : int = 6000
    # 每轮常驻块的字数预算。
    self_budget   : int = 800
    speaker_budget: int = 500
    group_budget  : int = 300
    hits_budget   : int = 500
    # 每个对象每一类（人物的事实、偏好、话头算一类，办事记录另算）最多保留的有效笔记；
    # 超出时先丢最久没印证也没用过的。
    max_notes_per_subject: int = 40
    stale_days           : int = 180
    superseded_keep_days : int = 90
    legacy_keep_days     : int = 180
    task_note_days       : int = 90


def _int(raw: Mapping[str, Any], key: str, default: int, low: int, high: int) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return int(min(high, max(low, number)))


def _minute(value: Any, default: int) -> int:
    match = _TIME_RE.fullmatch(str(value or "").strip())
    if match is None:
        return default
    return int(match.group(1)) * 60 + int(match.group(2))


def parse_memory_config(raw: Mapping[str, Any]) -> MemoryConfig:
    base = MemoryConfig()
    return MemoryConfig(
        enabled                = raw.get("enabled", True) is not False,
        event_keep_days        = _int(raw, "event_keep_days", base.event_keep_days, 31, 3650),
        night_start_minute     = _minute(raw.get("night_start"), base.night_start_minute),
        night_end_minute       = _minute(raw.get("night_end"), base.night_end_minute),
        backlog_trigger_events = _int(
            raw, "backlog_trigger_events", base.backlog_trigger_events, 10, 5000
        ),
        max_llm_calls_per_day=_int(
            raw, "max_llm_calls_per_day", base.max_llm_calls_per_day, 0, 500
        ),
        max_page_rewrites      = _int(raw, "max_page_rewrites", base.max_page_rewrites, 0, 100),
        verify_calls_per_night = _int(
            raw, "verify_calls_per_night", base.verify_calls_per_night, 0, 50
        ),
        batch_chars           = _int(raw, "batch_chars", base.batch_chars, 1000, 30000),
        self_budget           = _int(raw, "self_budget", base.self_budget, 0, 4000),
        speaker_budget        = _int(raw, "speaker_budget", base.speaker_budget, 0, 4000),
        group_budget          = _int(raw, "group_budget", base.group_budget, 0, 4000),
        hits_budget           = _int(raw, "hits_budget", base.hits_budget, 0, 4000),
        max_notes_per_subject = _int(
            raw, "max_notes_per_subject", base.max_notes_per_subject, 5, 500
        ),
        stale_days=_int(raw, "stale_days", base.stale_days, 7, 3650),
        superseded_keep_days=_int(raw, "superseded_keep_days", base.superseded_keep_days, 1, 3650),
        legacy_keep_days=_int(raw, "legacy_keep_days", base.legacy_keep_days, 1, 3650),
        task_note_days=_int(raw, "task_note_days", base.task_note_days, 1, 3650),
    )


def _plugin_options(context: Any) -> Mapping[str, Any]:
    try:
        config = context.get_settings_snapshot().config
    except Exception:
        return {}
    plugins = config.get("plugins") if hasattr(config, "get") else None
    options = plugins.get(PLUGIN_NAME) if isinstance(plugins, Mapping) else None
    return options if isinstance(options, Mapping) else {}


def load_memory_config(context: Any) -> MemoryConfig:
    """每次调用都读当前配置快照，改动在下一次调用时生效。"""

    return parse_memory_config(_plugin_options(context))


def route_configured(context: Any, route: str = "reasoning") -> bool:
    ai     = _plugin_options(context).get("ai")
    routes = ai.get("routes") if isinstance(ai, Mapping) else None
    return isinstance(routes, Mapping) and bool(routes.get(route))
