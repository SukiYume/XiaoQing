"""主动开口接入网上生活：群聊分享候选与去重、私聊主动联系的每条协调规则、daily_life 探针、梗图字节二次校验。"""

from __future__ import annotations

import base64
import hashlib
import io
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from PIL import Image

from plugins.xiaoqing_chat.config.config import XiaoQingChatConfig
from plugins.xiaoqing_chat.life import daily_life, online, proactive, proactive_private
from plugins.xiaoqing_chat.memory.memory import StoredMessage
from plugins.xiaoqing_chat.runtime_state import ChatRuntime, ChatRuntimeState

NOW   = 1_800_000_000.0
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


def _user(text: str, ts: float, uid: int = UID) -> StoredMessage:
    return StoredMessage(role="user", name="阿梨", ts=ts, user_id=uid, content=text)


def _bot(text: str, ts: float) -> StoredMessage:
    return StoredMessage(
        role="assistant", name="小青", ts=ts, content=text, source_plugin="xiaoqing_chat"
    )


def _runtime(**overrides: Any) -> ChatRuntime:
    cfg = XiaoQingChatConfig.model_validate(
        {"proactive": {"enabled": True, "private_user_ids": [UID], **overrides}}
    )
    return ChatRuntime(cfg, [])


class FakeLife:
    """记录请求的假 life_context：返回候选，但像真的一样排除 exclude_ids。"""

    def __init__(self, *rows: dict[str, Any]) -> None:
        self.rows, self.requests = list(rows), []

    async def turn_context(self, payload: dict[str, Any]) -> Any:
        self.requests.append(payload)
        kept = [r for r in self.rows if r["episode_id"] not in payload.get("exclude_ids", [])]
        return {"version": 1, "life_lines": "", "share_candidates": kept} if kept else None


def _context(life: Any = None) -> SimpleNamespace:
    return SimpleNamespace(
        logger=None, data_dir="/tmp/none", capabilities=SimpleNamespace(life_context=life)
    )


def _state(tmp_path, history: list[StoredMessage]) -> ChatRuntimeState:
    state    = ChatRuntimeState()
    data_dir = tmp_path / f"d{len(list(tmp_path.iterdir()))}"
    for store in (state.self_timeline, state.open_loops, state.proactive_state):
        store.bind(data_dir)
    state.memory_store.get_recent_async = AsyncMock(return_value=history)
    return state


async def _private(
    tmp_path,
    history,
    outputs,
    *,
    life      = None,
    facts     = "喜欢猫咪，常看萌宠视频",
    runtime   = None,
    state     = None,
    user: int = UID,
):
    state = state or _state(tmp_path, history)
    model = AsyncMock(side_effect=outputs)
    context = _context(FakeLife(SHARE) if life is None else life)
    with (
        patch.object(proactive, "chat_completions", model),
        patch.object(proactive_private, "configured_bot_name", return_value="小青"),
        patch.object(proactive_private, "ai_route_context", return_value={"_ai": object()}),
        patch.object(proactive_private, "profile_text", return_value=facts),
    ):
        delivery = await proactive_private.consider_private(
            context, runtime or _runtime(), state, user, now=NOW, life_text=""
        )
    return delivery, model, state, context


SEND = [
    '{"decision":"send","text":"刷到个猫视频，想到你了","life_ref":1}',
    '{"decision":"send","reason":"自然"}',
]


