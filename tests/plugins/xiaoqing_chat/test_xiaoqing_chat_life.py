"""角色在时间上的连续性：作息、收尾沉默、自我陈述、未完话头、记忆用法、改口、审查和主动开口。"""

from __future__ import annotations

import json
import time
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from plugins.xiaoqing_chat.chat_runtime import ChatRuntime
from plugins.xiaoqing_chat.config.config import XiaoQingChatConfig
from plugins.xiaoqing_chat.life import extract as life_extract
from plugins.xiaoqing_chat.life import proactive
from plugins.xiaoqing_chat.life.closing_guard import bot_closed, closing_kinds, should_stay_silent
from plugins.xiaoqing_chat.life.open_loops import OpenLoopStore
from plugins.xiaoqing_chat.life.schedule import (
    current_slot,
    delay_factor,
    parse_routine,
    participation_factor,
    state_text,
)
from plugins.xiaoqing_chat.life.self_timeline import (
    SelfTimelineStore,
    new_entry,
    self_timeline_block,
)
from plugins.xiaoqing_chat.memory.memory import StoredMessage
from plugins.xiaoqing_chat.memory.memory_db import MemoryDB, RetrievedItem
from plugins.xiaoqing_chat.memory.mention_policy import (
    AVOID,
    MENTION,
    TONE,
    UNCERTAIN,
    classify,
    format_items,
)
from plugins.xiaoqing_chat.memory.person_profile import load_profile, update_profile_and_index
from plugins.xiaoqing_chat.persona import PersonaCard
from plugins.xiaoqing_chat.runtime_state import ChatRuntimeState

NOW = 1_800_000_000.0


@pytest.fixture(autouse=True)
def _fresh_extraction_throttle():
    life_extract._last_state_read.clear()
    yield
    life_extract._last_state_read.clear()


def _user(
    text: str, *, ts: float, user_id: int = 7, name: str = "阿梨", message_id: int | None = None
):
    return StoredMessage(
        role="user", name=name, ts=ts, user_id=user_id, content=text, message_id=message_id
    )


def _bot(text: str, *, ts: float, source: str = "xiaoqing_chat"):
    return StoredMessage(role="assistant", name="小青", ts=ts, content=text, source_plugin=source)


# ---------------------------------------------------------------- 作息


ROUTINE = (
    "周一至周五 08:00-11:50 上课（忙）",
    "周一、三 14:00-17:30 做实验（忙）",
    "周日至周四 23:30-07:00 睡觉（睡）",
    "每天 17:30-18:30 吃晚饭",
)


def test_routine_slots_follow_days_order_and_midnight() -> None:
    slots = parse_routine(ROUTINE)
    # 2026-10-26 是周一。
    assert current_slot(slots, datetime(2026, 10, 26, 9, 0)).activity == "上课"
    assert current_slot(slots, datetime(2026, 10, 28, 15, 0)).activity == "做实验"
    assert current_slot(slots, datetime(2026, 10, 27, 15, 0)) is None
    # 周日 23:30 开始的睡眠一直延续到周一早上。
    assert current_slot(slots, datetime(2026, 10, 26, 3, 0)).availability == "asleep"
    # 周五晚不在“周日至周四”里，周六凌晨也就不算睡觉时段。
    assert current_slot(slots, datetime(2026, 10, 31, 3, 0)) is None
    assert current_slot(slots, datetime(2026, 11, 1, 18, 0)).availability == "free"


def test_routine_pacing_and_prompt_text() -> None:
    busy, asleep = parse_routine(ROUTINE)[0], parse_routine(ROUTINE)[2]
    assert participation_factor(busy, busy=0.5, asleep=0.1) == 0.5
    assert participation_factor(asleep, busy=0.5, asleep=0.1) == 0.1
    assert participation_factor(None, busy=0.5, asleep=0.1) == 1.0
    assert delay_factor(asleep, busy=1.5, asleep=3.0) == 3.0
    assert "上课" in state_text(busy) and "回得短一点" in state_text(busy)
    assert state_text(None) == ""


def test_routine_section_is_validated_and_kept_out_of_facts() -> None:
    card = PersonaCard.parse(
        "## 你是谁\n- 学生。\n\n## 作息〔作息〕\n- 工作日 08:00-12:00 上课（忙）\n"
    )
    assert card.routine == ("工作日 08:00-12:00 上课（忙）",)
    assert "上课" not in card.facts("小青")
    with pytest.raises(ValueError, match="作息"):
        PersonaCard.parse("## 你是谁\n- 学生。\n\n## 作息〔作息〕\n- 偶尔 上课\n")


