"""作息桥：〔作息〕优先，读不到就退回 quiet_hours；时间一律用配置时区。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from plugins.xiaoqing_life import persona_bridge as pb
from tests.helpers.xiaoqing_life_test_support import (
    at,
    make_cfg,
    make_context,
    write_persona_card,
)

ROUTINE = ("每天 00:30-08:00 睡觉（睡）", "工作日 09:00-11:50 上课（忙）")


def test_routine_slots_drive_availability_and_minutes_until_sleep(tmp_path: Path) -> None:
    context, cfg = make_context(tmp_path), make_cfg()
    write_persona_card(context, tmp_path, ROUTINE)
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


def test_missing_or_broken_routine_falls_back_to_quiet_hours(tmp_path: Path) -> None:
    context, cfg = make_context(tmp_path), make_cfg(quiet_hours="01:00-09:00")
    write_persona_card(context, tmp_path, None)
    assert pb.availability(context, cfg, at(3, 0)).availability == "asleep"
    day = pb.availability(context, cfg, at(23, 0))
    assert (day.availability, day.minutes_until_sleep) == ("free", 120)
    write_persona_card(context, tmp_path, ())
    assert pb.availability(context, cfg, at(3, 0)).availability == "asleep"
    write_persona_card(context, tmp_path, ("这不是作息",))
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


def test_a_persona_card_that_cannot_be_read_falls_back_to_the_name(tmp_path: Path) -> None:
    context = make_context(tmp_path)
    write_persona_card(context, tmp_path, None)

    assert pb.persona_facts(context) == f"你是{pb.bot_name(context)}。"


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
