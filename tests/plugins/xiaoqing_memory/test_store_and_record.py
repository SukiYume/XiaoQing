"""事件追加、换月、末行损坏、幂等键、重置代际；写入时对每类笔记的本地核对。"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from plugins.xiaoqing_memory.events import month_of
from plugins.xiaoqing_memory.model import GLOBAL, Page, PageLine
from plugins.xiaoqing_memory.store import MemoryStore
from tests.plugins.xiaoqing_memory.support import (
    ALI,
    BO,
    GROUP,
    T0,
    agent_user,
    bot_event,
    chat_user,
    life_system,
    new_store,
    record,
    task_event,
    user_event,
)


@pytest.mark.unit
def test_events_append_dedupe_and_survive_restart(temp_data_dir) -> None:
    store  = new_store(temp_data_dir)
    caller = chat_user(GROUP)
    first  = record(
        store, caller, GROUP, events=[user_event("你好", ALI, "阿梨", "k1"), bot_event("嗨", "k2")]
    )
    assert first["seqs"] == [1, 2]
    again = record(
        store,
        caller,
        GROUP,
        events=[user_event("你好", ALI, "阿梨", "k1"), user_event("在吗", BO, "阿博", "k3")],
    )
    assert again["seqs"] == [None, 3]
    reopened = MemoryStore(store.data_dir)
    assert [event.seq for event in reopened.events.events(GROUP)] == [1, 2, 3]
    assert reopened.events.by_key(GROUP, "k3").text == "在吗"
    # 重启后继续编号，不重用。
    assert record(reopened, caller, GROUP, events=[bot_event("在", "k4")])["seqs"] == [4]


@pytest.mark.unit
def test_month_rollover_and_corrupt_tail_line(temp_data_dir) -> None:
    store  = new_store(temp_data_dir)
    caller = chat_user(GROUP)
    october = datetime(2026, 10, 31, 23, 0, tzinfo=UTC).timestamp()
    november = datetime(2026, 11, 1, 1, 0, tzinfo=UTC).timestamp()
    record(
        store,
        caller,
        GROUP,
        events=[user_event("十月", ALI, "阿梨", "a", ts=october)],
        now=november,
    )
    record(
        store,
        caller,
        GROUP,
        events=[user_event("十一月", ALI, "阿梨", "b", ts=november)],
        now=november,
    )
    files = sorted(path.name for path in (store.data_dir / "events" / GROUP).glob("*.jsonl"))
    assert files == [f"{month_of(october)}.jsonl", f"{month_of(november)}.jsonl"]
    path = store.data_dir / "events" / GROUP / files[-1]
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"seq": 99, "text": "写到一半')
    reopened = MemoryStore(store.data_dir)
    assert [event.text for event in reopened.events.events(GROUP)] == ["十月", "十一月"]
    record(reopened, caller, GROUP, events=[bot_event("续上", "c", ts=november)], now=november)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[-1])["text"] == "续上"
    assert [event.text for event in MemoryStore(store.data_dir).events.events(GROUP)][-1] == "续上"


@pytest.mark.unit
def test_wrong_writer_and_origin_are_rejected(temp_data_dir) -> None:
    store = new_store(temp_data_dir)
    out = record(store, agent_user("u7"), "u7", events=[user_event("x", 7, "七", "a")])
    assert out["seqs"] == [None] and out["rejected"][0]["reason"] == "event_kind"
    bad_origin = dict(user_event("x", 7, "七", "b"), origin="web")
    out = record(store, chat_user("u7"), "u7", events=[bad_origin])
    assert out["rejected"][0]["reason"] == "event_origin"
    out = record(store, life_system(), GLOBAL, events=[user_event("x", 7, "七", "c")])
    assert out["rejected"][0]["reason"] == "event_kind"


@pytest.mark.unit
def test_self_notes_need_her_own_delivered_words(temp_data_dir) -> None:
    store  = new_store(temp_data_dir)
    caller = chat_user("u7")
    record(
        store,
        caller,
        "u7",
        events=[user_event("你在干嘛", 7, "七", "u1"), bot_event("刚下课，在食堂排队", "b1")],
    )
    ok = record(
        store,
        caller,
        "u7",
        ops=[
            {
                "op"     : "add",
                "kind"   : "self.state",
                "text"   : "在食堂排队",
                "hours"  : 2,
                "scope"  : "global",
                "sources": [{"key": "b1"}],
            },
            {
                "op"     : "add",
                "kind"   : "self.promise",
                "text"   : "晚点把笔记发你",
                "hours"  : 24,
                "scope"  : "global",
                "sources": [{"key": "b1"}],
                "data"   : {"counterpart": "七"},
            },
            {"op": "add", "kind": "self.plan", "text": "晚上去自习", "sources": [{"key": "u1"}]},
        ],
    )
    assert ok["ids"][0] and store.note(GLOBAL, ok["ids"][0]).data["origin_scope"] == "u7"
    # 承诺只留在原范围，不能记进 global。
    assert ok["ids"][1] is None and ok["rejected"][0]["reason"] == "scope_not_allowed"
    # 出处是对方的话，不是她自己说的。
    assert ok["ids"][2] is None and ok["rejected"][1]["reason"] == "source_not_self"
    dup = record(
        store,
        caller,
        "u7",
        ops=[
            {
                "op"     : "add",
                "kind"   : "self.state",
                "text"   : "在食堂排队呢",
                "hours"  : 2,
                "scope"  : "global",
                "sources": [{"key": "b1"}],
            },
        ],
    )
    assert dup["ids"] == [ok["ids"][0]]


@pytest.mark.unit
def test_loops_require_the_speaker_and_merge_reschedules(temp_data_dir) -> None:
    store  = new_store(temp_data_dir)
    caller = chat_user(GROUP)
    record(
        store,
        caller,
        GROUP,
        events=[user_event("周五面试好慌", ALI, "阿梨", "m1"), bot_event("加油", "m2")],
    )
    due = T0 + 2 * 86400
    out = record(
        store,
        caller,
        GROUP,
        ops=[
            {
                "op"     : "add",
                "kind"   : "person.loop",
                "text"   : "周五的面试",
                "data"   : {"user_id": ALI, "due_at": due},
                "sources": [{"key": "m2"}],
            },
            {
                "op"  : "add",
                "kind": "person.loop",
                "text": "周五的面试",
                "data": {"user_id": BO, "due_at": due},
            },
        ],
    )
    loop_id = out["ids"][0]
    assert loop_id and out["rejected"][0]["reason"] == "speaker_not_in_dialogue"
    moved = record(
        store,
        caller,
        GROUP,
        ops=[
            {
                "op"  : "add",
                "kind": "person.loop",
                "text": "周五的面试",
                "data": {"user_id": ALI, "due_at": due + 86400},
            },
        ],
    )
    assert moved["ids"] == [loop_id]
    assert store.note(GROUP, loop_id).data["due_at"] == due + 86400
    closed = record(store, caller, GROUP, ops=[{"op": "close", "id": loop_id, "status": "done"}])
    assert closed["ids"] == [loop_id] and store.note(GROUP, loop_id).status == "done"
    # 别的群不能结束这个群的话头。
    other = record(store, chat_user("g301"), "g301", ops=[{"op": "close", "id": loop_id}])
    assert other["rejected"][0]["reason"] == "not_found"


@pytest.mark.unit
def test_task_event_leaves_a_work_note_about_the_initiator(temp_data_dir) -> None:
    store = new_store(temp_data_dir)
    out   = record(
        store,
        agent_user("u7"),
        "u7",
        events=[
            task_event(
                "原话：记一笔午饭 25 元\n结果：已记账",
                7,
                "七",
                "task:t1",
                task_id = "t1",
                status  = "done",
                summary = "帮他记了一笔午饭 25 元",
            ),
        ],
    )
    assert out["seqs"] == [1]
    notes = [note for note in store.notes("u7").values() if note.kind == "work.task"]
    assert len(notes) == 1 and notes[0].subject == "person:7" and notes[0].origin == "tool"
    assert notes[0].text == "帮他记了一笔午饭 25 元"


@pytest.mark.unit
def test_life_notes_are_global_upserts_with_web_origin(temp_data_dir) -> None:
    store = new_store(temp_data_dir)
    op    = {
        "op"       : "add",
        "kind"     : "episode",
        "key"      : "ep1",
        "text"     : "看了一个讲猎户座星云怎么拍的视频",
        "data"     : {"title": "猎户座", "topics": ["天文摄影"]},
        "keep_days": 30,
    }
    first = record(store, life_system(), GLOBAL, ops=[op])
    again = record(
        store, life_system(), GLOBAL, ops=[dict(op, text="看了猎户座星云拍摄教程，三脚架要稳")]
    )
    assert first["ids"] == again["ids"]
    note = store.note(GLOBAL, first["ids"][0])
    assert note.origin == "web" and note.text.endswith("三脚架要稳")


@pytest.mark.unit
def test_import_is_idempotent_and_marked_legacy(temp_data_dir) -> None:
    store = new_store(temp_data_dir)
    op    = {
        "op"         : "import",
        "kind"       : "person.fact",
        "key"        : "person:old:1",
        "subject"    : f"person:{ALI}",
        "text"       : "在准备考研",
        "data"       : {"name": "阿梨"},
        "observed_at": T0 - 86400,
    }
    first = record(store, chat_user(GROUP), GROUP, ops=[op])
    again = record(store, chat_user(GROUP), GROUP, ops=[op])
    assert first["ids"] == again["ids"]
    note = store.note(GROUP, first["ids"][0])
    assert note.confidence == "legacy" and note.subject == f"person:{ALI}"
    refused = record(store, agent_user("u7"), "u7", ops=[op])
    assert refused["rejected"][0]["reason"] == "op_kind"


@pytest.mark.unit
def test_forget_scope_bumps_generation_and_drops_shared_self_notes(temp_data_dir) -> None:
    store  = new_store(temp_data_dir)
    caller = chat_user("u7")
    record(store, caller, "u7", events=[bot_event("在宿舍躺着", "b1")])
    out = record(
        store,
        caller,
        "u7",
        ops=[
            {
                "op"     : "add",
                "kind"   : "self.state",
                "text"   : "在宿舍躺着",
                "scope"  : "global",
                "sources": [{"key": "b1"}],
            },
        ],
    )
    record(store, chat_user("u8"), "u8", events=[bot_event("在图书馆", "c1")])
    keep = record(
        store,
        chat_user("u8"),
        "u8",
        ops=[
            {
                "op"     : "add",
                "kind"   : "self.state",
                "text"   : "在图书馆",
                "scope"  : "global",
                "sources": [{"key": "c1"}],
            },
        ],
    )
    assert store.forget_scope("u7") == 1
    assert store.events.events("u7") == []
    assert store.note(GLOBAL, out["ids"][0]) is None
    assert store.note(GLOBAL, keep["ids"][0]) is not None
    # 重置后的新消息继续编号，旧键可以重新写入。
    assert record(store, caller, "u7", events=[bot_event("在宿舍躺着", "b1")])["seqs"] == [2]


def test_forget_scope_scrubs_what_the_self_page_wrote_from_its_shared_notes(temp_data_dir) -> None:
    """清空会话时，自我页上照它的自我记录写的行和简介一起去掉，哪怕这句还引用着别处的笔记。"""

    store = new_store(temp_data_dir)
    ids   = []
    for scope, text, key in (("u7", "在宿舍躺着", "b1"), ("u8", "在图书馆", "c1")):
        caller = chat_user(scope)
        record(store, caller, scope, events=[bot_event(text, key)])
        out = record(
            store,
            caller,
            scope,
            ops=[
                {
                    "op"     : "add",
                    "kind"   : "self.state",
                    "text"   : text,
                    "scope"  : "global",
                    "sources": [{"key": key}],
                }
            ],
        )
        ids.append(out["ids"][0])
    gone, kept = ids
    store.put_page(
        Page(
            subject        = "self",
            scope          = GLOBAL,
            brief          = "白天在图书馆，晚上在宿舍躺着",
            brief_note_ids = (gone, kept),
            sections       = {
                "recent": (
                    PageLine("在宿舍躺着", (gone,)),
                    PageLine("在图书馆", (kept,)),
                )
            },
        )
    )

    store.forget_scope("u7")

    page = store.page(GLOBAL, "self")
    assert page is not None
    assert page.brief == "" and page.note_ids() == {kept}
    assert [line.text for _, _, line in page.lines()] == ["在图书馆"]