# ---------------------------------------------------------------- 私聊收尾沉默


@pytest.mark.parametrize("text", ["晚安", "那我也睡啦", "拜拜！", "好的谢谢", "嗯嗯", "爱你mua"])
def test_closing_vocabulary_accepts_pure_closings(text: str) -> None:
    assert closing_kinds(text)


@pytest.mark.parametrize(
    "text",
    [
        "晚安，明天几点起？",
        "23:00 晚安",
        "[图片：猫] 晚安",
        "晚安，今天好累",
        "睡不着",
        "你好",
        "大家好",
    ],
)
def test_closing_vocabulary_rejects_new_content(text: str) -> None:
    assert not closing_kinds(text)


def test_bot_closing_needs_a_strong_closing_without_question() -> None:
    assert bot_closed("好嘞，早点睡，晚安～")
    assert not bot_closed("好的")
    assert not bot_closed("早呀")
    assert not bot_closed("晚安，明天要我叫你吗？")


def test_silence_only_after_the_bot_has_closed() -> None:
    history = [_user("我睡了", ts=NOW - 60), _bot("晚安，好梦", ts=NOW - 50)]
    assert should_stay_silent(
        "晚安",
        [*history, _user("晚安", ts=NOW, message_id=9)],
        now                = NOW,
        window_seconds     = 1800,
        current_message_id = 9,
    )
    # 角色还没收尾时，第一句晚安要回。
    assert not should_stay_silent(
        "晚安",
        [_user("今天好累", ts=NOW - 60), _bot("抱抱", ts=NOW - 50)],
        now            = NOW,
        window_seconds = 1800,
    )
    # 收尾之后对方又说了别的内容，那一句还没被接住。
    assert not should_stay_silent(
        "晚安", [*history, _user("对了明天降温", ts=NOW - 10)], now=NOW, window_seconds=1800
    )
    # 收尾太久以前的，不算这一段对话。
    assert not should_stay_silent("晚安", history, now=NOW + 7200, window_seconds=1800)


# ---------------------------------------------------------------- 自我陈述


def test_self_timeline_scopes_promises_and_expires(tmp_path) -> None:
    store = SelfTimelineStore()
    store.bind(tmp_path)
    state = new_entry(
        chat_id="g1", kind="state", text="在食堂吃糖醋里脊", hours=1, source="chat", now=NOW
    )
    promise = new_entry(
        chat_id     = "u7",
        kind        = "promise",
        text        = "帮她整理照片",
        hours       = 500,
        source      = "agent",
        counterpart = "阿梨",
        now         = NOW,
    )
    store.apply(added=[state, promise], chat_id="g1", max_entries=10, now=NOW)

    assert [e.text for e in store.visible("u7", now=NOW + 60)] == [
        "在食堂吃糖醋里脊",
        "帮她整理照片",
    ]
    # 私聊里答应的事不带进群里；近况属于她自己，各处共用。
    assert [e.text for e in store.visible("g2", now=NOW + 60)] == ["在食堂吃糖醋里脊"]
    # 近况一小时后过期；承诺的存活时间被夹到 168 小时。
    assert [e.kind for e in store.visible("u7", now=NOW + 7200)] == ["promise"]
    assert promise.expires_at == NOW + 168 * 3600

    store.apply(resolved=[(promise.entry_id, "done")], chat_id="g2", max_entries=10, now=NOW)
    assert store.visible("u7", now=NOW + 60)[-1].status == "active", "别的会话不能结束这条承诺"
    store.apply(resolved=[(promise.entry_id, "done")], chat_id="u7", max_entries=10, now=NOW)
    assert all(e.kind != "promise" for e in store.visible("u7", now=NOW + 60))

    reloaded = SelfTimelineStore()
    reloaded.bind(tmp_path)
    assert [e.text for e in reloaded.visible("u7", now=NOW + 60)] == ["在食堂吃糖醋里脊"]
    reloaded.clear("g1")
    assert reloaded.visible("u7", now=NOW + 60) == []


def test_self_timeline_block_describes_entries() -> None:
    promise = new_entry(
        chat_id     = "u7",
        kind        = "promise",
        text        = "帮她整理照片",
        hours       = 5,
        source      = "agent",
        counterpart = "阿梨",
        now         = NOW - 7200,
    )
    block = self_timeline_block([promise], now=NOW)
    assert "约2小时前答应阿梨：帮她整理照片（还没办完）" in block
    assert self_timeline_block([], now=NOW) == ""


