"""整理：摘录核对、说话人一致、重复只更新印证时间、改口替换、页面校验与回退、核对撤回、预算、有界增长。"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime

import pytest

from plugins.xiaoqing_memory.config import MemoryConfig
from plugins.xiaoqing_memory.consolidate import pages as pages_mod
from plugins.xiaoqing_memory.consolidate.runner import (
    consolidate_scope,
    nightly_pass,
    prune_scope,
    rewrite_page,
    verify_lines,
)
from plugins.xiaoqing_memory.model import PAGE_LIMITS, Page, PageLine, page_kind
from plugins.xiaoqing_memory.recall import recall
from tests.plugins.xiaoqing_memory.support import (
    ALI,
    BO,
    CFG,
    GROUP,
    T0,
    ZONE,
    FakeModel,
    agent_user,
    bot_event,
    chat_user,
    deps,
    new_store,
    record,
    task_event,
    user_event,
)


def _seed(store, lines):
    events = []
    for index, (speaker, text) in enumerate(lines):
        if speaker == "bot":
            events.append(bot_event(text, f"k{index}", ts=T0 + index))
        else:
            user_id, name = speaker
            events.append(user_event(text, user_id, name, f"k{index}", ts=T0 + index))
    record(store, chat_user(GROUP), GROUP, events=events, now=T0 + 100)


def _run(coro):
    return asyncio.run(coro)


@pytest.mark.unit
def test_extract_checks_quotes_and_speakers(temp_data_dir) -> None:
    store = new_store(temp_data_dir)
    _seed(
        store,
        [
            ((ALI, "阿梨"), "我下周三去杭州面试后端，有点慌"),
            ((BO, "阿博"), "阿梨肯定能过"),
            ("bot", "加油呀"),
        ],
    )
    model = FakeModel(
        {
            "notes": [
                {
                    "kind"  : "person.fact",
                    "about" : ALI,
                    "text"  : "下周三去杭州面试后端岗",
                    "source": "e1",
                    "quote" : "下周三去杭州面试后端",
                },
                # 摘录不在原文里。
                {
                    "kind"  : "person.fact",
                    "about" : ALI,
                    "text"  : "已经拿到 offer",
                    "source": "e1",
                    "quote" : "拿到offer了",
                },
                # 阿博说的话不能当成阿梨的事实。
                {
                    "kind"  : "person.fact",
                    "about" : ALI,
                    "text"  : "肯定能过",
                    "source": "e2",
                    "quote" : "阿梨肯定能过",
                },
                # 编号不在这一批里。
                {"kind": "person.fact", "about": ALI, "text": "x", "source": "e99", "quote": "x"},
                {
                    "kind"  : "group.fact",
                    "text"  : "群友会互相打气",
                    "source": "e2",
                    "quote" : "肯定能过",
                },
            ]
        },
        {"names": ["阿梨"], "brief": "在找工作的阿梨", "brief_notes": [], "sections": {}},
    )
    counters = _run(consolidate_scope(store, CFG, deps(model), GROUP, force=True, rewrites=0))
    notes = {note.text: note for note in store.notes(GROUP).values()}
    assert set(notes) == {"下周三去杭州面试后端岗", "群友会互相打气"}
    fact = notes["下周三去杭州面试后端岗"]
    assert fact.subject == f"person:{ALI}" and fact.quote == "下周三去杭州面试后端"
    assert (
        notes["群友会互相打气"].confidence == "reported"
        and notes["群友会互相打气"].data["speaker"] == "阿博"
    )
    assert counters["extract.rejected"] == 3
    assert store.scope_state(GROUP)["watermark"] == 3
    # 没有模型调用也能取用：新笔记按确定性规则追加进页面。
    page = store.page(GROUP, f"person:{ALI}")
    assert page is not None and page.note_ids() == {fact.id}


@pytest.mark.unit
def test_repeat_touches_and_single_value_attribute_supersedes(temp_data_dir) -> None:
    store = new_store(temp_data_dir)
    _seed(store, [((ALI, "阿梨"), "我住在苏州园区")])
    first = FakeModel(
        {
            "notes": [
                {
                    "kind"     : "person.fact",
                    "about"    : ALI,
                    "text"     : "住在苏州",
                    "attribute": "城市",
                    "source"   : "e1",
                    "quote"    : "住在苏州园区",
                }
            ]
        }
    )
    _run(consolidate_scope(store, CFG, deps(first), GROUP, force=True, rewrites=0))
    old_id = next(iter(store.notes(GROUP)))
    record(
        store,
        chat_user(GROUP),
        GROUP,
        events=[
            user_event("我还是住苏州园区那边", ALI, "阿梨", "k10", ts=T0 + 500),
            user_event("下个月搬去上海了", ALI, "阿梨", "k11", ts=T0 + 600),
        ],
        now=T0 + 700,
    )
    second = FakeModel(
        {
            "same" : [{"id": old_id, "source": "e2", "quote": "住苏州园区"}],
            "notes": [
                {
                    "kind"     : "person.fact",
                    "about"    : ALI,
                    "text"     : "下个月搬去上海",
                    "attribute": "城市",
                    "source"   : "e3",
                    "quote"    : "下个月搬去上海",
                }
            ],
        }
    )
    _run(consolidate_scope(store, CFG, deps(second), GROUP, force=True, rewrites=0))
    notes = store.notes(GROUP)
    old   = notes[old_id]
    new   = next(note for note in notes.values() if note.id != old_id)
    assert old.status == "superseded" and new.supersedes == old_id
    assert old.observed_at >= T0 + 500


@pytest.mark.unit
def test_page_rewrite_validates_citations_and_limits(temp_data_dir) -> None:
    store = new_store(temp_data_dir)
    _seed(store, [((ALI, "阿梨"), "我养了只猫叫豆包"), ((ALI, "阿梨"), "我是数学系大三的")])

    def good_page(ids):
        return {
            "names"      : ["阿梨"],
            "brief"      : "数学系大三，养猫",
            "brief_notes": ids,
            "sections"   : {
                "stable": [
                    {"text": "数学系大三", "notes": ids[:1]},
                    {"text": "养了只猫叫豆包", "notes": ids[1:]},
                ]
            },
        }

    def page_from_notes(messages):
        ids = re.findall(r'"id": "(n[0-9a-f]+)"', messages[1]["content"])
        return good_page(ids)

    model = FakeModel(
        {
            "notes": [
                {
                    "kind"  : "person.fact",
                    "about" : ALI,
                    "text"  : "养了只猫叫豆包",
                    "source": "e1",
                    "quote" : "养了只猫叫豆包",
                },
                {
                    "kind"  : "person.fact",
                    "about" : ALI,
                    "text"  : "数学系大三",
                    "source": "e2",
                    "quote" : "数学系大三",
                },
            ]
        },
        page_from_notes,
    )
    _run(consolidate_scope(store, CFG, deps(model), GROUP, force=True))
    page = store.page(GROUP, f"person:{ALI}")
    assert page.brief == "数学系大三，养猫" and len(page.sections["stable"]) == 2
    notes = list(store.notes(GROUP).values())
    # 没有出处的行、超长的页面：整页作废。
    uncited = json.dumps(
        {"brief": "", "sections": {"stable": [{"text": "喜欢喝奶茶", "notes": []}]}}
    )
    assert (
        pages_mod.parse_page(
            uncited, subject=page.subject, scope=GROUP, notes=notes, old=page, now=T0
        )[1]
        == "uncited_line"
    )
    long = json.dumps(
        {"brief": "", "sections": {"stable": [{"text": "很" * 120, "notes": [notes[0].id]}] * 6}}
    )
    assert (
        pages_mod.parse_page(
            long, subject=page.subject, scope=GROUP, notes=notes, old=page, now=T0
        )[1]
        == "over_limit"
    )


@pytest.mark.unit
def test_verify_retracts_unsupported_line(temp_data_dir) -> None:
    store = new_store(temp_data_dir)
    _seed(store, [((ALI, "阿梨"), "我室友养了只猫")])
    model = FakeModel(
        {
            "notes": [
                {
                    "kind"  : "person.fact",
                    "about" : ALI,
                    "text"  : "养了只猫",
                    "source": "e1",
                    "quote" : "室友养了只猫",
                }
            ]
        }
    )
    _run(consolidate_scope(store, CFG, deps(model), GROUP, force=True, rewrites=0))
    note = next(iter(store.notes(GROUP).values()))
    store.put_page(
        Page(
            subject  = f"person:{ALI}",
            scope    = GROUP,
            sections = {"stable": (PageLine("自己养了只猫", (note.id,)),)},
        )
    )
    checker = FakeModel({"results": [{"line": "L1", "verdict": "unsupported", "rewrite": ""}]})
    counters = _run(verify_lines(store, CFG, deps(checker), calls=1, force=True))
    assert counters["verify.unsupported"] == 1
    assert store.note(GROUP, note.id).status == "retracted"
    assert store.page(GROUP, f"person:{ALI}").sections["stable"] == ()
    out = recall(
        store,
        CFG,
        {"purpose": "reply", "speaker": {"user_id": ALI}, "text": "猫"},
        caller = "xiaoqing_chat",
        scope  = GROUP,
        now    = T0 + 3600,
    )
    assert "猫" not in out["blocks"]["speaker"]


def _page_with_line(store, note, text: str, section: str = "stable") -> None:
    store.put_page(
        Page(
            subject  = f"person:{ALI}",
            scope    = GROUP,
            sections = {section: (PageLine(text, (note.id,)),)},
        )
    )


def _one_note(store, said: str, text: str, quote: str):
    _seed(store, [((BO, "阿波"), "你那个是什么"), ((ALI, "阿梨"), said)])
    model = FakeModel(
        {
            "notes": [
                {"kind": "person.fact", "about": ALI, "text": text, "source": "e2", "quote": quote}
            ]
        }
    )
    _run(consolidate_scope(store, CFG, deps(model), GROUP, force=True, rewrites=0))
    return next(iter(store.notes(GROUP).values()))


@pytest.mark.unit
def test_checked_notes_are_not_rechecked_or_retracted_later(temp_data_dir) -> None:
    store = new_store(temp_data_dir)
    note  = _one_note(store, "我有一张健身卡", "有健身卡", "我有一张健身卡")
    _page_with_line(store, note, "有一张健身卡")
    passed = FakeModel({"results": [{"line": "L1", "verdict": "supported", "rewrite": ""}]})
    _run(verify_lines(store, CFG, deps(passed), calls=1, force=True))
    assert store.note(GROUP, note.id).verified_at > 0

    # 依据都核对过的行不再交给模型。
    again = FakeModel({"results": [{"line": "L1", "verdict": "unsupported", "rewrite": ""}]})
    _run(verify_lines(store, CFG, deps(again), calls=1, force=True))
    assert again.calls == []

    # 页面重写后换了说法、又多引用了一条新笔记：新笔记要核对；判为不支持时只撤回新的那条。
    extra = store.note(GROUP, note.id).with_changes(
        id="nfresh0001", text="卡里还有钱", verified_at=0.0
    )
    store.put_notes(GROUP, [extra])
    store.put_page(
        Page(
            subject  = f"person:{ALI}",
            scope    = GROUP,
            sections = {"stable": (PageLine("健身卡里还有钱", (note.id, extra.id)),)},
        )
    )
    _run(verify_lines(store, CFG, deps(again), calls=1, force=True))
    assert len(again.calls) == 1
    assert store.note(GROUP, note.id).status == "active"
    assert store.note(GROUP, extra.id).status == "retracted"
    assert store.page(GROUP, f"person:{ALI}").sections["stable"] == ()


@pytest.mark.unit
def test_partly_supported_line_corrects_its_single_note_and_sees_context(temp_data_dir) -> None:
    store = new_store(temp_data_dir)
    note  = _one_note(store, "健身卡", "办了一张年卡", "健身卡")
    _page_with_line(store, note, "办了一张健身年卡")
    checker = FakeModel(
        {"results": [{"line": "L1", "verdict": "partly", "rewrite": "有一张健身卡"}]}
    )
    _run(verify_lines(store, CFG, deps(checker), calls=1, force=True))
    sent = json.loads(checker.calls[0][1]["content"])
    # 原话带日期和星期，换算出来的具体日期才核对得了。
    assert re.fullmatch(r"\[\d\d-\d\d 周. \d\d:\d\d\] 阿梨：健身卡", sent[0]["original"][0])
    assert sent[0]["before"][0].endswith("阿波：你那个是什么")
    assert store.note(GROUP, note.id).text == "有一张健身卡"
    assert store.page(GROUP, f"person:{ALI}").sections["stable"][0].text == "有一张健身卡"


@pytest.mark.unit
def test_unreadable_model_output_keeps_the_batch_for_a_retry(temp_data_dir) -> None:
    """模型的输出读不出对象时这一批不算整理过；下一次给出能读的结果，照常写出笔记。"""

    store = new_store(temp_data_dir)
    _seed(store, [((ALI, "阿梨"), "我下周去杭州面试")])
    garbled = FakeModel("这一批我看不出要记什么。")
    counters = _run(consolidate_scope(store, CFG, deps(garbled), GROUP, force=True))
    assert counters["extract.unparsed"] == 1
    assert store.scope_state(GROUP)["watermark"] == 0
    assert store.notes(GROUP) == {}

    working = FakeModel(
        {
            "notes": [
                {
                    "kind"  : "person.fact",
                    "about" : ALI,
                    "text"  : "下周去杭州面试",
                    "source": "e1",
                    "quote" : "下周去杭州面试",
                }
            ]
        }
    )
    counters = _run(consolidate_scope(store, CFG, deps(working), GROUP, force=True))
    assert len(working.calls) >= 1 and counters["notes.added"] == 1
    assert store.scope_state(GROUP)["watermark"] == 1
    assert store.scope_state(GROUP)["unparsed"] is None


@pytest.mark.unit
def test_batch_that_never_parses_is_let_go_after_a_few_tries(temp_data_dir) -> None:
    """同一批连着读不出来：前两次留着重试，第三次放过，后面的消息才整理得到。"""

    store = new_store(temp_data_dir)
    _seed(store, [((ALI, "阿梨"), "今天好累")])
    for expected in (0, 0, 1):
        counters = _run(
            consolidate_scope(store, CFG, deps(FakeModel("不是 JSON")), GROUP, force=True)
        )
        assert counters["extract.unparsed"] == 1
        assert store.scope_state(GROUP)["watermark"] == expected
    assert counters["extract.abandoned"] == 1


@pytest.mark.unit
def test_empty_object_from_the_model_means_nothing_to_note(temp_data_dir) -> None:
    store = new_store(temp_data_dir)
    _seed(store, [((ALI, "阿梨"), "哈哈")])
    counters = _run(consolidate_scope(store, CFG, deps(FakeModel("{}")), GROUP, force=True))
    assert "extract.unparsed" not in counters and counters["extract.batches"] == 1
    assert store.scope_state(GROUP)["watermark"] == 1


@pytest.mark.unit
def test_budget_is_reserved_before_calls_and_failures_keep_watermark(temp_data_dir) -> None:
    store = new_store(temp_data_dir)
    _seed(store, [((ALI, "阿梨"), "今天好累")])
    cfg = MemoryConfig(max_llm_calls_per_day=0)
    model    = FakeModel()
    counters = _run(consolidate_scope(store, cfg, deps(model), GROUP))
    assert model.calls == [] and counters["llm.budget_exhausted"] >= 1
    assert store.scope_state(GROUP)["watermark"] == 0
    broken   = FakeModel(RuntimeError("upstream down"))
    counters = _run(consolidate_scope(store, MemoryConfig(), deps(broken), GROUP))
    assert counters["extract.failed"] == 1 and store.scope_state(GROUP)["watermark"] == 0
    assert store.calls_used(deps(broken).day()) == 1


@pytest.mark.unit
def test_reset_during_consolidation_is_not_committed(temp_data_dir) -> None:
    store = new_store(temp_data_dir)
    _seed(store, [((ALI, "阿梨"), "我在学吉他")])

    def reset_then_answer(_messages):
        store.forget_scope(GROUP)
        return {
            "notes": [
                {
                    "kind"  : "person.fact",
                    "about" : ALI,
                    "text"  : "在学吉他",
                    "source": "e1",
                    "quote" : "在学吉他",
                }
            ]
        }

    _run(consolidate_scope(store, CFG, deps(FakeModel(reset_then_answer)), GROUP, force=True))
    assert store.notes(GROUP) == {}


@pytest.mark.unit
def test_nightly_gate_runs_in_window_or_on_backlog(temp_data_dir) -> None:
    store = new_store(temp_data_dir)
    _seed(store, [((ALI, "阿梨"), "随便聊聊")])
    model = FakeModel()
    day = datetime(2026, 11, 12, 14, 0, tzinfo=ZONE)
    _run(nightly_pass(store, CFG, deps(model), day))
    assert model.calls == []
    night = datetime(2026, 11, 12, 3, 0, tzinfo=ZONE)
    _run(nightly_pass(store, CFG, deps(model), night))
    assert len(model.calls) >= 1
    assert store.scope_state(GROUP)["watermark"] == 1


@pytest.mark.unit
def test_dated_notes_keep_their_day_and_pages_learn_when_it_passed(temp_data_dir) -> None:
    store = new_store(temp_data_dir)
    said = datetime(2026, 11, 3, 20, 0, tzinfo=ZONE).timestamp()  # 周二
    record(
        store,
        chat_user(GROUP),
        GROUP,
        events=[
            user_event("周五我去成都开会，周日回来", ALI, "阿梨", "k1", ts=said),
            user_event("我在学吉他", ALI, "阿梨", "k2", ts=said + 1),
        ],
        now=said + 10,
    )
    model = FakeModel(
        {
            "notes": [
                {
                    "kind"  : "person.fact",
                    "about" : ALI,
                    "text"  : "11月7日至9日去成都开会",
                    "until" : "2026-11-09",
                    "source": "e1",
                    "quote" : "周五我去成都开会",
                },
                {
                    "kind"  : "person.fact",
                    "about" : ALI,
                    "text"  : "在学吉他",
                    "until" : "下周",
                    "source": "e2",
                    "quote" : "我在学吉他",
                },
            ]
        }
    )
    _run(consolidate_scope(store, CFG, deps(model, clock=lambda: said + 3600), GROUP, rewrites=0))
    prompt = model.calls[0][1]["content"]
    assert "现在是 2026-11-03 周二" in prompt and "11-03 周二 20:00" in prompt
    by_text = {note.text: note for note in store.notes(GROUP).values()}
    trip    = by_text["11月7日至9日去成都开会"]
    ends = datetime(2026, 11, 10, 0, 0, tzinfo=ZONE).timestamp()
    assert trip.ends_at() == ends and trip.expires_at == ends + 30 * 86400
    assert by_text["在学吉他"].ends_at() == 0 and by_text["在学吉他"].expires_at == 0

    page = Page(
        subject        = f"person:{ALI}",
        scope          = GROUP,
        sections       = {"recent": (PageLine("周五去成都开会", (trip.id,)),)},
        updated_at     = said + 3600,
        prompt_version = pages_mod.PROMPT_VERSION,
    )
    store.put_page(page)

    def pending(now: float) -> list[tuple[str, int]]:
        return pages_mod.pending_subjects(store.notes(GROUP), store.pages(GROUP), GROUP, now)

    # 日子没过、页面已经写过：不用重写；日子过了：要重写成过去的事。
    assert pending(ends - 1) == []
    assert pending(ends + 60) == [(f"person:{ALI}", 1)]
    writer = FakeModel()
    _run(rewrite_page(store, CFG, deps(writer, clock=lambda: ends + 60), GROUP, f"person:{ALI}"))
    assert "今天是 2026-11-10 周二" in writer.calls[0][1]["content"]
    assert '"until": "2026-11-09"' in writer.calls[0][1]["content"]


@pytest.mark.unit
def test_facts_learned_while_doing_an_errand_stay_facts(temp_data_dir) -> None:
    store = new_store(temp_data_dir)
    record(
        store,
        agent_user(GROUP),
        GROUP,
        events=[
            task_event("订了周五去成都的高铁，记账默认用微信", ALI, "阿梨", "t1", task_id="t1"),
        ],
        now=T0 + 10,
    )
    model = FakeModel(
        {
            "notes": [
                {
                    "kind"  : "person.fact",
                    "about" : ALI,
                    "text"  : "周五去成都",
                    "source": "e1",
                    "quote" : "订了周五去成都的高铁",
                },
                {
                    "kind"  : "person.pref",
                    "about" : ALI,
                    "text"  : "记账默认用微信",
                    "source": "e1",
                    "quote" : "记账默认用微信",
                },
            ]
        }
    )
    _run(consolidate_scope(store, CFG, deps(model), GROUP, force=True, rewrites=0))
    kinds = {note.text: note.kind for note in store.notes(GROUP).values()}
    assert kinds["周五去成都"] == "person.fact"
    assert kinds["记账默认用微信"] == "person.pref"


@pytest.mark.unit
def test_batches_with_only_her_own_messages_cost_no_calls(temp_data_dir) -> None:
    store = new_store(temp_data_dir)
    record(
        store,
        chat_user("u456"),
        "u456",
        events=[bot_event("早上好，今天的论文推送来啦", f"b{i}", ts=T0 + i) for i in range(3)],
        now=T0 + 100,
    )
    model    = FakeModel()
    counters = _run(consolidate_scope(store, CFG, deps(model), "u456"))
    assert model.calls == [] and counters["extract.skipped"] == 1
    assert store.scope_state("u456")["watermark"] == 3


@pytest.mark.unit
def test_scopes_with_more_said_by_people_go_first(temp_data_dir) -> None:
    store = new_store(temp_data_dir)
    pushes = [bot_event(f"推送 {i}", f"p{i}", ts=T0 + i) for i in range(6)]
    record(store, chat_user("g200"), "g200", events=pushes, now=T0 + 100)
    _seed(store, [((ALI, "阿梨"), "周末去爬山吗"), ((BO, "阿波"), "去啊")])
    model = FakeModel()
    night = datetime(2026, 11, 12, 3, 0, tzinfo=ZONE)
    _run(nightly_pass(store, MemoryConfig(max_llm_calls_per_day=1), deps(model), night))
    assert len(model.calls) == 1
    assert "周末去爬山吗" in model.calls[0][1]["content"]
    assert store.scope_state("g200")["watermark"] == 6


@pytest.mark.unit
def test_pending_pages_of_quiet_scopes_are_rewritten_at_night(temp_data_dir) -> None:
    store = new_store(temp_data_dir)
    _seed(store, [((ALI, "阿梨"), "我在学吉他")])
    extract = FakeModel(
        {
            "notes": [
                {
                    "kind"  : "person.fact",
                    "about" : ALI,
                    "text"  : "在学吉他",
                    "source": "e1",
                    "quote" : "在学吉他",
                }
            ]
        }
    )
    # 当天预算只够抽取：页面先按确定性规则追加。
    _run(consolidate_scope(store, MemoryConfig(max_llm_calls_per_day=1), deps(extract), GROUP))
    appended = store.page(GROUP, f"person:{ALI}")
    assert appended is not None and appended.prompt_version == 0
    note_id = next(iter(store.notes(GROUP)))
    writer  = FakeModel(
        {
            "names"   : ["阿梨"],
            "brief"   : "",
            "sections": {"stable": [{"text": "在学吉他", "notes": [note_id]}]},
        }
    )
    night = datetime(2026, 11, 13, 3, 0, tzinfo=ZONE)
    _run(nightly_pass(store, CFG, deps(writer, clock=lambda: night.timestamp()), night))
    page = store.page(GROUP, f"person:{ALI}")
    assert page is not None and page.prompt_version == pages_mod.PROMPT_VERSION
    assert [line.text for line in page.sections["stable"]] == ["在学吉他"]


@pytest.mark.unit
def test_bounded_growth_over_thirty_days(temp_data_dir) -> None:
    """可控时钟跑 30 天脚本化对话，模型每批都给新事实：笔记数、页面字数、事件文件数都有上限。"""

    store = new_store(temp_data_dir)
    cfg = MemoryConfig(max_notes_per_subject=12, event_keep_days=31)
    clock = {"now": T0}

    def facts(messages):
        seqs = re.findall(r"^e(\d+) .*?<(\d+)>：(.*)$", messages[1]["content"], re.MULTILINE)
        return {
            "notes": [
                {
                    "kind"  : "person.fact",
                    "about" : int(uid),
                    "text"  : f"第{seq}条：{text[:10]}",
                    "source": f"e{seq}",
                    "quote" : text[:6],
                }
                for seq, uid, text in seqs
            ]
        }

    def page(messages):
        ids = re.findall(r'"id": "(n[0-9a-f]+)"', messages[1]["content"])[-5:]
        return {
            "brief"      : "群友",
            "brief_notes": ids[:1],
            "sections"   : {
                "stable": [
                    {"text": f"一件事{index}", "notes": [note_id]}
                    for index, note_id in enumerate(ids)
                ]
            },
        }

    for day in range(60):
        clock["now"] = T0 + day * 86400
        events       = [
            user_event(
                f"第{day}天我做了件新鲜事编号{index}",
                ALI if index % 2 else BO,
                "阿梨" if index % 2 else "阿博",
                f"d{day}-{index}",
                ts=clock["now"] + index,
            )
            for index in range(6)
        ]
        record(store, chat_user(GROUP), GROUP, events=events, now=clock["now"] + 10)
        model = FakeModel(*([facts] * 3 + [page] * 4))
        _run(
            consolidate_scope(
                store, cfg, deps(model, clock=lambda: clock["now"] + 60), GROUP, force=True
            )
        )
        prune_scope(store, cfg, GROUP, clock["now"] + 60)
    active = [note for note in store.notes(GROUP).values() if note.status == "active"]
    per_person: dict[str, int] = {}
    for note in active:
        per_person[note.subject] = per_person.get(note.subject, 0) + 1
    assert per_person and max(per_person.values()) <= 12
    assert len(store.notes(GROUP)) <= 2 * 12 + 2 * 6 * 95
    assert all(
        page.char_count() <= PAGE_LIMITS[page_kind(page.subject)]
        for page in store.pages(GROUP).values()
    )
    months = list((store.data_dir / "events" / GROUP).glob("*.jsonl"))
    assert len(months) <= 3


@pytest.mark.unit
def test_closed_self_records_are_kept_one_week_but_stories_stay() -> None:
    from plugins.xiaoqing_memory.consolidate.prune import prune_notes
    from plugins.xiaoqing_memory.model import Note

    def note(note_id, kind, expires):
        return Note(
            id          = note_id,
            kind        = kind,
            scope       = "global",
            subject     = "self",
            text        = note_id,
            origin      = "self",
            created_at  = T0,
            observed_at = T0,
            expires_at  = expires,
        )

    notes = {
        "old_state": note("old_state", "self.state", T0 + 3600),
        "new_state": note("new_state", "self.state", T0 + 6 * 86400),
        "story"    : note("story", "self.story", T0 + 365 * 86400),
    }
    result = prune_notes(notes, CFG, T0 + 8 * 86400)
    assert "old_state" in result.removed
    assert "new_state" not in result.removed and result.changed["new_state"].status == "done"
    assert "story" not in result.removed and "story" not in result.changed
