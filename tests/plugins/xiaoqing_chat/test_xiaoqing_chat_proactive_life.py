"""主动开口接入网上生活：群聊分享候选与去重、私聊主动联系的每条协调规则、daily_life 探针、梗图字节二次校验。

主动开口从定时任务的处理函数 ``scheduled_proactive`` 进入，走真实的运行时状态、统一记忆、聊天记录和
投递回执；只在边界上放替身：模型（按调用种类回答的剧本模型）、网上生活能力（候选列表）和时间（假时钟）。
"""

from __future__ import annotations

import base64
import hashlib
import io
import random
import time
from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from PIL import Image

import tests.helpers.xiaoqing_chat_test_support as _fixture_support
from plugins.xiaoqing_chat import main as chat_main
from plugins.xiaoqing_chat.config.config import XiaoQingChatConfig
from plugins.xiaoqing_chat.life import daily_life, online, proactive
from plugins.xiaoqing_chat.memory.memory import StoredMessage
from plugins.xiaoqing_chat.message_parts import render_stored_message
from plugins.xiaoqing_chat.runtime_state import ChatRuntime, ChatRuntimeState
from plugins.xiaoqing_chat.utils.wall_clock import local_datetime
from plugins.xiaoqing_memory.model import Note
from tests.helpers.xiaoqing_chat_memory_test_support import MemoryHarness, put_history
from tests.helpers.xiaoqing_chat_test_support import (
    HOUR,
    NOW,
    SCENE_IDS,
    Run,
    bot_message,
    commit,
    group_run,
    proactive_model,
    scene,
    settle_chat_background,
)

mock_context        = _fixture_support.mock_context
clock               = _fixture_support.clock
no_swallowed_errors = _fixture_support.no_swallowed_errors

UID   = 77
SHARE = {
    "episode_id": "e1",
    "line"      : "今天 12:20 刷B站热门，点开了「猫猫的一天」（UP主 甲），讲的是：猫日常，你当时觉得：好可爱",
    "topics"    : ["猫咪"],
}
SHARE2 = {
    "episode_id": "e2",
    "line"      : "昨天 21:00 看知乎热榜，点开了「显卡」，你当时觉得：贵",
    "topics"    : ["装机"],
}
SEND_DRAFT = '{"decision":"send","text":"刷到个猫视频，想到你了","life_ref":1}'
SEND_OK    = '{"decision":"send","reason":"自然"}'
SKIP       = '{"decision":"skip"}'


def _user(text: str, ts: float, uid: int = UID) -> StoredMessage:
    return StoredMessage(role="user", name="阿梨", ts=ts, user_id=uid, content=text)


def _runtime(**overrides: Any) -> ChatRuntime:
    cfg = XiaoQingChatConfig.model_validate(
        {"proactive": {"enabled": True, "private_user_ids": [UID], **overrides}}
    )
    return ChatRuntime(cfg, [])


class FakeLife:
    """记录请求的假网上生活能力：返回候选，但像真的一样排除 exclude_ids。"""

    def __init__(self, *rows: dict[str, Any]) -> None:
        self.rows                           = list(rows)
        self.requests: list[dict[str, Any]] = []

    async def turn_context(self, payload: dict[str, Any]) -> Any:
        self.requests.append(payload)
        kept = [r for r in self.rows if r["episode_id"] not in payload.get("exclude_ids", [])]
        return {"version": 1, "life_lines": "", "share_candidates": kept} if kept else None


class _NoLife:
    async def turn_context(self, payload: dict[str, Any]) -> Any:
        raise RuntimeError("boom")


def _remember(harness: MemoryHarness, user: int, facts: str) -> None:
    """她记得的这位朋友的事（私聊范围里的一条人物笔记）。"""

    if facts:
        now = time.time()
        harness.store.put_notes(
            f"u{user}",
            [
                Note(
                    id          = "nfacts",
                    kind        = "person.fact",
                    scope       = f"u{user}",
                    subject     = f"person:{user}",
                    text        = facts,
                    data        = {"name": "阿梨"},
                    created_at  = now,
                    observed_at = now,
                )
            ],
        )