# ---------------------------------------------------------------- 未完话头


def test_open_loop_is_asked_once_then_reviewed(tmp_path) -> None:
    store = OpenLoopStore()
    store.bind(tmp_path)
    store.add(
        "g1",
        user_id   = 7,
        user_name = "阿梨",
        title     = "数电期末考试",
        due_at    = NOW,
        max_loops = 10,
        now       = NOW - 86400,
    )
    # 同一件事再次提到只更新时间。
    store.add(
        "g1",
        user_id   = 7,
        user_name = "阿梨",
        title     = "数电期末考试改期",
        due_at    = NOW + 3600,
        max_loops = 10,
        now       = NOW - 3600,
    )
    assert len(store.get("g1")) == 1

    assert store.for_speaker("g1", 7, followup_seconds=36 * 3600, now=NOW) is None, "时间还没到"
    loop, mode = store.for_speaker("g1", 7, followup_seconds=36 * 3600, now=NOW + 7200)
    assert mode == "ask" and loop.user_id == 7
    assert store.for_speaker("g1", 8, followup_seconds=36 * 3600, now=NOW + 7200) is None, (
        "只对本人提起"
    )

    store.mark_asked("g1", loop.loop_id, now=NOW + 7200)
    assert store.for_speaker("g1", 7, followup_seconds=36 * 3600, now=NOW + 7300)[1] == "review"
    store.close("g1", {loop.loop_id}, now=NOW + 7400)
    assert store.for_speaker("g1", 7, followup_seconds=36 * 3600, now=NOW + 7500) is None


# ---------------------------------------------------------------- 投递后抽取


def _extraction_state(tmp_path):
    state = ChatRuntimeState()
    state.self_timeline.bind(tmp_path)
    state.open_loops.bind(tmp_path)
    return state


@pytest.mark.asyncio
async def test_extraction_records_self_claims_and_loops_from_trusted_speakers(tmp_path) -> None:
    state   = _extraction_state(tmp_path)
    history = [
        _user("我明天上午九点考数电", ts=NOW - 30),
        _bot("我刚在食堂，考完告诉我结果，我帮你对答案", ts=NOW),
    ]
    model_output = json.dumps(
        {
            "self": [
                {"kind": "state", "text": "在食堂", "hours": 99},
                {"kind": "promise", "text": "帮阿梨对答案", "hours": 24, "counterpart": "阿梨"},
                {"kind": "gossip", "text": "不认识的类别"},
            ],
            "resolved_self": [{"id": "not-a-real-id", "status": "done"}],
            "loops"        : [
                {"user_id": 7, "title": "数电考试", "due": "2027-01-15T09:00"},
                {"user_id": 999, "title": "冒名的人", "due": "2027-01-15T09:00"},
            ],
            "resolved_loops": [],
        },
        ensure_ascii=False,
    )
    with (
        patch.object(life_extract, "chat_completions", AsyncMock(return_value=model_output)),
        patch.object(
            life_extract.time, "time", return_value=datetime(2027, 1, 14, 20, 0).timestamp()
        ),
    ):
        called = await life_extract.extract_life_records(
            state    = state,
            life_cfg = XiaoQingChatConfig().life,
            secrets  = {"_ai": object()},
            llm      = XiaoQingChatConfig().llm_config("reasoning"),
            chat_id  = "g1",
            bot_name = "小青",
            history  = history,
            source   = "chat",
        )
    assert called
    entries = state.self_timeline.entries()
    assert [(e.kind, e.text) for e in entries] == [("state", "在食堂"), ("promise", "帮阿梨对答案")]
    assert entries[0].expires_at - entries[0].ts == 12 * 3600, "近况最多维持 12 小时"
    loops = state.open_loops.get("g1")
    assert [(loop.user_id, loop.user_name, loop.title) for loop in loops] == [
        (7, "阿梨", "数电考试")
    ]


@pytest.mark.asyncio
async def test_extraction_skips_model_without_any_cue(tmp_path) -> None:
    state = _extraction_state(tmp_path)
    model = AsyncMock(return_value="{}")
    with patch.object(life_extract, "chat_completions", model):
        called = await life_extract.extract_life_records(
            state    = state,
            life_cfg = XiaoQingChatConfig().life,
            secrets  = {"_ai": object()},
            llm      = XiaoQingChatConfig().llm_config("reasoning"),
            chat_id  = "g1",
            bot_name = "小青",
            history=[_user("哈哈哈", ts=NOW - 5), _bot("笑死", ts=NOW)],
            source="chat",
        )
    assert not called and model.await_count == 0


