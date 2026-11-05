"""词条、表达、经历、已看过条目与兴趣的生命周期规则（纯函数，时间由调用方传入）。

只认来自外部材料的证据：模型写的总结、日记、小青自己的回复都不是来源。
"""

from __future__ import annotations

import math
from typing import Any

from .config import LifeConfig
from .safety import normalize_for_match, similar
from .stores import MAX_EXPRESSIONS, MAX_INTERESTS, MAX_TERMS, SEEN_DAYS, new_id

DAY                     = 86400.0
MAX_SOURCES             = 20
MAX_EVIDENCE            = 20
INTEREST_HALF_LIFE_DAYS = 30.0


# ───────────────────────────── 词条 ─────────────────────────────


def term_key(term: str) -> str:
    return normalize_for_match(term)


def _add_source(entry: dict[str, Any], item_id: str) -> bool:
    """同一 item_id 不重复计数；返回是否新增了独立来源。"""

    sources: list[str] = entry.setdefault("sources", [])
    if item_id in sources:
        return False
    sources.append(item_id)
    del sources[:-MAX_SOURCES]
    return True


def upsert_term(
    terms: dict[str, dict[str, Any]],
    fields: dict[str, Any],
    item_id: str,
    now: float,
    *,
    verified: bool = False,
) -> dict[str, Any] | None:
    """新词进 ``candidate``；已知词只追加来源与时间，不改含义。"""

    key = term_key(str(fields.get("term", "")))
    if not key:
        return None
    entry = terms.get(key)
    if entry is None:
        entry = {
            "term"          : str(fields["term"])[:24],
            "meaning"       : str(fields.get("meaning", ""))[:120],
            "usage"         : str(fields.get("usage", ""))[:80],
            "tone"          : str(fields.get("tone", "中性"))[:6],
            "status"        : "candidate",
            "verified"      : False,
            "sources"       : [],
            "first_seen"    : now,
            "last_seen"     : now,
            "last_suggested": 0.0,
        }
        terms[key] = entry
    _add_source(entry, item_id)
    entry["last_seen"] = max(float(entry.get("last_seen", 0.0)), now)
    entry["verified"]  = bool(entry.get("verified")) or verified
    return entry


def _status_for(entry: dict[str, Any], now: float, cfg: LifeConfig) -> str | None:
    """None 表示该删除。"""

    idle = now - float(entry.get("last_seen", now))
    if idle > cfg.term_forget_days * DAY:
        return None
    known = len(entry.get("sources", [])) >= 2 or bool(entry.get("verified"))
    if not known:
        age = now - float(entry.get("first_seen", now))
        return None if age > cfg.term_candidate_days * DAY else "candidate"
    return "fading" if idle > cfg.term_fading_days * DAY else "known"


def refresh_terms(terms: dict[str, dict[str, Any]], now: float, cfg: LifeConfig) -> None:
    """重算状态、删除过期词条，并把总量压到上限（先丢最久没看到的）。"""

    for key in list(terms):
        status = _status_for(terms[key], now, cfg)
        if status is None:
            del terms[key]
        else:
            terms[key]["status"] = status
    if len(terms) > MAX_TERMS:
        oldest = sorted(terms, key=lambda k: float(terms[k].get("last_seen", 0.0)))
        for key in oldest[: len(terms) - MAX_TERMS]:
            del terms[key]


# ───────────────────────────── 表达 ─────────────────────────────


def upsert_expression(
    expressions: list[dict[str, Any]],
    fields: dict[str, Any],
    item_id: str,
    now: float,
) -> dict[str, Any] | None:
    key = normalize_for_match(str(fields.get("pattern", "")))
    if not key:
        return None
    for entry in expressions:
        if normalize_for_match(str(entry.get("pattern", ""))) == key:
            _add_source(entry, item_id)
            entry["last_seen"] = max(float(entry.get("last_seen", 0.0)), now)
            return entry
    entry = {
        "id"            : new_id("x"),
        "situation"     : str(fields.get("situation", ""))[:60],
        "pattern"       : str(fields.get("pattern", ""))[:100],
        "kind"          : "move" if fields.get("kind") == "move" else "phrase",
        "sources"       : [item_id],
        "first_seen"    : now,
        "last_seen"     : now,
        "last_suggested": 0.0,
    }
    expressions.append(entry)
    return entry


