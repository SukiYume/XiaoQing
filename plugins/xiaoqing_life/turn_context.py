"""聊天侧取用：从内存快照里挑出和当前话题相关的一两点，写成文本块。无网络、无模型、不碰磁盘。

聊天侧只拿相关的少量内容，不报清单、不念内部字段；打算和完成分开；她最近说过的不再推给她。
"""

from __future__ import annotations

import random
import re
from datetime import datetime, timedelta
from typing import Any

from . import methods as methods_mod
from . import reinforce
from .config import LifeConfig
from .lifecycle import DAY, expression_status, term_key
from .safety import is_blocked, is_profane, normalize_for_match, overlap
from .stores import MAX_LOOKUP_QUEUE, LifeStore

MAX_TEXT_CHARS      = 500
MAX_RECENT          = 8
MAX_BOT_RECENT      = 6
MAX_UNKNOWN_TERMS   = 3
MAX_UNKNOWN_CHARS   = 12
MAX_EXCLUDE_IDS     = 20
SHARE_WINDOW_HOURS  = 24
MAX_SHARE           = 3
MAX_PLAN_SLOTS      = 3
HABIT_PREFER        = 100
MAX_ASKED_LEARNINGS = 2
ASK_EPISODES        = 3
ASK_DAYS            = 2
PAST_DAYS           = 7
TOPIC_DAYS          = 3
USE_FRESH_DAYS      = 21
MAX_UNDERSTOOD      = 3
_MIN_OVERLAP        = 2

_ASK_RE = re.compile(
    r"最近(?:都|在|有)?(?:干|忙|看|玩|刷|做)|刷到|刷了|好玩的|有趣的|有意思的|今天(?:都)?(?:干|做|忙|玩|看)"
    r"|在看(?:什么|啥)|看了(?:什么|啥)|在干(?:嘛|什么|啥)|干啥呢|推荐点|有(?:什么|啥)推荐|最近有(?:什么|啥)"
)
_PAST_RE = re.compile(r"昨天|前天|前几天|上周|之前|以前|上次|那天|这几天|前阵子")
_LINK_RE = re.compile(
    r"链接|哪个视频|什么视频|叫啥|叫什么|谁做的|谁发的|哪个up|UP主|出处|地址|av号|BV号|发我|发一下",
    re.IGNORECASE,
)
_PLAN_RE = re.compile(
    r"(?:等会|待会|一会|今晚|晚上|明天|下午|晚点|接下来|周末).{0,6}(?:干嘛|干什么|干啥|做什么|做啥|有什么安排|有啥安排|打算|去哪|忙什么)"
    r"|有什么打算|有啥打算|有什么安排|有啥安排|打算(?:干|做)"
)
_FOLLOW_RE = re.compile(
    r"(?:那个|这个|那条|这条|刚才|刚说|刚刚|你说的).{0,8}(?:视频|帖子|内容|东西|讲啥|讲什么|是啥|咋回事|怎么回事)"
    r"|讲啥的|讲什么的|说的啥|咋回事"
)
_LEARN_RE  = re.compile(r"学到(?:了)?(?:啥|什么)|有什么收获|有啥收获|收获|有啥变化|有什么变化")
_WEEK_RE   = re.compile(r"这周|本周|这一周|最近过得|这几天过得")
_LIKE_RE   = re.compile(r"喜欢什么|喜欢看什么|迷上|最近.{0,3}迷|爱看什么|什么兴趣")
_METHOD_RE = re.compile(r"怎么查的|你怎么知道|怎么知道的|怎么查到|最近学到(?:了)?(?:啥|什么)")

_VERBS = {
    "bili_popular": "刷B站热门，点开了",
    "bili_ranking": "翻B站排行榜，点开了",
    "bili_search" : "在B站搜东西，点开了",
    "zhihu_hot"   : "看知乎热榜，点开了",
    "weibo_hot"   : "刷微博热搜，看了",
    "baidu_hot"   : "看百度热搜，看了",
}

