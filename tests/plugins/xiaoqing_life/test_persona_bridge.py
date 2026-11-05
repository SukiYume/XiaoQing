"""作息桥：〔作息〕优先，读不到就退回 quiet_hours；时间一律用配置时区。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.xiaoqing_life import persona_bridge as pb
from tests.plugins.xiaoqing_life.support import at, make_cfg, make_context

ROUTINE = ("每天 00:30-08:00 睡觉（睡）", "工作日 09:00-11:50 上课（忙）")


def _card(monkeypatch: pytest.MonkeyPatch, routine=ROUTINE) -> None:
    card = SimpleNamespace(routine=tuple(routine), facts=lambda name: f"{name}的人设")
    monkeypatch.setattr(pb, "persona_card", lambda _context: card)


def test_routine_slots_drive_availability_and_minutes_until_sleep(
    monkeypatch, tmp_path: Path
) -> None:
    _card(monkeypatch)
    context, cfg = make_context(tmp_path), make_cfg()
    free = pb.availability(context, cfg, at(14, 0))
    assert (free.availability, free.minutes_until_sleep) == ("free", 630)
    late = pb.availability(context, cfg, at(23, 40))
    assert (late.availability, late.minutes_until_sleep) == ("free", 50)
    asleep = pb.availability(context, cfg, at(2, 0))
    assert (asleep.availability, asleep.activity, asleep.minutes_until_sleep) == (
        "asleep",
        "睡觉",
        0,
    )
    busy = pb.availability(context, cfg, at(10, 0))  # 2026-11-05 是周四
    assert (busy.availability, busy.activity) == ("busy", "上课")
    weekend = pb.availability(context, cfg, at(10, 0, day=7))  # 周六不上课
    assert weekend.availability == "free"


def test_missing_or_broken_routine_falls_back_to_quiet_hours(monkeypatch, tmp_path: Path) -> None:
    context, cfg = make_context(tmp_path), make_cfg(quiet_hours="01:00-09:00")
    monkeypatch.setattr(pb, "persona_card", lambda _context: None)
    assert pb.availability(context, cfg, at(3, 0)).availability == "asleep"
    day = pb.availability(context, cfg, at(23, 0))
    assert (day.availability, day.minutes_until_sleep) == ("free", 120)
    _card(monkeypatch, routine=())
    assert pb.availability(context, cfg, at(3, 0)).availability == "asleep"
    _card(monkeypatch, routine=("这不是作息",))
    assert pb.availability(context, cfg, at(3, 0)).availability == "asleep"
    assert pb.availability(context, cfg, at(12, 0)).availability == "free"


def test_quiet_hours_may_cross_midnight_or_not(tmp_path: Path) -> None:
    for window, probe, asleep in (
        ("23:00-07:00", at(23, 30), True),
        ("23:00-07:00", at(8, 0), False),
        ("13:00-15:00", at(14, 0), True),
        ("13:00-15:00", at(12, 0), False),
    ):
        presence = pb._quiet_presence(make_cfg(quiet_hours=window), probe)
        assert (presence.availability == "asleep") is asleep


def test_card_loader_failures_do_not_raise(monkeypatch, tmp_path: Path) -> None:
    context        = make_context(tmp_path)
    context.config = {"plugins": {"xiaoqing_chat": "not a mapping"}}
    assert isinstance(pb.persona_facts(context), str)  # 配置读不懂也不抛异常
    broken = SimpleNamespace(routine=ROUTINE, facts=lambda _n: 1 / 0)
    monkeypatch.setattr(pb, "persona_card", lambda _c: broken)
    assert pb.persona_facts(context) == ""


def test_real_persona_card_loads_with_default_chat_config(tmp_path: Path) -> None:
    context = make_context(tmp_path)
    facts   = pb.persona_facts(context)
    assert isinstance(facts, str)
    presence = pb.availability(context, make_cfg(), at(14, 0))
    assert presence.availability in {"free", "busy", "asleep"}


def test_now_uses_the_configured_timezone_not_the_host(tmp_path: Path) -> None:
    context = make_context(tmp_path)
    assert (
        pb.now(context).tzinfo is not None
        and getattr(pb.now(context).tzinfo, "key", "") == "Asia/Shanghai"
    )
    context.config = {**context.config, "timezone": "America/Los_Angeles"}
    moment         = pb.now(context)
    assert (
        isinstance(moment, datetime) and getattr(moment.tzinfo, "key", "") == "America/Los_Angeles"
    )
    assert pb.bot_name(context) == "小青"
