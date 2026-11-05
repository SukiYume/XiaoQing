"""自我强化：上网的收获 → 偏好、来源可靠性、习惯、好奇边界；以及说法的社交反馈。

所有信号只来自她自己的真实经历（喜欢 / 不喜欢、查实、小课题进展）和群友对她说法的真实反应，
不来自模型的自述。纯函数，时间由调用方传入；状态都放在 ``state`` 里随存储持久化。
"""

from __future__ import annotations

import hashlib
import math
import re
from datetime import datetime
from typing import Any

from .safety import is_blocked, normalize_for_match, overlap, similar

ALPHA              = 0.3
REWARD_MIN         = -1.0
REWARD_MAX         = 3.0
PREF_SCALE         = 8.0
PREF_CLAMP         = 8.0
SOURCE_PREF_SLOPE  = 0.5
FAIL_THRESHOLD     = 2
UNRELIABLE_SECONDS = 6 * 3600
UNRELIABLE_FACTOR  = 0.2
HABIT_MIN_SAMPLES  = 3
HABIT_SLOPE        = 0.3
HABIT_LOW, HABIT_HIGH = 0.7, 1.3
TOPIC_PENALTY = -0.5
MAX_FRONTIER  = 50
# 太泛的标签和带期数 / 编号的栏目名不当作可探索的话题。
GENERIC_TOPICS = frozenset(
    {
        "日常",
        "杂谈",
        "科技",
        "生活",
        "教程",
        "课程",
        "经验分享",
        "分享",
        "vlog",
        "VLOG",
        "知识",
        "搞笑",
        "游戏",
        "音乐",
        "动画",
        "影视",
        "综合",
    }
)
_SERIAL_RE           = re.compile(r"[0-9０-９]|[·・]|第.{1,3}[期集话章]")
FRONTIER_TOPIC_CHARS = 12
FEEDBACK_WINDOW_SEC  = 2 * 3600
MAX_FINGERPRINTS     = 10
HABIT_COOLDOWN_SCALE = 0.5
SUPPRESS_DAYS        = 14
FOLLOW_LINES         = 3

_OK_RE            = re.compile(r"哈哈|笑死|好笑|草|233|确实|学到了|懂了|好家伙|绝了|太对")
_CONFUSED_RE      = re.compile(r"啥意思|什么意思|没懂|不懂|是什么梗|啥梗|什么梗")
_ONLY_QUESTION_RE = re.compile(r"^\s*[?？]+\s*$")


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


# ───────────────────────────── 收获 ─────────────────────────────


def session_reward(
    *,
    liked: int,
    disliked: int,
    new_known: int,
    new_expressions: int,
    thread_progress: int,
    memes: int,
) -> float:
    """一次上网的收获；失败和全是看过的不算（由调用方不调用本函数）。"""

    raw = (
        liked
        - 0.5 * disliked
        + 0.5 * new_known
        + 0.3 * new_expressions
        + 1.0 * thread_progress
        + 0.5 * memes
    )
    return clamp(raw, REWARD_MIN, REWARD_MAX)


# ───────────────────────────── 偏好（EMA） ─────────────────────────────


def _prefs(state: dict[str, Any], kind: str) -> dict[str, float]:
    prefs: dict[str, float] = state.setdefault("preferences", {}).setdefault(kind, {})
    return prefs


def update_preference(state: dict[str, Any], kind: str, key: str, reward: float) -> float:
    """``v ← v + α(reward − v)``；第一次直接取 reward。返回新值。"""

    prefs      = _prefs(state, kind)
    old        = prefs.get(key)
    prefs[key] = round(reward if old is None else old + ALPHA * (reward - old), 4)
    return prefs[key]


def _mean(values: dict[str, float]) -> float:
    return sum(values.values()) / len(values) if values else 0.0


def pref_adjust(state: dict[str, Any], kind: str, key: str) -> float:
    """打分加成 ``clamp(8×(v−均值), −8, +8)``；还没有记录的项是 0。"""

    prefs = state.get("preferences", {}).get(kind, {})
    if key not in prefs:
        return 0.0
    return clamp(PREF_SCALE * (prefs[key] - _mean(prefs)), -PREF_CLAMP, PREF_CLAMP)


def source_pref_factor(state: dict[str, Any], source: str) -> float:
    """热榜来源权重的乘数 ``exp(0.5×(v−均值))``。"""

    prefs = state.get("preferences", {}).get("source", {})
    if source not in prefs:
        return 1.0
    return math.exp(SOURCE_PREF_SLOPE * (prefs[source] - _mean(prefs)))


