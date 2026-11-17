"""聊天交给网上生活的信号：记忆给的群话题词、没确认意思的说法；收表情留痕。"""

from __future__ import annotations

import hashlib
import logging
import shutil
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image

from plugins.xiaoqing_chat.config.config import XiaoQingChatConfig
from plugins.xiaoqing_chat.expression.bw_jargon_store import JargonRecord, JargonStore
from plugins.xiaoqing_chat.life import online
from plugins.xiaoqing_chat.media import event_media
from plugins.xiaoqing_chat.media.models import RenderedMedia
from tests.helpers.assertions import logged_events
from tests.helpers.xiaoqing_chat_media_test_support import make_media_runtime

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


def _received_emoji(path: Path, color: str, *, striped: bool = False) -> RenderedMedia:
    image = Image.new("RGB", (24, 24), color)
    if striped:  # 画面和纯色图明显不同，不会被当成已收录表情的相似图。
        image.paste("black", (0, 0, 12, 24))
    image.save(path)
    return RenderedMedia(
        media_hash   = hashlib.sha256(path.read_bytes()).hexdigest(),
        kind         = "emoji",
        description  = f"{color}色猫猫",
        emotion_tags = ("无语",),
        marker       = "[表情包：无语]",
        cached_path  = path,
    )


@pytest.mark.asyncio
async def test_collected_emoji_is_logged_and_failures_are_not_silent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="plugins.xiaoqing_chat")
    runtime = make_media_runtime(enable_auto_collect_inbound_emoji=True)
    context = SimpleNamespace(data_dir=tmp_path)
    event    = {"message_id": 1, "group_id": 2, "user_id": 3}
    received = _received_emoji(tmp_path / "red.png", "red")
    assert await event_media._collected_new_emoji(received, event, context=context, runtime=runtime)
    (collected,) = logged_events(caplog, "media.emoji.collect")
    assert collected.fields["new"] is True

    def boom(*_: Any, **__: Any) -> Any:
        raise OSError("disk")

    # 另一张表情要落盘进表情库时磁盘出错：这一轮照常继续，但要留下警告。
    monkeypatch.setattr(shutil, "copyfile", boom)
    failing = _received_emoji(tmp_path / "blue.png", "white", striped=True)
    assert not await event_media._collected_new_emoji(
        failing, event, context=context, runtime=runtime
    )
    (warned,) = logged_events(caplog, "media.emoji.collect_error")
    assert warned.fields["error"] == "OSError"


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