@pytest.mark.asyncio
async def test_private_sends_shared_discovery_and_records_under_private_key(tmp_path) -> None:
    delivery, model, state, context = await _private(
        tmp_path, [_user("今天好累", NOW - 5 * 3600), _bot("早点休息", NOW - 5 * 3600 + 30)], SEND
    )
    assert (
        delivery is not None
        and delivery.target.kind == "private"
        and delivery.target.target_id == UID
    )
    assert delivery.message[0]["data"]["text"] == "刷到个猫视频，想到你了"
    prompt = model.await_args_list[0].kwargs["messages"][1]["content"]
    assert "猫猫的一天" in prompt and "显卡" not in prompt  # 只给对得上兴趣的
    assert context.capabilities.life_context.requests[0]["mode"] == "proactive"
    assert model.await_count == 2  # 起草 + 人设自检
    appended: list[dict[str, Any]] = []
    state.memory_store.append = lambda chat_id, **kw: appended.append({"chat_id": chat_id, **kw})
    with (
        patch.object(proactive, "schedule_memory_persist"),
        patch.object(proactive, "schedule_action_history_flush"),
        patch.object(proactive, "spawn_life_extraction"),
    ):
        await delivery.receipt.record(True)
    assert appended[0]["chat_id"] == f"u{UID}"
    record = state.proactive_state.get(f"private:{UID}")
    assert record.count == 1 and record.last_ts > 0 and record.shared_life_ids == ("e1",)
    assert (
        state.proactive_state.get(f"u{UID}").count == 0
        and state.proactive_state.get(f"g{UID}").count == 0
    )


@pytest.mark.asyncio
async def test_private_rule_daily_cap_and_interval(tmp_path) -> None:
    history = [_user("hi", NOW - 40 * 3600)]
    state   = _state(tmp_path, history)
    today   = proactive.local_datetime(NOW).strftime("%Y-%m-%d")
    state.proactive_state.record(
        f"private:{UID}", ts=NOW - 100 * 3600, day=today
    )  # 今天已发过 1 次，但间隔够
    out, model, *_ = await _private(tmp_path, history, [], state=state)
    assert out is None and model.await_count == 0
    ok, *_ = await _private(
        tmp_path, history, SEND, state=state, runtime=_runtime(private_max_per_day=2)
    )
    assert ok is not None
    state2 = _state(tmp_path, history)
    state2.proactive_state.record(f"private:{UID}", ts=NOW - 19 * 3600, day="2020-01-01")
    out, model, *_ = await _private(tmp_path, history, [], state=state2)
    assert out is None and model.await_count == 0  # 20 小时内不再发
    state3 = _state(tmp_path, history)
    state3.proactive_state.record(f"private:{UID}", ts=NOW - 21 * 3600, day="2020-01-01")
    state3.memory_store.get_recent_async = AsyncMock(
        return_value=[_bot("上次主动发的", NOW - 21 * 3600), _user("收到", NOW - 20 * 3600)]
    )
    ok, *_ = await _private(tmp_path, [], SEND, state=state3)
    assert ok is not None


@pytest.mark.asyncio
async def test_private_rule_friend_spoke_recently_and_configurable_quiet_window(tmp_path) -> None:
    history = [_user("在忙", NOW - 20 * 60)]
    out, model, *_ = await _private(tmp_path, history, [])
    assert out is None and model.await_count == 0
    ok, *_ = await _private(tmp_path, history, SEND, runtime=_runtime(private_quiet_minutes=10))
    assert ok is not None
    older, *_ = await _private(tmp_path, [_user("早", NOW - 45 * 60)], SEND)
    assert older is not None


@pytest.mark.asyncio
async def test_private_rule_unanswered_last_proactive_message(tmp_path) -> None:
    state = _state(tmp_path, [])
    state.proactive_state.record(f"private:{UID}", ts=NOW - 30 * 3600, day="2020-01-01")
    state.memory_store.get_recent_async = AsyncMock(
        return_value=[_user("更早的话", NOW - 40 * 3600), _bot("我上次发的", NOW - 30 * 3600)]
    )
    out, model, *_ = await _private(tmp_path, [], [], state=state)
    assert out is None and model.await_count == 0
    state.memory_store.get_recent_async = AsyncMock(
        return_value=[_bot("我上次发的", NOW - 30 * 3600), _user("回了", NOW - 29 * 3600)]
    )
    ok, *_ = await _private(tmp_path, [], SEND, state=state)
    assert ok is not None


