"""取用：私聊的事不进群、群里本人说的能带进私聊、到期话头、预算、agent 拿不到网页来源的内容。"""

from __future__ import annotations

import random
from dataclasses import replace

import pytest

from plugins.xiaoqing_memory.config import MemoryConfig
from plugins.xiaoqing_memory.forget import forget
from plugins.xiaoqing_memory.model import GLOBAL, Note, Page, PageLine
from plugins.xiaoqing_memory.recall import recall
from plugins.xiaoqing_memory.search import search
from plugins.xiaoqing_memory.store import MemoryStore
from tests.helpers.xiaoqing_memory_test_support import (
    ALI,
    BO,
    CFG,
    GROUP,
    T0,
    bot_event,
    chat_user,
    life_system,
    new_store,
    record,
    user_event,
)


def _fact(note_id: str, scope: str, user_id: int, text: str, **extra) -> Note:
    return Note(
        id          = note_id,
        kind        = extra.pop("kind", "person.fact"),
        scope       = scope,
        subject     = f"person:{user_id}",
        text        = text,
        data        = {"name": extra.pop("name", "阿梨"), **extra.pop("data", {})},
        created_at  = T0,
        observed_at = T0,
        **extra,
    )


def _ask(store: MemoryStore, scope: str, speaker: int, text: str, **extra) -> dict:
    payload = {
        "purpose": "reply",
        "speaker": {"user_id": speaker, "name": "阿梨"},
        "text"   : text,
        **extra,
    }
    return recall(store, CFG, payload, caller="xiaoqing_chat", scope=scope, now=T0 + 600)


@pytest.mark.unit
def test_private_fact_never_reaches_the_group(temp_data_dir) -> None:
    store = new_store(temp_data_dir)
    store.put_notes(f"u{ALI}", [_fact("secret", f"u{ALI}", ALI, "私下说在偷偷准备转专业")])
    group = _ask(store, GROUP, ALI, "最近在忙啥")
    assert "转专业" not in str(group["blocks"])
    private = _ask(store, f"u{ALI}", ALI, "最近在忙啥")
    assert "转专业" in private["blocks"]["speaker"]


@pytest.mark.unit
def test_own_public_words_follow_into_private_but_not_others(temp_data_dir) -> None:
    store = new_store(temp_data_dir)
    store.put_notes(
        GROUP,
        [
            _fact("own", GROUP, ALI, "在准备秋招，投的后端"),
            _fact("told", GROUP, ALI, "听说她要搬家", confidence="reported"),
            _fact("bo", GROUP, BO, "养了一只橘猫", name="阿博"),
        ],
    )
    store.put_page(
        Page(
            subject        = f"person:{ALI}",
            scope          = GROUP,
            brief          = "群里的阿梨",
            brief_note_ids = ("own",),
            sections       = {
                "stable": (
                    PageLine("在准备秋招，投后端岗", ("own",)),
                    PageLine("可能要搬家", ("told",)),
                )
            },
        )
    )
    private = _ask(store, f"u{ALI}", ALI, "秋招好难")
    assert "秋招" in private["blocks"]["speaker"]
    # 私聊里读到的群里的事标出是在群里说的；在群里读同一页不标。
    assert "在准备秋招，投后端岗（在群里说的）" in private["blocks"]["speaker"]
    assert "在群里说的" not in _ask(store, GROUP, ALI, "秋招好难")["blocks"]["speaker"]
    assert "搬家" not in str(private["blocks"])
    assert "橘猫" not in str(private["blocks"])
    # 别人的私聊看不到。
    assert "秋招" not in str(_ask(store, f"u{BO}", BO, "秋招好难")["blocks"])


