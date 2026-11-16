"""带媒体回复和持久化。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from unittest.mock import AsyncMock, patch

import pytest

import tests.helpers.xiaoqing_chat_test_support as _fixture_support
from core.ai import AIModelInfo
from plugins.xiaoqing_chat.handlers import handle_smalltalk
from plugins.xiaoqing_chat.store_binding import bound_state
from tests.helpers.reply_checker_test_support import checker_verdict
from tests.helpers.xiaoqing_chat_media_test_support import (
    animated_gif_bytes,
    message_parts_to_legacy,
    write_png,
)
from tests.helpers.xiaoqing_chat_test_support import (
    GROUP_GATE_CLOSED,
    ScriptedChatModel,
    ack_reply_delivery,
    chat_message_event,
    scripted_chat_context,
    settle_chat_background,
)

mock_context       = _fixture_support.mock_context
sample_group_event = _fixture_support.sample_group_event

_GROUP_CHAT   = "g67890"
_PRIVATE_CHAT = "u12345"
# 不额外起后台的识图精修和梗解读，这些轮次里模型只被问到剧本里写了的事。
_QUIET_MEDIA = {
    "media": {"enable_emoji_refine_background": False, "enable_meme_cultural_hint": False}
}


def _seed_emoji_library(context, chat_id: str, *, name: str, marker: str, tags: list[str]) -> str:
    """在表情库里放一张已审核通过、本会话可用的表情，返回它的哈希。"""

    library_dir = context.data_dir / "media" / "library"
    image_path  = write_png(library_dir / f"{name}.png")
    media_hash  = hashlib.sha256(image_path.read_bytes()).hexdigest()
    record      = {
        "media_hash"     : media_hash,
        "file_path"      : f"media/library/{image_path.name}",
        "description"    : name,
        "emotion_tags"   : tags,
        "usage_count"    : 0,
        "last_used_ts"   : 0.0,
        "marker"         : marker,
        "status"         : "active",
        "source"         : "manual",
        "source_chat_ids": [chat_id],
        "visibility"     : "chat",
    }
    (library_dir / "index.json").write_text(
        json.dumps({"entries": {media_hash: record}}, ensure_ascii=False), encoding="utf-8"
    )
    return media_hash


async def _take_turn(
    context,
    event,
    *,
    text: str         = "你好",
    reply: str | None = None,
    gate: dict | None = None,
    sticker: bool     = False,
) -> tuple[list[dict], ScriptedChatModel]:
    """走完整一轮闲聊：模型按剧本回答 ``reply``，发出了消息就确认送达。

    ``sticker`` 表示入站消息里带一张动画表情：下载返回一张动图，识图模型把它认作一张“开心”表情（和库里预置的“无语”表情不是同一张）；
    否则不分析入站媒体。返回发出的消息段和剧本模型，收尾由调用方负责。
    """

    script = {} if reply is None else {"reply": reply, "checker": checker_verdict(reason="ok")}
    if sticker:
        script["vision"] = (
            '{"kind":"emoji","description":"一只狗开心地跳起来","emotion_tags":["开心"]}'
        )
    model = ScriptedChatModel(**script)
    if sticker:
        model.models = (
            AIModelInfo("scripted-vision", "scripted", "scripted-vision", ("text", "image")),
        )
    config = [_QUIET_MEDIA, gate or {}]
    if not sticker:
        config.append({"media": {"enable_inbound_media_context": False}})
    scripted_chat_context(context, model, *config)
    download = AsyncMock(return_value=(animated_gif_bytes(), "image/gif"))
    with patch("plugins.xiaoqing_chat.media.sources._download_url_bytes", new=download):
        result = await handle_smalltalk(text, event, context)
    if result:
        await ack_reply_delivery(result)
    return result, model


async def _await_media_usage(context, kind: str, key: str) -> int:
    """送达确认后，媒体的使用次数在后台线程里登记到素材库；等它落盘后返回次数。"""

    media_dir = context.data_dir / "media"
    path      = media_dir / ("library/index.json" if kind == "emoji" else "qq_face_catalog.json")
    count     = 0
    for _ in range(500):
        if path.exists():
            entries = json.loads(path.read_text(encoding="utf-8")).get("entries", {})
            count = int(entries.get(key, {}).get("usage_count", 0))
        if count:
            break
        await asyncio.sleep(0.01)
    return count


async def _last_reply_parts(context, chat_id: str) -> tuple[dict, ...]:
    history = await bound_state(context).memory_store.get_async(chat_id)
    return history[-1].parts


@pytest.mark.asyncio
async def test_smalltalk_emoji_reply_returns_mixed_text_and_image_and_persists_marker(
    mock_context, sample_group_event
):
    media_hash = _seed_emoji_library(
        mock_context, _GROUP_CHAT, name="无语猫猫", marker="[表情包：无语]", tags=["无语"]
    )
    event = chat_message_event(sample_group_event, "你好", _xc_command_forced=True)

    result, model = await _take_turn(mock_context, event, reply="懂了[想发表情：无语]")

    assert mock_context.send_action.await_count == 0
    assert [segment["type"] for segment in result] == ["text", "emoji"]
    assert result[0]["data"]["text"] == "懂了"
    parts = await _last_reply_parts(mock_context, _GROUP_CHAT)
    content, media_items = message_parts_to_legacy(parts)
    assert content == "懂了[[xc_media_1]]"
    assert media_items[0]["kind"] == "emoji"
    assert media_items[0]["media_hash"] == media_hash
    assert media_items[0]["marker"] == "[表情包：无语]"
    assert await _await_media_usage(mock_context, "emoji", media_hash) == 1
    await settle_chat_background(mock_context, model)


@pytest.mark.asyncio
async def test_smalltalk_emoji_only_reply_returns_single_image_and_marker_memory(
    mock_context, sample_group_event
):
    media_hash = _seed_emoji_library(
        mock_context, _GROUP_CHAT, name="无语猫猫", marker="[表情包：无语]", tags=["无语"]
    )
    event = chat_message_event(
        sample_group_event,
        "",
        message=[
            {
                "type": "image",
                "data": {"url": "https://example.com/sticker.png", "summary": "[动画表情]"},
            }
        ],
        _xc_command_forced=True,
    )

    result, model = await _take_turn(
        mock_context, event, text="", reply="[想发表情：无语]", sticker=True
    )

    assert mock_context.send_action.await_count == 0
    assert [segment["type"] for segment in result] == ["emoji"]
    parts = await _last_reply_parts(mock_context, _GROUP_CHAT)
    content, media_items = message_parts_to_legacy(parts)
    assert content == "[[xc_media_1]]"
    assert media_items[0]["media_hash"] == media_hash
    assert media_items[0]["mode"] == "text_with_emoji"
    assert await _await_media_usage(mock_context, "emoji", media_hash) == 1
    await settle_chat_background(mock_context, model)


@pytest.mark.asyncio
async def test_smalltalk_face_reply_returns_mixed_text_and_face_and_persists_marker(
    mock_context, sample_group_event
):
    event = chat_message_event(sample_group_event, "你好", _xc_command_forced=True)

    result, model = await _take_turn(mock_context, event, reply="懂了[想发QQ表情：狗头]")

    assert mock_context.send_action.await_count == 0
    assert [segment["type"] for segment in result] == ["text", "face"]
    assert result[0]["data"]["text"] == "懂了"
    assert result[1]["data"]["id"] == "277"
    parts = await _last_reply_parts(mock_context, _GROUP_CHAT)
    content, media_items = message_parts_to_legacy(parts)
    assert content == "懂了[[xc_media_1]]"
    assert media_items[0]["kind"] == "qq_face"
    assert media_items[0]["face_id"] == "277"
    assert await _await_media_usage(mock_context, "qq_face", "277") == 1
    await settle_chat_background(mock_context, model)


@pytest.mark.asyncio
async def test_smalltalk_reply_applies_only_one_outbound_media_plan(
    mock_context, sample_group_event
):
    media_hash = _seed_emoji_library(
        mock_context, _GROUP_CHAT, name="无语猫猫", marker="[表情包：无语]", tags=["无语"]
    )
    event = chat_message_event(sample_group_event, "你好", _xc_command_forced=True)

    result, model = await _take_turn(
        mock_context, event, reply="懂了\n你看这个\n再说[想发表情：无语]"
    )

    assert mock_context.send_action.await_count == 0
    assert [segment["type"] for segment in result] == ["text", "emoji", "text"]
    assert result[0]["data"]["text"] == "懂了"
    assert result[2]["data"]["text"] == "你看这个\n再说"
    parts = await _last_reply_parts(mock_context, _GROUP_CHAT)
    content, media_items = message_parts_to_legacy(parts)
    assert content == "懂了[[xc_media_1]]\n你看这个\n再说"
    assert [item["kind"] for item in media_items] == ["emoji"]
    # 持久化的文本按行分段：中间那句“你看这个”和“再说”各占一段。
    assert [part["kind"] for part in parts] == ["text", "emoji", "text", "text"]
    assert media_items[0]["media_hash"] == media_hash
    assert await _await_media_usage(mock_context, "emoji", media_hash) == 1
    await settle_chat_background(mock_context, model)


@pytest.mark.asyncio
async def test_smalltalk_face_only_reply_returns_single_face_and_marker_memory(
    mock_context, sample_private_event
):
    event = chat_message_event(sample_private_event, "你好", _xc_command_forced=True)

    result, model = await _take_turn(mock_context, event, reply="[想发QQ表情：微笑]")

    assert mock_context.send_action.await_count == 0
    assert [segment["type"] for segment in result] == ["face"]
    assert result[0]["data"]["id"] == "14"
    parts = await _last_reply_parts(mock_context, _PRIVATE_CHAT)
    content, media_items = message_parts_to_legacy(parts)
    assert content == "[[xc_media_1]]"
    assert media_items[0]["face_id"] == "14"
    assert media_items[0]["mode"] == "text_with_face"
    assert await _await_media_usage(mock_context, "qq_face", "14") == 1
    await settle_chat_background(mock_context, model)


@pytest.mark.asyncio
async def test_smalltalk_does_not_force_reply_when_new_emoji_collected(
    mock_context, sample_group_event
):
    event = chat_message_event(
        sample_group_event,
        "",
        message=[
            {
                "type": "image",
                "data": {"url": "https://example.com/sticker.png", "summary": "[动画表情]"},
            }
        ],
    )

    result, model = await _take_turn(
        mock_context, event, text="", gate=GROUP_GATE_CLOSED, sticker=True
    )

    # 收到的表情被收进了候选库，但没人叫她，是否接话仍交给门控。
    assert event["_xc_new_emoji_count"] == 1
    assert result == []
    decision = bound_state(mock_context).get_reply_gate_decision(_GROUP_CHAT)
    assert decision is not None
    assert (decision.should_reply, decision.reason) == (False, "probability")
    assert model.calls_of("reply") == []
    await settle_chat_background(mock_context, model)