_LIFE_HEADER = (
    "你最近上网看到的（你自己的经历，也只是你生活里上网的这一部分）。"
    "聊到相关的或被问起时可以从这里取，说哪件、说多少随当时的话头。"
    "事实照记录：谁做的、你看到了多少（多半是扫了简介、弹幕和评论区）；"
    "感想可以用自己的话重新说，记录外的细节就是记不清："
)
_SLANG_HEADER = "你在网上学到的说法（帮你听懂对方；对得上时也可以自己用）："
_GUIDE        = "这些是依据，不是要讲的清单。打算和做完分开说；兴趣变了要前后都有记录才算。"
_PART_NOTE    = "对方问的是一整天（或这阵子）：上网只是其中一部分，其他的事看人物事实里的生活记录。"
_PLAN_HEADER  = "接下来的安排（只是打算，还没发生）："
_LEARN_HEADER = "你之前自己查明白的（可以用自己的话讲，记录外的细节说大概）："


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
        "mode": payload.get("mode") if payload.get("mode") in ("proactive", "probe") else "reply",
        "exclude": texts("exclude_ids", MAX_EXCLUDE_IDS, 40, newest=False),
        "text": text.strip()[:MAX_TEXT_CHARS] if isinstance(text, str) else "",
        "recent": texts("recent", MAX_RECENT, 200),
        "bot_recent": texts("bot_recent", MAX_BOT_RECENT, 200),
        "unknown": texts("unknown_terms", MAX_UNKNOWN_TERMS, MAX_UNKNOWN_CHARS, newest=False),
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


def followed_up(store: LifeStore, req: dict[str, Any], now: float) -> list[dict[str, Any]]:
    """对方追问“那个视频讲啥的”：取她刚说过的那条（回声抑制在这里不适用，正是要接着说它）。"""

    spoken = " ".join(req["bot_recent"])
    ranked = [
        (overlap(spoken, f"{e.get('title', '')} {e.get('reaction', '')} {e.get('gist', '')}"), e)
        for e in store.episodes
        if float(e["at"]) <= now and now - float(e["at"]) <= PAST_DAYS * DAY
    ]
    ranked = [(score, e) for score, e in ranked if score >= _MIN_OVERLAP]
    ranked.sort(key=lambda pair: (pair[0], float(pair[1]["at"])), reverse=True)
    return [e for _, e in ranked[:1]]


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
    if _FOLLOW_RE.search(text):
        talked = followed_up(store, req, now)
        if talked:
            return talked, asking
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
    gist  = f"，讲的是：{episode['gist']}" if episode.get("gist") else ""
    dense = f"（{episode['dense_moment']}）" if episode.get("dense_moment") else ""
    feel  = f"，你当时觉得：{episode['reaction']}" if episode.get("reaction") else ""
    return f"- {label} {at:%H:%M} {_opening(episode, title, who)}{gist}{dense}{feel}{tail}"


def _opening(episode: dict[str, Any], title: str, who: str) -> str:
    """按读到多少（``depth``）说清动作：看了字幕、认真读了、查百科 / 翻维基读了，否则扫了一眼。"""

    source = str(episode.get("source"))
    depth  = str(episode.get("depth") or "skim")
    if source == "baike_read":
        return f"查百科读了「{title}」{who}"
    if source == "wiki_read":
        return f"翻维基读了「{title}」{who}"
    if source.startswith("bili_") and depth == "watched":
        rough = "个大概" if episode.get("subtitle_partial") else ""
        return f"点开看了「{title}」{who}，跟着字幕看了{rough}"
    if source.startswith("bili_") and depth == "read":
        return f"点开了「{title}」{who}，认真读了简介和UP主的说明"
    return f"{_VERBS.get(source, '上网看到了')}「{title}」{who}"


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
    if thread.get("status") == "parked":
        hint = f"（下次想从{thread['resume_hint']}接着看）" if thread.get("resume_hint") else ""
        return f"你查过一阵：{title}，查到一半{thread.get('park_reason') or '资料不太够'}，先放着了{hint}"
    if thread.get("status") == "open":
        notes = [n for n in thread.get("notes", []) if n.get("note") and not n.get("superseded_by")]
        parts = []
        if notes:
            parts.append(f"上次查到：{notes[-1]['note']}")
        if thread.get("open_question"):
            parts.append(f"还没弄懂：{thread['open_question']}")
        if not parts:
            return f"你在查的小事：{title}（还没查完，可以说在研究）"
        return f"你在查的小事：{title}（还没查完；{'；'.join(parts)}）"
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
        if recent and _LIKE_RE.search(text):
            lines.append("对方在问你最近迷上什么（有记录的才算）：")
        lines += [f"- 最近的你：{s['text']}" for s in recent[:2]]
    return lines