async def private_run(
    context,
    tmp_path,
    history: Sequence[StoredMessage] = (),
    *,
    reply: Any                    = None,
    checker: Any                  = None,
    reasoning: Any                = None,
    life: Any                     = None,
    facts: str                    = "喜欢猫咪，常看萌宠视频",
    seed: Any                     = None,
    user: int                     = UID,
    memory: MemoryHarness | None  = None,
    config: dict[str, Any] | None = None,
    **proactive_config: Any,
) -> Run:
    """白名单里有 ``user`` 时跑一次定时检查：聊天记录 ``history`` 放在他的私聊里，``seed(state)`` 预置节奏记录。"""

    model   = proactive_model(reply, checker, reasoning)
    harness = memory or MemoryHarness(tmp_path / f"memory{next(SCENE_IDS)}")
    _remember(harness, user, facts)
    life  = FakeLife(SHARE) if life is None else life
    state = scene(
        context,
        tmp_path,
        model,
        life             = life,
        memory           = harness.service(""),
        config           = config,
        proactive_config = {"private_user_ids": [user], **proactive_config},
    )
    if history:
        put_history(state, f"u{user}", list(history))
    if seed is not None:
        seed(state)
    deliveries = await chat_main.scheduled_proactive(context)
    return Run(deliveries, model, state, life, context)


# ---- 私聊 -------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_private_sends_shared_discovery_and_records_under_private_key(
    mock_context, tmp_path
) -> None:
    run = await private_run(
        mock_context,
        tmp_path,
        [_user("今天好累", NOW - 5 * HOUR), bot_message("早点休息", NOW - 5 * HOUR + 30)],
        reply   = SEND_DRAFT,
        checker = SEND_OK,
        life    = FakeLife(SHARE, SHARE2),
    )
    delivery = run.delivery
    assert (
        delivery is not None
        and delivery.target.kind == "private"
        and delivery.target.target_id == UID
    )
    assert delivery.message[0]["data"]["text"] == "刷到个猫视频，想到你了"
    prompt = run.draft_material()
    assert "猫猫的一天" in prompt and "显卡" not in prompt  # 只给对得上兴趣的
    assert run.life.requests[0]["mode"] == "proactive"
    assert len(run.model.calls) == 2  # 起草 + 人设自检
    await commit(run)
    spoken = run.state.memory_store.get(f"u{UID}")[-1]
    assert spoken.role == "assistant" and render_stored_message(spoken) == "刷到个猫视频，想到你了"
    record = run.state.proactive_state.get(f"private:{UID}")
    assert record.count == 1 and record.last_ts > 0 and record.shared_life_ids == ("e1",)
    assert (
        run.state.proactive_state.get(f"u{UID}").count == 0
        and run.state.proactive_state.get(f"g{UID}").count == 0
    )


@pytest.mark.asyncio
async def test_private_rule_daily_cap_and_interval(mock_context, tmp_path) -> None:
    history = [_user("hi", NOW - 40 * HOUR)]
    today   = local_datetime(NOW).strftime("%Y-%m-%d")

    def sent_today(state: ChatRuntimeState) -> None:
        state.proactive_state.record(
            f"private:{UID}", ts=NOW - 100 * HOUR, day=today
        )  # 今天已发过 1 次，但间隔够

    run = await private_run(mock_context, tmp_path, history, seed=sent_today)
    assert run.delivery is None and run.model.calls == []
    ok = await private_run(
        mock_context,
        tmp_path,
        history,
        reply               = SEND_DRAFT,
        checker             = SEND_OK,
        seed                = sent_today,
        private_max_per_day = 2,
    )
    assert ok.delivery is not None

    def sent_19_hours_ago(state: ChatRuntimeState) -> None:
        state.proactive_state.record(f"private:{UID}", ts=NOW - 19 * HOUR, day="2020-01-01")

    run = await private_run(mock_context, tmp_path, history, seed=sent_19_hours_ago)
    assert run.delivery is None and run.model.calls == []  # 20 小时内不再发

    def sent_21_hours_ago(state: ChatRuntimeState) -> None:
        state.proactive_state.record(f"private:{UID}", ts=NOW - 21 * HOUR, day="2020-01-01")

    ok = await private_run(
        mock_context,
        tmp_path,
        [bot_message("上次主动发的", NOW - 21 * HOUR), _user("收到", NOW - 20 * HOUR)],
        reply   = SEND_DRAFT,
        checker = SEND_OK,
        seed    = sent_21_hours_ago,
    )
    assert ok.delivery is not None


