"""``/life`` 子命令的文本输出（全部 bot_admin）；只读快照，写操作经 ``LifeStore.mutate``。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .config import LifeConfig
from .lifecycle import DAY, term_key
from .persona_bridge import Presence
from .stores import LifeStore

HELP_TEXT = """🌐 小青的网上生活

/life [status]        开关、此刻作息、今日次数、最近一次决策
/life recent          最近 8 条经历
/life slang           词条前 15
/life threads         小课题
/life memes           梗图列表（梗图收集尚未接入）
/life run             立刻安排一次上网（跳过概率与间隔）
/life forget <词>     删除词条
/life meme on|off <短ID>  开关一张梗图"""

_AVAILABILITY = {"free": "空闲", "busy": "在忙", "asleep": "睡着"}
_CHOICES      = {
    "rest"           : "休息",
    "wind_down"      : "睡前整理",
    "thread_continue": "续查小课题",
    "hot_scroll"     : "刷热榜",
    "interest_dive"  : "按兴趣搜",
    "term_lookup"    : "查词",
}


def _age(now: float, at: float) -> str:
    gap = max(0.0, now - at)
    if gap < 3600:
        return f"{int(gap // 60)} 分钟前"
    if gap < DAY:
        return f"{int(gap // 3600)} 小时前"
    return f"{int(gap // DAY)} 天前"


def status_text(store: LifeStore, cfg: LifeConfig, presence: Presence, moment: datetime) -> str:
    state    = store.state
    decision = state.get("last_decision") or {}
    now      = moment.timestamp()
    same_day = state.get("day") == moment.strftime("%Y-%m-%d")
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
    open_threads = sum(1 for t in store.threads if t.get("status") in ("open", "reviewing"))
    lines.append(
        f"经历 {len(store.episodes)} 条，词条 {len(store.terms)} 个，表达 {len(store.expressions)} 条，"
        f"兴趣 {len(store.interests)} 个，进行中小课题 {open_threads} 个，待查词 {len(state.get('lookup_queue', []))} 个"
    )
    return "\n".join(lines)


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


def threads_text(store: LifeStore) -> str:
    if not store.threads:
        return "没有小课题。"
    return "\n".join(
        f"[{t.get('status', '')}] {t.get('title', '')}（第 {t.get('steps', 0)} 步）"
        + (f" 收获：{t['takeaway']}" if t.get("takeaway") else "")
        for t in store.threads
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
        "status" : "status",
        "状态"     : "status",
        "recent" : "recent",
        "最近"     : "recent",
        "slang"  : "slang",
        "词条"     : "slang",
        "threads": "threads",
        "小课题"    : "threads",
        "memes"  : "memes",
        "梗图"     : "memes",
        "run"    : "run",
        "运行"     : "run",
        "forget" : "forget",
        "忘记"     : "forget",
        "meme"   : "meme",
        "help"   : "help",
        "帮助"     : "help",
    }
    return aliases.get(head.lower(), "unknown"), rest.strip()