def tone_line(store: LifeStore, req: dict[str, Any], now: float) -> str:
    """话题对上且 14 天内有兴趣走向快照：给 1 行。"""

    context = " ".join([req["text"], *req["recent"][-2:]])
    folded  = normalize_for_match(context)
    found   = [
        s
        for s in store.snapshots
        if s.get("kind") == "tone"
        and now - float(s.get("created_at", 0.0)) <= 14 * DAY
        and (
            overlap(context, str(s.get("topic", ""))) >= _MIN_OVERLAP
            or (
                len(normalize_for_match(str(s.get("topic", "")))) >= 2
                and normalize_for_match(str(s["topic"])) in folded
            )
        )
    ]
    if not found:
        return ""
    return f"- 最近的你：{max(found, key=lambda s: float(s['created_at']))['text']}"


def wants_plans(text: str) -> bool:
    return bool(_PLAN_RE.search(text))


def learning_lines(store: LifeStore, req: dict[str, Any], now: float) -> tuple[list[str], bool]:
    """自己查明白的事：话题对上给 1 条；问“最近学到啥 / 有什么收获”给 ≤2 条加一句确定性的成长概括。"""

    rows = sorted(store.learnings, key=lambda r: float(r.get("created_at", 0.0)), reverse=True)
    method = methods_mod.chat_line(store.methods) if _METHOD_RE.search(req["text"]) else ""
    if not rows:
        return ([f"- {method}"] if method else []), False
    text = req["text"]
    if _LEARN_RE.search(text):
        picked = rows[:MAX_ASKED_LEARNINGS]
        lines  = [f"- {r['text']}" for r in picked]
        if growth := growth_line(rows, now):
            lines.append(f"- {growth}")
        if method:
            lines.append(f"- {method}")
    else:
        context = " ".join([text, *req["recent"][-2:]])
        scored  = [(overlap(context, f"{r.get('topic', '')} {r['text']}"), r) for r in rows]
        scored  = [pair for pair in scored if pair[0] >= _MIN_OVERLAP]
        if not scored:
            return ([f"- {method}"] if method else []), False
        picked = [max(scored, key=lambda pair: pair[0])[1]]
        lines = [f"- {picked[0]['text']}"]
        if method:
            lines.append(f"- {method}")
    for row in picked:
        row["last_used"] = now
    return lines, True


def growth_line(rows: list[dict[str, Any]], now: float) -> str:
    """确定性模板：这个月在某话题上查明白了几件事。"""

    recent = [r for r in rows if now - float(r.get("created_at", 0.0)) <= 30 * DAY]
    if not recent:
        return ""
    counts: dict[str, int] = {}
    for row in recent:
        counts[row.get("topic", "")] = counts.get(row.get("topic", ""), 0) + 1
    topic, n = max(counts.items(), key=lambda kv: kv[1])
    # 只说程度不报数，免得她把件数当成汇报念出来。
    amount = "好几件事" if n >= 3 else "一两件事"
    tail   = "，别的方面也零零碎碎查了点" if len(recent) > n else ""
    return f"这个月在{topic}上查明白了{amount}{tail}"


def plan_lines(store: LifeStore, routine_slots: list[str]) -> list[str]:
    """打算：今天剩下的作息时段（≤3）加进行中小课题的下一步。"""

    lines  = [f"- {slot}" for slot in routine_slots[:MAX_PLAN_SLOTS]]
    thread = next((t for t in store.threads if t.get("status") == "open"), None)
    if thread is not None:
        follow = "（接着上次查明白的事往下问）" if thread.get("parent_id") else "（还没查完）"
        lines.append(f"- 想接着查：{thread.get('next_query') or thread.get('title')}{follow}")
    return lines


def build_life_lines(
    store: LifeStore,
    req: dict[str, Any],
    moment: datetime,
    cfg: LifeConfig,
    routine_slots: list[str] | None = None,
    learnings: list[str] | None     = None,
) -> tuple[str, list[str]]:
    learn = learnings or []
    plan  = plan_lines(store, routine_slots or []) if wants_plans(req["text"]) else []
    episodes, asking = select_episodes(store, req, moment, cfg)
    link = bool(_LINK_RE.search(req["text"]))
    lines = [episode_line(e, moment, cfg, link=link) for e in episodes]
    diary = store.state.get("diary") or {}
    if asking and diary.get("day") == moment.strftime("%Y-%m-%d") and diary.get("text"):
        lines.append(f"- 今天大概：{diary['text']}")
    lines += recap_lines(store, req, moment)
    if (tone := tone_line(store, req, moment.timestamp())) and tone not in lines:
        lines.append(tone)
    thread_line = select_thread(store, req, asking, moment.timestamp())
    doing       = current_line(store, moment.timestamp())
    if not (lines or thread_line or doing or plan):
        if learn:
            return "\n".join([_LEARN_HEADER, *learn, _GUIDE]), []
        return "", []
    parts = [
        _LIFE_HEADER,
        *([doing] if doing else []),
        *lines,
        *([thread_line] if thread_line else []),
        *([_PLAN_HEADER, *plan] if plan else []),
        *([_LEARN_HEADER, *learn] if learn else []),
        *([_PART_NOTE] if _named_days(req["text"], moment) or asking else []),
        _GUIDE,
    ]
    return "\n".join(parts), [e["id"] for e in episodes]