@pytest.mark.asyncio
async def test_private_rule_friend_spoke_recently_and_configurable_quiet_window(
    mock_context, tmp_path
) -> None:
    history = [_user("在忙", NOW - 20 * 60)]
    run     = await private_run(mock_context, tmp_path, history)
    assert run.delivery is None and run.model.calls == []
    ok = await private_run(
        mock_context,
        tmp_path,
        history,
        reply                 = SEND_DRAFT,
        checker               = SEND_OK,
        private_quiet_minutes = 10,
    )
    assert ok.delivery is not None
    older = await private_run(
        mock_context, tmp_path, [_user("早", NOW - 45 * 60)], reply=SEND_DRAFT, checker=SEND_OK
    )
    assert older.delivery is not None


@pytest.mark.asyncio
async def test_private_rule_unanswered_last_proactive_message(mock_context, tmp_path) -> None:
    def sent_30_hours_ago(state: ChatRuntimeState) -> None:
        state.proactive_state.record(f"private:{UID}", ts=NOW - 30 * HOUR, day="2020-01-01")

    run = await private_run(
        mock_context,
        tmp_path,
        [_user("更早的话", NOW - 40 * HOUR), bot_message("我上次发的", NOW - 30 * HOUR)],
        seed=sent_30_hours_ago,
    )
    assert run.delivery is None and run.model.calls == []
    ok = await private_run(
        mock_context,
        tmp_path,
        [bot_message("我上次发的", NOW - 30 * HOUR), _user("回了", NOW - 29 * HOUR)],
        reply   = SEND_DRAFT,
        checker = SEND_OK,
        seed    = sent_30_hours_ago,
    )
    assert ok.delivery is not None


@pytest.mark.asyncio
async def test_private_same_episode_is_shared_with_the_same_person_only_once(
    mock_context, tmp_path
) -> None:
    def shared_e1_before(state: ChatRuntimeState) -> None:
        state.proactive_state.record(
            f"private:{UID}", ts=NOW - 100 * HOUR, day="2020-01-01", shared_life_ids=["e1"]
        )

    out = await private_run(
        mock_context, tmp_path, [_user("好的", NOW - 50 * HOUR)], seed=shared_e1_before
    )
    assert (
        out.delivery is None
        and out.model.calls == []
        and out.life.requests[0]["exclude_ids"] == ["e1"]
    )
    both = FakeLife(SHARE, {**SHARE2, "topics": ["猫咪"]})
    ok   = await private_run(
        mock_context,
        tmp_path,
        [_user("好的", NOW - 50 * HOUR)],
        reply   = '{"decision":"send","text":"另一个","life_ref":1}',
        checker = '{"decision":"send","reason":"x"}',
        seed    = shared_e1_before,
        life    = both,
    )
    assert ok.delivery is not None and both.requests[0]["exclude_ids"] == ["e1"]
    # 别的朋友不受影响
    other = await private_run(
        mock_context,
        tmp_path,
        reply   = SEND_DRAFT,
        checker = SEND_OK,
        seed    = shared_e1_before,
        user    = 88,
    )
    assert other.delivery is not None


@pytest.mark.asyncio
async def test_private_needs_real_material_matching_this_friends_interests(
    mock_context, tmp_path
) -> None:
    run = await private_run(mock_context, tmp_path, facts="喜欢打篮球")  # 话题对不上
    assert run.delivery is None and run.model.calls == []
    run = await private_run(mock_context, tmp_path, facts="")
    assert run.delivery is None and run.model.calls == []
    run = await private_run(mock_context, tmp_path, life=_NoLife())
    assert run.delivery is None and run.model.calls == []
    run = await private_run(mock_context, tmp_path, config={"life": {"enable_life_share": False}})
    assert run.delivery is None and run.model.calls == []


