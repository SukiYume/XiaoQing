"""读取 ``config.plugins.xiaoqing_life``；数值越界时钳制到边界，类型错误时回退到默认值。"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

PLUGIN_NAME = "xiaoqing_life"

DEFAULT_SEED_INTERESTS = (
    "天文观测",
    "物理科普",
    "新番",
    "独立游戏",
    "塞尔达",
    "空洞骑士",
    "星露谷",
    "轻小说",
    "历史冷知识",
    "米津玄师",
    "lo-fi",
    "装机",
    "猫",
)

SOURCE_DEFAULTS: dict[str, bool] = {
    "bili_popular" : True,
    "bili_ranking" : True,
    "bili_search"  : True,
    "bili_comments": True,
    "zhihu_hot"    : True,
    "weibo_hot"    : True,
    "baidu_hot"    : False,
    "baike_lookup" : True,
}

# 每种活动名义上的浏览时长（分钟，闭区间，会话开始时随机取一个）。
SESSION_MINUTES: dict[str, tuple[int, int]] = {
    "hot_scroll"     : (8, 20),
    "interest_dive"  : (15, 35),
    "thread_continue": (15, 30),
    "term_lookup"    : (5, 10),
}

# 作息活动文字里的关键词表（``Presence.activity``）：anime 偏向动画话题，meal 偏向随手刷热榜且条目减半，
# no_browse 即使标为空闲也不上网。
ACTIVITY_AFFINITY: dict[str, tuple[str, ...]] = {
    "anime"    : ("刷番", "番"),
    "meal"     : ("吃饭", "午饭", "晚饭", "早饭"),
    "no_browse": ("社", "活动", "课", "实验", "考试"),
}

_QUIET_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")


def _default_sources() -> dict[str, bool]:
    return dict(SOURCE_DEFAULTS)


@dataclass(frozen=True, slots=True)
class LifeConfig:
    enabled                : bool = False
    sources                : Mapping[str, bool] = field(default_factory=_default_sources)
    seed_interests         : tuple[str, ...] = DEFAULT_SEED_INTERESTS
    max_sessions_per_day   : int             = 5
    max_llm_calls_per_day  : int             = 24
    min_session_gap_minutes: float           = 75.0
    session_probability    : float           = 0.5
    # 滚动窗口内累计上网时长达到上限就休息。
    activity_window_minutes: float = 180.0
    activity_limit_minutes : float = 45.0
    failure_backoff_minutes: float = 60.0
    browse_when_busy       : bool  = False
    # 读不到人设卡〔作息〕时的睡眠时段。
    quiet_hours          : str                           = "00:30-08:00"
    wind_down_minutes    : float                         = 60.0
    max_items_per_session: int                           = 6
    max_comments_per_item: int                           = 12
    max_watch_per_session: int                           = 2
    activity_affinity    : Mapping[str, tuple[str, ...]] = field(
        default_factory=lambda: dict(ACTIVITY_AFFINITY)
    )
    learning_keep_days: int                           = 180
    choice_temperature: float                         = 8.0
    session_minutes   : Mapping[str, tuple[int, int]] = field(
        default_factory=lambda: dict(SESSION_MINUTES)
    )
    max_vision_per_session: int             = 2
    max_open_threads      : int             = 2
    thread_max_steps      : int             = 3
    episode_detail_days   : int             = 7
    episode_keep_days     : int             = 45
    term_candidate_days   : int             = 14
    term_fading_days      : int             = 30
    term_forget_days      : int             = 90
    active_use_probability: float           = 0.3
    suggest_cooldown_hours: float           = 6.0
    memes_enabled         : bool            = True
    meme_auto_approve     : bool            = True
    max_memes             : int             = 200
    max_meme_bytes        : int             = 1048576
    blocked_keywords      : tuple[str, ...] = ()

    def source_on(self, name: str) -> bool:
        return bool(self.sources.get(name, False))

    def quiet_window(self) -> tuple[int, int]:
        """``quiet_hours`` 的起止分钟数；写错时用默认值。"""

        match = _QUIET_RE.match(self.quiet_hours) or _QUIET_RE.match("00:30-08:00")
        assert match is not None
        a, b, c, d = (int(part) for part in match.groups())
        return min(a, 24) * 60 + min(b, 59), min(c, 24) * 60 + min(d, 59)


def _bool(value: Any, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _int(value: Any, default: int, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return min(maximum, max(minimum, value))


def _float(value: Any, default: float, *, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    number = float(value)
    if not math.isfinite(number):
        return default
    return min(maximum, max(minimum, number))


def _words(value: Any, default: tuple[str, ...], *, limit: int) -> tuple[str, ...]:
    if not isinstance(value, list):
        return default
    words = [item.strip()[:24] for item in value if isinstance(item, str) and item.strip()]
    return tuple(dict.fromkeys(words))[:limit]


def _sources(value: Any) -> dict[str, bool]:
    merged = _default_sources()
    if isinstance(value, Mapping):
        for name in merged:
            merged[name] = _bool(value.get(name), merged[name])
    return merged


def _quiet(value: Any, default: str) -> str:
    return value.strip() if isinstance(value, str) and _QUIET_RE.match(value) else default


def _limits(o: Mapping[str, Any], d: LifeConfig) -> dict[str, Any]:
    """所有数值字段按「默认值、下界、上界」表统一钳制。"""

    int_spec: dict[str, tuple[int, int]] = {
        "max_sessions_per_day"  : (0, 48),
        "max_llm_calls_per_day" : (0, 500),
        "max_items_per_session" : (1, 12),
        "max_comments_per_item" : (0, 20),
        "max_watch_per_session" : (0, 3),
        "max_vision_per_session": (0, 6),
        "max_open_threads"      : (0, 5),
        "thread_max_steps"      : (1, 10),
        "episode_detail_days"   : (1, 60),
        "episode_keep_days"     : (1, 365),
        "term_candidate_days"   : (1, 365),
        "term_fading_days"      : (1, 365),
        "term_forget_days"      : (1, 730),
        "max_memes"             : (0, 2000),
        "learning_keep_days"    : (1, 730),
        "max_meme_bytes"        : (1024, 8 * 1024 * 1024),
    }
    float_spec: dict[str, tuple[float, float]] = {
        "min_session_gap_minutes": (0.0, 1440.0),
        "session_probability"    : (0.0, 1.0),
        "choice_temperature"     : (0.5, 100.0),
        "activity_window_minutes": (15.0, 1440.0),
        "activity_limit_minutes" : (1.0, 1440.0),
        "failure_backoff_minutes": (0.0, 1440.0),
        "wind_down_minutes"      : (0.0, 360.0),
        "active_use_probability" : (0.0, 1.0),
        "suggest_cooldown_hours" : (0.0, 720.0),
    }
    values: dict[str, Any] = {}
    for name, (low, high) in int_spec.items():
        values[name] = _int(o.get(name), getattr(d, name), minimum=low, maximum=high)
    for name, (flow, fhigh) in float_spec.items():
        values[name] = _float(o.get(name), getattr(d, name), minimum=flow, maximum=fhigh)
    return values


def _session_minutes(value: Any) -> dict[str, tuple[int, int]]:
    merged = dict(SESSION_MINUTES)
    if isinstance(value, Mapping):
        for name, default in SESSION_MINUTES.items():
            pair = value.get(name)
            if (
                isinstance(pair, list)
                and len(pair) == 2
                and all(isinstance(n, int) and not isinstance(n, bool) for n in pair)
            ):
                low, high = max(1, min(pair)), min(240, max(pair))
                merged[name] = (low, max(low, high))
            else:
                merged[name] = default
    return merged


def _affinity(value: Any) -> dict[str, tuple[str, ...]]:
    merged = dict(ACTIVITY_AFFINITY)
    if isinstance(value, Mapping):
        for name in ACTIVITY_AFFINITY:
            words = value.get(name)
            if isinstance(words, list):
                merged[name] = tuple(w.strip() for w in words if isinstance(w, str) and w.strip())[
                    :20
                ]
    return merged


def parse_life_config(raw: Any) -> LifeConfig:
    o = raw if isinstance(raw, Mapping) else {}
    d = LifeConfig()
    return LifeConfig(
        enabled = _bool(o.get("enabled"), d.enabled),
        sources = _sources(o.get("sources")),
        seed_interests=_words(o.get("seed_interests"), d.seed_interests, limit=60),
        activity_affinity = _affinity(o.get("activity_affinity")),
        session_minutes   = _session_minutes(o.get("session_minutes")),
        browse_when_busy  = _bool(o.get("browse_when_busy"), d.browse_when_busy),
        quiet_hours       = _quiet(o.get("quiet_hours"), d.quiet_hours),
        memes_enabled     = _bool(o.get("memes_enabled"), d.memes_enabled),
        meme_auto_approve = _bool(o.get("meme_auto_approve"), d.meme_auto_approve),
        blocked_keywords=_words(o.get("blocked_keywords"), (), limit=200),
        **_limits(o, d),
    )


def _plugin_options(context: Any) -> Mapping[str, Any]:
    settings = context.get_settings_snapshot()
    plugins  = settings.config.get("plugins", {})
    raw      = plugins.get(PLUGIN_NAME, {}) if isinstance(plugins, Mapping) else {}
    return raw if isinstance(raw, Mapping) else {}


def load_life_config(context: Any) -> LifeConfig:
    """每次调用都读当前配置快照，改 ``enabled`` 后下一次检查即生效。"""

    return parse_life_config(_plugin_options(context))


def route_configured(context: Any, route: str) -> bool:
    """``ai.routes.<route>`` 是否存在；缺失时上网只记“模型未配置”。"""

    ai     = _plugin_options(context).get("ai")
    routes = ai.get("routes") if isinstance(ai, Mapping) else None
    return isinstance(routes, Mapping) and isinstance(routes.get(route), Mapping)