def expression_status(entry: dict[str, Any], now: float, cfg: LifeConfig) -> str:
    idle = now - float(entry.get("last_seen", now))
    if idle > cfg.term_fading_days * DAY:
        return "fading"
    return "known" if len(entry.get("sources", [])) >= 2 else "candidate"


def refresh_expressions(expressions: list[dict[str, Any]], now: float, cfg: LifeConfig) -> None:
    expressions[:] = [
        e for e in expressions if now - float(e.get("last_seen", now)) <= cfg.term_forget_days * DAY
    ]
    for entry in expressions:
        entry["status"] = expression_status(entry, now, cfg)
    if len(expressions) > MAX_EXPRESSIONS:
        expressions.sort(key=lambda e: float(e.get("last_seen", 0.0)), reverse=True)
        del expressions[MAX_EXPRESSIONS:]


# ───────────────────────────── 经历与已看过 ─────────────────────────────


def prune_episodes(
    episodes: list[dict[str, Any]], now: float, cfg: LifeConfig, protected: set[str]
) -> None:
    """超过保留期的经历删除；进行中小课题引用的经历保留。"""

    cutoff      = now - cfg.episode_keep_days * DAY
    episodes[:] = [
        e for e in episodes if float(e.get("at", 0.0)) >= cutoff or e.get("id") in protected
    ]


def mark_seen(seen: dict[str, float], item_ids: list[str], now: float) -> None:
    for item_id in item_ids:
        seen.setdefault(item_id, now)
    cutoff = now - SEEN_DAYS * DAY
    for item_id in [k for k, at in seen.items() if at < cutoff]:
        del seen[item_id]


# ───────────────────────────── 兴趣 ─────────────────────────────


def _decay(age_seconds: float) -> float:
    return float(0.5 ** (max(0.0, age_seconds) / (INTEREST_HALF_LIFE_DAYS * DAY)))


TOPIC_CHARS = 12


def merge_topic(interests: dict[str, dict[str, Any]], topic: str) -> str:
    """话题先并到已有的相近兴趣上（「天文」「天文观测」「看星星」不各占一项），否则用新键。"""

    key = topic.strip()[:TOPIC_CHARS]
    if not key or key in interests:
        return key
    return next((existing for existing in interests if similar(key, existing)), key)


def add_interest_evidence(
    interests: dict[str, dict[str, Any]],
    topic: str,
    *,
    episode_id: str,
    item_id: str,
    delta: float,
    now: float,
) -> bool:
    """喜欢 +1、不喜欢 -1；同一 item_id 只计一次。返回是否真的新增了证据。"""

    key = merge_topic(interests, topic)
    if not key or delta == 0:
        return False
    entry = interests.setdefault(
        key, {"base": 0.0, "evidence": [], "updated_at": now, "stage": "none"}
    )
    if any(e.get("item") == item_id for e in entry["evidence"]):
        return False
    entry["evidence"].append({"episode": episode_id, "item": item_id, "at": now, "delta": delta})
    del entry["evidence"][:-MAX_EVIDENCE]
    entry["updated_at"] = now
    entry["stage"]      = interest_stage(entry, now)
    return True


def seed_interests(
    interests: dict[str, dict[str, Any]], seeds: tuple[str, ...], now: float
) -> None:
    """种子兴趣有一个不衰减的常量底分 1，不算证据；学来的证据照常衰减。"""

    for topic in seeds:
        entry = interests.setdefault(
            topic, {"base": 1.0, "evidence": [], "updated_at": now, "stage": "none"}
        )
        entry["seed"] = True
        entry["base"] = 1.0