@pytest.mark.asyncio
async def test_private_missing_a_friend_after_days_of_silence_is_a_material(
    mock_context, tmp_path
) -> None:
    history = [
        _user("周末去爬山", NOW - 4 * 86400 - 60),
        bot_message("好耶，注意防晒", NOW - 4 * 86400),
    ]
    out = await private_run(
        mock_context,
        tmp_path,
        history,
        reply   = '{"decision":"send","text":"好几天没见你冒泡了，爬山晒黑没"}',
        checker = SEND_OK,
        life    = _NoLife(),
    )
    assert out.delivery is not None and out.delivery.message[0]["data"]["text"].startswith("好几天")
    assert "4 天没聊了" in out.draft_material()

    # 才隔一天、从没聊过、或者关掉了这一项，都不因为想念去找人。
    cases: list[tuple[list[StoredMessage], dict[str, Any]]] = [
        ([_user("周末去爬山", NOW - 86400)], {}),
        ([], {}),
        (history, {"private_miss_after_days": 0}),
    ]
    for case_history, overrides in cases:
        run = await private_run(mock_context, tmp_path, case_history, life=_NoLife(), **overrides)
        assert run.delivery is None and run.model.calls == []


@pytest.mark.asyncio
async def test_private_due_open_loop_is_a_material_and_is_marked_asked_on_commit(
    mock_context, tmp_path
) -> None:
    harness = MemoryHarness(tmp_path / "memory")
    now     = time.time()
    harness.store.put_notes(
        f"u{UID}",
        [
            Note(
                id="nloop",
                kind="person.loop",
                scope=f"u{UID}",
                subject=f"person:{UID}",
                text="数电期末考试",
                created_at=now - 86400,
                observed_at=now - 86400,
                expires_at=now + 7 * 86400,
                data={"user_id": UID, "user_name": "阿梨", "due_at": now - 3600, "loop": "open"},
            )
        ],
    )
    run = await private_run(
        mock_context,
        tmp_path,
        reply     = '{"decision":"send","text":"考得怎么样呀"}',
        checker   = '{"decision":"send","reason":"x"}',
        reasoning = "{}",
        life      = _NoLife(),
        facts     = "",
        memory    = harness,
        config    = {"life": {"enable_open_loops": True}},
    )
    assert run.delivery is not None and "数电期末考试" in run.draft_material()
    await commit(run)
    assert harness.store.note(f"u{UID}", "nloop").data["loop"] == "asked"
    assert run.state.proactive_state.get(f"private:{UID}").shared_life_ids == ()


@pytest.mark.asyncio
async def test_private_judge_or_local_check_can_still_stop_the_message(
    mock_context, tmp_path
) -> None:
    drop = '{"decision":"drop","reason":"突兀"}'
    run  = await private_run(
        mock_context, tmp_path, reply=[SEND_DRAFT, SEND_DRAFT], checker=[drop, drop]
    )
    assert run.delivery is None and len(run.model.calls) == 4  # 拦下后重写一次，第二稿还不过就不发
    run = await private_run(
        mock_context,
        tmp_path,
        [bot_message("刷到个猫视频，想到你了", NOW - 40 * HOUR)],
        reply   = SEND_DRAFT,
        checker = SEND_OK,
    )
    assert (
        run.delivery is None and len(run.model.calls) == 1
    )  # 和自己说过的一样：本地检查拦下，不必再自检


# ---- 定时任务入口：白名单、作息、活跃时段 -------------------------------------------------------


