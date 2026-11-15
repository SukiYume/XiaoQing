"""她的自我页和网上生活这一轮给的细节说的是同一条经历时，只说一遍：留网上生活的细节，去掉自我页里引用
到这条经历的那一行（一行常概括好几条经历）；自我页里另有依据（她在聊天里说过）的行和一句话简介照常保留。

三个插件都用真的：life 把经历发布进统一记忆，记忆取用给出自我页，life 的 ``turn_context`` 选出这一轮
的经历，聊天拼生活块。
"""

from __future__ import annotations

import random
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.xiaoqing_chat.config.config import XiaoQingChatConfig
from plugins.xiaoqing_chat.life.online import parse_response
from plugins.xiaoqing_chat.memory.client import parse_recall
from plugins.xiaoqing_chat.reply_plan import _load_life_blocks, _merge_online_life
from plugins.xiaoqing_life import memory_bridge
from plugins.xiaoqing_life import turn_context as tc
from plugins.xiaoqing_memory.model import GLOBAL, Note, Page, PageLine
from plugins.xiaoqing_memory.ops import apply_record
from plugins.xiaoqing_memory.recall import recall
from plugins.xiaoqing_memory.scopes import SELF_SUBJECT
from tests.plugins.xiaoqing_life.support import at, make_cfg
from tests.plugins.xiaoqing_life.support import new_store as new_life_store
from tests.plugins.xiaoqing_memory.support import CFG, life_system
from tests.plugins.xiaoqing_memory.support import new_store as new_memory_store

MOMENT = at(14, 0)
NOW    = MOMENT.timestamp()
TITLE  = "猫猫打太极合集"


def _life(tmp_path: Path):
    store = new_life_store(tmp_path)
    store.episodes.append(
        {
            "id"       : "ep-cat",
            "at"       : NOW - 3600,
            "activity" : "hot_scroll",
            "source"   : "bili_popular",
            "item_id"  : "bili_popular:1",
            "title"    : TITLE,
            "uploader" : "喵星观察站",
            "reaction" : "笑到停不下来",
            "feeling"  : "like",
            "share"    : True,
            "topics"   : ["猫"],
            "term_keys": [],
            "thread_id": "",
        }
    )
    # 三天前看过、不打算分享的一条：这一轮不会给出细节，但自我页的概括行会引用它。
    store.episodes.append(
        {
            "id"       : "ep-dog",
            "at"       : NOW - 3 * 86400,
            "activity" : "hot_scroll",
            "source"   : "bili_popular",
            "item_id"  : "bili_popular:2",
            "title"    : "柴犬拆家实录",
            "uploader" : "汪汪日记",
            "reaction" : "看完想养狗",
            "feeling"  : "like",
            "share"    : False,
            "topics"   : ["狗"],
            "term_keys": [],
            "thread_id": "",
        }
    )
    return store


def _episode(store, key: str) -> Note:
    return next(
        note
        for note in store.notes(GLOBAL).values()
        if note.kind == "episode" and note.sources[0].get("key") == key
    )


def _memory(tmp_path: Path, life_store, *, summary_line: bool = False):
    store = new_memory_store(tmp_path)
    ops   = memory_bridge.pending_ops(life_store, make_cfg(), 0.0)
    apply_record(store, {"events": [], "ops": ops}, life_system(), GLOBAL, now=NOW - 1800)
    episode = _episode(store, "ep-cat")
    dog     = _episode(store, "ep-dog")
    said    = Note(
        id          = "n-said",
        kind        = "self.state",
        scope       = GLOBAL,
        subject     = SELF_SUBJECT,
        text        = "这周在准备期中考试",
        origin      = "self",
        sources     = ({"type": "event", "scope": "g1", "seq": 3, "origin": "self"},),
        created_at  = NOW - 7200,
        observed_at = NOW - 7200,
        expires_at  = NOW + 86400,
    )
    store.put_notes(GLOBAL, [said])
    interests = (
        PageLine(text="最近爱刷猫猫狗狗的视频", note_ids=(episode.id, dog.id))
        if summary_line
        else PageLine(text="最近爱刷猫猫视频", note_ids=(episode.id,))
    )
    store.put_page(
        Page(
            subject        = SELF_SUBJECT,
            scope          = GLOBAL,
            brief          = "我是小青，爱刷萌宠视频。",
            brief_note_ids = (episode.id, dog.id),
            sections       = {
                "doing"    : (PageLine(text="这周在准备期中考试", note_ids=("n-said",)),),
                "interests": (interests,),
            },
            updated_at=NOW - 600,
        )
    )
    return store


