"""群里的话题词：整理第一步顺带产出，只给群聊里这两三天可以自然提起的，只有词。"""

from __future__ import annotations

import asyncio

import pytest

from plugins.xiaoqing_memory import topics as topics_mod
from plugins.xiaoqing_memory.consolidate.runner import consolidate_scope
from plugins.xiaoqing_memory.recall import recall
from tests.helpers.xiaoqing_memory_test_support import (
    ALI,
    BO,
    CFG,
    GROUP,
    T0,
    FakeModel,
    bot_event,
    chat_user,
    deps,
    new_store,
    record,
    user_event,
)

DAY = 86400.0


def _seed(store, scope: str) -> None:
    record(
        store,
        chat_user(scope),
        scope,
        events=[
            user_event("芙莉莲二期第五集看了没，打斗太帅了", ALI, "阿梨", "k1", ts=T0),
            user_event("我和室友又吵起来了，烦死", BO, "阿博", "k2", ts=T0 + 1),
            bot_event("【地震快讯】某地发生3级地震", "k3", ts=T0 + 2),
        ],
        now=T0 + 10,
    )


_TOPICS = {
    "notes" : [],
    "topics": [
        {
            "topic": "芙莉莲二期",
            # 超长的关键词整个不要，不截成半个词。
            "keywords": ["新番", "打斗场面超级无敌帅", "打斗", "第五集", "多余"],
            "usage"   : "normal",
            "source"  : "e1",
        },
        {"topic": "和室友吵架", "keywords": ["室友"], "usage": "avoid", "source": "e2"},
        {"topic": "编号不对", "keywords": [], "usage": "normal", "source": "e99"},
        {"topic": "x", "keywords": [], "usage": "normal", "source": "e1"},
        # 机器人推送的内容不是群友在聊的。
        {"topic": "地震快讯", "keywords": ["地震"], "usage": "normal", "source": "e3"},
    ],
}


@pytest.mark.unit
def test_group_extraction_keeps_topic_words_with_usage(temp_data_dir) -> None:
    store = new_store(temp_data_dir)
    _seed(store, GROUP)
    counters = asyncio.run(
        consolidate_scope(store, CFG, deps(FakeModel(_TOPICS)), GROUP, force=True, rewrites=0)
    )
    kept = store.scope_state(GROUP)["topics"]
    assert [(row["topic"], row["usage"]) for row in kept] == [
        ("芙莉莲二期", "normal"),
        ("和室友吵架", "avoid"),
    ]
    assert kept[0]["keywords"] == ["新番", "打斗", "第五集"] and kept[0]["at"] == T0
    assert counters["topics.heard"] == 2 and counters["extract.rejected"] == 3


@pytest.mark.unit
def test_private_chats_never_produce_topic_words(temp_data_dir) -> None:
    store = new_store(temp_data_dir)
    _seed(store, f"u{ALI}")
    model = FakeModel(_TOPICS)
    asyncio.run(consolidate_scope(store, CFG, deps(model), f"u{ALI}", force=True, rewrites=0))
    assert "topics" not in model.calls[0][0]["content"]
    assert not store.scope_state(f"u{ALI}").get("topics")


@pytest.mark.unit
def test_recall_gives_only_fresh_normal_topics_in_the_group(temp_data_dir) -> None:
    store = new_store(temp_data_dir)
    _seed(store, GROUP)
    asyncio.run(
        consolidate_scope(store, CFG, deps(FakeModel(_TOPICS)), GROUP, force=True, rewrites=0)
    )
    payload = {"purpose": "reply", "text": "在吗", "speaker": {"user_id": ALI, "name": "阿梨"}}
    result = recall(store, CFG, payload, caller="xiaoqing_chat", scope=GROUP, now=T0 + DAY)
    assert result["topics"] == [{"topic": "芙莉莲二期", "keywords": ["新番", "打斗", "第五集"]}]
    stale = recall(store, CFG, payload, caller="xiaoqing_chat", scope=GROUP, now=T0 + 4 * DAY)
    assert stale["topics"] == []
    # 她主动开口时拿到的是这个群还记着的全部话题词（过了两三天的也在），私聊照样没有
    speaking = {"purpose": "proactive"}
    later = recall(store, CFG, speaking, caller="xiaoqing_chat", scope=GROUP, now=T0 + 4 * DAY)
    assert later["topics"] == [{"topic": "芙莉莲二期", "keywords": ["新番", "打斗", "第五集"]}]
    alone = recall(store, CFG, speaking, caller="xiaoqing_chat", scope=f"u{ALI}", now=T0 + DAY)
    assert "topics" not in alone
    private = recall(store, CFG, payload, caller="xiaoqing_chat", scope=f"u{ALI}", now=T0 + DAY)
    assert "topics" not in private
    task = recall(
        store, CFG, {**payload, "purpose": "task"}, caller="xiaoqing_agent", scope=GROUP, now=T0
    )
    assert "topics" not in task


@pytest.mark.unit
def test_same_topic_refreshes_and_keeps_the_more_careful_usage() -> None:
    first  = [topics_mod.clean({"topic": "考研 复试", "keywords": ["复试"]}, T0)]
    again  = [topics_mod.clean({"topic": "考研复试", "keywords": ["调剂"], "usage": "tone"}, T0 + 5)]
    merged = topics_mod.merge(topics_mod.merge([], first, T0), again, T0 + 5)
    assert len(merged) == 1
    assert merged[0]["at"] == T0 + 5 and merged[0]["keywords"] == ["复试", "调剂"]
    assert merged[0]["usage"] == "tone"
    old = topics_mod.merge(merged, [], T0 + 15 * DAY)
    assert old == []
    many = topics_mod.merge(
        [], [topics_mod.clean({"topic": f"话题{i}"}, T0 + i) for i in range(30)], T0 + 30
    )
    assert len(many) == topics_mod.MAX_KEPT and many[-1]["topic"] == "话题29"


@pytest.mark.unit
def test_forgetting_the_group_drops_its_topic_words(temp_data_dir) -> None:
    store = new_store(temp_data_dir)
    _seed(store, GROUP)
    asyncio.run(
        consolidate_scope(store, CFG, deps(FakeModel(_TOPICS)), GROUP, force=True, rewrites=0)
    )
    store.forget_scope(GROUP)
    assert store.scope_state(GROUP)["topics"] == []


def test_heard_gives_every_topic_the_group_still_remembers() -> None:
    now  = 1_800_000_000.0
    rows = [
        {"topic": f"话题{n}", "keywords": ["词"], "usage": "normal", "at": now - n * 86400}
        for n in range(8)
    ]
    rows.append({"topic": "谁的伤心事", "keywords": [], "usage": "avoid", "at": now})
    rows.append({"topic": "很久以前", "keywords": [], "usage": "normal", "at": now - 20 * 86400})
    assert [row["topic"] for row in topics_mod.fresh(rows, now)] == ["话题0", "话题1", "话题2"]
    heard = topics_mod.heard(rows, now)
    assert [row["topic"] for row in heard] == [f"话题{n}" for n in range(8)]
    assert heard[0] == {"topic": "话题0", "keywords": ["词"]}
