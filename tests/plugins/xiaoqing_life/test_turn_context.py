"""聊天侧取用：问近况、提过去、话题重合、无关不给；听懂与可以用；去回声；冷却；入队。"""

from __future__ import annotations

import random
from datetime import datetime
from pathlib import Path
from typing import Any

from plugins.xiaoqing_life import turn_context as tc
from plugins.xiaoqing_life.stores import LifeStore
from tests.plugins.xiaoqing_life.support import at, make_cfg

MOMENT = at(14, 0)
NOW    = MOMENT.timestamp()
HOUR   = 3600.0
DAY    = 86400.0


class Rng(random.Random):
    def __init__(self, value: float) -> None:
        super().__init__(0)
        self.value = value

    def random(self) -> float:
        return self.value


def _store(tmp_path: Path) -> LifeStore:
    store = LifeStore(tmp_path / "life")
    store.load()
    return store


def _episode(n: int, title: str, ago: float, **kw: Any) -> dict[str, Any]:
    return {
        "id"       : f"e{n}",
        "at"       : NOW - ago,
        "day"      : datetime.fromtimestamp(NOW - ago, MOMENT.tzinfo).strftime("%Y-%m-%d"),
        "activity" : "hot_scroll",
        "source"   : "bili_popular",
        "item_id"  : f"bili_popular:{n}",
        "title"    : title,
        "reaction" : "挺有意思的",
        "feeling"  : "like",
        "share"    : False,
        "topics"   : [],
        "term_keys": [],
        "thread_id": "",
        **kw,
    }


def _term(
    store: LifeStore, name: str, meaning: str, *, status="known", seen_ago=DAY, **kw: Any
) -> None:
    store.terms[name] = {
        "term"          : name,
        "meaning"       : meaning,
        "usage"         : kw.pop("usage", ""),
        "tone"          : "夸",
        "status"        : status,
        "verified"      : False,
        "sources"       : ["a", "b"],
        "first_seen"    : NOW - 5 * DAY,
        "last_seen"     : NOW - seen_ago,
        "last_suggested": 0.0,
        **kw,
    }


def _call(
    store, text="", *, recent=(), bot_recent=(), unknown=(), rng=0.99, cfg=None, mode="reply"
):
    payload = {
        "version"      : 1,
        "mode"         : mode,
        "text"         : text,
        "recent"       : list(recent),
        "bot_recent"   : list(bot_recent),
        "is_private"   : False,
        "unknown_terms": list(unknown),
    }
    return tc.turn_context(store, cfg or make_cfg(), payload, MOMENT, Rng(rng))


