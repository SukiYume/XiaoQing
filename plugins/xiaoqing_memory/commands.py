"""``/mem`` 的子命令解析和文字输出。"""

from __future__ import annotations

import time
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

from .model import GLOBAL, SECTION_TITLES, Page
from .render import ago
from .scopes import SELF_SUBJECT, group_subject, person_subject, valid_scope
from .store import MemoryStore
from .text import clip

HELP_TEXT = """/mem 小青的统一记忆（仅 Bot 管理员，私聊）
/mem status — 各范围的事件、笔记、页面数量，水位，今天用了几次模型
/mem stats — 最近 7 天的整理计数
/mem page <范围> [QQ号|group|self] — 看整理好的页面
/mem search <范围> <关键词> — 查笔记、页面和原始记录
/mem why <笔记ID> — 这条笔记的出处原文
/mem forget <范围> [QQ号|笔记ID] — 清空范围，或删一个人、一条笔记
/mem consolidate [范围] — 立刻整理一次（不受每日预算限制）
/mem rebuild <范围> — 从笔记重写这个范围的全部页面
范围写成 global、g<群号> 或 u<QQ号>。"""

ACTIONS = {
    "status"     : ("status", "状态"),
    "stats"      : ("stats", "统计"),
    "page"       : ("page", "页面"),
    "search"     : ("search", "搜索"),
    "why"        : ("why", "出处"),
    "forget"     : ("forget", "忘记"),
    "consolidate": ("consolidate", "整理"),
    "rebuild"    : ("rebuild", "重建"),
    "help"       : ("help", "帮助"),
}


def route(args: str) -> tuple[str, str]:
    head, _, rest = str(args or "").strip().partition(" ")
    word = head.strip().lower()
    if not word:
        return "status", ""
    for action, names in ACTIONS.items():
        if word in names:
            return action, rest.strip()
    return "help", ""


def status_text(store: MemoryStore, *, day: str, limit: int, enabled: bool) -> str:
    lines = [
        f"统一记忆：{'开启' if enabled else '关闭'}；今天整理用了 {store.calls_used(day)}/{limit} 次模型"
    ]
    now = time.time()
    for scope in store.known_scopes():
        state  = store.scope_state(scope)
        events = store.events.events(scope) if scope != GLOBAL else []
        notes  = store.notes(scope)
        active = Counter(note.kind for note in notes.values() if note.is_active(now))
        pages  = store.pages(scope)
        last   = float(state.get("last_event_at") or 0)
        kinds  = "、".join(f"{kind}×{count}" for kind, count in sorted(active.items())) or "无"
        lines.append(
            f"[{scope}] 事件 {len(events)}（水位 e{state.get('watermark', 0)}，"
            f"待整理 {sum(1 for e in events if e.seq > int(state.get('watermark') or 0))}）；"
            f"笔记 {kinds}；页面 {len(pages)}"
            + (f"；最近写入 {ago(max(0.0, now - last))}" if last else "")
        )
    return "\n".join(lines)


def stats_text(store: MemoryStore) -> str:
    stats = store.stats()
    if not stats:
        return "还没有统计。"
    rows = []
    for day in sorted(stats)[-7:]:
        values = stats[day]
        parts  = "，".join(f"{name}={values[name]}" for name in sorted(values))
        rows.append(f"{day}：{parts}")
    return "\n".join(rows)


def render_page(page: Page) -> str:
    rows = [f"《{page.subject}》@{page.scope}  v{page.version}"]
    if page.names:
        rows.append("称呼：" + "、".join(page.names))
    if page.brief:
        rows.append(page.brief)
    for section, items in page.sections.items():
        if not items:
            continue
        rows.append(f"〔{SECTION_TITLES.get(section, section)}〕")
        rows.extend(f"- {line.text}  ← {','.join(line.note_ids)}" for line in items)
    return "\n".join(rows)


def page_text(store: MemoryStore, scope: str, who: str) -> str:
    if not valid_scope(scope):
        return "范围写成 global、g<群号> 或 u<QQ号>。"
    pages = store.pages(scope)
    if not who:
        if not pages:
            return f"{scope} 还没有页面。"
        return "\n".join(
            f"{subject}：{page.brief or '（无简介）'}（{page.char_count()} 字）"
            for subject, page in sorted(pages.items())
        )
    subject = {
        "group": group_subject(scope),
        "self" : SELF_SUBJECT,
    }.get(who, person_subject(int(who)) if who.isdigit() else who)
    page = pages.get(subject)
    return render_page(page) if page else f"{scope} 里没有 {subject} 的页面。"


def why_text(store: MemoryStore, note_id: str, zone: ZoneInfo) -> str:
    note = store.find_note(note_id.strip())
    if note is None:
        return "没有这条笔记。"
    rows = [
        f"{note.id} [{note.kind}] @{note.scope} 关于 {note.subject}",
        f"内容：{note.text}",
        f"来源：{note.origin}，可信度：{note.confidence}，用法：{note.usage}，状态：{note.status}",
        f"取用 {note.use_count} 次" + (f"，替换了 {note.supersedes}" if note.supersedes else ""),
    ]
    if note.quote:
        rows.append(f"摘录：「{note.quote}」")
    for source in note.sources:
        if source.get("type") != "event":
            rows.append(f"出处：{source.get('type')} {source.get('key', '')}")
            continue
        event = store.events.get(
            str(source.get("scope") or note.scope), int(source.get("seq") or 0)
        )
        if event is None:
            rows.append(f"出处 e{source.get('seq')}：原文已不在保留期内")
            continue
        when = datetime.fromtimestamp(event.ts, zone).strftime("%Y-%m-%d %H:%M")
        who  = "她" if event.role == "assistant" else (event.name or "有人")
        rows.append(f"出处 e{event.seq} [{when}] {who}：{clip(event.text, 300)}")
    return "\n".join(rows)