@pytest.mark.unit
def test_forgotten_note_takes_its_page_lines_and_brief_with_it(temp_data_dir) -> None:
    """删掉一条笔记后，页面上照它写的那一行和简介都不再出现；别的行照常。"""

    store = new_store(temp_data_dir)
    store.put_notes(
        GROUP,
        [
            _fact("major", GROUP, ALI, "在准备转专业"),
            _fact("cat", GROUP, ALI, "养了一只橘猫"),
        ],
    )
    store.put_page(
        Page(
            subject        = f"person:{ALI}",
            scope          = GROUP,
            brief          = "在准备转专业的阿梨",
            brief_note_ids = ("major",),
            sections       = {
                "stable": (
                    PageLine("在准备转专业", ("major",)),
                    PageLine("养了只橘猫", ("cat",)),
                )
            },
        )
    )
    assert "转专业" in _ask(store, GROUP, ALI, "在吗")["blocks"]["speaker"]

    assert forget(store, {"ids": ["major"]}, scope=GROUP)["removed"] == 1

    block = _ask(store, GROUP, ALI, "在吗")["blocks"]["speaker"]
    assert "转专业" not in block
    assert "橘猫" in block
    page = store.page(GROUP, f"person:{ALI}")
    assert page is not None and page.brief == "" and page.note_ids() == {"cat"}


@pytest.mark.unit
def test_brief_without_a_live_note_behind_it_is_not_recalled(temp_data_dir) -> None:
    """简介和各行一样要有依据：它引用的笔记不在了（比如被直接从存储里删掉），就不取用。"""

    store = new_store(temp_data_dir)
    store.put_notes(GROUP, [_fact("cat", GROUP, ALI, "养了一只橘猫")])
    store.put_page(
        Page(
            subject        = f"person:{ALI}",
            scope          = GROUP,
            brief          = "在准备转专业的阿梨",
            brief_note_ids = ("gone",),
            sections       = {"stable": (PageLine("养了只橘猫", ("cat",)),)},
        )
    )

    block = _ask(store, GROUP, ALI, "在吗")["blocks"]["speaker"]

    assert "转专业" not in block
    assert "橘猫" in block


@pytest.mark.unit
def test_other_member_mentioned_by_name_comes_in_as_a_hit(temp_data_dir) -> None:
    store = new_store(temp_data_dir)
    store.put_notes(GROUP, [_fact("bo", GROUP, BO, "养了一只橘猫叫年糕", name="阿博")])
    store.put_page(
        Page(
            subject        = f"person:{BO}",
            scope          = GROUP,
            names          = ("阿博",),
            brief          = "爱晒猫的阿博",
            brief_note_ids = ("bo",),
            sections       = {"stable": (PageLine("养了只橘猫叫年糕", ("bo",)),)},
        )
    )
    out = _ask(store, GROUP, ALI, "阿博家的猫最近还好吗")
    assert "年糕" in out["blocks"]["hits"]
    assert "年糕" not in out["blocks"]["speaker"]


@pytest.mark.unit
def test_avoid_and_tone_lines_are_marked(temp_data_dir) -> None:
    store = new_store(temp_data_dir)
    store.put_notes(
        GROUP,
        [
            _fact("a", GROUP, ALI, "上个月和对象分手了", usage="avoid"),
            _fact("b", GROUP, ALI, "不喜欢被叫全名", usage="tone"),
        ],
    )
    block = _ask(store, GROUP, ALI, "在吗")["blocks"]["speaker"]
    assert "分手" in block and "对方没提就别主动说" in block
    assert "影响你说话的方式和分寸" in block


@pytest.mark.unit
def test_renamed_speaker_is_still_the_same_person(temp_data_dir) -> None:
    store = new_store(temp_data_dir)
    store.put_notes(GROUP, [_fact("major", GROUP, ALI, "阿梨学计算机")])
    store.put_page(
        Page(
            subject  = f"person:{ALI}",
            scope    = GROUP,
            names    = ("阿梨",),
            sections = {"stable": (PageLine("阿梨学计算机", ("major",)),)},
        )
    )
    payload = {"purpose": "reply", "speaker": {"user_id": ALI, "name": "鱼丸"}, "text": "我学啥的"}
    renamed = recall(store, CFG, payload, caller="xiaoqing_chat", scope=GROUP, now=T0 + 600)
    assert renamed["blocks"]["speaker"].startswith("你记得的关于鱼丸的事（同一个人，也叫阿梨）：")
    same = _ask(store, GROUP, ALI, "我学啥的")
    assert same["blocks"]["speaker"].startswith("你记得的关于阿梨的事：")


