"""整理的编排：门控、预算、四步的顺序和提交。

- 一个范围在夜间窗口里且有新事件，或者积压的新事件够多时整理；群友说的话多的范围先整理。
- 一批里没有人说的话（只有她自己的消息和推送）时不调模型，水位照样前移。
- 每天的模型调用有上限，先占预算并落盘再调用，失败不退还；``force`` 时只记账不拦。
- 开始时记下范围的代际，提交前核对；期间被清空则放弃。水位只在一批提交成功后前移。
- 夜间窗口里每天一次：没有新事件的范围也重写待重写的页面（预算不够时先追加的、到了日子的），
  再整理自我页、核对、全量清理。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from core.ai import first_json_object

from .. import topics as topics_mod
from ..config import MemoryConfig
from ..model import GLOBAL, GROUP_SECTIONS, PERSON_SECTIONS, Event, Note, Page, text_limit
from ..render import page_lines
from ..scopes import SELF_SUBJECT, person_subject
from ..store import MemoryStore
from . import extract, pages, prune, verify
from .common import Complete, today_line

logger = logging.getLogger("plugins.xiaoqing_memory")

# 同一批消息连着这么多次整理都读不出模型的输出，就放过这一批：不然这个范围之后的消息永远整理不到。
_UNPARSED_TRIES = 3


@dataclass
class Deps:
    complete: Complete
    bot_name: str
    zone    : ZoneInfo
    clock   : Callable[[], float] = time.time

    def day(self, now: float | None = None) -> str:
        return datetime.fromtimestamp(self.clock() if now is None else now, self.zone).strftime(
            "%Y-%m-%d"
        )


class Counters(dict[str, int]):
    def add(self, name: str, value: int = 1) -> None:
        if value:
            self[name] = self.get(name, 0) + value

    def merge(self, other: dict[str, Any]) -> None:
        for name, value in other.items():
            if isinstance(value, int):
                self.add(name, value)


def _pages_text(
    store: MemoryStore, scope: str, subjects: set[str], notes: dict[str, Note], now: float
) -> str:
    rows: list[str] = []
    for subject in sorted(subjects):
        page = store.page(scope, subject)
        if page is None:
            continue
        order = GROUP_SECTIONS if subject.startswith("group:") else PERSON_SECTIONS
        title = (
            "这个群"
            if subject.startswith("group:")
            else f"{(page.names or ('',))[0]}<{subject.split(':', 1)[1]}>"
        )
        rows.append(f"[{title}] {page.brief}")
        rows.extend(
            "  " + text
            for _, text, _ in page_lines(page, notes, now=now, stale_days=3650, order=order)
        )
    return "\n".join(rows)[:2500]


def _reserve(
    store: MemoryStore, cfg: MemoryConfig, deps: Deps, counters: Counters, *, force: bool
) -> bool:
    if store.reserve_calls(deps.day(), 1, cfg.max_llm_calls_per_day, force=force):
        counters.add("llm.calls")
        return True
    counters.add("llm.budget_exhausted")
    return False


async def extract_scope(
    store: MemoryStore, cfg: MemoryConfig, deps: Deps, scope: str, *, force: bool = False
) -> Counters:
    """第一步：水位之后的事件写成笔记，逐批提交。"""

    counters   = Counters()
    generation = store.generation(scope)
    watermark  = int(store.scope_state(scope).get("watermark") or 0)
    events     = store.events.after(scope, watermark)
    for batch in extract.batches(events, cfg.batch_chars):
        if not any(from_people(event) for event in batch):
            # 记忆记的是人：只有她自己的消息和推送的一批写不出笔记，不花调用。
            store.update_scope(scope, watermark=batch[-1].seq)
            counters.add("extract.skipped")
            continue
        if not _reserve(store, cfg, deps, counters, force=force):
            break
        now      = deps.clock()
        notes    = store.notes(scope)
        known    = extract.known_notes(notes, scope, batch)
        subjects = {note.subject for note in known.values()} | {
            person_subject(event.user_id) for event in batch if event.user_id
        }
        messages = extract.build_messages(
            bot_name   = deps.bot_name,
            scope      = scope,
            batch      = batch,
            known      = list(known.values()),
            pages_text = _pages_text(store, scope, subjects, notes, now),
            zone       = deps.zone,
            today      = today_line(now, deps.zone),
        )
        try:
            raw = await deps.complete(messages)
        except Exception as exc:
            counters.add("extract.failed")
            logger.warning(
                "xiaoqing_memory step=extract.failed scope=%s error_type=%s",
                scope,
                type(exc).__name__,
            )
            break
        if store.generation(scope) != generation:
            counters.add("extract.reset")
            break
        parsed = first_json_object(raw)
        if parsed is None:
            # 读不出对象和“这一批没有要记的”（空对象）是两回事：前者这一批不算整理过。
            counters.add("extract.unparsed")
            tries = _unparsed_tries(store, scope, batch) + 1
            logger.warning(
                "xiaoqing_memory step=extract.unparsed scope=%s events=%d tries=%d",
                scope,
                len(batch),
                tries,
            )
            if tries < _UNPARSED_TRIES:
                store.update_scope(scope, unparsed={"first_seq": batch[0].seq, "tries": tries})
                break
            store.update_scope(scope, watermark=batch[-1].seq, unparsed=None)
            counters.add("extract.abandoned")
            continue
        result = extract.parse(
            parsed,
            scope = scope,
            batch = batch,
            known = known,
            now   = now,
            zone  = deps.zone,
        )
        merged: dict[str, Note] = {}
        for note in [*result.touched, *result.added, *result.superseded]:
            merged[note.id] = note
        if merged:
            store.put_notes(scope, merged.values())
        changes: dict[str, Any] = {
            "watermark"           : batch[-1].seq,
            "last_consolidated_at": now,
            "unparsed"            : None,
        }
        if result.topics:
            changes["topics"] = topics_mod.merge(
                store.scope_state(scope).get("topics"), result.topics, now
            )
        store.update_scope(scope, **changes)
        counters.add("extract.batches")
        counters.add("extract.events", len(batch))
        counters.add("notes.added", len(result.added))
        counters.add("notes.touched", len(result.touched))
        counters.add("notes.superseded", len(result.superseded))
        counters.add("topics.heard", len(result.topics))
        counters.add("extract.rejected", sum(result.rejected.values()))
        logger.info(
            "xiaoqing_memory step=extract scope=%s events=%d added=%d touched=%d superseded=%d rejected=%s",
            scope,
            len(batch),
            len(result.added),
            len(result.touched),
            len(result.superseded),
            dict(result.rejected),
        )
    return counters


def _unparsed_tries(store: MemoryStore, scope: str, batch: list[Event]) -> int:
    """从这条消息起的一批已经连着几次读不出模型的输出。"""

    record = store.scope_state(scope).get("unparsed")
    if not isinstance(record, dict) or record.get("first_seq") != batch[0].seq:
        return 0
    return int(record.get("tries") or 0)


async def rewrite_page(
    store: MemoryStore,
    cfg: MemoryConfig,
    deps: Deps,
    scope: str,
    subject: str,
    *,
    force: bool       = False,
    allow_model: bool = True,
) -> Counters:
    """第二步：重写一个对象的页面；没有预算或失败时确定性追加。"""

    counters   = Counters()
    now        = deps.clock()
    generation = store.generation(scope)
    readable   = store.notes_for([scope, GLOBAL])
    notes      = pages.subject_notes(readable, scope, subject, now)
    old        = store.page(scope, subject)
    if not notes:
        return counters
    page: Page | None = None
    if allow_model and _reserve(store, cfg, deps, counters, force=force):
        names = sorted(
            {str(note.data.get("name") or "") for note in notes if note.data.get("name")}
        )
        try:
            raw = await deps.complete(
                pages.build_messages(
                    bot_name = deps.bot_name,
                    subject  = subject,
                    page     = old,
                    notes    = notes,
                    zone     = deps.zone,
                    names    = names[:4],
                    today    = today_line(now, deps.zone),
                )
            )
            page, reason = pages.parse_page(
                raw, subject=subject, scope=scope, notes=notes, old=old, now=now
            )
            if page is None:
                counters.add(f"pages.invalid.{reason}")
        except Exception as exc:
            counters.add("pages.failed")
            logger.warning(
                "xiaoqing_memory step=page.failed scope=%s error_type=%s", scope, type(exc).__name__
            )
    if page is None:
        cited = old.note_ids() if old else set()
        fresh = [note for note in notes if note.status == "active" and note.id not in cited]
        page  = pages.append_fallback(old, subject, scope, fresh)
        if page is None or (old is not None and page == old):
            return counters
        counters.add("pages.appended")
    else:
        counters.add("pages.rewritten")
    if store.generation(scope) != generation:
        counters.add("pages.reset")
        return counters
    store.put_page(page)
    logger.info(
        "xiaoqing_memory step=page scope=%s subject=%s chars=%d lines=%d",
        scope,
        subject.split(":", 1)[0],
        page.char_count(),
        len(page.lines()),
    )
    return counters


async def _rewrite_pending(
    store: MemoryStore, cfg: MemoryConfig, deps: Deps, scope: str, *, force: bool, budget: int
) -> Counters:
    counters = Counters()
    pending  = pages.pending_subjects(store.notes(scope), store.pages(scope), scope, deps.clock())
    for index, (subject, _count) in enumerate(pending):
        counters.merge(
            await rewrite_page(
                store, cfg, deps, scope, subject, force=force, allow_model=index < budget
            )
        )
    return counters


async def consolidate_scope(
    store: MemoryStore,
    cfg: MemoryConfig,
    deps: Deps,
    scope: str,
    *,
    force: bool          = False,
    rewrites: int | None = None,
) -> Counters:
    counters = Counters()
    async with store.locks.hold(f"consolidate:{scope}"):
        counters.merge(await extract_scope(store, cfg, deps, scope, force=force))
        budget = cfg.max_page_rewrites if rewrites is None else rewrites
        counters.merge(await _rewrite_pending(store, cfg, deps, scope, force=force, budget=budget))
        counters.merge(prune_scope(store, cfg, scope, deps.clock()))
    return counters


async def rewrite_quiet_scope(
    store: MemoryStore, cfg: MemoryConfig, deps: Deps, scope: str, *, rewrites: int
) -> Counters:
    """没有新事件的范围：只重写待重写的页面（上线当天预算不够先追加的、写着的日子已经过了的）。"""

    async with store.locks.hold(f"consolidate:{scope}"):
        return await _rewrite_pending(store, cfg, deps, scope, force=False, budget=rewrites)


async def consolidate_self(
    store: MemoryStore, cfg: MemoryConfig, deps: Deps, *, force: bool = False
) -> Counters:
    counters = Counters()
    async with store.locks.hold("consolidate:global"):
        notes = store.notes(GLOBAL)
        if force or pages.self_pending(notes, store.page(GLOBAL, SELF_SUBJECT)):
            counters.merge(await rewrite_page(store, cfg, deps, GLOBAL, SELF_SUBJECT, force=force))
        counters.merge(prune_scope(store, cfg, GLOBAL, deps.clock()))
    return counters


async def verify_lines(
    store: MemoryStore, cfg: MemoryConfig, deps: Deps, *, calls: int, force: bool = False
) -> Counters:
    """第三步：抽几行页面内容对照原文核对。"""

    counters = Counters()
    if calls <= 0:
        return counters
    scopes                     = [scope for scope in store.known_scopes() if scope != GLOBAL]
    all_pages: dict[str, Page] = {}
    notes                      = store.notes_for(scopes)
    for scope in scopes:
        for subject, page in store.pages(scope).items():
            all_pages[f"{scope}:{subject}"] = page
    chosen = verify.candidates(all_pages, notes, store.events, limit=calls * verify.LINES_PER_CALL)
    for start in range(0, len(chosen), verify.LINES_PER_CALL):
        batch = chosen[start : start + verify.LINES_PER_CALL]
        if not _reserve(store, cfg, deps, counters, force=force):
            break
        try:
            raw = await deps.complete(verify.build_messages(deps.bot_name, batch, deps.zone))
        except Exception as exc:
            counters.add("verify.failed")
            logger.warning("xiaoqing_memory step=verify.failed error_type=%s", type(exc).__name__)
            break
        verdicts = verify.parse(raw, batch)
        counters.merge(verify.verdict_counts(verdicts))
        now                                                                     = deps.clock()
        by_page: dict[tuple[str, str], list[tuple[verify.Candidate, str, str]]] = {}
        for item in verdicts:
            by_page.setdefault((item[0].scope, item[0].subject), []).append(item)
        for (scope, subject), items in by_page.items():
            async with store.locks.hold(f"consolidate:{scope}"):
                current = store.page(scope, subject)
                if current is None:
                    continue
                applied = verify.apply_verdicts(current, items)
                changed = []
                for note_id in applied.confirmed | applied.retract:
                    note = store.note(scope, note_id)
                    if note is None:
                        continue
                    if note_id in applied.retract:
                        changed.append(note.with_changes(status="retracted", observed_at=now))
                        continue
                    note = note.with_changes(verified_at=now)
                    if note_id in applied.corrected:
                        note = note.with_changes(
                            text=applied.corrected[note_id][: text_limit(note.kind)]
                        )
                    changed.append(note)
                if changed:
                    store.put_notes(scope, changed)
                store.put_page(applied.page)
        logger.info(
            "xiaoqing_memory step=verify lines=%d verdicts=%s",
            len(batch),
            [v for _, v, _ in verdicts],
        )
    return counters


def prune_scope(store: MemoryStore, cfg: MemoryConfig, scope: str, now: float) -> Counters:
    """第四步：清理笔记、事件和页面里失去依据的行。"""

    counters = Counters()
    dead     = (
        store.events.prune(scope, keep_days=cfg.event_keep_days, now=now)
        if scope != GLOBAL
        else set()
    )
    result = prune.prune_notes(store.notes(scope), cfg, now, dead_seqs=dead)
    if result.changed:
        store.put_notes(scope, result.changed.values())
    if result.removed:
        store.remove_notes(scope, result.removed)
    counters.merge(result.counts())
    counters.add("events.pruned", len(dead))
    notes = store.notes_for([scope, GLOBAL])
    for subject, page in store.pages(scope).items():
        kept = pages.drop_dead_lines(page, notes)
        if kept is None:
            store.drop_pages(scope, [subject])
            counters.add("pages.dropped")
        elif kept != page:
            store.put_page(kept)
    return counters


def in_night(cfg: MemoryConfig, moment: datetime) -> bool:
    minute = moment.hour * 60 + moment.minute
    start, end = cfg.night_start_minute, cfg.night_end_minute
    if start <= end:
        return start <= minute < end
    return minute >= start or minute < end


def from_people(event: Event) -> bool:
    """别人说的话，或者她替人办的事；她自己的消息和推送不算。"""

    return (event.kind == "message" and event.role == "user") or event.kind == "task"


def backlog(store: MemoryStore, scope: str) -> int:
    watermark = int(store.scope_state(scope).get("watermark") or 0)
    return len(store.events.after(scope, watermark))


def people_backlog(store: MemoryStore, scope: str) -> int:
    watermark = int(store.scope_state(scope).get("watermark") or 0)
    return sum(1 for event in store.events.after(scope, watermark) if from_people(event))


async def nightly_pass(
    store: MemoryStore, cfg: MemoryConfig, deps: Deps, moment: datetime
) -> Counters:
    """定时检查：哪些范围该整理了；夜间窗口里每天一次另做自我页、核对和全量清理。"""

    counters = Counters()
    night    = in_night(cfg, moment)
    rewrites = cfg.max_page_rewrites
    scopes   = [scope for scope in store.known_scopes() if scope != GLOBAL]
    # 群友说的话多的范围先整理：上线当天预算不够时，先轮到人多的群，只有推送的会话排最后。
    pending = sorted(
        ((people_backlog(store, scope), backlog(store, scope), scope) for scope in scopes),
        key     = lambda item: (item[0], item[1]),
        reverse = True,
    )
    for _people, waiting, scope in pending:
        if waiting <= 0 or not (night or waiting >= cfg.backlog_trigger_events):
            continue
        result = await consolidate_scope(store, cfg, deps, scope, rewrites=rewrites)
        rewrites = max(0, rewrites - result.get("pages.rewritten", 0))
        counters.merge(result)
    day   = deps.day()
    marks = store.verify_state()
    if night and marks.get("last_day") != day:
        for scope in scopes:
            if rewrites <= 0:
                break
            result = await rewrite_quiet_scope(store, cfg, deps, scope, rewrites=rewrites)
            rewrites = max(0, rewrites - result.get("pages.rewritten", 0))
            counters.merge(result)
        counters.merge(await consolidate_self(store, cfg, deps))
        counters.merge(await verify_lines(store, cfg, deps, calls=cfg.verify_calls_per_night))
        for scope in scopes:
            counters.merge(prune_scope(store, cfg, scope, deps.clock()))
        marks["last_day"] = day
        store.save_state()
    if counters:
        store.bump(day, dict(counters), save=True)
        logger.info("xiaoqing_memory step=consolidate.summary counters=%s", dict(counters))
    return counters
