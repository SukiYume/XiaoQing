"""角色在时间上的连续性：作息、收尾沉默、自我陈述、未完话头、记忆用法、改口、审查和主动开口。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

import tests.helpers.xiaoqing_chat_test_support as _fixture_support
from plugins.xiaoqing_chat.config.config import XiaoQingChatConfig
from plugins.xiaoqing_chat.life import extract as life_extract
from plugins.xiaoqing_chat.life import proactive
from plugins.xiaoqing_chat.life.closing_guard import bot_closed, closing_kinds, should_stay_silent
from plugins.xiaoqing_chat.life.schedule import (
    current_slot,
    parse_routine,
    routine_factor,
    state_text,
)
from plugins.xiaoqing_chat.life.self_timeline import new_entry, self_timeline_block
from plugins.xiaoqing_chat.message_parts import render_stored_message
from plugins.xiaoqing_chat.persona import PersonaCard
from plugins.xiaoqing_chat.runtime_state import ChatRuntimeState, reset_global_state
from plugins.xiaoqing_chat.store_binding import bound_state
from tests.helpers.assertions import logged_events
from tests.helpers.xiaoqing_chat_memory_test_support import MemoryHarness, put_history
from tests.helpers.xiaoqing_chat_test_support import (
    ScriptedChatModel,
    bot_message,
    scripted_chat_context,
    settle_chat_background,
    user_message,
)

mock_context = _fixture_support.mock_context

NOW = 1_800_000_000.0


@pytest.fixture(autouse=True)
def _fresh_extraction_throttle():
    life_extract._last_state_read.clear()
    yield
    life_extract._last_state_read.clear()


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
    assert routine_factor(busy, busy=0.5, asleep=0.1) == 0.5
    assert routine_factor(asleep, busy=0.5, asleep=0.1) == 0.1
    assert routine_factor(None, busy=0.5, asleep=0.1) == 1.0
    assert routine_factor(asleep, busy=1.5, asleep=3.0) == 3.0
    assert "上课" in state_text(busy) and "回得短一点" in state_text(busy)
    free = parse_routine(ROUTINE)[3]
    assert "吃晚饭" in state_text(free)
    assert state_text(asleep) != state_text(free) != state_text(busy)
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


@pytest.mark.parametrize(
    ("text", "closed"),
    [
        pytest.param("好嘞，早点睡，晚安～", True, id="strong-closing"),
        pytest.param("晚安晚安，快睡吧～", True, id="repeated-goodnight"),
        pytest.param(
            "好，快去吃饭吧\n\n回聊～\n\n面试要是想练了再喊我呀", True, id="trailing-reminder"
        ),
        pytest.param("晚安，睡个好觉～周五交实验报告别忘啦！", True, id="goodnight-with-reminder"),
        pytest.param("好的", False, id="weak-ack"),
        pytest.param("早呀", False, id="greeting"),
        pytest.param("晚安，明天要我叫你吗？", False, id="closing-with-question"),
        pytest.param("抱抱，想说就说", False, id="comfort-keeps-door-open"),
    ],
)
def test_bot_closing_needs_a_strong_closing_without_question(text: str, closed: bool) -> None:
    assert bot_closed(text) is closed


def test_silence_only_after_the_bot_has_closed() -> None:
    history = [user_message("我睡了", ts=NOW - 60), bot_message("晚安，好梦", ts=NOW - 50)]
    assert should_stay_silent(
        "晚安",
        [*history, user_message("晚安", ts=NOW, message_id=9)],
        now                = NOW,
        window_seconds     = 1800,
        current_message_id = 9,
    )
    # 角色还没收尾时，第一句晚安要回。
    assert not should_stay_silent(
        "晚安",
        [user_message("今天好累", ts=NOW - 60), bot_message("抱抱", ts=NOW - 50)],
        now            = NOW,
        window_seconds = 1800,
    )
    # 收尾之后对方又说了别的内容，那一句还没被接住。
    assert not should_stay_silent(
        "晚安", [*history, user_message("对了明天降温", ts=NOW - 10)], now=NOW, window_seconds=1800
    )
    # 收尾太久以前的，不算这一段对话。
    assert not should_stay_silent("晚安", history, now=NOW + 7200, window_seconds=1800)


# ---------------------------------------------------------------- 自我陈述


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


# ---------------------------------------------------------------- 投递后抽取


def _extraction_setup(tmp_path, chat_id, messages):
    """聊天历史里放好这几条消息，接上临时目录里的统一记忆；返回（状态，上下文，记忆，历史）。"""

    state = ChatRuntimeState()
    state.memory_store.bind_data_dir(tmp_path)
    for message in messages:
        state.memory_store.append(
            chat_id,
            role          = message.role,
            name          = message.name,
            user_id       = message.user_id,
            content       = message.content,
            ts            = message.ts,
            source_plugin = message.source_plugin,
        )
    harness = MemoryHarness(tmp_path)
    context = SimpleNamespace(
        data_dir=tmp_path, capabilities=SimpleNamespace(memory=harness.service(chat_id))
    )
    return state, context, harness, state.memory_store.get(chat_id)


async def _extract(state, context, chat_id, history, model, source="chat"):
    return await life_extract.extract_life_records(
        context  = context,
        state    = state,
        life_cfg = XiaoQingChatConfig().life,
        secrets  = {"_ai": model},
        llm      = XiaoQingChatConfig().llm_config("reasoning"),
        chat_id  = chat_id,
        bot_name = "小青",
        history  = history,
        source   = source,
    )


@pytest.mark.asyncio
async def test_extraction_records_self_claims_and_loops_from_trusted_speakers(tmp_path) -> None:
    now = time.time()
    state, context, memory, h = _extraction_setup(
        tmp_path,
        "g1",
        [
            user_message("我明天上午九点考数电", ts=now - 30),
            bot_message("我刚在食堂，考完告诉我结果，我帮你对答案", ts=now),
        ],
    )
    due = datetime.fromtimestamp(now + 86400, ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%dT09:00")
    model_output = json.dumps(
        {
            "self": [
                {"kind": "state", "text": "在食堂", "hours": 99},
                {"kind": "promise", "text": "帮阿梨对答案", "hours": 24, "counterpart": "阿梨"},
                {"kind": "gossip", "text": "不认识的类别"},
            ],
            "resolved_self": [{"id": "not-a-real-id", "status": "done"}],
            "loops"        : [
                {"user_id": 7, "title": "数电考试", "due": due},
                {"user_id": 999, "title": "冒名的人", "due": due},
            ],
            "resolved_loops": [],
        },
        ensure_ascii=False,
    )
    assert await _extract(state, context, "g1", h, ScriptedChatModel(reasoning=model_output))
    entries = memory.notes("global", "g1", kinds=("self.state", "self.promise"))
    assert [(note.kind, note.text, note.scope) for note in entries] == [
        ("self.state", "在食堂", "global"),
        ("self.promise", "帮阿梨对答案", "g1"),
    ]
    assert entries[0].expires_at - entries[0].created_at == pytest.approx(12 * 3600, abs=5)
    loops = memory.notes("g1", kinds=("person.loop",))
    assert [(loop.data["user_id"], loop.data["user_name"], loop.text) for loop in loops] == [
        (7, "阿梨", "数电考试")
    ]


@pytest.mark.asyncio
async def test_extraction_skips_model_without_any_cue(tmp_path) -> None:
    state, context, _memory, history = _extraction_setup(
        tmp_path, "g1", [user_message("哈哈哈", ts=NOW - 5), bot_message("笑死", ts=NOW)]
    )
    model = ScriptedChatModel(reasoning="{}")
    called = await _extract(state, context, "g1", history, model)
    assert not called and model.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["both-features-off", "no-model"])
async def test_extraction_does_nothing_without_a_feature_or_a_model(tmp_path, missing) -> None:
    state, context, memory, history = _extraction_setup(
        tmp_path,
        "g1",
        [user_message("我明天上午九点考数电", ts=NOW - 30), bot_message("我刚在食堂", ts=NOW)],
    )
    model = ScriptedChatModel(reasoning='{"self": [{"kind": "state", "text": "在食堂"}]}')
    life_cfg = XiaoQingChatConfig().life
    if missing == "both-features-off":
        life_cfg = life_cfg.model_copy(
            update={"enable_self_timeline": False, "enable_open_loops": False}
        )
    called = await life_extract.extract_life_records(
        context  = context,
        state    = state,
        life_cfg = life_cfg,
        secrets  = {} if missing == "no-model" else {"_ai": model},
        llm      = XiaoQingChatConfig().llm_config("reasoning"),
        chat_id  = "g1",
        bot_name = "小青",
        history  = history,
        source   = "chat",
    )

    # 两个功能都关着、或者没有可用的模型：不调模型，也不往记忆里写任何东西。
    assert called is False and model.calls == []
    assert memory.notes("global", "g1", kinds=("self.state", "self.promise")) == []


@pytest.mark.asyncio
async def test_agent_result_resolves_an_earlier_promise(tmp_path) -> None:
    now = time.time()
    state, context, memory, history = _extraction_setup(
        tmp_path, "u7", [bot_message("好，照片我回头整理完发你", ts=now - 60)]
    )
    promise = json.dumps({"self": [{"kind": "promise", "text": "整理照片", "hours": 24}]})
    await _extract(state, context, "u7", history, ScriptedChatModel(reasoning=promise))
    [note] = memory.notes("u7", kinds=("self.promise",))
    state.memory_store.append(
        "u7",
        role          = "assistant",
        name          = "小青",
        content       = "照片整理好了，按日期分了文件夹",
        ts            = now,
        source_plugin = "xiaoqing_agent",
    )
    output = json.dumps({"self": [], "resolved_self": [{"id": note.id, "status": "done"}]})
    await _extract(
        state,
        context,
        "u7",
        state.memory_store.get("u7"),
        ScriptedChatModel(reasoning=output),
        source="agent",
    )
    assert memory.notes("u7", kinds=("self.promise",))[0].status == "done"


@pytest.mark.asyncio
async def test_agent_reply_records_friends_loops_but_not_her_own_promises(tmp_path) -> None:
    """办事回复里的“到点提醒你”由提醒工具兑现，不算她欠着的事；对方说的行程照样记成话头。"""

    now = time.time()
    state, context, memory, history = _extraction_setup(
        tmp_path,
        "u7",
        [
            user_message("后天下午三点高铁出发，帮我加个日程", ts=now - 30),
            bot_message("加好了，后天下午三点出发，到点提醒你", ts=now, source="xiaoqing_agent"),
        ],
    )
    due = datetime.fromtimestamp(now + 2 * 86400, ZoneInfo("Asia/Shanghai")).strftime(
        "%Y-%m-%dT15:00"
    )
    output = json.dumps(
        {
            "self": [
                {"kind": "promise", "text": "到点提醒对方出发", "hours": 60},
                {"kind": "state", "text": "在帮对方加日程", "hours": 1},
            ],
            "loops": [{"user_id": 7, "title": "高铁出发", "due": due}],
        },
        ensure_ascii=False,
    )
    model = ScriptedChatModel(reasoning=output)
    assert await _extract(state, context, "u7", history, model, source="agent")
    assert memory.notes("global", "u7", kinds=("self.promise", "self.state")) == []
    assert [loop.text for loop in memory.notes("u7", kinds=("person.loop",))] == ["高铁出发"]


def test_agent_reply_without_a_done_or_loop_cue_skips_the_model() -> None:
    def worth(new_self: bool) -> bool:
        return life_extract.worth_extracting(
            reply         = "加好了，明天到点提醒你",
            dialogue      = ["阿梨<7>：帮我加个日程"],
            open_self     = [],
            open_loops    = [],
            self_enabled  = True,
            loops_enabled = True,
            new_self      = new_self,
        )

    # 聊天回复里的“明天”是她自己的打算线索；办事回复里不是。
    assert worth(True) and not worth(False)


# ---------------------------------------------------------------- 主动开口


def test_active_hours_wrap_midnight() -> None:
    from plugins.xiaoqing_chat.config.config import ProactiveConfig

    assert ProactiveConfig(active_hours_end="24:00").active_hours_end == "24:00"
    with pytest.raises(ValueError, match="HH:MM"):
        ProactiveConfig(active_hours_end="24:30")
    assert proactive.within_active_hours("09:00", "23:00", 10 * 60)
    assert not proactive.within_active_hours("09:00", "23:00", 23 * 60 + 30)
    assert proactive.within_active_hours("22:00", "02:00", 60)


def _hours_ago(hours: float) -> float:
    return time.time() - hours * 3600


def _proactive_setup(mock_context, tmp_path, model, history, **life):
    """主动开口检查要的上下文：群 42 是默认群、全天都在活跃时段、人设卡里没有作息（不会因为
    睡觉 / 上课而跳过）；聊天记录是 ``history``。每次都是全新的运行时状态和数据目录。"""

    reset_global_state()
    mock_context.data_dir = tmp_path / f"run{len(list(tmp_path.glob('run*')))}"
    persona               = tmp_path / "persona.md"
    persona.write_text("## 你是谁\n- 学生。\n", encoding="utf-8")
    scripted_chat_context(
        mock_context,
        model,
        {
            "proactive": {
                "enabled"           : True,
                "active_hours_start": "00:00",
                "active_hours_end"  : "00:00",
            },
            "personality": {"persona_file": str(persona)},
            "life"       : life,
        },
    )
    mock_context.default_groups = lambda: [42]
    put_history(bound_state(mock_context), "g42", history)
    return mock_context


async def _consider(mock_context, tmp_path, history, *, settle=True, life=None, **scripts):
    """让主动开口的定时检查跑一次；``scripts`` 是模型按调用种类的剧本：``reply`` 是起草，
    ``checker`` 是发送前自检。返回（要发的群消息或 None，剧本模型）。"""

    model = ScriptedChatModel(**scripts)
    _proactive_setup(mock_context, tmp_path, model, history, **(life or {}))
    deliveries = await proactive.run_proactive(mock_context)
    if settle:
        await settle_chat_background(mock_context, model)
    return (deliveries[0] if deliveries else None), model


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["", "xiaoqing_chat", "xiaoqing_agent"])
async def test_proactive_stops_after_own_reply(mock_context, tmp_path, caplog, source) -> None:
    caplog.set_level(logging.INFO, logger="plugins.xiaoqing_chat")
    delivery, model = await _consider(
        mock_context, tmp_path, [bot_message("任务完成了", ts=_hours_ago(2), source=source)]
    )
    assert delivery is None
    assert model.calls == []
    skipped = [event.fields.get("reason_code") for event in logged_events(caplog, "proactive.skip")]
    assert "own_last_word" in skipped


@pytest.mark.asyncio
async def test_proactive_resumes_after_external_message(mock_context, tmp_path) -> None:
    history = [
        bot_message("任务完成了", ts=_hours_ago(3), source="xiaoqing_agent"),
        user_message("今天的土星环真好看", ts=_hours_ago(2)),
    ]
    delivery, model = await _consider(
        mock_context,
        tmp_path,
        history,
        reply   = ['{"decision":"send","text":"这张土星环好漂亮"}'],
        checker = ['{"decision":"send","reason":"自然"}'],
    )
    assert delivery is not None
    assert len(model.calls) == 2


@pytest.mark.asyncio
async def test_proactive_sends_after_draft_and_judge(mock_context, tmp_path) -> None:
    history = [
        user_message("有人看今天的 APOD 吗", ts=_hours_ago(3)),
        bot_message("今日天文图：土星环", ts=_hours_ago(2), source="apod"),
    ]
    delivery, model = await _consider(
        mock_context,
        tmp_path,
        history,
        reply   = ['{"decision":"send","text":"今天这张土星环也太好看了"}'],
        checker = ['{"decision":"send","reason":"自然"}'],
    )
    assert delivery is not None and delivery.target.target_id == 42
    assert delivery.message[0]["data"]["text"] == "今天这张土星环也太好看了"
    assert len(model.calls) == 2
    assert "今日天文图：土星环" in model.calls[0].messages[1]["content"]


@pytest.mark.asyncio
async def test_proactive_respects_judge_skip_and_quiet_rules(mock_context, tmp_path) -> None:
    quiet = [user_message("晚上吃啥", ts=_hours_ago(3))]
    drop = '{"decision":"drop","reason":"材料里没有二楼"}'
    delivery, model = await _consider(
        mock_context,
        tmp_path,
        quiet,
        reply=[
            '{"decision":"send","text":"食堂二楼走起"}',
            '{"decision":"send","text":"食堂二楼真的走起"}',
        ],
        checker=[drop, drop],
    )
    assert delivery is None and len(model.calls) == 4

    # 自检拦下后带着理由重写一次；第二稿过了就发第二稿
    delivery, model = await _consider(
        mock_context,
        tmp_path,
        quiet,
        reply=[
            '{"decision":"send","text":"食堂二楼走起"}',
            '{"decision":"send","text":"晚上想吃糖醋里脊"}',
        ],
        checker=[drop, '{"decision":"send","reason":"自然"}'],
    )
    assert delivery is not None
    assert delivery.message[0]["data"]["text"] == "晚上想吃糖醋里脊"
    second = model.calls[2].messages[1]["content"]
    assert "你刚才想发：食堂二楼走起" in second and "觉得不行：材料里没有二楼" in second
    assert model.calls[3].messages[1]["content"].endswith("她准备发：晚上想吃糖醋里脊")

    # 自检没给理由（或自检本身失败）：没有可改的方向，不重写
    delivery, model = await _consider(
        mock_context,
        tmp_path,
        quiet,
        reply   = ['{"decision":"send","text":"食堂二楼走起"}'],
        checker = ['{"decision":"drop"}'],
    )
    assert delivery is None and len(model.calls) == 2

    delivery, model = await _consider(
        mock_context, tmp_path, quiet, reply=['{"decision":"skip","reason":"no_natural_opening"}']
    )
    assert delivery is None and len(model.calls) == 1

    # 群里刚有人说话，或者最后一句就是她自己说的：都不开口，也不调用模型。
    for history in (
        [user_message("在吗", ts=_hours_ago(1 / 60))],
        [bot_message("有人吗", ts=_hours_ago(3))],
    ):
        delivery, model = await _consider(mock_context, tmp_path, history)
        assert delivery is None and model.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [True, None, False])
async def test_proactive_commit_records_message_and_rhythm(mock_context, tmp_path, outcome) -> None:
    history = [user_message("有人看今天的 APOD 吗", ts=_hours_ago(3))]
    text = "刚看完今天那张图，真好看"
    delivery, model = await _consider(
        mock_context,
        tmp_path,
        history,
        settle = False,
        life   = {"enable_self_timeline": True},
        reply=[json.dumps({"decision": "send", "text": text}, ensure_ascii=False)],
        checker   = ['{"decision":"send","reason":"ok"}'],
        reasoning = ["{}"],
    )
    assert delivery is not None
    state = bound_state(mock_context)
    await delivery.receipt.record(outcome)
    await delivery.receipt.record(True)  # 重复回报保持首次结算结果。
    await asyncio.gather(*state.background_tasks())

    said    = [m for m in await state.memory_store.get_async("g42") if m.role == "assistant"]
    rhythm  = state.proactive_state.get("g42")
    reading = model.calls_of("reasoning")
    if outcome is False:
        assert said == []
        assert rhythm.count == 0
        assert reading == []
    else:
        assert [render_stored_message(m) for m in said] == [text]
        assert rhythm.count == 1 and rhythm.last_ts > 0
        # 发出去的话交给近况抽取读一遍。
        assert len(reading) == 1
    await settle_chat_background(mock_context, model)


@pytest.mark.asyncio
async def test_extraction_recognises_a_reworded_follow_up_question(tmp_path) -> None:
    now = time.time()
    state, context, memory, _history = _extraction_setup(
        tmp_path,
        "g1",
        [
            user_message("下午考数电期末", ts=now - 7200),
            user_message("我下午考驾照科目二", ts=now - 7100, user_id=8, name="老王"),
        ],
    )
    from plugins.xiaoqing_chat.memory.client import push_pending

    await push_pending(
        context,
        state,
        "g1",
        bot_name = "小青",
        ops      = [
            {
                "op"  : "add",
                "kind": "person.loop",
                "text": "数电期末考试",
                "data": {"user_id": 7, "user_name": "阿梨", "due_at": now - 3600},
            },
            {
                "op"  : "add",
                "kind": "person.loop",
                "text": "驾照科目二",
                "data": {"user_id": 8, "user_name": "老王", "due_at": now - 3600},
            },
        ],
    )
    mine, theirs = memory.notes("g1", kinds=("person.loop",))
    state.memory_store.append(
        "g1", role="user", name="阿梨", user_id=7, content="刚出考场", ts=now - 5
    )
    state.memory_store.append("g1", role="assistant", name="小青", content="考得咋样", ts=now)
    output = json.dumps({"asked_loops": [mine.id, theirs.id]})
    model = ScriptedChatModel(reasoning=output)
    await _extract(state, context, "g1", state.memory_store.get("g1")[-2:], model)
    statuses = {
        loop.data["user_id"]: loop.data.get("loop", "open")
        for loop in memory.notes("g1", kinds=("person.loop",))
    }
    # 只把这段对话里说话人的话头交给模型；别人的话头不会被顺带改掉。
    assert statuses == {7: "asked", 8: "open"}
    assert "驾照科目二" not in model.calls[0].messages[1]["content"]


@pytest.mark.asyncio
async def test_extraction_ignores_canned_fallbacks_but_reads_subjectless_status(tmp_path) -> None:
    state, context, _memory, _history = _extraction_setup(tmp_path, "g1", [])
    model = ScriptedChatModel(
        reasoning=json.dumps({"self": [{"kind": "state", "text": "刚下课", "hours": 1}]})
    )
    assert not await _extract(
        state,
        context,
        "g1",
        [user_message("在干嘛", ts=NOW - 5), bot_message("等我一下，脑子突然卡壳了", ts=NOW)],
        model,
    )
    assert await _extract(
        state,
        context,
        "g1",
        [user_message("在干嘛", ts=NOW - 5), bot_message("刚下课，正往食堂冲", ts=NOW)],
        model,
    )
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_proactive_backs_off_after_a_skip(mock_context, tmp_path) -> None:
    base = time.time()
    quiet = [user_message("晚上吃啥", ts=base - 3 * 3600)]
    model = ScriptedChatModel(reply='{"decision":"skip","reason":"no_natural_opening"}')
    _proactive_setup(mock_context, tmp_path, model, quiet)
    clock = [base]
    # 时间是边界：三次检查依次发生在 0、10 分钟后、3 小时后。
    with patch.object(time, "time", side_effect=lambda: clock[0]):
        for offset in (0, 600, 3 * 3600):
            clock[0] = base + offset
            await proactive.run_proactive(mock_context)
    # 第二次在两小时内被跳过，第三次才再问模型。
    assert len(model.calls) == 2
    await settle_chat_background(mock_context, model)


@pytest.mark.parametrize(
    ("text", "asked"),
    [
        ("小青，你还没说你几点起", True),
        ("小青和阿夏，你俩谁更早起", True),
        ("你一般什么时候有空", True),
        ("小青你明天几点的课", True),
        ("明天早八的课你去吗", False),
        ("今天食堂的饺子还行", False),
    ],
)
def test_routine_is_given_as_her_own_schedule_only_when_asked(text, asked) -> None:
    from plugins.xiaoqing_chat.life.schedule import asks_about_routine, routine_block

    assert asks_about_routine(text) is asked
    block = routine_block(["周一至周五 00:30-07:20 睡觉（睡）", "每天 17:30-18:30 吃晚饭"])
    assert "- 周一至周五 00:30-07:20 睡觉" in block and "（睡）" not in block
    assert "- 每天 17:30-18:30 吃晚饭" in block
    assert routine_block([]) == ""
