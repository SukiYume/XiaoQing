"""聊天侧取用：从内存快照里挑出和当前话题相关的一两点，写成文本块。无网络、无模型、不碰磁盘。

聊天侧只拿相关的少量内容，不报清单、不念内部字段；打算和完成分开；她最近说过的不再推给她。
"""

from __future__ import annotations

import random
import re
from datetime import datetime, timedelta
from typing import Any

from .config import LifeConfig
from .lifecycle import DAY, expression_status, term_key
from .safety import is_blocked, is_profane, normalize_for_match, overlap
from .stores import MAX_LOOKUP_QUEUE, LifeStore

MAX_TEXT_CHARS    = 500
MAX_RECENT        = 8
MAX_BOT_RECENT    = 6
MAX_UNKNOWN_TERMS = 3
MAX_UNKNOWN_CHARS = 12
ASK_EPISODES      = 3
ASK_DAYS          = 2
PAST_DAYS         = 7
TOPIC_DAYS        = 3
USE_FRESH_DAYS    = 21
MAX_UNDERSTOOD    = 3
_MIN_OVERLAP      = 2

_ASK_RE = re.compile(
    r"最近(?:都|在|有)?(?:干|忙|看|玩|刷|做)|刷到|刷了|好玩的|有趣的|有意思的|今天(?:都)?(?:干|做|忙|玩|看)"
    r"|在看(?:什么|啥)|看了(?:什么|啥)|在干(?:嘛|什么|啥)|干啥呢|推荐点|有(?:什么|啥)推荐|最近有(?:什么|啥)"
)
_PAST_RE = re.compile(r"昨天|前天|前几天|上周|之前|以前|上次|那天|这几天|前阵子")
_LINK_RE = re.compile(
    r"链接|哪个视频|什么视频|叫啥|叫什么|谁做的|谁发的|哪个up|UP主|出处|地址|av号|BV号|发我|发一下",
    re.IGNORECASE,
)
_WEEK_RE = re.compile(r"这周|本周|这一周|最近过得|这几天过得")
_LIKE_RE = re.compile(r"喜欢什么|喜欢看什么|迷上|最近.{0,3}迷|爱看什么|什么兴趣")

_VERBS = {
    "bili_popular": "刷B站热门，点开了",
    "bili_ranking": "翻B站排行榜，点开了",
    "bili_search" : "在B站搜东西，点开了",
    "zhihu_hot"   : "看知乎热榜，点开了",
    "weibo_hot"   : "刷微博热搜，看了",
    "baidu_hot"   : "看百度热搜，看了",
}

_LIFE_HEADER = (
    "你最近上网看到的（你自己的经历。聊到相关的、或者别人问你最近在干嘛时，"
    "自然带出一两点就好，不用报清单；细节记不清就说记不清。看视频你通常只扫了简介、弹幕和评论区，按这个程度说，别说成看完了或描述画面）："
)
_SLANG_HEADER = (
    "你在网上学到的说法（帮你听懂对方；自己用的话一次最多一个，对得上才用，"
    "像顺嘴说出来，不解释梗）："
)


def clean_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """校验并截断聊天发来的请求；类型不对的字段按空处理。"""

    def texts(key: str, limit: int, chars: int, *, newest: bool = True) -> list[str]:
        value = payload.get(key)
        if not isinstance(value, list):
            return []
        found = [v.strip()[:chars] for v in value if isinstance(v, str) and v.strip()]
        return found[-limit:] if newest else list(dict.fromkeys(found))[:limit]

    text = payload.get("text")
    return {
        "mode"      : "proactive" if payload.get("mode") == "proactive" else "reply",
        "text"      : text.strip()[:MAX_TEXT_CHARS] if isinstance(text, str) else "",
        "recent"    : texts("recent", MAX_RECENT, 200),
        "bot_recent": texts("bot_recent", MAX_BOT_RECENT, 200),
        "unknown"   : texts("unknown_terms", MAX_UNKNOWN_TERMS, MAX_UNKNOWN_CHARS, newest=False),
    }


def _day_label(at: datetime, moment: datetime) -> str:
    gap = (moment.date() - at.date()).days
    return {0: "今天", 1: "昨天", 2: "前天"}.get(gap, f"{at.month}月{at.day}日")


def echoed(title: str, bot_recent: list[str]) -> bool:
    """标题的一段原文已经出现在她最近的回复里：她刚说过，不再给。"""

    folded = normalize_for_match(title)
    spoken = normalize_for_match(" ".join(bot_recent))
    if not folded or not spoken:
        return False
    width = min(6, len(folded))
    return any(folded[i : i + width] in spoken for i in range(len(folded) - width + 1))