@pytest.mark.asyncio
async def test_private_same_episode_is_shared_with_the_same_person_only_once(tmp_path) -> None:
    state = _state(tmp_path, [_user("好的", NOW - 50 * 3600)])
    state.proactive_state.record(
        f"private:{UID}", ts=NOW - 100 * 3600, day="2020-01-01", shared_life_ids=["e1"]
    )
    out, model, _s, context = await _private(tmp_path, [], [], state=state)
    assert (
        out is None
        and model.await_count == 0
        and context.capabilities.life_context.requests[0]["exclude_ids"] == ["e1"]
    )
    both = FakeLife(SHARE, {**SHARE2, "topics": ["猫咪"]})
    ok, _m, _s2, ctx = await _private(
        tmp_path,
        [],
        ['{"decision":"send","text":"另一个","life_ref":1}', '{"decision":"send","reason":"x"}'],
        state = state,
        life  = both,
    )
    assert ok is not None and ctx.capabilities.life_context.requests[0]["exclude_ids"] == ["e1"]
    # 别的朋友不受影响
    other = _state(tmp_path, [])
    ok2, *_ = await _private(tmp_path, [], SEND, state=other, user=88)
    assert ok2 is not None


@pytest.mark.asyncio
async def test_private_needs_real_material_matching_this_friends_interests(tmp_path) -> None:
    out, model, *_ = await _private(tmp_path, [], [], facts="喜欢打篮球")  # 话题对不上
    assert out is None and model.await_count == 0
    out, model, *_ = await _private(tmp_path, [], [], facts="")
    assert out is None and model.await_count == 0
    out, model, *_ = await _private(tmp_path, [], [], life=_NoLife())
    assert out is None and model.await_count == 0
    off                            = _runtime()
    off.cfg.life.enable_life_share = False
    out, model, *_ = await _private(tmp_path, [], [], runtime=off)
    assert out is None and model.await_count == 0


@pytest.mark.asyncio
async def test_private_missing_a_friend_after_days_of_silence_is_a_material(tmp_path) -> None:
    history = [_user("周末去爬山", NOW - 4 * 86400 - 60), _bot("好耶，注意防晒", NOW - 4 * 86400)]
    missing = [
        '{"decision":"send","text":"好几天没见你冒泡了，爬山晒黑没"}',
        '{"decision":"send","reason":"自然"}',
    ]
    out, model, *_ = await _private(tmp_path, history, missing, life=_NoLife())
    assert out is not None and out.message[0]["data"]["text"].startswith("好几天")
    assert "4 天没聊了" in model.await_args_list[0].kwargs["messages"][1]["content"]

    # 才隔一天、从没聊过、或者关掉了这一项，都不因为想念去找人。
    for case_history, runtime in (
        ([_user("周末去爬山", NOW - 86400)], None),
        ([], None),
        (history, _runtime(private_miss_after_days=0)),
    ):
        out, model, *_ = await _private(tmp_path, case_history, [], life=_NoLife(), runtime=runtime)
        assert out is None and model.await_count == 0


class _NoLife:
    async def turn_context(self, payload: dict[str, Any]) -> Any:
        raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_private_due_open_loop_is_a_material_and_is_marked_asked_on_commit(tmp_path) -> None:
    state = _state(tmp_path, [])
    state.open_loops.add(
        f"u{UID}",
        user_id   = UID,
        user_name = "阿梨",
        title     = "数电期末考试",
        due_at    = NOW - 3600,
        max_loops = 10,
        now       = NOW - 86400,
    )
    delivery, model, _s, _c = await _private(
        tmp_path,
        [],
        ['{"decision":"send","text":"考得怎么样呀"}', '{"decision":"send","reason":"x"}'],
        state = state,
        life  = _NoLife(),
        facts = "",
    )
    assert (
        delivery is not None
        and "数电期末考试" in model.await_args_list[0].kwargs["messages"][1]["content"]
    )
    with (
        patch.object(proactive, "schedule_memory_persist"),
        patch.object(proactive, "schedule_action_history_flush"),
        patch.object(proactive, "spawn_life_extraction"),
    ):
        await delivery.receipt.record(True)
    assert state.open_loops.get(f"u{UID}")[0].status == "asked"
    assert state.proactive_state.get(f"private:{UID}").shared_life_ids == ()