@pytest.mark.asyncio
async def test_agent_result_resolves_an_earlier_promise(tmp_path) -> None:
    state   = _extraction_state(tmp_path)
    promise = new_entry(
        chat_id="u7", kind="promise", text="整理照片", hours=24, source="agent", now=time.time()
    )
    state.self_timeline.apply(added=[promise], chat_id="u7", max_entries=10)
    output = json.dumps({"self": [], "resolved_self": [{"id": promise.entry_id, "status": "done"}]})
    with patch.object(life_extract, "chat_completions", AsyncMock(return_value=output)):
        await life_extract.extract_life_records(
            state    = state,
            life_cfg = XiaoQingChatConfig().life,
            secrets  = {"_ai": object()},
            llm      = XiaoQingChatConfig().llm_config("reasoning"),
            chat_id  = "u7",
            bot_name = "小青",
            history  = [
                _bot("照片整理好了，按日期分了文件夹", ts=time.time(), source="xiaoqing_agent")
            ],
            source="agent",
        )
    assert state.self_timeline.visible("u7") == []


# ---------------------------------------------------------------- 记忆用法


def test_mention_policy_labels_and_current_state_filter() -> None:
    fresh = {"type": "topic_summary", "updated_at": NOW - 600}
    old   = {"type": "topic_summary", "updated_at": NOW - 40 * 86400}
    assert classify("阿梨和老王吵了一架", fresh, now=NOW) == AVOID
    assert classify("阿梨的猫上个月去世了", fresh, now=NOW) == TONE
    assert classify("阿梨在追芙莉莲", old, now=NOW) == UNCERTAIN
    assert (
        classify("阿梨喜欢猫", {"type": "person_profile", "updated_at": NOW - 400 * 86400}, now=NOW)
        == MENTION
    )

    items = [
        RetrievedItem(
            "a", "阿梨今天在实验室加班", 0.9, {"type": "topic_summary", "updated_at": NOW - 86400}
        ),
        RetrievedItem(
            "b", "阿梨<7>：不吃香菜", 0.8, {"type": "person_info", "updated_at": NOW - 86400}
        ),
    ]
    lines = format_items(items, current_text="你在干嘛呢", now=NOW)
    assert lines == ["- [可以自然提起] 阿梨<7>：不吃香菜"], "问当下状态时不拿昨天的状态当现在"
    assert len(format_items(items, current_text="周末去哪玩", now=NOW)) == 2


# ---------------------------------------------------------------- 改口


def test_single_valued_attribute_replaces_old_fact(tmp_path) -> None:
    db     = MemoryDB()
    kwargs = {
        "data_dir"    : tmp_path,
        "memory_db"   : db,
        "chat_id"     : "g1",
        "subject_id"  : 7,
        "subject_name": "阿梨",
    }
    update_profile_and_index(
        new_facts=["我住在杭州", "我喜欢猫"], attributes={"我住在杭州": "居住地"}, **kwargs
    )
    replaced = update_profile_and_index(
        new_facts=["我搬到上海了"], attributes={"我搬到上海了": "居住地"}, **kwargs
    )

    assert [(item.old_fact, item.new_fact) for item in replaced] == [("我住在杭州", "我搬到上海了")]
    profile = load_profile(tmp_path, chat_id="g1", subject_id=7)
    assert profile.facts == ["我喜欢猫", "我搬到上海了"]
    assert profile.attributes == {"居住地": "我搬到上海了"}
    text = db.get("profile:g1:7").text
    assert "以前说过、后来改了" in text and "我住在杭州" in text


# ---------------------------------------------------------------- 主动开口


def test_active_hours_wrap_midnight() -> None:
    from plugins.xiaoqing_chat.config.config import ProactiveConfig

    assert ProactiveConfig(active_hours_end="24:00").active_hours_end == "24:00"
    with pytest.raises(ValueError, match="HH:MM"):
        ProactiveConfig(active_hours_end="24:30")
    assert proactive.within_active_hours("09:00", "23:00", 10 * 60)
    assert not proactive.within_active_hours("09:00", "23:00", 23 * 60 + 30)
    assert proactive.within_active_hours("22:00", "02:00", 60)


