"""聊天交给网上生活的信号：记忆给的群话题词、没确认意思的说法；收表情留痕。"""

from __future__ import annotations

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
    assert "explained_terms" not in dup  # 本群黑话块没解释什么时不带
    explained = online.build_request(
        text            = "t",
        history         = [],
        is_private      = True,
        unknown_words   = [],
        explained_terms = ("绝绝子", "绝绝子"),
    )
    assert explained["explained_terms"] == ["绝绝子"]


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


def test_unreadable_jargon_store_never_breaks_the_reply(tmp_path: Path) -> None:
    broken = SimpleNamespace(bw_jargon_store=None)  # 读不了就当没有
    with pytest.raises(AttributeError):
        online.unconfirmed_jargon(broken, tmp_path, "g1", "x")


class _Capability:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    async def turn_context(self, payload: dict[str, Any]) -> Any:
        self.requests.append(payload)
        return None


@pytest.mark.asyncio
async def test_fetch_online_life_sends_group_topics_and_jargon(tmp_path: Path) -> None:
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
        text       = "这番依托答辩",
        history    = [],
        is_private = False,
        chat_id    = "g1",
        state=SimpleNamespace(bw_jargon_store=store),
        topics=({"topic": "新番推荐", "keywords": ["新番"]},),
    )
    await online.fetch_online_life(
        context,
        runtime,
        text       = "这番依托答辩",
        history    = [],
        is_private = False,
        chat_id    = "g1",
        state=SimpleNamespace(bw_jargon_store=None),  # 读不了黑话库也照常取
    )
    assert len(capability.requests) == 2 and "topics" not in capability.requests[1]
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