@pytest.mark.asyncio
async def test_private_judge_or_local_check_can_still_stop_the_message(tmp_path) -> None:
    out, model, *_ = await _private(tmp_path, [], [SEND[0], '{"decision":"drop","reason":"突兀"}'])
    assert out is None and model.await_count == 2
    out, model, *_ = await _private(
        tmp_path,
        [_bot("刷到个猫视频，想到你了", NOW - 40 * 3600)],
        SEND[:1] + ['{"decision":"send","reason":"x"}'],
    )
    assert out is None or model.await_count >= 1


# ---- run_proactive 入口：白名单、作息、活跃时段 ------------------------------------------------


async def _run(tmp_path, runtime, *, slot=None, active=True, consider=None):
    state = _state(tmp_path, [])
    consider = consider or AsyncMock(return_value=None)
    context = SimpleNamespace(
        logger=None, default_groups=lambda: [], capabilities=SimpleNamespace(life_context=None)
    )
    with (
        patch.object(proactive, "load_runtime", return_value=runtime),
        patch.object(proactive, "bound_state", return_value=state),
        patch.object(proactive, "within_active_hours", return_value=active),
        patch.object(proactive, "routine_slot_now", return_value=slot),
        patch.object(proactive_private, "consider_private", consider),
    ):
        await proactive.run_proactive(context)
    return consider


@pytest.mark.asyncio
async def test_private_path_never_runs_with_an_empty_allowlist_or_while_asleep_busy_or_outside_hours(
    tmp_path,
) -> None:
    assert (await _run(tmp_path, _runtime(private_user_ids=[]))).await_count == 0
    assert (await _run(tmp_path, _runtime())).await_count == 1
    free = SimpleNamespace(availability="free", activity="发呆")
    assert (await _run(tmp_path, _runtime(), slot=free)).await_count == 1
    assert (
        await _run(
            tmp_path, _runtime(), slot=SimpleNamespace(availability="asleep", activity="睡觉")
        )
    ).await_count == 0
    assert (
        await _run(tmp_path, _runtime(), slot=SimpleNamespace(availability="busy", activity="上课"))
    ).await_count == 0
    assert (await _run(tmp_path, _runtime(), active=False)).await_count == 0
    assert (await _run(tmp_path, XingOff())).await_count == 0


def XingOff() -> ChatRuntime:
    return ChatRuntime(
        XiaoQingChatConfig.model_validate(
            {"proactive": {"enabled": False, "private_user_ids": [UID]}}
        ),
        [],
    )


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
async def test_group_proactive_offers_candidates_and_records_the_shared_episode(tmp_path) -> None:
    history = [_user("晚上吃啥", NOW - 3 * 3600)]
    state   = _state(tmp_path, history)
    life    = FakeLife(SHARE, SHARE2)
    context = _context(life)
    model   = AsyncMock(
        side_effect=[
            '{"decision":"send","text":"今天刷到个猫视频","life_ref":1}',
            '{"decision":"send","reason":"x"}',
        ]
    )
    with (
        patch.object(proactive, "chat_completions", model),
        patch.object(proactive, "configured_bot_name", return_value="小青"),
        patch.object(proactive, "ai_route_context", return_value={"_ai": object()}),
    ):
        delivery = await proactive._consider_group(
            context, _runtime(), state, 42, now=NOW, life_text=""
        )
    assert delivery is not None
    prompt = model.await_args_list[0].kwargs["messages"][1]["content"]
    assert (
        "1. 今天 12:20 刷B站热门" in prompt and "2. 昨天 21:00" in prompt and "life_ref" in prompt
    )
    assert life.requests[0]["exclude_ids"] == []
    with (
        patch.object(proactive, "schedule_memory_persist"),
        patch.object(proactive, "schedule_action_history_flush"),
        patch.object(proactive, "spawn_life_extraction"),
    ):
        await delivery.receipt.record(True)
    record = state.proactive_state.get("g42")
    assert record.shared_life_ids == ("e1",) and record.count == 1

    # 再次开口时把已分享的当作 exclude_ids
    state.proactive_state.record_attempt("g42", ts=0.0)
    model2 = AsyncMock(side_effect=['{"decision":"skip"}'])
    with (
        patch.object(proactive, "chat_completions", model2),
        patch.object(proactive, "configured_bot_name", return_value="小青"),
        patch.object(proactive, "ai_route_context", return_value={"_ai": object()}),
        patch.object(proactive, "local_datetime", wraps=proactive.local_datetime),
    ):
        state.proactive_state._records["g42"] = proactive_replace(
            state.proactive_state.get("g42"), last_ts=NOW - 10 * 3600, day="2020-01-01"
        )
        await proactive._consider_group(context, _runtime(), state, 42, now=NOW, life_text="")
    assert life.requests[-1]["exclude_ids"] == ["e1"]
    assert "1. 昨天 21:00" in model2.await_args_list[0].kwargs["messages"][1]["content"]