def test_asking_about_recent_life_gives_up_to_three_recent_episodes_share_first(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.episodes += [
        _episode(1, "猫猫的一天", 2 * HOUR),
        _episode(2, "新显卡评测", 5 * HOUR, share=True),
        _episode(3, "月全食直播", 30 * HOUR),
        _episode(4, "今年的最后一场雨", 20 * HOUR),
        _episode(5, "三天前的视频", 3 * DAY),
    ]
    store.state["diary"] = {"day": "2026-11-05", "text": "今天刷了不少猫", "episode_ids": ["e1"]}
    response, _ = _call(store, "最近刷到啥好玩的了吗")
    lines = response["life_lines"]
    assert lines.startswith("你最近上网看到的") and "细节记不清就说记不清" in lines
    assert lines.count("「") == 3 and "三天前的视频" not in lines
    assert lines.index("新显卡评测") < lines.index("猫猫的一天")  # share 优先
    assert "今天 12:00 刷B站热门，点开了「猫猫的一天」" in lines
    assert "今天大概：今天刷了不少猫" in lines
    assert response["episode_ids"] == ["e2", "e1", "e4"]


def test_referring_to_the_past_widens_to_a_week_with_summaries_after_detail_days(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.episodes += [_episode(1, "昨天的视频", 28 * HOUR), _episode(2, "上周的视频", 8 * DAY)]
    near, _ = _call(store, "你昨天看了什么")
    assert "昨天 10:00 刷B站热门，点开了「昨天的视频」" in near["life_lines"]
    cfg = make_cfg(episode_detail_days=3)
    store.episodes.append(_episode(3, "五天前的视频", 5 * DAY))
    far, _ = _call(store, "前几天你看了啥", cfg=cfg)
    assert (
        "五天前的视频」（细节记不清了）" in far["life_lines"]
        and "你当时觉得" not in far["life_lines"].split("五天前")[1]
    )
    assert "上周的视频" not in far["life_lines"]  # 8 天超出一周


def test_topic_overlap_surfaces_one_recent_episode_and_unrelated_chat_gets_nothing(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.episodes += [
        _episode(1, "猎户座大星云拍摄教程", 6 * HOUR, topics=["天文摄影"]),
        _episode(2, "新显卡评测", 3 * HOUR, topics=["装机"]),
        _episode(3, "很久以前的天文视频", 5 * DAY, topics=["天文摄影"]),
    ]
    response, _ = _call(store, "我周末想试试天文摄影")
    assert (
        "猎户座大星云拍摄教程" in response["life_lines"] and "新显卡" not in response["life_lines"]
    )
    assert response["life_lines"].count("「") == 1 and "很久以前" not in response["life_lines"]
    assert _call(store, "今天中午吃什么好呢")[0] is None
    assert _call(store, "最近刷到啥好玩的", cfg=make_cfg())[0] is not None


def test_episodes_she_just_talked_about_are_not_offered_again(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.episodes.append(_episode(1, "猫猫的一天", 2 * HOUR))
    response, _ = _call(store, "最近刷到啥好玩的", bot_recent=["我刚看了个猫猫的一天，笑死"])
    assert response is None


def test_threads_are_phrased_by_state(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.threads += [
        {
            "id"      : "t1",
            "title"   : "猎户座大星云怎么拍",
            "why"     : "想拍星云",
            "status"  : "open",
            "steps"   : 1,
            "notes"   : [],
            "takeaway": "",
        },
        {
            "id"      : "t2",
            "title"   : "这个梗哪来的",
            "why"     : "好奇",
            "status"  : "done",
            "steps"   : 3,
            "notes"   : [],
            "takeaway": "来自某个直播切片",
        },
    ]
    asking, _ = _call(store, "最近在忙什么")
    assert "你在查的小事：猎户座大星云怎么拍（还没查完，可以说在研究）" in asking["life_lines"]
    done, _ = _call(store, "你知道这个梗哪来的吗")
    assert "你前几天查过：这个梗哪来的，收获：来自某个直播切片" in done["life_lines"]
    store.threads[0]["status"] = "reviewing"
    reviewing, _ = _call(store, "最近在忙什么")
    assert "还没查完" not in reviewing["life_lines"] and "查得差不多了" in reviewing["life_lines"]


def test_understood_terms_come_from_the_message_or_recent_turns_and_include_fading(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _term(store, "哈基米", "对猫咪的爱称", usage="看到猫时感叹")
    _term(store, "南北绿豆", "对哈基米的另一种说法", status="fading")
    _term(store, "没出现", "不相关")
    response, _ = _call(store, "你看这个哈基米", recent=["刚才有人提到南北绿豆"])
    notes = response["slang_notes"]
    assert notes.startswith("你在网上学到的说法") and "不解释梗" in notes
    assert "「哈基米」：对猫咪的爱称，看到猫时感叹" in notes and "「南北绿豆」" in notes
    assert "没出现" not in notes and "可以顺手用的" not in notes  # 掷骰没中不推荐
    assert set(response["term_keys"]) == {"哈基米", "南北绿豆"}


def test_usable_term_needs_known_fresh_relevant_unspoken_and_cooled(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _term(store, "哈基米", "对猫咪的爱称", usage="看到猫图拍得好可爱时感叹")
    ask = "你家猫图拍得好可爱，是什么猫"
    response, dirty = _call(store, ask, rng=0.0)
    assert "可以顺手用的：「哈基米」" in response["slang_notes"] and dirty
    assert store.terms["哈基米"]["last_suggested"] == NOW
    assert _call(store, ask, rng=0.0)[0] is None  # 冷却期内不再推
    store.terms["哈基米"]["last_suggested"] = NOW - 7 * HOUR
    assert _call(store, ask, rng=0.0)[0] is not None  # 过了冷却
    store.terms["哈基米"]["last_suggested"] = 0.0
    assert _call(store, ask, bot_recent=["哈基米哈基米"], rng=0.0)[0] is None  # 她刚说过
    assert _call(store, "完全无关的话题讨论", rng=0.0)[0] is None
    store.terms["哈基米"]["status"] = "fading"
    assert _call(store, ask, rng=0.0)[0] is None
    store.terms["哈基米"] |= {"status": "known", "last_seen": NOW - 22 * DAY}
    assert _call(store, ask, rng=0.0)[0] is None  # 21 天没看到不推
    store.terms["哈基米"]["last_seen"] = NOW - DAY
    assert _call(store, ask, rng=0.99)[0] is None  # 概率没中
    assert _call(store, ask, cfg=make_cfg(active_use_probability=0.0), rng=0.0)[0] is None


def test_expression_hint_needs_situation_overlap_and_respects_cooldown(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.expressions.append(
        {
            "id"            : "x1",
            "situation"     : "当想夸一件事又顺带损一下时",
            "pattern"       : "X基础，Y不基础",
            "kind"          : "phrase",
            "sources"       : ["a", "b"],
            "first_seen"    : NOW - DAY,
            "last_seen"     : NOW - DAY,
            "last_suggested": 0.0,
        }
    )
    response, dirty = _call(store, "这个手机想夸一下又想损一下", rng=0.0)
    assert response["expression_hint"] == "当想夸一件事又顺带损一下时：X基础，Y不基础" and dirty
    assert _call(store, "这个手机想夸一下又想损一下", rng=0.0)[0] is None
    assert _call(store, "今天天气不错", rng=0.0)[0] is None


def test_unknown_terms_are_queued_without_session_info_and_deduplicated(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _term(store, "哈基米", "已知")
    response, dirty = _call(
        store, "你好", unknown=["绝绝子", "绝绝子", "哈基米", "nmsl", "加微信领福利", " "]
    )
    assert response is None and dirty
    assert store.state["lookup_queue"] == ["绝绝子"]
    off = make_cfg(sources={"baike_lookup": False})
    store.state["lookup_queue"] = []
    assert _call(store, "你好", unknown=["绝绝子"], cfg=off)[1] is False
    assert store.state["lookup_queue"] == []
    for n in range(30):
        tc.enqueue_unknown(store, [f"词{n}"], make_cfg())
    assert len(store.state["lookup_queue"]) == 20


def test_proactive_mode_returns_no_candidates_for_now_and_bad_payloads_are_tolerated(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.episodes.append(_episode(1, "猫猫的一天", HOUR, share=True))
    proactive = _call(store, "", mode="proactive")[0]
    assert proactive["share_candidates"][0]["episode_id"] == "e1" and proactive["life_lines"] == ""
    payload = {
        "text"         : 5,
        "recent"       : "x",
        "bot_recent"   : [1, None],
        "unknown_terms": {"a": 1},
        "mode"         : 3,
    }
    response, dirty = tc.turn_context(store, make_cfg(), payload, MOMENT, Rng(0.9))
    assert response is None and not dirty
    clean = tc.clean_payload(
        {
            "text"         : "x" * 900,
            "recent"       : ["a"] * 20,
            "unknown_terms": [f"{n}" + "长" * 30 for n in range(9)],
        }
    )
    assert len(clean["text"]) == 500 and len(clean["recent"]) == 8 and len(clean["unknown"]) == 3
    assert len(clean["unknown"][0]) == 12
