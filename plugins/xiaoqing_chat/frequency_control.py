from __future__ import annotations

import random
import re as _re
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .runtime_state import _ChatRuntime


_HIGH_WORDS = frozenset({
    "竟然", "没想到", "哈哈", "哈哈哈", "哈哈哈哈", "离谱", "绝了",
    "火锅", "好吃", "好玩", "emo", "好家伙", "真的吗", "真假",
    "wtf", "omg", "啊啊啊", "呜呜", "牛啊", "绝绝子",
})

def _build_high_words_re(words: frozenset) -> "_re.Pattern[str]":
    cjk_pats = []
    latin_pats = []
    for w in words:
        # Latin-only words need word boundaries to avoid substring false positives
        if _re.fullmatch(r'[a-zA-Z0-9]+', w):
            latin_pats.append(_re.escape(w))
        else:
            cjk_pats.append(_re.escape(w))
    parts = []
    if latin_pats:
        parts.append(r'(?<![a-zA-Z0-9])(?:' + '|'.join(latin_pats) + r')(?![a-zA-Z0-9])')
    if cjk_pats:
        parts.append('|'.join(cjk_pats))
    return _re.compile('|'.join(parts), _re.IGNORECASE)

_HIGH_WORDS_RE = _build_high_words_re(_HIGH_WORDS)

_LOW_PATTERN = _re.compile(
    r'^(\s|[\U00010000-\U0010ffff]|'
    r'https?://\S+|'
    r'\d+|'
    r'[^\w\u4e00-\u9fff]'
    r')+$'
)


def _score_interest(text: str) -> str:
    """
    对消息打兴趣度分：'high' | 'neutral' | 'low'

    - high: 含问号/感叹符、以疑问助词结尾（吗/嘛/啊/呢/吧/诶）、含生活感叹词
    - low:  纯表情/链接/数字/标点，或长度 ≤ 2 且无上述高分信号
    - neutral: 其余
    """
    t = (text or "").strip()
    if not t:
        return "low"
    if "?" in t or "？" in t or "!" in t or "！" in t:
        return "high"
    if t.endswith(("吗", "嘛", "啊", "呢", "吧", "诶")):
        return "high"
    if len(t) <= 2:
        return "low"
    if _LOW_PATTERN.fullmatch(t):
        return "low"
    tl = t.lower()
    if _HIGH_WORDS_RE.search(tl):
        return "high"
    return "neutral"


# ---------------------------------------------------------------------------
# Time-based talk_value: returns a 0~1 multiplier based on current hour.
# Mimics MaiBot's per-time-period frequency tuning so the bot talks less at
# night and more during daytime / evening.
# ---------------------------------------------------------------------------

# Default time-period schedule (can be overridden via config)
_DEFAULT_TALK_SCHEDULE: list[tuple[int, int, float]] = [
    # (hour_start, hour_end_exclusive, talk_value)
    (0,  7,  0.3),   # 深夜/凌晨 → 大幅减少发言
    (7,  9,  0.6),   # 早晨起床 → 适度
    (9,  12, 1.0),   # 上午活跃
    (12, 14, 0.8),   # 午饭/午休
    (14, 18, 1.0),   # 下午活跃
    (18, 23, 1.0),   # 晚间活跃
    (23, 24, 0.5),   # 深夜 → 减少
]


def _get_talk_value(schedule: list[tuple[int, int, float]] | None = None) -> float:
    """Return the talk_value for the current hour based on the schedule."""
    hour = time.localtime().tm_hour
    sched = schedule or _DEFAULT_TALK_SCHEDULE
    for start, end, val in sched:
        if start <= hour < end:
            return max(0.0, min(1.0, val))
    return 0.8  # fallback


def _freq_record(chat_id: str, runtime: _ChatRuntime, state, *, forced: bool) -> None:
    now = time.time()
    state.set_last_reply_ts(chat_id, now)
    timestamps = state.get_reply_timestamps(chat_id)
    timestamps.append(now)
    state.set_reply_timestamps(chat_id, timestamps)

    if forced:
        state.set_continuous_reply_count(chat_id, 0)
        return

    c = state.get_continuous_reply_count(chat_id) + 1
    state.set_continuous_reply_count(chat_id, c)
    if runtime.cfg.continuous_reply_limit > 0 and c > runtime.cfg.continuous_reply_limit:
        state.set_continuous_cooldown_until(chat_id, now + max(0.0, runtime.cfg.continuous_cooldown_seconds))
        state.set_continuous_reply_count(chat_id, 0)


