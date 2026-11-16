"""角色自己的一天、全局心境、分句气泡、气泡间停顿、近况抽取的限频与私聊范围、兜底回复。"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

import tests.helpers.xiaoqing_chat_test_support as _fixture_support
from plugins.xiaoqing_chat import handlers
from plugins.xiaoqing_chat.config.config import (
    PersonalityConfig,
    ResponsePostProcessConfig,
    XiaoQingChatConfig,
)
from plugins.xiaoqing_chat.life import daily_life
from plugins.xiaoqing_chat.life import extract as life_extract
from plugins.xiaoqing_chat.life.daily_life import (
    DailyLifeStore,
    DayEvent,
    DayRecord,
    ensure_today,
    past_block,
    refers_to_past,
    today_block,
)
from plugins.xiaoqing_chat.life.self_timeline import new_entry
from plugins.xiaoqing_chat.llm.postprocess import process_llm_response
from plugins.xiaoqing_chat.persona import PersonaCard
from plugins.xiaoqing_chat.runtime_state import ChatRuntimeState
from plugins.xiaoqing_chat.store_binding import bound_state
from tests.helpers.xiaoqing_chat_memory_test_support import (
    MemoryHarness,
    chat_context,
    put_history,
    self_texts,
)
from tests.helpers.xiaoqing_chat_test_support import (
    ScriptedChatModel,
    ack_reply_delivery,
    bot_message,
    chat_message_event,
    scripted_chat_context,
    settle_chat_background,
    user_message,
)

mock_context = _fixture_support.mock_context

# 时间都按 Core 配置的时区理解，测试里同样带上时区。
ZONE = ZoneInfo("Asia/Shanghai")
MOMENT = datetime(2026, 10, 28, 15, 0, tzinfo=ZONE)


@pytest.fixture(autouse=True)
def _fresh_module_state():
    life_extract._last_state_read.clear()
    daily_life._last_failure.clear()
    handlers._last_mood_check_at = 0.0
    yield
    life_extract._last_state_read.clear()
    daily_life._last_failure.clear()
    handlers._last_mood_check_at = 0.0


# ---------------------------------------------------------------- 今天的生活


def _record(day: str = "2026-10-28") -> DayRecord:
    return DayRecord(
        day    = day,
        events = (
            DayEvent(8 * 60 + 10, "早八差点迟到"),
            DayEvent(12 * 60, "食堂糖醋里脊卖完了"),
            DayEvent(20 * 60, "天文社看了土星"),
        ),
    )


def test_today_block_only_reveals_what_already_happened() -> None:
    block = today_block(_record(), MOMENT)
    assert "早八差点迟到" in block and "糖醋里脊卖完了" in block
    assert "（" not in block.split("\n", 1)[1], "没有细节的旧记录照原样列出"
    assert "土星" not in block, "晚上的事下午还没发生"
    assert today_block(_record("2026-10-27"), MOMENT) == "", "昨天的记录不当成今天"
    assert today_block(_record(), datetime(2026, 10, 28, 7, 0, tzinfo=ZONE)) == ""


def test_past_block_combines_days_and_what_she_said() -> None:
    said = new_entry(
        chat_id = "g1",
        kind    = "state",
        text    = "在图书馆赶报告",
        hours   = 2,
        source  = "chat",
        now=datetime(2026, 10, 27, 21, 0, tzinfo=ZONE).timestamp(),
    )
    block = past_block([_record("2026-10-27")], [said], MOMENT)
    assert "10月27日（周二）" in block
    assert "早八差点迟到" in block and "说过在图书馆赶报告" in block
    assert past_block([None], [], MOMENT) == ""
    assert refers_to_past("你昨天干嘛了") and not refers_to_past("你在干嘛")


@pytest.mark.asyncio
async def test_ensure_today_generates_once_and_backs_off_after_failure(tmp_path) -> None:
    store = DailyLifeStore()
    store.bind(tmp_path)
    output = json.dumps(
        {
            "events": [
                {"at": "12:10", "text": "食堂排队排到怀疑人生"},
                {"at": "08:05", "text": "早八踩点进教室"},
                {"at": "25:00", "text": "不存在的时间"},
            ]
        },
        ensure_ascii=False,
    )
    model = ScriptedChatModel(reasoning=output)
    kwargs = {
        "store"  : store,
        "persona": "你的名字是小青。",
        "routine": ("周一至周五 08:00-11:50 上课（忙）",),
        "secrets": {"_ai": model},
        "llm"    : XiaoQingChatConfig().llm_config("reasoning"),
        "now"    : MOMENT.timestamp(),
    }
    record = await ensure_today(**kwargs)
    again  = await ensure_today(**kwargs)
    assert record is not None and again == record and len(model.calls) == 1
    assert [event.clock for event in record.events] == ["08:05", "12:10"]
    reloaded = DailyLifeStore()
    reloaded.bind(tmp_path)
    assert reloaded.get("2026-10-28") == record

    failing = DailyLifeStore()
    failing.bind(tmp_path / "other")
    broken = ScriptedChatModel(reasoning='{"events": []}')
    failing_kwargs = {**kwargs, "store": failing, "secrets": {"_ai": broken}}
    assert await ensure_today(**failing_kwargs) is None
    assert await ensure_today(**failing_kwargs) is None
    assert len(broken.calls) == 1, "失败后半小时内不再重试"


@pytest.mark.asyncio
async def test_ensure_today_without_a_model_plans_nothing_and_stores_nothing(tmp_path) -> None:
    store = DailyLifeStore()
    store.bind(tmp_path)

    record = await ensure_today(
        store   = store,
        persona = "你的名字是小青。",
        routine = (),
        secrets = {},
        llm     = XiaoQingChatConfig().llm_config("reasoning"),
        now     = MOMENT.timestamp(),
    )

    # 没有可用的模型（路由没配）：今天没有安排，也不留空记录，之后有了模型还能照常安排。
    assert record is None
    assert store.get("2026-10-28") is None
    assert not list(tmp_path.rglob("*.json"))


@pytest.mark.asyncio
async def test_a_chat_turn_arranges_today_once_when_nothing_is_planned_yet(
    mock_context, sample_private_event
) -> None:
    from plugins.xiaoqing_chat.handlers import handle_smalltalk
    from plugins.xiaoqing_chat.life.daily_life import day_key
    from plugins.xiaoqing_chat.utils.wall_clock import local_datetime

    events = [{"at": "00:00", "text": "熬夜看完一集番"}, {"at": "00:01", "text": "泡了碗面"}]
    day = json.dumps({"events": events}, ensure_ascii=False)
    model = ScriptedChatModel(reply="在呢", reasoning=day)
    scripted_chat_context(mock_context, model, {"life": {"enable_daily_life": True}})

    for text in ("你好", "在干嘛"):
        event  = chat_message_event(sample_private_event, text)
        result = await handle_smalltalk(text, event, mock_context)
        await ack_reply_delivery(result)
        await settle_chat_background(mock_context, model)

    today = bound_state(mock_context).daily_life.get(day_key(local_datetime()))
    assert today is not None
    assert [event.text for event in today.events] == ["熬夜看完一集番", "泡了碗面"]
    day_calls = [call for call in model.calls if '"events"' in call.text]
    assert len(day_calls) == 1, "今天已经安排过，第二轮不再安排"


# ---------------------------------------------------------------- 心境


def _mood_runtime(card: str) -> SimpleNamespace:
    return SimpleNamespace(
        cfg=SimpleNamespace(
            personality=PersonalityConfig(
                card                       = PersonaCard.parse(card),
                state_probability          = 1.0,
                state_min_duration_seconds = 7200,
                state_max_duration_seconds = 7200,
            )
        )
    )


def test_mood_is_one_global_state_and_turns_sleepy_at_night() -> None:
    runtime = _mood_runtime("## 心境〔心境〕\n- 现在有点调皮\n- 现在有点困，话比平时少")
    state   = ChatRuntimeState()
    first   = handlers._refresh_mood_state(runtime, state)
    assert handlers._refresh_mood_state(runtime, state) == first, "同一时刻是同一种心情"
    sleepy = handlers._refresh_mood_state(runtime, state, availability="asleep")
    assert "困" in sleepy


# ---------------------------------------------------------------- 分句气泡


def test_casual_reply_is_split_into_clause_bubbles() -> None:
    cfg = ResponsePostProcessConfig()
    # 大约每 22 字一个气泡：二三十字的一句发两条，四十多字才拆满三条，短句不拆。
    assert process_llm_response(
        "我周末经常睡到中午，几点起嘛……看我什么时候被饿醒了", cfg, bot_name="小青"
    ) == [
        "我周末经常睡到中午，几点起嘛……",
        "看我什么时候被饿醒了",
    ]
    assert process_llm_response(
        "我周末经常睡到中午，几点起嘛……看我什么时候被饿醒了，反正闹钟是叫不醒我的",
        cfg,
        bot_name="小青",
    ) == [
        "我周末经常睡到中午，几点起嘛……",
        "看我什么时候被饿醒了，反正闹钟是叫不醒我的",
    ]
    assert process_llm_response(
        "我周末经常睡到中午，几点起嘛……看我什么时候被饿醒了，反正闹钟是叫不醒我的，室友喊我也没用",
        cfg,
        bot_name="小青",
    ) == [
        "我周末经常睡到中午，几点起嘛……",
        "看我什么时候被饿醒了",
        "反正闹钟是叫不醒我的，室友喊我也没用",
    ]
    assert process_llm_response("你才胖了，懂不懂啦", cfg, bot_name="小青") == [
        "你才胖了，懂不懂啦"
    ]
    # 链接、邮箱和数字之间的逗号保持原样。
    assert process_llm_response("physlab@163.com，按学号命名就行", cfg, bot_name="小青") == [
        "physlab@163.com，按学号命名就行"
    ]
    assert process_llm_response("1，2，3都可以", cfg, bot_name="小青") == ["1，2，3都可以"]
    # 表情里的半个括号不吞掉后面的句末。
    assert process_llm_response(
        "好难过:( 不过没事！明天再说吧。周末一起去(笑)", cfg, bot_name="小青"
    ) == ["好难过:( 不过没事！", "明天再说吧。", "周末一起去(笑)"]
    # 对方提到几句话、多少字时由调用方关闭分句。
    from plugins.xiaoqing_chat.reply_generator import _allows_clause_bubbles

    assert not _allows_clause_bubbles("小青，用一句话安利一部你最喜欢的老番")
    assert not _allows_clause_bubbles("帮我写个800字的检讨书")
    assert _allows_clause_bubbles("周末几点起呀")
    assert process_llm_response(
        "我周末经常睡到中午，几点起嘛", cfg, bot_name="小青", clause_split=False
    ) == ["我周末经常睡到中午，几点起嘛"]


def test_unpaired_lenticular_brackets_copied_from_summaries_are_dropped() -> None:
    cfg = ResponsePostProcessConfig()
    # 实测原样：读图摘要里的【】被抄了半截进回复。
    assert process_llm_response("physlab2026@163.com】【。", cfg, bot_name="小青") == [
        "physlab2026@163.com。"
    ]
    assert process_llm_response("【重要】周五交报告", cfg, bot_name="小青") == [
        "【重要】周五交报告"
    ]


# ---------------------------------------------------------------- 抽取限频与私聊范围


def _state(tmp_path):
    state = ChatRuntimeState()
    state.memory_store.bind_data_dir(tmp_path)
    return state, MemoryHarness(tmp_path), tmp_path


async def _extract(setup, chat_id: str, reply: str, model) -> bool:
    state, memory, data_dir = setup
    now     = time.time()
    history = put_history(
        state, chat_id, [user_message("在干嘛", now - 5), bot_message(reply, now)]
    )
    return await life_extract.extract_life_records(
        context  = chat_context(memory, data_dir, chat_id),
        state    = state,
        life_cfg = XiaoQingChatConfig().life,
        secrets  = {"_ai": model},
        llm      = XiaoQingChatConfig().llm_config("reasoning"),
        chat_id  = chat_id,
        bot_name = "小青",
        history  = history,
        source   = "chat",
    )


@pytest.mark.asyncio
async def test_state_only_replies_are_read_at_most_every_interval(tmp_path) -> None:
    state = _state(tmp_path)
    model = ScriptedChatModel(reasoning="{}")
    assert await _extract(state, "g1", "刚下课，有点饿", model)
    assert not await _extract(state, "g1", "还在食堂排队", model), "十分钟内只读一次近况"
    assert await _extract(state, "g1", "回头查完发你", model), "承诺每次都读"
    assert await _extract(state, "g2", "刚下课", model), "限频按会话分开"
    assert len(model.calls) == 3


@pytest.mark.asyncio
async def test_private_status_stays_private_unless_shareable(tmp_path) -> None:
    state  = _state(tmp_path)
    output = json.dumps(
        {
            "self": [
                {"kind": "state", "text": "刚跟阿梨打完电话", "hours": 2, "shareable": False},
                {"kind": "state", "text": "在宿舍摸鱼", "hours": 2, "shareable": True},
            ]
        },
        ensure_ascii=False,
    )
    assert await _extract(
        state, "u7", "刚跟你打完电话，现在在宿舍摸鱼", ScriptedChatModel(reasoning=output)
    )
    memory = state[1]
    assert sorted(self_texts(memory, "u7")) == sorted(["刚跟阿梨打完电话", "在宿舍摸鱼"])
    assert self_texts(memory, "g1") == ["在宿舍摸鱼"]


# ---------------------------------------------------------------- 气泡之间真的停顿


def _bubble_turn_context(mock_context, reply: str, sent: list, on_send=None):
    """私聊里模型回一段分成几个气泡的回复；发出去的气泡记在 ``sent``（发送这条边界上的替身）。"""

    model = ScriptedChatModel(reply=reply)
    scripted_chat_context(mock_context, model, {"humanize": {"enable_typing_delay": True}})

    async def send_action(action):
        sent.append(action)
        if on_send is not None:
            on_send(len(sent))
        return True

    mock_context.send_action = send_action
    return model


@pytest.mark.asyncio
@pytest.mark.parametrize("source,bypass", [("upstream_ws", True), ("inbound_ws", False)])
async def test_intermediate_bubbles_go_out_directly_on_upstream_ws(
    mock_context, sample_private_event, source, bypass
) -> None:
    from core.interfaces import ACTION_BYPASS_SINK_KEY, EVENT_SOURCE_KEY
    from plugins.xiaoqing_chat.handlers import handle_smalltalk

    sent: list = []
    model      = _bubble_turn_context(mock_context, "一\n\n二\n\n三", sent)
    event      = chat_message_event(sample_private_event, "你好", **{EVENT_SOURCE_KEY: source})
    # 时间是边界：气泡之间的停顿换成立即返回的计数替身。
    with patch.object(asyncio, "sleep", AsyncMock()) as pause:
        result = await handle_smalltalk("你好", event, mock_context)

    # 前两个气泡直接发出，最后一个作为本次处理的结果交给 Core。
    assert [bubble["data"]["text"] for bubble in result] == ["三"]
    assert [action["params"]["message"][0]["data"]["text"] for action in sent] == ["一", "二"]
    assert all(bool(action.get(ACTION_BYPASS_SINK_KEY)) is bypass for action in sent)
    assert pause.await_count == 2, "明确召唤时气泡之间也停顿"
    await ack_reply_delivery(result)
    await settle_chat_background(mock_context, model)


@pytest.mark.asyncio
async def test_turn_going_stale_after_a_sent_bubble_records_only_what_went_out(
    mock_context, sample_private_event
) -> None:
    from core.interfaces import EVENT_SOURCE_KEY
    from plugins.xiaoqing_chat.handlers import handle_smalltalk
    from plugins.xiaoqing_chat.memory.local_ids import MAX_REPLY_RECENT_MESSAGES

    sent: list = []

    def friend_floods_chat(sent_count: int) -> None:
        # 第二个气泡发出时，对方又连着发来很多条：要接的那句已经太远，后面的气泡不再发。
        if sent_count == 2:
            for index in range(MAX_REPLY_RECENT_MESSAGES):
                bound_state(mock_context).memory_store.append(
                    "u12345", role="user", name="TestUser", user_id=12345, content=f"又一句{index}"
                )

    model = _bubble_turn_context(mock_context, "一\n\n二\n\n三\n\n四", sent, friend_floods_chat)
    event = chat_message_event(sample_private_event, "你好", **{EVENT_SOURCE_KEY: "upstream_ws"})
    with patch.object(asyncio, "sleep", AsyncMock()):
        result = await handle_smalltalk("你好", event, mock_context)

    assert result == []
    assert len(sent) == 2
    # 聊天记录只记发出去的两个气泡，没发出去的最后一个气泡不算她说过。
    went_out  = [action["params"]["message"][0]["data"]["text"] for action in sent]
    history   = await bound_state(mock_context).memory_store.get_async("u12345")
    assistant = [message.content for message in history if message.role == "assistant"]
    assert assistant == ["\n\n".join(went_out)]
    assert "四" not in assistant[0]
    await settle_chat_background(mock_context, model)


@pytest.mark.asyncio
async def test_near_duplicate_status_is_not_recorded_twice(tmp_path) -> None:
    state  = _state(tmp_path)
    output = json.dumps(
        {
            "self": [
                {"kind": "state", "text": "刚摸鱼", "hours": 2},
                {"kind": "state", "text": "现在在摸鱼", "hours": 2},
            ]
        },
        ensure_ascii=False,
    )
    assert await _extract(
        state, "g1", "刚摸鱼呢，现在也还在摸鱼", ScriptedChatModel(reasoning=output)
    )
    assert self_texts(state[1], "g1") == ["刚摸鱼"]


def test_no_image_this_turn_never_falls_back_to_having_seen_it() -> None:
    from plugins.xiaoqing_chat.reply_fallbacks import MEDIA_SEEN_FALLBACKS, _forced_fallback_texts

    plan = SimpleNamespace(event={}, text="小青，抽卡那张图你没看到吧", request_id="r1")
    texts, _step = _forced_fallback_texts(plan, "media_grounding")
    assert not set(texts) & set(MEDIA_SEEN_FALLBACKS)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("你怎么不理我", "在呢在呢"),
        ("小青在吗", "在呢在呢"),
        ("这道题为什么选C", "这个我真拿不准，不瞎说了"),
        ("你想我们周末去哪玩？", "这个我真拿不准，不瞎说了"),
        ("你知道他在干嘛吗", "这个我真拿不准，不瞎说了"),
    ],
)
def test_social_turns_fall_back_to_presence_instead_of_unsure(text: str, expected: str) -> None:
    from plugins.xiaoqing_chat.reply_fallbacks import _forced_fallback_texts

    plan = SimpleNamespace(event={}, text=text, request_id="r1")
    texts, _step = _forced_fallback_texts(plan, "context_grounding")
    assert texts[0] == expected


def test_event_detail_is_kept_shown_and_survives_reload(tmp_path) -> None:
    events = daily_life._parse_events(
        [
            {
                "at"    : "12:10",
                "text"  : "午饭打了糖醋里脊",
                "detail": "排到我正好是最后一份  阿姨多给了半勺汁",
            },
            {"at": "20:00", "text": "天文社看了土星", "detail": "环看得很清楚"},
            {"at": "08:05", "text": "早八踩点进教室"},
        ]
    )
    assert [e.detail for e in events] == [
        "",
        "排到我正好是最后一份 阿姨多给了半勺汁",
        "环看得很清楚",
    ]
    store = DailyLifeStore()
    store.bind(tmp_path)
    store.save(DayRecord(day="2026-10-28", events=events, generated_at=1.0), now=MOMENT)
    again = DailyLifeStore()
    again.bind(tmp_path)
    record = again.get("2026-10-28")
    assert record is not None and record.events == events
    block = today_block(record, MOMENT)
    assert "- 12:10 午饭打了糖醋里脊（排到我正好是最后一份 阿姨多给了半勺汁）" in block
    assert "- 08:05 早八踩点进教室\n" in block and "土星" not in block
    assert today_block(record, MOMENT, header="今天的事：").startswith("今天的事：\n- 08:05")


def test_generation_sees_the_last_few_days_so_today_is_not_the_same_again() -> None:
    older = DayRecord(day="2026-10-26", events=(DayEvent(600, "课上抄漏了一个负号"),))
    messages = daily_life.build_messages(
        persona = "p",
        routine = ["08:00-12:00 上课"],
        moment  = MOMENT,
        earlier = [_record("2026-10-27"), None, older],
    )
    user = messages[1]["content"]
    assert "前几天发生过的事：\n- 10-27：早八差点迟到；食堂糖醋里脊卖完了；天文社看了土星" in user
    assert "- 10-26：课上抄漏了一个负号" in user
    blank = daily_life.build_messages(persona="p", routine=[], moment=MOMENT)[1]["content"]
    assert "前几天发生过的事：（没有记录）" in blank