@pytest.mark.unit
def test_lines_say_when_their_day_passed_or_how_old_they_are(temp_data_dir) -> None:
    store = new_store(temp_data_dir)
    day   = 86400.0
    store.put_notes(
        GROUP,
        [
            _fact("trip", GROUP, ALI, "11月7日去成都开会", data={"ends_at": T0 + 2 * day}),
            _fact("game", GROUP, ALI, "最近在玩一款种田游戏"),
            _fact("major", GROUP, ALI, "学的是天文"),
        ],
    )
    store.put_page(
        Page(
            subject        = f"person:{ALI}",
            scope          = GROUP,
            brief          = "这周去成都开会",
            brief_note_ids = ("trip",),
            sections       = {
                "stable": (PageLine("学天文", ("major",)),),
                "recent": (
                    PageLine("11月7日去成都开会", ("trip",)),
                    PageLine("最近在玩种田游戏", ("game",)),
                ),
            },
        )
    )
    # 每轮都被取用不让一行“永远是新的”：旧不旧看是什么时候说的。
    store.mark_used(list(store.notes(GROUP).values()) * 3, T0 + 20 * day)

    def speaker_block(now: float) -> str:
        payload = {"purpose": "reply", "speaker": {"user_id": ALI, "name": "阿梨"}, "text": "在吗"}
        out = recall(store, CFG, payload, caller="xiaoqing_chat", scope=GROUP, now=now)
        return out["blocks"]["speaker"]

    soon = speaker_block(T0 + day)
    assert "已经过了" not in soon and "天前的事" not in soon
    later = speaker_block(T0 + 20 * day)
    trip  = next(line for line in later.splitlines() if "成都" in line)
    game  = next(line for line in later.splitlines() if "种田" in line)
    major = next(line for line in later.splitlines() if "天文" in line)
    assert "写着的日子已经过了" in trip
    assert "这周去成都开会（写着的日子已经过了）" in later
    assert "约20天前的事" in game
    assert "（" not in major


@pytest.mark.unit
def test_due_loop_ask_then_review(temp_data_dir) -> None:
    store  = new_store(temp_data_dir)
    caller = chat_user(GROUP)
    record(
        store, caller, GROUP, events=[user_event("明天数电考试", ALI, "阿梨", "m1")], now=T0 - 86400
    )
    out = record(
        store,
        caller,
        GROUP,
        ops=[
            {
                "op"  : "add",
                "kind": "person.loop",
                "text": "数电考试",
                "data": {"user_id": ALI, "due_at": T0 - 3600},
            },
        ],
        now=T0 - 86400,
    )
    loop_id = out["ids"][0]
    due     = _ask(store, GROUP, ALI, "终于考完了")["due"]
    assert due["id"] == loop_id and due["mode"] == "ask"
    assert _ask(store, GROUP, BO, "终于考完了")["due"] is None
    record(
        store,
        caller,
        GROUP,
        ops=[{"op": "update", "id": loop_id, "data": {"loop": "asked", "asked_at": T0}}],
    )
    assert _ask(store, GROUP, ALI, "还行吧")["due"]["mode"] == "review"


@pytest.mark.unit
def test_self_entries_shared_and_private_promises(temp_data_dir) -> None:
    store = new_store(temp_data_dir)
    record(
        store, chat_user("u7"), "u7", events=[bot_event("在宿舍赶实验报告，弄完把笔记发你", "b1")]
    )
    record(
        store,
        chat_user("u7"),
        "u7",
        ops=[
            {
                "op"     : "add",
                "kind"   : "self.state",
                "text"   : "在宿舍赶实验报告",
                "scope"  : "global",
                "sources": [{"key": "b1"}],
            },
            {
                "op"     : "add",
                "kind"   : "self.promise",
                "text"   : "把笔记发给对方",
                "hours"  : 24,
                "sources": [{"key": "b1"}],
                "data"   : {"counterpart": "七"},
            },
        ],
    )
    here = recall(
        store,
        CFG,
        {"purpose": "reply", "text": "x"},
        caller = "xiaoqing_chat",
        scope  = "u7",
        now    = T0 + 60,
    )
    there = recall(
        store,
        CFG,
        {"purpose": "reply", "text": "x"},
        caller = "xiaoqing_chat",
        scope  = GROUP,
        now    = T0 + 60,
    )
    assert {entry["kind"] for entry in here["self_entries"]} == {"state", "promise"}
    assert [entry["kind"] for entry in there["self_entries"]] == ["state"]