# ───────────────────────────── 来源可靠性 ─────────────────────────────


def record_source(state: dict[str, Any], source: str, ok: bool, now: float) -> None:
    """同一来源连续失败 ≥2 次：6 小时内权重 ×0.2，之后自动恢复。不重试、不绕过风控。"""

    health = state.setdefault("source_health", {}).setdefault(source, {"fails": 0, "until": 0.0})
    if ok:
        health["fails"] = 0
        return
    if health.get("until", 0.0) and now >= float(health["until"]):
        health["fails"], health["until"] = 0, 0.0  # 上次惩罚已过期，从头数
    health["fails"] = int(health.get("fails", 0)) + 1
    if health["fails"] >= FAIL_THRESHOLD:
        health["until"] = now + UNRELIABLE_SECONDS


def health_factor(state: dict[str, Any], source: str, now: float) -> float:
    health = state.get("source_health", {}).get(source)
    if health and now < float(health.get("until", 0.0)):
        return UNRELIABLE_FACTOR
    return 1.0


# ───────────────────────────── 习惯 ─────────────────────────────


def habit_bucket(moment: datetime) -> str:
    return f"{'wd' if moment.weekday() < 5 else 'we'}{moment.hour:02d}"


def record_habit(state: dict[str, Any], moment: datetime, reward: float) -> None:
    habits        = state.setdefault("habits", {})
    bucket        = habits.setdefault(habit_bucket(moment), {"n": 0, "sum": 0.0})
    bucket["n"]   = int(bucket["n"]) + 1
    bucket["sum"] = round(float(bucket["sum"]) + reward, 4)


def _overall(habits: dict[str, Any]) -> float:
    n = sum(int(b["n"]) for b in habits.values())
    return sum(float(b["sum"]) for b in habits.values()) / n if n else 0.0


def habit_multiplier(state: dict[str, Any], moment: datetime) -> float:
    """这一刻（工作日 / 周末 × 小时）收获高于平均就更愿意出发，低于平均则更懒；样本不足为 1。"""

    habits = state.get("habits", {})
    bucket = habits.get(habit_bucket(moment))
    if not bucket or int(bucket["n"]) < HABIT_MIN_SAMPLES:
        return 1.0
    mean = float(bucket["sum"]) / int(bucket["n"])
    return clamp(1.0 + HABIT_SLOPE * (mean - _overall(habits)), HABIT_LOW, HABIT_HIGH)


def habit_text(state: dict[str, Any]) -> str:
    """最明显的习惯的一句话；没有足够样本就是空。"""

    habits                         = state.get("habits", {})
    overall                        = _overall(habits)
    best: tuple[float, str] | None = None
    for bucket, data in habits.items():
        if int(data["n"]) < HABIT_MIN_SAMPLES:
            continue
        mean = float(data["sum"]) / int(data["n"])
        if mean > overall and (best is None or mean > best[0]):
            best = (mean, bucket)
    if best is None:
        return ""
    kind, hour = ("工作日" if best[1].startswith("wd") else "周末"), int(best[1][2:])
    return f"{kind}{hour}点前后刷得最有收获，渐渐成了习惯"


# ───────────────────────────── 好奇边界 ─────────────────────────────


def _strong(interests: dict[str, dict[str, Any]], topic: str) -> bool:
    for key, entry in interests.items():
        if (key == topic or similar(key, topic)) and entry.get("stage") in (
            "repeated",
            "consistent",
        ):
            return True
    return False


def grow_frontier(
    frontier: list[dict[str, Any]],
    interests: dict[str, dict[str, Any]],
    liked_rows: list[dict[str, Any]],
    extra_words: dict[str, list[str]],
    blocked: tuple[str, ...],
    now: float,
) -> int:
    """喜欢的经历带来新的可探索话题：视频标签、消化话题、小课题关键词。返回新增条数。"""

    added = 0
    for row in liked_rows:
        words = [*row.get("tags", []), *row.get("topics", []), *extra_words.get(row["id"], [])]
        for raw in words:
            topic = str(raw).strip()[:FRONTIER_TOPIC_CHARS]
            if len(topic) < 2 or topic in GENERIC_TOPICS or _SERIAL_RE.search(topic):
                continue
            if is_blocked(topic, blocked) or _strong(interests, topic):
                continue
            if any(f["topic"] == topic or similar(f["topic"], topic) for f in frontier):
                continue
            if topic in interests:
                continue
            frontier.append(
                {"topic": topic, "from_episode": row["id"], "added_at": now, "tried": False}
            )
            added += 1
    if len(frontier) > MAX_FRONTIER:
        frontier.sort(key=lambda f: (not f.get("tried"), float(f.get("added_at", 0.0))))
        del frontier[: len(frontier) - MAX_FRONTIER]  # 先丢试过的、最旧的
        frontier.sort(key=lambda f: float(f.get("added_at", 0.0)))
    return added