def _episode_relevance(episode: dict[str, Any], context_text: str) -> int:
    body = " ".join(
        [episode.get("title", ""), episode.get("reaction", ""), *episode.get("topics", [])]
    )
    score  = overlap(context_text, body)
    folded = normalize_for_match(context_text)
    if any(
        normalize_for_match(t) and normalize_for_match(t) in folded
        for t in episode.get("topics", [])
    ):
        score += _MIN_OVERLAP
    return score


def _named_days(text: str, moment: datetime) -> set[str]:
    """消息里明说“昨天 / 前天”时，只看那一天。"""

    days: set[str] = set()
    if "昨天" in text:
        days.add((moment - timedelta(days=1)).strftime("%Y-%m-%d"))
    if "前天" in text:
        days.add((moment - timedelta(days=2)).strftime("%Y-%m-%d"))
    return days


def select_episodes(
    store: LifeStore, req: dict[str, Any], moment: datetime, cfg: LifeConfig
) -> tuple[list[dict[str, Any]], bool]:
    """返回 (经历, 是否在问近况)。只给已经“刷到”的（at ≤ 现在）。

    问近况最近 2 天至多 3 条；提到过去放宽到 7 天（明说昨天 / 前天就只看那天）；
    问链接 / 哪个视频时按话题挑最相关的 1～2 条；否则话题重合 1 条。
    """

    now     = moment.timestamp()
    text    = req["text"]
    asking  = bool(_ASK_RE.search(text))
    past    = bool(_PAST_RE.search(text))
    link    = bool(_LINK_RE.search(text))
    context = " ".join([text, *req["recent"][-2:]])
    pool    = [
        e
        for e in store.episodes
        if float(e["at"]) <= now and not echoed(str(e.get("title", "")), req["bot_recent"])
    ]
    named = _named_days(text, moment)
    if named:
        day_pool = [e for e in pool if e.get("day") in named]
        day_pool.sort(key=lambda e: (bool(e.get("share")), float(e["at"])), reverse=True)
        return day_pool[:ASK_EPISODES], asking
    if link:
        recent = [e for e in pool if now - float(e["at"]) <= PAST_DAYS * DAY]
        recent.sort(key=lambda e: (_episode_relevance(e, context), float(e["at"])), reverse=True)
        return recent[:2], asking
    if asking or past:
        days  = PAST_DAYS if past else ASK_DAYS
        fresh = [e for e in pool if now - float(e["at"]) <= days * DAY]
        fresh.sort(key=lambda e: (bool(e.get("share")), float(e["at"])), reverse=True)
        return fresh[:ASK_EPISODES], asking
    ranked = [
        (score, e)
        for e in pool
        if now - float(e["at"]) <= TOPIC_DAYS * DAY
        if (score := _episode_relevance(e, context)) >= _MIN_OVERLAP
    ]
    ranked.sort(key=lambda pair: (pair[0], float(pair[1]["at"])), reverse=True)
    return [e for _, e in ranked[:1]], False


def episode_line(
    episode: dict[str, Any], moment: datetime, cfg: LifeConfig, *, link: bool = False
) -> str:
    """说清是什么、谁做的、讲了什么，再说她的感想；链接只在对方要的时候给。"""

    at    = datetime.fromtimestamp(float(episode["at"]), moment.tzinfo)
    label = _day_label(at, moment)
    title = str(episode.get("title", ""))
    who   = f"（UP主 {episode['uploader']}）" if episode.get("uploader") else ""
    tail  = f"（链接 {episode['url']}）" if link and episode.get("url") else ""
    if (moment - at).days >= cfg.episode_detail_days:
        return f"- {label}刷到过「{title}」{who}（细节记不清了）{tail}"
    verb  = _VERBS.get(str(episode.get("source")), "上网看到了")
    gist  = f"，讲的是：{episode['gist']}" if episode.get("gist") else ""
    dense = f"（{episode['dense_moment']}）" if episode.get("dense_moment") else ""
    return (
        f"- {label} {at:%H:%M} {verb}「{title}」{who}{gist}{dense}，"
        f"你当时觉得：{episode.get('reaction', '')}{tail}"
    )


def select_thread(store: LifeStore, req: dict[str, Any], asking: bool, now: float) -> str:
    """话题重合或问近况时给 1 个小课题；在查的写“在查”，查完的写收获。"""

    context                                 = " ".join([req["text"], *req["recent"][-2:]])
    best: tuple[int, dict[str, Any]] | None = None
    for thread in store.threads:
        if thread.get("status") == "dropped":
            continue
        score = overlap(context, f"{thread.get('title', '')} {thread.get('why', '')}")
        if asking and thread.get("status") in ("open", "reviewing"):
            score += _MIN_OVERLAP
        if score >= _MIN_OVERLAP and (best is None or score > best[0]):
            best = (score, thread)
    if best is None:
        return ""
    thread = best[1]
    title  = thread.get("title", "")
    if thread.get("status") == "open":
        return f"你在查的小事：{title}（还没查完，可以说在研究）"
    if thread.get("status") == "reviewing":
        return f"你在查的小事：{title}（查得差不多了，还没想好怎么总结）"
    takeaway = str(thread.get("takeaway") or "")
    return f"你前几天查过：{title}，收获：{takeaway}" if takeaway else ""


