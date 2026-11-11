"""聊天交给网上生活的信号：群里最近的话题词、没确认意思的说法；收表情留痕。"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from plugins.xiaoqing_chat.config.config import XiaoQingChatConfig
from plugins.xiaoqing_chat.expression.bw_jargon_store import JargonRecord, JargonStore
from plugins.xiaoqing_chat.life import online
from plugins.xiaoqing_chat.media import event_media

NOW = time.time()
DAY = 86400.0


def _summaries(tmp_path: Path, chat_id: str, rows: list[dict[str, Any]]) -> None:
    path = tmp_path / "hippo_memorizer" / f"{chat_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")


def _row(topic: str, summary: str, *, ago: float = 3600.0, mention: str = "normal") -> dict:
    return {
        "topic_id"  : topic,
        "topic"     : topic,
        "summary"   : summary,
        "keywords"  : ["关键词一", "关键词二", "关键词三", "第四个"],
        "key_points": [],
        "updated_at": NOW - ago,
        "mention"   : mention,
    }


def test_group_topics_only_pass_fresh_topics_that_can_be_brought_up(tmp_path: Path) -> None:
    _summaries(
        tmp_path,
        "g1",
        [
            _row("星穹铁道新版本", "大家在聊新角色和抽卡"),
            _row("期末复习", "有人说复习不完", ago=4 * DAY),  # 太久了
            _row("吵架的事", "两个人吵起来了", mention="avoid"),
            _row("宠物去世", "朋友的猫去世了", mention=""),  # 没标注时按伤心事只影响语气
            _row("新番推荐", "推荐了几部新番"),
        ],
    )
    topics = online.group_topics(tmp_path, "g1", now=NOW)
    assert [t["topic"] for t in topics] == ["新番推荐", "星穹铁道新版本"]  # 新的在前
    assert topics[0]["keywords"] == ["关键词一", "关键词二", "关键词三"]
    assert "summary" not in topics[0]  # 不给摘要和原话
    assert online.group_topics(tmp_path, "g-none", now=NOW) == []


def test_request_carries_topics_only_for_group_chats() -> None:
    topics = [{"topic": "新番推荐", "keywords": []}]
    group  = online.build_request(
        text="t", history=[], is_private=False, unknown_words=[], topics=topics
    )
    private = online.build_request(
        text="t", history=[], is_private=True, unknown_words=[], topics=topics
    )
    assert group["topics"] == topics and "topics" not in private
    dup = online.build_request(text="t", history=[], is_private=False, unknown_words=["梗", "梗"])
    assert dup["unknown_terms"] == ["梗"]


def test_unconfirmed_jargon_in_this_turn_goes_to_life(tmp_path: Path) -> None:
    store = JargonStore()
    store.bind(tmp_path)
    store.save(
        [
            JargonRecord(content="依托答辩", scope_chat_id="g1", count=3),
            JargonRecord(content="绝绝子", scope_chat_id="g1", meaning="很棒", confirmed=True),
            JargonRecord(content="别群的梗", scope_chat_id="g2", count=3),
            JargonRecord(content="没出现", scope_chat_id="g1", count=3),
        ]
    )
    state = SimpleNamespace(bw_jargon_store=store)
    text = "这也太依托答辩了，绝绝子，别群的梗"
    assert online.unconfirmed_jargon(state, tmp_path, "g1", text) == ["依托答辩"]


def test_life_signals_never_break_the_reply(tmp_path: Path) -> None:
    broken = SimpleNamespace(bw_jargon_store=None)  # 读不了就当没有
    assert online.life_signals(broken, tmp_path, "g1", "x", is_private=False) == ([], [])
    _summaries(tmp_path, "g1", [_row("新番推荐", "推荐了几部新番")])
    topics, words = online.life_signals(None, tmp_path, "g1", "x", is_private=True)
    assert (topics, words) == ([], [])  # 私聊不给话题


class _Capability:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    async def turn_context(self, payload: dict[str, Any]) -> Any:
        self.requests.append(payload)
        return None


@pytest.mark.asyncio
async def test_fetch_online_life_sends_group_topics_and_jargon(tmp_path: Path) -> None:
    _summaries(tmp_path, "g1", [_row("新番推荐", "推荐了几部新番")])
    store = JargonStore()
    store.bind(tmp_path)
    store.save([JargonRecord(content="依托答辩", scope_chat_id="g1", count=3)])
    capability = _Capability()
    context    = SimpleNamespace(
        capabilities=SimpleNamespace(life_context=capability),
        data_dir   = tmp_path,
        request_id = "r",
        logger     = MagicMock(),
    )
    runtime = SimpleNamespace(cfg=XiaoQingChatConfig())
    await online.fetch_online_life(
        context,
        runtime,
        text          = "这番依托答辩",
        history       = [],
        is_private    = False,
        unknown_words = [],
        chat_id       = "g1",
        state=SimpleNamespace(bw_jargon_store=store),
    )
    request = capability.requests[0]
    assert request["topics"][0]["topic"] == "新番推荐"
    assert request["unknown_terms"] == ["依托答辩"]


@pytest.mark.asyncio
async def test_collected_emoji_is_logged_and_failures_are_not_silent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = SimpleNamespace(cfg=XiaoQingChatConfig())
    context = SimpleNamespace(logger=MagicMock())
    rendered = SimpleNamespace(
        kind="emoji", cached_path=tmp_path / "a.png", media_hash="abcdef1234567890", marker="m"
    )
    event = {"message_id": 1, "group_id": 2, "user_id": 3}
    monkeypatch.setattr(event_media, "collect_emoji_candidate", lambda *a, **k: (object(), True))
    assert await event_media._collected_new_emoji(rendered, event, context=context, runtime=runtime)
    logged = context.logger.info.call_args[0][1]
    assert '"media.emoji.collect"' in logged and '"new": true' in logged

    def boom(*_: Any, **__: Any) -> Any:
        raise OSError("disk")

    monkeypatch.setattr(event_media, "collect_emoji_candidate", boom)
    assert not await event_media._collected_new_emoji(
        rendered, event, context=context, runtime=runtime
    )
    warned = context.logger.warning.call_args[0][1]
    assert '"media.emoji.collect_error"' in warned and "OSError" in warned


def test_terms_life_verified_become_confirmed_group_jargon(tmp_path: Path) -> None:
    store = JargonStore()
    store.bind(tmp_path)
    store.save(
        [
            JargonRecord(content="city不city", scope_chat_id="g1", meaning="猜的意思", count=2),
            JargonRecord(
                content="绝绝子", scope_chat_id="g1", meaning="本群的意思", confirmed=True
            ),
        ]
    )
    state = SimpleNamespace(bw_jargon_store=store)
    rows = [("city不city", "时髦不时髦（百科）"), ("绝绝子", "百科的意思"), ("没有这条", "x")]
    assert online.confirm_jargon(state, tmp_path, "g1", rows) == 1
    records = store.load()
    fresh   = records[JargonStore.key_for("city不city", "g1")]
    assert (fresh.meaning, fresh.confirmed, fresh.source) == ("时髦不时髦（百科）", True, "百科")
    assert records[JargonStore.key_for("绝绝子", "g1")].meaning == "本群的意思"  # 已确认的不动
    parsed = online.parse_response(
        {"life_lines": "", "verified_terms": [{"term": "city不city", "meaning": "m"}, "bad"]}
    )
    assert parsed.verified_terms == (("city不city", "m"),)