@pytest.mark.asyncio
async def test_private_path_never_runs_with_an_empty_allowlist_or_while_asleep_busy_or_outside_hours(
    mock_context, tmp_path, clock
) -> None:
    async def sent(**proactive_config: Any) -> bool:
        run = await private_run(
            mock_context, tmp_path, reply=SEND_DRAFT, checker=SEND_OK, **proactive_config
        )
        return run.delivery is not None

    assert not await sent(private_user_ids=[])
    assert await sent()
    clock.now = NOW - 4 * HOUR  # 周五 12:00：食堂吃午饭，作息里是明写的“空闲”
    assert await sent()
    clock.now = NOW + 17.5 * HOUR  # 周六 09:30：还在睡觉
    assert not await sent()
    clock.now = NOW + 65.5 * HOUR  # 周一 09:30：上课，忙
    assert not await sent()
    clock.now = NOW + 7.5 * HOUR  # 周五 23:30：过了活跃时段，作息里没有安排
    assert not await sent()
    clock.now = NOW
    assert not await sent(enabled=False)


@pytest.mark.asyncio
@pytest.mark.parametrize(("roll", "speaks"), [(0.2, False), (0.8, True)])
async def test_group_proactive_skips_about_half_the_time_while_busy(
    mock_context, tmp_path, clock, monkeypatch, roll, speaks
) -> None:
    clock.now = NOW + 65.5 * HOUR  # 周一 09:30：上课，忙
    monkeypatch.setattr(random, "random", lambda: roll)

    run = await group_run(
        mock_context,
        tmp_path,
        [_user("晚上吃啥", clock.now - 3 * HOUR)],
        reply   = '{"decision":"send","text":"食堂二楼走起"}',
        checker = SEND_OK,
    )

    # 在忙时她只有一半的概率抽空看群：没抽中就不起草，抽中了照常开口。
    assert (run.delivery is not None) is speaks
    assert bool(run.model.calls) is speaks
    if speaks:
        await commit(run)
    else:
        await settle_chat_background(mock_context, run.model)


def test_private_config_defaults_and_validation() -> None:
    cfg = XiaoQingChatConfig().proactive
    assert cfg.private_user_ids == [] and cfg.private_max_per_day == 1
    assert cfg.private_min_interval_hours == 20 and cfg.private_quiet_minutes == 30
    assert cfg.private_miss_after_days == 3
    assert XiaoQingChatConfig.model_validate(
        {"proactive": {"private_user_ids": [5, 5, 6]}}
    ).proactive.private_user_ids == [5, 6]
    with pytest.raises(ValueError):
        XiaoQingChatConfig.model_validate({"proactive": {"private_user_ids": [0]}})


# ---- 群聊 -------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_group_proactive_offers_candidates_and_records_the_shared_episode(
    mock_context, tmp_path, clock
) -> None:
    life = FakeLife(SHARE, SHARE2)
    run  = await group_run(
        mock_context,
        tmp_path,
        [_user("晚上吃啥", NOW - 3 * HOUR)],
        reply   = ['{"decision":"send","text":"今天刷到个猫视频","life_ref":1}', SKIP],
        checker = '{"decision":"send","reason":"x"}',
        life    = life,
    )
    assert run.delivery is not None
    prompt = run.draft_material()
    assert (
        "1. 今天 12:20 刷B站热门" in prompt and "2. 昨天 21:00" in prompt and "life_ref" in prompt
    )
    assert life.requests[0]["exclude_ids"] == []
    await commit(run)
    record = run.state.proactive_state.get("g42")
    assert record.shared_life_ids == ("e1",) and record.count == 1

    # 再次开口时把已分享的当作 exclude_ids：隔了几个小时，群里又有人说了话。
    clock.advance(6 * HOUR)
    put_history(run.state, "g42", [_user("晚饭后再聊聊", NOW + 3 * HOUR)])
    assert await chat_main.scheduled_proactive(mock_context) == []
    assert life.requests[-1]["exclude_ids"] == ["e1"]
    assert "1. 昨天 21:00" in run.model.calls_of("reply")[1].messages[1]["content"]