@pytest.mark.unit
def test_agent_recall_drops_web_sourced_content_property(temp_data_dir) -> None:
    """随机生成各种来源的记录：交给 agent 的内容里没有任何一条来源链含网页文字。"""

    rng = random.Random(7)
    for round_index in range(20):
        store = MemoryStore(temp_data_dir / f"r{round_index}")
        notes = []
        for index in range(12):
            origin  = rng.choice(["user", "web", "tool", "derived"])
            sources = ({"type": "external", "origin": "web"},) if rng.random() < 0.3 else ()
            notes.append(
                Note(
                    id          = f"n{index}",
                    kind        = rng.choice(["person.fact", "person.pref"]),
                    scope       = "u7",
                    subject     = "person:7",
                    text        = f"偏好{index}号",
                    origin      = origin,
                    sources     = sources,
                    created_at  = T0,
                    observed_at = T0,
                )
            )
        store.put_notes("u7", notes)
        lines = tuple(PageLine(f"第{note.id}行", (note.id,)) for note in notes)
        store.put_page(
            Page(
                subject="person:7", scope="u7", sections={"stable": lines[:6], "errands": lines[6:]}
            )
        )
        out = recall(
            store,
            CFG,
            {"purpose": "task", "speaker": {"user_id": 7}},
            caller = "xiaoqing_agent",
            scope  = "u7",
            now    = T0,
        )
        shown = str(out["blocks"])
        for note in notes:
            if "web" in note.source_origins():
                assert f"第{note.id}行" not in shown and f"偏好{note.id[1:]}号" not in shown


@pytest.mark.unit
def test_budgets_cut_whole_lines(temp_data_dir) -> None:
    store = new_store(temp_data_dir)
    notes = [
        _fact(f"n{i}", GROUP, ALI, f"第{i}件很长很长很长很长很长很长的事情") for i in range(40)
    ]
    store.put_notes(GROUP, notes)
    cfg = MemoryConfig(speaker_budget=200)
    out = recall(
        store,
        cfg,
        {"purpose": "reply", "speaker": {"user_id": ALI, "name": "阿梨"}, "text": "x"},
        caller = "xiaoqing_chat",
        scope  = GROUP,
        now    = T0,
    )
    block = out["blocks"]["speaker"]
    assert 0 < len(block) <= 200
    assert all(line.endswith("事情") or line.endswith("：") for line in block.splitlines())


@pytest.mark.unit
def test_recent_bot_lines_are_not_repeated(temp_data_dir) -> None:
    store = new_store(temp_data_dir)
    store.put_notes(GROUP, [_fact("n1", GROUP, ALI, "下周三去杭州面试")])
    shown = _ask(store, GROUP, ALI, "嗯")["blocks"]["speaker"]
    assert "杭州" in shown
    again = _ask(store, GROUP, ALI, "嗯", bot_recent=["你下周三去杭州面试对吧"])["blocks"][
        "speaker"
    ]
    assert "杭州" not in again


@pytest.mark.unit
def test_unconsolidated_events_show_up_as_hits_outside_the_window(temp_data_dir) -> None:
    store  = new_store(temp_data_dir)
    caller = chat_user(GROUP)
    record(
        store,
        caller,
        GROUP,
        events=[
            user_event("我们宿舍楼下新开了一家螺蛳粉店", BO, "阿博", "e1", ts=T0 - 7200),
            user_event("今天好冷", ALI, "阿梨", "e2", ts=T0 - 60),
        ],
    )
    out = _ask(store, GROUP, ALI, "螺蛳粉店在哪来着", recent=["今天好冷"])
    assert "螺蛳粉" in out["blocks"]["hits"] and "阿博" in out["blocks"]["hits"]
    inside = _ask(store, GROUP, ALI, "螺蛳粉店在哪来着", recent=["我们宿舍楼下新开了一家螺蛳粉店"])
    assert "螺蛳粉" not in inside["blocks"]["hits"]