def _should_reply(
    runtime: _ChatRuntime,
    state,
    chat_id: str,
    text: str,
    is_private: bool,
    mentioned: bool,
    enable_private_brain_chat: bool,
    interest: str = "neutral",
) -> bool:
    """
    判断是否应该回复消息，基于 heartflow 评分、时间段 talk_value 和动态概率控制。

    改进点（对标 MaiBot）：
    1. 引入 talk_value 时间段调节（深夜少说、白天多说）
    2. 连续不回复时动态降低阈值（积累更多消息后更倾向回复）
    3. interest 分级影响更细腻（连续乘以 talk_value）

    Args:
        runtime: 运行时配置
        state: 全局状态
        chat_id: 聊天ID
        text: 消息文本
        is_private: 是否私聊
        mentioned: 是否被艾特
        enable_private_brain_chat: 是否启用私聊深度对话
        interest: 消息兴趣度分级 ("high"/"neutral"/"low")

    Returns:
        是否应该回复
    """
    now = time.time()
    last = state.get_last_reply_ts(chat_id)
    cooldown_until = state.get_continuous_cooldown_until(chat_id)
    cooldown_left = max(0.0, cooldown_until - now)
    window = [t for t in state.get_reply_timestamps(chat_id) if now - t < 60.0]
    state.set_reply_timestamps(chat_id, window)
    goal = state.goal_store.get(chat_id).goal if runtime.cfg.goal.enable_goal else ""

    # --- Time-based talk_value (MaiBot-style) ---
    config_schedule = None
    if hasattr(runtime.cfg, 'talk_schedule') and runtime.cfg.talk_schedule:
        config_schedule = [
            (entry.hour_start, entry.hour_end, entry.talk_value)
            for entry in runtime.cfg.talk_schedule
        ]
    talk_value = _get_talk_value(config_schedule)

    score = state.heartflow.score(
        chat_id=chat_id,
        text=text,
        goal=goal,
        mentioned=mentioned,
        is_private=is_private,
        replies_last_minute=len(window),
        max_replies_per_minute=runtime.cfg.max_replies_per_minute,
        cooldown_left_seconds=cooldown_left,
        min_reply_interval_seconds=runtime.cfg.min_reply_interval_seconds,
        seconds_since_last_reply=max(0.0, now - last) if last else 9999.0,
        base=runtime.cfg.heartflow.base_score,
        threshold=runtime.cfg.heartflow.threshold,
        enable_random=runtime.cfg.heartflow.enable_random_gate,
        weight_private=runtime.cfg.heartflow.weight_private,
        weight_mentioned=runtime.cfg.heartflow.weight_mentioned,
        weight_question=runtime.cfg.heartflow.weight_question,
        weight_goal_match=runtime.cfg.heartflow.weight_goal_match,
        weight_short_text=runtime.cfg.heartflow.weight_short_text,
        weight_rate_limit=runtime.cfg.heartflow.weight_rate_limit,
        weight_cooldown=runtime.cfg.heartflow.weight_cooldown,
        weight_interval=runtime.cfg.heartflow.weight_interval,
        weight_no_reply_streak=runtime.cfg.heartflow.weight_no_reply_streak,
        weight_long_silence=runtime.cfg.heartflow.weight_long_silence,
    )
    # heartflow 评分检查：分数低于阈值则不回复
    if runtime.cfg.heartflow.enable_heartflow:
        if score < runtime.cfg.heartflow.threshold:
            return False
        # 随机门控：即使分数足够，也按概率随机过滤
        if runtime.cfg.heartflow.enable_random_gate and random.random() >= max(0.0, min(1.0, score)):
            return False

    # --- 概率控制（深度对话模式下跳过此检查）---
    if not (enable_private_brain_chat and is_private):
        p = runtime.cfg.reply_probability_private if is_private else runtime.cfg.reply_probability_base

        # Apply time-based talk_value multiplier (MaiBot-style)
        if not is_private:
            p = p * talk_value

        # Interest-based adjustment (more granular than before)
        if not is_private:
            if interest == "high":
                p = min(p * 1.5, 0.85)
            elif interest == "low":
                p = p * 0.15

        # --- Dynamic threshold: consecutive no-reply lowers the bar ---
        # (MaiBot-style: after many skipped messages, become more willing to reply)
        try:
            no_reply_count = int(getattr(state, '_get_no_reply_streak', lambda cid: 0)(chat_id))
        except (TypeError, ValueError):
            no_reply_count = 0
        if no_reply_count >= 5:
            # Boost probability after 5+ consecutive skips
            p = min(p * 1.4, 0.9)
        elif no_reply_count >= 3:
            p = min(p * 1.2, 0.85)

        if random.random() >= max(0.0, min(1.0, p)):
            return False
    return True
