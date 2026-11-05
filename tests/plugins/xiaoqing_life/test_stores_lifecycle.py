"""存储（损坏恢复、重启保留、并发、预算跨日）、词条/兴趣/经历生命周期与小课题状态机。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from plugins.xiaoqing_life import lifecycle as lc
from plugins.xiaoqing_life import threads as th
from plugins.xiaoqing_life.config import LifeConfig
from plugins.xiaoqing_life.stores import LifeStore, reserve_llm_call, roll_day
from tests.plugins.xiaoqing_life.support import make_cfg

DAY = lc.DAY
NOW = 1_800_000_000.0


def _store(tmp_path: Path) -> LifeStore:
    store = LifeStore(tmp_path / "life")
    store.load()
    return store


@pytest.mark.asyncio
async def test_state_survives_restart_and_writes_a_backup(tmp_path: Path) -> None:
    store = _store(tmp_path)
    await store.mutate(lambda s: s.episodes.append({"id": "e1", "at": NOW}))
    await store.mutate(lambda s: s.terms.update({"k": {"term": "k"}}))
    again = _store(tmp_path)
    assert again.episodes == [{"id": "e1", "at": NOW}] and "k" in again.terms
    assert (tmp_path / "life" / "episodes.json.bak").exists()


@pytest.mark.asyncio
async def test_corrupt_main_file_recovers_from_backup(tmp_path: Path) -> None:
    store = _store(tmp_path)
    await store.mutate(lambda s: s.episodes.append({"id": "e1", "at": NOW}))
    (tmp_path / "life" / "episodes.json").write_text("{not json", encoding="utf-8")
    assert _store(tmp_path).episodes == [{"id": "e1", "at": NOW}]


def test_unreadable_files_are_quarantined_and_start_empty(tmp_path: Path) -> None:
    folder = tmp_path / "life"
    folder.mkdir()
    (folder / "terms.json").write_text("garbage", encoding="utf-8")
    (folder / "episodes.json").write_text(json.dumps({"wrong": "type"}), encoding="utf-8")
    store = _store(tmp_path)
    assert store.terms == {} and store.episodes == []
    assert (folder / "terms.json.corrupt").exists() and (folder / "episodes.json.corrupt").exists()
    assert store.state["llm_calls_today"] == 0


@pytest.mark.asyncio
async def test_concurrent_mutations_serialize_and_readers_see_a_consistent_snapshot(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)

    def add(n: int):
        return lambda s: s.episodes.append({"id": f"e{n}", "at": NOW + n})

    await asyncio.gather(*(store.mutate(add(n)) for n in range(25)))
    assert len(store.episodes) == 25
    assert len(_store(tmp_path).episodes) == 25


@pytest.mark.asyncio
async def test_llm_budget_is_counted_before_the_call_and_resets_by_day(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert await reserve_llm_call(store, "2026-11-05", 2)
    assert await reserve_llm_call(store, "2026-11-05", 2)
    assert not await reserve_llm_call(store, "2026-11-05", 2)
    assert _store(tmp_path).state["llm_calls_today"] == 2  # 重启不清零
    assert await reserve_llm_call(store, "2026-11-06", 2)  # 跨日重置
    assert store.state["llm_calls_today"] == 1
    state = {"day": "2026-11-05", "sessions_today": 3, "llm_calls_today": 9, "activity_log": [1]}
    roll_day(state, "2026-11-06")
    assert (state["sessions_today"], state["llm_calls_today"], state["activity_log"]) == (0, 0, [1])


# ───────────────────────────── 词条 ─────────────────────────────


def _term(terms, name="哈基米", item="a", now=NOW, verified=False):
    return lc.upsert_term(terms, {"term": name, "meaning": "m"}, item, now, verified=verified)


def test_term_lifecycle_candidate_known_fading_forgotten() -> None:
    cfg         = LifeConfig()  # 14 / 30 / 90
    terms: dict = {}
    _term(terms, item="a")
    lc.refresh_terms(terms, NOW, cfg)
    entry = terms["哈基米"]
    assert entry["status"] == "candidate"

    _term(terms, item="a", now=NOW + DAY)  # 同一来源不重复计数
    assert entry["sources"] == ["a"]
    _term(terms, item="b", now=NOW + DAY)
    lc.refresh_terms(terms, NOW + DAY, cfg)
    assert entry["status"] == "known" and entry["meaning"] == "m"

    seen = NOW + DAY
    lc.refresh_terms(terms, seen + 30 * DAY, cfg)
    assert entry["status"] == "known"  # 恰好 30 天还不算 fading
    lc.refresh_terms(terms, seen + 30 * DAY + 1, cfg)
    assert entry["status"] == "fading"
    lc.refresh_terms(terms, seen + 90 * DAY + 1, cfg)
    assert terms == {}


def test_candidate_without_second_source_expires_and_verified_is_known() -> None:
    cfg         = LifeConfig()
    terms: dict = {}
    _term(terms, "单来源", item="a")
    _term(terms, "百科词", item="baike:x", verified=True)
    lc.refresh_terms(terms, NOW + 14 * DAY, cfg)
    assert set(terms) == {"单来源", "百科词"}
    lc.refresh_terms(terms, NOW + 14 * DAY + 1, cfg)
    assert set(terms) == {"百科词"} and terms["百科词"]["status"] == "known"


def test_known_term_seen_again_leaves_fading() -> None:
    cfg         = LifeConfig()
    terms: dict = {}
    _term(terms, item="a")
    _term(terms, item="b")
    lc.refresh_terms(terms, NOW + 40 * DAY, cfg)
    assert terms["哈基米"]["status"] == "fading"
    _term(terms, item="c", now=NOW + 40 * DAY)
    lc.refresh_terms(terms, NOW + 40 * DAY, cfg)
    assert terms["哈基米"]["status"] == "known"


def test_term_store_is_capped_dropping_the_stalest() -> None:
    terms = {
        f"k{n}": {"term": f"k{n}", "sources": ["a", "b"], "last_seen": NOW + n, "first_seen": NOW}
        for n in range(510)
    }
    lc.refresh_terms(terms, NOW + 510, LifeConfig())
    assert len(terms) == 500 and "k0" not in terms and "k509" in terms


def test_expressions_merge_by_pattern_and_age_out() -> None:
    cfg         = LifeConfig()
    exprs: list = []
    lc.upsert_expression(exprs, {"situation": "夸", "pattern": "X基础，Y不基础"}, "a", NOW)
    lc.upsert_expression(exprs, {"situation": "夸", "pattern": "X 基础，Y 不基础"}, "b", NOW + 1)
    assert len(exprs) == 1 and exprs[0]["sources"] == ["a", "b"]
    lc.refresh_expressions(exprs, NOW + 2, cfg)
    assert exprs[0]["status"] == "known"
    lc.refresh_expressions(exprs, NOW + 91 * DAY, cfg)
    assert exprs == []


# ───────────────────────────── 兴趣 ─────────────────────────────


def test_interest_evidence_counts_each_item_once_and_stages_progress() -> None:
    interests: dict = {}
    add = lambda item, at, delta=1: lc.add_interest_evidence(  # noqa: E731
        interests, "猫", episode_id=f"e-{item}", item_id=item, delta=delta, now=at
    )
    assert add("a", NOW) and not add("a", NOW + 1)  # 同一来源不重复计
    assert interests["猫"]["stage"] == "tentative"
    assert add("b", NOW + 2)
    assert interests["猫"]["stage"] == "repeated"
    # 前一个 14 天窗口也要有 ≥2 条不同来源才算 consistent
    assert add("c", NOW - 20 * DAY) and add("d", NOW - 21 * DAY)
    assert lc.interest_stage(interests["猫"], NOW + 3) == "consistent"
    assert add("e", NOW + 4, -1)
    assert lc.interest_score(interests["猫"], NOW + 4) < 4


def test_seed_interests_start_at_one_without_evidence_and_scores_decay_by_half_life() -> None:
    interests: dict = {}
    lc.seed_interests(interests, ("天文观测",), NOW)
    entry = interests["天文观测"]
    assert entry["evidence"] == [] and entry["stage"] == "none"
    assert lc.interest_score(entry, NOW) == pytest.approx(1.0)
    assert lc.interest_score(entry, NOW + 300 * DAY) == pytest.approx(1.0)  # 种子底分不衰减
    lc.add_interest_evidence(interests, "猫", episode_id="e", item_id="i", delta=1, now=NOW)
    assert lc.interest_score(interests["猫"], NOW + 30 * DAY) == pytest.approx(0.5)
    weights = lc.interest_weights(interests, NOW)
    assert set(weights) == {"天文观测", "猫"}


def test_stale_seed_without_evidence_is_dropped_by_refresh() -> None:
    interests = {"旧": {"base": 1.0, "evidence": [], "updated_at": NOW, "stage": "none"}}
    lc.refresh_interests(interests, NOW + 200 * DAY)
    assert interests == {}


# ───────────────────────────── 经历与已看过 ─────────────────────────────


def test_episodes_expire_unless_a_live_thread_cites_them() -> None:
    cfg      = LifeConfig()
    episodes = [
        {"id": "old", "at": NOW - 46 * DAY},
        {"id": "kept", "at": NOW - 46 * DAY},
        {"id": "new", "at": NOW - 44 * DAY},
    ]
    lc.prune_episodes(episodes, NOW, cfg, {"kept"})
    assert [e["id"] for e in episodes] == ["kept", "new"]


def test_seen_items_expire_after_seven_days() -> None:
    seen = {"old": NOW - 8 * DAY, "fresh": NOW - DAY}
    lc.mark_seen(seen, ["fresh", "new"], NOW)
    assert set(seen) == {"fresh", "new"} and seen["fresh"] == NOW - DAY


# ───────────────────────────── 小课题 ─────────────────────────────


def test_thread_limits_and_similar_titles() -> None:
    cfg = make_cfg(max_open_threads=2)
    threads: list = []
    first         = th.open_thread(
        threads, cfg, title="猎户座大星云怎么拍", why="想拍", next_query="", now=NOW
    )
    assert first and first["next_query"] == "猎户座大星云怎么拍"
    assert (
        th.open_thread(threads, cfg, title="猎户座大星云怎么拍摄", why="", next_query="", now=NOW)
        is None
    )
    assert th.open_thread(threads, cfg, title="这个梗哪来的", why="", next_query="", now=NOW)
    assert th.open_thread(threads, cfg, title="第三个课题", why="", next_query="", now=NOW) is None
    assert th.open_thread(threads, cfg, title=" ", why="", next_query="", now=NOW) is None


def test_thread_steps_cooldown_and_review_transitions() -> None:
    cfg = make_cfg(thread_max_steps=3)
    threads: list = []
    thread = th.open_thread(threads, cfg, title="课题", why="", next_query="q", now=NOW)
    th.record_step(thread, cfg, episode_id="e1", note="一点", new_content=False, now=NOW)
    assert thread["cooldown_until"] == 0.0
    th.record_step(thread, cfg, episode_id="e1", note="", new_content=False, now=NOW + 10)
    assert thread["cooldown_until"] == NOW + 10 + 180 * 60 and thread["steps"] == 0
    for n in range(3):
        th.record_step(
            thread, cfg, episode_id=f"e{n}", note=f"要点{n}", new_content=True, now=NOW + n
        )
    assert thread["steps"] == 3 and thread["status"] == "reviewing"  # 步数到上限进回看
    assert len(thread["notes"]) == 3


def test_thread_answered_moves_to_reviewing_and_done_needs_known_episode_ids() -> None:
    cfg           = make_cfg()
    threads: list = []
    thread = th.open_thread(threads, cfg, title="课题", why="", next_query="", now=NOW)
    th.mark_answered(thread, episode_id="e1", note="查明白了", now=NOW + 1)
    assert thread["status"] == "reviewing"
    assert not th.finish(thread, "收获", ["e-unknown"], NOW + 2)
    assert not th.finish(thread, "  ", ["e1"], NOW + 2)
    assert th.finish(thread, "收获", ["e1"], NOW + 2) and thread["status"] == "done"
    assert th.protected_episode_ids(threads) == set()  # 已结束的不再保护经历


def test_finished_threads_are_kept_thirty_days() -> None:
    threads = [
        {"id": "a", "status": "done", "updated_at": NOW - 29 * DAY},
        {"id": "b", "status": "dropped", "updated_at": NOW - 31 * DAY},
        {
            "id"        : "c",
            "status"    : "open",
            "updated_at": NOW - 90 * DAY,
            "notes"     : [{"episode_id": "e9"}],
        },
    ]
    th.prune_threads(threads, NOW)
    assert [t["id"] for t in threads] == ["a", "c"]
    assert th.protected_episode_ids(threads) == {"e9"}
