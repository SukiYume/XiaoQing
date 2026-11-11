"""每天的运行计数：只记数、留 7 天、跨日记一行汇总、/life stats 能看。"""

from __future__ import annotations

from types import SimpleNamespace

from plugins.xiaoqing_life import commands, stats
from plugins.xiaoqing_life.stores import default_state


def _outcome(**details: float) -> SimpleNamespace:
    return SimpleNamespace(episodes=3, terms=1, details=details)


def test_sessions_and_details_are_counted_per_day() -> None:
    state = default_state()
    stats.record_session(
        state,
        "2026-11-05",
        "thread_continue",
        "ok",
        "digested",
        _outcome(rounds=3, findings=2, answered=1, liked=2),
    )
    stats.record_session(state, "2026-11-05", "hot_scroll", "ok", "digested", _outcome())
    stats.record_session(
        state, "2026-11-05", "term_lookup", "ok", "looked_up", _outcome(hits=1, misses=2)
    )
    row = state["stats"]["2026-11-05"]
    assert row["session.thread_continue"] == 1 and row["outcome.digested"] == 2
    assert (row["thread_continue.rounds"], row["thread_continue.findings"]) == (3, 2)
    assert (row["term_lookup.hits"], row["term_lookup.misses"]) == (1, 2)
    assert "thread_continue.liked" not in row  # 只记排查用得上的几项
    assert row["episodes"] == 9
    assert stats.summary(state, "2026-11-05").startswith("episodes=9 ")


def test_only_the_last_week_is_kept_and_the_command_shows_it() -> None:
    state = default_state()
    for day in range(1, 11):
        stats.bump(state, f"2026-11-{day:02d}", {"chat.turns": day})
    assert sorted(state["stats"]) == [f"2026-11-{day:02d}" for day in range(4, 11)]
    text = stats.stats_text(state)
    assert text.splitlines()[0] == "2026-11-10：chat.turns=10"
    assert commands.route("stats")[0] == "stats" and commands.route("统计")[0] == "stats"
    assert stats.stats_text(default_state()) == "还没有运行计数。"
