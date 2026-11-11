"""``/life`` 子命令的文本输出（全部 bot_admin）；只读快照，写操作经 ``LifeStore.mutate``。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from . import chat_topics, dossier, reinforce
from .config import LifeConfig
from .days import DAY, day_key
from .knowledge import HYPOTHESIS
from .lifecycle import term_key
from .persona_bridge import Presence
from .stores import LifeStore
from .works import FORMS

HELP_TEXT = """🌐 小青的网上生活

/life [status]        开关、此刻作息、今日次数、最近一次决策
/life recent          最近 8 条经历
/life slang           词条前 15
/life threads         小课题（含搁置原因、最新要点、更正数）
/life methods         她自己摸索出的查法
/life stats           最近 7 天的运行计数（上网、研究、小课题、核实、查词、交给聊天的）
/life works           她在慢慢整理的小作品
/life dossier [话题]  主题档案（不带话题列出全部）
/life memes           梗图列表
/life run             立刻安排一次上网（跳过概率与间隔）
/life forget <词>     删除词条
/life meme on|off <短ID>  开关一张梗图
/life login           B站扫码登录（二维码只发私聊）；login status / login cancel
/life logout          删除本地 B站 登录凭证"""

_AVAILABILITY = {"free": "空闲", "busy": "在忙", "asleep": "睡着"}
_CHOICES      = {
    "rest"           : "休息",
    "wind_down"      : "睡前整理",
    "thread_continue": "续查小课题",
    "hot_scroll"     : "刷热榜",
    "interest_dive"  : "按兴趣搜",
    "term_lookup"    : "查词",
    "work_on"        : "整理作品",
}


def _age(now: float, at: float) -> str:
    gap = max(0.0, now - at)
    if gap < 3600:
        return f"{int(gap // 60)} 分钟前"
    if gap < DAY:
        return f"{int(gap // 3600)} 小时前"
    return f"{int(gap // DAY)} 天前"


def status_text(
    store: LifeStore, cfg: LifeConfig, presence: Presence, moment: datetime, *, bili: str = ""
) -> str:
    state    = store.state
    decision = state.get("last_decision") or {}
    now      = moment.timestamp()
    same_day = state.get("day") == day_key(moment)
    sessions = state.get("sessions_today", 0) if same_day else 0
    calls    = state.get("llm_calls_today", 0) if same_day else 0
    lines    = [
        f"网上生活：{'开启' if cfg.enabled else '关闭'}；此刻{_AVAILABILITY[presence.availability]}"
        + (f"（{presence.activity}）" if presence.activity else ""),
        f"今日上网 {sessions}/{cfg.max_sessions_per_day} 次，模型调用 {calls}/{cfg.max_llm_calls_per_day} 次",
    ]
    if decision:
        scores = "，".join(
            f"{_CHOICES.get(k, k)} {v:.0f}" for k, v in decision.get("scores", {}).items()
        )
        outcome = f"；结果 {decision['outcome']}" if decision.get("outcome") else ""
        lines.append(
            f"最近决策（{_age(now, float(decision.get('at', now)))}）："
            f"{_CHOICES.get(decision.get('choice', ''), decision.get('choice', ''))}，"
            f"理由 {decision.get('reason', '')}{outcome}" + (f"（{scores}）" if scores else "")
        )
    lines += _reinforcement_lines(store, now)
    if bili:
        lines.append(bili)
    open_threads = sum(1 for t in store.threads if t.get("status") in ("open", "reviewing"))
    lines.append(
        f"经历 {len(store.episodes)} 条，词条 {len(store.terms)} 个，表达 {len(store.expressions)} 条，"
        f"兴趣 {len(store.interests)} 个，进行中小课题 {open_threads} 个，待查词 {len(state.get('lookup_queue', []))} 个，方法 {len(store.methods)} 条，作品 {len(store.works)} 个，档案 {len(store.dossiers)} 个"
    )
    return "\n".join(lines)


def _reinforcement_lines(store: LifeStore, now: float) -> list[str]:
    """自我强化的可见部分：最近的偏好、不稳的来源、习惯、好奇边界与自己查明白的事。"""

    state            = store.state
    lines: list[str] = []
    prefs            = state.get("preferences", {})
    if prefs.get("activity"):
        shown = "，".join(
            f"{_CHOICES.get(k, k)} {reinforce.pref_adjust(state, 'activity', k):+.1f}"
            for k in prefs["activity"]
        )
        lines.append(f"最近的偏好（打分加成）：{shown}")
    if prefs.get("source"):
        shown = "，".join(
            f"{k} ×{reinforce.source_pref_factor(state, k):.2f}" for k in prefs["source"]
        )
        lines.append(f"热榜来源偏好：{shown}")
    down = [
        k for k in state.get("source_health", {}) if reinforce.health_factor(state, k, now) < 1.0
    ]
    if down:
        lines.append(f"近期不稳的来源（权重 ×0.2，6 小时内恢复）：{'、'.join(down)}")
    if habit := reinforce.habit_text(state):
        lines.append(f"习惯：{habit}")
    untried = sum(1 for f in store.frontier if not f.get("tried"))
    unsure  = sum(1 for r in store.learnings if r.get("confidence") == HYPOTHESIS)
    lines.append(
        f"好奇边界 {untried} 个待试，自己查明白的事 {len(store.learnings)} 件"
        + (f"（其中 {unsure} 件只有一个来源，还没核实）" if unsure else "")
    )
    heard = len(state.get("heard_topics", []))
    picks = [p["topic"] for p in chat_topics.fresh(state, now)]
    if heard or picks:
        lines.append(
            f"群里听到的话题 {heard} 个" + (f"，想去看看：{'、'.join(picks)}" if picks else "")
        )
    factors = (state.get("last_decision") or {}).get("factors")
    if factors:
        lines.append(f"上次决策的自我强化因素：{factors}")
    return lines


def recent_text(store: LifeStore, moment: datetime) -> str:
    if not store.episodes:
        return "还没有上网经历。"
    now = moment.timestamp()
    rows = sorted(store.episodes, key=lambda e: float(e.get("at", 0.0)), reverse=True)[:8]
    return "\n".join(
        f"{_age(now, float(e['at']))} [{e.get('source', '')}] 「{e.get('title', '')}」：{e.get('reaction', '')}"
        for e in rows
    )


def slang_text(store: LifeStore, moment: datetime) -> str:
    if not store.terms:
        return "还没有学到词条。"
    now = moment.timestamp()
    rows = sorted(store.terms.values(), key=lambda t: float(t.get("last_seen", 0.0)), reverse=True)[
        :15
    ]
    return "\n".join(
        f"「{t['term']}」{t.get('status', '')} 来源{len(t.get('sources', []))} "
        f"{_age(now, float(t.get('last_seen', now)))}看到"
        for t in rows
    )


def _latest_finding(thread: dict[str, Any]) -> str:
    notes = [n for n in thread.get("notes", []) if n.get("note") and not n.get("superseded_by")]
    return str(notes[-1]["note"]) if notes else ""


def threads_text(store: LifeStore) -> str:
    if not store.threads:
        return "没有小课题。"
    lines = []
    for t in store.threads:
        line = f"[{t.get('status', '')}] {t.get('title', '')}（第 {t.get('steps', 0)} 步）"
        if t.get("takeaway"):
            line += f" 收获：{t['takeaway']}"
        if t.get("status") == "parked" and t.get("park_reason"):
            line += f" 搁置原因：{t['park_reason']}"
        if t.get("status") == "dropped" and t.get("drop_reason"):
            line += f" 放弃原因：{t['drop_reason']}"
        if finding := _latest_finding(t):
            line += f" 最新要点：{finding}"
        if t.get("corrections"):
            line += f" 更正 {len(t['corrections'])} 次"
        lines.append(line)
    return "\n".join(lines)


def methods_text(store: LifeStore) -> str:
    if not store.methods:
        return "还没有自己摸索出的查法。"
    return "\n".join(
        f"[{m.get('status', 'active')}] {m.get('text', '')}（适用：{m.get('applies_to', '')}；"
        f"用过 {m.get('uses', 0)} 次，帮上忙 {m.get('wins', 0)} 次）"
        for m in store.methods
    )


async def forget_term(store: LifeStore, word: str) -> str:
    key = term_key(word)
    if not key:
        return "用法: /life forget <词>"

    def apply(s: LifeStore) -> bool:
        found                   = s.terms.pop(key, None) is not None
        queue                   = s.state["lookup_queue"]
        s.state["lookup_queue"] = [q for q in queue if term_key(q) != key]
        return found

    return "已删除词条。" if await store.mutate(apply) else "没有这个词条。"


def route(args: str) -> tuple[str, str]:
    head, _, rest = (args or "").strip().partition(" ")
    aliases: dict[str, Any] = {
        ""       : "status",
        "stats"  : "stats",
        "统计"     : "stats",
        "status" : "status",
        "状态"     : "status",
        "recent" : "recent",
        "最近"     : "recent",
        "slang"  : "slang",
        "词条"     : "slang",
        "works"  : "works",
        "作品"     : "works",
        "dossier": "dossier",
        "档案"     : "dossier",
        "methods": "methods",
        "方法"     : "methods",
        "threads": "threads",
        "小课题"    : "threads",
        "memes"  : "memes",
        "梗图"     : "memes",
        "run"    : "run",
        "运行"     : "run",
        "forget" : "forget",
        "忘记"     : "forget",
        "meme"   : "meme",
        "login"  : "login",
        "登录"     : "login",
        "logout" : "logout",
        "退出登录"   : "logout",
        "help"   : "help",
        "帮助"     : "help",
    }
    return aliases.get(head.lower(), "unknown"), rest.strip()


def works_text(store: LifeStore) -> str:
    if not store.works:
        return "还没有作品。"
    lines = []
    for work in store.works:
        form = FORMS.get(str(work.get("form")), "")
        lines.append(
            f"[{work.get('status', '')}] 《{work.get('title', '')}》（{form}，{work.get('topic', '')}，"
            f"整理 {work.get('revisions', 0)} 次，{len(work.get('entries', []))} 条）"
        )
        if work.get("intro"):
            lines.append(f"  开头：{work['intro']}")
        for entry in work.get("entries", []):
            mark = "（待核实）" if entry.get("kind") == "todo" else ""
            lines.append(f"  - {mark}{entry.get('text', '')}")
    return "\n".join(lines)


def dossier_text(store: LifeStore, topic: str, moment: datetime) -> str:
    if not topic:
        return dossier.list_text(store.dossiers, moment)
    entry = dossier.find_recall(store.dossiers, topic, fallback=False)
    if entry is None:
        return "没有这个话题的档案。"
    return dossier.command_text(entry, moment)
