"""角色自己的一天、全局心境、分句气泡、线下邀约边界、摘要标注和抽取限频。"""

from __future__ import annotations

import json
import time
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

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
from plugins.xiaoqing_chat.life.self_timeline import SelfTimelineStore, new_entry
from plugins.xiaoqing_chat.llm.postprocess import process_llm_response
from plugins.xiaoqing_chat.memory.memory import StoredMessage
from plugins.xiaoqing_chat.memory.mention_policy import AVOID, MENTION, TONE, classify
from plugins.xiaoqing_chat.persona import PersonaCard
from plugins.xiaoqing_chat.runtime_state import ChatRuntimeState

# 时间都按 Core 配置的时区理解，测试里同样带上时区。
ZONE = ZoneInfo("Asia/Shanghai")
MOMENT = datetime(2026, 10, 28, 15, 0, tzinfo=ZONE)


@pytest.fixture(autouse=True)
def _fresh_module_state():
    life_extract._last_state_read.clear()
    daily_life._last_failure.clear()
    handlers._last_mood_check.clear()
    yield
    life_extract._last_state_read.clear()
    daily_life._last_failure.clear()
    handlers._last_mood_check.clear()


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
    model = AsyncMock(return_value=output)
    kwargs = {
        "store"  : store,
        "persona": "你的名字是小青。",
        "routine": ("周一至周五 08:00-11:50 上课（忙）",),
        "secrets": {"_ai": object()},
        "llm"    : XiaoQingChatConfig().llm_config("reasoning"),
        "now"    : MOMENT.timestamp(),
    }
    with patch.object(daily_life, "chat_completions", model):
        record = await ensure_today(**kwargs)
        again  = await ensure_today(**kwargs)
    assert record is not None and again == record and model.await_count == 1
    assert [event.clock for event in record.events] == ["08:05", "12:10"]
    reloaded = DailyLifeStore()
    reloaded.bind(tmp_path)
    assert reloaded.get("2026-10-28") == record

    failing = DailyLifeStore()
    failing.bind(tmp_path / "other")
    broken = AsyncMock(return_value='{"events": []}')
    with patch.object(daily_life, "chat_completions", broken):
        assert await ensure_today(**{**kwargs, "store": failing}) is None
        assert await ensure_today(**{**kwargs, "store": failing}) is None
    assert broken.await_count == 1, "失败后半小时内不再重试"


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


def test_mood_is_shared_across_chats_and_turns_sleepy_at_night() -> None:
    runtime = _mood_runtime("## 心境〔心境〕\n- 现在有点调皮\n- 现在有点困，话比平时少")
    state   = ChatRuntimeState()
    first   = handlers._refresh_mood_state(runtime, state, "g1")
    assert handlers._refresh_mood_state(runtime, state, "u7") == first, "同一时刻各会话同一种心情"
    sleepy = handlers._refresh_mood_state(runtime, state, "g1", availability="asleep")
    assert "困" in sleepy


# ---------------------------------------------------------------- 分句气泡


def test_casual_reply_is_split_into_clause_bubbles() -> None:
    cfg = ResponsePostProcessConfig()
    assert process_llm_response(
        "我周末经常睡到中午，几点起嘛……看我什么时候被饿醒了", cfg, bot_name="小青"
    ) == [
        "我周末经常睡到中午",
        "几点起嘛……",
        "看我什么时候被饿醒了",
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


# ---------------------------------------------------------------- 摘要标注


def test_mention_label_from_summary_overrides_keywords() -> None:
    now = time.time()
    assert (
        classify(
            "聊了分手后的番剧推荐",
            {"type": "topic_summary", "mention": "normal", "updated_at": now},
        )
        == MENTION
    )
    assert (
        classify("几个人在争论", {"type": "topic_summary", "mention": "avoid", "updated_at": now})
        == AVOID
    )
    assert (
        classify("阿梨说起奶奶", {"type": "topic_summary", "mention": "tone", "updated_at": now})
        == TONE
    )
    assert classify("阿梨和老王吵了一架", {"type": "topic_summary", "updated_at": now}) == AVOID


# ---------------------------------------------------------------- 抽取限频与私聊范围


def _user(text: str, ts: float) -> StoredMessage:
    return StoredMessage(role="user", name="阿梨", ts=ts, user_id=7, content=text)


def _bot(text: str, ts: float) -> StoredMessage:
    return StoredMessage(
        role="assistant", name="小青", ts=ts, content=text, source_plugin="xiaoqing_chat"
    )


def _state(tmp_path) -> ChatRuntimeState:
    state = ChatRuntimeState()
    state.self_timeline.bind(tmp_path)
    state.open_loops.bind(tmp_path)
    return state


async def _extract(state, chat_id: str, reply: str, model) -> bool:
    now = time.time()
    with patch.object(life_extract, "chat_completions", model):
        return await life_extract.extract_life_records(
            state    = state,
            life_cfg = XiaoQingChatConfig().life,
            secrets  = {"_ai": object()},
            llm      = XiaoQingChatConfig().llm_config("reasoning"),
            chat_id  = chat_id,
            bot_name = "小青",
            history  = [_user("在干嘛", now - 5), _bot(reply, now)],
            source   = "chat",
        )


@pytest.mark.asyncio
async def test_state_only_replies_are_read_at_most_every_interval(tmp_path) -> None:
    state = _state(tmp_path)
    model = AsyncMock(return_value="{}")
    assert await _extract(state, "g1", "刚下课，有点饿", model)
    assert not await _extract(state, "g1", "还在食堂排队", model), "十分钟内只读一次近况"
    assert await _extract(state, "g1", "回头查完发你", model), "承诺每次都读"
    assert await _extract(state, "g2", "刚下课", model), "限频按会话分开"
    assert model.await_count == 3


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
        state, "u7", "刚跟你打完电话，现在在宿舍摸鱼", AsyncMock(return_value=output)
    )
    assert [entry.text for entry in state.self_timeline.visible("u7")] == [
        "刚跟阿梨打完电话",
        "在宿舍摸鱼",
    ]
    assert [entry.text for entry in state.self_timeline.visible("g1")] == ["在宿舍摸鱼"]