def test_shared_ids_are_kept_to_twenty_and_survive_reload(tmp_path) -> None:
    state = ChatRuntimeState()
    state.proactive_state.bind(tmp_path)
    for n in range(25):
        state.proactive_state.record("g1", ts=NOW + n, day="d", shared_life_ids=[f"e{n}"])
    again = ChatRuntimeState()
    again.proactive_state.bind(tmp_path)
    ids = again.proactive_state.get("g1").shared_life_ids
    assert len(ids) == 20 and ids[-1] == "e24" and ids[0] == "e5"


@pytest.mark.asyncio
async def test_group_path_offers_no_shared_episode_without_the_capability_or_with_sharing_off(
    mock_context, tmp_path
) -> None:
    history = [_user("晚上吃啥", NOW - 3 * HOUR)]
    for life, config in (
        (None, None),
        (FakeLife(SHARE), {"life": {"enable_life_share": False}}),
    ):
        run = await group_run(mock_context, tmp_path, history, reply=SKIP, life=life, config=config)
        assert run.deliveries == []
        assert "life_ref" not in run.draft_material()
    assert (
        proactive.parse_life_ref('{"life_ref": 3}', [SHARE]) == []
        and proactive.parse_life_ref("坏", [SHARE]) == []
    )
    assert proactive.parse_life_ref('{"life_ref": true}', [SHARE]) == []


# ---- daily_life 探针 --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_online_life_follows_the_capability_and_switch() -> None:
    runtime = _runtime()
    on      = SimpleNamespace(
        capabilities=SimpleNamespace(
            life_context=SimpleNamespace(
                turn_context=AsyncMock(return_value={"version": 1, "online_life": True})
            )
        )
    )
    assert await online.probe_online_life(on, runtime) is True
    on.capabilities.life_context.turn_context.assert_awaited_once_with(
        {"version": 1, "mode": "probe"}
    )
    off_result = SimpleNamespace(
        capabilities=SimpleNamespace(
            life_context=SimpleNamespace(turn_context=AsyncMock(return_value=None))
        )
    )
    assert await online.probe_online_life(off_result, runtime) is False
    assert (
        await online.probe_online_life(
            SimpleNamespace(capabilities=SimpleNamespace(life_context=None)), runtime
        )
        is False
    )
    boom = SimpleNamespace(
        capabilities=SimpleNamespace(
            life_context=SimpleNamespace(turn_context=AsyncMock(side_effect=RuntimeError("x")))
        )
    )
    assert await online.probe_online_life(boom, runtime) is False
    runtime.cfg.life.enable_online_life = False
    assert await online.probe_online_life(on, runtime) is False


def test_daily_life_rule_is_added_only_when_online_life_is_on() -> None:
    from datetime import datetime

    moment = datetime(2026, 11, 5, 8, 0)
    plain = daily_life.build_messages(persona="p", routine=[], moment=moment)[0]["content"]
    online_on = daily_life.build_messages(persona="p", routine=[], moment=moment, online_life=True)[
        0
    ]["content"]
    assert plain == f"p\n\n{daily_life._SYSTEM}"  # 关闭时不加这条规则
    assert online_on.startswith(plain) and online_on != plain


@pytest.mark.asyncio
async def test_scheduled_daily_life_passes_the_probe_result(mock_context, tmp_path) -> None:
    day = '{"events":[{"at":"08:10","text":"早八迟到"},{"at":"12:00","text":"食堂打饭"}]}'
    systems: dict[bool, str] = {}
    for probe in (True, False):
        model = proactive_model(reasoning=day)
        life = SimpleNamespace(
            turn_context=AsyncMock(return_value={"version": 1, "online_life": probe})
        )
        state = scene(
            mock_context, tmp_path, model, life=life, config={"life": {"enable_daily_life": True}}
        )
        await chat_main.scheduled_daily_life(mock_context)
        (call,) = model.calls_of("reasoning")
        systems[probe] = call.messages[0]["content"]
        assert state.daily_life.get(daily_life.day_key(local_datetime(NOW))) is not None
        await settle_chat_background(mock_context, model)
    # 探针说网上生活在线时，生成今天的小事多带一条规则。
    assert systems[True].startswith(systems[False]) and systems[True] != systems[False]


