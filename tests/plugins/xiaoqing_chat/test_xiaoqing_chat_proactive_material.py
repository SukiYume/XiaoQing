"""主动开口手里的东西：推送全文和配图、当天小事的细节、前几次说过的话和有没有人接、这个群聊到才去搜的事。

整段流程从定时任务的处理函数进入（现场搭建在 ``xiaoqing_chat_test_support`` 里）：真实的聊天记录、
推送记录、统一记忆和投递回执，边界上只有剧本模型、网上生活能力和假时钟。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image

import tests.helpers.xiaoqing_chat_test_support as _fixture_support
from core.ai import AICompletionResult
from plugins.xiaoqing_chat import main as chat_main
from plugins.xiaoqing_chat.config.config import XiaoQingChatConfig
from plugins.xiaoqing_chat.life import online, proactive, push_log
from plugins.xiaoqing_chat.life.daily_life import DayEvent, DayRecord, day_key
from plugins.xiaoqing_chat.life.proactive_state import ProactiveRecord
from plugins.xiaoqing_chat.life.push_log import Push, PushLogStore
from plugins.xiaoqing_chat.llm.gateway import LLMError
from plugins.xiaoqing_chat.memory.memory import StoredMessage
from plugins.xiaoqing_chat.runtime_state import ChatRuntime, ChatRuntimeState
from plugins.xiaoqing_chat.utils.wall_clock import local_datetime
from plugins.xiaoqing_memory.model import Note
from plugins.xiaoqing_memory.scopes import group_subject
from tests.helpers.xiaoqing_chat_memory_test_support import MemoryHarness
from tests.helpers.xiaoqing_chat_test_support import (
    HOUR,
    NOW,
    bot_message,
    commit,
    group_run,
)

mock_context        = _fixture_support.mock_context
clock               = _fixture_support.clock
no_swallowed_errors = _fixture_support.no_swallowed_errors


def _user(text: str, ts: float, name: str = "阿梨") -> StoredMessage:
    return StoredMessage(role="user", name=name, ts=ts, user_id=77, content=text)


def _runtime() -> ChatRuntime:
    return ChatRuntime(XiaoQingChatConfig.model_validate({"proactive": {"enabled": True}}), [])


class _Vision:
    """识图路由的替身（经 ``secrets["_ai"]`` 递进去）：记下每次调用，答一段文字或抛错。"""

    def __init__(self, answer: str | Exception) -> None:
        self.answer                      = answer
        self.calls: list[dict[str, Any]] = []

    async def complete(self, route: str, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        self.calls.append({"route": route, "messages": messages, **kwargs})
        if isinstance(self.answer, Exception):
            raise self.answer
        return AICompletionResult(
            response={"choices": [{"message": {"content": self.answer}, "finish_reason": "stop"}]},
            profile="vision",
            provider="scripted",
            model="scripted-vision",
            finish_reason="stop",
            attempts=1,
        )


# ---- 推送记录 ---------------------------------------------------------------------------------


def test_image_paths_take_only_local_files() -> None:
    message = [
        {"type": "image", "data": {"file": "file:///C:/data/apod/a%20b.jpg"}},
        {"type": "text", "data": {"text": "标题"}},
        {"type": "image", "data": {"file": "file:///tmp/quake.jpg"}},
        {"type": "image", "data": {"file": "https://example.com/x.jpg"}},
        {"type": "image", "data": {"file": "base64://AAAA"}},
    ]
    assert push_log.image_paths(message) == ("C:/data/apod/a b.jpg", "/tmp/quake.jpg")
    assert push_log.image_paths("纯文本") == () and push_log.image_paths(None) == ()


def test_push_log_keeps_recent_full_text_per_chat_and_survives_reload(tmp_path) -> None:
    store = PushLogStore()
    store.bind(tmp_path)
    store.append("g1", Push(ts=NOW - 40 * HOUR, source="apod", local_id="m0", text="前天的图"))
    for n in range(1, 9):
        store.append(
            "g1", Push(ts=NOW - (9 - n) * 60, source="codex", local_id=f"m{n}", text="长文" * 2000)
        )
    store.append("g2", Push(ts=NOW, source="github", local_id="m1", text="别的群"))
    again = PushLogStore()
    again.bind(tmp_path)
    kept = again.since("g1", 0.0)
    assert [p.local_id for p in kept] == [f"m{n}" for n in range(3, 9)]  # 每个会话留最近 6 条
    assert len(kept[0].text) == push_log.MAX_TEXT_CHARS
    assert [p.local_id for p in again.since("g1", NOW - 3 * 60)] == ["m7", "m8"]
    assert [p.text for p in again.since("g2", 0.0)] == ["别的群"]
    again.set_image_note("g1", kept[-1], "图上是一片星系")
    reloaded = PushLogStore()
    reloaded.bind(tmp_path)
    assert reloaded.since("g1", 0.0)[-1].image_note == "图上是一片星系"


@pytest.mark.asyncio
async def test_looking_at_a_push_image_goes_through_the_vision_route(tmp_path) -> None:
    picture = tmp_path / "apod.png"
    Image.new("RGB", (2400, 1200), "navy").save(picture)
    push = Push(ts=NOW, source="apod", local_id="m1", text="NGC 206", images=(str(picture),))
    llm    = _runtime().cfg.llm_config("vision")
    vision = _Vision("画面是一片 斜着的星系\n右上偏亮")
    note = await push_log.look_at_images(push, secrets={"_ai": vision}, llm=llm)
    assert note == "画面是一片 斜着的星系 右上偏亮"
    (call,) = vision.calls
    assert call["route"] == "vision" and call["required_modalities"] == ("text", "image")
    _, image = call["messages"][0]["content"]
    assert image["image_url"]["url"].startswith("data:image/jpeg;base64,")
    # 图没了、不是图片或模型不可用：当作没有图
    unused = _Vision("不该被问")
    for broken in (
        Push(ts=NOW, source="apod", local_id="m2", text="x", images=(str(tmp_path / "gone.jpg"),)),
        Push(ts=NOW, source="apod", local_id="m3", text="x"),
    ):
        assert await push_log.look_at_images(broken, secrets={"_ai": unused}, llm=llm) == ""
    assert unused.calls == []
    down = _Vision(LLMError("down"))
    assert await push_log.look_at_images(push, secrets={"_ai": down}, llm=llm) == ""


@pytest.mark.asyncio
async def test_push_notices_give_full_text_and_what_the_picture_shows(
    mock_context, tmp_path
) -> None:
    picture = tmp_path / "apod.png"
    Image.new("RGB", (64, 48), "navy").save(picture)

    def pushes(state: ChatRuntimeState) -> None:
        state.push_log.append(
            "g42", Push(ts=NOW - 8 * HOUR, source="github", local_id="m1", text="昨晚的趋势")
        )
        state.push_log.append(
            "g42",
            Push(
                ts       = NOW - 3 * HOUR,
                source   = "apod",
                local_id = "m2",
                text     = "NGC 206 and the Star Clouds of Andromeda\n" + "正文" * 300,
                images   = (str(picture),),
            ),
        )

    history = [
        _user("晚上吃啥", NOW - 9 * HOUR),
        bot_message("NGC 206…（内容较长，只记下开头）", NOW - 3 * HOUR, source="apod"),
    ]
    run = await group_run(
        mock_context,
        tmp_path,
        history,
        reply                    = '{"decision":"skip"}',
        vision                   = "一片斜着的星系，右上偏亮",
        seed                     = pushes,
        retry_after_skip_minutes = 0,
    )
    material = run.draft_material()
    assert "（apod 推送）NGC 206 and the Star Clouds of Andromeda" in material
    assert "昨晚的趋势" not in material  # 六小时前的不算
    assert (
        "正文正文" in material and material.count("只记下开头") == 1
    )  # 推送给全文，只有聊天记录里是截断的
    assert "配图里看到的：一片斜着的星系，右上偏亮" in material
    assert len(run.model.calls_of("vision")) == 1

    # 下一次起草：同样的推送材料，图不再看一遍，看到的内容存在推送记录里。
    assert await chat_main.scheduled_proactive(mock_context) == []
    assert run.model.calls_of("reply")[1].messages[1]["content"] == material
    assert len(run.model.calls_of("vision")) == 1, "图只看一遍，看到的内容存进推送记录"
    assert (
        run.state.push_log.since("g42", NOW - 4 * HOUR)[-1].image_note == "一片斜着的星系，右上偏亮"
    )

    # 她接过话之后的推送才算
    spoken = [
        *history[:1],
        history[1],
        bot_message("这张好看", NOW - 2.5 * HOUR),
        _user("确实", NOW - 2 * HOUR),
    ]
    run = await group_run(
        mock_context, tmp_path, spoken, reply='{"decision":"skip"}', vision="不该被问", seed=pushes
    )
    assert "（apod 推送）" not in run.draft_material() and run.model.calls_of("vision") == []


# ---- 前几次主动说的话 -------------------------------------------------------------------------


def test_feedback_block_shows_who_picked_up_each_earlier_line() -> None:
    record = ProactiveRecord(
        recent=(
            (NOW - 5 * 86400, "太久以前的"),
            (NOW - 50 * HOUR, "聊天记录翻不到的那句"),
            (NOW - 26 * HOUR, "昨天分享的歌单"),
            (NOW - 4 * HOUR, "今天说的作业"),
        )
    )
    history = [
        _user("早", NOW - 30 * HOUR),
        bot_message("昨天分享的歌单", NOW - 26 * HOUR),
        _user("隔了半天才说话", NOW - 20 * HOUR),
        bot_message("今天说的作业", NOW - 4 * HOUR),
        _user(
            "都有什么作业呀，说来听听，是不是又拖到最后一天才写的那种", NOW - 4 * HOUR + 30, "鱼丸"
        ),
    ]
    block = proactive.feedback_block(record, history, now=NOW)
    lines = block.splitlines()
    assert lines[0] == proactive._FEEDBACK_HEADER and len(lines) == 4
    assert "太久以前的" not in block
    assert lines[1].endswith("聊天记录翻不到的那句")  # 记录已经翻不到：只列原话
    assert lines[2].endswith("昨天分享的歌单（没人接）")
    assert "今天说的作业（鱼丸接了：都有什么作业呀" in lines[3] and len(lines[3]) < 90
    assert proactive.feedback_block(ProactiveRecord(), history, now=NOW) == ""


def test_spoken_lines_are_recorded_and_capped(tmp_path) -> None:
    state = ChatRuntimeState()
    state.proactive_state.bind(tmp_path)
    for n in range(7):
        state.proactive_state.record("g1", ts=NOW + n, day="d", text=f"第{n}句" + "长" * 300)
    state.proactive_state.record("g1", ts=NOW + 9, day="d")  # 没带原话的不记
    again = ChatRuntimeState()
    again.proactive_state.bind(tmp_path)
    recent = again.proactive_state.get("g1").recent
    assert [text[:3] for _, text in recent] == ["第2句", "第3句", "第4句", "第5句", "第6句"]
    assert all(len(text) == 200 for _, text in recent) and recent[-1][0] == NOW + 6


# ---- 候选 -------------------------------------------------------------------------------------


class _Life:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows                           = rows
        self.requests: list[dict[str, Any]] = []

    async def turn_context(self, payload: dict[str, Any]) -> Any:
        self.requests.append(payload)
        return {"version": 1, "share_candidates": self.rows}


@pytest.mark.asyncio
async def test_share_candidates_carry_the_groups_topics_and_the_heard_here_mark() -> None:
    rows = [
        {"episode_id": f"e{n}", "line": "讲" * 900, "topics": ["猫"], "heard_here": n == 0}
        for n in range(8)
    ]
    life = _Life(rows)
    context = SimpleNamespace(capabilities=SimpleNamespace(life_context=life))
    topics = [{"topic": f"话题{n}", "keywords": []} for n in range(25)]
    found  = await online.fetch_share_candidates(
        context, _runtime(), exclude_ids=["e9"], topics=topics
    )
    assert [c["episode_id"] for c in found] == [f"e{n}" for n in range(6)]
    assert [c["heard_here"] for c in found[:2]] == [True, False] and len(found[0]["line"]) == 700
    request = life.requests[0]
    assert request["mode"] == "proactive" and request["is_private"] is False
    assert len(request["topics"]) == 20 and request["exclude_ids"] == ["e9"]
    # 私聊不带话题词
    await online.fetch_share_candidates(context, _runtime(), exclude_ids=[])
    assert "topics" not in life.requests[1] and "is_private" not in life.requests[1]


# ---- 起草时她看到的全部材料 -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_group_draft_sees_everything_she_has_and_the_line_is_remembered(
    mock_context, tmp_path
) -> None:
    history = [
        *[_user(f"第{n}句群聊", NOW - 30 * HOUR + n) for n in range(30)],
        bot_message("昨天分享的歌单", NOW - 26 * HOUR),
        _user("NapCat 删库了", NOW - 5 * HOUR, "鱼丸"),
        bot_message("APOD 开头…（内容较长，只记下开头）", NOW - 2 * HOUR, source="apod"),
    ]
    moment  = local_datetime(NOW)
    minute  = moment.hour * 60 + moment.minute
    harness = MemoryHarness(tmp_path / "memory")
    harness.store.put_notes(
        "g42",
        [
            Note(
                id          = "ngroup",
                kind        = "group.fact",
                scope       = "g42",
                subject     = group_subject("g42"),
                text        = "这个群常聊的：QQ 机器人、显卡",
                created_at  = NOW,
                observed_at = NOW,
            )
        ],
    )
    topics = [{"topic": "NapCat 删库", "keywords": ["NapCat"]}]
    harness.store.update_scope(
        "g42", topics=[{**topic, "usage": "normal", "at": NOW} for topic in topics]
    )

    def seed(state: ChatRuntimeState) -> None:
        state.daily_life.save(
            DayRecord(
                day    = day_key(moment),
                events = (
                    DayEvent(max(0, minute - 30), "午饭打了糖醋里脊", "排到我正好是最后一份"),
                    DayEvent(min(1439, minute + 60), "晚上去社团", "还没发生"),
                ),
            ),
            now=moment,
        )
        state.push_log.append(
            "g42",
            Push(ts=NOW - 2 * HOUR, source="apod", local_id="m9", text="APOD 全文" + "x" * 500),
        )
        state.proactive_state.record(
            "g42", ts=NOW - 26 * HOUR, day="2020-01-01", text="昨天分享的歌单"
        )

    life = _Life(
        [
            {
                "episode_id": "e7",
                "line"      : "今天 07:44 在B站搜东西，点开了「后路」（听群里在聊「NapCat 删库」才去搜的），评论区有一句：「给AI配刹车」",
                "topics"    : ["QQ机器人"],
                "heard_here": True,
            }
        ]
    )
    draft = "鱼丸 上次说的 NapCat，我搜到个留后路的 https://b23.tv/x"
    run   = await group_run(
        mock_context,
        tmp_path,
        history,
        reply   = '{"decision":"send","text":"' + draft + '","life_ref":1}',
        checker = '{"decision":"send","reason":"有出处"}',
        life    = life,
        harness = harness,
        seed    = seed,
        config  = {"life": {"enable_daily_life": True}},
    )
    assert run.delivery is not None
    assert life.requests[0]["topics"] == topics and life.requests[0]["is_private"] is False
    material = run.draft_material()
    assert "1. 今天 07:44 在B站搜东西" in material and "「给AI配刹车」" in material
    assert "- " in material and "午饭打了糖醋里脊（排到我正好是最后一份）" in material
    assert "晚上去社团" not in material
    assert "这个群常聊的：QQ 机器人、显卡" in material
    assert "（apod 推送）APOD 全文xxxx" in material and material.count("只记下开头") == 1
    assert "昨天分享的歌单（没人接）" in material
    assert "第0句群聊" in material, "最近群聊给到四十条，看得到更早聊过什么"
    judged = run.model.calls_of("checker")[0].messages
    assert judged[1]["content"].endswith(draft)
    await commit(run)
    record = run.state.proactive_state.get("g42")
    assert record.shared_life_ids == ("e7",)
    assert [text for _, text in record.recent] == ["昨天分享的歌单", draft]


def test_draft_text_keeps_a_link_and_drops_markers() -> None:
    link = "https://www.bilibili.com/video/BV1aKHG6VE9b"
    text = proactive.parse_draft(
        '{"decision":"send","text":"“[表情]评论说给AI配刹车，比给它能干更实在 ' + link + '”"}'
    )
    assert text == f"评论说给AI配刹车，比给它能干更实在 {link}"
    assert len(proactive.clean_text("长" * 500)) == 300
    assert proactive.parse_draft('{"decision":"skip","reason":"没想说的"}') == ""