def share_line(episode: dict[str, Any], moment: datetime, cfg: LifeConfig) -> str:
    """主动分享用的第一人称材料行：讲清标题、UP 主、讲了什么、她的感想，能给链接就给。"""

    return episode_line(episode, moment, cfg, link=True).removeprefix("- ")


def share_candidates(
    store: LifeStore, req: dict[str, Any], moment: datetime, cfg: LifeConfig
) -> list[dict[str, Any]]:
    """最近 24 小时已经刷到的、想分享的经历（排除调用方给的已分享 ID）。"""

    now  = moment.timestamp()
    rows = [
        e
        for e in store.episodes
        if e.get("share")
        and now - SHARE_WINDOW_HOURS * 3600 <= float(e["at"]) <= now
        and e["id"] not in req["exclude"]
    ]
    rows.sort(key=lambda e: float(e["at"]), reverse=True)
    return [
        {
            "episode_id": e["id"],
            "line"      : share_line(e, moment, cfg),
            "topics"    : list(e.get("topics", [])),
        }
        for e in rows[:MAX_SHARE]
    ]


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
    scale = reinforce.HABIT_COOLDOWN_SCALE if reinforce.is_habit(entry) else 1.0
    return (
        now - float(entry.get("last_suggested", 0.0)) >= cfg.suggest_cooldown_hours * 3600.0 * scale
    )


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
        if key in spoken or not _cooled(entry, now, cfg) or reinforce.is_suppressed(entry, now):
            continue
        score = overlap(context, f"{entry.get('meaning', '')} {entry.get('usage', '')}")
        rank  = score + (HABIT_PREFER if reinforce.is_habit(entry) else 0)
        if score >= _MIN_OVERLAP and (best is None or rank > best[0]):
            best = (rank, key, entry)
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
        if (
            expression_status(entry, now, cfg) == "fading"
            or not _cooled(entry, now, cfg)
            or reinforce.is_suppressed(entry, now)
        ):
            continue
        if normalize_for_match(str(entry.get("pattern", ""))) in spoken:
            continue
        score = overlap(context, str(entry.get("situation", "")))
        rank  = score + (HABIT_PREFER if reinforce.is_habit(entry) else 0)
        if score >= _MIN_OVERLAP and (best is None or rank > best[0]):
            best = (rank, entry)
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
    routine_slots: list[str] | None = None,
) -> tuple[dict[str, Any] | None, bool]:
    """返回 (响应, 是否改了内存状态需要落盘)。关闭时由调用方直接返回 None。"""

    req = clean_payload(payload)
    if req["mode"] == "probe":
        return {"version": 1, "online_life": True}, False
    dirty = False
    if cfg.source_on("baike_lookup") and enqueue_unknown(store, req["unknown"], cfg):
        dirty = True
    if req["mode"] == "proactive":
        candidates = share_candidates(store, req, moment, cfg)
        if not candidates:
            return None, dirty
        return (
            {
                "version"         : 1,
                "life_lines"      : "",
                "slang_notes"     : "",
                "expression_hint" : "",
                "share_candidates": candidates,
                "episode_ids"     : [c["episode_id"] for c in candidates],
                "term_keys"       : [],
            },
            dirty,
        )
    now   = moment.timestamp()
    dirty = (
        reinforce.apply_feedback(
            store.terms, store.expressions, req["recent"], req["bot_recent"], now
        )
        or dirty
    )
    learn, learn_dirty = learning_lines(store, req, now)
    dirty = dirty or learn_dirty
    life_lines, episode_ids = build_life_lines(store, req, moment, cfg, routine_slots, learn)
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