def test_plugin_notices_stop_at_her_last_word() -> None:
    history = [
        _bot("旧的推送", ts=NOW - 600, source="apod"),
        _bot("她自己接过话", ts=NOW - 500),
        _bot("今日天文图：猎户座大星云", ts=NOW - 100, source="apod"),
    ]
    assert proactive.plugin_notices(history, now=NOW) == ["（apod 推送）今日天文图：猎户座大星云"]


def _proactive_runtime(**overrides) -> ChatRuntime:
    cfg = XiaoQingChatConfig.model_validate({"proactive": {"enabled": True, **overrides}})
    return ChatRuntime(cfg, [])


async def _consider(tmp_path, history, outputs, *, runtime=None):
    state    = ChatRuntimeState()
    data_dir = tmp_path / f"run{len(list(tmp_path.iterdir()))}"
    for store in (state.self_timeline, state.open_loops, state.proactive_state):
        store.bind(data_dir)
    state.memory_store.get_recent_async = AsyncMock(return_value=history)
    model = AsyncMock(side_effect=outputs)
    with (
        patch.object(proactive, "chat_completions", model),
        patch.object(proactive, "configured_bot_name", return_value="小青"),
        patch.object(proactive, "ai_route_context", return_value={"_ai": object()}),
    ):
        delivery = await proactive._consider_group(
            SimpleNamespace(logger=None),
            runtime or _proactive_runtime(),
            state,
            42,
            now       = NOW,
            life_text = "",
        )
    return delivery, model, state


@pytest.mark.asyncio
async def test_proactive_sends_after_draft_and_judge(tmp_path) -> None:
    history = [
        _user("有人看今天的 APOD 吗", ts=NOW - 3 * 3600),
        _bot("今日天文图：土星环", ts=NOW - 2 * 3600, source="apod"),
    ]
    delivery, model, _state = await _consider(
        tmp_path,
        history,
        [
            '{"decision":"send","text":"今天这张土星环也太好看了"}',
            '{"decision":"send","reason":"自然"}',
        ],
    )
    assert delivery is not None and delivery.target.target_id == 42
    assert delivery.message[0]["data"]["text"] == "今天这张土星环也太好看了"
    assert model.await_count == 2
    assert "今日天文图：土星环" in model.await_args_list[0].kwargs["messages"][1]["content"]


@pytest.mark.asyncio
async def test_proactive_respects_judge_skip_and_quiet_rules(tmp_path) -> None:
    quiet = [_user("晚上吃啥", ts=NOW - 3 * 3600)]
    delivery, _model, _state = await _consider(
        tmp_path,
        quiet,
        ['{"decision":"send","text":"食堂二楼走起"}', '{"decision":"drop","reason":"突兀"}'],
    )
    assert delivery is None

    delivery, model, _state = await _consider(
        tmp_path, quiet, ['{"decision":"skip","reason":"no_natural_opening"}']
    )
    assert delivery is None and model.await_count == 1

    # 群里刚有人说话，或者最后一句就是她自己说的：都不开口，也不调用模型。
    for history in ([_user("在吗", ts=NOW - 60)], [_bot("有人吗", ts=NOW - 3 * 3600)]):
        delivery, model, _state = await _consider(tmp_path, history, [])
        assert delivery is None and model.await_count == 0


@pytest.mark.asyncio
async def test_proactive_commit_records_message_and_rhythm(tmp_path) -> None:
    history = [_user("有人看今天的 APOD 吗", ts=NOW - 3 * 3600)]
    delivery, _model, state = await _consider(
        tmp_path,
        history,
        ['{"decision":"send","text":"今天那张图好看"}', '{"decision":"send","reason":"ok"}'],
    )
    appended: list[dict]      = []
    state.memory_store.append = lambda chat_id, **kwargs: appended.append(
        {"chat_id": chat_id, **kwargs}
    )
    with (
        patch.object(proactive, "schedule_memory_persist"),
        patch.object(proactive, "schedule_action_history_flush"),
        patch.object(proactive, "spawn_life_extraction") as extraction,
    ):
        await delivery.receipt.record(True)
    assert appended[0]["chat_id"] == "g42" and appended[0]["role"] == "assistant"
    record = state.proactive_state.get("g42")
    assert record.count == 1 and record.last_ts > 0
    assert extraction.call_args.kwargs["source"] == "proactive"


