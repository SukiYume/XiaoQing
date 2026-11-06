"""主题档案：她自己“这件事的来龙去脉”（确定性，不调模型）。

档案是**同步出来的快照**：``sync`` 幂等地从当前小课题、作品和喜欢 / mixed 的经历更新档案，
档案里存自己的快照副本，小课题和经历被清理后档案仍在。不依赖任何进程级全局钩子（那会在不同存储之间串）。
档案只是依据，不是词条、表达、兴趣的来源。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from .config import LifeConfig
from .safety import normalize_for_match, overlap
from .stores import LifeStore
from .works import episode_related, topics_match

DAY              = 86400.0
MAX_QUESTIONS    = 12
MAX_CORRECTIONS  = 6
MAX_WORKS        = 4
MAX_HIGHLIGHTS   = 6
MAX_DOSSIERS     = 60
FIRST_KEPT       = 2
LAST_KEPT        = 4
RECALL_QUESTIONS = 3
SUGGEST_COOLDOWN = 6 * 3600
MIN_OVERLAP      = 2
ORIGIN_CHARS     = 60

_WORK_STATE = {"drafting": "在整理", "done": "整理完了", "shelved": "先放着了"}


def _find_key(dossiers: Mapping[str, dict[str, Any]], topic: str) -> str | None:
    if topic in dossiers:
        return topic
    return next((k for k in dossiers if topics_match(k, topic)), None)


def _ensure(
    dossiers: dict[str, dict[str, Any]], topic: str, started: float, origin: str
) -> dict[str, Any]:
    key   = _find_key(dossiers, topic) or topic[:12]
    entry = dossiers.get(key)
    if entry is None:
        entry = {
            "topic"      : key,
            "started_at" : started,
            "origin"     : origin[:ORIGIN_CHARS],
            "questions"  : [],
            "corrections": [],
            "works"      : [],
            "highlights" : [],
            "last_active": started,
        }
        dossiers[key] = entry
    entry["started_at"] = min(float(entry["started_at"]), started)
    if not entry.get("origin") and origin:
        entry["origin"] = origin[:ORIGIN_CHARS]
    return entry


def _touch(entry: dict[str, Any], at: float) -> None:
    entry["last_active"] = max(float(entry.get("last_active", 0.0)), at)


def _sync_thread(dossiers: dict[str, dict[str, Any]], thread: Mapping[str, Any]) -> None:
    topic = str(thread.get("topic") or "").strip()
    if not topic:
        return
    created = float(thread.get("created_at", 0.0))
    entry = _ensure(dossiers, topic, created, str(thread.get("why") or thread.get("title", "")))
    status = str(thread.get("status", "open"))
    reason = {"parked": thread.get("park_reason"), "dropped": thread.get("drop_reason")}.get(status)
    row = {
        "thread_id": thread.get("id", ""),
        "title"    : str(thread.get("title", ""))[:20],
        "status"   : status,
        "takeaway" : str(thread.get("takeaway") or "")[:80],
        "reason"   : str(reason or "")[:30],
        "at"       : float(thread.get("updated_at", created)),
    }
    questions: list[dict[str, Any]] = entry["questions"]
    for index, old in enumerate(questions):
        if old.get("thread_id") == row["thread_id"]:
            questions[index] = row
            break
    else:
        questions.append(row)
    questions.sort(key=lambda q: float(q.get("at", 0.0)))
    del questions[:-MAX_QUESTIONS]
    corrections: list[dict[str, Any]] = entry["corrections"]
    for fix in thread.get("corrections", []):
        pair = (str(fix.get("before", "")), str(fix.get("after", "")))
        if not any((c["before"], c["after"]) == pair for c in corrections):
            corrections.append(
                {
                    "before"        : pair[0],
                    "after"         : pair[1],
                    "at"            : float(fix.get("at", 0.0)),
                    "last_suggested": 0.0,
                }
            )
    corrections.sort(key=lambda c: float(c.get("at", 0.0)))
    del corrections[:-MAX_CORRECTIONS]
    _touch(entry, row["at"])


def _sync_work(dossiers: dict[str, dict[str, Any]], work: Mapping[str, Any]) -> None:
    topic = str(work.get("topic") or "").strip()
    if not topic:
        return
    created = float(work.get("created_at", 0.0))
    entry   = _ensure(dossiers, topic, created, str(work.get("why") or work.get("title", "")))
    row     = {
        "work_id": work.get("id", ""),
        "title"  : str(work.get("title", ""))[:20],
        "form"   : work.get("form", ""),
        "status" : work.get("status", "drafting"),
    }
    works: list[dict[str, Any]] = entry["works"]
    for index, old in enumerate(works):
        if old.get("work_id") == row["work_id"]:
            works[index] = row
            break
    else:
        works.append(row)
    del works[:-MAX_WORKS]
    _touch(entry, float(work.get("updated_at", created)))


def _sync_highlights(entry: dict[str, Any], episodes: list[dict[str, Any]]) -> None:
    """喜欢或 mixed 的相关经历：保留最早 2 条 + 最近 4 条（用并集，经历被清理后旧的仍在）。"""

    rows: dict[str, dict[str, Any]] = {h["episode_id"]: h for h in entry["highlights"]}
    for e in episodes:
        if (
            e.get("feeling") in ("like", "mixed")
            and not e.get("auto")
            and episode_related(e, entry["topic"])
        ):
            rows.setdefault(
                str(e["id"]),
                {
                    "episode_id": str(e["id"]),
                    "title"     : str(e.get("title", ""))[:40],
                    "reaction"  : str(e.get("reaction", ""))[:40],
                    "at"        : float(e.get("at", 0.0)),
                },
            )
    ordered = sorted(rows.values(), key=lambda h: float(h["at"]))
    if len(ordered) > MAX_HIGHLIGHTS:
        ordered = [*ordered[:FIRST_KEPT], *ordered[-LAST_KEPT:]]
    entry["highlights"] = ordered
    if ordered:
        _touch(entry, float(ordered[-1]["at"]))


def sync(store: LifeStore, cfg: LifeConfig, now: float) -> None:
    """幂等：从当前小课题、作品、喜欢的经历更新档案；超过 ``dossier_keep_days`` 不活跃的删除，总数 ≤60。"""

    dossiers = store.dossiers
    for thread in store.threads:
        _sync_thread(dossiers, thread)
    for work in store.works:
        _sync_work(dossiers, work)
    for entry in dossiers.values():
        _sync_highlights(entry, store.episodes)
    for key in [
        k
        for k, e in dossiers.items()
        if now - float(e["last_active"]) > cfg.dossier_keep_days * DAY
    ]:
        del dossiers[key]
    if len(dossiers) > MAX_DOSSIERS:
        oldest = sorted(dossiers, key=lambda k: float(dossiers[k]["last_active"]))
        for key in oldest[: len(dossiers) - MAX_DOSSIERS]:
            del dossiers[key]


# ───────────────────────────── 召回 ─────────────────────────────


def _question_text(q: Mapping[str, Any]) -> str:
    status = q.get("status")
    if status == "done":
        return f"查明白了：{q.get('takeaway', '')}"
    if status == "parked":
        return f"查到一半{q.get('reason') or '资料不太够'}，先放着了"
    if status == "dropped":
        return f"不查了：{q.get('reason', '')}" if q.get("reason") else "后来不查了"
    if status == "reviewing":
        return "查得差不多了"
    return "还在查"


def recall_lines(entry: Mapping[str, Any], moment: datetime, detail_days: int) -> list[str]:
    """≤5 行：开头 + 查过（≤3，最近的在前）+ 改过想法（≤1）+ 整理的东西（≤1）+ 印象深的（≤1，超过细节期）。"""

    started   = datetime.fromtimestamp(float(entry["started_at"]), moment.tzinfo)
    origin    = f"，起因：{entry['origin']}" if entry.get("origin") else ""
    lines     = [f"关于{entry['topic']}（{started.month}月{started.day}日开始留意的{origin}）："]
    questions = sorted(
        entry.get("questions", []), key=lambda q: float(q.get("at", 0.0)), reverse=True
    )
    for q in questions[:RECALL_QUESTIONS]:
        lines.append(f"- 查过：{q['title']}（{_question_text(q)}）")
    fixes = sorted(
        entry.get("corrections", []), key=lambda c: float(c.get("at", 0.0)), reverse=True
    )
    if fixes:
        lines.append(f"- 后来改过想法：之前以为{fixes[0]['before']}，后来发现{fixes[0]['after']}")
    if entry.get("works"):
        work = entry["works"][-1]
        lines.append(
            f"- 整理的东西：《{work['title']}》（{_WORK_STATE.get(work.get('status'), '在整理')}）"
        )
    cutoff = moment.timestamp() - detail_days * DAY
    old    = [h for h in entry.get("highlights", []) if float(h["at"]) < cutoff]
    if old:
        pick = old[-1]
        lines.append(f"- 印象深的：「{pick['title']}」，当时觉得{pick['reaction']}")
    return lines


def _score(entry: Mapping[str, Any], context: str) -> int:
    titles = " ".join(str(q.get("title", "")) for q in entry.get("questions", []))
    score  = overlap(context, f"{entry['topic']} {titles}")
    topic  = normalize_for_match(str(entry["topic"]))
    if len(topic) >= 2 and topic in normalize_for_match(context):
        score += MIN_OVERLAP
    return score


def find_recall(
    dossiers: Mapping[str, dict[str, Any]], context: str, *, fallback: bool
) -> dict[str, Any] | None:
    """重合最高的档案；没有重合但明显是在追问时（``fallback``）取最近活跃的。"""

    scored = [(_score(e, context), e) for e in dossiers.values()]
    scored = [p for p in scored if p[0] >= MIN_OVERLAP]
    if scored:
        return max(scored, key=lambda p: (p[0], float(p[1]["last_active"])))[1]
    if fallback and dossiers:
        return max(dossiers.values(), key=lambda e: float(e["last_active"]))
    return None


def correction_line(
    dossiers: Mapping[str, dict[str, Any]], context: str, now: float
) -> tuple[str, dict[str, Any] | None]:
    """话题和某个档案的更正对上：（一行, 那条更正）。同一条更正 6 小时内只给一次。

    ``last_suggested`` 由调用方在这一行真的放进生活块之后再改（被长度预算挤掉的不算给过）。
    """

    best: tuple[int, dict[str, Any], dict[str, Any]] | None = None
    for entry in dossiers.values():
        score = _score(entry, context)
        if score < MIN_OVERLAP:
            continue
        for fix in entry.get("corrections", []):
            if now - float(fix.get("last_suggested", 0.0)) < SUGGEST_COOLDOWN:
                continue
            if best is None or score > best[0]:
                best = (score, entry, fix)
    if best is None:
        return "", None
    _, entry, fix = best
    return (
        f"你在{entry['topic']}上改过一个想法：之前以为{fix['before']}，后来发现{fix['after']}",
        fix,
    )


def command_text(entry: Mapping[str, Any], moment: datetime) -> str:
    """``/life dossier <话题>``：同样的格式，不受细节期限制。"""

    return "\n".join(recall_lines(entry, moment, 0))


def list_text(dossiers: Mapping[str, dict[str, Any]], moment: datetime) -> str:
    if not dossiers:
        return "还没有主题档案。"
    rows = sorted(dossiers.values(), key=lambda e: float(e["last_active"]), reverse=True)
    now = moment.timestamp()
    return "\n".join(
        f"{e['topic']}：问题 {len(e['questions'])} 个，作品 {len(e['works'])} 个，"
        f"最近 {int((now - float(e['last_active'])) // DAY)} 天前动过"
        for e in rows
    )