@pytest.mark.unit
def test_legacy_summaries_only_on_explicit_reference(temp_data_dir) -> None:
    store = new_store(temp_data_dir)
    record(
        store,
        chat_user(GROUP),
        GROUP,
        ops=[
            {
                "op"     : "import",
                "kind"   : "legacy.summary",
                "key"    : "topic:1",
                "subject": "group:300",
                "text"   : "话题：抽卡爆率\n摘要：大家讨论十连抽出两个金",
            },
        ],
    )
    assert "抽卡" not in str(_ask(store, GROUP, ALI, "今天抽卡又歪了")["blocks"])
    assert "抽卡" in _ask(store, GROUP, ALI, "还记得上次抽卡那事吗")["blocks"]["hits"]


@pytest.mark.unit
def test_search_lists_self_records_without_query(temp_data_dir) -> None:
    store = new_store(temp_data_dir)
    record(store, chat_user("u7"), "u7", events=[bot_event("在食堂", "b1")])
    record(
        store,
        chat_user("u7"),
        "u7",
        ops=[
            {
                "op"     : "add",
                "kind"   : "self.state",
                "text"   : "在食堂",
                "scope"  : "global",
                "sources": [{"key": "b1"}],
            },
        ],
    )
    record(
        store,
        life_system(),
        GLOBAL,
        ops=[
            {"op": "add", "kind": "episode", "key": "e1", "text": "看了个猫猫视频"},
        ],
    )
    found = search(store, {"kinds": ["self.state", "self.story"]}, scope=GROUP, now=T0)
    assert [item["text"] for item in found["items"]] == ["在食堂"]
    assert found["items"][0]["entry"]["kind"] == "state"


@pytest.mark.unit
def test_notes_the_page_writer_left_out_are_not_listed_as_new(temp_data_dir) -> None:
    """页面重写时就有、却没写进页面的笔记（多半是重复的旧事实）不再列为“刚记下”；之后才记下的照常列。"""

    store = new_store(temp_data_dir)
    old   = [
        _fact("kept", GROUP, ALI, "爱吃螺蛳粉", confidence="legacy"),
        _fact("dup", GROUP, ALI, "螺蛳粉重度爱好者，每周都要吃", confidence="legacy"),
    ]
    store.put_notes(GROUP, old)
    store.put_page(
        Page(
            subject    = f"person:{ALI}",
            scope      = GROUP,
            sections   = {"stable": (PageLine("爱吃螺蛳粉", ("kept",)),)},
            updated_at = T0 + 100,
        )
    )
    late = replace(_fact("late", GROUP, ALI, "下周去面试"), created_at=T0 + 200)
    store.put_notes(GROUP, [late])
    speaker = _ask(store, GROUP, ALI, "在干嘛")["blocks"]["speaker"]
    assert speaker.count("螺蛳粉") == 1
    assert "[刚记下] 下周去面试" in speaker


@pytest.mark.unit
def test_imported_promises_and_states_keep_the_time_they_were_said(temp_data_dir) -> None:
    """记入时间晚于原话说出时间的承诺和近况（比如批量导入的旧记录），按原话说出的时间算“多久以前”，不当成刚说的。"""

    store = new_store(temp_data_dir)
    said  = T0 - 3 * 86400
    store.put_notes(
        "u7",
        [
            Note(
                id          = "p1",
                kind        = "self.promise",
                scope       = "u7",
                subject     = "self",
                text        = "周五提醒对方交实验报告",
                data        = {"counterpart": "七"},
                created_at  = T0,
                observed_at = said,
                expires_at  = T0 + 7 * 86400,
            )
        ],
    )
    task = recall(
        store,
        CFG,
        {"purpose": "task", "speaker": {"user_id": 7}},
        caller = "xiaoqing_agent",
        scope  = "u7",
        now    = T0 + 60,
    )
    assert "约3天前答应七" in task["blocks"]["promises"]
    reply = recall(
        store,
        CFG,
        {"purpose": "reply", "text": "x"},
        caller = "xiaoqing_chat",
        scope  = "u7",
        now    = T0 + 60,
    )
    assert reply["self_entries"][0]["ts"] == said