def test_self_timeline_keeps_a_week_of_history(tmp_path) -> None:
    store = SelfTimelineStore()
    store.bind(tmp_path)
    now = time.time()
    old = new_entry(
        chat_id = "g1",
        kind    = "state",
        text    = "三天前在图书馆",
        hours   = 1,
        source  = "chat",
        now     = now - 3 * 86400,
    )
    stale = new_entry(
        chat_id="g1", kind="state", text="十天前的事", hours=1, source="chat", now=now - 10 * 86400
    )
    store.apply(added=[old, stale], chat_id="g1", max_entries=50, now=now)
    store.apply(chat_id="g1", max_entries=50, now=now)
    assert [entry.text for entry in store.history("g2", since=now - 5 * 86400)] == [
        "三天前在图书馆"
    ]
    assert store.visible("g1", now=now) == [], "过期的不再当作近况"


@pytest.mark.asyncio
@pytest.mark.parametrize("min_messages,called", [(8, True), (20, False)])
async def test_person_facts_threshold_is_configurable(tmp_path, min_messages, called) -> None:
    from plugins.xiaoqing_chat.memory import knowledge_extract
    from plugins.xiaoqing_chat.memory.memory_db import MemoryDB

    history = [_user(f"第{index}句", float(index + 1)) for index in range(9)]
    model = AsyncMock(return_value='{"facts": []}')
    with patch.object(knowledge_extract, "chat_completions", model):
        await knowledge_extract.maybe_extract_person_facts(
            data_dir     = tmp_path,
            secrets      = {"_ai": object()},
            memory_db    = MemoryDB(),
            chat_id      = "u7",
            history      = history,
            llm          = XiaoQingChatConfig().llm_config("reasoning"),
            min_messages = min_messages,
        )
    assert model.await_count == (1 if called else 0)


# ---------------------------------------------------------------- 气泡之间真的停顿


def _finalization(source: str, sent: list):
    from plugins.xiaoqing_chat.chat_runtime import ChatRuntime

    async def send_action(action):
        sent.append(action)
        return True

    return SimpleNamespace(
        event={"_source": source, "user_id": 7, "group_id": None},
        hctx=SimpleNamespace(chat_id="u7", runtime=ChatRuntime(XiaoQingChatConfig(), [])),
        context=SimpleNamespace(send_action=send_action),
        generated=SimpleNamespace(local_id="u1"),
    )


def _batches(*texts: str) -> list:
    return [[{"type": "text", "data": {"text": text}}] for text in texts]


@pytest.mark.asyncio
@pytest.mark.parametrize("source,bypass", [("upstream_ws", True), ("inbound_ws", False)])
async def test_intermediate_bubbles_go_out_directly_on_upstream_ws(source, bypass) -> None:
    from core.interfaces import ACTION_BYPASS_SINK_KEY
    from plugins.xiaoqing_chat import turn_delivery

    sent: list   = []
    finalization = _finalization(source, sent)
    batches      = _batches("一", "二", "三")
    bubbles      = turn_delivery._ReplyBubbles(finalization, (), batches)
    with (
        patch.object(turn_delivery, "_drop_stale_generated_turn", return_value=False),
        patch.object(turn_delivery.asyncio, "sleep", AsyncMock()) as pause,
    ):
        assert await turn_delivery._send_smalltalk_intermediate_batches(
            finalization, batches, bubbles, apply_humanize=False
        )
    assert len(sent) == 2
    assert all(bool(action.get(ACTION_BYPASS_SINK_KEY)) is bypass for action in sent)
    assert pause.await_count == 2, "明确召唤时气泡之间也停顿"


@pytest.mark.asyncio
async def test_turn_going_stale_after_a_sent_bubble_records_only_what_went_out() -> None:
    from plugins.xiaoqing_chat import turn_delivery
    from plugins.xiaoqing_chat.message_parts import build_text_message_parts

    sent: list   = []
    finalization = _finalization("upstream_ws", sent)
    batches      = _batches("一", "二", "三", "四")
    bubbles      = turn_delivery._ReplyBubbles(finalization, (), batches)
    with (
        patch.object(turn_delivery, "_drop_stale_generated_turn", side_effect=[False, False, True]),
        patch.object(turn_delivery.asyncio, "sleep", AsyncMock()),
        patch.object(turn_delivery, "log_step"),
        patch.object(turn_delivery, "_commit_smalltalk_delivery", AsyncMock()) as commit,
        patch.object(turn_delivery, "_rollback_smalltalk_delivery", AsyncMock()) as rollback,
    ):
        assert not await turn_delivery._send_smalltalk_intermediate_batches(
            finalization, batches, bubbles, apply_humanize=False
        )
    assert len(sent) == 2
    rollback.assert_not_awaited()
    # 聊天记录只记发出去的两个气泡，没发的“三”“四”不算她说过。
    commit.assert_awaited_once_with(finalization, build_text_message_parts("一\n\n二"), ())


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
    assert await _extract(state, "g1", "刚摸鱼呢，现在也还在摸鱼", AsyncMock(return_value=output))
    assert [entry.text for entry in state.self_timeline.visible("g1")] == ["刚摸鱼"]


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