def test_open_loop_stops_surfacing_after_repeated_misses(tmp_path) -> None:
    store = OpenLoopStore()
    store.bind(tmp_path)
    store.add(
        "g1", user_id=7, user_name="阿梨", title="面试", due_at=NOW, max_loops=10, now=NOW - 3600
    )
    loop, _mode = store.for_speaker("g1", 7, followup_seconds=86400, now=NOW + 60)
    for _ in range(3):
        store.mark_surfaced("g1", loop.loop_id)
    assert store.for_speaker("g1", 7, followup_seconds=86400, now=NOW + 60) is None


@pytest.mark.asyncio
async def test_extraction_recognises_a_reworded_follow_up_question(tmp_path) -> None:
    state = _extraction_state(tmp_path)
    state.open_loops.add(
        "g1",
        user_id   = 7,
        user_name = "阿梨",
        title     = "数电期末考试",
        due_at    = time.time() - 3600,
        max_loops = 10,
    )
    state.open_loops.add(
        "g1",
        user_id   = 8,
        user_name = "老王",
        title     = "驾照科目二",
        due_at    = time.time() - 3600,
        max_loops = 10,
    )
    mine, theirs = state.open_loops.get("g1")
    output = json.dumps({"asked_loops": [mine.loop_id, theirs.loop_id]})
    model = AsyncMock(return_value=output)
    with patch.object(life_extract, "chat_completions", model):
        await life_extract.extract_life_records(
            state    = state,
            life_cfg = XiaoQingChatConfig().life,
            secrets  = {"_ai": object()},
            llm      = XiaoQingChatConfig().llm_config("reasoning"),
            chat_id  = "g1",
            bot_name = "小青",
            history=[_user("刚出考场", ts=time.time() - 5), _bot("考得咋样", ts=time.time())],
            source="chat",
        )
    statuses = {loop.user_id: loop.status for loop in state.open_loops.get("g1")}
    # 只把这段对话里说话人的话头交给模型；别人的话头不会被顺带改掉。
    assert statuses == {7: "asked", 8: "open"}
    assert "驾照科目二" not in model.await_args.kwargs["messages"][1]["content"]


def test_bot_closing_with_a_trailing_reminder_still_counts() -> None:
    assert bot_closed("好，快去吃饭吧\n\n回聊～\n\n面试要是想练了再喊我呀")
    assert bot_closed("晚安晚安，快睡吧～")
    assert bot_closed("晚安，睡个好觉～周五交实验报告别忘啦！")
    assert not bot_closed("抱抱，想说就说")


@pytest.mark.asyncio
async def test_extraction_ignores_canned_fallbacks_but_reads_subjectless_status(tmp_path) -> None:
    state = _extraction_state(tmp_path)
    model = AsyncMock(
        return_value=json.dumps({"self": [{"kind": "state", "text": "刚下课", "hours": 1}]})
    )
    kwargs = {
        "state"   : state,
        "life_cfg": XiaoQingChatConfig().life,
        "secrets" : {"_ai": object()},
        "llm"     : XiaoQingChatConfig().llm_config("reasoning"),
        "chat_id" : "g1",
        "bot_name": "小青",
        "source"  : "chat",
    }
    with patch.object(life_extract, "chat_completions", model):
        assert not await life_extract.extract_life_records(
            history=[_user("在干嘛", ts=NOW - 5), _bot("等我一下，脑子突然卡壳了", ts=NOW)],
            **kwargs,
        )
        assert await life_extract.extract_life_records(
            history=[_user("在干嘛", ts=NOW - 5), _bot("刚下课，正往食堂冲", ts=NOW)], **kwargs
        )
    assert model.await_count == 1


@pytest.mark.asyncio
async def test_proactive_backs_off_after_a_skip(tmp_path) -> None:
    quiet = [_user("晚上吃啥", ts=NOW - 3 * 3600)]
    state = ChatRuntimeState()
    for store in (state.self_timeline, state.open_loops, state.proactive_state):
        store.bind(tmp_path)
    state.memory_store.get_recent_async = AsyncMock(return_value=quiet)
    model = AsyncMock(return_value='{"decision":"skip","reason":"no_natural_opening"}')
    with (
        patch.object(proactive, "chat_completions", model),
        patch.object(proactive, "configured_bot_name", return_value="小青"),
        patch.object(proactive, "ai_route_context", return_value={"_ai": object()}),
    ):
        for offset in (0, 600, 3 * 3600):
            await proactive._consider_group(
                SimpleNamespace(logger=None),
                _proactive_runtime(),
                state,
                42,
                now       = NOW + offset,
                life_text = "",
            )
    # 第二次在两小时内被跳过，第三次才再问模型。
    assert model.await_count == 2