def interest_score(entry: dict[str, Any], now: float) -> float:
    base  = float(entry.get("base", 0.0))
    total = base if entry.get("seed") else base * _decay(now - float(entry.get("updated_at", now)))
    for item in entry.get("evidence", []):
        total += float(item.get("delta", 0)) * _decay(now - float(item.get("at", now)))
    return total


def _positive_items(entry: dict[str, Any], start: float, end: float) -> int:
    items = {
        e.get("item")
        for e in entry.get("evidence", [])
        if e.get("delta", 0) > 0 and start <= float(e.get("at", 0.0)) < end
    }
    return len(items)


def interest_stage(entry: dict[str, Any], now: float) -> str:
    """1 条证据 tentative；近 14 天 ≥2 条不同来源 repeated；前后两个 14 天窗口各 ≥2 consistent。"""

    window = 14 * DAY
    recent = _positive_items(entry, now - window, now + 1)
    before = _positive_items(entry, now - 2 * window, now - window)
    if recent >= 2 and before >= 2:
        return "consistent"
    if recent >= 2:
        return "repeated"
    return "tentative" if entry.get("evidence") else "none"


def refresh_interests(interests: dict[str, dict[str, Any]], now: float) -> None:
    """重算阶段，删掉分数衰减到几乎为零且没有证据的话题，并压到上限。"""

    for key in list(interests):
        entry          = interests[key]
        entry["stage"] = interest_stage(entry, now)
        if not entry["evidence"] and not entry.get("seed") and interest_score(entry, now) < 0.05:
            del interests[key]
    if len(interests) > MAX_INTERESTS:
        ranked = sorted(interests, key=lambda k: interest_score(interests[k], now))
        for key in ranked[: len(interests) - MAX_INTERESTS]:
            del interests[key]


def interest_weights(interests: dict[str, dict[str, Any]], now: float) -> dict[str, float]:
    """抽题用的权重：只取分数为正的话题。"""

    weights = {k: interest_score(v, now) for k, v in interests.items()}
    return {k: w for k, w in weights.items() if w > 0.0 and math.isfinite(w)}


# ───────────────────────────── 巩固快照 ─────────────────────────────

SNAPSHOT_TIERS = (2, 4, 8)


def _snapshot_text(topic: str, stage: str, total: int, liked: int) -> str:
    # 次数留在快照字段里给 /life 和测试看；交给她的文字只说程度，免得被当成报数念出来。
    mood = "大多挺喜欢" if liked * 2 > total else "看是看了，没那么喜欢"
    if stage == "consistent":
        return f"这阵子一直常刷到{topic}相关的内容，{mood}"
    return f"这两周常刷到{topic}相关的内容，{mood}"


def consolidate(
    interests: dict[str, dict[str, Any]], snapshots: list[dict[str, Any]], now: float
) -> int:
    """兴趣到 repeated / consistent 时生成确定性快照（不调模型）；同一话题同一档位只生成一次。

    快照只是“最近的自己”的依据，不是任何学习的来源。返回新增条数。
    """

    made = 0
    for topic, entry in interests.items():
        stage = interest_stage(entry, now)
        if stage not in ("repeated", "consistent"):
            continue
        evidence = entry.get("evidence", [])
        tier = max((t for t in SNAPSHOT_TIERS if len(evidence) >= t), default=0)
        if not tier or any(s["topic"] == topic and s["tier"] == tier for s in snapshots):
            continue
        liked = sum(1 for e in evidence if e.get("delta", 0) > 0)
        times = [float(e.get("at", now)) for e in evidence]
        snapshots.append(
            {
                "topic"         : topic,
                "tier"          : tier,
                "stage"         : stage,
                "evidence_count": len(evidence),
                "liked"         : liked,
                "since"         : min(times),
                "until"         : max(times),
                "created_at"    : now,
                "text"          : _snapshot_text(topic, stage, len(evidence), liked),
            }
        )
        made += 1
    return made