_DOING_BY_SOURCE = {
    "bili_popular": "刷B站热门",
    "bili_ranking": "翻B站排行榜",
    "zhihu_hot"   : "刷知乎热榜",
    "weibo_hot"   : "刷微博热搜",
    "baidu_hot"   : "看百度热搜",
}


def current_line(store: LifeStore, now: float) -> str:
    """正在名义浏览时段内：她知道自己在刷什么，但不含任何还没刷到的标题。"""

    current = store.state.get("current") or {}
    if not current or not float(current.get("start", 0.0)) <= now < float(current.get("end", 0.0)):
        return ""
    choice = current.get("choice")
    query  = str(current.get("query") or "")
    if choice == "hot_scroll":
        doing = _DOING_BY_SOURCE.get(str(current.get("source")), "刷热榜")
    elif choice == "interest_dive":
        doing = f"在B站搜〔{query}〕" if query else "在B站搜东西"
    elif choice == "thread_continue":
        doing = f"在B站接着查〔{query}〕" if query else "在B站接着查"
    else:
        doing = "查百科里的一个说法"
    return f"你这会儿正在{doing}。"


def recap_lines(store: LifeStore, req: dict[str, Any], moment: datetime) -> list[str]:
    """问这周 / 最近过得怎样给周记或日记，问喜欢什么给巩固快照；都只是“最近的自己”的依据。"""

    text, now = req["text"], moment.timestamp()
    lines: list[str] = []
    if _WEEK_RE.search(text):
        notes = [
            j
            for j in store.journal
            if j.get("kind") == "weekly" and now - float(j["at"]) <= 7 * DAY
        ]
        if notes:
            lines.append(f"- 这周大概：{notes[-1]['text']}")
        else:
            diaries = [
                j
                for j in store.journal
                if j.get("kind") == "diary" and now - float(j["at"]) <= 4 * DAY
            ]
            lines += [f"- {j['day']}：{j['text']}" for j in diaries[-3:]]
    if _WEEK_RE.search(text) or _LIKE_RE.search(text):
        recent = [s for s in store.snapshots if now - float(s["created_at"]) <= 30 * DAY]
        recent.sort(key=lambda s: float(s["created_at"]), reverse=True)
        lines += [f"- 最近的你：{s['text']}" for s in recent[:2]]
    return lines


def build_life_lines(
    store: LifeStore, req: dict[str, Any], moment: datetime, cfg: LifeConfig
) -> tuple[str, list[str]]:
    episodes, asking = select_episodes(store, req, moment, cfg)
    link = bool(_LINK_RE.search(req["text"]))
    lines = [episode_line(e, moment, cfg, link=link) for e in episodes]
    diary = store.state.get("diary") or {}
    if asking and diary.get("day") == moment.strftime("%Y-%m-%d") and diary.get("text"):
        lines.append(f"- 今天大概：{diary['text']}")
    lines += recap_lines(store, req, moment)
    thread_line = select_thread(store, req, asking, moment.timestamp())
    doing       = current_line(store, moment.timestamp())
    if not (lines or thread_line or doing):
        return "", []
    parts = [
        _LIFE_HEADER,
        *([doing] if doing else []),
        *lines,
        *([thread_line] if thread_line else []),
    ]
    return "\n".join(parts), [e["id"] for e in episodes]


# ───────────────────────────── 词条与表达 ─────────────────────────────