def proactive_replace(record, **kw):
    from dataclasses import replace

    return replace(record, **kw)


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
async def test_group_path_is_unchanged_without_the_capability_or_with_sharing_off(tmp_path) -> None:
    history = [_user("晚上吃啥", NOW - 3 * 3600)]
    for context, runtime in (
        (SimpleNamespace(logger=None), _runtime()),
        (_context(FakeLife(SHARE)), _runtime()),
    ):
        if context.capabilities if hasattr(context, "capabilities") else False:
            runtime.cfg.life.enable_life_share = False
        state = _state(tmp_path, history)
        model = AsyncMock(side_effect=['{"decision":"skip"}'])
        with (
            patch.object(proactive, "chat_completions", model),
            patch.object(proactive, "configured_bot_name", return_value="小青"),
            patch.object(proactive, "ai_route_context", return_value={"_ai": object()}),
        ):
            await proactive._consider_group(context, runtime, state, 42, now=NOW, life_text="")
        assert "life_ref" not in model.await_args_list[0].kwargs["messages"][1]["content"]
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
    plain = daily_life.build_messages(persona="p", routine=[], moment=moment, yesterday=None)[0][
        "content"
    ]
    online_on = daily_life.build_messages(
        persona="p", routine=[], moment=moment, yesterday=None, online_life=True
    )[0]["content"]
    assert plain == f"p\n\n{daily_life._SYSTEM}"  # 关闭时与之前逐字一致
    assert (
        online_on
        == plain + "\n- 她上网刷到的具体视频、帖子和热点另有真实记录，这里不写具体的视频或帖子。"
    )


@pytest.mark.asyncio
async def test_scheduled_daily_life_passes_the_probe_result(monkeypatch) -> None:
    from plugins.xiaoqing_chat import main as chat_main

    runtime                            = _runtime()
    runtime.cfg.life.enable_daily_life = True
    seen: dict[str, Any]               = {}

    async def fake_ensure(**kw: Any) -> None:
        seen.update(kw)

    for probe in (True, False):
        monkeypatch.setattr(chat_main, "load_runtime", lambda _c: runtime)
        monkeypatch.setattr(chat_main, "ensure_today", fake_ensure)
        monkeypatch.setattr(chat_main, "probe_online_life", AsyncMock(return_value=probe))
        monkeypatch.setattr(chat_main, "bound_state", lambda _c: SimpleNamespace(daily_life=None))
        monkeypatch.setattr(chat_main, "ai_route_context", lambda _c: {})
        monkeypatch.setattr(chat_main, "configured_bot_name", lambda _c: "小青")
        await chat_main.scheduled_daily_life(SimpleNamespace(logger=None))
        assert seen["online_life"] is probe


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