# ---- 梗图字节二次校验 -------------------------------------------------------------------------


def _png(size: tuple[int, int] = (16, 16)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (1, 2, 3)).save(buffer, format="PNG")
    return buffer.getvalue()


def _raw(image: bytes, mime: str = "image/png") -> dict[str, Any]:
    return {
        "sha256"      : hashlib.sha256(image).hexdigest(),
        "label"       : "名字",
        "mime"        : mime,
        "data_b64"    : base64.b64encode(image).decode(),
        "emotion_tags": [],
    }


def test_decode_meme_revalidates_bytes_with_the_emoji_library_budget() -> None:
    runtime = _runtime()
    assert online._decode_meme(_raw(_png()), runtime) is not None
    fake = b"GIF89a" + b"\x00" * 40  # 哈希对得上但不是合法图片
    assert online._decode_meme(_raw(fake, "image/gif"), runtime) is None
    mismatched_mime = _raw(_png(), "image/gif")
    assert online._decode_meme(mismatched_mime, runtime) is None
    runtime.cfg.media.max_image_pixels = 100
    assert online._decode_meme(_raw(_png((32, 32))), runtime) is None  # 超过表情库的像素预算
    runtime.cfg.media.max_image_pixels  = 20_000_000
    runtime.cfg.media.max_analyze_bytes = 50
    assert online._decode_meme(_raw(_png()), runtime) is None  # 超过字节预算


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_kind", ["group", "private"])
async def test_proactive_does_not_repeat_a_shared_episode_from_the_self_page(
    mock_context, tmp_path, chat_kind
) -> None:
    """分享候选里已经有这条经历的细节，自我页里只引用它的行不再放；另有依据的行照常放。"""

    from plugins.xiaoqing_memory.model import GLOBAL, Page, PageLine
    from plugins.xiaoqing_memory.scopes import SELF_SUBJECT

    harness = MemoryHarness(tmp_path / "memory")
    now     = time.time()
    harness.store.put_notes(
        GLOBAL,
        [
            Note(
                id          = "n-cat",
                kind        = "episode",
                scope       = GLOBAL,
                subject     = SELF_SUBJECT,
                text        = "在B站看了「猫猫的一天」：好可爱",
                origin      = "web",
                sources     = ({"type": "external", "key": "e1", "origin": "web"},),
                created_at  = now,
                observed_at = now,
                expires_at  = now + 86400,
            ),
            Note(
                id          = "n-exam",
                kind        = "self.state",
                scope       = GLOBAL,
                subject     = SELF_SUBJECT,
                text        = "这周在准备期中考试",
                origin      = "self",
                sources     = ({"type": "event", "scope": "g42", "seq": 1, "origin": "self"},),
                created_at  = now,
                observed_at = now,
                expires_at  = now + 86400,
            ),
        ],
    )
    harness.store.put_page(
        Page(
            subject  = SELF_SUBJECT,
            scope    = GLOBAL,
            sections = {
                "doing"    : (PageLine(text="这周在准备期中考试", note_ids=("n-exam",)),),
                "interests": (PageLine(text="最近爱刷猫猫视频", note_ids=("n-cat",)),),
            },
            updated_at=now,
        )
    )
    config = {"life": {"enable_self_timeline": True}}
    if chat_kind == "group":
        run = await group_run(
            mock_context,
            tmp_path,
            [_user("晚上吃啥", NOW - 3 * HOUR)],
            reply   = SKIP,
            life    = FakeLife(SHARE),
            harness = harness,
            config  = config,
        )
    else:
        run = await private_run(
            mock_context,
            tmp_path,
            reply  = SKIP,
            life   = FakeLife(SHARE),
            memory = harness,
            config = config,
        )
    prompt = run.draft_material()
    assert "猫猫的一天" in prompt  # 分享候选的细节在
    assert "最近爱刷猫猫视频" not in prompt
    # 自我页和“最近说过的关于自己的事”各放一次这条依据充分的行。
    assert prompt.count("这周在准备期中考试") == 2