def understood_terms(store: LifeStore, req: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    haystack = normalize_for_match(" ".join([req["text"], *req["recent"][-3:]]))
    found    = [
        (key, entry)
        for key, entry in store.terms.items()
        if entry.get("status") in ("candidate", "known", "fading")
        and len(key) >= 2
        and key in haystack
    ]
    found.sort(key=lambda pair: len(pair[0]), reverse=True)
    return found[:MAX_UNDERSTOOD]


def _term_line(entry: dict[str, Any]) -> str:
    usage   = str(entry.get("usage") or "")
    meaning = f"{entry.get('meaning', '')}，{usage}" if usage else str(entry.get("meaning", ""))
    return f"「{entry.get('term', '')}」：{meaning}"


def _cooled(entry: dict[str, Any], now: float, cfg: LifeConfig) -> bool:
    return now - float(entry.get("last_suggested", 0.0)) >= cfg.suggest_cooldown_hours * 3600.0


def pick_usable_term(
    store: LifeStore, req: dict[str, Any], now: float, cfg: LifeConfig, rng: random.Random
) -> tuple[str, dict[str, Any]] | None:
    """known 且最近 21 天看到过、和当前话题有重合、她刚没说过、过了冷却的词条挑 1 个。"""

    if rng.random() >= cfg.active_use_probability:
        return None
    context                                      = " ".join([req["text"], *req["recent"][-2:]])
    spoken                                       = normalize_for_match(" ".join(req["bot_recent"]))
    best: tuple[int, str, dict[str, Any]] | None = None
    for key, entry in store.terms.items():
        if (
            entry.get("status") != "known"
            or now - float(entry.get("last_seen", 0.0)) > USE_FRESH_DAYS * DAY
        ):
            continue
        if key in spoken or not _cooled(entry, now, cfg):
            continue
        score = overlap(context, f"{entry.get('meaning', '')} {entry.get('usage', '')}")
        if score >= _MIN_OVERLAP and (best is None or score > best[0]):
            best = (score, key, entry)
    return (best[1], best[2]) if best else None


def pick_expression(
    store: LifeStore, req: dict[str, Any], now: float, cfg: LifeConfig, rng: random.Random
) -> dict[str, Any] | None:
    if rng.random() >= cfg.active_use_probability:
        return None
    context                                 = " ".join([req["text"], *req["recent"][-2:]])
    spoken                                  = normalize_for_match(" ".join(req["bot_recent"]))
    best: tuple[int, dict[str, Any]] | None = None
    for entry in store.expressions:
        if expression_status(entry, now, cfg) == "fading" or not _cooled(entry, now, cfg):
            continue
        if normalize_for_match(str(entry.get("pattern", ""))) in spoken:
            continue
        score = overlap(context, str(entry.get("situation", "")))
        if score >= _MIN_OVERLAP and (best is None or score > best[0]):
            best = (score, entry)
    return best[1] if best else None


def build_slang_notes(
    store: LifeStore, req: dict[str, Any], now: float, cfg: LifeConfig, rng: random.Random
) -> tuple[str, list[str], bool]:
    heard  = understood_terms(store, req)
    lines  = [f"- {_term_line(entry)}" for _, entry in heard]
    keys   = [key for key, _ in heard]
    usable = pick_usable_term(store, req, now, cfg, rng)
    suffix = ""
    if usable is not None:
        key, entry = usable
        entry["last_suggested"] = now
        suffix                  = f"可以顺手用的：{_term_line(entry)}"
        keys.append(key)
    if not lines and not suffix:
        return "", [], False
    return "\n".join([_SLANG_HEADER, *lines, *([suffix] if suffix else [])]), keys, bool(suffix)


def build_expression_hint(
    store: LifeStore, req: dict[str, Any], now: float, cfg: LifeConfig, rng: random.Random
) -> str:
    entry = pick_expression(store, req, now, cfg, rng)
    if entry is None:
        return ""
    entry["last_suggested"] = now
    return f"当{entry['situation'].removeprefix('当')}：{entry['pattern']}"


# ───────────────────────────── 听不懂的词入队 ─────────────────────────────


def enqueue_unknown(store: LifeStore, words: list[str], cfg: LifeConfig) -> bool:
    """只把词本身放进待查队列（不记来自哪个会话）；已知、脏话、屏蔽词不入队。"""

    queue: list[str] = store.state["lookup_queue"]
    changed          = False
    for word in words:
        if not word or len(queue) >= MAX_LOOKUP_QUEUE or word in queue:
            continue
        if (
            term_key(word) in store.terms
            or is_profane(word)
            or is_blocked(word, cfg.blocked_keywords)
        ):
            continue
        queue.append(word)
        changed = True
    return changed


def turn_context(
    store: LifeStore,
    cfg: LifeConfig,
    payload: dict[str, Any],
    moment: datetime,
    rng: random.Random,
) -> tuple[dict[str, Any] | None, bool]:
    """返回 (响应, 是否改了内存状态需要落盘)。关闭时由调用方直接返回 None。"""

    req   = clean_payload(payload)
    dirty = False
    if cfg.source_on("baike_lookup") and enqueue_unknown(store, req["unknown"], cfg):
        dirty = True
    if req["mode"] == "proactive":
        return None, dirty
    now = moment.timestamp()
    life_lines, episode_ids = build_life_lines(store, req, moment, cfg)
    slang, term_keys, used = build_slang_notes(store, req, now, cfg, rng)
    hint  = build_expression_hint(store, req, now, cfg, rng)
    dirty = dirty or used or bool(hint)
    if not (life_lines or slang or hint):
        return None, dirty
    return (
        {
            "version"         : 1,
            "life_lines"      : life_lines,
            "slang_notes"     : slang,
            "expression_hint" : hint,
            "share_candidates": [],
            "episode_ids"     : episode_ids,
            "term_keys"       : term_keys,
        },
        dirty,
    )