def frontier_weights(frontier: list[dict[str, Any]], now: float) -> dict[str, float]:
    """未试过的 frontier 话题，越近期喜欢的越可能。"""

    return {
        f["topic"]: 1.0 / (1.0 + max(0.0, now - float(f.get("added_at", now))) / 86400.0)
        for f in frontier
        if not f.get("tried")
    }


def mark_tried(frontier: list[dict[str, Any]], topic: str) -> None:
    for entry in frontier:
        if entry["topic"] == topic:
            entry["tried"] = True


# ───────────────────────────── 说法的社交反馈 ─────────────────────────────


def fingerprint(text: str) -> str:
    return hashlib.sha1(normalize_for_match(text).encode("utf-8")).hexdigest()[:12]


def _classify(followers: list[str], key: str) -> str:
    """她那句话之后的几句里：ok（笑、认同、跟着用）、confused（没懂）或空。"""

    ok = confused = False
    for line in followers:
        if _CONFUSED_RE.search(line) or _ONLY_QUESTION_RE.match(line):
            confused = True
        elif _OK_RE.search(line) or (key and key in normalize_for_match(line)):
            ok = True
    if ok:
        return "ok"
    return "confused" if confused else ""


def _followers(recent: list[str], bot_lines: set[str], index: int) -> list[str]:
    after: list[str] = []
    for line in recent[index + 1 :]:
        if line in bot_lines:
            break
        after.append(line)
        if len(after) >= FOLLOW_LINES:
            break
    return after


def _record_use(entry: dict[str, Any], verdict: str, now: float) -> None:
    if verdict == "ok":
        entry["use_ok"] = int(entry.get("use_ok", 0)) + 1
    else:
        entry["use_confused"] = int(entry.get("use_confused", 0)) + 1
    if int(entry.get("use_confused", 0)) >= 2 and int(entry.get("use_ok", 0)) == 0:
        entry["suppressed_until"] = now + SUPPRESS_DAYS * 86400.0


def is_habit(entry: dict[str, Any]) -> bool:
    return int(entry.get("use_ok", 0)) >= 2


def is_suppressed(entry: dict[str, Any], now: float) -> bool:
    return now < float(entry.get("suppressed_until", 0.0))


def apply_feedback(
    terms: dict[str, dict[str, Any]],
    expressions: list[dict[str, Any]],
    recent: list[str],
    bot_recent: list[str],
    now: float,
) -> bool:
    """用请求里按顺序排列的 ``recent`` 找出她用过的词条 / 表达后群友的反应；返回是否有变化。

    只评最近 2 小时内推荐过的；同一条回复对同一项只评一次（存回复文本的指纹）。
    """

    bot_lines = {line for line in bot_recent if line}
    changed   = False
    for index, line in enumerate(recent):
        if line not in bot_lines:
            continue
        folded    = normalize_for_match(line)
        followers = _followers(recent, bot_lines, index)
        if not followers:
            continue
        print_ = fingerprint(line)
        for key, entry in terms.items():
            changed |= _judge(entry, key, key in folded, followers, print_, now)
        for entry in expressions:
            pattern = str(entry.get("pattern", ""))
            used    = overlap(line, pattern) >= 3
            changed |= _judge(entry, normalize_for_match(pattern)[:6], used, followers, print_, now)
    return changed


def _judge(
    entry: dict[str, Any], key: str, used: bool, followers: list[str], print_: str, now: float
) -> bool:
    if not used or now - float(entry.get("last_suggested", 0.0)) > FEEDBACK_WINDOW_SEC:
        return False
    seen: list[str] = entry.setdefault("fps", [])
    if print_ in seen:
        return False
    verdict = _classify(followers, key if len(key) >= 2 else "")
    if not verdict:
        return False
    seen.append(print_)
    del seen[:-MAX_FINGERPRINTS]
    _record_use(entry, verdict, now)
    return True