def _chat_runtime():
    cfg                            = XiaoQingChatConfig()
    cfg.life.enable_schedule_state = False
    cfg.life.enable_daily_life     = False
    cfg.life.enable_open_loops     = False
    return SimpleNamespace(cfg=cfg)


def _life_block(
    tmp_path: Path, text: str, *, dedupe: bool = True, summary_line: bool = False
) -> str:
    life_store = _life(tmp_path / "life")
    memory = _memory(tmp_path / "mem", life_store, summary_line=summary_line)
    recalled = parse_recall(
        recall(
            memory,
            CFG,
            {"purpose": "reply", "text": text, "speaker": {"user_id": 7, "name": "阿梨"}},
            caller = "xiaoqing_chat",
            scope  = "g1",
            now    = NOW,
        )
    )
    raw, _ = tc.turn_context(
        life_store,
        make_cfg(),
        {"version": 1, "mode": "reply", "text": text, "recent": [], "bot_recent": []},
        MOMENT,
        random.Random(0),
    )
    online = parse_response(raw)
    block, _loop = _load_life_blocks(
        runtime      = _chat_runtime(),
        state        = SimpleNamespace(),
        event        = {},
        text         = text,
        recalled     = recalled,
        online_shown = online.shown_keys if dedupe else (),
    )
    merged, _, _ = _merge_online_life(online, block, "", "")
    return merged


@pytest.mark.unit
def test_episode_life_already_detailed_is_not_repeated_by_the_self_page(tmp_path: Path) -> None:
    block = _life_block(tmp_path, "最近刷到啥好玩的")
    assert TITLE in block and "喵星观察站" in block  # 网上生活给的细节在
    assert "最近爱刷猫猫视频" not in block  # 只引用这条经历的自我页行不再放
    assert "这周在准备期中考试" in block  # 她在聊天里说过的照常有


@pytest.mark.unit
def test_without_the_shown_keys_the_same_episode_would_appear_twice(tmp_path: Path) -> None:
    block = _life_block(tmp_path, "最近刷到啥好玩的", dedupe=False)
    assert TITLE in block and "最近爱刷猫猫视频" in block


@pytest.mark.unit
def test_self_page_line_stays_when_life_gives_nothing_this_turn(tmp_path: Path) -> None:
    block = _life_block(tmp_path, "你作业写完了吗")
    assert TITLE not in block
    assert "最近爱刷猫猫视频" in block and "这周在准备期中考试" in block


@pytest.mark.unit
def test_summary_line_goes_when_one_of_its_episodes_is_detailed(tmp_path: Path) -> None:
    # 整理出来的自我页一行常概括几条经历；其中一条这一轮给了细节，概括行就先不放。
    block = _life_block(tmp_path, "最近刷到啥好玩的", summary_line=True)
    assert TITLE in block and "柴犬拆家实录" not in block
    assert "最近爱刷猫猫狗狗的视频" not in block
    assert "我是小青，爱刷萌宠视频。" in block  # 一句话简介照常有
    assert "这周在准备期中考试" in block


@pytest.mark.unit
def test_summary_line_stays_when_none_of_its_episodes_is_detailed(tmp_path: Path) -> None:
    block = _life_block(tmp_path, "你作业写完了吗", summary_line=True)
    assert "最近爱刷猫猫狗狗的视频" in block and "我是小青，爱刷萌宠视频。" in block
